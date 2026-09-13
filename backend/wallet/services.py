from decimal import Decimal

from django.conf import settings
from django.db import transaction

from .models import Wallet, WalletTransaction


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
        seller_wallet.save(update_fields=["balance", "updated_at"])
        WalletTransaction.objects.create(
            wallet=seller_wallet,
            entry_type=WalletTransaction.EntryType.SALE_CREDIT,
            amount=seller_amount,
            balance_after=seller_wallet.balance,
            order=order,
            reference=str(order.id),
            description=f"Sale of '{listing.title}'",
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

        NewsListing.objects.filter(pk=listing.pk).update(purchase_count=F("purchase_count") + 1)


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
    with transaction.atomic():
        wallet = Wallet.objects.select_for_update().get(pk=withdrawal_request.wallet_id)
        if wallet.balance < withdrawal_request.amount:
            raise ValueError("Insufficient wallet balance for this withdrawal.")

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
