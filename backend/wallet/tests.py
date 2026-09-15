from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from rest_framework import status
from rest_framework.test import APITestCase

from news.models import Category, NewsListing
from payments.models import Order

from .models import CompanyPayoutAccount, CompanyWithdrawalRequest, PayoutAccount, Wallet, WalletHold, WalletTransaction, WithdrawalRequest
from .services import (
    credit_wallet_on_sale,
    debit_wallet_for_withdrawal,
    get_available_balance,
    get_or_create_platform_wallet,
    get_or_create_user_wallet,
    release_matured_holds,
)

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


def make_paid_order(buyer, listing):
    return Order.objects.create(buyer=buyer, listing=listing, amount=listing.price, status=Order.Status.PAID)


class CreditWalletOnSaleTests(TestCase):
    def setUp(self):
        self.seller = make_seller()
        self.buyer = make_buyer()
        self.listing = make_listing(self.seller, price="10000")
        self.order = make_paid_order(self.buyer, self.listing)

    @override_settings(PLATFORM_COMMISSION_RATE=0.10)
    def test_splits_commission_correctly(self):
        credit_wallet_on_sale(self.order)
        seller_wallet = get_or_create_user_wallet(self.seller)
        platform_wallet = get_or_create_platform_wallet()
        seller_wallet.refresh_from_db()
        platform_wallet.refresh_from_db()
        self.assertEqual(seller_wallet.balance, Decimal("9000.00"))
        self.assertEqual(platform_wallet.balance, Decimal("1000.00"))

    def test_idempotent_does_not_double_credit(self):
        credit_wallet_on_sale(self.order)
        credit_wallet_on_sale(self.order)
        seller_wallet = get_or_create_user_wallet(self.seller)
        seller_wallet.refresh_from_db()
        self.assertEqual(seller_wallet.balance, Decimal("9000.00"))
        self.assertEqual(
            WalletTransaction.objects.filter(order=self.order, entry_type=WalletTransaction.EntryType.SALE_CREDIT).count(), 1
        )

    def test_increments_purchase_count(self):
        credit_wallet_on_sale(self.order)
        self.listing.refresh_from_db()
        self.assertEqual(self.listing.purchase_count, 1)

    def test_ledger_balance_after_matches_wallet_balance(self):
        credit_wallet_on_sale(self.order)
        seller_wallet = get_or_create_user_wallet(self.seller)
        seller_wallet.refresh_from_db()
        entry = WalletTransaction.objects.get(wallet=seller_wallet, entry_type=WalletTransaction.EntryType.SALE_CREDIT)
        self.assertEqual(entry.balance_after, seller_wallet.balance)


@override_settings(WALLET_HOLD_PERIOD_HOURS=72)
class WalletHoldTests(TestCase):
    def setUp(self):
        self.seller = make_seller()
        self.buyer = make_buyer()
        self.listing = make_listing(self.seller, price="10000")
        self.order = make_paid_order(self.buyer, self.listing)

    def test_sale_credit_creates_a_matching_hold(self):
        credit_wallet_on_sale(self.order)
        wallet = get_or_create_user_wallet(self.seller)
        self.assertEqual(WalletHold.objects.filter(wallet=wallet, released=False).count(), 1)
        self.assertEqual(wallet.pending_balance, Decimal("9000.00"))

    def test_available_balance_excludes_unmatured_hold(self):
        credit_wallet_on_sale(self.order)
        wallet = get_or_create_user_wallet(self.seller)
        self.assertEqual(get_available_balance(wallet), Decimal("0.00"))

    def test_available_balance_after_hold_matures(self):
        from datetime import timedelta

        from django.utils import timezone

        credit_wallet_on_sale(self.order)
        wallet = get_or_create_user_wallet(self.seller)
        WalletHold.objects.filter(wallet=wallet).update(matures_at=timezone.now() - timedelta(hours=1))

        available = get_available_balance(wallet)
        wallet.refresh_from_db()
        self.assertEqual(available, Decimal("9000.00"))
        self.assertEqual(wallet.pending_balance, Decimal("0.00"))
        self.assertTrue(WalletHold.objects.get(wallet=wallet).released)

    def test_withdrawal_blocked_while_funds_held(self):
        from rest_framework.test import APIClient

        credit_wallet_on_sale(self.order)
        payout = PayoutAccount.objects.create(
            user=self.seller, account_type=PayoutAccount.AccountType.MOBILE_MONEY,
            account_number="0712345678", account_name="Seller One", status=PayoutAccount.Status.VERIFIED,
        )
        client = APIClient()
        client.force_authenticate(self.seller)
        resp = client.post("/api/wallet/withdrawals/", {"amount": "1000", "payout_account": str(payout.id)})
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("held", str(resp.data).lower())

    def test_refund_before_maturity_releases_hold_without_going_negative(self):
        from payments.models import RefundRequest
        from payments.services import process_refund

        credit_wallet_on_sale(self.order)
        wallet = get_or_create_user_wallet(self.seller)
        refund = RefundRequest.objects.create(order=self.order, requested_by=self.buyer, reason="bad", amount=self.order.amount)

        process_refund(refund)

        wallet.refresh_from_db()
        self.assertEqual(wallet.balance, Decimal("0.00"))
        self.assertEqual(wallet.pending_balance, Decimal("0.00"))
        self.assertEqual(get_available_balance(wallet), Decimal("0.00"))
        self.assertTrue(WalletHold.objects.get(wallet=wallet).released)


