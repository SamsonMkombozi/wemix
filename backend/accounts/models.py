import uuid

from django.contrib.auth.models import AbstractUser
from django.core.validators import RegexValidator
from django.db import models

from core.models import BaseModel, TimeStampedModel

phone_validator = RegexValidator(
    regex=r"^\+?255\d{9}$|^0\d{9}$",
    message="Enter a valid Tanzanian phone number, e.g. +255712345678 or 0712345678.",
)


class User(AbstractUser):
    """
    Custom user model. We keep Django's username/password/permission
    machinery (AbstractUser) but replace the primary key with a UUID and
    add the platform-specific role + verification fields.
    """

    class Role(models.TextChoices):
        BUYER = "buyer", "Buyer"
        SELLER = "seller", "Seller"
        JOURNALIST = "journalist", "Journalist"
        MEDIA_HOUSE = "media_house", "Media House"
        MODERATOR = "moderator", "Moderator"
        ADMIN = "admin", "Admin"
        SUPER_ADMIN = "super_admin", "Super Admin"

    class VerificationStatus(models.TextChoices):
        UNSUBMITTED = "unsubmitted", "Not submitted"
        PENDING = "pending", "Pending review"
        VERIFIED = "verified", "Verified"
        REJECTED = "rejected", "Rejected"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    email = models.EmailField(unique=True)
    phone_number = models.CharField(max_length=20, validators=[phone_validator], blank=True)
    role = models.CharField(max_length=20, choices=Role.choices, default=Role.BUYER, db_index=True)

    # National ID / KYC summary status. Underlying evidence lives in
    # IdentityVerification below (one user may have several attempts).
    id_verification_status = models.CharField(
        max_length=20,
        choices=VerificationStatus.choices,
        default=VerificationStatus.UNSUBMITTED,
        db_index=True,
    )

    is_email_verified = models.BooleanField(default=False)
    is_phone_verified = models.BooleanField(default=False)

    # Two-factor authentication
    is_2fa_enabled = models.BooleanField(default=False)
    totp_secret = models.CharField(max_length=64, blank=True, editable=False)

    # Trust / moderation
    is_suspended = models.BooleanField(default=False)
    suspended_until = models.DateTimeField(null=True, blank=True)
    is_banned = models.BooleanField(default=False)
    trust_score = models.PositiveSmallIntegerField(
        default=50, help_text="0-100 internal trust score, adjusted by moderation events."
    )

    # Soft delete: real financial records (Order, WalletTransaction) reference
    # User, so hard-deleting a row risks orphaning or cascading through
    # financial history. Deactivation sets is_active=False (blocks login,
    # reusing Django's built-in flag) AND is_deleted=True/deleted_at (an
    # explicit, queryable "this account chose to leave" marker distinct
    # from an admin-disabled account) rather than removing the row.
    is_deleted = models.BooleanField(default=False, db_index=True)
    deleted_at = models.DateTimeField(null=True, blank=True)

    organization_name = models.CharField(
        max_length=255, blank=True, help_text="Media house / organization name, if applicable."
    )

    avatar = models.ImageField(upload_to="avatars/", null=True, blank=True)
    bio = models.CharField(
        max_length=500, blank=True, help_text="Short public profile bio, shown on listing pages."
    )

    website_url = models.URLField(blank=True)
    twitter_url = models.URLField(blank=True)
    facebook_url = models.URLField(blank=True)

    is_verified_badge = models.BooleanField(
        default=False,
        help_text="Editorial trust signal (Twitter/X blue-check style), distinct from "
                   "id_verification_status -- that's 'we checked your ID', this is 'this is a "
                   "notable/trusted publisher'. Moderator/admin-set only, never seller-settable.",
    )

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = ["username"]

    class Meta:
        indexes = [
            models.Index(fields=["role", "id_verification_status"]),
        ]

    def __str__(self):
        return f"{self.get_full_name() or self.username} ({self.get_role_display()})"

    @property
    def can_sell(self) -> bool:
        return (
            self.role in {self.Role.SELLER, self.Role.JOURNALIST, self.Role.MEDIA_HOUSE}
            and self.id_verification_status == self.VerificationStatus.VERIFIED
            and not self.is_banned
            and not self.is_suspended
        )

    @property
    def can_buy(self) -> bool:
        return not self.is_banned and not self.is_suspended


def national_id_upload_path(instance, filename):
    return f"kyc/{instance.user_id}/{uuid.uuid4()}_{filename}"


class IdentityVerification(BaseModel):
    """
    A single National ID verification attempt: front/back ID images, a
    selfie for liveness/face-match, and the OCR + review outcome.
    """

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        OCR_PROCESSING = "ocr_processing", "OCR processing"
        AWAITING_REVIEW = "awaiting_review", "Awaiting human review"
        VERIFIED = "verified", "Verified"
        REJECTED = "rejected", "Rejected"

    class IdType(models.TextChoices):
        NIDA = "nida", "NIDA National ID"
        PASSPORT = "passport", "Passport"
        DRIVERS_LICENSE = "drivers_license", "Driver's License"
        VOTER_ID = "voter_id", "Voter ID"

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="identity_verifications")
    id_type = models.CharField(max_length=20, choices=IdType.choices, default=IdType.NIDA)

    front_image = models.ImageField(upload_to=national_id_upload_path)
    back_image = models.ImageField(upload_to=national_id_upload_path, blank=True, null=True)
    selfie_image = models.ImageField(upload_to=national_id_upload_path)

    # OCR extraction results
    ocr_full_name = models.CharField(max_length=255, blank=True)
    ocr_id_number = models.CharField(max_length=64, blank=True)
    ocr_date_of_birth = models.DateField(null=True, blank=True)
    ocr_raw_payload = models.JSONField(default=dict, blank=True)
    ocr_confidence = models.FloatField(null=True, blank=True)

    # Face match between selfie and ID photo
    face_match_score = models.FloatField(null=True, blank=True)
    liveness_passed = models.BooleanField(null=True, blank=True)

    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING, db_index=True)
    reviewed_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True, related_name="reviewed_verifications"
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)
    rejection_reason = models.CharField(max_length=255, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["user", "status"])]

    def __str__(self):
        return f"ID verification for {self.user_id} [{self.status}]"


class TwoFactorRecoveryCode(BaseModel):
    """One-time-use recovery codes for accounts with 2FA enabled."""

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="recovery_codes")
    code_hash = models.CharField(max_length=128)
    used_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [models.Index(fields=["user", "used_at"])]


class LoginSession(TimeStampedModel):
    """Tracks active sessions/devices for security review and forced logout."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="login_sessions")
    session_key = models.CharField(max_length=64, unique=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.CharField(max_length=512, blank=True)
    is_active = models.BooleanField(default=True)
    last_seen_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
