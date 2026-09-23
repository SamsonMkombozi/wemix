from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from rest_framework import status
from rest_framework.test import APITestCase

from news.models import Category, NewsListing
from wallet.services import credit_wallet_on_sale, get_or_create_platform_wallet, get_or_create_user_wallet

from .models import Order, RefundRequest, Subscription, SubscriptionTransaction
from .services import process_refund

User = get_user_model()


def make_seller(username="seller1"):
    return User.objects.create_user(
        username=username, email=f"{username}@example.com", password="pw12345678!",
        role=User.Role.SELLER, id_verification_status=User.VerificationStatus.VERIFIED,
    )


def make_buyer(username="buyer1"):
    return User.objects.create_user(username=username, email=f"{username}@example.com", password="pw12345678!", role=User.Role.BUYER)


def make_listing(seller, price="10000"):
    import uuid

    category = Category.objects.create(name=f"Cat-{uuid.uuid4().hex[:8]}")
    return NewsListing.objects.create(
        seller=seller, title="Test story", description="d", body="b",
        news_type=NewsListing.NewsType.TEXT, category=category,
        price=Decimal(price), status=NewsListing.ListingStatus.PUBLISHED,
        verification_status=NewsListing.VerificationStatus.VERIFIED,
    )


@override_settings(PLATFORM_COMMISSION_RATE=0.10)
class ProcessRefundTests(TestCase):
    def setUp(self):
        self.seller = make_seller()
        self.buyer = make_buyer()
        self.listing = make_listing(self.seller, price="10000")
        self.order = Order.objects.create(buyer=self.buyer, listing=self.listing, amount=self.listing.price, status=Order.Status.PAID)
        credit_wallet_on_sale(self.order)
        self.refund = RefundRequest.objects.create(order=self.order, requested_by=self.buyer, reason="not as described", amount=self.order.amount)

    def test_reverses_seller_and_platform_wallets(self):
        seller_wallet = get_or_create_user_wallet(self.seller)
        platform_wallet = get_or_create_platform_wallet()
        self.assertEqual(seller_wallet.balance, Decimal("9000.00"))
        self.assertEqual(platform_wallet.balance, Decimal("1000.00"))

        process_refund(self.refund)

        seller_wallet.refresh_from_db()
        platform_wallet.refresh_from_db()
        self.assertEqual(seller_wallet.balance, Decimal("0.00"))
        self.assertEqual(platform_wallet.balance, Decimal("0.00"))

    def test_marks_order_refunded(self):
        process_refund(self.refund)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.Status.REFUNDED)

    def test_idempotent_does_not_double_debit(self):
        process_refund(self.refund)
        process_refund(self.refund)
        seller_wallet = get_or_create_user_wallet(self.seller)
        seller_wallet.refresh_from_db()
        self.assertEqual(seller_wallet.balance, Decimal("0.00"))


class RefundRequestAPITests(APITestCase):
    def setUp(self):
        self.seller = make_seller()
        self.buyer = make_buyer()
        self.other_buyer = make_buyer("otherbuyer")
        self.listing = make_listing(self.seller)
        self.paid_order = Order.objects.create(buyer=self.buyer, listing=self.listing, amount=self.listing.price, status=Order.Status.PAID)
        self.unpaid_order = Order.objects.create(
            buyer=self.buyer,
            listing=make_listing(self.seller, price="2000"),
            amount=Decimal("2000"), status=Order.Status.PENDING_PAYMENT,
        )

    def test_buyer_can_request_refund_on_own_paid_order(self):
        self.client.force_authenticate(self.buyer)
        resp = self.client.post("/api/payments/refunds/", {"order": str(self.paid_order.id), "reason": "bad content"})
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)

    def test_cannot_refund_someone_elses_order(self):
        self.client.force_authenticate(self.other_buyer)
        resp = self.client.post("/api/payments/refunds/", {"order": str(self.paid_order.id), "reason": "bad content"})
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_cannot_refund_unpaid_order(self):
        self.client.force_authenticate(self.buyer)
        resp = self.client.post("/api/payments/refunds/", {"order": str(self.unpaid_order.id), "reason": "changed my mind"})
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)


