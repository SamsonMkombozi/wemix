from django.urls import path

from . import views

app_name = "payments"

urlpatterns = [
    path("orders/", views.CreateOrderView.as_view(), name="create-order"),
    path("orders/mine/", views.MyOrdersView.as_view(), name="my-orders"),
    path("orders/all/", views.AllOrdersListView.as_view(), name="all-orders"),
    path("orders/<uuid:id>/", views.OrderDetailView.as_view(), name="order-detail"),
    path("orders/<uuid:id>/status/", views.OrderStatusPollView.as_view(), name="order-status"),
    path("webhooks/selcom/", views.SelcomWebhookView.as_view(), name="selcom-webhook"),
    path("webhooks/nala/", views.NalaWebhookView.as_view(), name="nala-webhook"),
    path("webhooks/selcom-subscription/", views.SelcomSubscriptionWebhookView.as_view(), name="selcom-subscription-webhook"),
    path("subscriptions/", views.CreateSubscriptionView.as_view(), name="subscription-create"),
    path("subscriptions/mine/", views.MySubscriptionsView.as_view(), name="my-subscriptions"),
    path("subscriptions/all/", views.AllSubscriptionsListView.as_view(), name="all-subscriptions"),
    path("subscribers/mine/", views.MySubscribersView.as_view(), name="my-subscribers"),
    path("subscriptions/<uuid:pk>/cancel/", views.SubscriptionCancelView.as_view(), name="subscription-cancel"),
    path("refunds/", views.RefundRequestCreateView.as_view(), name="refund-create"),
    path("refunds/queue/", views.RefundRequestQueueView.as_view(), name="refund-queue"),
    path("refunds/<uuid:pk>/review/", views.RefundRequestReviewView.as_view(), name="refund-review"),
    path("reports/", views.FinancialReportView.as_view(), name="financial-report"),
    path("reports/export/", views.FinancialReportExportView.as_view(), name="financial-report-export"),
    path("reports/export-pdf/", views.FinancialReportPDFExportView.as_view(), name="financial-report-export-pdf"),
]
