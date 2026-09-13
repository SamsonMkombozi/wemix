"""
manage.py disable_all_2fa

Disables two-factor authentication for every user in the database:
clears is_2fa_enabled and totp_secret, and removes any leftover
TwoFactorRecoveryCode rows (they're meaningless once 2FA is off).

Safe to run multiple times -- it's just a bulk update, not additive.

Usage:
    python manage.py disable_all_2fa
    python manage.py disable_all_2fa --dry-run   # show who would be affected, change nothing
"""

from django.core.management.base import BaseCommand

from accounts.models import TwoFactorRecoveryCode, User


class Command(BaseCommand):
    help = "Disable two-factor authentication for every user."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run", action="store_true",
            help="Show who currently has 2FA enabled without changing anything.",
        )

    def handle(self, *args, **options):
        enabled_users = User.objects.filter(is_2fa_enabled=True)
        count = enabled_users.count()

        if count == 0:
            self.stdout.write("No users currently have 2FA enabled. Nothing to do.")
            return

        self.stdout.write(f"{count} user(s) currently have 2FA enabled:")
        for u in enabled_users:
            self.stdout.write(f"  - {u.username} ({u.email})")

        if options["dry_run"]:
            self.stdout.write(self.style.WARNING("\nDry run -- no changes made."))
            return

        updated = enabled_users.update(is_2fa_enabled=False, totp_secret="")
        deleted, _ = TwoFactorRecoveryCode.objects.all().delete()

        self.stdout.write(self.style.SUCCESS(
            f"\nDisabled 2FA for {updated} user(s). Removed {deleted} leftover recovery code(s)."
        ))
