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

from .models import AIVerificationResult, Bookmark, Category, Follow, ListingView, NewsListing, NewsMedia, SavedSearch, Tag
from .services import MEDIA_DOWNLOAD_TOKEN_MAX_AGE, make_media_download_token
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
    SavedSearchSerializer,
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
        if self.action in {"reviews", "corrections"}:
            if self.request.method == "GET":
                return [permissions.AllowAny()]
            return [permissions.IsAuthenticated()]
        if self.action in {"moderate", "delete_permanently"}:
            return [IsModeratorOrAbove()]
        if self.action in {"bookmark", "social_share"}:
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
        from .models import NewsListingRevision
        from .services import log_news_action

        original = serializer.instance
        was_published = original.status == NewsListing.ListingStatus.PUBLISHED
        tracked_fields = ["title", "description", "body", "byline", "dateline", "location"]
        snapshot = {f: getattr(original, f) for f in tracked_fields}
        snapshot["price"] = str(original.price)

        listing = serializer.save()

        NewsListingRevision.objects.create(listing=listing, edited_by=self.request.user, snapshot=snapshot)

        content_changed = any(snapshot[f] != getattr(listing, f) for f in tracked_fields) or snapshot["price"] != str(listing.price)

        description = "Listing updated."
        if was_published and content_changed:
            # A seller correcting a typo shouldn't be able to silently
            # change a listing's actual content after it already passed
            # verification and started selling -- pull it back to pending
            # re-review, same as the media-upload flagging path does.
            listing.status = NewsListing.ListingStatus.SUBMITTED
            listing.verification_status = NewsListing.VerificationStatus.PENDING
            listing.save(update_fields=["status", "verification_status", "updated_at"])
            description = "Listing edited after publishing; pulled back to pending re-review."

        log_news_action(
            actor=self.request.user,
            action=AuditLog.Action.UPDATE,
            listing=listing,
            description=description,
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

        from .tasks import process_listing_submission_task

        # In the default eager mode (no Celery broker configured) this
        # runs synchronously right here, so `listing` already reflects
        # the AI outcome by the time we serialize it below -- identical
        # to the old inline call. With a real broker, this returns
        # immediately and the response reflects "still pending" instead;
        # a caller wanting the final outcome polls ai-results/ or waits
        # for the Notification, same as a real async system requires.
        process_listing_submission_task.delay(
            str(listing.id), str(request.user.id),
            request.META.get("REMOTE_ADDR"), request.META.get("HTTP_USER_AGENT", "")[:512],
        )
        listing.refresh_from_db()

        return Response(NewsListingDetailSerializer(listing, context={"request": request}).data)

    @action(detail=True, methods=["post"], parser_classes=[MultiPartParser, FormParser])
    def media(self, request, slug=None):
        listing = self.get_object()
        serializer = NewsMediaSerializer(data=request.data, context={"listing": listing})
        serializer.is_valid(raise_exception=True)
        media_obj = serializer.save()

        from .tasks import process_media_upload_task

        process_media_upload_task.delay(
            str(media_obj.id), str(request.user.id),
            request.META.get("REMOTE_ADDR"), request.META.get("HTTP_USER_AGENT", "")[:512],
        )

        return Response(serializer.data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["get"], url_path="ai-results")
    def ai_results(self, request, slug=None):
        listing = self.get_object()
        results = listing.ai_verifications.order_by("-created_at")
        return Response(AIVerificationResultSerializer(results, many=True).data)

    @action(detail=True, methods=["get"])
    def revisions(self, request, slug=None):
        """GET /api/news/listings/<slug>/revisions/ -- owner/staff only.
        IsListingOwner (this viewset's default permission) allows any
        authenticated user through on GET (safe methods bypass its
        owner check, since that's correct for viewing a public listing) --
        revision history is not public, so it's checked explicitly here.
        """
        listing = self.get_object()
        user = request.user
        is_owner_or_staff = listing.seller_id == user.id or (
            user.role in {user.Role.MODERATOR, user.Role.ADMIN, user.Role.SUPER_ADMIN} or user.is_staff
        )
        if not is_owner_or_staff:
            return Response({"detail": "Not your listing."}, status=status.HTTP_403_FORBIDDEN)

        results = listing.revisions.select_related("edited_by").order_by("-created_at")
        return Response([
            {
                "id": str(r.id),
                "edited_by": r.edited_by.username if r.edited_by else "",
                "snapshot": r.snapshot,
                "created_at": r.created_at,
            }
            for r in results
        ])

    @action(detail=True, methods=["get"], url_path="download/(?P<media_id>[^/.]+)")
    def download(self, request, slug=None, media_id=None):
        """GET /api/news/listings/<slug>/download/<media_id>/ -- verifies
        the requester has actually paid for this listing (or is the
        seller/staff), then hands back a signed, short-lived download URL
        rather than streaming the file directly from this authenticated
        endpoint. The actual bytes are served by MediaDownloadByTokenView
        below, a public (no-auth-header) endpoint that only a valid,
        unexpired signature can unlock -- the standard presigned-URL
        pattern, so the link also works if handed to e.g. a <video> tag
        or opened directly rather than only via an authenticated fetch.
        """
        from payments.models import Order

        listing = self.get_object()
        media_obj = get_object_or_404(NewsMedia, pk=media_id, listing=listing)

        user = request.user
        is_owner_or_staff = listing.seller_id == user.id or (
            user.role in {user.Role.MODERATOR, user.Role.ADMIN, user.Role.SUPER_ADMIN} or user.is_staff
        )
        has_paid = Order.objects.filter(buyer=user, listing=listing, status=Order.Status.PAID).exists()
        if not (is_owner_or_staff or has_paid):
            return Response(
                {"detail": "You need to purchase this listing before downloading its original media."},
                status=status.HTTP_403_FORBIDDEN,
            )

        if not media_obj.file:
            from django.http import Http404
            raise Http404("No file attached to this media item.")

        token = make_media_download_token(media_obj.id)
        download_url = request.build_absolute_uri(f"/api/news/media-download/{token}/")
        return Response({"download_url": download_url, "expires_in": MEDIA_DOWNLOAD_TOKEN_MAX_AGE})

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

    @action(detail=True, methods=["post"], url_path="delete-permanently")
    def delete_permanently(self, request, slug=None):
        """POST /api/news/listings/<slug>/delete-permanently/ -- hard-delete
        a listing, distinct from the soft REMOVED status that DELETE/reject
        already produce. Refused if any buyer has a paid order against this
        listing (they've already paid for access to it; deleting the row
        would orphan that purchase) -- suspend/reject is the correct tool
        in that case, not a hard delete. Requires a `reason` in the body
        and always writes an AuditLog entry, since this is irreversible.
        """
        from django.db.models import ProtectedError

        from payments.models import Order

        listing = self.get_object()
        reason = (request.data.get("reason") or "").strip()
        if not reason:
            return Response({"detail": "A reason is required to permanently delete a listing."}, status=status.HTTP_400_BAD_REQUEST)

        order_count = Order.objects.filter(listing=listing).count()
        paid_order_count = Order.objects.filter(listing=listing, status=Order.Status.PAID).count()
        if paid_order_count:
            return Response(
                {
                    "detail": f"Cannot permanently delete: {paid_order_count} buyer(s) already paid for "
                              f"this listing and have access to it. Use Reject or Suspend instead to keep "
                              f"their purchase history intact.",
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
        if order_count:
            return Response(
                {
                    "detail": f"Cannot permanently delete: {order_count} order record(s) (e.g. failed/"
                              f"cancelled payment attempts) reference this listing and must be kept for "
                              f"financial history. Use Reject or Suspend instead.",
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        from .services import log_news_action

        listing_id, title = listing.id, listing.title
        log_news_action(
            actor=request.user, action=AuditLog.Action.DELETE, listing=listing,
            description=f"Listing '{title}' permanently deleted by moderator. Reason: {reason}",
            request=request,
        )
        try:
            listing.delete()
        except ProtectedError:
            return Response(
                {"detail": "Cannot permanently delete: other records still reference this listing."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        return Response({"detail": f"Listing '{title}' permanently deleted.", "id": str(listing_id)})

    @action(detail=True, methods=["get", "post"])
    def corrections(self, request, slug=None):
        """GET (public): a listing's correction/retraction history --
        visible to anyone, including someone who bought it before a
        correction was posted. POST (owner or staff only): add one, and
        notify every past buyer so the correction doesn't just sit
        silently on the page."""
        from .models import Correction
        from .serializers import CorrectionCreateSerializer, CorrectionSerializer

        listing = self.get_object()

        if request.method == "GET":
            results = listing.corrections.select_related("created_by").order_by("-created_at")
            return Response(CorrectionSerializer(results, many=True).data)

        user = request.user
        is_owner_or_staff = listing.seller_id == user.id or (
            user.role in {user.Role.MODERATOR, user.Role.ADMIN, user.Role.SUPER_ADMIN} or user.is_staff
        )
        if not is_owner_or_staff:
            return Response({"detail": "Only the seller or a moderator can post a correction."}, status=status.HTTP_403_FORBIDDEN)

        serializer = CorrectionCreateSerializer(data=request.data, context={"listing": listing, "request": request})
        serializer.is_valid(raise_exception=True)
        correction = serializer.save()

        from payments.models import Order

        from core.models import Notification
        from core.notifications import send_notification

        buyer_orders = Order.objects.filter(listing=listing, status=Order.Status.PAID).select_related("buyer")
        label = "Retraction" if correction.is_retraction else "Correction"
        notified_buyer_ids = set()
        for order in buyer_orders:
            if order.buyer_id in notified_buyer_ids:
                continue
            notified_buyer_ids.add(order.buyer_id)
            send_notification(
                user=order.buyer, notification_type=Notification.NotificationType.MODERATION_ACTION,
                title=f"{label} issued for '{listing.title}'",
                message=correction.text[:200],
                link_path=f"listing.html?slug={listing.slug}",
            )

        from .services import log_news_action

        log_news_action(
            actor=user, action=AuditLog.Action.UPDATE, listing=listing,
            description=f"{label} posted: {correction.text[:200]}", request=request,
        )
        return Response(CorrectionSerializer(correction).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["post"], url_path="social-share")
    def social_share(self, request, slug=None):
        """POST /api/news/listings/<slug>/social-share/ {"provider": "..."}
        -- purchase-gated (or owner/staff). For providers with a real,
        unauthenticated web share-intent URL (facebook/x/linkedin/
        whatsapp), returns that URL to open in a new tab -- this actually
        works today, no OAuth app needed. For instagram/tiktok/youtube,
        there is no such unauthenticated intent; those platforms require
        a registered OAuth developer app we don't have credentials for
        (same category as the Nala payment integration -- see
        payments/nala_client.py) -- returns manual=True with a ready-to-
        paste caption instead of pretending to publish directly.
        Every call is logged to SocialShareRecord for copyright/usage
        tracking regardless of which path it took.
        """
        import urllib.parse

        from django.conf import settings

        from payments.models import Order

        from .models import SocialShareRecord

        listing = self.get_object()
        provider = request.data.get("provider")
        valid_providers = {c.value for c in SocialShareRecord.Provider}
        if provider not in valid_providers:
            return Response({"detail": f"provider must be one of {sorted(valid_providers)}."}, status=status.HTTP_400_BAD_REQUEST)

        user = request.user
        is_owner_or_staff = listing.seller_id == user.id or (
            user.role in {user.Role.MODERATOR, user.Role.ADMIN, user.Role.SUPER_ADMIN} or user.is_staff
        )
        order = Order.objects.filter(buyer=user, listing=listing, status=Order.Status.PAID).order_by("-created_at").first()
        if not (is_owner_or_staff or order):
            return Response(
                {"detail": "You need to purchase this listing before sharing it."},
                status=status.HTTP_403_FORBIDDEN,
            )

        SocialShareRecord.objects.create(user=user, listing=listing, order=order, provider=provider)

        listing_url = f"{settings.FRONTEND_BASE_URL.rstrip('/')}/listing.html?slug={listing.slug}"
        caption = f"{listing.title} -- via WEMIX ({listing_url})"

        SHARE_INTENT_URLS = {
            "facebook": f"https://www.facebook.com/sharer/sharer.php?u={urllib.parse.quote(listing_url, safe='')}",
            "x": f"https://twitter.com/intent/tweet?text={urllib.parse.quote(listing.title, safe='')}&url={urllib.parse.quote(listing_url, safe='')}",
            "linkedin": f"https://www.linkedin.com/sharing/share-offsite/?url={urllib.parse.quote(listing_url, safe='')}",
            "whatsapp": f"https://api.whatsapp.com/send?text={urllib.parse.quote(caption, safe='')}",
        }

        if provider in SHARE_INTENT_URLS:
            return Response({"manual": False, "share_url": SHARE_INTENT_URLS[provider]})

        return Response({
            "manual": True,
            "caption": caption,
            "detail": f"{dict(SocialShareRecord.Provider.choices)[provider]} doesn't offer a direct-post link "
                      f"without a registered developer app (same situation as the Nala payment integration). "
                      f"Copy the caption above and download the original media to post manually for now.",
        })

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


class SavedSearchListCreateView(generics.ListCreateAPIView):
    """GET/POST /api/news/saved-searches/ -- the current user's own
    standing alert filters."""

    serializer_class = SavedSearchSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        return SavedSearch.objects.filter(user=self.request.user)


class SavedSearchDetailView(generics.RetrieveUpdateDestroyAPIView):
    """GET/PATCH/DELETE /api/news/saved-searches/<id>/"""

    serializer_class = SavedSearchSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        return SavedSearch.objects.filter(user=self.request.user)


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


class MarketplaceSitemapView(APIView):
    """GET /sitemap.xml -- public. Lists the homepage plus every publicly
    visible (published + AI-cleared) listing page, pointing at the
    FRONTEND (not this API) since that's what search engines should
    actually crawl. No prior sitemap existed at all."""

    permission_classes = [permissions.AllowAny]

    def get(self, request):
        from django.conf import settings
        from django.http import HttpResponse

        base = settings.FRONTEND_BASE_URL.rstrip("/")
        listings = NewsListing.objects.filter(
            status=NewsListing.ListingStatus.PUBLISHED,
            verification_status__in=[NewsListing.VerificationStatus.VERIFIED, NewsListing.VerificationStatus.PARTIALLY_VERIFIED],
        ).order_by("-created_at").only("slug", "updated_at")[:5000]

        urls = [f"{base}/index.html", f"{base}/login.html", f"{base}/register.html", f"{base}/terms.html"]
        entries = [f"<url><loc>{u}</loc></url>" for u in urls]
        entries += [
            f"<url><loc>{base}/listing.html?slug={l.slug}</loc><lastmod>{l.updated_at.date().isoformat()}</lastmod></url>"
            for l in listings
        ]
        xml = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
            + "\n".join(entries) + "\n</urlset>"
        )
        return HttpResponse(xml, content_type="application/xml")


class MarketplaceRssFeedView(APIView):
    """GET /feed.xml -- public RSS 2.0 feed of the latest published,
    verified listings. Lets syndication partners and power users consume
    'latest verified stories' without a browser -- didn't exist before."""

    permission_classes = [permissions.AllowAny]

    def get(self, request):
        from django.conf import settings
        from django.http import HttpResponse
        from django.utils.http import http_date

        base = settings.FRONTEND_BASE_URL.rstrip("/")
        listings = NewsListing.objects.filter(
            status=NewsListing.ListingStatus.PUBLISHED,
            verification_status__in=[NewsListing.VerificationStatus.VERIFIED, NewsListing.VerificationStatus.PARTIALLY_VERIFIED],
        ).select_related("seller").order_by("-created_at")[:50]

        def esc(s):
            return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

        items = []
        for l in listings:
            link = f"{base}/listing.html?slug={l.slug}"
            items.append(
                f"<item><title>{esc(l.title)}</title><link>{link}</link><guid>{link}</guid>"
                f"<description>{esc(l.description)}</description>"
                f"<author>{esc(l.seller.username)}</author>"
                f"<pubDate>{http_date(l.created_at.timestamp())}</pubDate></item>"
            )

        xml = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<rss version="2.0"><channel>'
            "<title>WEMIX — Global News Market</title>"
            f"<link>{base}/index.html</link>"
            "<description>Latest verified, published stories on WEMIX.</description>"
            + "".join(items) +
            "</channel></rss>"
        )
        return HttpResponse(xml, content_type="application/rss+xml")


class MediaDownloadByTokenView(APIView):
    """GET /api/news/media-download/<token>/ -- public (no Authorization
    header required): streams a NewsMedia file if `token` is a valid,
    unexpired signature from make_media_download_token(). The access
    decision (did this user pay for the listing?) already happened in
    NewsListingViewSet.download, which is the only place these tokens
    are minted -- this view just proves the token is genuine and fresh."""

    permission_classes = [permissions.AllowAny]

    def get(self, request, token=None):
        from django.core import signing
        from django.http import FileResponse, Http404

        from .services import read_media_download_token

        try:
            media_id = read_media_download_token(token)
        except signing.SignatureExpired:
            return Response({"detail": "This download link has expired. Go back and click Download again."}, status=status.HTTP_403_FORBIDDEN)
        except signing.BadSignature:
            return Response({"detail": "Invalid download link."}, status=status.HTTP_403_FORBIDDEN)

        media_obj = get_object_or_404(NewsMedia, pk=media_id)
        if not media_obj.file:
            raise Http404("No file attached to this media item.")
        return FileResponse(
            media_obj.file.open("rb"), as_attachment=True,
            filename=media_obj.file.name.rsplit("/", 1)[-1],
        )
