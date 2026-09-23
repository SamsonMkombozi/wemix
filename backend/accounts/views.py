from django.core import signing
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import generics, permissions, serializers, status
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView
from rest_framework_simplejwt.views import TokenObtainPairView

from core.models import AuditLog
from core.permissions import IsAdminOrAbove, IsModeratorOrAbove

from .models import CorporateVerification, IdentityVerification, User
from .serializers import (
    AdminCreateUserSerializer,
    AdminSetPasswordSerializer,
    ChangePasswordSerializer,
    Confirm2FASerializer,
    CorporateVerificationReviewSerializer,
    CorporateVerificationSerializer,
    CustomTokenObtainPairSerializer,
    Disable2FASerializer,
    Enable2FASerializer,
    IdentityVerificationReviewSerializer,
    IdentityVerificationSerializer,
    ModeratorUserSerializer,
    PasswordResetConfirmSerializer,
    PasswordResetRequestSerializer,
    RegisterSerializer,
    UserAdminEditSerializer,
    UserSerializer,
)
from .services import (
    blacklist_all_tokens_for_user,
    deactivate_account,
    log_action,
    read_email_verification_token,
    send_password_reset_email,
    send_verification_email,
)


class RegisterView(generics.CreateAPIView):
    """POST /api/accounts/register/  -- public signup."""

    queryset = User.objects.all()
    serializer_class = RegisterSerializer
    permission_classes = [permissions.AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "login"  # reuse the tight login/signup throttle bucket

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = serializer.save()
        send_verification_email(user)
        log_action(
            actor=user,
            action=AuditLog.Action.CREATE,
            target_model="User",
            target_id=user.id,
            description="Account registered.",
            request=request,
        )
        return Response(
            {
                "user": UserSerializer(user).data,
                "detail": "Account created. Check your email to verify your address.",
            },
            status=status.HTTP_201_CREATED,
        )


class VerifyEmailView(APIView):
    """POST /api/accounts/verify-email/  { "token": "..." }"""

    permission_classes = [permissions.AllowAny]

    def post(self, request):
        token = request.data.get("token", "")
        try:
            payload = read_email_verification_token(token)
        except signing.SignatureExpired:
            return Response({"detail": "This verification link has expired."}, status=status.HTTP_400_BAD_REQUEST)
        except signing.BadSignature:
            return Response({"detail": "Invalid verification link."}, status=status.HTTP_400_BAD_REQUEST)

        user = get_object_or_404(User, pk=payload["user_id"], email=payload["email"])
        if not user.is_email_verified:
            user.is_email_verified = True
            user.save(update_fields=["is_email_verified"])
            log_action(actor=user, action=AuditLog.Action.UPDATE, target_model="User", target_id=user.id,
                       description="Email verified.", request=request)
        return Response({"detail": "Email verified successfully."})


class ResendVerificationEmailView(APIView):
    permission_classes = [permissions.IsAuthenticated]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "otp"

    def post(self, request):
        if request.user.is_email_verified:
            return Response({"detail": "Email is already verified."}, status=status.HTTP_400_BAD_REQUEST)
        send_verification_email(request.user)
        return Response({"detail": "Verification email sent."})


class CustomTokenObtainPairView(TokenObtainPairView):
    """POST /api/accounts/login/ -- issues JWT pair; enforces ban/suspension
    and 2FA before doing so."""

    serializer_class = CustomTokenObtainPairSerializer
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "login"

    def post(self, request, *args, **kwargs):
        response = super().post(request, *args, **kwargs)
        if response.status_code == 200:
            log_action(
                actor=request.user if request.user.is_authenticated else None,
                action=AuditLog.Action.LOGIN,
                target_model="User",
                description="Successful login.",
                request=request,
            )
        return response


class MeView(generics.RetrieveUpdateAPIView):
    """GET/PATCH /api/accounts/me/"""

    serializer_class = UserSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_object(self):
        return self.request.user


class ExportMyDataView(APIView):
    """GET /api/accounts/me/export/ -- a JSON download of everything this
    account owns, across every app: profile, KYC/corporate verification
    submissions, listings, orders, wallet transactions, subscriptions,
    reviews, bookmarks, saved searches, support tickets. Self-service
    data portability -- previously the only way to get this out was a
    direct database query on someone's behalf."""

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        from django.http import HttpResponse

        user = request.user

        def as_list(qs, fields):
            return [{f: str(getattr(obj, f)) for f in fields} for obj in qs]

        data = {
            "exported_at": timezone.now().isoformat(),
            "profile": UserSerializer(user).data,
            "identity_verifications": as_list(
                user.identity_verifications.all(), ["id", "id_type", "status", "created_at"]
            ),
            "corporate_verifications": as_list(
                user.corporate_verifications.all(), ["id", "company_name", "status", "created_at"]
            ),
            "listings": as_list(
                user.news_listings.all(), ["id", "title", "slug", "status", "price", "created_at"]
            ),
            "orders": as_list(
                user.orders.all(), ["id", "listing_id", "amount", "status", "created_at"]
            ),
            "reviews_written": as_list(
                user.reviews_written.all(), ["id", "listing_id", "rating", "comment", "created_at"]
            ),
            "bookmarks": as_list(user.bookmarks.all(), ["id", "listing_id", "created_at"]),
            "support_tickets": as_list(
                user.support_tickets.all(), ["id", "category", "subject", "status", "created_at"]
            ),
            "saved_searches": as_list(
                user.saved_searches.all(), ["id", "name", "category_id", "created_at"]
            ),
            "subscriptions": as_list(
                user.subscriptions.all(), ["id", "seller_id", "price", "status", "current_period_end"]
            ),
            "wallet": None,
        }
        if hasattr(user, "wallet"):
            data["wallet"] = {
                "balance": str(user.wallet.balance),
                "currency": user.wallet.currency,
                "transactions": as_list(
                    user.wallet.transactions.all(), ["id", "entry_type", "amount", "description", "created_at"]
                ),
            }

        import json

        response = HttpResponse(json.dumps(data, indent=2, default=str), content_type="application/json")
        response["Content-Disposition"] = f'attachment; filename="wemix-data-export-{user.username}.json"'
        return response


class DeactivateAccountView(APIView):
    """POST /api/accounts/me/deactivate/ -- soft-deletes the current
    user's own account. Requires re-entering the password as a
    confirmation step, same pattern as ChangePasswordView, since this
    immediately blocks all future login."""

    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        password = request.data.get("password", "")
        if not request.user.check_password(password):
            return Response({"password": ["Incorrect password."]}, status=status.HTTP_400_BAD_REQUEST)

        deactivate_account(request.user)
        log_action(
            actor=request.user, action=AuditLog.Action.UPDATE, target_model="User",
            target_id=request.user.id, description="Account deactivated by user.", request=request,
        )
        return Response({"detail": "Your account has been deactivated."})


class ToggleVerifiedBadgeView(APIView):
    """POST /api/accounts/users/<uuid:user_id>/toggle-verified-badge/ --
    moderator/admin only. Distinct editorial trust signal from KYC's
    id_verification_status -- that's "we checked your ID", this is
    "this is a notable/trusted publisher", and only a moderator can set it."""

    permission_classes = [IsModeratorOrAbove]

    def post(self, request, user_id=None):
        target_user = get_object_or_404(User, pk=user_id)
        target_user.is_verified_badge = not target_user.is_verified_badge
        target_user.save(update_fields=["is_verified_badge"])
        log_action(
            actor=request.user, action=AuditLog.Action.UPDATE, target_model="User",
            target_id=target_user.id,
            description=f"Verified badge {'granted to' if target_user.is_verified_badge else 'removed from'} {target_user.username}.",
            request=request,
        )
        return Response({"is_verified_badge": target_user.is_verified_badge})


class ChangePasswordView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        serializer = ChangePasswordSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        request.user.set_password(serializer.validated_data["new_password"])
        request.user.save(update_fields=["password"])
        blacklist_all_tokens_for_user(request.user)
        log_action(actor=request.user, action=AuditLog.Action.UPDATE, target_model="User",
                   target_id=request.user.id, description="Password changed.", request=request)
        return Response({"detail": "Password updated."})


class PasswordResetRequestView(APIView):
    """POST /api/accounts/password-reset/request/  { "email": "..." }

    Always returns the same generic 200 response, whether or not the
    email matches an account -- the alternative (a 404 for unknown
    emails) turns this endpoint into an account-existence oracle, which
    is exactly the kind of thing a password-reset flow must not leak."""

    permission_classes = [permissions.AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "password_reset"

    def post(self, request):
        from django.contrib.auth.tokens import default_token_generator
        from django.utils.encoding import force_bytes
        from django.utils.http import urlsafe_base64_encode

        serializer = PasswordResetRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        generic_response = Response({"detail": "If an account exists for that email, a reset link has been sent."})

        user = User.objects.filter(email__iexact=serializer.validated_data["email"], is_deleted=False).first()
        if not user:
            return generic_response

        uid = urlsafe_base64_encode(force_bytes(user.pk))
        token = default_token_generator.make_token(user)
        send_password_reset_email(user, uid, token)
        # Deliberately no audit-log entry keyed on the target account here
        # -- logging "password reset requested" against an arbitrary
        # email an anonymous caller typed in would let that same
        # unauthenticated caller pollute another user's audit trail.
        # PasswordResetConfirmView logs the action that actually matters:
        # the password changing.
        return generic_response


class PasswordResetConfirmView(APIView):
    """POST /api/accounts/password-reset/confirm/  { "uid", "token", "new_password" }"""

    permission_classes = [permissions.AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "password_reset"

    def post(self, request):
        serializer = PasswordResetConfirmSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = serializer.validated_data["user"]
        user.set_password(serializer.validated_data["new_password"])
        user.save(update_fields=["password"])
        blacklist_all_tokens_for_user(user)
        log_action(actor=user, action=AuditLog.Action.UPDATE, target_model="User", target_id=user.id,
                   description="Password reset via email link.", request=request)
        return Response({"detail": "Password has been reset. You can now log in with your new password."})


class IdentityVerificationCreateListView(generics.ListCreateAPIView):
    """GET: list my own KYC submissions. POST: submit a new one."""

    serializer_class = IdentityVerificationSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        return IdentityVerification.objects.filter(user=self.request.user)

    def get_throttles(self):
        # Only the submission itself is rate-limited -- listing your own
        # past submissions (GET) shouldn't share that tight bucket.
        if self.request.method == "POST":
            self.throttle_scope = "kyc_submit"
            return [ScopedRateThrottle()]
        return super().get_throttles()


class IdentityVerificationReviewQueueView(generics.ListAPIView):
    """GET /api/accounts/kyc/queue/ -- moderator view of pending submissions."""

    serializer_class = IdentityVerificationSerializer
    permission_classes = [IsModeratorOrAbove]

    def get_queryset(self):
        return IdentityVerification.objects.filter(
            status__in=[IdentityVerification.Status.PENDING, IdentityVerification.Status.AWAITING_REVIEW]
        ).select_related("user")


class IdentityVerificationReviewDetailView(generics.UpdateAPIView):
    """PATCH /api/accounts/kyc/<id>/review/ -- moderator approves/rejects."""

    queryset = IdentityVerification.objects.all()
    serializer_class = IdentityVerificationReviewSerializer
    permission_classes = [IsModeratorOrAbove]

    def perform_update(self, serializer):
        instance = serializer.save()
        log_action(
            actor=self.request.user,
            action=AuditLog.Action.APPROVE if instance.status == IdentityVerification.Status.VERIFIED else AuditLog.Action.REJECT,
            target_model="IdentityVerification",
            target_id=instance.id,
            description=f"KYC {instance.status} for user {instance.user_id}.",
            request=self.request,
        )


class CorporateVerificationCreateListView(generics.ListCreateAPIView):
    """GET: list my own KYB submissions. POST: submit a new one -- lets a
    buyer account represent an organization once approved (User.is_corporate)."""

    serializer_class = CorporateVerificationSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        return CorporateVerification.objects.filter(user=self.request.user)


class CorporateVerificationReviewQueueView(generics.ListAPIView):
    """GET /api/accounts/corporate-verifications/queue/ -- moderator queue."""

    serializer_class = CorporateVerificationSerializer
    permission_classes = [IsModeratorOrAbove]

    def get_queryset(self):
        return CorporateVerification.objects.filter(
            status=CorporateVerification.Status.PENDING
        ).select_related("user")


class CorporateVerificationReviewDetailView(generics.UpdateAPIView):
    """PATCH /api/accounts/corporate-verifications/<id>/review/"""

    queryset = CorporateVerification.objects.all()
    serializer_class = CorporateVerificationReviewSerializer
    permission_classes = [IsModeratorOrAbove]

    def perform_update(self, serializer):
        instance = serializer.save()
        log_action(
            actor=self.request.user,
            action=AuditLog.Action.APPROVE if instance.status == CorporateVerification.Status.VERIFIED else AuditLog.Action.REJECT,
            target_model="CorporateVerification",
            target_id=instance.id,
            description=f"Corporate verification {instance.status} for user {instance.user_id}.",
            request=self.request,
        )


class Enable2FAView(generics.RetrieveAPIView):
    """GET /api/accounts/2fa/enable/ -- returns a new secret + QR provisioning URI."""

    serializer_class = Enable2FASerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_object(self):
        return self.request.user


class Confirm2FAView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        serializer = Confirm2FASerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        serializer.save()
        log_action(actor=request.user, action=AuditLog.Action.UPDATE, target_model="User",
                   target_id=request.user.id, description="2FA enabled.", request=request)
        return Response({"detail": "Two-factor authentication enabled."})


class Disable2FAView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        serializer = Disable2FASerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        serializer.save()
        log_action(actor=request.user, action=AuditLog.Action.UPDATE, target_model="User",
                   target_id=request.user.id, description="2FA disabled.", request=request)
        return Response({"detail": "Two-factor authentication disabled."})


class UserListView(generics.ListCreateAPIView):
    """GET /api/accounts/users/ -- moderator/admin: full user directory,
    searchable/filterable.
    POST /api/accounts/users/ -- admin/super_admin only: create a new
    account directly (staff-vouched, pre-verified email)."""

    permission_classes = [IsModeratorOrAbove]

    def get_permissions(self):
        if self.request.method == "POST":
            return [IsAdminOrAbove()]
        return [IsModeratorOrAbove()]

    def get_serializer_class(self):
        if self.request.method == "POST":
            return AdminCreateUserSerializer
        return ModeratorUserSerializer

    def get_queryset(self):
        qs = User.objects.all().order_by("-date_joined")
        params = self.request.query_params
        q = params.get("q")
        role = params.get("role")
        status_filter = params.get("status")  # banned | suspended | deactivated | active

        if q:
            from django.db.models import Q
            qs = qs.filter(Q(username__icontains=q) | Q(email__icontains=q) | Q(organization_name__icontains=q))
        if role:
            qs = qs.filter(role=role)
        if status_filter == "banned":
            qs = qs.filter(is_banned=True)
        elif status_filter == "suspended":
            qs = qs.filter(is_suspended=True)
        elif status_filter == "deactivated":
            qs = qs.filter(is_deleted=True)
        elif status_filter == "active":
            qs = qs.filter(is_banned=False, is_suspended=False, is_deleted=False)
        return qs

    def perform_create(self, serializer):
        role = serializer.validated_data.get("role")
        if role == User.Role.SUPER_ADMIN and self.request.user.role != User.Role.SUPER_ADMIN:
            raise serializers.ValidationError({"role": "Only a super admin can create another super admin account."})
        user = serializer.save()
        log_action(
            actor=self.request.user, action=AuditLog.Action.CREATE, target_model="User", target_id=user.id,
            description=f"Account created directly by admin (role: {user.role}).", request=self.request,
        )


class UserDetailView(generics.RetrieveUpdateAPIView):
    """GET /api/accounts/users/<id>/ -- moderator/admin: full detail.
    PATCH /api/accounts/users/<id>/ -- admin/super_admin only: direct
    edit of core identity fields (name, email, phone, role,
    organization). Role-escalation to super_admin is blocked unless the
    actor is already a super_admin. Self-edits are blocked -- use the
    normal profile endpoint for your own account."""

    queryset = User.objects.all()

    def get_permissions(self):
        if self.request.method in {"PATCH", "PUT"}:
            return [IsAdminOrAbove()]
        return [IsModeratorOrAbove()]

    def get_serializer_class(self):
        if self.request.method in {"PATCH", "PUT"}:
            return UserAdminEditSerializer
        return ModeratorUserSerializer

    def update(self, request, *args, **kwargs):
        target_user = self.get_object()
        if target_user.id == request.user.id:
            return Response({"detail": "Use your own profile settings to edit your own account."}, status=status.HTTP_400_BAD_REQUEST)

        new_role = request.data.get("role")
        if new_role == User.Role.SUPER_ADMIN and request.user.role != User.Role.SUPER_ADMIN:
            return Response({"role": ["Only a super admin can grant super admin access."]}, status=status.HTTP_400_BAD_REQUEST)
        if target_user.role == User.Role.SUPER_ADMIN and request.user.role != User.Role.SUPER_ADMIN:
            return Response({"detail": "Only a super admin can edit another super admin's account."}, status=status.HTTP_400_BAD_REQUEST)

        old_values = {f: getattr(target_user, f) for f in ["first_name", "last_name", "email", "phone_number", "role", "organization_name"]}
        response = super().update(request, *args, **kwargs)
        target_user.refresh_from_db()
        changed = {f: (old, getattr(target_user, f)) for f, old in old_values.items() if old != getattr(target_user, f)}

        log_action(
            actor=request.user, action=AuditLog.Action.UPDATE, target_model="User", target_id=target_user.id,
            description=f"Profile fields edited by admin: {changed}", request=request,
        )
        return Response(ModeratorUserSerializer(target_user).data)


class UserModerateView(APIView):
    """POST /api/accounts/users/<id>/moderate/  {action, notes}
    -- moderator/admin only. Directly issues a warning/suspension/ban to
    any user, without needing a pre-existing AntiCircumventionFlag to
    hang it off of (unlike the flag-review flow). Reuses the exact same
    enforcement logic (violation history, trust score, notification)."""

    permission_classes = [IsModeratorOrAbove]

    def post(self, request, user_id=None):
        from moderation.services import apply_user_enforcement

        action = request.data.get("action")
        valid_actions = {"warning", "temporary_suspension", "permanent_ban"}
        if action not in valid_actions:
            return Response({"action": [f"Must be one of {sorted(valid_actions)}."]}, status=status.HTTP_400_BAD_REQUEST)

        target_user = get_object_or_404(User, pk=user_id)
        if target_user.id == request.user.id:
            return Response({"detail": "You can't moderate your own account."}, status=status.HTTP_400_BAD_REQUEST)

        apply_user_enforcement(target_user, action)
        log_action(
            actor=request.user, action=AuditLog.Action.UPDATE, target_model="User", target_id=target_user.id,
            description=f"Moderator action '{action}' applied directly (not via a flag). Notes: {request.data.get('notes', '')}",
            request=request,
        )
        target_user.refresh_from_db()
        return Response(ModeratorUserSerializer(target_user).data)


class UserForceDeactivateView(APIView):
    """POST /api/accounts/users/<id>/force-deactivate/ -- moderator/admin
    only. Same soft-delete as the user's own self-service deactivation,
    but callable by staff on any account (e.g. in response to a report),
    no password confirmation needed since the moderator isn't proving
    it's their own account."""

    permission_classes = [IsModeratorOrAbove]

    def post(self, request, user_id=None):
        target_user = get_object_or_404(User, pk=user_id)
        if target_user.id == request.user.id:
            return Response({"detail": "Use the self-service deactivation instead."}, status=status.HTTP_400_BAD_REQUEST)
        if target_user.is_deleted:
            return Response({"detail": "Already deactivated."}, status=status.HTTP_400_BAD_REQUEST)

        deactivate_account(target_user)
        log_action(
            actor=request.user, action=AuditLog.Action.UPDATE, target_model="User", target_id=target_user.id,
            description="Account force-deactivated by moderator.", request=request,
        )
        target_user.refresh_from_db()
        return Response(ModeratorUserSerializer(target_user).data)


class UserSetPasswordView(APIView):
    """POST /api/accounts/users/<id>/set-password/  {new_password}
    -- admin/super_admin only. Direct password override -- e.g. helping
    a locked-out user, or responding to a compromised-account report.
    Never returns the password anywhere; notifies the affected user by
    email that an admin changed it, and logs the action (without the
    password value)."""

    permission_classes = [IsAdminOrAbove]

    def post(self, request, user_id=None):
        target_user = get_object_or_404(User, pk=user_id)
        if target_user.id == request.user.id:
            return Response({"detail": "Use your own change-password endpoint instead."}, status=status.HTTP_400_BAD_REQUEST)

        serializer = AdminSetPasswordSerializer(data=request.data, context={"target_user": target_user})
        serializer.is_valid(raise_exception=True)
        target_user.set_password(serializer.validated_data["new_password"])
        target_user.save(update_fields=["password"])

        from core.models import Notification
        from core.notifications import send_notification
        send_notification(
            user=target_user, notification_type=Notification.NotificationType.MODERATION_ACTION,
            title="Your password was changed by an administrator",
            message="If you didn't request this, contact support immediately.",
        )
        log_action(
            actor=request.user, action=AuditLog.Action.UPDATE, target_model="User", target_id=target_user.id,
            description="Password reset directly by admin.", request=request,
        )
        return Response({"detail": "Password updated."})


class UserReverseStatusView(APIView):
    """POST /api/accounts/users/<id>/reverse-status/  {action}
    -- moderator/admin only. Lifts a previously-applied restriction:
    action is one of unsuspend / unban / reactivate. Deliberately
    separate from the one-directional moderate/force-deactivate actions
    so reversing a decision is its own explicit, logged, notified step
    -- not an implicit side effect of some other edit. Does NOT alter
    UserViolationHistory counts -- past violations remain part of the
    record even after a restriction is lifted, the same way a credit
    history keeps old entries after a debt is paid off."""

    permission_classes = [IsModeratorOrAbove]

    def post(self, request, user_id=None):
        action = request.data.get("action")
        valid_actions = {"unsuspend", "unban", "reactivate"}
        if action not in valid_actions:
            return Response({"action": [f"Must be one of {sorted(valid_actions)}."]}, status=status.HTTP_400_BAD_REQUEST)

        target_user = get_object_or_404(User, pk=user_id)
        if target_user.id == request.user.id:
            return Response({"detail": "You can't reverse status on your own account."}, status=status.HTTP_400_BAD_REQUEST)

        from core.models import Notification
        from core.notifications import send_notification

        if action == "unsuspend":
            if not target_user.is_suspended:
                return Response({"detail": "This account is not currently suspended."}, status=status.HTTP_400_BAD_REQUEST)
            target_user.is_suspended = False
            target_user.suspended_until = None
            target_user.save(update_fields=["is_suspended", "suspended_until"])
            title, message = "Your suspension has been lifted", "A moderator has lifted your account suspension early."
        elif action == "unban":
            if not target_user.is_banned:
                return Response({"detail": "This account is not currently banned."}, status=status.HTTP_400_BAD_REQUEST)
            target_user.is_banned = False
            target_user.save(update_fields=["is_banned"])
            title, message = "Your account ban has been lifted", "A moderator has reinstated your account."
        else:  # reactivate
            if not target_user.is_deleted:
                return Response({"detail": "This account is not currently deactivated."}, status=status.HTTP_400_BAD_REQUEST)
            target_user.is_deleted = False
            target_user.deleted_at = None
            target_user.is_active = True
            target_user.save(update_fields=["is_deleted", "deleted_at", "is_active"])
            title, message = "Your account has been reactivated", "A moderator has reactivated your account. You can log in again."

        from accounts.services import recalculate_trust_score
        recalculate_trust_score(target_user)

        send_notification(user=target_user, notification_type=Notification.NotificationType.MODERATION_ACTION, title=title, message=message)
        log_action(
            actor=request.user, action=AuditLog.Action.UPDATE, target_model="User", target_id=target_user.id,
            description=f"Status reversed: {action}.", request=request,
        )
        target_user.refresh_from_db()
        return Response(ModeratorUserSerializer(target_user).data)
