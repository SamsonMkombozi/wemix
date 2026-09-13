from rest_framework import serializers

from payments.models import Order

from .models import (
    AIVerificationResult,
    Bookmark,
    Category,
    Follow,
    ImageAnalysisResult,
    ListingView,
    NewsListing,
    NewsMedia,
    Review,
    Tag,
)


class CategorySerializer(serializers.ModelSerializer):
    class Meta:
        model = Category
        fields = ["id", "name", "slug", "description"]


class TagSerializer(serializers.ModelSerializer):
    class Meta:
        model = Tag
        fields = ["id", "name", "slug"]


class ImageAnalysisResultSerializer(serializers.ModelSerializer):
    class Meta:
        model = ImageAnalysisResult
        fields = [
            "id",
            "manipulation_score",
            "deepfake_score",
            "metadata_valid",
            "authenticity_score",
            "reverse_image_matches",
            "flagged",
            "created_at",
        ]


class NewsMediaSerializer(serializers.ModelSerializer):
    analysis_results = ImageAnalysisResultSerializer(many=True, read_only=True)

    class Meta:
        model = NewsMedia
        fields = ["id", "media_type", "file", "preview_file", "is_cover", "order", "analysis_results"]
        read_only_fields = ["id", "analysis_results"]

    def create(self, validated_data):
        listing = self.context["listing"]
        return NewsMedia.objects.create(listing=listing, **validated_data)


class AIVerificationResultSerializer(serializers.ModelSerializer):
    class Meta:
        model = AIVerificationResult
        fields = [
            "id",
            "fake_news_score",
            "misinformation_score",
            "clickbait_score",
            "authenticity_score",
            "sentiment_score",
            "similarity_score",
            "risk_score",
            "confidence_score",
            "outcome",
            "reviewed_by_human",
            "human_notes",
            "model_name",
            "model_version",
            "created_at",
        ]


class NewsListingListSerializer(serializers.ModelSerializer):
    """Lightweight serializer for browsing/search results. Never includes
    the paywalled `body` field."""

    seller_username = serializers.CharField(source="seller.username", read_only=True)
    cover_image = serializers.SerializerMethodField()
    is_bookmarked = serializers.SerializerMethodField()
    is_following_seller = serializers.SerializerMethodField()

    def get_is_following_seller(self, obj):
        request = self.context.get("request")
        if not request or not request.user.is_authenticated:
            return False
        prefetched = self.context.get("followed_seller_ids")
        if prefetched is not None:
            return obj.seller_id in prefetched
        return Follow.objects.filter(follower=request.user, followed_id=obj.seller_id).exists()

    def get_is_bookmarked(self, obj):
        request = self.context.get("request")
        if not request or not request.user.is_authenticated:
            return False
        # Prefetched by the view as `_prefetched_bookmarked_ids` when
        # listing many objects, to avoid one query per row; falls back to
        # a direct query for single-object contexts (e.g. after toggling).
        prefetched = self.context.get("bookmarked_ids")
        if prefetched is not None:
            return obj.id in prefetched
        return Bookmark.objects.filter(user=request.user, listing=obj).exists()

    class Meta:
        model = NewsListing
        fields = [
            "id",
            "title",
            "slug",
            "description",
            "news_type",
            "category",
            "location",
            "price",
            "currency",
            "verification_status",
            "ai_score",
            "status",
            "seller_username",
            "cover_image",
            "byline",
            "dateline",
            "reading_time_minutes",
            "featured",
            "is_bookmarked",
            "is_following_seller",
            "view_count",
            "purchase_count",
            "average_rating",
            "review_count",
            "published_at",
            "created_at",
        ]

    def get_cover_image(self, obj):
        cover = obj.media.filter(is_cover=True).first() or obj.media.first()
        if not cover:
            return None
        request = self.context.get("request")
        url = cover.preview_file.url if cover.preview_file else cover.file.url
        return request.build_absolute_uri(url) if request else url


