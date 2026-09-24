from unittest.mock import Mock, patch

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from rest_framework import status
from rest_framework.test import APITestCase

from core.models import TermsAcceptance

from .models import CorporateVerification, IdentityVerification

User = get_user_model()

REGISTER_URL = "/api/accounts/register/"


class RegistrationTermsTests(APITestCase):
    def _payload(self, **overrides):
        payload = {
            "username": "newuser", "email": "newuser@example.com",
            "password": "pw12345678!", "password_confirm": "pw12345678!",
            "role": "buyer", "terms_accepted": True,
        }
        payload.update(overrides)
        return payload

    def test_registration_without_terms_accepted_fails(self):
        resp = self.client.post(REGISTER_URL, self._payload(terms_accepted=False), format="json")
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("terms_accepted", resp.data)

    def test_registration_with_terms_accepted_succeeds(self):
        resp = self.client.post(REGISTER_URL, self._payload(), format="json")
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)

    def test_registration_records_terms_acceptance(self):
        self.client.post(REGISTER_URL, self._payload(), format="json")
        user = User.objects.get(username="newuser")
        self.assertEqual(TermsAcceptance.objects.filter(user=user).count(), 1)

    def test_journalist_tier_rejected_for_buyer_role(self):
        resp = self.client.post(REGISTER_URL, self._payload(role="buyer", journalist_tier="professional"), format="json")
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_journalist_tier_accepted_for_journalist_role(self):
        resp = self.client.post(
            REGISTER_URL,
            self._payload(username="journo", email="journo@example.com", role="journalist", journalist_tier="professional"),
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        user = User.objects.get(username="journo")
        self.assertEqual(user.journalist_tier, "professional")

    def test_media_house_requires_organization_name(self):
        resp = self.client.post(
            REGISTER_URL,
            self._payload(username="mh1", email="mh1@example.com", role="media_house"),
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_cannot_self_assign_staff_role(self):
        resp = self.client.post(
            REGISTER_URL,
            self._payload(username="wannabemod", email="wannabemod@example.com", role="moderator"),
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)


class TermsStatusTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="u1", email="u1@example.com", password="pw12345678!")

    def test_needs_acceptance_true_when_never_accepted(self):
        self.client.force_authenticate(self.user)
        resp = self.client.get("/api/terms/status/")
        self.assertTrue(resp.data["needs_acceptance"])

    def test_needs_acceptance_false_after_accepting_current_version(self):
        from django.conf import settings

        self.client.force_authenticate(self.user)
        self.client.post("/api/terms/accept/", {"version": settings.TERMS_VERSION})
        resp = self.client.get("/api/terms/status/")
        self.assertFalse(resp.data["needs_acceptance"])

    def test_needs_acceptance_true_after_version_bump(self):
        TermsAcceptance.objects.create(user=self.user, version="2020-01-01")
        self.client.force_authenticate(self.user)
        resp = self.client.get("/api/terms/status/")
        self.assertTrue(resp.data["needs_acceptance"])


class KYCReviewTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="journo1", email="journo1@example.com", password="pw12345678!",
            role=User.Role.JOURNALIST, journalist_tier=User.JournalistTier.PROFESSIONAL,
        )
        self.moderator = User.objects.create_user(username="mod1", email="mod1@example.com", password="pw12345678!", role=User.Role.MODERATOR)

    def test_approving_with_press_credential_sets_press_credentialed(self):
        verification = IdentityVerification.objects.create(
            user=self.user, id_type=IdentityVerification.IdType.NIDA,
            front_image="kyc/front.jpg", selfie_image="kyc/selfie.jpg",
            press_credential_image="kyc/press.jpg",
        )
        self.client.force_authenticate(self.moderator)
        resp = self.client.patch(f"/api/accounts/kyc/{verification.id}/review/", {"status": "verified"})
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.user.refresh_from_db()
        self.assertTrue(self.user.is_press_credentialed)
        self.assertEqual(self.user.id_verification_status, User.VerificationStatus.VERIFIED)

    def test_approving_without_press_credential_does_not_set_flag(self):
        verification = IdentityVerification.objects.create(
            user=self.user, id_type=IdentityVerification.IdType.NIDA,
            front_image="kyc/front.jpg", selfie_image="kyc/selfie.jpg",
        )
        self.client.force_authenticate(self.moderator)
        self.client.patch(f"/api/accounts/kyc/{verification.id}/review/", {"status": "verified"})
        self.user.refresh_from_db()
        self.assertFalse(self.user.is_press_credentialed)

    def test_rejecting_sets_user_status_rejected(self):
        verification = IdentityVerification.objects.create(
            user=self.user, id_type=IdentityVerification.IdType.NIDA,
            front_image="kyc/front.jpg", selfie_image="kyc/selfie.jpg",
        )
        self.client.force_authenticate(self.moderator)
        self.client.patch(f"/api/accounts/kyc/{verification.id}/review/", {"status": "rejected", "rejection_reason": "blurry"})
        self.user.refresh_from_db()
        self.assertEqual(self.user.id_verification_status, User.VerificationStatus.REJECTED)

    def test_seller_cannot_review_own_kyc(self):
        verification = IdentityVerification.objects.create(
            user=self.user, id_type=IdentityVerification.IdType.NIDA,
            front_image="kyc/front.jpg", selfie_image="kyc/selfie.jpg",
        )
        self.client.force_authenticate(self.user)
        resp = self.client.patch(f"/api/accounts/kyc/{verification.id}/review/", {"status": "verified"})
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)


