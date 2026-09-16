"""
Celery tasks for the slow, external-call-bound work that used to run
inline inside the request/response cycle: AI text verification,
anti-circumvention scanning, and image analysis. In the default
CELERY_TASK_ALWAYS_EAGER=True mode (no broker configured -- see
settings.py) these still run synchronously, in-process, before .delay()
returns, so behavior is unchanged from before this file existed. Only a
real broker + running worker makes this genuinely non-blocking.

Every argument is a plain id/primitive, not a model instance or request
object -- Celery task arguments have to survive JSON serialization across
a real broker, which model instances and Django's HttpRequest don't.
"""

from celery import shared_task


@shared_task
def process_listing_submission_task(listing_id: str, actor_id: str, ip_address, user_agent: str) -> None:
    from accounts.models import User
    from core.models import AuditLog
    from moderation.circumvention_scanner import HIGH_CONFIDENCE_PATTERNS, scan_listing

    from .ai_verification import run_ai_verification
    from .models import NewsListing
    from .services import log_news_action

    listing = NewsListing.objects.get(pk=listing_id)
    actor = User.objects.filter(pk=actor_id).first()

    ai_result = run_ai_verification(listing)
    listing.refresh_from_db()
    log_news_action(
        actor=actor, action=AuditLog.Action.UPDATE, listing=listing,
        description=f"AI verification ran: outcome={ai_result.outcome}, fake_news_score={ai_result.fake_news_score}.",
        ip_address=ip_address, user_agent=user_agent,
    )

    circumvention_flags = scan_listing(listing)
    if circumvention_flags:
        log_news_action(
            actor=actor, action=AuditLog.Action.FLAG, listing=listing,
            description=f"Anti-circumvention scan found {len(circumvention_flags)} issue(s): "
                        f"{', '.join(f.detected_pattern for f in circumvention_flags)}.",
            ip_address=ip_address, user_agent=user_agent,
        )
        has_high_confidence_match = any(f.detected_pattern in HIGH_CONFIDENCE_PATTERNS for f in circumvention_flags)
        if has_high_confidence_match:
            listing.status = NewsListing.ListingStatus.SUBMITTED
            listing.verification_status = NewsListing.VerificationStatus.NEEDS_HUMAN_REVIEW
            listing.save(update_fields=["status", "verification_status", "updated_at"])

    from .services_alerts import notify_saved_search_matches

    if listing.status == NewsListing.ListingStatus.PUBLISHED:
        notify_saved_search_matches(listing)


@shared_task
def process_media_upload_task(media_id: str, actor_id: str, ip_address, user_agent: str) -> None:
    from accounts.models import User
    from core.models import AuditLog

    from .image_analysis import run_image_analysis
    from .media_preview import generate_preview
    from .models import NewsListing, NewsMedia
    from .services import log_news_action

    media_obj = NewsMedia.objects.select_related("listing").get(pk=media_id)
    listing = media_obj.listing
    actor = User.objects.filter(pk=actor_id).first()

    generate_preview(media_obj)
    analysis = run_image_analysis(media_obj)
    if analysis.flagged and listing.status == NewsListing.ListingStatus.PUBLISHED:
        listing.status = NewsListing.ListingStatus.SUBMITTED
        listing.verification_status = NewsListing.VerificationStatus.NEEDS_HUMAN_REVIEW
        listing.save(update_fields=["status", "verification_status", "updated_at"])
        log_news_action(
            actor=actor, action=AuditLog.Action.FLAG, listing=listing,
            description=f"Image analysis flagged newly uploaded media (manipulation_score="
                        f"{analysis.manipulation_score}, duplicate_matches={len(analysis.reverse_image_matches)}); "
                        f"listing pulled back for review.",
            ip_address=ip_address, user_agent=user_agent,
        )
