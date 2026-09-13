"""
moderation/services.py — shared enforcement logic used by both the
manual moderator review endpoint (FlagReviewSerializer) and the
automatic anti-circumvention scanner, so a warning/suspension/ban means
exactly the same thing regardless of who (or what) triggered it.
"""

from datetime import timedelta

from django.utils import timezone

from .models import AntiCircumventionFlag, UserViolationHistory


def apply_enforcement_action(
    flag: AntiCircumventionFlag,
    action: str,
    *,
    suspension_days: int = 7,
    reviewed_by_human: bool = True,
    reviewer=None,
) -> AntiCircumventionFlag:
    """Applies `action` (none/warning/content_blocked/temporary_suspension/
    permanent_ban) to the user behind `flag`, updates their rolling
    UserViolationHistory, and marks the flag as actioned. Used both for a
    human moderator's decision and for the scanner's automatic first-line
    enforcement on unambiguous detections."""

    flag.action_taken = action
    flag.reviewed_by_human = reviewed_by_human
    flag.reviewer = reviewer
    flag.save(update_fields=["action_taken", "reviewed_by_human", "reviewer", "updated_at"])

    if action != "none":
        apply_user_enforcement(flag.user, action, suspension_days=suspension_days)

    return flag


def apply_user_enforcement(user, action: str, *, suspension_days: int = 7) -> None:
    """The actual enforcement logic (violation history, account flags,
    trust score, notification) -- independent of any AntiCircumventionFlag,
    so a moderator can apply this directly to a user (e.g. from the Users
    management tab) without needing a pre-existing flag to hang it off of.
    apply_enforcement_action() above is a thin wrapper around this for the
    flag-review flow specifically."""

    history, _ = UserViolationHistory.objects.get_or_create(user=user)

    if action == "warning":
        history.warning_count += 1
    elif action == "temporary_suspension":
        history.suspension_count += 1
        user.is_suspended = True
        user.suspended_until = timezone.now() + timedelta(days=suspension_days)
    elif action == "permanent_ban":
        history.is_banned = True
        user.is_banned = True

    history.last_violation_at = timezone.now()
    history.save()

    if action in {"temporary_suspension", "permanent_ban"}:
        user.save(update_fields=["is_suspended", "suspended_until", "is_banned"])

    from accounts.services import recalculate_trust_score
    recalculate_trust_score(user)

    from core.models import Notification
    from core.notifications import send_notification

    title = message = None
    if action == "warning":
        title = "You received a warning"
        message = "A moderator flagged content on your account for a policy violation."
    elif action == "temporary_suspension":
        title = "Your account has been suspended"
        message = f"Your account is suspended until {user.suspended_until:%Y-%m-%d} due to a policy violation."
    elif action == "permanent_ban":
        title = "Your account has been banned"
        message = "Your account was permanently banned due to a policy violation."
    elif action == "content_blocked":
        title = "Content blocked"
        message = "A listing was blocked for containing prohibited content."

    if title:
        send_notification(user=user, notification_type=Notification.NotificationType.MODERATION_ACTION, title=title, message=message)


def next_graduated_action(user) -> str:
    """Decides the next automatic enforcement step for a user based on
    their violation history so far: first unambiguous offense -> warning,
    second -> temporary suspension, third or more -> permanent ban."""
    history = UserViolationHistory.objects.filter(user=user).first()
    if not history or (history.warning_count == 0 and history.suspension_count == 0):
        return "warning"
    if history.suspension_count == 0:
        return "temporary_suspension"
    return "permanent_ban"
