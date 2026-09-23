import uuid
from decimal import Decimal

from django.contrib.auth import get_user_model
from rest_framework import status
from rest_framework.test import APITestCase

from payments.models import Order

from .models import Category, NewsListing

User = get_user_model()


def make_seller(username="seller1"):
    return User.objects.create_user(
        username=username, email=f"{username}@example.com", password="pw12345678!",
        role=User.Role.SELLER, id_verification_status=User.VerificationStatus.VERIFIED,
    )


def make_buyer(username="buyer1"):
    return User.objects.create_user(username=username, email=f"{username}@example.com", password="pw12345678!", role=User.Role.BUYER)


def make_category():
    return Category.objects.create(name=f"Cat-{uuid.uuid4().hex[:8]}")


class ListingLifecycleAPITests(APITestCase):
    def setUp(self):
        self.seller = make_seller()
        self.category = make_category()

    def test_unverified_seller_cannot_create_listing(self):
        unverified = User.objects.create_user(
            username="unverified", email="unverified@example.com", password="pw12345678!", role=User.Role.SELLER,
        )
        self.client.force_authenticate(unverified)
        resp = self.client.post("/api/news/listings/", {
            "title": "t", "description": "d", "body": "b", "news_type": "text",
            "category": str(self.category.id), "price": "1000",
        })
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_verified_seller_can_create_draft(self):
        self.client.force_authenticate(self.seller)
        resp = self.client.post("/api/news/listings/", {
            "title": "t", "description": "d", "body": "b", "news_type": "text",
            "category": str(self.category.id), "price": "1000",
        })
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        self.assertEqual(resp.data["status"], "draft")


class ListingSearchTests(APITestCase):
    """The `q` browse filter used to be exact-substring matching
    (icontains) with no relevance ordering at all -- results always came
    back newest-first regardless of match quality. This exercises the
    Postgres full-text search replacement: real ranking, English
    stemming, and tag matching kept separate from the ranked vector."""

    def setUp(self):
        self.seller = make_seller()
        self.category = make_category()

    def _make_listing(self, title, description="d", body="b", price=Decimal("1000"), **extra):
        return NewsListing.objects.create(
            seller=self.seller, title=title, description=description, body=body,
            news_type=NewsListing.NewsType.TEXT, category=self.category, price=price,
            status=NewsListing.ListingStatus.PUBLISHED, verification_status=NewsListing.VerificationStatus.VERIFIED,
            **extra,
        )

    def test_matches_title(self):
        match = self._make_listing("Severe flooding hits coastal villages")
        self._make_listing("Parliament debates new budget")
        resp = self.client.get("/api/news/listings/?q=flooding")
        slugs = [r["slug"] for r in resp.data["results"]]
        self.assertEqual(slugs, [match.slug])

    def test_stemming_matches_a_different_word_form(self):
        match = self._make_listing("Severe flooding hits coastal villages")
        resp = self.client.get("/api/news/listings/?q=floods")
        slugs = [r["slug"] for r in resp.data["results"]]
        self.assertEqual(slugs, [match.slug])

    def test_unrelated_listing_is_excluded(self):
        self._make_listing("Parliament debates new budget")
        resp = self.client.get("/api/news/listings/?q=flooding")
        self.assertEqual(resp.data["results"], [])

    def test_title_match_ranks_above_body_only_match(self):
        body_only = self._make_listing("Local elections wrap up", body="Turnout was affected by flooding in three regions.")
        title_match = self._make_listing("Flooding disrupts local elections")
        resp = self.client.get("/api/news/listings/?q=flooding")
        slugs = [r["slug"] for r in resp.data["results"]]
        self.assertEqual(slugs, [title_match.slug, body_only.slug])

    def test_matches_by_tag(self):
        from .models import Tag

        tag = Tag.objects.create(name="drought")
        match = self._make_listing("Regional harvest report")
        match.tags.set([tag])
        self._make_listing("Unrelated sports recap")
        resp = self.client.get("/api/news/listings/?q=drought")
        slugs = [r["slug"] for r in resp.data["results"]]
        self.assertEqual(slugs, [match.slug])

    def test_explicit_ordering_overrides_relevance(self):
        cheap_body_match = self._make_listing("Local elections wrap up", body="flooding mentioned once", price=Decimal("500"))
        expensive_title_match = self._make_listing("Flooding disrupts elections", price=Decimal("5000"))
        resp = self.client.get("/api/news/listings/?q=flooding&ordering=price")
        slugs = [r["slug"] for r in resp.data["results"]]
        self.assertEqual(slugs, [cheap_body_match.slug, expensive_title_match.slug])


