from unittest.mock import Mock, patch

from django.contrib.auth import get_user_model
from django.test import override_settings
from rest_framework import status
from rest_framework.test import APIClient, APITestCase

from .models import APIKey, SupportTicket

User = get_user_model()


AT_SETTINGS = dict(AFRICASTALKING_API_KEY="fake-key", AFRICASTALKING_USERNAME="fake-user")


class SMSClientTests(APITestCase):
    def test_no_op_when_not_configured(self):
        from .sms_client import send_sms

        with patch("core.sms_client.requests.post") as mock_post:
            result = send_sms("+255700000000", "hello")
        self.assertFalse(result)
        mock_post.assert_not_called()

    @override_settings(**AT_SETTINGS)
    def test_returns_true_on_a_successful_recipient(self):
        from .sms_client import send_sms

        mock_resp = Mock()
        mock_resp.json.return_value = {"SMSMessageData": {"Recipients": [{"status": "Success"}]}}
        with patch("core.sms_client.requests.post", return_value=mock_resp) as mock_post:
            result = send_sms("+255700000000", "hello")
        self.assertTrue(result)
        mock_post.assert_called_once()
        self.assertEqual(mock_post.call_args.kwargs["data"]["to"], "+255700000000")

    @override_settings(**AT_SETTINGS)
    def test_returns_false_when_provider_reports_no_success(self):
        from .sms_client import send_sms

        mock_resp = Mock()
        mock_resp.json.return_value = {"SMSMessageData": {"Recipients": [{"status": "InvalidPhoneNumber"}]}}
        with patch("core.sms_client.requests.post", return_value=mock_resp):
            self.assertFalse(send_sms("+255700000000", "hello"))

    @override_settings(**AT_SETTINGS)
    def test_returns_false_and_never_raises_on_network_failure(self):
        with patch("core.sms_client.requests.post", side_effect=ConnectionError("boom")):
            from .sms_client import send_sms

            self.assertFalse(send_sms("+255700000000", "hello"))


class SendNotificationSMSTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="smsuser", email="smsuser@example.com", password="pw12345678!", phone_number="+255700000000",
        )

    def test_send_sms_false_by_default(self):
        from .notifications import send_notification

        with patch("core.sms_client.send_sms") as mock_sms:
            send_notification(user=self.user, notification_type="sale_completed", title="Sold!", send_email=False)
        mock_sms.assert_not_called()

    def test_send_sms_true_calls_sms_client_when_phone_present(self):
        from .notifications import send_notification

        with patch("core.sms_client.send_sms", return_value=True) as mock_sms:
            notification = send_notification(
                user=self.user, notification_type="sale_completed", title="Sold!", message="Nice one.",
                send_email=False, send_sms=True,
            )
        mock_sms.assert_called_once_with("+255700000000", "Sold! - Nice one.")
        self.assertTrue(notification.sms_sent)

    def test_send_sms_skipped_when_user_has_no_phone_number(self):
        from .notifications import send_notification

        no_phone_user = User.objects.create_user(username="nophone", email="nophone@example.com", password="pw12345678!")
        with patch("core.sms_client.send_sms") as mock_sms:
            send_notification(user=no_phone_user, notification_type="sale_completed", title="Sold!", send_email=False, send_sms=True)
        mock_sms.assert_not_called()


class APIKeyTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="u1", email="u1@example.com", password="pw12345678!")

    def test_create_key_returns_raw_value_once(self):
        self.client.force_authenticate(self.user)
        resp = self.client.post("/api/api-keys/", {"name": "Newsroom script"})
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        self.assertIn("raw_key", resp.data)
        self.assertTrue(resp.data["raw_key"].startswith("wmx_"))

    def test_list_never_returns_raw_value(self):
        self.client.force_authenticate(self.user)
        self.client.post("/api/api-keys/", {"name": "k1"})
        resp = self.client.get("/api/api-keys/")
        self.assertNotIn("raw_key", resp.data[0])
        self.assertIn("prefix", resp.data[0])

    def test_key_authenticates_requests(self):
        self.client.force_authenticate(self.user)
        create_resp = self.client.post("/api/api-keys/", {"name": "k1"})
        raw_key = create_resp.data["raw_key"]

        anon_client = APIClient()
        resp = anon_client.get("/api/accounts/me/", HTTP_AUTHORIZATION=f"Api-Key {raw_key}")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data["username"], "u1")

    def test_revoked_key_no_longer_authenticates(self):
        self.client.force_authenticate(self.user)
        create_resp = self.client.post("/api/api-keys/", {"name": "k1"})
        raw_key = create_resp.data["raw_key"]
        key_id = create_resp.data["id"]

        self.client.post(f"/api/api-keys/{key_id}/revoke/")

        anon_client = APIClient()
        resp = anon_client.get("/api/accounts/me/", HTTP_AUTHORIZATION=f"Api-Key {raw_key}")
        self.assertEqual(resp.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_garbage_key_rejected(self):
        anon_client = APIClient()
        resp = anon_client.get("/api/accounts/me/", HTTP_AUTHORIZATION="Api-Key totally-made-up-value")
        self.assertEqual(resp.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_moderator_can_list_a_users_keys(self):
        self.client.force_authenticate(self.user)
        self.client.post("/api/api-keys/", {"name": "k1"})

        moderator = User.objects.create_user(username="mod1", email="mod1@example.com", password="pw12345678!", role=User.Role.MODERATOR)
        self.client.force_authenticate(moderator)
        resp = self.client.get(f"/api/accounts/users/{self.user.id}/api-keys/")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(len(resp.data), 1)

    def test_regular_user_cannot_list_another_users_keys(self):
        other = User.objects.create_user(username="u3", email="u3@example.com", password="pw12345678!")
        self.client.force_authenticate(other)
        resp = self.client.get(f"/api/accounts/users/{self.user.id}/api-keys/")
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_moderator_can_revoke_a_users_key(self):
        self.client.force_authenticate(self.user)
        create_resp = self.client.post("/api/api-keys/", {"name": "k1"})
        raw_key = create_resp.data["raw_key"]
        key_id = create_resp.data["id"]

        moderator = User.objects.create_user(username="mod1", email="mod1@example.com", password="pw12345678!", role=User.Role.MODERATOR)
        self.client.force_authenticate(moderator)
        resp = self.client.post(f"/api/api-keys/{key_id}/admin-revoke/")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

        anon_client = APIClient()
        auth_resp = anon_client.get("/api/accounts/me/", HTTP_AUTHORIZATION=f"Api-Key {raw_key}")
        self.assertEqual(auth_resp.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_regular_user_cannot_admin_revoke(self):
        self.client.force_authenticate(self.user)
        create_resp = self.client.post("/api/api-keys/", {"name": "k1"})
        key_id = create_resp.data["id"]

        other = User.objects.create_user(username="u4", email="u4@example.com", password="pw12345678!")
        self.client.force_authenticate(other)
        resp = self.client.post(f"/api/api-keys/{key_id}/admin-revoke/")
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_cannot_revoke_someone_elses_key(self):
        self.client.force_authenticate(self.user)
        create_resp = self.client.post("/api/api-keys/", {"name": "k1"})
        key_id = create_resp.data["id"]

        other = User.objects.create_user(username="u2", email="u2@example.com", password="pw12345678!")
        self.client.force_authenticate(other)
        resp = self.client.post(f"/api/api-keys/{key_id}/revoke/")
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)


class SupportTicketTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="u1", email="u1@example.com", password="pw12345678!")
        self.other_user = User.objects.create_user(username="u2", email="u2@example.com", password="pw12345678!")
        self.moderator = User.objects.create_user(username="mod1", email="mod1@example.com", password="pw12345678!", role=User.Role.MODERATOR)

    def test_user_can_create_ticket(self):
        self.client.force_authenticate(self.user)
        resp = self.client.post("/api/support-tickets/", {"category": "question", "subject": "Help", "message": "How do I withdraw?"})
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        self.assertEqual(resp.data["status"], "open")

    def test_user_sees_only_own_tickets_by_default(self):
        SupportTicket.objects.create(user=self.user, category="question", subject="mine", message="m")
        SupportTicket.objects.create(user=self.other_user, category="question", subject="not mine", message="m")
        self.client.force_authenticate(self.user)
        resp = self.client.get("/api/support-tickets/")
        subjects = [t["subject"] for t in resp.data["results"]]
        self.assertEqual(subjects, ["mine"])

    def test_staff_can_see_all_tickets_with_flag(self):
        SupportTicket.objects.create(user=self.user, category="question", subject="t1", message="m")
        SupportTicket.objects.create(user=self.other_user, category="question", subject="t2", message="m")
        self.client.force_authenticate(self.moderator)
        resp = self.client.get("/api/support-tickets/?all=1")
        self.assertEqual(len(resp.data["results"]), 2)

    def test_only_owner_or_staff_can_reply(self):
        ticket = SupportTicket.objects.create(user=self.user, category="question", subject="t1", message="m")
        self.client.force_authenticate(self.other_user)
        resp = self.client.post(f"/api/support-tickets/{ticket.id}/reply/", {"message": "butting in"})
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_staff_reply_moves_open_ticket_to_in_progress(self):
        ticket = SupportTicket.objects.create(user=self.user, category="question", subject="t1", message="m")
        self.client.force_authenticate(self.moderator)
        resp = self.client.post(f"/api/support-tickets/{ticket.id}/reply/", {"message": "looking into it"})
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        ticket.refresh_from_db()
        self.assertEqual(ticket.status, SupportTicket.Status.IN_PROGRESS)

    def test_staff_reply_can_set_status_directly(self):
        ticket = SupportTicket.objects.create(user=self.user, category="question", subject="t1", message="m")
        self.client.force_authenticate(self.moderator)
        resp = self.client.post(f"/api/support-tickets/{ticket.id}/reply/", {"message": "done", "status": "resolved"})
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        ticket.refresh_from_db()
        self.assertEqual(ticket.status, SupportTicket.Status.RESOLVED)

    def test_owner_can_reply_to_own_ticket(self):
        ticket = SupportTicket.objects.create(user=self.user, category="question", subject="t1", message="m")
        self.client.force_authenticate(self.user)
        resp = self.client.post(f"/api/support-tickets/{ticket.id}/reply/", {"message": "any update?"})
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(ticket.messages.count(), 1)
