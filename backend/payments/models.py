import uuid

from django.conf import settings
from django.db import models

from core.models import BaseModel
from news.models import NewsListing


class Order(BaseModel):
    """A buyer's intent to purchase one listing. One Order maps to one
    Transaction attempt sequence (an order can have multiple failed
    transaction attempts before a successful one)."""

    class Status(models.TextChoices):
        PENDING_PAYMENT = "pending_payment", "Pending payment"
        PAID = "paid", "Paid"
        FAILED = "failed", "Failed"
        CANCELLED = "cancelled", "Cancelled"
        REFUNDED = "refunded", "Refunded"

    class PaymentProvider(models.TextChoices):
        SELCOM = "selcom", "Selcom (Tanzania mobile money/cards)"
        NALA = "nala", "Nala/Rafiki (international)"

    buyer = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="orders")
    listing = models.ForeignKey(NewsListing, on_delete=models.PROTECT, related_name="orders")
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    currency = models.CharField(max_length=3, default="TZS")
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING_PAYMENT, db_index=True)
    payment_provider = models.CharField(
        max_length=20, choices=PaymentProvider.choices, default=PaymentProvider.SELCOM,
        help_text="Which payment rail this order was checked out through. Selcom for local "
                   "TZS payments; Nala/Rafiki for international buyers.",
    )

    access_granted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["buyer", "status"])]
        constraints = [
            # a buyer can only hold one non-cancelled/failed order per listing
            models.UniqueConstraint(
                fields=["buyer", "listing"],
                condition=models.Q(status__in=["pending_payment", "paid"]),
                name="unique_active_order_per_buyer_listing",
            )
        ]


class SelcomTransaction(BaseModel):
    """One attempt to pay for an Order via the Selcom API."""

    class Channel(models.TextChoices):
        MOBILE_MONEY = "mobile_money", "Mobile Money"
        CARD = "card", "Card"
        BANK = "bank", "Bank"

    class Status(models.TextChoices):
        INITIATED = "initiated", "Initiated"
        PENDING = "pending", "Pending"
        SUCCESS = "success", "Success"
        FAILED = "failed", "Failed"
        CANCELLED = "cancelled", "Cancelled"
        EXPIRED = "expired", "Expired"

    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name="transactions")
    selcom_order_id = models.CharField(max_length=100, unique=True)
    reference = models.CharField(max_length=100, unique=True, help_text="Our internal idempotency reference.")
    channel = models.CharField(max_length=20, choices=Channel.choices)
    msisdn = models.CharField(max_length=20, blank=True, help_text="Payer phone number for mobile money.")
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    currency = models.CharField(max_length=3, default="TZS")

    status = models.CharField(max_length=20, choices=Status.choices, default=Status.INITIATED, db_index=True)
    selcom_transaction_id = models.CharField(max_length=100, blank=True)
    selcom_resultcode = models.CharField(max_length=20, blank=True)
    failure_reason = models.CharField(max_length=255, blank=True)

    initiated_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["order", "status"])]


class NalaTransaction(BaseModel):
    """One attempt to pay for an Order via Nala/Rafiki -- used for
    international buyers paying in a non-TZS currency. Mirrors
    SelcomTransaction's shape deliberately, so the rest of the system
    (webhook logging, wallet crediting) doesn't need to know which
    provider was used.

    IMPORTANT -- honesty note: this model's fields (external reference
    ID, status values, channel choices) are a best-effort guess based on
    how B2B payment collection APIs conventionally work, NOT confirmed
    against Nala/Rafiki's actual API reference -- their real technical
    docs weren't publicly available when this was built (see
    nala_client.py's docstring for the full explanation). Once real API
    docs/credentials exist, this model's fields may need adjusting to
    match what Nala's API actually returns."""

    class Channel(models.TextChoices):
        CARD = "card", "Card"
        BANK_TRANSFER = "bank_transfer", "Bank Transfer"
        MOBILE_MONEY = "mobile_money", "Mobile Money"
        STABLECOIN = "stablecoin", "Stablecoin (USDC/USDT)"

    class Status(models.TextChoices):
        INITIATED = "initiated", "Initiated"
        PENDING = "pending", "Pending"
        SUCCESS = "success", "Success"
        FAILED = "failed", "Failed"
        CANCELLED = "cancelled", "Cancelled"
        EXPIRED = "expired", "Expired"

    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name="nala_transactions")
    nala_collection_id = models.CharField(max_length=100, unique=True, help_text="Nala's ID for this collection request, once real API integration exists.")
    reference = models.CharField(max_length=100, unique=True, help_text="Our internal idempotency reference.")
    channel = models.CharField(max_length=20, choices=Channel.choices, default=Channel.CARD)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    currency = models.CharField(max_length=3, default="USD")

    status = models.CharField(max_length=20, choices=Status.choices, default=Status.INITIATED, db_index=True)
    nala_transaction_id = models.CharField(max_length=100, blank=True)
    failure_reason = models.CharField(max_length=255, blank=True)

    initiated_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["order", "status"])]