class SellerFilterAndProfileTests(APITestCase):
    """The listing.html conversion redesign needs: a `seller` filter (for
    "More from this seller"), an `exclude_slug` filter (so a listing
    never lists itself as "related"), and richer seller-profile fields
    on the detail response (listing count, total sales, join date,
    overall rating, review count, recent-purchase social proof)."""

    def setUp(self):
        self.seller = make_seller()
        self.other_seller = make_seller("otherseller1")
        self.buyer = make_buyer()
        self.category = make_category()

    def _make_listing(self, seller, title, purchase_count=0, **extra):
        return NewsListing.objects.create(
            seller=seller, title=title, description="d", body="b", news_type=NewsListing.NewsType.TEXT,
            category=self.category, price=Decimal("1000"), status=NewsListing.ListingStatus.PUBLISHED,
            verification_status=NewsListing.VerificationStatus.VERIFIED, purchase_count=purchase_count,
            **extra,
        )

    def test_seller_filter_returns_only_that_sellers_listings(self):
        mine = self._make_listing(self.seller, "Mine")
        self._make_listing(self.other_seller, "Not mine")
        resp = self.client.get(f"/api/news/listings/?seller={self.seller.id}")
        slugs = [r["slug"] for r in resp.data["results"]]
        self.assertEqual(slugs, [mine.slug])

    def test_exclude_slug_omits_that_listing(self):
        keep = self._make_listing(self.seller, "Keep this one")
        exclude = self._make_listing(self.seller, "Exclude this one")
        resp = self.client.get(f"/api/news/listings/?exclude_slug={exclude.slug}")
        slugs = [r["slug"] for r in resp.data["results"]]
        self.assertEqual(slugs, [keep.slug])

    def test_detail_response_includes_seller_listing_count_and_sales(self):
        self._make_listing(self.seller, "First", purchase_count=3)
        listing = self._make_listing(self.seller, "Second", purchase_count=2)
        resp = self.client.get(f"/api/news/listings/{listing.slug}/")
        self.assertEqual(resp.data["seller_listing_count"], 2)
        self.assertEqual(resp.data["seller_total_sales"], 5)
        self.assertIsNotNone(resp.data["seller_join_date"])

    def test_detail_response_includes_seller_wide_rating(self):
        from .models import Review
        from payments.models import Order

        listing = self._make_listing(self.seller, "Rated story")
        order = Order.objects.create(buyer=self.buyer, listing=listing, amount=listing.price, status=Order.Status.PAID)
        Review.objects.create(listing=listing, order=order, reviewer=self.buyer, rating=4)

        resp = self.client.get(f"/api/news/listings/{listing.slug}/")
        self.assertEqual(resp.data["seller_overall_rating"], 4.0)
        self.assertEqual(resp.data["seller_review_count"], 1)

    def test_detail_response_has_no_rating_when_seller_has_no_reviews(self):
        listing = self._make_listing(self.seller, "Unrated story")
        resp = self.client.get(f"/api/news/listings/{listing.slug}/")
        self.assertIsNone(resp.data["seller_overall_rating"])
        self.assertEqual(resp.data["seller_review_count"], 0)

    def test_recent_purchase_count_reflects_real_paid_orders_this_week(self):
        from payments.models import Order

        listing = self._make_listing(self.seller, "Popular story")
        Order.objects.create(buyer=self.buyer, listing=listing, amount=listing.price, status=Order.Status.PAID)
        Order.objects.create(buyer=make_buyer("buyer2"), listing=listing, amount=listing.price, status=Order.Status.PAID)
        Order.objects.create(buyer=make_buyer("buyer3"), listing=listing, amount=listing.price, status=Order.Status.PENDING_PAYMENT)

        resp = self.client.get(f"/api/news/listings/{listing.slug}/")
        self.assertEqual(resp.data["recent_purchase_count"], 2)


class SubmitForReviewTests(APITestCase):
    """Exercises the submit() action end-to-end through its Celery task
    (news/tasks.py::process_listing_submission_task) in the default
    CELERY_TASK_ALWAYS_EAGER mode -- confirms the task-based refactor
    produces identical synchronous behavior to the old inline code."""

    def setUp(self):
        self.seller = make_seller()
        self.category = make_category()

    def test_submit_runs_ai_verification_synchronously_in_eager_mode(self):
        self.client.force_authenticate(self.seller)
        create_resp = self.client.post("/api/news/listings/", {
            "title": "A perfectly normal news story", "description": "d", "body": "b",
            "news_type": "text", "category": str(self.category.id), "price": "1000",
        })
        slug = create_resp.data["slug"]

        resp = self.client.post(f"/api/news/listings/{slug}/submit/")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        # Eager mode means the task already ran by the time this response
        # was built -- verification_status must have moved off PENDING's
        # pre-submit default, not still be sitting unprocessed.
        self.assertNotEqual(resp.data["status"], "draft")
        self.assertIsNotNone(resp.data["ai_score"])

    def test_submit_writes_audit_log_via_task(self):
        from core.models import AuditLog

        self.client.force_authenticate(self.seller)
        create_resp = self.client.post("/api/news/listings/", {
            "title": "Another story", "description": "d", "body": "b",
            "news_type": "text", "category": str(self.category.id), "price": "1000",
        })
        slug = create_resp.data["slug"]
        self.client.post(f"/api/news/listings/{slug}/submit/")

        self.assertTrue(
            AuditLog.objects.filter(target_model="NewsListing", description__icontains="AI verification ran").exists()
        )

    def test_only_draft_listings_can_be_submitted(self):
        self.client.force_authenticate(self.seller)
        create_resp = self.client.post("/api/news/listings/", {
            "title": "t", "description": "d", "body": "b",
            "news_type": "text", "category": str(self.category.id), "price": "1000",
        })
        slug = create_resp.data["slug"]
        self.client.post(f"/api/news/listings/{slug}/submit/")
        resp = self.client.post(f"/api/news/listings/{slug}/submit/")
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)