class CorporateVerificationTests(APITestCase):
    def setUp(self):
        self.buyer = User.objects.create_user(username="corpbuyer", email="corpbuyer@example.com", password="pw12345678!", role=User.Role.BUYER)
        self.moderator = User.objects.create_user(username="mod1", email="mod1@example.com", password="pw12345678!", role=User.Role.MODERATOR)

    def test_buyer_can_submit_kyb(self):
        from django.core.files.uploadedfile import SimpleUploadedFile

        self.client.force_authenticate(self.buyer)
        resp = self.client.post("/api/accounts/corporate-verifications/", {
            "company_name": "Acme Corp",
            "business_license": SimpleUploadedFile("license.pdf", b"%PDF-1.4\nfake but valid-looking pdf bytes"),
            "company_registration": SimpleUploadedFile("reg.pdf", b"%PDF-1.4\nfake but valid-looking pdf bytes"),
        }, format="multipart")
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)

    def test_approval_sets_is_corporate(self):
        verification = CorporateVerification.objects.create(
            user=self.buyer, company_name="Acme Corp",
            business_license="docs/license.pdf", company_registration="docs/reg.pdf",
        )
        self.client.force_authenticate(self.moderator)
        resp = self.client.patch(f"/api/accounts/corporate-verifications/{verification.id}/review/", {"status": "verified"})
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.buyer.refresh_from_db()
        self.assertTrue(self.buyer.is_corporate)

    def test_rejection_does_not_set_is_corporate(self):
        verification = CorporateVerification.objects.create(
            user=self.buyer, company_name="Acme Corp",
            business_license="docs/license.pdf", company_registration="docs/reg.pdf",
        )
        self.client.force_authenticate(self.moderator)
        self.client.patch(f"/api/accounts/corporate-verifications/{verification.id}/review/", {"status": "rejected", "rejection_reason": "invalid docs"})
        self.buyer.refresh_from_db()
        self.assertFalse(self.buyer.is_corporate)


class LoginGatingTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="u1", email="u1@example.com", password="pw12345678!")

    def test_banned_user_cannot_login(self):
        self.user.is_banned = True
        self.user.save()
        resp = self.client.post("/api/accounts/login/", {"email": "u1@example.com", "password": "pw12345678!"})
        # A ValidationError (business-rule rejection, not a bad-credentials
        # AuthenticationFailed) maps to 400 under DRF's default exception
        # handling -- this is existing, intentional behavior, not a bug
        # introduced here.
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertNotIn("access", resp.data)

    def test_suspended_user_cannot_login(self):
        self.user.is_suspended = True
        self.user.save()
        resp = self.client.post("/api/accounts/login/", {"email": "u1@example.com", "password": "pw12345678!"})
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertNotIn("access", resp.data)

    def test_active_user_can_login(self):
        resp = self.client.post("/api/accounts/login/", {"email": "u1@example.com", "password": "pw12345678!"})
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertIn("access", resp.data)


class FaceMatchTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="fmuser", email="fmuser@example.com", password="pw12345678!")
        self.verification = IdentityVerification.objects.create(
            user=self.user, id_type=IdentityVerification.IdType.NIDA,
            front_image=SimpleUploadedFile("front.jpg", b"fake-front-bytes"),
            selfie_image=SimpleUploadedFile("selfie.jpg", b"fake-selfie-bytes"),
        )

    def test_no_op_when_not_configured(self):
        from .face_match import run_face_match

        with patch("accounts.face_match.requests.post") as mock_post:
            run_face_match(self.verification)
        mock_post.assert_not_called()
        self.verification.refresh_from_db()
        self.assertIsNone(self.verification.face_match_score)
        self.assertIsNone(self.verification.liveness_passed)

    @override_settings(KYC_FACE_MATCH_API_URL="https://kyc.example/match", KYC_FACE_MATCH_API_KEY="fake-key")
    def test_saves_score_and_liveness_from_a_configured_vendor(self):
        from .face_match import run_face_match

        mock_resp = Mock()
        mock_resp.json.return_value = {"face_match_score": 92.5, "liveness_passed": True}
        with patch("accounts.face_match.requests.post", return_value=mock_resp) as mock_post:
            run_face_match(self.verification)
        mock_post.assert_called_once()
        self.verification.refresh_from_db()
        self.assertEqual(self.verification.face_match_score, 92.5)
        self.assertTrue(self.verification.liveness_passed)

    @override_settings(KYC_FACE_MATCH_API_URL="https://kyc.example/match", KYC_FACE_MATCH_API_KEY="fake-key")
    def test_never_raises_and_leaves_fields_unset_on_vendor_failure(self):
        from .face_match import run_face_match

        with patch("accounts.face_match.requests.post", side_effect=ConnectionError("boom")):
            run_face_match(self.verification)  # must not raise
        self.verification.refresh_from_db()
        self.assertIsNone(self.verification.face_match_score)
        self.assertIsNone(self.verification.liveness_passed)

    @override_settings(KYC_FACE_MATCH_API_URL="https://kyc.example/match", KYC_FACE_MATCH_API_KEY="fake-key")
    def test_kyc_submission_runs_face_match_without_blocking(self):
        import io

        from PIL import Image

        def real_jpeg_bytes():
            buf = io.BytesIO()
            Image.new("RGB", (20, 20), color="blue").save(buf, format="JPEG")
            return buf.getvalue()

        self.client.force_authenticate(self.user)
        mock_resp = Mock()
        mock_resp.json.return_value = {"face_match_score": 80.0, "liveness_passed": True}
        with patch("accounts.face_match.requests.post", return_value=mock_resp):
            resp = self.client.post("/api/accounts/kyc/", {
                "id_type": "nida",
                "front_image": SimpleUploadedFile("front2.jpg", real_jpeg_bytes(), content_type="image/jpeg"),
                "selfie_image": SimpleUploadedFile("selfie2.jpg", real_jpeg_bytes(), content_type="image/jpeg"),
            }, format="multipart")
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        created = IdentityVerification.objects.get(pk=resp.data["id"])
        self.assertEqual(created.face_match_score, 80.0)


