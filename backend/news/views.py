from django.contrib.auth import get_user_model
from django.db.models import F
from django.shortcuts import get_object_or_404
from rest_framework import generics, permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.response import Response
from rest_framework.views import APIView

from core.models import AuditLog
from core.permissions import IsAdminOrAbove, IsListingOwner, IsModeratorOrAbove, IsVerifiedSeller

from .models import AIVerificationResult, Bookmark, Category, Follow, ListingView, NewsListing, NewsMedia, Tag
from .serializers import (
    AIVerificationResultSerializer,
    BookmarkedListingSerializer,
    CategorySerializer,
    FollowedUserSerializer,
    ModerateListingSerializer,
    NewsListingDetailSerializer,
    NewsListingListSerializer,
    NewsListingWriteSerializer,
    NewsMediaSerializer,
    ReviewCreateSerializer,
    ReviewSerializer,
    SubmitForReviewSerializer,
    TagSerializer,
)


class CategoryListCreateView(generics.ListCreateAPIView):
    """GET is public; POST (creating new categories) is admin-only."""

    queryset = Category.objects.all()
    serializer_class = CategorySerializer

    def get_permissions(self):
        if self.request.method == "POST":
            return [IsAdminOrAbove()]
        return [permissions.AllowAny()]


class TagListView(generics.ListAPIView):
    queryset = Tag.objects.all()
    serializer_class = TagSerializer
    permission_classes = [permissions.AllowAny]