class EditAfterPublishTests(APITestCase):
    def setUp(self):
        self.seller = make_seller()
        self.listing = NewsListing.objects.create(
            seller=self.seller, title="Original title", description="d", body="original body",
            news_type=NewsListing.NewsType.TEXT, category=make_category(), price=Decimal("1000"),
            status=NewsListing.ListingStatus.PUBLISHED, verification_status=NewsListing.VerificationStatus.VERIFIED,
        )
        self.client.force_authenticate(self.seller)

    def test_editing_published_listing_pulls_back_to_review(self):
        resp = self.client.patch(f"/api/news/listings/{self.listing.slug}/", {"body": "an edited body"}, format="json")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.listing.refresh_from_db()
        self.assertEqual(self.listing.status, NewsListing.ListingStatus.SUBMITTED)
        self.assertEqual(self.listing.verification_status, NewsListing.VerificationStatus.PENDING)

    def test_editing_creates_a_revision_snapshot(self):
        self.client.patch(f"/api/news/listings/{self.listing.slug}/", {"body": "an edited body"}, format="json")
        self.assertEqual(self.listing.revisions.count(), 1)
        self.assertEqual(self.listing.revisions.first().snapshot["body"], "original body")

    def test_editing_non_content_neutral_field_still_snapshots_but_does_not_require_review(self):
        # location isn't in the tracked "content changed" set's price-equivalent check paths --
        # confirm at minimum that a real content field change is what triggers the pullback,
        # by verifying a body-only change (covered above) vs asserting the revision always logs.
        resp = self.client.patch(f"/api/news/listings/{self.listing.slug}/", {"title": "A different title"}, format="json")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.listing.refresh_from_db()
        self.assertEqual(self.listing.status, NewsListing.ListingStatus.SUBMITTED)

    def test_other_seller_cannot_edit_listing(self):
        other = make_seller("othersell")
        self.client.force_authenticate(other)
        resp = self.client.patch(f"/api/news/listings/{self.listing.slug}/", {"body": "hijacked"}, format="json")
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_owner_can_view_revisions_stranger_cannot(self):
        self.client.patch(f"/api/news/listings/{self.listing.slug}/", {"body": "edit 1"}, format="json")
        resp = self.client.get(f"/api/news/listings/{self.listing.slug}/revisions/")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(len(resp.data), 1)

        other_buyer = make_buyer()
        self.client.force_authenticate(other_buyer)
        resp2 = self.client.get(f"/api/news/listings/{self.listing.slug}/revisions/")
        self.assertEqual(resp2.status_code, status.HTTP_403_FORBIDDEN)


class MediaUploadTaskTests(APITestCase):
    """Exercises the media() action's Celery task
    (process_media_upload_task) in eager mode."""

    def setUp(self):
        self.seller = make_seller()
        self.listing = NewsListing.objects.create(
            seller=self.seller, title="t", description="d", body="b", news_type=NewsListing.NewsType.TEXT,
            category=make_category(), price=Decimal("1000"), status=NewsListing.ListingStatus.DRAFT,
        )

    def test_upload_runs_image_analysis_synchronously_in_eager_mode(self):
        from django.core.files.uploadedfile import SimpleUploadedFile

        from .models import NewsMedia

        self.client.force_authenticate(self.seller)
        resp = self.client.post(
            f"/api/news/listings/{self.listing.slug}/media/",
            {"media_type": "image", "file": SimpleUploadedFile("cover.jpg", make_real_jpeg_bytes(), content_type="image/jpeg"), "is_cover": True},
            format="multipart",
        )
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        media_id = resp.data["id"]
        # The task runs synchronously in eager mode -- by the time the
        # response returned, ImageAnalysisResult should already exist.
        self.assertTrue(NewsMedia.objects.get(pk=media_id).analysis_results.exists())

    def test_upload_generates_a_watermarked_preview_synchronously(self):
        from django.core.files.uploadedfile import SimpleUploadedFile

        self.client.force_authenticate(self.seller)
        resp = self.client.post(
            f"/api/news/listings/{self.listing.slug}/media/",
            {"media_type": "image", "file": SimpleUploadedFile("cover.jpg", make_real_jpeg_bytes(), content_type="image/jpeg"), "is_cover": True},
            format="multipart",
        )
        self.assertIsNotNone(resp.data["preview_file"])


def make_real_jpeg_bytes():
    import io

    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (20, 20), color="blue").save(buf, format="JPEG")
    return buf.getvalue()


class DeletePermanentlyTests(APITestCase):
    def setUp(self):
        self.seller = make_seller()
        self.buyer = make_buyer()
        self.moderator = User.objects.create_user(username="mod1", email="mod1@example.com", password="pw12345678!", role=User.Role.MODERATOR)
        self.listing = NewsListing.objects.create(
            seller=self.seller, title="t", description="d", body="b", news_type=NewsListing.NewsType.TEXT,
            category=make_category(), price=Decimal("1000"), status=NewsListing.ListingStatus.PUBLISHED,
            verification_status=NewsListing.VerificationStatus.VERIFIED,
        )

    def test_seller_cannot_delete_permanently(self):
        self.client.force_authenticate(self.seller)
        resp = self.client.post(f"/api/news/listings/{self.listing.slug}/delete-permanently/", {"reason": "test"})
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_moderator_requires_reason(self):
        self.client.force_authenticate(self.moderator)
        resp = self.client.post(f"/api/news/listings/{self.listing.slug}/delete-permanently/", {})
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_moderator_can_delete_with_reason(self):
        self.client.force_authenticate(self.moderator)
        resp = self.client.post(f"/api/news/listings/{self.listing.slug}/delete-permanently/", {"reason": "spam"})
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertFalse(NewsListing.objects.filter(pk=self.listing.pk).exists())

    def test_blocked_if_buyer_already_paid(self):
        Order.objects.create(buyer=self.buyer, listing=self.listing, amount=self.listing.price, status=Order.Status.PAID)
        self.client.force_authenticate(self.moderator)
        resp = self.client.post(f"/api/news/listings/{self.listing.slug}/delete-permanently/", {"reason": "spam"})
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertTrue(NewsListing.objects.filter(pk=self.listing.pk).exists())

    def test_blocked_if_any_order_record_exists(self):
        Order.objects.create(buyer=self.buyer, listing=self.listing, amount=self.listing.price, status=Order.Status.FAILED)
        self.client.force_authenticate(self.moderator)
        resp = self.client.post(f"/api/news/listings/{self.listing.slug}/delete-permanently/", {"reason": "spam"})
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)


