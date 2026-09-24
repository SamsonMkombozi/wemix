from django.conf import settings
from django.utils import timezone
from rest_framework import serializers

from .models import CompanyPayoutAccount, CompanyWithdrawalRequest, PayoutAccount, Wallet, WalletTransaction, WithdrawalRequest


class WalletTransactionSerializer(serializers.ModelSerializer):
    class Meta:
        model = WalletTransaction
        fields = ["id", "entry_type", "amount", "balance_after", "reference", "description", "created_at"]


class WalletSerializer(serializers.ModelSerializer):
    available_balance = serializers.SerializerMethodField()

    class Meta:
        model = Wallet
        fields = ["id", "balance", "pending_balance", "available_balance", "currency", "is_frozen"]

    def get_available_balance(self, obj):
        return obj.balance - obj.pending_balance


def _verified_kyc_name(user_id):
    """The user's OCR'd full name from their most recent VERIFIED
    IdentityVerification, or None if they don't have one yet. Real
    mobile-money/bank name-lookup APIs aren't available to this system
    (no such endpoint exists in the Selcom/Nala integrations we have
    credentials for), so this is the closest honest substitute for
    "detect the account holder automatically": the platform already knows
    the user's real name from their own KYC submission, so it uses that
    instead of asking them to retype it -- and since it's the source of
    truth for the payout account's name, a mismatch against KYC becomes
    structurally impossible rather than just a warning score."""
    from accounts.models import IdentityVerification

    verified_kyc = IdentityVerification.objects.filter(
        user_id=user_id, status=IdentityVerification.Status.VERIFIED,
    ).order_by("-reviewed_at").first()
    if not verified_kyc or not verified_kyc.ocr_full_name:
        return None
    return verified_kyc.ocr_full_name.strip()


class PayoutAccountSerializer(serializers.ModelSerializer):
    class Meta:
        model = PayoutAccount
        fields = [
            "id", "account_type", "provider", "account_number", "account_name",
            "is_default", "status", "rejection_reason", "created_at",
        ]
        # account_name is no longer client-writable -- see create() below.
        read_only_fields = ["id", "account_name", "status", "rejection_reason", "created_at"]

    def create(self, validated_data):
        user = self.context["request"].user
        kyc_name = _verified_kyc_name(user.id)
        if not kyc_name:
            raise serializers.ValidationError(
                "Your identity verification (KYC) needs to be approved before you can register a "
                "payout account -- the account holder name is taken automatically from your verified ID, "
                "not typed in, so there's nothing to verify a name against yet."
            )
        validated_data["account_name"] = kyc_name

        # Only one default at a time -- if this is the user's first account,
        # or they explicitly asked for it, make it the default and clear any
        # existing one.
        make_default = validated_data.get("is_default") or not PayoutAccount.objects.filter(user=user).exists()
        if make_default:
            PayoutAccount.objects.filter(user=user, is_default=True).update(is_default=False)
            validated_data["is_default"] = True
        return PayoutAccount.objects.create(user=user, **validated_data)


class PayoutAccountReviewSerializer(serializers.Serializer):
    STATUS_CHOICES = ["verified", "rejected"]
    status = serializers.ChoiceField(choices=STATUS_CHOICES)
    rejection_reason = serializers.CharField(required=False, allow_blank=True, max_length=255)

    def validate(self, attrs):
        if attrs["status"] == "verified":
            account = self.context["account"]
            kyc_name = _verified_kyc_name(account.user_id)
            # Belt-and-suspenders re-check at the moment of final approval,
            # not just at account creation: refuses to verify a payout
            # destination whose name isn't (or is no longer) backed by a
            # verified KYC identity, so no payment can ever be approved to
            # go to an account that doesn't match KYC documents.
            if not kyc_name:
                raise serializers.ValidationError(
                    "This user doesn't have a verified identity (KYC) on file -- can't approve a payout "
                    "account without one."
                )
            if kyc_name != account.account_name.strip():
                raise serializers.ValidationError(
                    "This payout account's name no longer matches the user's current verified KYC name "
                    f"(\"{account.account_name}\" vs. \"{kyc_name}\") -- ask them to re-register it."
                )
        return attrs

    def save(self, **kwargs):
        from django.utils import timezone

        account = self.context["account"]
        account.status = self.validated_data["status"]
        account.reviewed_by = self.context["request"].user
        account.reviewed_at = timezone.now()
        account.rejection_reason = self.validated_data.get("rejection_reason", "")
        account.save(update_fields=["status", "reviewed_by", "reviewed_at", "rejection_reason", "updated_at"])
        return account