class PasswordResetTests(APITestCase):
    def setUp(self):
        from django.core.cache import cache

        cache.clear()  # DRF throttling is cache-backed, not DB-backed -- doesn't reset via transaction rollback
        self.user = User.objects.create_user(username="pwreset1", email="pwreset1@example.com", password="OldPass123!")

    def _extract_uid_token(self):
        from django.contrib.auth.tokens import default_token_generator
        from django.utils.encoding import force_bytes
        from django.utils.http import urlsafe_base64_encode

        uid = urlsafe_base64_encode(force_bytes(self.user.pk))
        token = default_token_generator.make_token(self.user)
        return uid, token

    def test_request_returns_generic_response_for_known_email(self):
        resp = self.client.post("/api/accounts/password-reset/request/", {"email": "pwreset1@example.com"})
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertIn("If an account exists", resp.data["detail"])

    def test_request_returns_identical_generic_response_for_unknown_email(self):
        resp = self.client.post("/api/accounts/password-reset/request/", {"email": "nobody-here@example.com"})
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertIn("If an account exists", resp.data["detail"])

    def test_request_sends_a_real_email_with_a_working_link(self):
        from django.core import mail

        self.client.post("/api/accounts/password-reset/request/", {"email": "pwreset1@example.com"})
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("reset-password.html", mail.outbox[0].body)
        self.assertIn(self.user.email, mail.outbox[0].to)

    def test_confirm_with_valid_token_changes_password(self):
        uid, token = self._extract_uid_token()
        resp = self.client.post("/api/accounts/password-reset/confirm/", {
            "uid": uid, "token": token, "new_password": "BrandNewPass456!",
        })
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password("BrandNewPass456!"))

    def test_confirm_with_garbage_token_is_rejected(self):
        uid, _ = self._extract_uid_token()
        resp = self.client.post("/api/accounts/password-reset/confirm/", {
            "uid": uid, "token": "not-a-real-token", "new_password": "BrandNewPass456!",
        })
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password("OldPass123!"))

    def test_confirm_with_garbage_uid_is_rejected(self):
        resp = self.client.post("/api/accounts/password-reset/confirm/", {
            "uid": "not-valid-base64!!", "token": "whatever", "new_password": "BrandNewPass456!",
        })
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_token_cannot_be_reused_after_password_already_changed(self):
        uid, token = self._extract_uid_token()
        first = self.client.post("/api/accounts/password-reset/confirm/", {
            "uid": uid, "token": token, "new_password": "BrandNewPass456!",
        })
        self.assertEqual(first.status_code, status.HTTP_200_OK)

        second = self.client.post("/api/accounts/password-reset/confirm/", {
            "uid": uid, "token": token, "new_password": "AnotherPass789!",
        })
        self.assertEqual(second.status_code, status.HTTP_400_BAD_REQUEST)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password("BrandNewPass456!"))

    def test_confirm_enforces_password_strength_rules(self):
        uid, token = self._extract_uid_token()
        resp = self.client.post("/api/accounts/password-reset/confirm/", {
            "uid": uid, "token": token, "new_password": "12345",
        })
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_reset_blacklists_existing_refresh_tokens(self):
        login_resp = self.client.post("/api/accounts/login/", {"email": "pwreset1@example.com", "password": "OldPass123!"})
        refresh_token = login_resp.data["refresh"]

        uid, token = self._extract_uid_token()
        self.client.post("/api/accounts/password-reset/confirm/", {
            "uid": uid, "token": token, "new_password": "BrandNewPass456!",
        })

        refresh_resp = self.client.post("/api/accounts/login/refresh/", {"refresh": refresh_token})
        self.assertEqual(refresh_resp.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_request_is_throttled(self):
        for _ in range(5):
            resp = self.client.post("/api/accounts/password-reset/request/", {"email": "pwreset1@example.com"})
            self.assertEqual(resp.status_code, status.HTTP_200_OK)
        throttled = self.client.post("/api/accounts/password-reset/request/", {"email": "pwreset1@example.com"})
        self.assertEqual(throttled.status_code, status.HTTP_429_TOO_MANY_REQUESTS)


class PublicSellerSearchTests(APITestCase):
    def setUp(self):
        self.seller = User.objects.create_user(
            username="searchable_seller", email="searchable_seller@example.com", password="Pass123!",
            role=User.Role.SELLER, organization_name="Acme News Co",
        )
        self.journalist = User.objects.create_user(
            username="searchable_journo", email="searchable_journo@example.com", password="Pass123!",
            role=User.Role.JOURNALIST,
        )
        self.buyer = User.objects.create_user(
            username="searchable_buyer", email="searchable_buyer@example.com", password="Pass123!",
            role=User.Role.BUYER,
        )
        self.suspended_seller = User.objects.create_user(
            username="searchable_suspended", email="searchable_suspended@example.com", password="Pass123!",
            role=User.Role.SELLER, is_suspended=True,
        )
        self.banned_seller = User.objects.create_user(
            username="searchable_banned", email="searchable_banned@example.com", password="Pass123!",
            role=User.Role.SELLER, is_banned=True,
        )

    def test_no_auth_required(self):
        resp = self.client.get("/api/accounts/search/", {"q": "searchable"})
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

    def test_finds_seller_by_username(self):
        resp = self.client.get("/api/accounts/search/", {"q": "searchable_seller"})
        usernames = [u["username"] for u in resp.data]
        self.assertIn("searchable_seller", usernames)

    def test_finds_seller_by_organization_name(self):
        resp = self.client.get("/api/accounts/search/", {"q": "Acme"})
        usernames = [u["username"] for u in resp.data]
        self.assertIn("searchable_seller", usernames)

    def test_finds_journalist(self):
        resp = self.client.get("/api/accounts/search/", {"q": "searchable_journo"})
        usernames = [u["username"] for u in resp.data]
        self.assertIn("searchable_journo", usernames)

    def test_excludes_buyers(self):
        resp = self.client.get("/api/accounts/search/", {"q": "searchable"})
        usernames = [u["username"] for u in resp.data]
        self.assertNotIn("searchable_buyer", usernames)

    def test_excludes_suspended_accounts(self):
        resp = self.client.get("/api/accounts/search/", {"q": "searchable"})
        usernames = [u["username"] for u in resp.data]
        self.assertNotIn("searchable_suspended", usernames)

    def test_excludes_banned_accounts(self):
        resp = self.client.get("/api/accounts/search/", {"q": "searchable"})
        usernames = [u["username"] for u in resp.data]
        self.assertNotIn("searchable_banned", usernames)

    def test_empty_query_returns_no_results(self):
        resp = self.client.get("/api/accounts/search/")
        self.assertEqual(resp.data, [])

    def test_response_excludes_sensitive_fields(self):
        resp = self.client.get("/api/accounts/search/", {"q": "searchable_seller"})
        result = resp.data[0]
        self.assertNotIn("email", result)
        self.assertNotIn("phone_number", result)
        self.assertNotIn("id_verification_status", result)
