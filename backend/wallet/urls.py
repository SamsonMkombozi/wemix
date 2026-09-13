from django.urls import path

from . import views

app_name = "wallet"

urlpatterns = [
    path("me/", views.MyWalletView.as_view(), name="my-wallet"),
    path("platform/", views.PlatformWalletView.as_view(), name="platform-wallet"),
    path("withdrawals/", views.WithdrawalRequestListCreateView.as_view(), name="withdrawals"),
    path("withdrawals/queue/", views.WithdrawalRequestQueueView.as_view(), name="withdrawals-queue"),
    path("withdrawals/<uuid:pk>/review/", views.WithdrawalRequestReviewView.as_view(), name="withdrawals-review"),
    path("payout-accounts/", views.PayoutAccountListCreateView.as_view(), name="payout-accounts"),
    path("payout-accounts/queue/", views.PayoutAccountQueueView.as_view(), name="payout-accounts-queue"),
    path("payout-accounts/<uuid:pk>/", views.PayoutAccountDetailView.as_view(), name="payout-account-detail"),
    path("payout-accounts/<uuid:pk>/set-default/", views.PayoutAccountSetDefaultView.as_view(), name="payout-account-set-default"),
    path("payout-accounts/<uuid:pk>/review/", views.PayoutAccountReviewView.as_view(), name="payout-account-review"),
]
