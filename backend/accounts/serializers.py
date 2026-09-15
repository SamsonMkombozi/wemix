import pyotp
from django.conf import settings
from django.contrib.auth import password_validation
from django.db import transaction
from rest_framework import serializers
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer

from .models import CorporateVerification, IdentityVerification, User

# Roles a person can self-select at signup. Staff-side roles (moderator,
# admin, super_admin) are never assignable through the public API — those
# are granted internally (Django admin / a future internal endpoint).
SELF_ASSIGNABLE_ROLES = [User.Role.BUYER, User.Role.SELLER, User.Role.JOURNALIST, User.Role.MEDIA_HOUSE]


class RegisterSerializer(serializers.ModelSerializer):
    password = serializers.CharField(write_only=True, style={"input_type": "password"})
    password_confirm = serializers.CharField(write_only=True, style={"input_type": "password"})
    role = serializers.ChoiceField(choices=[(r.value, r.label) for r in SELF_ASSIGNABLE_ROLES], default=User.Role.BUYER)
    journalist_tier = serializers.ChoiceField(
        choices=[(t.value, t.label) for t in User.JournalistTier], required=False, default=User.JournalistTier.NONE,
    )
    terms_accepted = serializers.BooleanField(write_only=True)

    class Meta:
        model = User
        fields = [
            "username",
            "email",
            "password",
            "password_confirm",
            "first_name",
            "last_name",
            "phone_number",
            "role",
            "organization_name",
            "journalist_tier",
            "terms_accepted",
        ]

    def validate_terms_accepted(self, value):
        if not value:
            raise serializers.ValidationError("You must accept the Terms & Conditions to create an account.")
        return value

    def validate(self, attrs):
        if attrs["password"] != attrs.pop("password_confirm"):
            raise serializers.ValidationError({"password_confirm": "Passwords do not match."})
        password_validation.validate_password(attrs["password"])
        if attrs.get("role") == User.Role.MEDIA_HOUSE and not attrs.get("organization_name"):
            raise serializers.ValidationError(
                {"organization_name": "Organization name is required for a media house account."}
            )
        if attrs.get("journalist_tier") and attrs["journalist_tier"] != User.JournalistTier.NONE and attrs.get("role") not in {
            User.Role.JOURNALIST, User.Role.MEDIA_HOUSE,
        }:
            raise serializers.ValidationError(
                {"journalist_tier": "Only journalist/media house accounts can set a journalist tier."}
            )
        return attrs

    def create(self, validated_data):
        from core.models import TermsAcceptance

        validated_data.pop("terms_accepted")
        password = validated_data.pop("password")
        user = User(**validated_data)
        user.set_password(password)
        user.save()

        request = self.context.get("request")
        TermsAcceptance.objects.create(
            user=user, version=settings.TERMS_VERSION,
            ip_address=request.META.get("REMOTE_ADDR") if request else None,
        )
        return user


class UserSerializer(serializers.ModelSerializer):
    """Read/update serializer for a user's own profile. Trust/verification
    fields are exposed read-only -- they're only ever changed by the
    moderation/KYC workflow, never directly by the user."""

    class Meta:
        model = User
        fields = [
            "id",
            "username",
            "email",
            "first_name",
            "last_name",
            "phone_number",
            "role",
            "organization_name",
            "avatar",
            "bio",
            "website_url",
            "twitter_url",
            "facebook_url",
            "is_verified_badge",
            "id_verification_status",
            "is_email_verified",
            "is_phone_verified",
            "is_2fa_enabled",
            "trust_score",
            "journalist_tier",
            "is_press_credentialed",
            "is_corporate",
            "subscription_price",
            "date_joined",
        ]
        read_only_fields = [
            "id",
            "email",
            "role",
            "id_verification_status",
            "is_verified_badge",
            "is_email_verified",
            "is_phone_verified",
            "is_2fa_enabled",
            "trust_score",
            "journalist_tier",
            "is_press_credentialed",
            "is_corporate",
            "date_joined",
        ]


