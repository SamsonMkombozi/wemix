import uuid

from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.utils.text import slugify

from core.models import BaseModel


class Category(BaseModel):
    name = models.CharField(max_length=100, unique=True)
    slug = models.SlugField(max_length=120, unique=True, blank=True)
    description = models.CharField(max_length=255, blank=True)

    class Meta:
        verbose_name_plural = "categories"
        ordering = ["name"]

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = slugify(self.name)
        super().save(*args, **kwargs)

    def __str__(self):
        return self.name


class Tag(BaseModel):
    name = models.CharField(max_length=50, unique=True)
    slug = models.SlugField(max_length=60, unique=True, blank=True)

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = slugify(self.name)
        super().save(*args, **kwargs)

    def __str__(self):
        return self.name


class NewsListing(BaseModel):
    """A single piece of news content offered for sale on the marketplace."""

    class NewsType(models.TextChoices):
        TEXT = "text", "Text News"
        IMAGE = "image", "Image News"
        VIDEO = "video", "Video News"
        BREAKING = "breaking", "Breaking News"
        INVESTIGATIVE = "investigative", "Investigative Report"
        POLITICAL = "political", "Political News"
        BUSINESS = "business", "Business News"
        SPORTS = "sports", "Sports News"

    class VerificationStatus(models.TextChoices):
        PENDING = "pending", "Pending review"
        VERIFIED = "verified", "Verified"
        PARTIALLY_VERIFIED = "partially_verified", "Partially verified"
        NEEDS_HUMAN_REVIEW = "needs_human_review", "Needs human review"
        REJECTED = "rejected", "Rejected"

    class ListingStatus(models.TextChoices):
        DRAFT = "draft", "Draft"
        SUBMITTED = "submitted", "Submitted"
        PUBLISHED = "published", "Published"
        SUSPENDED = "suspended", "Suspended"
        REMOVED = "removed", "Removed"
        SOLD_OUT = "sold_out", "Sold out"

    seller = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="news_listings"
    )
    title = models.CharField(max_length=255)
    slug = models.SlugField(max_length=280, unique=True, blank=True)
    description = models.TextField()
    body = models.TextField(help_text="Full article body, only released to buyers after purchase.")

    news_type = models.CharField(max_length=20, choices=NewsType.choices, db_index=True)
    category = models.ForeignKey(Category, on_delete=models.PROTECT, related_name="listings")
    tags = models.ManyToManyField(Tag, blank=True, related_name="listings")
    location = models.CharField(max_length=255, blank=True)

    price = models.DecimalField(max_digits=12, decimal_places=2)
    currency = models.CharField(max_length=3, default="TZS")

    verification_status = models.CharField(
        max_length=25, choices=VerificationStatus.choices, default=VerificationStatus.PENDING, db_index=True
    )
    ai_score = models.FloatField(
        null=True, blank=True, help_text="Overall AI confidence score (0-100) from the verification engine."
    )
    status = models.CharField(max_length=20, choices=ListingStatus.choices, default=ListingStatus.DRAFT, db_index=True)

    view_count = models.PositiveIntegerField(default=0)
    purchase_count = models.PositiveIntegerField(default=0)
    average_rating = models.FloatField(null=True, blank=True, help_text="Denormalized from Review; null until the first review exists.")
    review_count = models.PositiveIntegerField(default=0)
    meta_description = models.CharField(
        max_length=300, blank=True,
        help_text="SEO meta description. Auto-derived from `description` if left blank.",
    )

    byline = models.CharField(
        max_length=255, blank=True,
        help_text="Credited author, if different from the seller account. E.g. wire-service convention "
                   "where the account holder and the reporting journalist aren't always the same person.",
    )
    dateline = models.CharField(
        max_length=255, blank=True,
        help_text="Standard news dateline: where (and optionally when) the story was reported, "
                   "e.g. 'DAR ES SALAAM, Aug 14'.",
    )
    reading_time_minutes = models.PositiveIntegerField(
        default=1, help_text="Auto-computed from body word count (~200 wpm) on save."
    )
    featured = models.BooleanField(
        default=False, db_index=True,
        help_text="Manually curated by a moderator/admin for homepage highlighting -- not seller-settable.",
    )

    published_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["status", "verification_status"]),
            models.Index(fields=["news_type", "category"]),
        ]

    def save(self, *args, **kwargs):
        if not self.slug:
            base = slugify(self.title)[:250]
            self.slug = f"{base}-{uuid.uuid4().hex[:8]}"
        if not self.meta_description and self.description:
            self.meta_description = self.description[:297] + ("..." if len(self.description) > 297 else "")
        if self.body:
            word_count = len(self.body.split())
            self.reading_time_minutes = max(1, round(word_count / 200))
        super().save(*args, **kwargs)

    def __str__(self):
        return self.title

    @property
    def seller_earning(self):
        from decimal import Decimal

        commission_rate = Decimal("0.10")
        return (self.price * (Decimal("1.00") - commission_rate)).quantize(Decimal("0.01"))

    @property
    def platform_commission(self):
        from decimal import Decimal

        commission_rate = Decimal("0.10")
        return (self.price * commission_rate).quantize(Decimal("0.01"))