class DebitWalletForWithdrawalTests(TestCase):
    def setUp(self):
        self.seller = make_seller()
        self.wallet = get_or_create_user_wallet(self.seller)
        self.wallet.balance = Decimal("5000")
        self.wallet.save()

    def test_debits_available_balance(self):
        payout = PayoutAccount.objects.create(
            user=self.seller, account_type=PayoutAccount.AccountType.MOBILE_MONEY,
            account_number="0712345678", account_name="Seller One", status=PayoutAccount.Status.VERIFIED,
        )
        withdrawal = WithdrawalRequest.objects.create(
            wallet=self.wallet, payout_account=payout, amount=Decimal("2000"),
            destination_type=WithdrawalRequest.Destination.MOBILE_MONEY,
        )
        debit_wallet_for_withdrawal(withdrawal)
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.balance, Decimal("3000"))

    def test_raises_on_insufficient_balance(self):
        payout = PayoutAccount.objects.create(
            user=self.seller, account_type=PayoutAccount.AccountType.MOBILE_MONEY,
            account_number="0712345678", account_name="Seller One", status=PayoutAccount.Status.VERIFIED,
        )
        withdrawal = WithdrawalRequest.objects.create(
            wallet=self.wallet, payout_account=payout, amount=Decimal("999999"),
            destination_type=WithdrawalRequest.Destination.MOBILE_MONEY,
        )
        with self.assertRaises(ValueError):
            debit_wallet_for_withdrawal(withdrawal)


@override_settings(WITHDRAWAL_DAILY_LIMIT=2_000_000, WITHDRAWAL_WEEKLY_LIMIT=8_000_000, WITHDRAWAL_MONTHLY_LIMIT=20_000_000, WITHDRAWAL_RISK_FLAG_THRESHOLD=1_500_000)
class WithdrawalLimitsAPITests(APITestCase):
    def setUp(self):
        self.seller = make_seller()
        self.wallet = get_or_create_user_wallet(self.seller)
        self.wallet.balance = Decimal("50000000")
        self.wallet.save()
        self.payout_account = PayoutAccount.objects.create(
            user=self.seller, account_type=PayoutAccount.AccountType.MOBILE_MONEY,
            account_number="0712345678", account_name="Seller One", status=PayoutAccount.Status.VERIFIED,
        )
        self.client.force_authenticate(self.seller)

    def _withdraw(self, amount):
        return self.client.post("/api/wallet/withdrawals/", {"amount": str(amount), "payout_account": str(self.payout_account.id)})

    def test_single_withdrawal_within_limit_succeeds(self):
        resp = self._withdraw(1_000_000)
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)

    def test_cumulative_daily_limit_enforced(self):
        self.assertEqual(self._withdraw(1_000_000).status_code, status.HTTP_201_CREATED)
        resp = self._withdraw(1_500_000)
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("24 hours", str(resp.data))

    def test_risk_flag_set_above_threshold(self):
        resp = self._withdraw(1_600_000)
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        self.assertTrue(WithdrawalRequest.objects.get(id=resp.data["id"]).risk_flagged)

    def test_risk_flag_not_set_below_threshold(self):
        resp = self._withdraw(500_000)
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        self.assertFalse(WithdrawalRequest.objects.get(id=resp.data["id"]).risk_flagged)

    def test_cannot_withdraw_more_than_balance(self):
        resp = self._withdraw(99_000_000)
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_cannot_use_unverified_payout_account(self):
        unverified = PayoutAccount.objects.create(
            user=self.seller, account_type=PayoutAccount.AccountType.MOBILE_MONEY,
            account_number="0700000000", account_name="Seller One", status=PayoutAccount.Status.PENDING,
        )
        resp = self.client.post("/api/wallet/withdrawals/", {"amount": "1000", "payout_account": str(unverified.id)})
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_cannot_use_another_users_payout_account(self):
        other_seller = make_seller("otherseller")
        other_account = PayoutAccount.objects.create(
            user=other_seller, account_type=PayoutAccount.AccountType.MOBILE_MONEY,
            account_number="0711111111", account_name="Other Seller", status=PayoutAccount.Status.VERIFIED,
        )
        resp = self.client.post("/api/wallet/withdrawals/", {"amount": "1000", "payout_account": str(other_account.id)})
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)


