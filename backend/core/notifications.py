"""
core/notifications.py — the only way a Notification should be created.

send_notification() creates the in-app row and, by default, also sends
an email using the same console/SMTP backend already configured for
account verification emails (settings.EMAIL_BACKEND) -- no new email
infrastructure needed. Email sending never blocks or breaks the calling
action: if it fails, the in-app notification still exists and the
failure is swallowed the same way accounts/services.py already does for
verification emails.

send_sms=True adds an SMS via core/sms_client.py (Africa's Talking) on
top of that, for the handful of notification types time-sensitive
enough that email alone isn't enough reach in a mobile-money-first
market -- opt-in per call site, not a blanket default, since most
notification types (moderation actions, KYC review outcomes, support
replies) don't need the reach or the per-message cost. Same
never-blocks, never-raises contract as email.
"""

import logging

from django.conf import settings
from django.core.mail import send_mail

logger = logging.getLogger("habari.notifications")


def send_notification(
    *,
    user,
    notification_type: str,
    title: str,
    message: str = "",
    link_path: str = "",
    send_email: bool = True,
    send_sms: bool = False,
):
    from .models import Notification

    notification = Notification.objects.create(
        user=user,
        notification_type=notification_type,
        title=title,
        message=message,
        link_path=link_path,
    )

    if send_email and user.email:
        email_sent = _send_notification_email(user, title, message, link_path)
        if email_sent:
            notification.email_sent = True
            notification.save(update_fields=["email_sent"])

    if send_sms and user.phone_number:
        from .sms_client import send_sms as send_sms_message

        sms_body = f"{title} - {message}" if message else title
        if send_sms_message(user.phone_number, sms_body[:300]):
            notification.sms_sent = True
            notification.save(update_fields=["sms_sent"])

    return notification


def _send_notification_email(user, title: str, message: str, link_path: str) -> bool:
    body_lines = [f"Hello {user.get_full_name() or user.username},", "", title]
    if message:
        body_lines += ["", message]
    if link_path:
        base_url = settings.FRONTEND_BASE_URL.rstrip("/")
        body_lines += ["", f"View it here: {base_url}/{link_path.lstrip('/')}"]

    try:
        from .credentials import get_effective_from_email, get_email_connection

        send_mail(
            subject=f"Habari Platform: {title}",
            message="\n".join(body_lines),
            from_email=get_effective_from_email(),
            recipient_list=[user.email],
            fail_silently=True,
            connection=get_email_connection(),
        )
        return True
    except Exception as exc:
        logger.warning("Failed to send notification email to %s: %s", user.email, exc)
        return False