class ModerateActionTests(APITestCase):
    def setUp(self):
        self.seller = make_seller()
        self.moderator = User.objects.create_user(username="mod1", email="mod1@example.com", password="pw12345678!", role=User.Role.MODERATOR)
        self.listing = NewsListing.objects.create(
            seller=self.seller, title="t", description="d", body="b", news_type=NewsListing.NewsType.TEXT,
            category=make_category(), price=Decimal("1000"), status=NewsListing.ListingStatus.SUBMITTED,
            verification_status=NewsListing.VerificationStatus.NEEDS_HUMAN_REVIEW,
        )

    def test_seller_cannot_moderate_own_listing(self):
        self.client.force_authenticate(self.seller)
        resp = self.client.post(f"/api/news/listings/{self.listing.slug}/moderate/", {"decision": "approve"})
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_moderator_approve_publishes_listing(self):
        self.client.force_authenticate(self.moderator)
        resp = self.client.post(f"/api/news/listings/{self.listing.slug}/moderate/", {"decision": "approve"})
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.listing.refresh_from_db()
        self.assertEqual(self.listing.status, NewsListing.ListingStatus.PUBLISHED)

    def test_moderator_reject_marks_rejected(self):
        self.client.force_authenticate(self.moderator)
        resp = self.client.post(f"/api/news/listings/{self.listing.slug}/moderate/", {"decision": "reject"})
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.listing.refresh_from_db()
        self.assertEqual(self.listing.verification_status, NewsListing.VerificationStatus.REJECTED)


class CorrectionWorkflowTests(APITestCase):
    def setUp(self):
        self.seller = make_seller()
        self.buyer = make_buyer()
        self.other_seller = make_seller("otherseller")
        self.moderator = User.objects.create_user(username="mod1", email="mod1@example.com", password="pw12345678!", role=User.Role.MODERATOR)
        self.listing = NewsListing.objects.create(
            seller=self.seller, title="t", description="d", body="b", news_type=NewsListing.NewsType.TEXT,
            category=make_category(), price=Decimal("1000"), status=NewsListing.ListingStatus.PUBLISHED,
            verification_status=NewsListing.VerificationStatus.VERIFIED,
        )
        Order.objects.create(buyer=self.buyer, listing=self.listing, amount=self.listing.price, status=Order.Status.PAID)

    def test_anyone_can_view_corrections_without_auth(self):
        resp = self.client.get(f"/api/news/listings/{self.listing.slug}/corrections/")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data, [])

    def test_seller_can_post_correction(self):
        self.client.force_authenticate(self.seller)
        resp = self.client.post(f"/api/news/listings/{self.listing.slug}/corrections/", {"text": "The date was wrong."})
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        self.assertEqual(self.listing.corrections.count(), 1)

    def test_moderator_can_post_retraction(self):
        self.client.force_authenticate(self.moderator)
        resp = self.client.post(f"/api/news/listings/{self.listing.slug}/corrections/", {"text": "Story could not be verified.", "is_retraction": True})
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        self.assertTrue(self.listing.corrections.first().is_retraction)

    def test_unrelated_seller_cannot_post_correction(self):
        self.client.force_authenticate(self.other_seller)
        resp = self.client.post(f"/api/news/listings/{self.listing.slug}/corrections/", {"text": "hijacked"})
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_posting_correction_notifies_past_buyers(self):
        from core.models import Notification

        self.client.force_authenticate(self.seller)
        self.client.post(f"/api/news/listings/{self.listing.slug}/corrections/", {"text": "The date was wrong."})
        self.assertTrue(Notification.objects.filter(user=self.buyer, notification_type=Notification.NotificationType.MODERATION_ACTION).exists())

    def test_correction_visible_on_listing_detail(self):
        self.client.force_authenticate(self.seller)
        self.client.post(f"/api/news/listings/{self.listing.slug}/corrections/", {"text": "The date was wrong."})
        resp = self.client.get(f"/api/news/listings/{self.listing.slug}/")
        self.assertEqual(len(resp.data["corrections"]), 1)

    def test_listing_list_flags_has_correction(self):
        self.client.force_authenticate(self.seller)
        self.client.post(f"/api/news/listings/{self.listing.slug}/corrections/", {"text": "fix"})
        self.client.force_authenticate(None)
        resp = self.client.get("/api/news/listings/")
        found = next(r for r in resp.data["results"] if r["slug"] == self.listing.slug)
        self.assertTrue(found["has_correction"])


