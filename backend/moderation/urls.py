from django.urls import path

from . import views

app_name = "moderation"

urlpatterns = [
    path("reports/", views.UserReportCreateView.as_view(), name="report-create"),
    path("reports/queue/", views.UserReportQueueView.as_view(), name="report-queue"),
    path("flags/", views.AntiCircumventionFlagQueueView.as_view(), name="flag-queue"),
    path("flags/<uuid:pk>/review/", views.AntiCircumventionFlagReviewView.as_view(), name="flag-review"),
    path("violations/<uuid:user_id>/", views.UserViolationHistoryView.as_view(), name="violation-detail"),
    path("queue/", views.ModerationQueueListView.as_view(), name="queue-list"),
    path("queue/<uuid:pk>/resolve/", views.ModerationQueueResolveView.as_view(), name="queue-resolve"),
    path("queue/<uuid:pk>/assign/", views.ModerationQueueAssignView.as_view(), name="queue-assign"),
]
