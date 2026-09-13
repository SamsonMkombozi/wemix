from rest_framework import serializers

from .models import AuditLog, Notification, SupportTicket, SupportTicketMessage, TermsAcceptance


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


class TermsAcceptanceSerializer(serializers.ModelSerializer):
    class Meta:
        model = TermsAcceptance
        fields = ["id", "version", "created_at"]
        read_only_fields = fields

    def create(self, validated_data):
        request = self.context["request"]
        return TermsAcceptance.objects.create(
            user=request.user, version=validated_data["version"], ip_address=request.META.get("REMOTE_ADDR"),
        )


class SupportTicketMessageSerializer(serializers.ModelSerializer):
    author_username = serializers.CharField(source="author.username", read_only=True, default="")

    class Meta:
        model = SupportTicketMessage
        fields = ["id", "author_username", "message", "is_staff_reply", "created_at"]
        read_only_fields = fields


class SupportTicketSerializer(serializers.ModelSerializer):
    user_username = serializers.CharField(source="user.username", read_only=True, default="")
    messages = SupportTicketMessageSerializer(many=True, read_only=True)

    class Meta:
        model = SupportTicket
        fields = [
            "id", "user_username", "category", "subject", "message", "related_order_id",
            "status", "messages", "created_at",
        ]
        read_only_fields = ["id", "user_username", "status", "messages", "created_at"]

    def create(self, validated_data):
        return SupportTicket.objects.create(user=self.context["request"].user, **validated_data)


class SupportTicketReplySerializer(serializers.Serializer):
    message = serializers.CharField()
    # Staff can additionally move the ticket's status while replying, so a
    # moderator doesn't need two separate calls to reply-and-resolve.
    status = serializers.ChoiceField(choices=SupportTicket.Status.choices, required=False)

    def save(self, **kwargs):
        request = self.context["request"]
        ticket = self.context["ticket"]
        is_staff = request.user.role in {request.user.Role.MODERATOR, request.user.Role.ADMIN, request.user.Role.SUPER_ADMIN} or request.user.is_staff

        SupportTicketMessage.objects.create(
            ticket=ticket, author=request.user, message=self.validated_data["message"], is_staff_reply=is_staff,
        )
        new_status = self.validated_data.get("status")
        if new_status:
            ticket.status = new_status
        elif is_staff and ticket.status == SupportTicket.Status.OPEN:
            ticket.status = SupportTicket.Status.IN_PROGRESS
        ticket.save(update_fields=["status", "updated_at"])

        if is_staff:
            from .notifications import send_notification

            send_notification(
                user=ticket.user, notification_type=Notification.NotificationType.SUPPORT_TICKET_REPLY,
                title=f"New reply on your ticket: '{ticket.subject}'",
                message=self.validated_data["message"][:200],
                link_path="dashboard.html?tab=support",
            )
        return ticket