def news_media_upload_path(instance, filename):
    return f"news/{instance.listing_id}/{uuid.uuid4()}_{filename}"


class NewsMedia(BaseModel):
    """Images/videos attached to a listing. Preview media may be watermarked
    or truncated; full media is only served after purchase."""

    class MediaType(models.TextChoices):
        IMAGE = "image", "Image"
        VIDEO = "video", "Video"
        DOCUMENT = "document", "Document"

    listing = models.ForeignKey(NewsListing, on_delete=models.CASCADE, related_name="media")
    media_type = models.CharField(max_length=10, choices=MediaType.choices)
    file = models.FileField(upload_to=news_media_upload_path)
    preview_file = models.FileField(upload_to=news_media_upload_path, blank=True, null=True)
    is_cover = models.BooleanField(default=False)
    order = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ["order", "created_at"]


class AIVerificationResult(BaseModel):
    """
    Output of the Habari AI Verification Engine's text/NLP pipeline for a
    given listing. One listing may be re-run multiple times (e.g. after the
    seller edits content), so this is a log, not a single row per listing.
    """

    class Outcome(models.TextChoices):
        VERIFIED = "verified", "Verified"
        PARTIALLY_VERIFIED = "partially_verified", "Partially verified"
        NEEDS_HUMAN_REVIEW = "needs_human_review", "Needs human review"
        REJECTED = "rejected", "Rejected"

    listing = models.ForeignKey(NewsListing, on_delete=models.CASCADE, related_name="ai_verifications")

    fake_news_score = models.FloatField(help_text="0-100, higher = more likely fake.")
    misinformation_score = models.FloatField(default=0)
    clickbait_score = models.FloatField(default=0)
    authenticity_score = models.FloatField(default=0)
    sentiment_score = models.FloatField(
        default=0, help_text="-1 (very negative) to +1 (very positive)."
    )
    similarity_score = models.FloatField(
        default=0, help_text="Highest similarity to existing content, 0-100."
    )
    duplicate_of = models.ForeignKey(
        NewsListing, on_delete=models.SET_NULL, null=True, blank=True, related_name="duplicates_of_this"
    )
    risk_score = models.FloatField(default=0)
    confidence_score = models.FloatField(help_text="Overall model confidence, 0-100.")

    outcome = models.CharField(max_length=25, choices=Outcome.choices, db_index=True)
    model_name = models.CharField(max_length=100, blank=True)
    model_version = models.CharField(max_length=50, blank=True)
    raw_response = models.JSONField(default=dict, blank=True)

    reviewed_by_human = models.BooleanField(default=False)
    human_reviewer = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="news_reviews",
    )
    human_notes = models.TextField(blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["listing", "outcome"])]


