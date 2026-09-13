"""
manage.py recalculate_trust_scores

Recalculates User.trust_score for every seller-role user from real
signals (review average + moderation violation history), via the same
recalculate_trust_score() function reviews and moderation actions call
automatically. Needed once after installing the Review & Rating system,
since existing users' trust_score was left at whatever a prior value
(seed data, manual edit, etc.) set it to until their own next review or
moderation event fires -- this backfills everyone immediately instead
of waiting.

Usage:
    python manage.py recalculate_trust_scores
    python manage.py recalculate_trust_scores --username=demo_amina
"""

from django.core.management.base import BaseCommand

from accounts.models import User
from accounts.services import recalculate_trust_score


class Command(BaseCommand):
    help = "Recalculate trust_score for seller-role users from real review/violation data."

    def add_arguments(self, parser):
        parser.add_argument("--username", type=str, help="Recalculate a single user by username.")

    def handle(self, *args, **options):
        if options["username"]:
            qs = User.objects.filter(username=options["username"])
            if not qs.exists():
                self.stderr.write(f"No user found with username '{options['username']}'")
                return
        else:
            qs = User.objects.filter(
                role__in=[User.Role.SELLER, User.Role.JOURNALIST, User.Role.MEDIA_HOUSE]
            )

        count = qs.count()
        if count == 0:
            self.stdout.write("No matching users.")
            return

        self.stdout.write(f"Recalculating trust_score for {count} user(s)...")
        for user in qs:
            before = user.trust_score
            after = recalculate_trust_score(user)
            changed = " (changed)" if before != after else ""
            self.stdout.write(f"  {user.username:20s} {before:>3} -> {after:>3}{changed}")

        self.stdout.write(self.style.SUCCESS("\nDone."))
