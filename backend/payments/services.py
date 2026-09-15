import json
import uuid

from django.conf import settings
from django.db import models, transaction
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from news.models import NewsListing
from wallet.models import Wallet, WalletHold, WalletTransaction
from wallet.services import credit_wallet_on_sale

from .models import NalaTransaction, Order, PaymentWebhookLog, SelcomTransaction
from .nala_client import NalaAPIError, NalaClient
from .selcom_client import SelcomAPIError, SelcomClient


def create_order_and_initiate_payment(*, buyer, listing: NewsListing, channel: str, msisdn: str = ""):
    """Creates (or reuses) a pending Order for this buyer+listing, opens a
    new SelcomTransaction attempt, and calls Selcom to get a checkout URL.
    Raises rest_framework.exceptions.ValidationError on any business-rule
    violation, so it's safe to call directly from a DRF view."""

    if listing.status != NewsListing.ListingStatus.PUBLISHED:
        raise ValidationError("This listing is not currently available for purchase.")

    if listing.seller_id == buyer.id:
        raise ValidationError("You cannot purchase your own listing.")

    if Order.objects.filter(buyer=buyer, listing=listing, status=Order.Status.PAID).exists():
        raise ValidationError("You already own this listing.")

    with transaction.atomic():
        order, _ = Order.objects.get_or_create(
            buyer=buyer,
            listing=listing,
            status=Order.Status.PENDING_PAYMENT,
            defaults={"amount": listing.price, "currency": listing.currency},
        )

        selcom_order_id = f"HB{uuid.uuid4().hex[:20].upper()}"
        reference = f"HB-{order.id.hex[:12]}-{int(timezone.now().timestamp())}"

        txn = SelcomTransaction.objects.create(
            order=order,
            selcom_order_id=selcom_order_id,
            reference=reference,
            channel=channel,
            msisdn=msisdn,
            amount=order.amount,
            currency=order.currency,
        )

    client = SelcomClient()
    try:
        response = client.create_order_minimal(
            order_id=selcom_order_id,
            buyer_email=buyer.email,
            buyer_name=buyer.get_full_name() or buyer.username,
            buyer_phone=msisdn or buyer.phone_number,
            amount=order.amount,
            currency=order.currency,
            webhook_url=settings.SELCOM_WEBHOOK_URL,
            redirect_url=f"{settings.FRONTEND_BASE_URL.rstrip('/')}/orders/{order.id}",
        )
    except SelcomAPIError as exc:
        txn.status = SelcomTransaction.Status.FAILED
        txn.failure_reason = str(exc)[:255]
        txn.save(update_fields=["status", "failure_reason", "updated_at"])
        raise ValidationError(f"Payment initiation failed: {exc}")

    txn.selcom_resultcode = str(response.get("resultcode", ""))[:20]
    txn.status = SelcomTransaction.Status.PENDING
    txn.save(update_fields=["status", "selcom_resultcode", "updated_at"])

    payment_gateway_url = None
    data = response.get("data")
    if isinstance(data, list) and data:
        payment_gateway_url = data[0].get("payment_gateway_url")

    return order, txn, payment_gateway_url


