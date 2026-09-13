"""
manage.py run_image_analysis

Runs image analysis on NewsMedia rows that don't have a result yet.

Usage:
    python manage.py run_image_analysis                # all unanalyzed image media
    python manage.py run_image_analysis --all --force   # re-run on everything
"""

from django.core.management.base import BaseCommand

from news.image_analysis import run_image_analysis
from news.models import NewsMedia


class Command(BaseCommand):
    help = "Run image analysis on media that needs it."

    def add_arguments(self, parser):
        parser.add_argument("--all", action="store_true", help="Include non-image media too (returns a minimal result).")
        parser.add_argument("--force", action="store_true", help="Re-run even if a result already exists.")

    def handle(self, *args, **options):
        qs = NewsMedia.objects.all() if options["all"] else NewsMedia.objects.filter(media_type=NewsMedia.MediaType.IMAGE)
        if not options["force"]:
            qs = qs.filter(analysis_results__isnull=True)
        qs = qs.distinct()

        count = qs.count()
        if count == 0:
            self.stdout.write("Nothing to analyze.")
            return

        self.stdout.write(f"Running image analysis on {count} media item(s)...")
        flagged_count = 0
        for media in qs:
            result = run_image_analysis(media)
            if result.flagged:
                flagged_count += 1
            self.stdout.write(
                f"  {str(media.id)[:8]}  manipulation={result.manipulation_score:>6}  "
                f"flagged={result.flagged}  listing={media.listing.title[:40]}"
            )

        self.stdout.write(self.style.SUCCESS(f"\nDone. {flagged_count}/{count} flagged."))
