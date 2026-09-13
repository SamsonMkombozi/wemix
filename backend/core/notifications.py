"""
core/notifications.py — the only way a Notification should be created.

send_notification() creates the in-app row and, by default, also sends
an email using the same console/SMTP backend already configured for
account verification emails (settings.EMAIL_BACKEND) -- no new email
infrastructure needed. Email sending never blocks or breaks the calling
action: if it fails, the in-app notification still exists and the
failure is swallowed the same way accounts/services.py already does for
verification emails.
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
