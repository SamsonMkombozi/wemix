def notify_saved_search_matches(listing) -> int:
    """Checks every active SavedSearch against a newly-published listing
    and notifies each matching owner (never the listing's own seller,
    even if their own saved search happens to match their own listing).
    Called once, at publish time -- not a live/continuously-updated
    query. Returns how many notifications were sent (mainly for tests)."""
    from core.models import Notification
    from core.notifications import send_notification

    from .models import SavedSearch

    candidates = SavedSearch.objects.filter(is_active=True).exclude(user_id=listing.seller_id).select_related("user")
    sent = 0
    for saved_search in candidates:
        if not saved_search.matches(listing):
            continue
        send_notification(
            user=saved_search.user,
            notification_type=Notification.NotificationType.SAVED_SEARCH_MATCH,
            title=f"New story matches your saved search: '{listing.title}'",
            message=listing.description[:200],
            link_path=f"listing.html?slug={listing.slug}",
            send_sms=True,
        )
        sent += 1
    return sent
