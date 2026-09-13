from django.shortcuts import get_object_or_404
from rest_framework import generics, permissions
from rest_framework.response import Response
from rest_framework.views import APIView

from core.models import AuditLog
from core.permissions import IsModeratorOrAbove

from .models import AntiCircumventionFlag, ModerationQueueItem, UserReport, UserViolationHistory
from .serializers import (
    AntiCircumventionFlagSerializer,
    FlagReviewSerializer,
    ModerationQueueItemSerializer,
    ModerationQueueResolveSerializer,
    UserReportSerializer,
    UserViolationHistorySerializer,
)


def log_moderation_action(*, actor, action, target_model, target_id, description="", request=None):
    ip = request.META.get("REMOTE_ADDR") if request else None
    ua = request.META.get("HTTP_USER_AGENT", "")[:512] if request else ""
    AuditLog.objects.create(
        actor=actor, action=action, target_model=target_model, target_id=str(target_id),
        description=description, ip_address=ip, user_agent=ua,
    )


class UserReportCreateView(generics.CreateAPIView):
    """POST /api/moderation/reports/ -- any authenticated user can report
    a listing or another user."""

    serializer_class = UserReportSerializer
    permission_classes = [permissions.IsAuthenticated]

    def perform_create(self, serializer):
        report = serializer.save()
        ModerationQueueItem.objects.create(
            item_type=ModerationQueueItem.ItemType.USER_REPORT,
            related_object_id=report.id,
            priority=ModerationQueueItem.Priority.NORMAL,
        )


class UserReportQueueView(generics.ListAPIView):
    """GET /api/moderation/reports/queue/ -- moderator view of all reports."""

    serializer_class = UserReportSerializer
    permission_classes = [IsModeratorOrAbove]
    queryset = UserReport.objects.select_related("reporter", "reported_user", "listing").order_by("-created_at")


class AntiCircumventionFlagQueueView(generics.ListAPIView):
    """GET /api/moderation/flags/ -- moderator queue of unreviewed
    anti-circumvention detections, most severe first."""

    serializer_class = AntiCircumventionFlagSerializer
    permission_classes = [IsModeratorOrAbove]

    def get_queryset(self):
        qs = AntiCircumventionFlag.objects.select_related("user", "listing")
        if self.request.query_params.get("unreviewed") == "1":
            qs = qs.filter(reviewed_by_human=False)
        severity_order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
        return sorted(qs, key=lambda f: (severity_order.get(f.severity, 9), f.created_at))[:200]


class AntiCircumventionFlagReviewView(APIView):
    """POST /api/moderation/flags/<id>/review/ -- moderator applies an
    enforcement action (warning / suspension / ban) to the flagged user."""

    permission_classes = [IsModeratorOrAbove]

    def post(self, request, pk=None):
        flag = get_object_or_404(AntiCircumventionFlag, pk=pk)
        serializer = FlagReviewSerializer(data=request.data, context={"flag": flag, "request": request})
        serializer.is_valid(raise_exception=True)
        serializer.save()
        log_moderation_action(
            actor=request.user, action=AuditLog.Action.FLAG, target_model="AntiCircumventionFlag",
            target_id=flag.id, description=f"Action '{serializer.validated_data['action']}' applied to user {flag.user_id}.",
            request=request,
        )
        return Response(AntiCircumventionFlagSerializer(flag).data)


class UserViolationHistoryView(generics.RetrieveAPIView):
    """GET /api/moderation/violations/<user_id>/ -- a user's violation record."""

    serializer_class = UserViolationHistorySerializer
    permission_classes = [IsModeratorOrAbove]
    lookup_field = "user_id"
    lookup_url_kwarg = "user_id"
    queryset = UserViolationHistory.objects.select_related("user")


class ModerationQueueListView(generics.ListAPIView):
    """GET /api/moderation/queue/ -- unified queue across report types.
    Filter with ?status=open and/or ?item_type=user_report."""

    serializer_class = ModerationQueueItemSerializer
    permission_classes = [IsModeratorOrAbove]

    def get_queryset(self):
        qs = ModerationQueueItem.objects.all().order_by("-priority", "-created_at")
        status_param = self.request.query_params.get("status")
        item_type = self.request.query_params.get("item_type")
        if status_param:
            qs = qs.filter(status=status_param)
        else:
            qs = qs.filter(status=ModerationQueueItem.Status.OPEN)
        if item_type:
            qs = qs.filter(item_type=item_type)
        return qs


class ModerationQueueResolveView(APIView):
    """POST /api/moderation/queue/<id>/resolve/ -- mark a queue item
    in_progress / resolved / dismissed."""

    permission_classes = [IsModeratorOrAbove]

    def post(self, request, pk=None):
        item = get_object_or_404(ModerationQueueItem, pk=pk)
        serializer = ModerationQueueResolveSerializer(data=request.data, context={"item": item})
        serializer.is_valid(raise_exception=True)
        serializer.save()
        log_moderation_action(
            actor=request.user, action=AuditLog.Action.UPDATE, target_model="ModerationQueueItem",
            target_id=item.id, description=f"Queue item marked {serializer.validated_data['status']}.",
            request=request,
        )
        return Response(ModerationQueueItemSerializer(item).data)


class ModerationQueueAssignView(APIView):
    """POST /api/moderation/queue/<id>/assign/ -- assign to self (or
    another moderator by passing {"assigned_to": "<user_id>"})."""

    permission_classes = [IsModeratorOrAbove]

    def post(self, request, pk=None):
        item = get_object_or_404(ModerationQueueItem, pk=pk)
        assigned_to_id = request.data.get("assigned_to", str(request.user.id))
        item.assigned_to_id = assigned_to_id
        item.status = ModerationQueueItem.Status.IN_PROGRESS
        item.save(update_fields=["assigned_to", "status", "updated_at"])
        return Response(ModerationQueueItemSerializer(item).data)