class PaymentWebhookLog(BaseModel):
    """Raw, immutable log of every inbound Selcom webhook call, kept for
    reconciliation and signature-verification auditing regardless of
    whether it was accepted or rejected."""

    class Verdict(models.TextChoices):
        ACCEPTED = "accepted", "Accepted"
        SIGNATURE_INVALID = "signature_invalid", "Invalid signature"
        UNKNOWN_ORDER = "unknown_order", "Unknown order"
        DUPLICATE = "duplicate", "Duplicate (already processed)"
        ERROR = "error", "Processing error"

    transaction = models.ForeignKey(
        SelcomTransaction, on_delete=models.SET_NULL, null=True, blank=True, related_name="webhook_logs"
    )
    nala_transaction = models.ForeignKey(
        NalaTransaction, on_delete=models.SET_NULL, null=True, blank=True, related_name="webhook_logs"
    )
    provider = models.CharField(
        max_length=20, choices=Order.PaymentProvider.choices, default=Order.PaymentProvider.SELCOM,
    )
    headers = models.JSONField(default=dict, blank=True)
    body = models.JSONField(default=dict, blank=True)
    verdict = models.CharField(max_length=25, choices=Verdict.choices, db_index=True)
    source_ip = models.GenericIPAddressField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]


class RefundRequest(BaseModel):
    class Status(models.TextChoices):
        REQUESTED = "requested", "Requested"
        APPROVED = "approved", "Approved"
        PROCESSING = "processing", "Processing"
        COMPLETED = "completed", "Completed"
        REJECTED = "rejected", "Rejected"
        FAILED = "failed", "Failed"

    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name="refund_requests")
    requested_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="refund_requests")
    reason = models.TextField()
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.REQUESTED, db_index=True)

    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="refund_reviews",
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)
    selcom_refund_reference = models.CharField(max_length=100, blank=True)

    class Meta:
        ordering = ["-created_at"]


class Subscription(BaseModel):
    """A buyer's recurring access to everything a given seller publishes
    -- built on top of the existing Follow relationship's intent, but
    actually billed. `current_period_end` is the real access gate (see
    NewsListingDetailSerializer._has_access); `status` is for display/
    record-keeping. Mobile money in Tanzania is a push-payment model, not
    stored-card recurring billing, so renewal is a reminder + a fresh
    manual charge each period (see send_subscription_renewal_reminders),
    not a silent auto-charge this codebase can't actually confirm Selcom
    supports."""

    class Status(models.TextChoices):
        PENDING_PAYMENT = "pending_payment", "Pending payment"
        ACTIVE = "active", "Active"
        CANCELLED = "cancelled", "Cancelled (access continues until period end)"
        EXPIRED = "expired", "Expired"

    subscriber = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="subscriptions")
    seller = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="subscribers_list")
    price = models.DecimalField(max_digits=12, decimal_places=2)
    currency = models.CharField(max_length=3, default="TZS")
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING_PAYMENT, db_index=True)
    current_period_end = models.DateTimeField(null=True, blank=True)
    cancelled_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["subscriber", "seller"],
                condition=models.Q(status__in=["pending_payment", "active"]),
                name="unique_active_subscription_per_pair",
            )
        ]

    def is_currently_active(self) -> bool:
        from django.utils import timezone

        return bool(self.current_period_end and self.current_period_end >= timezone.now())

    def __str__(self):
        return f"{self.subscriber_id} -> {self.seller_id} [{self.status}]"


class SubscriptionTransaction(BaseModel):
    """One Selcom payment attempt for a Subscription -- mirrors
    SelcomTransaction's shape so the rest of the system (webhook
    logging pattern, field names) stays familiar, but kept separate
    since Subscription isn't an Order (no NewsListing involved)."""

    class Channel(models.TextChoices):
        MOBILE_MONEY = "mobile_money", "Mobile Money"
        CARD = "card", "Card"
        BANK = "bank", "Bank"

    class Status(models.TextChoices):
        INITIATED = "initiated", "Initiated"
        PENDING = "pending", "Pending"
        SUCCESS = "success", "Success"
        FAILED = "failed", "Failed"
        CANCELLED = "cancelled", "Cancelled"
        EXPIRED = "expired", "Expired"

    subscription = models.ForeignKey(Subscription, on_delete=models.CASCADE, related_name="transactions")
    selcom_order_id = models.CharField(max_length=100, unique=True)
    reference = models.CharField(max_length=100, unique=True)
    channel = models.CharField(max_length=20, choices=Channel.choices)
    msisdn = models.CharField(max_length=20, blank=True)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    currency = models.CharField(max_length=3, default="TZS")

    status = models.CharField(max_length=20, choices=Status.choices, default=Status.INITIATED, db_index=True)
    selcom_transaction_id = models.CharField(max_length=100, blank=True)
    selcom_resultcode = models.CharField(max_length=20, blank=True)
    failure_reason = models.CharField(max_length=255, blank=True)

    initiated_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["subscription", "status"])]