class NewsListingDetailSerializer(serializers.ModelSerializer):
    """Full detail view. `body` is only populated for the seller who owns
    the listing, staff/moderators, or a buyer with a completed order --
    everyone else gets null plus `body_locked: true` so the frontend can
    render a paywall prompt."""

    seller_username = serializers.CharField(source="seller.username", read_only=True)
    seller_avatar = serializers.ImageField(source="seller.avatar", read_only=True, use_url=True)
    seller_bio = serializers.CharField(source="seller.bio", read_only=True)
    seller_verified_badge = serializers.BooleanField(source="seller.is_verified_badge", read_only=True)
    seller_website_url = serializers.CharField(source="seller.website_url", read_only=True)
    seller_twitter_url = serializers.CharField(source="seller.twitter_url", read_only=True)
    seller_facebook_url = serializers.CharField(source="seller.facebook_url", read_only=True)
    tags = TagSerializer(many=True, read_only=True)
    media = NewsMediaSerializer(many=True, read_only=True)
    body = serializers.SerializerMethodField()
    body_locked = serializers.SerializerMethodField()
    seller_earning = serializers.DecimalField(max_digits=12, decimal_places=2, read_only=True)
    platform_commission = serializers.DecimalField(max_digits=12, decimal_places=2, read_only=True)
    latest_ai_verification = serializers.SerializerMethodField()
    is_bookmarked = serializers.SerializerMethodField()
    is_following_seller = serializers.SerializerMethodField()
    seller_follower_count = serializers.SerializerMethodField()

    def get_is_following_seller(self, obj):
        request = self.context.get("request")
        if not request or not request.user.is_authenticated:
            return False
        return Follow.objects.filter(follower=request.user, followed_id=obj.seller_id).exists()

    def get_seller_follower_count(self, obj):
        return Follow.objects.filter(followed_id=obj.seller_id).count()

    def get_is_bookmarked(self, obj):
        request = self.context.get("request")
        if not request or not request.user.is_authenticated:
            return False
        return Bookmark.objects.filter(user=request.user, listing=obj).exists()

    class Meta:
        model = NewsListing
        fields = [
            "id",
            "title",
            "slug",
            "description",
            "meta_description",
            "body",
            "body_locked",
            "news_type",
            "category",
            "tags",
            "location",
            "price",
            "currency",
            "verification_status",
            "ai_score",
            "status",
            "seller",
            "seller_username",
            "seller_avatar",
            "seller_bio",
            "seller_verified_badge",
            "seller_website_url",
            "seller_twitter_url",
            "seller_facebook_url",
            "seller_earning",
            "platform_commission",
            "media",
            "latest_ai_verification",
            "byline",
            "dateline",
            "reading_time_minutes",
            "featured",
            "is_bookmarked",
            "is_following_seller",
            "seller_follower_count",
            "view_count",
            "purchase_count",
            "average_rating",
            "review_count",
            "published_at",
            "created_at",
        ]
        read_only_fields = [
            "id", "slug", "verification_status", "ai_score", "seller", "created_at",
            "reading_time_minutes", "featured",
        ]

    def _has_access(self, obj) -> bool:
        request = self.context.get("request")
        if not request or not request.user.is_authenticated:
            return False
        user = request.user
        if obj.seller_id == user.id:
            return True
        if user.role in {user.Role.MODERATOR, user.Role.ADMIN, user.Role.SUPER_ADMIN} or user.is_staff:
            return True
        return Order.objects.filter(buyer=user, listing=obj, status=Order.Status.PAID).exists()

    def get_body(self, obj):
        return obj.body if self._has_access(obj) else None

    def get_body_locked(self, obj):
        return not self._has_access(obj)

    def get_latest_ai_verification(self, obj):
        latest = obj.ai_verifications.order_by("-created_at").first()
        return AIVerificationResultSerializer(latest).data if latest else None


class NewsListingWriteSerializer(serializers.ModelSerializer):
    """Used for create/update by the seller. Deliberately excludes
    verification_status/ai_score/status transitions that should only
    happen via the AI pipeline or moderation actions."""

    tag_ids = serializers.PrimaryKeyRelatedField(
        source="tags", queryset=Tag.objects.all(), many=True, required=False, write_only=True
    )

    class Meta:
        model = NewsListing
        fields = [
            "id",
            "slug",
            "title",
            "description",
            "meta_description",
            "body",
            "news_type",
            "category",
            "tag_ids",
            "location",
            "byline",
            "dateline",
            "price",
            "currency",
            "status",
            "verification_status",
            "reading_time_minutes",
            "featured",
        ]
        read_only_fields = ["id", "slug", "status", "verification_status", "reading_time_minutes", "featured"]

    def create(self, validated_data):
        tags = validated_data.pop("tags", [])
        request = self.context["request"]
        listing = NewsListing.objects.create(seller=request.user, **validated_data)
        if tags:
            listing.tags.set(tags)
        return listing

    def update(self, instance, validated_data):
        tags = validated_data.pop("tags", None)
        for attr, value in validated_data.items():
            setattr(instance, attr, value)
        # Any edit to sellable content should re-trigger AI review rather
        # than silently keeping a stale verification status.
        instance.verification_status = NewsListing.VerificationStatus.PENDING
        instance.save()
        if tags is not None:
            instance.tags.set(tags)
        return instance