class WithdrawalRequestSerializer(serializers.ModelSerializer):
    wallet_owner_username = serializers.CharField(source="wallet.owner.username", read_only=True, default="")
    payout_account_display = serializers.SerializerMethodField()
    # The model field is nullable (backward compatibility with rows that
    # predate this model), but every NEW withdrawal request must specify
    # one -- override DRF's auto-derived optionality.
    payout_account = serializers.PrimaryKeyRelatedField(queryset=PayoutAccount.objects.all(), required=True)

    class Meta:
        model = WithdrawalRequest
        fields = [
            "id",
            "wallet_owner_username",
            "payout_account",
            "payout_account_display",
            "amount",
            "destination_type",
            "destination_details",
            "status",
            "rejection_reason",
            "payout_reference",
            "risk_flagged",
            "created_at",
        ]
        read_only_fields = [
            "id", "destination_type", "destination_details", "status",
            "rejection_reason", "payout_reference", "risk_flagged", "created_at",
        ]

    def get_payout_account_display(self, obj):
        if obj.payout_account:
            return f"{obj.payout_account.get_account_type_display()} ({obj.payout_account.account_number})"
        if obj.destination_details:
            return str(obj.destination_details)
        return ""

    def validate_amount(self, value):
        from datetime import timedelta

        from .services import get_available_balance

        wallet = self.context["wallet"]
        if value <= 0:
            raise serializers.ValidationError("Withdrawal amount must be positive.")
        available = get_available_balance(wallet)
        if value > available:
            held = wallet.pending_balance
            hint = f" ({held} still held under the {settings.WALLET_HOLD_PERIOD_HOURS}h payout hold)" if held > 0 else ""
            raise serializers.ValidationError(f"Withdrawal amount exceeds your available balance of {available}{hint}.")

        # Velocity limits: sum this wallet's non-rejected/failed withdrawals
        # over the trailing day/week/month, including this new one, against
        # the configured caps. Pending requests count too (not just
        # completed ones) so a burst of unreviewed requests can't bypass
        # the limit while waiting in the queue.
        now = timezone.now()
        active_statuses = [
            WithdrawalRequest.Status.REQUESTED, WithdrawalRequest.Status.APPROVED,
            WithdrawalRequest.Status.PROCESSING, WithdrawalRequest.Status.COMPLETED,
        ]
        windows = [
            (timedelta(days=1), settings.WITHDRAWAL_DAILY_LIMIT, "24 hours"),
            (timedelta(days=7), settings.WITHDRAWAL_WEEKLY_LIMIT, "7 days"),
            (timedelta(days=30), settings.WITHDRAWAL_MONTHLY_LIMIT, "30 days"),
        ]
        for window, limit, label in windows:
            from django.db.models import Sum

            existing = WithdrawalRequest.objects.filter(
                wallet=wallet, status__in=active_statuses, created_at__gte=now - window,
            ).aggregate(total=Sum("amount"))["total"] or 0
            if existing + value > limit:
                raise serializers.ValidationError(
                    f"This would exceed the {label} withdrawal limit "
                    f"({existing} already requested/paid + {value} > {limit} limit)."
                )
        return value

    def validate(self, attrs):
        attrs["risk_flagged"] = attrs.get("amount", 0) >= settings.WITHDRAWAL_RISK_FLAG_THRESHOLD
        return attrs

    def validate_payout_account(self, account):
        user = self.context["request"].user
        if account.user_id != user.id:
            raise serializers.ValidationError("This payout account doesn't belong to you.")
        if account.status != PayoutAccount.Status.VERIFIED:
            raise serializers.ValidationError(
                "This payout account hasn't been verified yet. A moderator needs to approve it first."
            )
        return account

    def create(self, validated_data):
        account = validated_data["payout_account"]
        validated_data["destination_type"] = account.account_type
        validated_data["destination_details"] = {
            "provider": account.provider, "account_number": account.account_number,
            "account_name": account.account_name,
        }
        return WithdrawalRequest.objects.create(wallet=self.context["wallet"], **validated_data)


