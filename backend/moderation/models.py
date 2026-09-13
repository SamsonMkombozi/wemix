from django.conf import settings
from django.db import models

from core.models import BaseModel
from news.models import NewsListing


class ContentScanTarget(models.TextChoices):
    """What kind of platform content is being scanned for circumvention."""

    LISTING_DESCRIPTION = "listing_description", "News listing description"
    LISTING_BODY = "listing_body", "News listing body"
    MESSAGE = "message", "Direct/marketplace message"
    COMMENT = "comment", "Comment"


class AntiCircumventionFlag(BaseModel):
    """
    A single detection event from the anti-circumvention AI: content that
    appears to contain contact details or language suggesting an
    off-platform payment arrangement.
    """

    class DetectedPattern(models.TextChoices):
        PHONE_NUMBER = "phone_number", "Phone number"
        EMAIL = "email", "Email address"
        WHATSAPP = "whatsapp", "WhatsApp number/handle"
        TELEGRAM = "telegram", "Telegram handle"
        SOCIAL_HANDLE = "social_handle", "Social media handle"
        OFF_PLATFORM_LANGUAGE = "off_platform_language", "Off-platform payment language"

    class Severity(models.TextChoices):
        LOW = "low", "Low"
        MEDIUM = "medium", "Medium"
        HIGH = "high", "High"
        CRITICAL = "critical", "Critical"

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="circumvention_flags"
    )
    listing = models.ForeignKey(
        NewsListing, on_delete=models.CASCADE, null=True, blank=True, related_name="circumvention_flags"
    )
    target_type = models.CharField(max_length=30, choices=ContentScanTarget.choices)
    detected_pattern = models.CharField(max_length=30, choices=DetectedPattern.choices, db_index=True)
    matched_text = models.CharField(
        max_length=255, blank=True, help_text="Redacted/masked snippet of the matched content, not full raw text."
    )
    confidence = models.FloatField(default=0)
    severity = models.CharField(max_length=10, choices=Severity.choices, default=Severity.LOW, db_index=True)

    action_taken = models.CharField(
        max_length=20,
        choices=[
            ("none", "No action"),
            ("warning", "Warning issued"),
            ("content_blocked", "Content blocked"),
            ("temporary_suspension", "Temporary suspension"),
            ("permanent_ban", "Permanent ban"),
        ],
        default="none",
    )
    reviewed_by_human = models.BooleanField(default=False)
    reviewer = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="circumvention_reviews",
    )

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["user", "severity"])]


class UserViolationHistory(BaseModel):
    """Rolling per-user tally used to decide graduated enforcement
    (1st offense = warning, repeat offenses escalate)."""

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="violation_history"
    )
    warning_count = models.PositiveIntegerField(default=0)
    suspension_count = models.PositiveIntegerField(default=0)
    last_violation_at = models.DateTimeField(null=True, blank=True)
    is_banned = models.BooleanField(default=False)


class ModerationQueueItem(BaseModel):
    """
    Unified queue for anything awaiting human moderator attention:
    AI-flagged news listings, identity verifications escalated for review,
    and circumvention flags above an auto-action threshold.
    """

    class ItemType(models.TextChoices):
        NEWS_VERIFICATION = "news_verification", "News AI verification"
        IDENTITY_VERIFICATION = "identity_verification", "Identity verification"
        CIRCUMVENTION_FLAG = "circumvention_flag", "Anti-circumvention flag"
        USER_REPORT = "user_report", "User-submitted report"

    class Priority(models.TextChoices):
        LOW = "low", "Low"
        NORMAL = "normal", "Normal"
        HIGH = "high", "High"
        URGENT = "urgent", "Urgent"

    class Status(models.TextChoices):
        OPEN = "open", "Open"
        IN_PROGRESS = "in_progress", "In progress"
        RESOLVED = "resolved", "Resolved"
        DISMISSED = "dismissed", "Dismissed"

    item_type = models.CharField(max_length=30, choices=ItemType.choices, db_index=True)
    # Generic pointer via string id + type rather than a GenericForeignKey,
    # to keep this app decoupled from the others at the DB-constraint level.
    related_object_id = models.UUIDField()
    priority = models.CharField(max_length=10, choices=Priority.choices, default=Priority.NORMAL, db_index=True)
    status = models.CharField(max_length=15, choices=Status.choices, default=Status.OPEN, db_index=True)

    assigned_to = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="assigned_moderation_items",
    )
    resolution_notes = models.TextField(blank=True)
    resolved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-priority", "-created_at"]
        indexes = [models.Index(fields=["item_type", "status"])]


class UserReport(BaseModel):
    """A report filed by one user against a listing or another user."""

    class Reason(models.TextChoices):
        MISINFORMATION = "misinformation", "Misinformation"
        FRAUD = "fraud", "Fraud / scam"
        OFF_PLATFORM_SOLICITATION = "off_platform_solicitation", "Off-platform solicitation"
        HARASSMENT = "harassment", "Harassment"
        COPYRIGHT = "copyright", "Copyright infringement"
        OTHER = "other", "Other"

    reporter = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="filed_reports"
    )
    reported_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="reports_against",
    )
    listing = models.ForeignKey(
        NewsListing, on_delete=models.CASCADE, null=True, blank=True, related_name="reports"
    )
    reason = models.CharField(max_length=30, choices=Reason.choices)
    details = models.TextField(blank=True)

    class Meta:
        ordering = ["-created_at"]
