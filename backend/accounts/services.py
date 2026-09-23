from django.conf import settings
from django.core import signing
from django.core.mail import send_mail

from core.models import AuditLog

EMAIL_VERIFICATION_SALT = "accounts.email-verification"
EMAIL_VERIFICATION_MAX_AGE = 60 * 60 * 24 * 3  # 3 days


def make_email_verification_token(user) -> str:
    return signing.dumps({"user_id": str(user.id), "email": user.email}, salt=EMAIL_VERIFICATION_SALT)


def read_email_verification_token(token: str, max_age: int = EMAIL_VERIFICATION_MAX_AGE) -> dict:
    """Raises signing.BadSignature / signing.SignatureExpired on failure."""
    return signing.loads(token, salt=EMAIL_VERIFICATION_SALT, max_age=max_age)


def send_password_reset_email(user, uid: str, token: str) -> None:
    """Same silent-degrade contract as send_verification_email: a broken
    mail transport must never surface to the caller (the request-reset
    endpoint always returns a generic "if that email exists" response
    regardless of whether the send actually worked, so there's nothing
    useful to propagate here anyway)."""
    reset_url = f"{settings.FRONTEND_BASE_URL.rstrip('/')}/reset-password.html?uid={uid}&token={token}"
    subject = "Reset your WEMIX password"
    text_body = (
        f"Hello {user.get_full_name() or user.username},\n\n"
        f"Someone requested a password reset for this account. If this was you, open the link "
        f"below to choose a new password:\n{reset_url}\n\n"
        f"This link expires in 24 hours and can only be used once. If you did not request this, "
        f"you can safely ignore this email -- your password has not been changed."
    )
    try:
        from core.credentials import get_effective_from_email, get_email_connection

        send_mail(
            subject=subject,
            message=text_body,
            from_email=get_effective_from_email(),
            recipient_list=[user.email],
            fail_silently=True,
            connection=get_email_connection(),
        )
    except Exception:
        pass


def send_verification_email(user) -> None:
    token = make_email_verification_token(user)
    verify_url = f"{settings.FRONTEND_BASE_URL.rstrip('/')}/verify-email?token={token}"
    subject = "Verify your Habari Platform account"
    text_body = (
        f"Hello {user.get_full_name() or user.username},\n\n"
        f"Please verify your email address by opening the link below:\n{verify_url}\n\n"
        f"This link expires in 3 days. If you did not create this account, ignore this email."
    )
    try:
        from core.credentials import get_effective_from_email, get_email_connection

        send_mail(
            subject=subject,
            message=text_body,
            from_email=get_effective_from_email(),
            recipient_list=[user.email],
            fail_silently=True,
            connection=get_email_connection(),
        )
    except Exception:
        # Never let a broken mail transport break registration.
        pass


def blacklist_all_tokens_for_user(user) -> None:
    """Forces every other session out once a password is changed or reset
    -- otherwise a stolen refresh token would keep working right through
    a password reset meant to lock the account down. Silently a no-op
    for any outstanding token that's already expired/blacklisted (that's
    BlacklistedToken.objects.get_or_create's job, not this function's)."""
    from rest_framework_simplejwt.token_blacklist.models import BlacklistedToken, OutstandingToken

    for outstanding in OutstandingToken.objects.filter(user=user):
        BlacklistedToken.objects.get_or_create(token=outstanding)


def log_action(*, actor, action, target_model="", target_id="", description="", request=None, metadata=None):
    ip = None
    ua = ""
    if request is not None:
        ip = request.META.get("REMOTE_ADDR")
        ua = request.META.get("HTTP_USER_AGENT", "")[:512]
    AuditLog.objects.create(
        actor=actor,
        action=action,
        target_model=target_model,
        target_id=str(target_id),
        description=description,
        ip_address=ip,
        user_agent=ua,
        metadata=metadata or {},
    )


def recalculate_trust_score(user) -> int:
    """
    Derives User.trust_score from real signals instead of leaving it as
    a static number nothing ever updates:

      - Base: the user's average rating across all their listings'
        reviews, scaled from a 1-5 star average to 0-100. A seller with
        no reviews yet starts at a neutral 50 rather than 0 (no data
        shouldn't read as "bad") or 100 (no data shouldn't read as
        "perfect" either).
      - Penalty: their moderation violation history pulls the score
        down -- 5 points per warning, 15 per suspension, capped so a
        single review-average dip can't be the only thing that matters
        but repeat offenses genuinely cost real trust.

    Called after every new review (news/reviews.py) and every
    moderation enforcement action (moderation/services.py) so the
    number stays live rather than drifting stale.
    """
    from django.db.models import Avg

    from moderation.models import UserViolationHistory
    from news.models import NewsListing

    from .models import User

    rating_avg = NewsListing.objects.filter(
        seller=user, review_count__gt=0
    ).aggregate(avg=Avg("average_rating"))["avg"]

    base = round((rating_avg / 5) * 100) if rating_avg is not None else 50

    penalty = 0
    history = UserViolationHistory.objects.filter(user=user).first()
    if history:
        penalty = min(40, history.warning_count * 5 + history.suspension_count * 15)
        if history.is_banned:
            penalty = 100

    score = max(0, min(100, base - penalty))
    User.objects.filter(pk=user.pk).update(trust_score=score)
    return score


def deactivate_account(user) -> None:
    """
    Soft-deletes a user's account: blocks login (is_active=False, the
    same flag Django's own auth backend already checks) and marks
    is_deleted/deleted_at as an explicit, queryable "this account chose
    to leave" record distinct from an admin-disabled account. Never
    deletes the row -- real financial records (Order, WalletTransaction)
    reference User, and hard-deleting risks orphaning or cascading
    through financial history.

    Also unpublishes the user's currently-published listings (buyers who
    already purchased keep their access; new buyers just won't see it in
    the marketplace anymore) rather than leaving live, purchasable
    content attached to a deactivated account.
    """
    from django.utils import timezone

    from news.models import NewsListing

    user.is_deleted = True
    user.deleted_at = timezone.now()
    user.is_active = False
    user.save(update_fields=["is_deleted", "deleted_at", "is_active"])

    NewsListing.objects.filter(
        seller=user, status=NewsListing.ListingStatus.PUBLISHED
    ).update(status=NewsListing.ListingStatus.REMOVED)