class ModeratorUserSerializer(serializers.ModelSerializer):
    """Fuller user view for moderator/admin management -- includes
    moderation-relevant fields the self-facing UserSerializer
    deliberately omits (ban/suspension state, soft-delete state,
    email). Read-only in its entirety; changes go through the
    dedicated moderate/deactivate/toggle-verified-badge/edit/
    set-password/reverse-status actions so every change is logged and
    notifies the user, rather than a silent field edit."""

    full_name = serializers.SerializerMethodField()

    def get_full_name(self, obj):
        name = f"{obj.first_name} {obj.last_name}".strip()
        return name or obj.username

    class Meta:
        model = User
        fields = [
            "id", "username", "email", "full_name", "first_name", "last_name",
            "phone_number", "role", "organization_name", "avatar", "bio",
            "website_url", "twitter_url", "facebook_url", "is_verified_badge",
            "id_verification_status", "is_email_verified", "is_phone_verified",
            "trust_score", "is_banned", "is_suspended", "suspended_until",
            "is_deleted", "deleted_at", "is_active", "date_joined",
            "journalist_tier", "is_press_credentialed", "is_corporate",
        ]
        read_only_fields = fields


class UserAdminEditSerializer(serializers.ModelSerializer):
    """Admin-only (not plain moderator) direct edit of a user's core
    identity fields, including role. Deliberately separate from
    ModeratorUserSerializer (read-only) so a field edit is always an
    explicit, logged action through this serializer -- never a side
    effect of some other view accidentally allowing a PATCH.

    Role-escalation safety: enforced in the view, not here, since it
    needs the requesting user (the serializer doesn't have natural
    access to "who is making this change" beyond the target instance).
    """

    class Meta:
        model = User
        fields = ["first_name", "last_name", "email", "phone_number", "role", "organization_name"]

    def validate_email(self, value):
        qs = User.objects.filter(email__iexact=value)
        if self.instance:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise serializers.ValidationError("A user with this email already exists.")
        return value


class AdminCreateUserSerializer(serializers.ModelSerializer):
    """POST /api/accounts/users/ (admin-only) -- staff-initiated account
    creation. Unlike self-registration (RegisterSerializer), this can
    set ANY role directly (subject to the same super_admin safety check
    as edits) and marks the email pre-verified, since an admin is
    vouching for the account rather than the usual email-confirmation
    flow being needed."""

    password = serializers.CharField(write_only=True)

    class Meta:
        model = User
        fields = ["username", "email", "password", "first_name", "last_name", "phone_number", "role", "organization_name"]

    def validate_password(self, value):
        password_validation.validate_password(value)
        return value

    def create(self, validated_data):
        password = validated_data.pop("password")
        user = User(**validated_data, is_email_verified=True, is_active=True)
        user.set_password(password)
        user.save()
        return user


class AdminSetPasswordSerializer(serializers.Serializer):
    new_password = serializers.CharField(write_only=True)

    def validate_new_password(self, value):
        password_validation.validate_password(value, user=self.context.get("target_user"))
        return value


class ChangePasswordSerializer(serializers.Serializer):
    current_password = serializers.CharField(write_only=True)
    new_password = serializers.CharField(write_only=True)

    def validate_current_password(self, value):
        user = self.context["request"].user
        if not user.check_password(value):
            raise serializers.ValidationError("Current password is incorrect.")
        return value

    def validate_new_password(self, value):
        password_validation.validate_password(value, user=self.context["request"].user)
        return value