class WithdrawalReviewSerializer(serializers.Serializer):
    STATUS_CHOICES = ["approved", "rejected", "processing", "completed", "failed"]
    status = serializers.ChoiceField(choices=STATUS_CHOICES)
    rejection_reason = serializers.CharField(required=False, allow_blank=True, max_length=255)
    payout_reference = serializers.CharField(required=False, allow_blank=True, max_length=100)

    def validate(self, attrs):
        if attrs["status"] == "rejected" and not attrs.get("rejection_reason"):
            raise serializers.ValidationError({"rejection_reason": "Required when rejecting a withdrawal."})
        return attrs

    def save(self, **kwargs):
        from django.utils import timezone

        from .services import debit_wallet_for_withdrawal

        withdrawal = self.context["withdrawal"]
        reviewer = self.context["request"].user
        new_status = self.validated_data["status"]

        already_completed = withdrawal.status == WithdrawalRequest.Status.COMPLETED
        previous_status = withdrawal.status

        withdrawal.status = new_status
        withdrawal.reviewed_by = reviewer
        withdrawal.reviewed_at = timezone.now()
        withdrawal.rejection_reason = self.validated_data.get("rejection_reason", withdrawal.rejection_reason)
        if self.validated_data.get("payout_reference"):
            withdrawal.payout_reference = self.validated_data["payout_reference"]
        withdrawal.save(update_fields=[
            "status", "reviewed_by", "reviewed_at", "rejection_reason", "payout_reference", "updated_at",
        ])

        if new_status == "completed" and not already_completed:
            debit_wallet_for_withdrawal(withdrawal)

        if new_status in {"completed", "rejected"} and previous_status != new_status and withdrawal.wallet.owner_id:
            from core.models import Notification
            from core.notifications import send_notification

            if new_status == "completed":
                send_notification(
                    user=withdrawal.wallet.owner, notification_type=Notification.NotificationType.WITHDRAWAL_COMPLETED,
                    title="Your withdrawal has been paid out",
                    message=f"{withdrawal.amount} {withdrawal.wallet.currency} has been sent to your {withdrawal.destination_type.replace('_', ' ')} account.",
                    link_path="dashboard.html?tab=withdrawals",
                )
            else:
                send_notification(
                    user=withdrawal.wallet.owner, notification_type=Notification.NotificationType.WITHDRAWAL_REJECTED,
                    title="Your withdrawal request was rejected",
                    message=withdrawal.rejection_reason or "Contact support for details.",
                    link_path="dashboard.html?tab=withdrawals",
                )

        return withdrawal


class CompanyPayoutAccountSerializer(serializers.ModelSerializer):
    class Meta:
        model = CompanyPayoutAccount
        fields = ["id", "account_type", "provider", "account_number", "account_name", "is_active", "created_at"]
        read_only_fields = ["id", "created_at"]

    def create(self, validated_data):
        return CompanyPayoutAccount.objects.create(added_by=self.context["request"].user, **validated_data)


class CompanyWithdrawalRequestSerializer(serializers.ModelSerializer):
    payout_account_display = serializers.SerializerMethodField()
    requested_by_username = serializers.CharField(source="requested_by.username", read_only=True, default="")

    class Meta:
        model = CompanyWithdrawalRequest
        fields = [
            "id", "payout_account", "payout_account_display", "amount", "reason",
            "requested_by_username", "status", "approved_at", "payout_reference",
            "rejection_reason", "created_at",
        ]
        read_only_fields = [
            "id", "status", "approved_at", "payout_reference", "rejection_reason", "created_at",
        ]

    def get_payout_account_display(self, obj):
        return f"{obj.payout_account.get_account_type_display()} ({obj.payout_account.account_number})"

    def validate_payout_account(self, account):
        if not account.is_active:
            raise serializers.ValidationError("This company payout account is inactive.")
        return account

    def validate_amount(self, value):
        if value <= 0:
            raise serializers.ValidationError("Amount must be positive.")
        return value

    def create(self, validated_data):
        return CompanyWithdrawalRequest.objects.create(
            requested_by=self.context["request"].user, **validated_data
        )


class CompanyWithdrawalReviewSerializer(serializers.Serializer):
    """Super-admin-only approve/reject/complete. Kept as a single-step
    action (like the seller WithdrawalReviewSerializer) rather than a
    literal multi-signature workflow -- the 'only company owners may
    approve' requirement is enforced by gating this view to IsSuperAdmin,
    not by requiring N separate approvals."""

    STATUS_CHOICES = ["approved", "completed", "rejected"]
    status = serializers.ChoiceField(choices=STATUS_CHOICES)
    rejection_reason = serializers.CharField(required=False, allow_blank=True, max_length=255)
    payout_reference = serializers.CharField(required=False, allow_blank=True, max_length=100)

    def validate(self, attrs):
        if attrs["status"] == "rejected" and not attrs.get("rejection_reason"):
            raise serializers.ValidationError({"rejection_reason": "Required when rejecting a withdrawal."})
        return attrs

    def save(self, **kwargs):
        from django.utils import timezone

        from .services import debit_platform_wallet_for_company_withdrawal

        withdrawal = self.context["withdrawal"]
        approver = self.context["request"].user
        new_status = self.validated_data["status"]
        already_completed = withdrawal.status == CompanyWithdrawalRequest.Status.COMPLETED

        withdrawal.status = new_status
        withdrawal.approved_by = approver
        withdrawal.approved_at = timezone.now()
        withdrawal.rejection_reason = self.validated_data.get("rejection_reason", withdrawal.rejection_reason)
        if self.validated_data.get("payout_reference"):
            withdrawal.payout_reference = self.validated_data["payout_reference"]
        withdrawal.save(update_fields=[
            "status", "approved_by", "approved_at", "rejection_reason", "payout_reference", "updated_at",
        ])

        if new_status == "completed" and not already_completed:
            debit_platform_wallet_for_company_withdrawal(withdrawal)

        return withdrawal
