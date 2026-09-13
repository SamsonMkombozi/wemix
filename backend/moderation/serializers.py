from rest_framework import serializers

from .models import AntiCircumventionFlag, ModerationQueueItem, UserReport, UserViolationHistory


class UserReportSerializer(serializers.ModelSerializer):
    class Meta:
        model = UserReport
        fields = ["id", "reported_user", "listing", "reason", "details", "created_at"]
        read_only_fields = ["id", "created_at"]

    def create(self, validated_data):
        validated_data["reporter"] = self.context["request"].user
        return UserReport.objects.create(**validated_data)


class AntiCircumventionFlagSerializer(serializers.ModelSerializer):
    username = serializers.CharField(source="user.username", read_only=True)

    class Meta:
        model = AntiCircumventionFlag
        fields = [
            "id",
            "user",
            "username",
            "listing",
            "target_type",
            "detected_pattern",
            "matched_text",
            "confidence",
            "severity",
            "action_taken",
            "reviewed_by_human",
            "created_at",
        ]
        read_only_fields = fields


class FlagReviewSerializer(serializers.Serializer):
    """Moderator decision on a single AntiCircumventionFlag. Applies the
    chosen enforcement action to the flagged user and updates their
    rolling violation history so future flags escalate correctly."""

    ACTION_CHOICES = ["none", "warning", "content_blocked", "temporary_suspension", "permanent_ban"]
    action = serializers.ChoiceField(choices=ACTION_CHOICES)
    suspension_days = serializers.IntegerField(required=False, min_value=1, default=7)

    def save(self, **kwargs):
        from .services import apply_enforcement_action

        flag = self.context["flag"]
        reviewer = self.context["request"].user
        action = self.validated_data["action"]

        return apply_enforcement_action(
            flag, action,
            suspension_days=self.validated_data.get("suspension_days", 7),
            reviewed_by_human=True,
            reviewer=reviewer,
        )


class UserViolationHistorySerializer(serializers.ModelSerializer):
    username = serializers.CharField(source="user.username", read_only=True)

    class Meta:
        model = UserViolationHistory
        fields = ["id", "user", "username", "warning_count", "suspension_count", "last_violation_at", "is_banned"]
        read_only_fields = fields


class ModerationQueueItemSerializer(serializers.ModelSerializer):
    class Meta:
        model = ModerationQueueItem
        fields = [
            "id",
            "item_type",
            "related_object_id",
            "priority",
            "status",
            "assigned_to",
            "resolution_notes",
            "resolved_at",
            "created_at",
        ]
        read_only_fields = ["id", "item_type", "related_object_id", "created_at"]


class ModerationQueueResolveSerializer(serializers.Serializer):
    STATUS_CHOICES = ["in_progress", "resolved", "dismissed"]
    status = serializers.ChoiceField(choices=STATUS_CHOICES)
    resolution_notes = serializers.CharField(required=False, allow_blank=True)

    def save(self, **kwargs):
        from django.utils import timezone

        item = self.context["item"]
        item.status = self.validated_data["status"]
        item.resolution_notes = self.validated_data.get("resolution_notes", item.resolution_notes)
        if item.status in {"resolved", "dismissed"}:
            item.resolved_at = timezone.now()
        item.save(update_fields=["status", "resolution_notes", "resolved_at", "updated_at"])
        return item