class CreateOrderRulesTests(APITestCase):
    def setUp(self):
        self.seller = make_seller()
        self.buyer = make_buyer()
        self.listing = make_listing(self.seller)

    def test_seller_cannot_buy_own_listing(self):
        from rest_framework.exceptions import ValidationError

        from .services import create_order_and_initiate_payment

        with self.assertRaises(ValidationError):
            create_order_and_initiate_payment(buyer=self.seller, listing=self.listing, channel="mobile_money", msisdn="0712345678")

    def test_cannot_buy_unpublished_listing(self):
        from rest_framework.exceptions import ValidationError

        from .services import create_order_and_initiate_payment

        self.listing.status = NewsListing.ListingStatus.DRAFT
        self.listing.save()
        with self.assertRaises(ValidationError):
            create_order_and_initiate_payment(buyer=self.buyer, listing=self.listing, channel="mobile_money", msisdn="0712345678")

    def test_cannot_buy_already_owned_listing(self):
        from rest_framework.exceptions import ValidationError

        from .services import create_order_and_initiate_payment

        Order.objects.create(buyer=self.buyer, listing=self.listing, amount=self.listing.price, status=Order.Status.PAID)
        with self.assertRaises(ValidationError):
            create_order_and_initiate_payment(buyer=self.buyer, listing=self.listing, channel="mobile_money", msisdn="0712345678")


class OrderDetailAndCancelTests(APITestCase):
    """Backs the checkout.html redesign: a real order summary (listing
    title, seller, amount, payment method) and a way to cancel a
    still-pending order instead of being stuck on a spinner forever."""

    def setUp(self):
        self.seller = make_seller()
        self.buyer = make_buyer()
        self.other_buyer = make_buyer("otherbuyer_checkout")
        self.listing = make_listing(self.seller)
        self.order = Order.objects.create(
            buyer=self.buyer, listing=self.listing, amount=self.listing.price,
            status=Order.Status.PENDING_PAYMENT, payment_provider=Order.PaymentProvider.SELCOM,
        )

    def test_order_detail_includes_summary_fields(self):
        self.client.force_authenticate(self.buyer)
        resp = self.client.get(f"/api/payments/orders/{self.order.id}/")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data["listing_title"], self.listing.title)
        self.assertEqual(resp.data["seller_username"], self.seller.username)
        self.assertEqual(resp.data["payment_provider"], "selcom")

    def test_other_buyer_cannot_view_order_detail(self):
        self.client.force_authenticate(self.other_buyer)
        resp = self.client.get(f"/api/payments/orders/{self.order.id}/")
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)

    def test_buyer_can_cancel_pending_order(self):
        self.client.force_authenticate(self.buyer)
        resp = self.client.post(f"/api/payments/orders/{self.order.id}/cancel/")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.Status.CANCELLED)

    def test_cannot_cancel_an_already_paid_order(self):
        self.order.status = Order.Status.PAID
        self.order.save()
        self.client.force_authenticate(self.buyer)
        resp = self.client.post(f"/api/payments/orders/{self.order.id}/cancel/")
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.Status.PAID)

    def test_other_buyer_cannot_cancel_someone_elses_order(self):
        self.client.force_authenticate(self.other_buyer)
        resp = self.client.post(f"/api/payments/orders/{self.order.id}/cancel/")
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)

    def test_cancelling_frees_the_listing_for_a_new_order(self):
        # unique_active_order_per_buyer_listing only blocks a second
        # pending_payment/paid order -- exercises that constraint
        # directly rather than the external-payment-calling service.
        self.client.force_authenticate(self.buyer)
        self.client.post(f"/api/payments/orders/{self.order.id}/cancel/")
        Order.objects.create(buyer=self.buyer, listing=self.listing, amount=self.listing.price, status=Order.Status.PENDING_PAYMENT)


@override_settings(PLATFORM_COMMISSION_RATE=0.10)
class FinancialReportAPITests(APITestCase):
    def setUp(self):
        self.moderator = User.objects.create_user(username="mod1", email="mod1@example.com", password="pw12345678!", role=User.Role.MODERATOR)
        self.seller = make_seller()
        self.buyer = make_buyer()
        for price in ["1000", "2000", "3000"]:
            listing = make_listing(self.seller, price=price)
            Order.objects.create(buyer=self.buyer, listing=listing, amount=Decimal(price), status=Order.Status.PAID)

    def test_reports_totals_match_paid_orders(self):
        self.client.force_authenticate(self.moderator)
        resp = self.client.get("/api/payments/reports/?period=monthly")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(Decimal(str(resp.data["totals"]["gross_sales"])), Decimal("6000"))
        self.assertEqual(Decimal(str(resp.data["totals"]["platform_commission"])), Decimal("600.00"))
        self.assertEqual(resp.data["totals"]["order_count"], 3)

    def test_buyer_cannot_access_reports(self):
        self.client.force_authenticate(self.buyer)
        resp = self.client.get("/api/payments/reports/?period=monthly")
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_csv_export_returns_csv(self):
        self.client.force_authenticate(self.moderator)
        resp = self.client.get("/api/payments/reports/export/?period=monthly")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp["Content-Type"], "text/csv")

    def test_pdf_export_returns_pdf(self):
        self.client.force_authenticate(self.moderator)
        resp = self.client.get("/api/payments/reports/export-pdf/?period=monthly")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp["Content-Type"], "application/pdf")


