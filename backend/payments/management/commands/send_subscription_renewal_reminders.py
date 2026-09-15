from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from core.models import Notification
from core.notifications import send_notification
from payments.models import Subscription

REMINDER_WINDOW_DAYS = 3


class Command(BaseCommand):
    help = (
        "Notifies subscribers whose active subscription is about to lapse "
        "(within REMINDER_WINDOW_DAYS) to renew manually -- mobile money "
        "in Tanzania doesn't support silent stored-card recurring charges, "
        "so renewal is always a fresh buyer-approved payment, prompted by "
        "this reminder rather than an automatic charge. Also expires any "
        "subscription whose period has already fully lapsed. Intended to "
        "run daily via cron; nothing else in the app depends on this "
        "running -- access itself is gated by current_period_end directly, "
        "checked lazily wherever it matters."
    )

    def handle(self, *args, **options):
        now = timezone.now()
        reminder_cutoff = now + timedelta(days=REMINDER_WINDOW_DAYS)

        due_soon = Subscription.objects.filter(
            status=Subscription.Status.ACTIVE, cancelled_at__isnull=True,
            current_period_end__gt=now, current_period_end__lte=reminder_cutoff,
        ).select_related("subscriber", "seller")
        reminded = 0
        for sub in due_soon:
            send_notification(
                user=sub.subscriber, notification_type=Notification.NotificationType.SUBSCRIPTION_RENEWAL_DUE,
                title=f"Your subscription to {sub.seller.username} renews soon",
                message=f"Renews on {sub.current_period_end:%Y-%m-%d}. Renew manually from your dashboard to keep access.",
                link_path="dashboard.html?tab=subscriptions",
            )
            reminded += 1

        lapsed = Subscription.objects.filter(
            status__in=[Subscription.Status.ACTIVE, Subscription.Status.CANCELLED], current_period_end__lt=now,
        )
        expired_count = lapsed.update(status=Subscription.Status.EXPIRED)

        self.stdout.write(self.style.SUCCESS(f"Sent {reminded} renewal reminder(s); expired {expired_count} lapsed subscription(s)."))