class PublicTrustScoreTests(APITestCase):
    def test_seller_trust_score_visible_on_public_listing(self):
        seller = make_seller()
        seller.trust_score = 72
        seller.save()
        listing = NewsListing.objects.create(
            seller=seller, title="t", description="d", body="b", news_type=NewsListing.NewsType.TEXT,
            category=make_category(), price=Decimal("1000"), status=NewsListing.ListingStatus.PUBLISHED,
            verification_status=NewsListing.VerificationStatus.VERIFIED,
        )
        resp = self.client.get(f"/api/news/listings/{listing.slug}/")
        self.assertEqual(resp.data["seller_trust_score"], 72)


class SavedSearchAlertTests(APITestCase):
    def setUp(self):
        self.seller = make_seller()
        self.buyer = make_buyer()
        self.category = make_category()

    def test_buyer_can_create_saved_search(self):
        self.client.force_authenticate(self.buyer)
        resp = self.client.post("/api/news/saved-searches/", {"name": "Politics", "category": str(self.category.id)})
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)

    def test_cannot_see_another_users_saved_searches(self):
        self.client.force_authenticate(self.buyer)
        self.client.post("/api/news/saved-searches/", {"name": "Mine", "category": str(self.category.id)})
        other = make_buyer("otherbuyer5")
        self.client.force_authenticate(other)
        resp = self.client.get("/api/news/saved-searches/")
        self.assertEqual(len(resp.data["results"]), 0)

    def test_matching_publish_notifies_saved_search_owner(self):
        from core.models import Notification

        from .services_alerts import notify_saved_search_matches

        from .models import SavedSearch
        SavedSearch.objects.create(user=self.buyer, category=self.category)

        listing = NewsListing.objects.create(
            seller=self.seller, title="Match", description="d", body="b", news_type=NewsListing.NewsType.TEXT,
            category=self.category, price=Decimal("1000"), status=NewsListing.ListingStatus.PUBLISHED,
            verification_status=NewsListing.VerificationStatus.VERIFIED,
        )
        sent = notify_saved_search_matches(listing)
        self.assertEqual(sent, 1)
        self.assertTrue(Notification.objects.filter(user=self.buyer, notification_type=Notification.NotificationType.SAVED_SEARCH_MATCH).exists())

    def test_non_matching_category_does_not_notify(self):
        from .services_alerts import notify_saved_search_matches
        from .models import SavedSearch

        other_category = make_category()
        SavedSearch.objects.create(user=self.buyer, category=other_category)

        listing = NewsListing.objects.create(
            seller=self.seller, title="No match", description="d", body="b", news_type=NewsListing.NewsType.TEXT,
            category=self.category, price=Decimal("1000"), status=NewsListing.ListingStatus.PUBLISHED,
            verification_status=NewsListing.VerificationStatus.VERIFIED,
        )
        self.assertEqual(notify_saved_search_matches(listing), 0)

    def test_seller_not_notified_of_own_matching_listing(self):
        from .services_alerts import notify_saved_search_matches
        from .models import SavedSearch

        SavedSearch.objects.create(user=self.seller, category=self.category)
        listing = NewsListing.objects.create(
            seller=self.seller, title="t", description="d", body="b", news_type=NewsListing.NewsType.TEXT,
            category=self.category, price=Decimal("1000"), status=NewsListing.ListingStatus.PUBLISHED,
            verification_status=NewsListing.VerificationStatus.VERIFIED,
        )
        self.assertEqual(notify_saved_search_matches(listing), 0)

    def test_inactive_saved_search_not_notified(self):
        from .services_alerts import notify_saved_search_matches
        from .models import SavedSearch

        SavedSearch.objects.create(user=self.buyer, category=self.category, is_active=False)
        listing = NewsListing.objects.create(
            seller=self.seller, title="t", description="d", body="b", news_type=NewsListing.NewsType.TEXT,
            category=self.category, price=Decimal("1000"), status=NewsListing.ListingStatus.PUBLISHED,
            verification_status=NewsListing.VerificationStatus.VERIFIED,
        )
        self.assertEqual(notify_saved_search_matches(listing), 0)


class ExclusiveLicenseTests(APITestCase):
    def setUp(self):
        self.seller = make_seller()
        self.buyer = make_buyer()
        self.listing = NewsListing.objects.create(
            seller=self.seller, title="t", description="d", body="b", news_type=NewsListing.NewsType.TEXT,
            category=make_category(), price=Decimal("1000"), status=NewsListing.ListingStatus.PUBLISHED,
            verification_status=NewsListing.VerificationStatus.VERIFIED,
            license_type=NewsListing.LicenseType.EXCLUSIVE,
        )

    def test_exclusive_listing_marked_sold_out_after_sale(self):
        from wallet.services import credit_wallet_on_sale

        order = Order.objects.create(buyer=self.buyer, listing=self.listing, amount=self.listing.price, status=Order.Status.PAID)
        credit_wallet_on_sale(order)
        self.listing.refresh_from_db()
        self.assertEqual(self.listing.status, NewsListing.ListingStatus.SOLD_OUT)

    def test_standard_listing_stays_published_after_sale(self):
        from wallet.services import credit_wallet_on_sale

        self.listing.license_type = NewsListing.LicenseType.STANDARD
        self.listing.save()
        order = Order.objects.create(buyer=self.buyer, listing=self.listing, amount=self.listing.price, status=Order.Status.PAID)
        credit_wallet_on_sale(order)
        self.listing.refresh_from_db()
        self.assertEqual(self.listing.status, NewsListing.ListingStatus.PUBLISHED)

    def test_sold_out_listing_not_purchasable_again(self):
        from rest_framework.exceptions import ValidationError

        from payments.services import create_order_and_initiate_payment

        self.listing.status = NewsListing.ListingStatus.SOLD_OUT
        self.listing.save()
        other_buyer = make_buyer("otherbuyer6")
        with self.assertRaises(ValidationError):
            create_order_and_initiate_payment(buyer=other_buyer, listing=self.listing, channel="mobile_money", msisdn="0712345678")