def create_nala_collection_and_initiate_payment(*, buyer, listing: NewsListing, currency: str = "USD"):
    """Nala/Rafiki equivalent of create_order_and_initiate_payment(), for
    international buyers paying in a non-TZS currency. Same business-rule
    validation and Order handling as the Selcom path; the only real
    difference is which external client gets called.

    NOTE: amount is currently stored in the listing's native currency
    (usually TZS) unconverted -- real FX conversion to the buyer's
    chosen currency isn't implemented yet, since that also depends on
    Nala's real API (many collection APIs handle FX server-side and
    return the converted amount in their response). Once real API docs
    exist, this almost certainly needs updating to use whatever
    conversion rate/amount Nala's response actually provides, rather
    than assuming a 1:1 amount in the target currency."""

    if listing.status != NewsListing.ListingStatus.PUBLISHED:
        raise ValidationError("This listing is not currently available for purchase.")

    if listing.seller_id == buyer.id:
        raise ValidationError("You cannot purchase your own listing.")

    if Order.objects.filter(buyer=buyer, listing=listing, status=Order.Status.PAID).exists():
        raise ValidationError("You already own this listing.")

    with transaction.atomic():
        order, _ = Order.objects.get_or_create(
            buyer=buyer,
            listing=listing,
            status=Order.Status.PENDING_PAYMENT,
            defaults={"amount": listing.price, "currency": currency, "payment_provider": Order.PaymentProvider.NALA},
        )
        if order.payment_provider != Order.PaymentProvider.NALA:
            order.payment_provider = Order.PaymentProvider.NALA
            order.currency = currency
            order.save(update_fields=["payment_provider", "currency", "updated_at"])

        reference = NalaClient.generate_reference()

        txn = NalaTransaction.objects.create(
            order=order,
            nala_collection_id=f"PENDING-{reference}",  # replaced once the real API responds with its own id
            reference=reference,
            amount=order.amount,
            currency=currency,
        )

    client = NalaClient()
    try:
        response = client.create_collection(
            reference=reference,
            amount=order.amount,
            currency=currency,
            description=f"Habari Platform: {listing.title[:100]}",
            webhook_url=settings.NALA_WEBHOOK_URL,
        )
    except NalaAPIError as exc:
        txn.status = NalaTransaction.Status.FAILED
        txn.failure_reason = str(exc)[:255]
        txn.save(update_fields=["status", "failure_reason", "updated_at"])
        raise ValidationError(f"Payment initiation failed: {exc}")

    # PLACEHOLDER field names -- update once real response schema is known.
    txn.nala_collection_id = str(response.get("id", txn.nala_collection_id))[:100]
    txn.status = NalaTransaction.Status.PENDING
    txn.save(update_fields=["status", "nala_collection_id", "updated_at"])

    payment_gateway_url = response.get("payment_url")  # PLACEHOLDER field name

    return order, txn, payment_gateway_url


def _find_transaction(payload: dict):
    order_id = payload.get("order_id") or payload.get("reference")
    if not order_id:
        return None
    return SelcomTransaction.objects.filter(selcom_order_id=order_id).select_related("order").first()


def handle_selcom_webhook(*, payload: dict, headers: dict, source_ip: str) -> tuple[str, SelcomTransaction | None]:
    """Verifies and processes an inbound Selcom payment webhook. Always
    logs the raw attempt to PaymentWebhookLog regardless of outcome, for
    reconciliation. Returns (verdict, transaction_or_None)."""

    if settings.SELCOM_WEBHOOK_IP_ALLOWLIST and source_ip not in settings.SELCOM_WEBHOOK_IP_ALLOWLIST:
        PaymentWebhookLog.objects.create(
            transaction=None, headers=headers, body=payload,
            verdict=PaymentWebhookLog.Verdict.SIGNATURE_INVALID, source_ip=source_ip,
        )
        return PaymentWebhookLog.Verdict.SIGNATURE_INVALID, None

    txn = _find_transaction(payload)
    if txn is None:
        PaymentWebhookLog.objects.create(
            transaction=None, headers=headers, body=payload,
            verdict=PaymentWebhookLog.Verdict.UNKNOWN_ORDER, source_ip=source_ip,
        )
        return PaymentWebhookLog.Verdict.UNKNOWN_ORDER, None

    client = SelcomClient()
    if not client.verify_webhook_signature(headers, payload):
        PaymentWebhookLog.objects.create(
            transaction=txn, headers=headers, body=payload,
            verdict=PaymentWebhookLog.Verdict.SIGNATURE_INVALID, source_ip=source_ip,
        )
        return PaymentWebhookLog.Verdict.SIGNATURE_INVALID, txn

    if txn.status == SelcomTransaction.Status.SUCCESS:
        # Already processed -- log as duplicate but don't re-credit anything.
        PaymentWebhookLog.objects.create(
            transaction=txn, headers=headers, body=payload,
            verdict=PaymentWebhookLog.Verdict.DUPLICATE, source_ip=source_ip,
        )
        return PaymentWebhookLog.Verdict.DUPLICATE, txn

    result_code = str(payload.get("payment_status", payload.get("result", ""))).upper()

    try:
        with transaction.atomic():
            txn = SelcomTransaction.objects.select_for_update().get(pk=txn.pk)
            order = Order.objects.select_for_update().get(pk=txn.order_id)

            payment_succeeded = result_code in {"COMPLETED", "SUCCESS", "PAID"}

            if payment_succeeded:
                txn.status = SelcomTransaction.Status.SUCCESS
                txn.selcom_transaction_id = str(payload.get("transid", payload.get("transaction_id", "")))[:100]
                txn.completed_at = timezone.now()
                txn.save()

                order.status = Order.Status.PAID
                order.access_granted_at = timezone.now()
                order.save(update_fields=["status", "access_granted_at", "updated_at"])

                credit_wallet_on_sale(order)
            else:
                txn.status = SelcomTransaction.Status.FAILED
                txn.failure_reason = str(payload.get("message", ""))[:255]
                txn.save()

                order.status = Order.Status.FAILED
                order.save(update_fields=["status", "updated_at"])
    except Exception:
        PaymentWebhookLog.objects.create(
            transaction=txn, headers=headers, body=payload,
            verdict=PaymentWebhookLog.Verdict.ERROR, source_ip=source_ip,
        )
        raise

    if payment_succeeded:
        _notify_sale_completed(order)

    PaymentWebhookLog.objects.create(
        transaction=txn, headers=headers, body=payload,
        verdict=PaymentWebhookLog.Verdict.ACCEPTED, source_ip=source_ip,
    )
    return PaymentWebhookLog.Verdict.ACCEPTED, txn