class CompanyTreasuryPermissionTests(APITestCase):
    def setUp(self):
        self.admin = User.objects.create_user(username="admin1", email="admin1@example.com", password="pw12345678!", role=User.Role.ADMIN)
        self.super_admin = User.objects.create_user(username="sa1", email="sa1@example.com", password="pw12345678!", role=User.Role.SUPER_ADMIN)
        self.seller = make_seller()
        platform_wallet = get_or_create_platform_wallet()
        platform_wallet.balance = Decimal("1000000")
        platform_wallet.save()
        self.account = CompanyPayoutAccount.objects.create(
            account_type=PayoutAccount.AccountType.BANK_ACCOUNT, account_number="1234", account_name="WEMIX Ltd",
        )

    def test_admin_can_request_withdrawal(self):
        self.client.force_authenticate(self.admin)
        resp = self.client.post("/api/wallet/company/withdrawals/", {"amount": "1000", "payout_account": str(self.account.id)})
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)

    def test_seller_cannot_request_company_withdrawal(self):
        self.client.force_authenticate(self.seller)
        resp = self.client.post("/api/wallet/company/withdrawals/", {"amount": "1000", "payout_account": str(self.account.id)})
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_admin_cannot_approve_company_withdrawal(self):
        self.client.force_authenticate(self.admin)
        create_resp = self.client.post("/api/wallet/company/withdrawals/", {"amount": "1000", "payout_account": str(self.account.id)})
        withdrawal_id = create_resp.data["id"]
        resp = self.client.post(f"/api/wallet/company/withdrawals/{withdrawal_id}/review/", {"status": "completed"})
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_super_admin_can_approve_and_debits_platform_wallet(self):
        self.client.force_authenticate(self.admin)
        create_resp = self.client.post("/api/wallet/company/withdrawals/", {"amount": "1000", "payout_account": str(self.account.id)})
        withdrawal_id = create_resp.data["id"]

        self.client.force_authenticate(self.super_admin)
        resp = self.client.post(f"/api/wallet/company/withdrawals/{withdrawal_id}/review/", {"status": "completed"})
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

        platform_wallet = get_or_create_platform_wallet()
        platform_wallet.refresh_from_db()
        self.assertEqual(platform_wallet.balance, Decimal("999000"))

    def test_super_admin_reject_requires_reason(self):
        self.client.force_authenticate(self.admin)
        create_resp = self.client.post("/api/wallet/company/withdrawals/", {"amount": "1000", "payout_account": str(self.account.id)})
        withdrawal_id = create_resp.data["id"]

        self.client.force_authenticate(self.super_admin)
        resp = self.client.post(f"/api/wallet/company/withdrawals/{withdrawal_id}/review/", {"status": "rejected"})
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)


class PayoutAccountNameMatchTests(APITestCase):
    def test_name_match_score_none_without_verified_kyc(self):
        seller = make_seller()
        account = PayoutAccount.objects.create(
            user=seller, account_type=PayoutAccount.AccountType.MOBILE_MONEY,
            account_number="0712345678", account_name="John Doe",
        )
        from .serializers import PayoutAccountSerializer
        data = PayoutAccountSerializer(account).data
        self.assertIsNone(data["name_match_score"])

    def test_name_match_score_high_for_matching_name(self):
        from accounts.models import IdentityVerification

        seller = make_seller()
        IdentityVerification.objects.create(
            user=seller, id_type=IdentityVerification.IdType.NIDA,
            front_image="kyc/front.jpg", selfie_image="kyc/selfie.jpg",
            status=IdentityVerification.Status.VERIFIED, ocr_full_name="John Doe",
        )
        account = PayoutAccount.objects.create(
            user=seller, account_type=PayoutAccount.AccountType.MOBILE_MONEY,
            account_number="0712345678", account_name="John Doe",
        )
        from .serializers import PayoutAccountSerializer
        data = PayoutAccountSerializer(account).data
        self.assertEqual(data["name_match_score"], 100)

    def test_name_match_score_low_for_mismatched_name(self):
        from accounts.models import IdentityVerification

        seller = make_seller()
        IdentityVerification.objects.create(
            user=seller, id_type=IdentityVerification.IdType.NIDA,
            front_image="kyc/front.jpg", selfie_image="kyc/selfie.jpg",
            status=IdentityVerification.Status.VERIFIED, ocr_full_name="John Doe",
        )
        account = PayoutAccount.objects.create(
            user=seller, account_type=PayoutAccount.AccountType.MOBILE_MONEY,
            account_number="0712345678", account_name="Completely Different Person",
        )
        from .serializers import PayoutAccountSerializer
        data = PayoutAccountSerializer(account).data
        self.assertLess(data["name_match_score"], 50)
