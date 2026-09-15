import uuid

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from .models import PaymentWebhookLog, Subscription, SubscriptionTransaction
from .selcom_client import SelcomAPIError, SelcomClient

SUBSCRIPTION_PERIOD_DAYS = 30


def create_subscription_and_initiate_payment(*, subscriber, seller, channel: str, msisdn: str = ""):
    """Mirrors payments/services.py's create_order_and_initiate_payment,
    for a recurring-intent Subscription instead of a one-off listing
    Order. Reused on renewal too -- a lapsed/cancelled subscriber calling
    this again just extends the same Subscription row rather than
    creating a new one, as long as it's not currently mid-payment."""

    if subscriber.id == seller.id:
        raise ValidationError("You cannot subscribe to yourself.")
    if not seller.subscription_price or seller.subscription_price <= 0:
        raise ValidationError("This seller doesn't offer subscriptions.")

    with transaction.atomic():
        subscription = Subscription.objects.filter(
            subscriber=subscriber, seller=seller, status__in=[Subscription.Status.PENDING_PAYMENT, Subscription.Status.ACTIVE],
        ).select_for_update().first()
        if subscription is None:
            subscription = Subscription.objects.filter(subscriber=subscriber, seller=seller).order_by("-created_at").first()
            if subscription and subscription.status in {Subscription.Status.CANCELLED, Subscription.Status.EXPIRED}:
                subscription.status = Subscription.Status.PENDING_PAYMENT
                subscription.price = seller.subscription_price
                subscription.save(update_fields=["status", "price", "updated_at"])
            else:
                subscription = Subscription.objects.create(
                    subscriber=subscriber, seller=seller, price=seller.subscription_price, currency="TZS",
                )
        elif subscription.status == Subscription.Status.ACTIVE and subscription.is_currently_active():
            raise ValidationError("You already have an active subscription to this seller.")

        selcom_order_id = f"HBSUB{uuid.uuid4().hex[:18].upper()}"
        reference = f"HBSUB-{subscription.id.hex[:12]}-{int(timezone.now().timestamp())}"

        txn = SubscriptionTransaction.objects.create(
            subscription=subscription,
            selcom_order_id=selcom_order_id,
            reference=reference,
            channel=channel,
            msisdn=msisdn,
            amount=subscription.price,
            currency=subscription.currency,
        )

    client = SelcomClient()
    try:
        response = client.create_order_minimal(
            order_id=selcom_order_id,
            buyer_email=subscriber.email,
            buyer_name=subscriber.get_full_name() or subscriber.username,
            buyer_phone=msisdn or subscriber.phone_number,
            amount=subscription.price,
            currency=subscription.currency,
            webhook_url=settings.SELCOM_SUBSCRIPTION_WEBHOOK_URL,
            redirect_url=f"{settings.FRONTEND_BASE_URL.rstrip('/')}/subscriptions/{subscription.id}",
        )
    except SelcomAPIError as exc:
        txn.status = SubscriptionTransaction.Status.FAILED
        txn.failure_reason = str(exc)[:255]
        txn.save(update_fields=["status", "failure_reason", "updated_at"])
        raise ValidationError(f"Payment initiation failed: {exc}")

    txn.selcom_resultcode = str(response.get("resultcode", ""))[:20]
    txn.status = SubscriptionTransaction.Status.PENDING
    txn.save(update_fields=["status", "selcom_resultcode", "updated_at"])

    payment_gateway_url = None
    data = response.get("data")
    if isinstance(data, list) and data:
        payment_gateway_url = data[0].get("payment_gateway_url")

    return subscription, txn, payment_gateway_url


def _find_subscription_transaction(payload: dict):
    order_id = payload.get("order_id") or payload.get("reference")
    if not order_id:
        return None
    return SubscriptionTransaction.objects.filter(selcom_order_id=order_id).select_related("subscription").first()