def _find_nala_transaction(payload: dict):
    # PLACEHOLDER field names for how Nala identifies which collection a
    # webhook is about -- update once real payload schema is known.
    collection_id = payload.get("id") or payload.get("reference")
    if not collection_id:
        return None
    return NalaTransaction.objects.filter(
        models.Q(nala_collection_id=collection_id) | models.Q(reference=collection_id)
    ).select_related("order").first()


def handle_nala_webhook(*, payload: dict, raw_body: bytes, signature_header: str, source_ip: str) -> tuple[str, NalaTransaction | None]:
    """Nala/Rafiki equivalent of handle_selcom_webhook(). Same
    idempotency/locking/notification pattern; PLACEHOLDER signature
    verification and payload field names (see nala_client.py)."""

    txn = _find_nala_transaction(payload)
    if txn is None:
        PaymentWebhookLog.objects.create(
            nala_transaction=None, provider=Order.PaymentProvider.NALA, headers={"signature": signature_header}, body=payload,
            verdict=PaymentWebhookLog.Verdict.UNKNOWN_ORDER, source_ip=source_ip,
        )
        return PaymentWebhookLog.Verdict.UNKNOWN_ORDER, None

    client = NalaClient()
    if not client.verify_webhook_signature(raw_body, signature_header):
        PaymentWebhookLog.objects.create(
            nala_transaction=txn, provider=Order.PaymentProvider.NALA, headers={"signature": signature_header}, body=payload,
            verdict=PaymentWebhookLog.Verdict.SIGNATURE_INVALID, source_ip=source_ip,
        )
        return PaymentWebhookLog.Verdict.SIGNATURE_INVALID, txn

    if txn.status == NalaTransaction.Status.SUCCESS:
        PaymentWebhookLog.objects.create(
            nala_transaction=txn, provider=Order.PaymentProvider.NALA, headers={"signature": signature_header}, body=payload,
            verdict=PaymentWebhookLog.Verdict.DUPLICATE, source_ip=source_ip,
        )
        return PaymentWebhookLog.Verdict.DUPLICATE, txn

    # PLACEHOLDER status field/values -- update once real payload schema is known.
    result_status = str(payload.get("status", "")).upper()

    try:
        with transaction.atomic():
            txn = NalaTransaction.objects.select_for_update().get(pk=txn.pk)
            order = Order.objects.select_for_update().get(pk=txn.order_id)

            payment_succeeded = result_status in {"COMPLETED", "SUCCESS", "PAID"}

            if payment_succeeded:
                txn.status = NalaTransaction.Status.SUCCESS
                txn.nala_transaction_id = str(payload.get("transaction_id", ""))[:100]
                txn.completed_at = timezone.now()
                txn.save()

                order.status = Order.Status.PAID
                order.access_granted_at = timezone.now()
                order.save(update_fields=["status", "access_granted_at", "updated_at"])

                credit_wallet_on_sale(order)
            else:
                txn.status = NalaTransaction.Status.FAILED
                txn.failure_reason = str(payload.get("message", ""))[:255]
                txn.save()

                order.status = Order.Status.FAILED
                order.save(update_fields=["status", "updated_at"])
    except Exception:
        PaymentWebhookLog.objects.create(
            nala_transaction=txn, provider=Order.PaymentProvider.NALA, headers={"signature": signature_header}, body=payload,
            verdict=PaymentWebhookLog.Verdict.ERROR, source_ip=source_ip,
        )
        raise

    if payment_succeeded:
        _notify_sale_completed(order)

    PaymentWebhookLog.objects.create(
        nala_transaction=txn, provider=Order.PaymentProvider.NALA, headers={"signature": signature_header}, body=payload,
        verdict=PaymentWebhookLog.Verdict.ACCEPTED, source_ip=source_ip,
    )
    return PaymentWebhookLog.Verdict.ACCEPTED, txn


