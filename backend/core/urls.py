from django.urls import path

from . import views

app_name = "core"

urlpatterns = [
    path("notifications/", views.NotificationListView.as_view(), name="notification-list"),
    path("notifications/unread-count/", views.NotificationUnreadCountView.as_view(), name="notification-unread-count"),
    path("notifications/<uuid:pk>/read/", views.NotificationMarkReadView.as_view(), name="notification-mark-read"),
    path("notifications/mark-all-read/", views.NotificationMarkAllReadView.as_view(), name="notification-mark-all-read"),
    path("system-status/", views.SystemStatusView.as_view(), name="system-status"),
    path("integration-credentials/<str:provider>/", views.IntegrationCredentialSaveView.as_view(), name="integration-credential-save"),
    path("integration-credentials/<str:provider>/test-connection/", views.IntegrationTestConnectionView.as_view(), name="integration-test-connection"),
    path("audit-log/", views.AuditLogListView.as_view(), name="audit-log"),
    path("api-keys/", views.APIKeyListCreateView.as_view(), name="api-key-list-create"),
    path("api-keys/<uuid:pk>/revoke/", views.APIKeyRevokeView.as_view(), name="api-key-revoke"),
    path("terms/status/", views.TermsStatusView.as_view(), name="terms-status"),
    path("terms/accept/", views.TermsAcceptView.as_view(), name="terms-accept"),
    path("support-tickets/", views.SupportTicketListCreateView.as_view(), name="support-ticket-list-create"),
    path("support-tickets/<uuid:pk>/reply/", views.SupportTicketReplyView.as_view(), name="support-ticket-reply"),
]
