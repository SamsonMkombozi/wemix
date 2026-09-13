"""
manage.py run_ai_verification

Runs the Habari AI Verification Engine on listings that need it -- by
default, every SUBMITTED listing that has no AIVerificationResult yet
(e.g. ones seeded/created before this pipeline existed).

Usage:
    python manage.py run_ai_verification                # all submitted, unverified listings
    python manage.py run_ai_verification --all           # every listing regardless of status
    python manage.py run_ai_verification --slug=my-slug  # a single listing by slug
    python manage.py run_ai_verification --force         # re-run even if a result already exists
"""

from django.core.management.base import BaseCommand

from news.ai_verification import run_ai_verification
from news.models import NewsListing


class Command(BaseCommand):
    help = "Run AI verification on listings that need it."

    def add_arguments(self, parser):
        parser.add_argument("--all", action="store_true", help="Run on every listing, not just submitted ones.")
        parser.add_argument("--slug", type=str, help="Run on a single listing by slug.")
        parser.add_argument("--force", action="store_true", help="Re-run even if a result already exists.")

    def handle(self, *args, **options):
        if options["slug"]:
            qs = NewsListing.objects.filter(slug=options["slug"])
            if not qs.exists():
                self.stderr.write(f"No listing found with slug '{options['slug']}'")
                return
        elif options["all"]:
            qs = NewsListing.objects.all()
        else:
            qs = NewsListing.objects.filter(status=NewsListing.ListingStatus.SUBMITTED)

        if not options["force"]:
            qs = qs.filter(ai_verifications__isnull=True)

        qs = qs.distinct()
        count = qs.count()

        if count == 0:
            self.stdout.write("Nothing to verify.")
            return

        self.stdout.write(f"Running AI verification on {count} listing(s)...")
        outcomes = {}
        for listing in qs:
            result = run_ai_verification(listing)
            outcomes[result.outcome] = outcomes.get(result.outcome, 0) + 1
            self.stdout.write(f"  {listing.title[:60]:60s} -> {result.outcome} (fake_news_score={result.fake_news_score})")

        self.stdout.write(self.style.SUCCESS(f"\nDone. Outcomes: {outcomes}"))
