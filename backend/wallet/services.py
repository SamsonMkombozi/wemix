from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from .models import Wallet, WalletHold, WalletTransaction


def get_or_create_user_wallet(user) -> Wallet:
    wallet, _ = Wallet.objects.get_or_create(owner=user, defaults={"currency": "TZS"})
    return wallet


def get_or_create_platform_wallet() -> Wallet:
    wallet, _ = Wallet.objects.get_or_create(
        is_platform=True, defaults={"owner": None, "currency": "TZS"}
    )
    return wallet


def _locked_wallet(wallet: Wallet) -> Wallet:
    """Re-fetch a wallet row with SELECT ... FOR UPDATE, for use inside an
    already-open transaction.atomic() block. Must be called after the
    wallet is known to exist (use the get_or_create_* helpers above first,
    outside or before the atomic block that needs the lock)."""
    return Wallet.objects.select_for_update().get(pk=wallet.pk)


def release_matured_holds(wallet: Wallet) -> None:
    """Releases every WalletHold on `wallet` whose hold period has
    elapsed, decrementing pending_balance for each. Called lazily
    wherever a wallet's available-for-withdrawal amount matters (wallet
    view, withdrawal creation, withdrawal completion) rather than
    depending on a cron job -- correct by the time anyone actually checks,
    with no separate scheduler to run. A `release_wallet_holds`
    management command also exists for operators who'd rather run this
    on a schedule instead of (or in addition to) the lazy path.
    """
    with transaction.atomic():
        locked_wallet = Wallet.objects.select_for_update().get(pk=wallet.pk)
        matured = WalletHold.objects.select_for_update().filter(
            wallet=locked_wallet, released=False, matures_at__lte=timezone.now()
        )
        total = sum((h.amount for h in matured), Decimal("0.00"))
        if total:
            locked_wallet.pending_balance = locked_wallet.pending_balance - total
            locked_wallet.save(update_fields=["pending_balance", "updated_at"])
            matured.update(released=True, released_at=timezone.now())
    wallet.refresh_from_db(fields=["pending_balance"])


def get_available_balance(wallet: Wallet) -> Decimal:
    """Balance minus still-held (unmatured) earnings -- what's actually
    eligible to withdraw right now. Releases matured holds first so this
    is always accurate as of "now", not as of whenever holds were last
    checked."""
    release_matured_holds(wallet)
    return wallet.balance - wallet.pending_balance


def credit_wallet_on_sale(order) -> None:
    """
    Splits a paid Order's amount between the seller's wallet and the
    platform wallet, using PLATFORM_COMMISSION_RATE, and writes the
    matching ledger entries. Idempotent: if a WalletTransaction already
    references this order, does nothing (so retried webhook deliveries or
    duplicate calls can't double-credit).

    Must be called with the Order already locked (select_for_update) by
    the caller, and only once the Order is confirmed PAID.
    """
    if WalletTransaction.objects.filter(
        order=order, entry_type=WalletTransaction.EntryType.SALE_CREDIT
    ).exists():
        return  # already credited -- avoid double-paying on webhook retries

    from news.models import NewsListing
    from django.db.models import F

    listing = order.listing

    # Ensure wallet rows exist before we try to lock them.
    get_or_create_user_wallet(listing.seller)
    get_or_create_platform_wallet()

    with transaction.atomic():
        seller_wallet = Wallet.objects.select_for_update().get(owner=listing.seller)
        platform_wallet = Wallet.objects.select_for_update().get(is_platform=True)

        commission_rate = Decimal(str(settings.PLATFORM_COMMISSION_RATE))
        commission = (order.amount * commission_rate).quantize(Decimal("0.01"))
        seller_amount = order.amount - commission

        seller_wallet.balance = seller_wallet.balance + seller_amount
        seller_wallet.pending_balance = seller_wallet.pending_balance + seller_amount
        seller_wallet.save(update_fields=["balance", "pending_balance", "updated_at"])
        sale_credit_txn = WalletTransaction.objects.create(
            wallet=seller_wallet,
            entry_type=WalletTransaction.EntryType.SALE_CREDIT,
            amount=seller_amount,
            balance_after=seller_wallet.balance,
            order=order,
            reference=str(order.id),
            description=f"Sale of '{listing.title}'",
        )
        WalletHold.objects.create(
            wallet=seller_wallet,
            transaction=sale_credit_txn,
            amount=seller_amount,
            matures_at=timezone.now() + timedelta(hours=settings.WALLET_HOLD_PERIOD_HOURS),
        )

        platform_wallet.balance = platform_wallet.balance + commission
        platform_wallet.save(update_fields=["balance", "updated_at"])
        WalletTransaction.objects.create(
            wallet=platform_wallet,
            entry_type=WalletTransaction.EntryType.COMMISSION_CREDIT,
            amount=commission,
            balance_after=platform_wallet.balance,
            order=order,
            reference=str(order.id),
            description=f"Commission on sale of '{listing.title}'",
        )

        update_fields = {"purchase_count": F("purchase_count") + 1}
        if listing.license_type == NewsListing.LicenseType.EXCLUSIVE:
            # An exclusive license means exactly one buyer, ever -- pull
            # the listing from sale immediately so a second buyer can't
            # complete a purchase while this one's payment is still
            # settling. SOLD_OUT already existed as a status choice with
            # nothing ever setting it.
            update_fields["status"] = NewsListing.ListingStatus.SOLD_OUT
        NewsListing.objects.filter(pk=listing.pk).update(**update_fields)