def handle_selcom_subscription_webhook(*, payload: dict, headers: dict, source_ip: str) -> tuple[str, SubscriptionTransaction | None]:
    """Subscription-payment equivalent of handle_selcom_webhook. Separate
    endpoint/function rather than overloading the listing-purchase
    webhook handler, so this entirely new, less battle-tested path can't
    regress the existing, heavily-tested order webhook flow."""

    if settings.SELCOM_WEBHOOK_IP_ALLOWLIST and source_ip not in settings.SELCOM_WEBHOOK_IP_ALLOWLIST:
        PaymentWebhookLog.objects.create(
            headers=headers, body=payload, verdict=PaymentWebhookLog.Verdict.SIGNATURE_INVALID, source_ip=source_ip,
        )
        return PaymentWebhookLog.Verdict.SIGNATURE_INVALID, None

    txn = _find_subscription_transaction(payload)
    if txn is None:
        PaymentWebhookLog.objects.create(
            headers=headers, body=payload, verdict=PaymentWebhookLog.Verdict.UNKNOWN_ORDER, source_ip=source_ip,
        )
        return PaymentWebhookLog.Verdict.UNKNOWN_ORDER, None

    client = SelcomClient()
    if not client.verify_webhook_signature(headers, payload):
        PaymentWebhookLog.objects.create(
            headers=headers, body=payload, verdict=PaymentWebhookLog.Verdict.SIGNATURE_INVALID, source_ip=source_ip,
        )
        return PaymentWebhookLog.Verdict.SIGNATURE_INVALID, txn

    if txn.status == SubscriptionTransaction.Status.SUCCESS:
        PaymentWebhookLog.objects.create(
            headers=headers, body=payload, verdict=PaymentWebhookLog.Verdict.DUPLICATE, source_ip=source_ip,
        )
        return PaymentWebhookLog.Verdict.DUPLICATE, txn

    result_code = str(payload.get("payment_status", payload.get("result", ""))).upper()

    try:
        with transaction.atomic():
            txn = SubscriptionTransaction.objects.select_for_update().get(pk=txn.pk)
            subscription = Subscription.objects.select_for_update().get(pk=txn.subscription_id)

            payment_succeeded = result_code in {"COMPLETED", "SUCCESS", "PAID"}

            if payment_succeeded:
                from datetime import timedelta

                txn.status = SubscriptionTransaction.Status.SUCCESS
                txn.selcom_transaction_id = str(payload.get("transid", payload.get("transaction_id", "")))[:100]
                txn.completed_at = timezone.now()
                txn.save()

                # Extends from "now" on a lapsed/new subscription, or from
                # the current period end on an early renewal -- never
                # shrinks a subscriber's remaining access.
                base = subscription.current_period_end if subscription.is_currently_active() else timezone.now()
                subscription.status = Subscription.Status.ACTIVE
                subscription.current_period_end = base + timedelta(days=SUBSCRIPTION_PERIOD_DAYS)
                subscription.save(update_fields=["status", "current_period_end", "updated_at"])

                from wallet.services import credit_wallet_for_subscription

                credit_wallet_for_subscription(txn)
            else:
                txn.status = SubscriptionTransaction.Status.FAILED
                txn.failure_reason = str(payload.get("message", ""))[:255]
                txn.save()
    except Exception:
        PaymentWebhookLog.objects.create(
            headers=headers, body=payload, verdict=PaymentWebhookLog.Verdict.ERROR, source_ip=source_ip,
        )
        raise

    if payment_succeeded:
        from core.models import Notification
        from core.notifications import send_notification

        send_notification(
            user=subscription.subscriber, notification_type=Notification.NotificationType.SUBSCRIPTION_ACTIVATED,
            title=f"Subscribed to {subscription.seller.username}",
            message=f"Your subscription is active until {subscription.current_period_end:%Y-%m-%d}.",
            link_path="dashboard.html?tab=subscriptions",
        )

    PaymentWebhookLog.objects.create(
        headers=headers, body=payload, verdict=PaymentWebhookLog.Verdict.ACCEPTED, source_ip=source_ip,
    )
    return PaymentWebhookLog.Verdict.ACCEPTED, txn