class IdentityVerificationSerializer(serializers.ModelSerializer):
    def validate_front_image(self, value):
        from core.validators import validate_image_upload
        validate_image_upload(value)
        return value

    def validate_back_image(self, value):
        from core.validators import validate_image_upload
        validate_image_upload(value)
        return value

    def validate_selfie_image(self, value):
        from core.validators import validate_image_upload
        validate_image_upload(value)
        return value

    def validate_press_credential_image(self, value):
        from core.validators import validate_image_upload
        validate_image_upload(value)
        return value

    class Meta:
        model = IdentityVerification
        fields = [
            "id",
            "id_type",
            "front_image",
            "back_image",
            "selfie_image",
            "press_credential_image",
            "status",
            "rejection_reason",
            "ocr_full_name",
            "ocr_id_number",
            "ocr_date_of_birth",
            "ocr_confidence",
            "created_at",
        ]
        read_only_fields = [
            "id", "status", "rejection_reason", "created_at",
            "ocr_full_name", "ocr_id_number", "ocr_date_of_birth", "ocr_confidence",
        ]

    def create(self, validated_data):
        from .ocr import run_ocr_on_verification

        user = self.context["request"].user
        with transaction.atomic():
            verification = IdentityVerification.objects.create(user=user, **validated_data)
            # Every new submission resets the account to "pending" until
            # OCR/human review runs again.
            User.objects.filter(pk=user.pk).update(
                id_verification_status=User.VerificationStatus.PENDING
            )

        # Runs after the transaction commits successfully -- OCR reads
        # the just-saved file from storage, and there's no reason a slow
        # OCR call should hold the DB transaction open. Never blocks
        # submission: run_ocr_on_verification degrades gracefully if
        # tesseract isn't installed.
        run_ocr_on_verification(verification)
        verification.refresh_from_db()
        return verification


class IdentityVerificationReviewSerializer(serializers.ModelSerializer):
    """Used by moderators/admins to approve or reject a submission."""

    class Meta:
        model = IdentityVerification
        fields = ["status", "rejection_reason"]

    def validate_status(self, value):
        allowed = {IdentityVerification.Status.VERIFIED, IdentityVerification.Status.REJECTED}
        if value not in allowed:
            raise serializers.ValidationError("Review outcome must be 'verified' or 'rejected'.")
        return value

    def update(self, instance, validated_data):
        from django.utils import timezone

        request = self.context["request"]
        instance.status = validated_data["status"]
        instance.rejection_reason = validated_data.get("rejection_reason", "")
        instance.reviewed_by = request.user
        instance.reviewed_at = timezone.now()
        instance.save(update_fields=["status", "rejection_reason", "reviewed_by", "reviewed_at", "updated_at"])

        new_user_status = (
            User.VerificationStatus.VERIFIED
            if instance.status == IdentityVerification.Status.VERIFIED
            else User.VerificationStatus.REJECTED
        )
        user_updates = {"id_verification_status": new_user_status}
        # A press credential attached to an approved submission confirms
        # the journalist_tier='professional' claim made at registration --
        # doesn't need its own separate review action.
        if instance.status == IdentityVerification.Status.VERIFIED and instance.press_credential_image:
            user_updates["is_press_credentialed"] = True
        User.objects.filter(pk=instance.user_id).update(**user_updates)

        from core.models import Notification
        from core.notifications import send_notification

        if instance.status == IdentityVerification.Status.VERIFIED:
            send_notification(
                user=instance.user, notification_type=Notification.NotificationType.KYC_APPROVED,
                title="Your identity verification was approved",
                message="You can now sell news on Habari Platform.",
                link_path="dashboard.html?tab=verification",
            )
        else:
            send_notification(
                user=instance.user, notification_type=Notification.NotificationType.KYC_REJECTED,
                title="Your identity verification was rejected",
                message=instance.rejection_reason or "Please review and resubmit your documents.",
                link_path="dashboard.html?tab=verification",
            )

        return instance


class Enable2FASerializer(serializers.Serializer):
    """Step 1: generate a secret + provisioning URI for the user to add to
    their authenticator app. Not yet enabled until confirmed below."""

    def to_representation(self, instance):
        user = self.context["request"].user
        secret = pyotp.random_base32()
        # Stored but is_2fa_enabled stays False until Confirm2FASerializer succeeds.
        user.totp_secret = secret
        user.save(update_fields=["totp_secret"])
        uri = pyotp.totp.TOTP(secret).provisioning_uri(name=user.email, issuer_name="Habari Platform")
        return {"secret": secret, "provisioning_uri": uri}