def credit_wallet_for_subscription(subscription_txn) -> None:
    """Subscription-payment equivalent of credit_wallet_on_sale --
    same commission split, same payout hold, just keyed to a
    SubscriptionTransaction instead of an Order (WalletTransaction.order
    is nullable specifically so this doesn't need one). Idempotent the
    same way, keyed off `reference` instead of `order`."""
    if WalletTransaction.objects.filter(
        reference=str(subscription_txn.id), entry_type=WalletTransaction.EntryType.SALE_CREDIT
    ).exists():
        return

    subscription = subscription_txn.subscription
    seller = subscription.seller

    get_or_create_user_wallet(seller)
    get_or_create_platform_wallet()

    with transaction.atomic():
        seller_wallet = Wallet.objects.select_for_update().get(owner=seller)
        platform_wallet = Wallet.objects.select_for_update().get(is_platform=True)

        commission_rate = Decimal(str(settings.PLATFORM_COMMISSION_RATE))
        commission = (subscription_txn.amount * commission_rate).quantize(Decimal("0.01"))
        seller_amount = subscription_txn.amount - commission

        seller_wallet.balance = seller_wallet.balance + seller_amount
        seller_wallet.pending_balance = seller_wallet.pending_balance + seller_amount
        seller_wallet.save(update_fields=["balance", "pending_balance", "updated_at"])
        sale_credit_txn = WalletTransaction.objects.create(
            wallet=seller_wallet,
            entry_type=WalletTransaction.EntryType.SALE_CREDIT,
            amount=seller_amount,
            balance_after=seller_wallet.balance,
            reference=str(subscription_txn.id),
            description=f"Subscription payment from {subscription.subscriber_id}",
        )
        WalletHold.objects.create(
            wallet=seller_wallet,
            transaction=sale_credit_txn,
            amount=seller_amount,
            matures_at=timezone.now() + timedelta(hours=settings.WALLET_HOLD_PERIOD_HOURS),
        )

        platform_wallet.balance = platform_wallet.balance + commission
        platform_wallet.save(update_fields=["balance", "updated_at"])
        WalletTransaction.objects.create(
            wallet=platform_wallet,
            entry_type=WalletTransaction.EntryType.COMMISSION_CREDIT,
            amount=commission,
            balance_after=platform_wallet.balance,
            reference=str(subscription_txn.id),
            description=f"Commission on subscription payment from {subscription.subscriber_id}",
        )


def debit_platform_wallet_for_company_withdrawal(company_withdrawal) -> None:
    """Mirrors debit_wallet_for_withdrawal but against the singleton
    PlatformWallet, for a CompanyWithdrawalRequest (moving commission
    revenue out to a company-owned bank/mobile-money account)."""
    with transaction.atomic():
        platform_wallet = Wallet.objects.select_for_update().get(is_platform=True)
        if platform_wallet.balance < company_withdrawal.amount:
            raise ValueError("Insufficient platform wallet balance for this withdrawal.")

        platform_wallet.balance = platform_wallet.balance - company_withdrawal.amount
        platform_wallet.save(update_fields=["balance", "updated_at"])
        WalletTransaction.objects.create(
            wallet=platform_wallet,
            entry_type=WalletTransaction.EntryType.WITHDRAWAL_DEBIT,
            amount=-company_withdrawal.amount,
            balance_after=platform_wallet.balance,
            reference=str(company_withdrawal.id),
            description=f"Company treasury withdrawal: {company_withdrawal.reason or 'no reason given'}",
        )


def debit_wallet_for_withdrawal(withdrawal_request) -> None:
    """Moves funds from available balance into a pending state is handled
    at the request layer (WithdrawalRequest.status); this writes the
    ledger debit once a withdrawal is confirmed sent to the payout rail."""
    wallet = Wallet.objects.get(pk=withdrawal_request.wallet_id)
    release_matured_holds(wallet)  # re-check: holds may have matured since the request was created

    with transaction.atomic():
        wallet = Wallet.objects.select_for_update().get(pk=withdrawal_request.wallet_id)
        available = wallet.balance - wallet.pending_balance
        if available < withdrawal_request.amount:
            raise ValueError("Insufficient available (non-held) wallet balance for this withdrawal.")

        wallet.balance = wallet.balance - withdrawal_request.amount
        wallet.save(update_fields=["balance", "updated_at"])
        WalletTransaction.objects.create(
            wallet=wallet,
            entry_type=WalletTransaction.EntryType.WITHDRAWAL_DEBIT,
            amount=-withdrawal_request.amount,
            balance_after=wallet.balance,
            reference=str(withdrawal_request.id),
            description="Withdrawal payout",
        )
