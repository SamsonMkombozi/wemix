import uuid

from django.conf import settings
from django.db import models


class UUIDModel(models.Model):
    """Base model using a UUID primary key instead of an auto-increment int."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    class Meta:
        abstract = True


class TimeStampedModel(models.Model):
    """Base model providing created/updated timestamps."""

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class BaseModel(UUIDModel, TimeStampedModel):
    class Meta:
        abstract = True


class AuditLog(BaseModel):
    """
    Immutable, append-only record of security-relevant actions across the
    platform. Written to by signals / service-layer code in other apps
    rather than being edited directly.
    """

    class Action(models.TextChoices):
        LOGIN = "login", "Login"
        LOGOUT = "logout", "Logout"
        LOGIN_FAILED = "login_failed", "Failed login"
        CREATE = "create", "Create"
        UPDATE = "update", "Update"
        DELETE = "delete", "Delete"
        APPROVE = "approve", "Approve"
        REJECT = "reject", "Reject"
        FLAG = "flag", "Flag"
        SUSPEND = "suspend", "Suspend"
        BAN = "ban", "Ban"
        PAYMENT = "payment", "Payment"
        WITHDRAWAL = "withdrawal", "Withdrawal"
        SETTINGS_CHANGE = "settings_change", "Settings change"
        OTHER = "other", "Other"

    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="audit_logs",
        help_text="User who performed the action. Null for system-initiated actions.",
    )
    action = models.CharField(max_length=32, choices=Action.choices, db_index=True)
    target_model = models.CharField(max_length=100, blank=True)
    target_id = models.CharField(max_length=64, blank=True)
    description = models.TextField(blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.CharField(max_length=512, blank=True)
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["target_model", "target_id"]),
            models.Index(fields=["action", "created_at"]),
        ]

    def __str__(self):
        return f"{self.get_action_display()} by {self.actor_id} @ {self.created_at:%Y-%m-%d %H:%M}"


class PlatformSetting(BaseModel):
    """Simple key/value store for admin-editable platform settings
    (commission rate, feature flags, thresholds, etc.)."""

    key = models.CharField(max_length=100, unique=True)
    value = models.JSONField()
    description = models.CharField(max_length=255, blank=True)

    class Meta:
        ordering = ["key"]

    def __str__(self):
        return self.key


class IntegrationCredential(BaseModel):
    """One field of one integration's configuration (e.g. Selcom's
    api_key), editable live from the Settings tab without a server
    restart or hand-editing .env. Secret fields are encrypted at rest
    (see core/credentials.py) -- this model never stores plaintext
    secrets in the database, and the API never returns a saved secret
    value back to the client, only whether it's set and a masked hint.

    Resolution order (see core/credentials.get_credential): a DB row
    here, if present, overrides the .env/settings.py value -- so
    deployments that haven't touched this UI keep working exactly as
    before purely from environment variables."""

    class Provider(models.TextChoices):
        SELCOM = "selcom", "Selcom"
        NALA = "nala", "Nala/Rafiki"
        AI_VERIFICATION = "ai_verification", "AI Verification"
        EMAIL = "email", "Email (SMTP)"

    provider = models.CharField(max_length=30, choices=Provider.choices, db_index=True)
    field_name = models.CharField(max_length=100, help_text="e.g. api_key, api_secret, vendor_id, base_url, host, port.")
    encrypted_value = models.TextField(blank=True, help_text="Fernet-encrypted. Empty means 'not set / cleared'.")
    is_secret = models.BooleanField(
        default=True,
        help_text="Secret fields (API keys, passwords) are masked in every API response. Non-secret "
                   "fields (a base URL, a port number) are returned in full since there's nothing to protect.",
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )

    class Meta:
        ordering = ["provider", "field_name"]
        constraints = [
            models.UniqueConstraint(fields=["provider", "field_name"], name="unique_credential_field_per_provider"),
        ]

    def __str__(self):
        return f"{self.provider}.{self.field_name}"


class Notification(BaseModel):
    """
    An in-app (and optionally emailed) notification for a user about
    something that happened to their account/content: a listing was
    approved/rejected, KYC was reviewed, a sale came through, a
    withdrawal/refund was processed, a moderation action was taken
    against them, or they got a new review. Created via
    core.notifications.send_notification() -- never construct directly,
    so email delivery stays consistent.
    """

    class NotificationType(models.TextChoices):
        LISTING_APPROVED = "listing_approved", "Listing approved"
        LISTING_REJECTED = "listing_rejected", "Listing rejected"
        LISTING_NEEDS_REVIEW = "listing_needs_review", "Listing needs human review"
        KYC_APPROVED = "kyc_approved", "Identity verification approved"
        KYC_REJECTED = "kyc_rejected", "Identity verification rejected"
        SALE_COMPLETED = "sale_completed", "Sale completed"
        WITHDRAWAL_COMPLETED = "withdrawal_completed", "Withdrawal completed"
        WITHDRAWAL_REJECTED = "withdrawal_rejected", "Withdrawal rejected"
        REFUND_PROCESSED = "refund_processed", "Refund processed"
        MODERATION_ACTION = "moderation_action", "Moderation action taken"
        NEW_REVIEW = "new_review", "New review received"

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="notifications"
    )
    notification_type = models.CharField(max_length=30, choices=NotificationType.choices, db_index=True)
    title = models.CharField(max_length=255)
    message = models.TextField(blank=True)
    link_path = models.CharField(
        max_length=255, blank=True,
        help_text="Relative frontend path to link to, e.g. 'dashboard.html?tab=listings'.",
    )
    is_read = models.BooleanField(default=False, db_index=True)
    read_at = models.DateTimeField(null=True, blank=True)
    email_sent = models.BooleanField(default=False)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["user", "is_read", "-created_at"])]

    def __str__(self):
        return f"{self.get_notification_type_display()} -> {self.user_id}"
