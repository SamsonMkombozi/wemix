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
    path("refunds/", views.RefundRequestCreateView.as_view(), name="refund-create"),
    path("refunds/queue/", views.RefundRequestQueueView.as_view(), name="refund-queue"),
    path("refunds/<uuid:pk>/review/", views.RefundRequestReviewView.as_view(), name="refund-review"),
]
