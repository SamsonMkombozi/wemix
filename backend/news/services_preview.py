def consume_free_preview_if_eligible(buyer, listing) -> bool:
    """Metered paywall: a buyer gets settings.FREE_PREVIEW_QUOTA_PER_MONTH
    free unlocks per calendar month, across any listings (not per
    seller). Re-visiting a listing already unlocked this month doesn't
    cost another slot. Returns True if the buyer now has (free) access,
    False if they're out of free slots this month.

    Idempotent via get_or_create keyed on (buyer, listing, month) --
    safe to call more than once per request (NewsListingDetailSerializer
    computes body and body_locked as two separate calls to _has_access).
    """
    from django.conf import settings
    from django.utils import timezone

    from .models import FreePreviewGrant

    if buyer.id == listing.seller_id:
        return False  # irrelevant -- the owner already has access via a different path

    month_key = timezone.now().strftime("%Y-%m")

    if FreePreviewGrant.objects.filter(buyer=buyer, listing=listing, month_key=month_key).exists():
        return True

    used_this_month = FreePreviewGrant.objects.filter(buyer=buyer, month_key=month_key).count()
    if used_this_month >= settings.FREE_PREVIEW_QUOTA_PER_MONTH:
        return False

    FreePreviewGrant.objects.get_or_create(buyer=buyer, listing=listing, month_key=month_key)
    return True