def _notify_sale_completed(order) -> None:
    from core.models import Notification
    from core.notifications import send_notification

    send_notification(
        user=order.listing.seller, notification_type=Notification.NotificationType.SALE_COMPLETED,
        title=f"You made a sale: '{order.listing.title}'",
        message=f"{order.listing.seller_earning} {order.currency} was credited to your wallet.",
        link_path="dashboard.html?tab=withdrawals",
    )


def process_refund(refund_request) -> None:
    """Reverses a sale's wallet crediting: debits the seller's earning
    and the platform's commission back out via ledger entries, and marks
    the order REFUNDED. Idempotent -- if the order is already refunded,
    does nothing (safe to call again on a retried/duplicated review
    action).

    NOTE: this reverses the internal ledger only. Actually returning the
    buyer's money via Selcom's refund API is a separate integration this
    project doesn't call yet -- record `selcom_refund_reference` on the
    RefundRequest once that's wired up, so there's a place to store it
    when it exists.
    """
    from decimal import Decimal

    with transaction.atomic():
        order = Order.objects.select_for_update().get(pk=refund_request.order_id)
        if order.status == Order.Status.REFUNDED:
            return  # already processed -- avoid double-debiting on a retried action

        listing = order.listing
        seller_wallet = Wallet.objects.select_for_update().get(owner=listing.seller_id)
        platform_wallet = Wallet.objects.select_for_update().get(is_platform=True)

        commission_rate = Decimal(str(settings.PLATFORM_COMMISSION_RATE))
        commission = (refund_request.amount * commission_rate).quantize(Decimal("0.01"))
        seller_amount = refund_request.amount - commission

        seller_wallet.balance = seller_wallet.balance - seller_amount
        seller_wallet.save(update_fields=["balance", "updated_at"])
        WalletTransaction.objects.create(
            wallet=seller_wallet,
            entry_type=WalletTransaction.EntryType.REFUND_DEBIT,
            amount=-seller_amount,
            balance_after=seller_wallet.balance,
            order=order,
            reference=str(refund_request.id),
            description=f"Refund reversal for '{listing.title}'",
        )

        # If the original sale's payout hold hasn't matured yet, release it
        # now rather than letting it mature later -- the money it was
        # holding just left `balance` above, so leaving pending_balance
        # reserved against it would make available-for-withdrawal
        # (balance - pending_balance) undercount, potentially negative.
        original_hold = WalletHold.objects.filter(
            transaction__order=order, transaction__entry_type=WalletTransaction.EntryType.SALE_CREDIT, released=False,
        ).first()
        if original_hold:
            seller_wallet.pending_balance = seller_wallet.pending_balance - original_hold.amount
            seller_wallet.save(update_fields=["pending_balance", "updated_at"])
            original_hold.released = True
            original_hold.released_at = timezone.now()
            original_hold.save(update_fields=["released", "released_at", "updated_at"])

        platform_wallet.balance = platform_wallet.balance - commission
        platform_wallet.save(update_fields=["balance", "updated_at"])
        WalletTransaction.objects.create(
            wallet=platform_wallet,
            entry_type=WalletTransaction.EntryType.REFUND_DEBIT,
            amount=-commission,
            balance_after=platform_wallet.balance,
            order=order,
            reference=str(refund_request.id),
            description=f"Commission reversal for refund of '{listing.title}'",
        )

        order.status = Order.Status.REFUNDED
        order.save(update_fields=["status", "updated_at"])