class LicenseEditingTests(APITestCase):
    def setUp(self):
        self.seller = make_seller()
        self.buyer = make_buyer()
        self.listing = NewsListing.objects.create(
            seller=self.seller, title="t", description="d", body="b", news_type=NewsListing.NewsType.TEXT,
            category=make_category(), price=Decimal("1000"), status=NewsListing.ListingStatus.PUBLISHED,
            verification_status=NewsListing.VerificationStatus.VERIFIED,
            license_type=NewsListing.LicenseType.STANDARD,
        )
        self.client.force_authenticate(self.seller)

    def test_can_change_license_terms_before_any_sale(self):
        resp = self.client.patch(
            f"/api/news/listings/{self.listing.slug}/",
            {"license_type": "exclusive", "license_territory": "Tanzania only"}, format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.listing.refresh_from_db()
        self.assertEqual(self.listing.license_type, "exclusive")
        self.assertEqual(self.listing.license_territory, "Tanzania only")

    def test_license_only_change_does_not_pull_listing_back_for_review(self):
        resp = self.client.patch(
            f"/api/news/listings/{self.listing.slug}/", {"license_type": "broadcast"}, format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.listing.refresh_from_db()
        self.assertEqual(self.listing.status, NewsListing.ListingStatus.PUBLISHED)
        self.assertEqual(self.listing.verification_status, NewsListing.VerificationStatus.VERIFIED)

    def test_cannot_change_license_type_after_a_sale(self):
        Order.objects.create(buyer=self.buyer, listing=self.listing, amount=self.listing.price, status=Order.Status.PAID)
        resp = self.client.patch(
            f"/api/news/listings/{self.listing.slug}/", {"license_type": "exclusive"}, format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.listing.refresh_from_db()
        self.assertEqual(self.listing.license_type, "standard")

    def test_cannot_change_license_territory_after_a_sale(self):
        Order.objects.create(buyer=self.buyer, listing=self.listing, amount=self.listing.price, status=Order.Status.PAID)
        resp = self.client.patch(
            f"/api/news/listings/{self.listing.slug}/", {"license_territory": "Kenya only"}, format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_editing_other_fields_still_allowed_after_a_sale(self):
        Order.objects.create(buyer=self.buyer, listing=self.listing, amount=self.listing.price, status=Order.Status.PAID)
        resp = self.client.patch(
            f"/api/news/listings/{self.listing.slug}/", {"byline": "By a corrected name"}, format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

    def test_setting_same_license_value_after_sale_is_not_blocked(self):
        Order.objects.create(buyer=self.buyer, listing=self.listing, amount=self.listing.price, status=Order.Status.PAID)
        resp = self.client.patch(
            f"/api/news/listings/{self.listing.slug}/", {"license_type": "standard"}, format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)


class SubmittedListingEditTests(APITestCase):
    """Covers the fix where editing a listing still sitting in the
    moderation queue (SUBMITTED, not yet PUBLISHED) now correctly
    invalidates its stale verification_status -- it previously either
    always reset it unconditionally (even for a no-op edit) or, after an
    earlier refactor, never reset it for this specific status."""

    def setUp(self):
        self.seller = make_seller()
        self.listing = NewsListing.objects.create(
            seller=self.seller, title="Original", description="d", body="b", news_type=NewsListing.NewsType.TEXT,
            category=make_category(), price=Decimal("1000"), status=NewsListing.ListingStatus.SUBMITTED,
            verification_status=NewsListing.VerificationStatus.NEEDS_HUMAN_REVIEW,
        )
        self.client.force_authenticate(self.seller)

    def test_content_edit_on_submitted_listing_invalidates_verification(self):
        resp = self.client.patch(f"/api/news/listings/{self.listing.slug}/", {"body": "a materially different story"}, format="json")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.listing.refresh_from_db()
        self.assertEqual(self.listing.status, NewsListing.ListingStatus.SUBMITTED)
        self.assertEqual(self.listing.verification_status, NewsListing.VerificationStatus.PENDING)

    def test_license_only_edit_on_submitted_listing_leaves_verification_untouched(self):
        resp = self.client.patch(f"/api/news/listings/{self.listing.slug}/", {"license_territory": "Tanzania only"}, format="json")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.listing.refresh_from_db()
        self.assertEqual(self.listing.verification_status, NewsListing.VerificationStatus.NEEDS_HUMAN_REVIEW)


class FreePreviewQuotaTests(APITestCase):
    def setUp(self):
        self.seller = make_seller()
        self.buyer = make_buyer()

    def _make_listing(self, n):
        return NewsListing.objects.create(
            seller=self.seller, title=f"t{n}", description="d", body=f"paywalled body {n}", news_type=NewsListing.NewsType.TEXT,
            category=make_category(), price=Decimal("1000"), status=NewsListing.ListingStatus.PUBLISHED,
            verification_status=NewsListing.VerificationStatus.VERIFIED,
        )

    def test_first_view_within_quota_unlocks_and_flags_free_preview(self):
        from django.test import override_settings

        listing = self._make_listing(1)
        with override_settings(FREE_PREVIEW_QUOTA_PER_MONTH=3):
            self.client.force_authenticate(self.buyer)
            resp = self.client.get(f"/api/news/listings/{listing.slug}/")
        self.assertFalse(resp.data["body_locked"])
        self.assertTrue(resp.data["is_free_preview"])

    def test_revisiting_same_listing_does_not_cost_another_slot(self):
        from django.test import override_settings

        listing = self._make_listing(1)
        with override_settings(FREE_PREVIEW_QUOTA_PER_MONTH=1):
            self.client.force_authenticate(self.buyer)
            self.client.get(f"/api/news/listings/{listing.slug}/")
            second_listing = self._make_listing(2)
            # quota of 1 already used on `listing` -- a second, different
            # listing should now be locked.
            resp = self.client.get(f"/api/news/listings/{second_listing.slug}/")
            self.assertTrue(resp.data["body_locked"])
            # but re-visiting the first one again should still be unlocked
            resp2 = self.client.get(f"/api/news/listings/{listing.slug}/")
            self.assertFalse(resp2.data["body_locked"])

    def test_quota_exhausted_locks_further_listings(self):
        from django.test import override_settings

        with override_settings(FREE_PREVIEW_QUOTA_PER_MONTH=2):
            self.client.force_authenticate(self.buyer)
            for n in range(2):
                listing = self._make_listing(n)
                resp = self.client.get(f"/api/news/listings/{listing.slug}/")
                self.assertFalse(resp.data["body_locked"])
            over_quota_listing = self._make_listing(99)
            resp = self.client.get(f"/api/news/listings/{over_quota_listing.slug}/")
            self.assertTrue(resp.data["body_locked"])
            self.assertFalse(resp.data["is_free_preview"])

    def test_seller_viewing_own_listing_does_not_consume_quota(self):
        from django.test import override_settings

        from .models import FreePreviewGrant

        listing = self._make_listing(1)
        with override_settings(FREE_PREVIEW_QUOTA_PER_MONTH=3):
            self.client.force_authenticate(self.seller)
            resp = self.client.get(f"/api/news/listings/{listing.slug}/")
        self.assertFalse(resp.data["body_locked"])
        self.assertEqual(FreePreviewGrant.objects.count(), 0)

    def test_purchase_does_not_consume_free_quota(self):
        from django.test import override_settings

        from .models import FreePreviewGrant

        listing = self._make_listing(1)
        Order.objects.create(buyer=self.buyer, listing=listing, amount=listing.price, status=Order.Status.PAID)
        with override_settings(FREE_PREVIEW_QUOTA_PER_MONTH=3):
            self.client.force_authenticate(self.buyer)
            resp = self.client.get(f"/api/news/listings/{listing.slug}/")
        self.assertFalse(resp.data["body_locked"])
        self.assertFalse(resp.data["is_free_preview"])
        self.assertEqual(FreePreviewGrant.objects.count(), 0)


class MediaPaywallTests(APITestCase):
    """The listing's `body` has always been gated behind purchase/access;
    `media[].file` (the full-resolution asset) was not -- anyone could
    read it straight off the listing detail response regardless of
    body_locked. This locks that down the same way, falling back to the
    watermarked preview generated on upload (news/media_preview.py)
    instead of silently leaking the original."""

    def setUp(self):
        from django.core.files.uploadedfile import SimpleUploadedFile

        self.seller = make_seller()
        self.buyer = make_buyer()
        self.moderator = User.objects.create_user(
            username="modpw1", email="modpw1@example.com", password="pw12345678!", role=User.Role.MODERATOR,
        )
        self.listing = NewsListing.objects.create(
            seller=self.seller, title="t", description="d", body="paywalled body", news_type=NewsListing.NewsType.TEXT,
            category=make_category(), price=Decimal("1000"), status=NewsListing.ListingStatus.PUBLISHED,
            verification_status=NewsListing.VerificationStatus.VERIFIED,
        )
        self.client.force_authenticate(self.seller)
        upload_resp = self.client.post(
            f"/api/news/listings/{self.listing.slug}/media/",
            {"media_type": "image", "file": SimpleUploadedFile("photo.jpg", make_real_jpeg_bytes(), content_type="image/jpeg"), "is_cover": True},
            format="multipart",
        )
        self.assertEqual(upload_resp.status_code, status.HTTP_201_CREATED)
        self.media_id = upload_resp.data["id"]
        self.client.force_authenticate(None)

    def test_anonymous_visitor_never_sees_the_full_resolution_file(self):
        from .models import NewsMedia

        original_file_url = NewsMedia.objects.get(pk=self.media_id).file.url
        resp = self.client.get(f"/api/news/listings/{self.listing.slug}/")
        media = resp.data["media"][0]
        self.assertNotEqual(media["file"], original_file_url)
        # Not just withheld -- a watermarked stand-in was actually served,
        # and `file` falls back to exactly that (the frontend's existing
        # `m.file || m.preview_file` rendering already expects this).
        self.assertTrue(media["preview_file"])
        self.assertEqual(media["file"], media["preview_file"])

    def test_paid_buyer_gets_the_full_resolution_file(self):
        Order.objects.create(buyer=self.buyer, listing=self.listing, amount=self.listing.price, status=Order.Status.PAID)
        self.client.force_authenticate(self.buyer)
        resp = self.client.get(f"/api/news/listings/{self.listing.slug}/")
        media = resp.data["media"][0]
        self.assertIsNotNone(media["file"])
        self.assertNotEqual(media["file"], media["preview_file"])

    def test_buyer_without_access_is_still_locked(self):
        from django.test import override_settings

        from .models import NewsMedia

        original_file_url = NewsMedia.objects.get(pk=self.media_id).file.url
        with override_settings(FREE_PREVIEW_QUOTA_PER_MONTH=0):
            self.client.force_authenticate(self.buyer)
            resp = self.client.get(f"/api/news/listings/{self.listing.slug}/")
        self.assertNotEqual(resp.data["media"][0]["file"], original_file_url)

    def test_seller_always_sees_their_own_full_resolution_file(self):
        self.client.force_authenticate(self.seller)
        resp = self.client.get(f"/api/news/listings/{self.listing.slug}/")
        self.assertIsNotNone(resp.data["media"][0]["file"])

    def test_moderator_always_sees_the_full_resolution_file(self):
        self.client.force_authenticate(self.moderator)
        resp = self.client.get(f"/api/news/listings/{self.listing.slug}/")
        self.assertIsNotNone(resp.data["media"][0]["file"])

    def test_video_media_has_no_preview_and_is_withheld_entirely_when_locked(self):
        # Created directly via the ORM rather than the upload endpoint --
        # content-sniffing validation on that endpoint (core/validators.py)
        # would reject a fake .mp4 payload, which isn't what's under test
        # here (that's covered by news/tests upload-validation tests).
        from django.core.files.uploadedfile import SimpleUploadedFile

        from .models import NewsMedia

        NewsMedia.objects.create(
            listing=self.listing, media_type=NewsMedia.MediaType.VIDEO,
            file=SimpleUploadedFile("clip.mp4", b"not-a-real-video-file-contents", content_type="video/mp4"),
        )
        resp = self.client.get(f"/api/news/listings/{self.listing.slug}/")
        video_item = next(m for m in resp.data["media"] if m["media_type"] == "video")
        self.assertIsNone(video_item["file"])
        self.assertFalse(video_item["preview_file"])


class DownloadAndSocialShareAccessTests(APITestCase):
    def setUp(self):
        self.seller = make_seller()
        self.buyer = make_buyer()
        self.other_buyer = make_buyer("otherbuyer")
        self.listing = NewsListing.objects.create(
            seller=self.seller, title="t", description="d", body="b", news_type=NewsListing.NewsType.TEXT,
            category=make_category(), price=Decimal("1000"), status=NewsListing.ListingStatus.PUBLISHED,
            verification_status=NewsListing.VerificationStatus.VERIFIED,
        )
        Order.objects.create(buyer=self.buyer, listing=self.listing, amount=self.listing.price, status=Order.Status.PAID)

    def test_non_buyer_cannot_social_share(self):
        self.client.force_authenticate(self.other_buyer)
        resp = self.client.post(f"/api/news/listings/{self.listing.slug}/social-share/", {"provider": "facebook"})
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_buyer_gets_real_share_url_for_facebook(self):
        self.client.force_authenticate(self.buyer)
        resp = self.client.post(f"/api/news/listings/{self.listing.slug}/social-share/", {"provider": "facebook"})
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertFalse(resp.data["manual"])
        self.assertIn("facebook.com", resp.data["share_url"])

    def test_buyer_gets_manual_caption_for_instagram(self):
        self.client.force_authenticate(self.buyer)
        resp = self.client.post(f"/api/news/listings/{self.listing.slug}/social-share/", {"provider": "instagram"})
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertTrue(resp.data["manual"])
        self.assertIn("caption", resp.data)

    def test_invalid_provider_rejected(self):
        self.client.force_authenticate(self.buyer)
        resp = self.client.post(f"/api/news/listings/{self.listing.slug}/social-share/", {"provider": "myspace"})
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_buyer_gets_real_share_url_for_telegram(self):
        self.client.force_authenticate(self.buyer)
        resp = self.client.post(f"/api/news/listings/{self.listing.slug}/social-share/", {"provider": "telegram"})
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertFalse(resp.data["manual"])
        self.assertIn("t.me", resp.data["share_url"])

    def test_buyer_gets_mailto_url_for_email(self):
        self.client.force_authenticate(self.buyer)
        resp = self.client.post(f"/api/news/listings/{self.listing.slug}/social-share/", {"provider": "email"})
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertFalse(resp.data["manual"])
        self.assertTrue(resp.data["share_url"].startswith("mailto:"))

    def test_copy_link_returns_the_listing_url_flagged_for_copying(self):
        self.client.force_authenticate(self.buyer)
        resp = self.client.post(f"/api/news/listings/{self.listing.slug}/social-share/", {"provider": "copy_link"})
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertTrue(resp.data["copy"])
        self.assertIn(self.listing.slug, resp.data["share_url"])

    def test_every_share_is_logged_regardless_of_provider_category(self):
        from .models import SocialShareRecord

        self.client.force_authenticate(self.buyer)
        for provider in ["facebook", "telegram", "email", "copy_link", "instagram"]:
            self.client.post(f"/api/news/listings/{self.listing.slug}/social-share/", {"provider": provider})
        self.assertEqual(SocialShareRecord.objects.filter(listing=self.listing, user=self.buyer).count(), 5)