class ImageAnalysisResult(BaseModel):
    """Output of the image authenticity pipeline for a single NewsMedia item."""

    media = models.ForeignKey(NewsMedia, on_delete=models.CASCADE, related_name="analysis_results")

    manipulation_score = models.FloatField(default=0, help_text="0-100, higher = more likely manipulated.")
    deepfake_score = models.FloatField(default=0)
    metadata_valid = models.BooleanField(null=True, blank=True)
    exif_payload = models.JSONField(default=dict, blank=True)
    reverse_image_matches = models.JSONField(
        default=list, blank=True, help_text="List of {url, similarity, source} matches found."
    )
    authenticity_score = models.FloatField(default=0)

    flagged = models.BooleanField(default=False)
    model_name = models.CharField(max_length=100, blank=True)
    model_version = models.CharField(max_length=50, blank=True)

    class Meta:
        ordering = ["-created_at"]


class Review(BaseModel):
    """
    A buyer's rating + written review of a listing they actually paid
    for. One review per Order (a buyer can't review the same purchase
    twice), enforced with a OneToOneField rather than just a unique
    constraint on (reviewer, listing), since a buyer could in principle
    purchase the same listing again after an earlier order was refunded.

    NewsListing.average_rating / review_count are denormalized from this
    table for fast list-view rendering (see news/reviews.py for the
    recalculation logic) -- the source of truth is always this table.
    """

    listing = models.ForeignKey(NewsListing, on_delete=models.CASCADE, related_name="reviews")
    order = models.OneToOneField("payments.Order", on_delete=models.CASCADE, related_name="review")
    reviewer = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="reviews_written"
    )
    rating = models.PositiveSmallIntegerField(validators=[MinValueValidator(1), MaxValueValidator(5)])
    comment = models.TextField(blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["listing", "-created_at"])]

    def __str__(self):
        return f"{self.rating}* review of '{self.listing.title}' by {self.reviewer_id}"


class Bookmark(BaseModel):
    """A buyer saving a listing for later -- the standard e-commerce
    wishlist pattern. One bookmark per (user, listing) pair."""

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="bookmarks")
    listing = models.ForeignKey(NewsListing, on_delete=models.CASCADE, related_name="bookmarked_by")

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(fields=["user", "listing"], name="unique_bookmark_per_user_listing"),
        ]

    def __str__(self):
        return f"{self.user_id} bookmarked '{self.listing.title}'"


class Follow(BaseModel):
    """A buyer following a specific journalist/seller -- Substack's core
    retention mechanic: people subscribe to writers, not just browse
    individual listings. One follow per (follower, followed) pair; a
    user can't follow themselves (enforced in the service layer, not
    just the API, since the constraint can't express it directly)."""

    follower = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="following")
    followed = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="followers")

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(fields=["follower", "followed"], name="unique_follow_per_pair"),
        ]

    def __str__(self):
        return f"{self.follower_id} follows {self.followed_id}"


class ListingView(BaseModel):
    """A single real page-view event -- the source of truth for
    analytics that NewsListing.view_count (a flat incrementing integer)
    can't answer: trending-this-week, unique-viewer counts, view
    trends over time. view_count itself is kept too, as a fast
    denormalized display number updated alongside this log -- this
    doesn't replace it, it adds real event-level data behind it."""

    listing = models.ForeignKey(NewsListing, on_delete=models.CASCADE, related_name="view_logs")
    viewer = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="listing_views"
    )
    session_key = models.CharField(max_length=40, blank=True, help_text="Anonymous-viewer session key, if any.")

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["listing", "-created_at"]),
            models.Index(fields=["viewer", "listing", "-created_at"]),
        ]

    def __str__(self):
        return f"view of '{self.listing.title}' @ {self.created_at:%Y-%m-%d %H:%M}"
