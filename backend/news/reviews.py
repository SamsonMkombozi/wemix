"""
news/reviews.py — service layer for the Review/Rating system.

create_review() is the only way a Review should be created (not direct
.objects.create()), because it also keeps two derived numbers in sync:

  1. NewsListing.average_rating / review_count -- denormalized for fast
     list-view rendering, recalculated from this listing's Review rows
     every time one is added.
  2. The seller's User.trust_score -- previously a decorative field
     nothing updated. Now genuinely derived from (a) their average
     rating across all their reviewed listings and (b) their moderation
     violation history (warnings/suspensions pull it down), so it means
     something instead of floating at whatever seed data set it to.
"""

from django.db import transaction
from django.db.models import Avg, Count
from rest_framework.exceptions import ValidationError

from payments.models import Order

from .models import NewsListing, Review


def create_review(*, reviewer, listing: NewsListing, order: Order, rating: int, comment: str = "") -> Review:
    if order.buyer_id != reviewer.id:
        raise ValidationError("You can only review your own orders.")
    if order.listing_id != listing.id:
        raise ValidationError("This order is not for this listing.")
    if order.status != Order.Status.PAID:
        raise ValidationError("You can only review a listing you've actually paid for.")
    if Review.objects.filter(order=order).exists():
        raise ValidationError("You've already reviewed this purchase.")

    with transaction.atomic():
        review = Review.objects.create(
            listing=listing, order=order, reviewer=reviewer, rating=rating, comment=comment,
        )
        recalculate_listing_rating(listing)

    from accounts.services import recalculate_trust_score
    recalculate_trust_score(listing.seller)

    from core.models import Notification
    from core.notifications import send_notification

    stars = "\u2605" * rating + "\u2606" * (5 - rating)
    send_notification(
        user=listing.seller, notification_type=Notification.NotificationType.NEW_REVIEW,
        title=f"New {rating}-star review on '{listing.title}'",
        message=f"{stars} {comment}".strip(),
        link_path=f"listing.html?slug={listing.slug}",
    )

    return review


def recalculate_listing_rating(listing: NewsListing) -> None:
    aggregate = Review.objects.filter(listing=listing).aggregate(avg=Avg("rating"), count=Count("id"))
    listing.average_rating = round(aggregate["avg"], 2) if aggregate["avg"] is not None else None
    listing.review_count = aggregate["count"] or 0
    listing.save(update_fields=["average_rating", "review_count", "updated_at"])