class NewsListingViewSet(viewsets.ModelViewSet):
    """
    /api/news/listings/                 GET (browse, public) / POST (create, verified seller)
    /api/news/listings/<id>/             GET / PATCH / DELETE (owner or staff)
    /api/news/listings/<id>/submit/      POST -- move draft -> submitted (AI review queue)
    /api/news/listings/<id>/media/       POST -- attach media to a listing
    /api/news/listings/<id>/ai-results/  GET  -- AI verification history (owner/staff only)
    /api/news/listings/mine/             GET  -- the current seller's own listings, any status
    """

    permission_classes = [IsVerifiedSeller, IsListingOwner]
    lookup_field = "slug"

    def get_permissions(self):
        if self.action in {"list", "retrieve"}:
            return [permissions.AllowAny()]
        if self.action == "create":
            return [permissions.IsAuthenticated(), IsVerifiedSeller()]
        if self.action == "reviews":
            if self.request.method == "GET":
                return [permissions.AllowAny()]
            return [permissions.IsAuthenticated()]
        if self.action == "moderate":
            return [IsModeratorOrAbove()]
        if self.action == "bookmark":
            return [permissions.IsAuthenticated()]
        if self.action == "follow_seller":
            return [permissions.IsAuthenticated()]
        if self.action == "toggle_featured":
            return [IsModeratorOrAbove()]
        # update / partial_update / destroy / submit / media / ai_results
        return [permissions.IsAuthenticated(), IsListingOwner()]


    def get_serializer_class(self):
        if self.action in {"create", "update", "partial_update"}:
            return NewsListingWriteSerializer
        if self.action == "list":
            return NewsListingListSerializer
        return NewsListingDetailSerializer

    def get_queryset(self):
        qs = NewsListing.objects.select_related("category", "seller").prefetch_related("tags", "media")
        user = self.request.user

        if self.action in {"list"}:
            # Public marketplace browsing: published + AI-cleared only,
            # unless the requester is staff/moderator (sees everything) or
            # asks for their own listings via ?mine=1.
            if self.request.query_params.get("mine") == "1" and user.is_authenticated:
                qs = qs.filter(seller=user)
            elif user.is_authenticated and (
                user.role in {user.Role.MODERATOR, user.Role.ADMIN, user.Role.SUPER_ADMIN} or user.is_staff
            ):
                pass  # no restriction
            else:
                qs = qs.filter(
                    status=NewsListing.ListingStatus.PUBLISHED,
                    verification_status__in=[
                        NewsListing.VerificationStatus.VERIFIED,
                        NewsListing.VerificationStatus.PARTIALLY_VERIFIED,
                    ],
                )

            qs = self._apply_filters(qs)

        return qs

    def _apply_filters(self, qs):
        params = self.request.query_params
        category = params.get("category")
        news_type = params.get("news_type")
        q = params.get("q")
        min_price = params.get("min_price")
        max_price = params.get("max_price")
        location = params.get("location")
        verification_status = params.get("verification_status")
        ordering = params.get("ordering", "-created_at")

        if category:
            qs = qs.filter(category__slug=category)
        if news_type:
            qs = qs.filter(news_type=news_type)
        if location:
            qs = qs.filter(location__icontains=location)
        if min_price:
            qs = qs.filter(price__gte=min_price)
        if max_price:
            qs = qs.filter(price__lte=max_price)
        if verification_status:
            qs = qs.filter(verification_status=verification_status)
        if params.get("queue") == "1":
            # Moderator convenience filter: everything currently awaiting a
            # human decision -- submitted, and not yet at a terminal AI
            # outcome (VERIFIED auto-publishes; REJECTED is already final).
            # PARTIALLY_VERIFIED is included here too: like
            # NEEDS_HUMAN_REVIEW/PENDING, it does NOT auto-publish, so a
            # listing that landed there is just as stuck awaiting a human
            # decision -- excluding it left those listings invisible to
            # every moderator with no way to ever resolve them.
            qs = qs.filter(
                status=NewsListing.ListingStatus.SUBMITTED,
                verification_status__in=[
                    NewsListing.VerificationStatus.PENDING,
                    NewsListing.VerificationStatus.NEEDS_HUMAN_REVIEW,
                    NewsListing.VerificationStatus.PARTIALLY_VERIFIED,
                ],
            )
        if params.get("trending") == "1":
            # Real analytics, not the flat view_count: counts actual
            # ListingView events from the last 7 days and orders by that,
            # so a listing that was popular months ago but dead this week
            # doesn't crowd out what's actually trending now.
            from datetime import timedelta

            from django.db.models import Count, Q
            from django.utils import timezone

            week_ago = timezone.now() - timedelta(days=7)
            qs = qs.annotate(
                recent_view_count=Count("view_logs", filter=Q(view_logs__created_at__gte=week_ago))
            ).order_by("-recent_view_count")
            return qs
        if q:
            from django.db.models import Q

            qs = qs.filter(Q(title__icontains=q) | Q(description__icontains=q) | Q(tags__name__icontains=q)).distinct()

        allowed_ordering = {"created_at", "-created_at", "price", "-price", "purchase_count", "-purchase_count"}
        if ordering in allowed_ordering:
            qs = qs.order_by(ordering)
        return qs

    def get_serializer_context(self):
        context = super().get_serializer_context()
        # Precompute which listings (of the ones about to be serialized)
        # the current user has bookmarked, in one query -- avoids an N+1
        # (one Bookmark lookup per row) on the list endpoint. Only worth
        # doing for `list`; `retrieve` serializes a single object so the
        # serializer's own per-object fallback query is fine there.
        if self.action == "list" and self.request.user.is_authenticated:
            context["bookmarked_ids"] = set(
                Bookmark.objects.filter(user=self.request.user).values_list("listing_id", flat=True)
            )
            context["followed_seller_ids"] = set(
                Follow.objects.filter(follower=self.request.user).values_list("followed_id", flat=True)
            )
        return context

    def retrieve(self, request, *args, **kwargs):
        instance = self.get_object()
        NewsListing.objects.filter(pk=instance.pk).update(view_count=F("view_count") + 1)
        instance.refresh_from_db(fields=["view_count"])
        self._log_view(request, instance)
        serializer = self.get_serializer(instance)
        return Response(serializer.data)

    def _log_view(self, request, listing):
        """Records a real ListingView event -- the source of truth for
        analytics view_count alone can't answer. Deduped for
        authenticated users (skip logging a repeat view of the same
        listing within 30 minutes, e.g. from a page refresh); anonymous
        views are always logged, same as the un-deduped behavior
        view_count already had before this existed -- not a regression,
        just not improved for that case yet."""
        from datetime import timedelta

        from django.utils import timezone

        if request.user.is_authenticated:
            recent_cutoff = timezone.now() - timedelta(minutes=30)
            already_logged_recently = ListingView.objects.filter(
                listing=listing, viewer=request.user, created_at__gte=recent_cutoff
            ).exists()
            if already_logged_recently:
                return
            ListingView.objects.create(listing=listing, viewer=request.user)
        else:
            session_key = request.session.session_key or ""
            ListingView.objects.create(listing=listing, session_key=session_key)

    def perform_create(self, serializer):
        listing = serializer.save()
        from .services import log_news_action

        log_news_action(
            actor=self.request.user,
            action=AuditLog.Action.CREATE,
            listing=listing,
            description="Listing created (draft).",
            request=self.request,
        )

    def perform_update(self, serializer):
        listing = serializer.save()
        from .services import log_news_action

        log_news_action(
            actor=self.request.user,
            action=AuditLog.Action.UPDATE,
            listing=listing,
            description="Listing updated; verification reset to pending.",
            request=self.request,
        )

    def perform_destroy(self, instance):
        instance.status = NewsListing.ListingStatus.REMOVED
        instance.save(update_fields=["status", "updated_at"])

    @action(detail=True, methods=["post"])
    def submit(self, request, slug=None):
        listing = self.get_object()
        if listing.status != NewsListing.ListingStatus.DRAFT:
            return Response(
                {"detail": f"Only draft listings can be submitted (current status: {listing.status})."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        serializer = SubmitForReviewSerializer(data={}, context={"listing": listing})
        serializer.is_valid(raise_exception=True)
        serializer.save()

        from .ai_verification import run_ai_verification
        from .services import log_news_action
        from moderation.circumvention_scanner import scan_listing

        ai_result = run_ai_verification(listing)
        listing.refresh_from_db()
        log_news_action(
            actor=request.user, action=AuditLog.Action.UPDATE, listing=listing,
            description=f"AI verification ran: outcome={ai_result.outcome}, fake_news_score={ai_result.fake_news_score}.",
            request=request,
        )

        circumvention_flags = scan_listing(listing)
        if circumvention_flags:
            log_news_action(
                actor=request.user, action=AuditLog.Action.FLAG, listing=listing,
                description=f"Anti-circumvention scan found {len(circumvention_flags)} issue(s): "
                            f"{', '.join(f.detected_pattern for f in circumvention_flags)}.",
                request=request,
            )
            from moderation.circumvention_scanner import HIGH_CONFIDENCE_PATTERNS

            # Any unambiguous contact-info match (phone/email/WhatsApp/
            # Telegram) means the listing itself still contains prohibited
            # content -- pull it back from auto-publish regardless of how
            # leniently the *user* was treated (first offense = only a
            # warning to the account, but the content still can't go live
            # as-is). Fuzzy off-platform-language-only matches are left to
            # the AI verification outcome since those carry more
            # false-positive risk.
            has_high_confidence_match = any(
                f.detected_pattern in HIGH_CONFIDENCE_PATTERNS for f in circumvention_flags
            )
            if has_high_confidence_match:
                listing.status = NewsListing.ListingStatus.SUBMITTED
                listing.verification_status = NewsListing.VerificationStatus.NEEDS_HUMAN_REVIEW
                listing.save(update_fields=["status", "verification_status", "updated_at"])

        return Response(NewsListingDetailSerializer(listing, context={"request": request}).data)

    @action(detail=True, methods=["post"], parser_classes=[MultiPartParser, FormParser])
    def media(self, request, slug=None):
        listing = self.get_object()
        serializer = NewsMediaSerializer(data=request.data, context={"listing": listing})
        serializer.is_valid(raise_exception=True)
        media_obj = serializer.save()

        from .image_analysis import run_image_analysis

        analysis = run_image_analysis(media_obj)
        if analysis.flagged and listing.status == NewsListing.ListingStatus.PUBLISHED:
            # A newly-uploaded image on an already-live listing came back
            # flagged (manipulation signal, no/edited metadata, or a
            # duplicate match) -- pull it back for human review rather
            # than leaving suspect media on a published, purchasable
            # listing.
            listing.status = NewsListing.ListingStatus.SUBMITTED
            listing.verification_status = NewsListing.VerificationStatus.NEEDS_HUMAN_REVIEW
            listing.save(update_fields=["status", "verification_status", "updated_at"])

            from .services import log_news_action
            log_news_action(
                actor=request.user, action=AuditLog.Action.FLAG, listing=listing,
                description=f"Image analysis flagged newly uploaded media (manipulation_score="
                            f"{analysis.manipulation_score}, duplicate_matches={len(analysis.reverse_image_matches)}); "
                            f"listing pulled back for review.",
                request=request,
            )

        return Response(serializer.data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["get"], url_path="ai-results")
    def ai_results(self, request, slug=None):
        listing = self.get_object()
        results = listing.ai_verifications.order_by("-created_at")
        return Response(AIVerificationResultSerializer(results, many=True).data)

    @action(detail=True, methods=["post"])
    def moderate(self, request, slug=None):
        """Manual moderator approve/reject -- distinct from the automatic
        AI verification / circumvention scanner paths."""
        listing = self.get_object()
        serializer = ModerateListingSerializer(data=request.data, context={"listing": listing})
        serializer.is_valid(raise_exception=True)
        serializer.save()

        from .services import log_news_action

        log_news_action(
            actor=request.user, action=AuditLog.Action.APPROVE if serializer.validated_data["decision"] == "approve" else AuditLog.Action.REJECT,
            listing=listing, description=f"Manually {serializer.validated_data['decision']}d by moderator.",
            request=request,
        )
        return Response(NewsListingDetailSerializer(listing, context={"request": request}).data)

    @action(detail=True, methods=["post"])
    def bookmark(self, request, slug=None):
        """Toggles a bookmark on/off for the current user -- one call,
        no separate add/remove endpoints to keep track of client-side."""
        listing = self.get_object()
        bookmark, created = Bookmark.objects.get_or_create(user=request.user, listing=listing)
        if not created:
            bookmark.delete()
            return Response({"is_bookmarked": False})
        return Response({"is_bookmarked": True})

    @action(detail=True, methods=["post"], url_path="follow-seller")
    def follow_seller(self, request, slug=None):
        """Toggles following the listing's seller on/off -- nested under
        a listing rather than a standalone user-lookup endpoint, since
        that's how buyers actually discover sellers in this UI."""
        listing = self.get_object()
        if listing.seller_id == request.user.id:
            return Response({"detail": "You can't follow yourself."}, status=status.HTTP_400_BAD_REQUEST)
        follow, created = Follow.objects.get_or_create(follower=request.user, followed_id=listing.seller_id)
        if not created:
            follow.delete()
            return Response({"is_following_seller": False})
        return Response({"is_following_seller": True})

    @action(detail=True, methods=["post"])
    def toggle_featured(self, request, slug=None):
        """Moderator-only homepage curation toggle -- deliberately separate
        from the general update action so sellers can never set this on
        their own listings."""
        listing = self.get_object()
        listing.featured = not listing.featured
        listing.save(update_fields=["featured", "updated_at"])
        return Response({"featured": listing.featured})

    @action(detail=True, methods=["get"], url_path="view-stats")
    def view_stats(self, request, slug=None):
        """GET .../view-stats/ -- real per-listing analytics from the
        ListingView log, not just the flat counter. Owner or staff only."""
        listing = self.get_object()
        if listing.seller_id != request.user.id and not (
            request.user.role in {request.user.Role.MODERATOR, request.user.Role.ADMIN, request.user.Role.SUPER_ADMIN}
            or request.user.is_staff
        ):
            return Response({"detail": "Not allowed."}, status=status.HTTP_403_FORBIDDEN)

        from datetime import timedelta

        from django.utils import timezone

        week_ago = timezone.now() - timedelta(days=7)
        logs = listing.view_logs.all()
        return Response({
            "total_views": listing.view_count,
            "logged_views": logs.count(),
            "unique_viewers": logs.exclude(viewer__isnull=True).values("viewer_id").distinct().count(),
            "views_last_7_days": logs.filter(created_at__gte=week_ago).count(),
        })

    @action(detail=True, methods=["get", "post"])
    def reviews(self, request, slug=None):
        listing = self.get_object()

        if request.method == "GET":
            reviews = listing.reviews.select_related("reviewer").order_by("-created_at")
            return Response(ReviewSerializer(reviews, many=True).data)

        # POST -- must be authenticated to write a review
        if not request.user.is_authenticated:
            return Response({"detail": "Authentication required."}, status=status.HTTP_401_UNAUTHORIZED)
        serializer = ReviewCreateSerializer(data=request.data, context={"request": request, "listing": listing})
        serializer.is_valid(raise_exception=True)
        review = serializer.save()
        return Response(ReviewSerializer(review).data, status=status.HTTP_201_CREATED)


class BookmarkListView(generics.ListAPIView):
    """GET /api/news/bookmarks/ -- the current user's bookmarked listings, most recently saved first."""

    serializer_class = BookmarkedListingSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        return Bookmark.objects.filter(user=self.request.user).select_related(
            "listing", "listing__seller", "listing__category"
        ).prefetch_related("listing__media")


class FollowingListView(generics.ListAPIView):
    """GET /api/news/following/ -- sellers/journalists the current user follows."""

    serializer_class = FollowedUserSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        return Follow.objects.filter(follower=self.request.user).select_related("followed")


class FollowToggleView(APIView):
    """POST /api/news/users/<uuid:user_id>/follow/ -- follow/unfollow a
    seller directly by their id, not routed through any specific
    listing. Needed because a followed seller might have zero currently
    visible listings (e.g. everything in draft) -- the listing-nested
    .../follow-seller/ action above is the natural entry point from a
    listing page, but the dashboard's "Following" list needs a way to
    unfollow that doesn't depend on the seller having a public listing
    to route through. Both paths do the exact same
    Follow.objects.get_or_create/delete, so following/unfollowing means
    the same thing regardless of which one was used."""

    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, user_id=None):
        User = get_user_model()
        if str(user_id) == str(request.user.id):
            return Response({"detail": "You can't follow yourself."}, status=status.HTTP_400_BAD_REQUEST)
        followed_user = get_object_or_404(User, pk=user_id)
        follow, created = Follow.objects.get_or_create(follower=request.user, followed=followed_user)
        if not created:
            follow.delete()
            return Response({"is_following": False})
        return Response({"is_following": True})