class SubmitForReviewSerializer(serializers.Serializer):
    """No fields -- just an action endpoint that moves a draft listing to
    'submitted' so it enters the AI verification / moderation queue."""

    def save(self, **kwargs):
        listing = self.context["listing"]
        listing.status = NewsListing.ListingStatus.SUBMITTED
        listing.save(update_fields=["status", "updated_at"])
        return listing


class ReviewSerializer(serializers.ModelSerializer):
    reviewer_username = serializers.CharField(source="reviewer.username", read_only=True)

    class Meta:
        model = Review
        fields = ["id", "reviewer_username", "rating", "comment", "created_at"]
        read_only_fields = fields


class ReviewCreateSerializer(serializers.Serializer):
    order_id = serializers.UUIDField()
    rating = serializers.IntegerField(min_value=1, max_value=5)
    comment = serializers.CharField(required=False, allow_blank=True, max_length=2000)

    def save(self, **kwargs):
        from payments.models import Order

        from .reviews import create_review

        request = self.context["request"]
        listing = self.context["listing"]
        order_id = self.validated_data["order_id"]

        try:
            order = Order.objects.get(pk=order_id)
        except Order.DoesNotExist:
            raise serializers.ValidationError({"order_id": "Order not found."})

        return create_review(
            reviewer=request.user, listing=listing, order=order,
            rating=self.validated_data["rating"], comment=self.validated_data.get("comment", ""),
        )


class ModerateListingSerializer(serializers.Serializer):
    """Manual moderator override on a listing's verification outcome --
    distinct from the automatic AI/circumvention paths. Lets a moderator
    approve (publish) or reject a listing that's sitting in
    PENDING/NEEDS_HUMAN_REVIEW, with an optional note."""

    DECISION_CHOICES = ["approve", "reject"]
    decision = serializers.ChoiceField(choices=DECISION_CHOICES)
    notes = serializers.CharField(required=False, allow_blank=True, max_length=1000)

    def save(self, **kwargs):
        from django.utils import timezone

        from .ai_verification import notify_verification_outcome
        from .models import AIVerificationResult

        listing = self.context["listing"]
        decision = self.validated_data["decision"]

        if decision == "approve":
            listing.verification_status = NewsListing.VerificationStatus.VERIFIED
            listing.status = NewsListing.ListingStatus.PUBLISHED
            listing.published_at = listing.published_at or timezone.now()
            outcome = AIVerificationResult.Outcome.VERIFIED
        else:
            listing.verification_status = NewsListing.VerificationStatus.REJECTED
            outcome = AIVerificationResult.Outcome.REJECTED

        listing.save(update_fields=["verification_status", "status", "published_at", "updated_at"])
        notify_verification_outcome(listing, outcome)
        return listing


class BookmarkedListingSerializer(serializers.ModelSerializer):
    """Used for GET /api/news/bookmarks/ -- returns the bookmarked
    listing itself (via NewsListingListSerializer's shape) plus when it
    was bookmarked, rather than a raw Bookmark row."""

    listing = NewsListingListSerializer(read_only=True)
    bookmarked_at = serializers.DateTimeField(source="created_at", read_only=True)

    class Meta:
        model = Bookmark
        fields = ["id", "listing", "bookmarked_at"]
        read_only_fields = fields


class FollowedUserSerializer(serializers.ModelSerializer):
    """Used for GET /api/news/following/ -- who the current user follows,
    with a bit of context about each (published listing count) rather
    than a raw Follow row."""

    followed_id = serializers.UUIDField(source="followed.id", read_only=True)
    username = serializers.CharField(source="followed.username", read_only=True)
    organization_name = serializers.CharField(source="followed.organization_name", read_only=True)
    published_listing_count = serializers.SerializerMethodField()
    followed_at = serializers.DateTimeField(source="created_at", read_only=True)

    def get_published_listing_count(self, obj):
        return NewsListing.objects.filter(
            seller_id=obj.followed_id, status=NewsListing.ListingStatus.PUBLISHED
        ).count()

    class Meta:
        model = Follow
        fields = ["id", "followed_id", "username", "organization_name", "published_listing_count", "followed_at"]
        read_only_fields = fields