class Confirm2FASerializer(serializers.Serializer):
    code = serializers.CharField(max_length=6, min_length=6)

    def validate_code(self, value):
        user = self.context["request"].user
        if not user.totp_secret:
            raise serializers.ValidationError("No pending 2FA setup found. Start enrollment again.")
        totp = pyotp.TOTP(user.totp_secret)
        if not totp.verify(value, valid_window=1):
            raise serializers.ValidationError("Invalid or expired code.")
        return value

    def save(self, **kwargs):
        user = self.context["request"].user
        user.is_2fa_enabled = True
        user.save(update_fields=["is_2fa_enabled"])
        return user


class Disable2FASerializer(serializers.Serializer):
    password = serializers.CharField(write_only=True)

    def validate_password(self, value):
        user = self.context["request"].user
        if not user.check_password(value):
            raise serializers.ValidationError("Password is incorrect.")
        return value

    def save(self, **kwargs):
        user = self.context["request"].user
        user.is_2fa_enabled = False
        user.totp_secret = ""
        user.save(update_fields=["is_2fa_enabled", "totp_secret"])
        return user


class CorporateVerificationSerializer(serializers.ModelSerializer):
    def validate_business_license(self, value):
        from core.validators import validate_document_or_image_upload
        validate_document_or_image_upload(value)
        return value

    def validate_company_registration(self, value):
        from core.validators import validate_document_or_image_upload
        validate_document_or_image_upload(value)
        return value

    def validate_tax_document(self, value):
        from core.validators import validate_document_or_image_upload
        validate_document_or_image_upload(value)
        return value

    class Meta:
        model = CorporateVerification
        fields = [
            "id", "company_name", "business_license", "company_registration", "tax_document",
            "status", "rejection_reason", "created_at",
        ]
        read_only_fields = ["id", "status", "rejection_reason", "created_at"]

    def create(self, validated_data):
        return CorporateVerification.objects.create(user=self.context["request"].user, **validated_data)


class CorporateVerificationReviewSerializer(serializers.ModelSerializer):
    class Meta:
        model = CorporateVerification
        fields = ["status", "rejection_reason"]

    def validate_status(self, value):
        allowed = {CorporateVerification.Status.VERIFIED, CorporateVerification.Status.REJECTED}
        if value not in allowed:
            raise serializers.ValidationError("Review outcome must be 'verified' or 'rejected'.")
        return value

    def update(self, instance, validated_data):
        from django.utils import timezone

        request = self.context["request"]
        instance.status = validated_data["status"]
        instance.rejection_reason = validated_data.get("rejection_reason", "")
        instance.reviewed_by = request.user
        instance.reviewed_at = timezone.now()
        instance.save(update_fields=["status", "rejection_reason", "reviewed_by", "reviewed_at", "updated_at"])

        User.objects.filter(pk=instance.user_id).update(
            is_corporate=(instance.status == CorporateVerification.Status.VERIFIED)
        )
        return instance


class CustomTokenObtainPairSerializer(TokenObtainPairSerializer):
    """Adds a 2FA gate and blocks banned/suspended accounts before issuing
    tokens; embeds role/verification status as JWT claims for convenience."""

    two_factor_code = serializers.CharField(required=False, allow_blank=True, write_only=True)

    @classmethod
    def get_token(cls, user):
        token = super().get_token(user)
        token["role"] = user.role
        token["id_verification_status"] = user.id_verification_status
        return token

    def validate(self, attrs):
        two_factor_code = attrs.pop("two_factor_code", "")
        data = super().validate(attrs)

        user = self.user
        if user.is_deleted:
            raise serializers.ValidationError("This account has been deactivated.")
        if user.is_banned:
            raise serializers.ValidationError("This account has been banned.")
        if user.is_suspended:
            raise serializers.ValidationError("This account is currently suspended.")

        if user.is_2fa_enabled:
            if not two_factor_code:
                raise serializers.ValidationError(
                    {"two_factor_code": "Two-factor authentication code is required."}
                )
            if not pyotp.TOTP(user.totp_secret).verify(two_factor_code, valid_window=1):
                raise serializers.ValidationError({"two_factor_code": "Invalid two-factor code."})

        data["user"] = UserSerializer(user).data
        return data
