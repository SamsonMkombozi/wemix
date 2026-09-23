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


def _generate_api_key_raw() -> str:
    import secrets

    return f"wmx_{secrets.token_urlsafe(32)}"


def _hash_api_key(raw_key: str) -> str:
    import hashlib

    return hashlib.sha256(raw_key.encode()).hexdigest()


class APIKey(BaseModel):
    """
    Programmatic access for enterprise/B2B buyers (media houses, verified
    corporate accounts) -- the same access a browser session gets via
    JWT, but usable from a script/newsroom system instead of a browser.
    The raw key is shown to the user exactly once, at creation; only its
    SHA-256 hash is ever stored, the same principle as password storage.
    """

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="api_keys")
    name = models.CharField(max_length=100, help_text="A label the user picks, e.g. 'Newsroom ingest script'.")
    key_hash = models.CharField(max_length=64, unique=True, db_index=True)
    prefix = models.CharField(max_length=12, help_text="First few characters of the raw key, shown in lists so a user can tell keys apart without re-seeing the full value.")
    is_active = models.BooleanField(default=True, db_index=True)
    last_used_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.name} ({self.prefix}...) for {self.user_id}"

    @classmethod
    def create_for_user(cls, user, name: str):
        """Returns (instance, raw_key) -- raw_key is never recoverable
        after this call returns."""
        raw_key = _generate_api_key_raw()
        instance = cls.objects.create(
            user=user, name=name, key_hash=_hash_api_key(raw_key), prefix=raw_key[:12],
        )
        return instance, raw_key


class TermsAcceptance(BaseModel):
    """Records that a user explicitly accepted a specific version of the
    Terms & Conditions -- one row per acceptance event, so re-accepting a
    later version (after the text changes) doesn't overwrite the history
    of what the user agreed to and when. `created_at` (from BaseModel) is
    the acceptance timestamp."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="terms_acceptances"
    )
    version = models.CharField(max_length=20, help_text="Matches settings.TERMS_VERSION at acceptance time.")
    ip_address = models.GenericIPAddressField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["user", "version"])]

    def __str__(self):
        return f"{self.user_id} accepted terms v{self.version} @ {self.created_at:%Y-%m-%d}"


class SupportTicket(BaseModel):
    """A buyer/seller-initiated support request, distinct from a refund
    request (payments.RefundRequest, money-specific) and from
    moderation.UserReport (reporting someone else's misconduct) -- this
    is 'I have a question/complaint/compliment about my own experience
    and want to track a reply', the standard help-desk pattern."""

    class Category(models.TextChoices):
        COMPLAINT = "complaint", "Complaint"
        REFUND_HELP = "refund_help", "Refund help"
        COMPLIMENT = "compliment", "Compliment"
        QUESTION = "question", "Question"
        OTHER = "other", "Other"

    class Status(models.TextChoices):
        OPEN = "open", "Open"
        IN_PROGRESS = "in_progress", "In progress"
        RESOLVED = "resolved", "Resolved"
        CLOSED = "closed", "Closed"

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="support_tickets"
    )
    category = models.CharField(max_length=20, choices=Category.choices, default=Category.QUESTION)
    subject = models.CharField(max_length=255)
    message = models.TextField()
    # No FK to Order/NewsListing -- core must stay decoupled from payments/news
    # at the DB-constraint level (same reasoning as ModerationQueueItem's
    # related_object_id). Optional context only, resolved client-side.
    related_order_id = models.UUIDField(null=True, blank=True)
    status = models.CharField(max_length=15, choices=Status.choices, default=Status.OPEN, db_index=True)
    assigned_to = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="assigned_support_tickets",
    )

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["status", "-created_at"]), models.Index(fields=["user"])]

    def __str__(self):
        return f"[{self.get_category_display()}] {self.subject} ({self.user_id})"


class SupportTicketMessage(BaseModel):
    """One message in a ticket's reply thread -- the buyer's original
    message lives on SupportTicket.message itself, this is every
    follow-up (staff reply or the user adding detail)."""

    ticket = models.ForeignKey(SupportTicket, on_delete=models.CASCADE, related_name="messages")
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="+"
    )
    message = models.TextField()
    is_staff_reply = models.BooleanField(default=False)

    class Meta:
        ordering = ["created_at"]


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
        SUPPORT_TICKET_REPLY = "support_ticket_reply", "Support ticket reply"
        SAVED_SEARCH_MATCH = "saved_search_match", "New listing matches a saved search"
        SUBSCRIPTION_RENEWAL_DUE = "subscription_renewal_due", "Subscription renewal due"
        SUBSCRIPTION_ACTIVATED = "subscription_activated", "Subscription activated"

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
    sms_sent = models.BooleanField(default=False)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["user", "is_read", "-created_at"])]

    def __str__(self):
        return f"{self.get_notification_type_display()} -> {self.user_id}"


class StaticPage(BaseModel):
    """Moderator-editable content for the informational pages every real
    site needs (About Us, Careers, Press Center, FAQ, Privacy Policy,
    Cookie Policy) -- previously unlinked placeholder text in the
    footer with nothing behind it. Public GET by slug only returns a
    page once is_published is set; staff can see/edit drafts."""

    slug = models.SlugField(max_length=60, unique=True, help_text="e.g. 'about', 'careers', 'privacy-policy'.")
    title = models.CharField(max_length=150)
    body = models.TextField(blank=True, help_text="Plain text or simple markdown -- rendered as-is by the frontend.")
    meta_description = models.CharField(max_length=255, blank=True)
    is_published = models.BooleanField(default=False)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
    )

    class Meta:
        ordering = ["slug"]

    def __str__(self):
        return self.title