FAKE_SELCOM_RESPONSE = {
    "resultcode": "000", "data": [{"payment_gateway_url": "https://selcom.example/pay/abc123"}],
}


@override_settings(PLATFORM_COMMISSION_RATE=0.10)
class SubscriptionTests(APITestCase):
    def setUp(self):
        self.seller = make_seller()
        self.seller.subscription_price = Decimal("5000")
        self.seller.save()
        self.buyer = make_buyer()

    @patch("payments.subscription_services.SelcomClient.create_order_minimal", return_value=FAKE_SELCOM_RESPONSE)
    def test_subscribe_creates_pending_subscription_and_checkout_url(self, mock_selcom):
        self.client.force_authenticate(self.buyer)
        resp = self.client.post("/api/payments/subscriptions/", {"seller": str(self.seller.id), "channel": "mobile_money", "msisdn": "0712345678"})
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        self.assertEqual(resp.data["payment_gateway_url"], "https://selcom.example/pay/abc123")
        self.assertEqual(resp.data["subscription"]["status"], "pending_payment")
        mock_selcom.assert_called_once()

    def test_cannot_subscribe_to_self(self):
        self.client.force_authenticate(self.seller)
        resp = self.client.post("/api/payments/subscriptions/", {"seller": str(self.seller.id)})
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_cannot_subscribe_if_seller_has_no_price_set(self):
        no_sub_seller = make_seller("nosub")
        self.client.force_authenticate(self.buyer)
        resp = self.client.post("/api/payments/subscriptions/", {"seller": str(no_sub_seller.id)})
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    @patch("payments.subscription_services.SelcomClient.verify_webhook_signature", return_value=True)
    @patch("payments.subscription_services.SelcomClient.create_order_minimal", return_value=FAKE_SELCOM_RESPONSE)
    def test_webhook_activates_subscription_and_credits_seller_wallet(self, mock_selcom, mock_verify):
        from payments.subscription_services import handle_selcom_subscription_webhook

        self.client.force_authenticate(self.buyer)
        self.client.post("/api/payments/subscriptions/", {"seller": str(self.seller.id), "channel": "mobile_money", "msisdn": "0712345678"})
        txn = SubscriptionTransaction.objects.get(subscription__seller=self.seller)

        verdict, _ = handle_selcom_subscription_webhook(
            payload={"order_id": txn.selcom_order_id, "payment_status": "COMPLETED", "transid": "TX1"},
            headers={}, source_ip="127.0.0.1",
        )
        self.assertEqual(verdict, "accepted")

        subscription = Subscription.objects.get(subscriber=self.buyer, seller=self.seller)
        self.assertEqual(subscription.status, Subscription.Status.ACTIVE)
        self.assertTrue(subscription.is_currently_active())

        seller_wallet = get_or_create_user_wallet(self.seller)
        seller_wallet.refresh_from_db()
        self.assertEqual(seller_wallet.balance, Decimal("4500.00"))

    def test_active_subscriber_unlocks_sellers_listings(self):
        listing = NewsListing.objects.create(
            seller=self.seller, title="t", description="d", body="paywalled body", news_type=NewsListing.NewsType.TEXT,
            category=Category.objects.create(name="Cat-sub-test"), price=Decimal("1000"),
            status=NewsListing.ListingStatus.PUBLISHED, verification_status=NewsListing.VerificationStatus.VERIFIED,
        )
        from django.utils import timezone
        from datetime import timedelta

        Subscription.objects.create(
            subscriber=self.buyer, seller=self.seller, price=Decimal("5000"),
            status=Subscription.Status.ACTIVE, current_period_end=timezone.now() + timedelta(days=10),
        )
        self.client.force_authenticate(self.buyer)
        resp = self.client.get(f"/api/news/listings/{listing.slug}/")
        self.assertFalse(resp.data["body_locked"])
        self.assertEqual(resp.data["body"], "paywalled body")

    def test_expired_subscriber_does_not_unlock_listing(self):
        listing = NewsListing.objects.create(
            seller=self.seller, title="t", description="d", body="paywalled body", news_type=NewsListing.NewsType.TEXT,
            category=Category.objects.create(name="Cat-sub-test2"), price=Decimal("1000"),
            status=NewsListing.ListingStatus.PUBLISHED, verification_status=NewsListing.VerificationStatus.VERIFIED,
        )
        from django.utils import timezone
        from datetime import timedelta

        Subscription.objects.create(
            subscriber=self.buyer, seller=self.seller, price=Decimal("5000"),
            status=Subscription.Status.ACTIVE, current_period_end=timezone.now() - timedelta(days=1),
        )
        self.client.force_authenticate(self.buyer)
        # Isolate the subscription-expiry check from the separate
        # metered-free-preview feature, which would otherwise also grant
        # access here (correctly, just not what this test is about).
        with override_settings(FREE_PREVIEW_QUOTA_PER_MONTH=0):
            resp = self.client.get(f"/api/news/listings/{listing.slug}/")
        self.assertTrue(resp.data["body_locked"])

    def test_cancel_active_subscription(self):
        from django.utils import timezone
        from datetime import timedelta

        subscription = Subscription.objects.create(
            subscriber=self.buyer, seller=self.seller, price=Decimal("5000"),
            status=Subscription.Status.ACTIVE, current_period_end=timezone.now() + timedelta(days=10),
        )
        self.client.force_authenticate(self.buyer)
        resp = self.client.post(f"/api/payments/subscriptions/{subscription.id}/cancel/")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        subscription.refresh_from_db()
        self.assertEqual(subscription.status, Subscription.Status.CANCELLED)
        # Access continues until period end even though cancelled.
        self.assertTrue(subscription.is_currently_active())

    def test_cannot_cancel_someone_elses_subscription(self):
        from django.utils import timezone
        from datetime import timedelta

        subscription = Subscription.objects.create(
            subscriber=self.buyer, seller=self.seller, price=Decimal("5000"),
            status=Subscription.Status.ACTIVE, current_period_end=timezone.now() + timedelta(days=10),
        )
        other = make_buyer("otherbuyer2")
        self.client.force_authenticate(other)
        resp = self.client.post(f"/api/payments/subscriptions/{subscription.id}/cancel/")
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)

    def test_moderator_can_list_all_subscriptions(self):
        from django.utils import timezone
        from datetime import timedelta

        Subscription.objects.create(
            subscriber=self.buyer, seller=self.seller, price=Decimal("5000"),
            status=Subscription.Status.ACTIVE, current_period_end=timezone.now() + timedelta(days=10),
        )
        moderator = User.objects.create_user(username="mod2", email="mod2@example.com", password="pw12345678!", role=User.Role.MODERATOR)
        self.client.force_authenticate(moderator)
        resp = self.client.get("/api/payments/subscriptions/all/")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(len(resp.data["results"]), 1)

    def test_buyer_cannot_list_all_subscriptions(self):
        self.client.force_authenticate(self.buyer)
        resp = self.client.get("/api/payments/subscriptions/all/")
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_all_subscriptions_filterable_by_status(self):
        from django.utils import timezone
        from datetime import timedelta

        Subscription.objects.create(
            subscriber=self.buyer, seller=self.seller, price=Decimal("5000"),
            status=Subscription.Status.ACTIVE, current_period_end=timezone.now() + timedelta(days=10),
        )
        Subscription.objects.create(
            subscriber=make_buyer("otherbuyer3"), seller=self.seller, price=Decimal("5000"),
            status=Subscription.Status.EXPIRED, current_period_end=timezone.now() - timedelta(days=1),
        )
        moderator = User.objects.create_user(username="mod3", email="mod3@example.com", password="pw12345678!", role=User.Role.MODERATOR)
        self.client.force_authenticate(moderator)
        resp = self.client.get("/api/payments/subscriptions/all/?status=expired")
        self.assertEqual(len(resp.data["results"]), 1)
        self.assertEqual(resp.data["results"][0]["status"], "expired")
