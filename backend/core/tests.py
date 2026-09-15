from django.contrib.auth import get_user_model
from rest_framework import status
from rest_framework.test import APIClient, APITestCase

from .models import APIKey, SupportTicket

User = get_user_model()


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
