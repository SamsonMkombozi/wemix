from django.urls import path
from rest_framework_simplejwt.views import TokenRefreshView

from . import views

app_name = "accounts"

urlpatterns = [
    path("register/", views.RegisterView.as_view(), name="register"),
    path("login/", views.CustomTokenObtainPairView.as_view(), name="login"),
    path("login/refresh/", TokenRefreshView.as_view(), name="login-refresh"),
    path("verify-email/", views.VerifyEmailView.as_view(), name="verify-email"),
    path("verify-email/resend/", views.ResendVerificationEmailView.as_view(), name="verify-email-resend"),
    path("me/", views.MeView.as_view(), name="me"),
    path("me/export/", views.ExportMyDataView.as_view(), name="export-my-data"),
    path("me/change-password/", views.ChangePasswordView.as_view(), name="change-password"),
    path("me/deactivate/", views.DeactivateAccountView.as_view(), name="deactivate"),
    path("users/", views.UserListView.as_view(), name="user-list"),
    path("users/<uuid:pk>/", views.UserDetailView.as_view(), name="user-detail"),
    path("users/<uuid:user_id>/moderate/", views.UserModerateView.as_view(), name="user-moderate"),
    path("users/<uuid:user_id>/force-deactivate/", views.UserForceDeactivateView.as_view(), name="user-force-deactivate"),
    path("users/<uuid:user_id>/set-password/", views.UserSetPasswordView.as_view(), name="user-set-password"),
    path("users/<uuid:user_id>/reverse-status/", views.UserReverseStatusView.as_view(), name="user-reverse-status"),
    path("users/<uuid:user_id>/toggle-verified-badge/", views.ToggleVerifiedBadgeView.as_view(), name="toggle-verified-badge"),
    path("kyc/", views.IdentityVerificationCreateListView.as_view(), name="kyc-list-create"),
    path("kyc/queue/", views.IdentityVerificationReviewQueueView.as_view(), name="kyc-queue"),
    path("kyc/<uuid:pk>/review/", views.IdentityVerificationReviewDetailView.as_view(), name="kyc-review"),
    path("corporate-verifications/", views.CorporateVerificationCreateListView.as_view(), name="corporate-verification-list-create"),
    path("corporate-verifications/queue/", views.CorporateVerificationReviewQueueView.as_view(), name="corporate-verification-queue"),
    path("corporate-verifications/<uuid:pk>/review/", views.CorporateVerificationReviewDetailView.as_view(), name="corporate-verification-review"),
    path("2fa/enable/", views.Enable2FAView.as_view(), name="2fa-enable"),
    path("2fa/confirm/", views.Confirm2FAView.as_view(), name="2fa-confirm"),
    path("2fa/disable/", views.Disable2FAView.as_view(), name="2fa-disable"),
]
