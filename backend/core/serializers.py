from rest_framework import serializers

from .models import AuditLog, Notification


class AuditLogSerializer(serializers.ModelSerializer):
    actor_username = serializers.CharField(source="actor.username", read_only=True, default="")

    class Meta:
        model = AuditLog
        fields = ["id", "action", "actor_username", "target_model", "target_id", "description", "created_at"]
        read_only_fields = fields


class NotificationSerializer(serializers.ModelSerializer):
    class Meta:
        model = Notification
        fields = [
            "id",
            "notification_type",
            "title",
            "message",
            "link_path",
            "is_read",
            "created_at",
        ]
        read_only_fields = fields
