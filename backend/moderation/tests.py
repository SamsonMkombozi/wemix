from django.contrib.auth import get_user_model
from rest_framework import status
from rest_framework.test import APITestCase

from .models import AntiCircumventionFlag, ModerationQueueItem, UserReport

User = get_user_model()


class UserReportTests(APITestCase):
    def setUp(self):
        self.reporter = User.objects.create_user(username="reporter", email="reporter@example.com", password="pw12345678!")
        self.reported = User.objects.create_user(username="reported", email="reported@example.com", password="pw12345678!")
        self.moderator = User.objects.create_user(username="mod1", email="mod1@example.com", password="pw12345678!", role=User.Role.MODERATOR)

    def test_authenticated_user_can_file_report(self):
        self.client.force_authenticate(self.reporter)
        resp = self.client.post("/api/moderation/reports/", {"reported_user": str(self.reported.id), "reason": "harassment", "details": "d"})
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)

    def test_filing_report_creates_queue_item(self):
        self.client.force_authenticate(self.reporter)
        self.client.post("/api/moderation/reports/", {"reported_user": str(self.reported.id), "reason": "harassment", "details": "d"})
        self.assertEqual(ModerationQueueItem.objects.filter(item_type=ModerationQueueItem.ItemType.USER_REPORT).count(), 1)

    def test_regular_user_cannot_view_report_queue(self):
        self.client.force_authenticate(self.reporter)
        resp = self.client.get("/api/moderation/reports/queue/")
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_moderator_can_view_report_queue(self):
        UserReport.objects.create(reporter=self.reporter, reported_user=self.reported, reason=UserReport.Reason.HARASSMENT)
        self.client.force_authenticate(self.moderator)
        resp = self.client.get("/api/moderation/reports/queue/")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(len(resp.data["results"]), 1)


class AntiCircumventionFlagReviewTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="flagged", email="flagged@example.com", password="pw12345678!")
        self.moderator = User.objects.create_user(username="mod1", email="mod1@example.com", password="pw12345678!", role=User.Role.MODERATOR)
        self.flag = AntiCircumventionFlag.objects.create(
            user=self.user, target_type="listing_description", detected_pattern=AntiCircumventionFlag.DetectedPattern.PHONE_NUMBER,
            matched_text="0712***678", confidence=90, severity=AntiCircumventionFlag.Severity.HIGH,
        )

    def test_moderator_can_apply_warning(self):
        self.client.force_authenticate(self.moderator)
        resp = self.client.post(f"/api/moderation/flags/{self.flag.id}/review/", {"action": "warning"})
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.flag.refresh_from_db()
        self.assertEqual(self.flag.action_taken, "warning")
        self.assertTrue(self.flag.reviewed_by_human)

    def test_non_moderator_cannot_review_flag(self):
        self.client.force_authenticate(self.user)
        resp = self.client.post(f"/api/moderation/flags/{self.flag.id}/review/", {"action": "warning"})
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)


class ModerationQueueResolveTests(APITestCase):
    def setUp(self):
        self.moderator = User.objects.create_user(username="mod1", email="mod1@example.com", password="pw12345678!", role=User.Role.MODERATOR)
        import uuid

        self.item = ModerationQueueItem.objects.create(item_type=ModerationQueueItem.ItemType.USER_REPORT, related_object_id=uuid.uuid4())

    def test_assign_sets_in_progress(self):
        self.client.force_authenticate(self.moderator)
        resp = self.client.post(f"/api/moderation/queue/{self.item.id}/assign/", {})
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.item.refresh_from_db()
        self.assertEqual(self.item.status, ModerationQueueItem.Status.IN_PROGRESS)
        self.assertEqual(self.item.assigned_to_id, self.moderator.id)

    def test_resolve_sets_resolved_status(self):
        self.client.force_authenticate(self.moderator)
        resp = self.client.post(f"/api/moderation/queue/{self.item.id}/resolve/", {"status": "resolved"})
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.item.refresh_from_db()
        self.assertEqual(self.item.status, ModerationQueueItem.Status.RESOLVED)

    def test_default_queue_list_shows_only_open(self):
        self.item.status = ModerationQueueItem.Status.RESOLVED
        self.item.save()
        self.client.force_authenticate(self.moderator)
        resp = self.client.get("/api/moderation/queue/")
        self.assertEqual(len(resp.data["results"]), 0)
