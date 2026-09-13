import json

from django.shortcuts import get_object_or_404
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import csrf_exempt
from rest_framework import generics, permissions, status
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from core.permissions import IsModeratorOrAbove

from .models import Order, RefundRequest
from .serializers import (
    CreateOrderSerializer,
    ModeratorOrderSerializer,
    OrderSerializer,
    RefundRequestSerializer,
    RefundReviewSerializer,
)
from .services import (
    create_nala_collection_and_initiate_payment,
    create_order_and_initiate_payment,
    handle_nala_webhook,
    handle_selcom_webhook,
)


class CreateOrderView(APIView):
    """POST /api/payments/orders/ -- start a purchase: creates an Order and
    a Selcom checkout session, returns the payment_gateway_url the buyer's
    client should redirect to (or use to trigger a mobile money USSD push,
    depending on channel)."""

    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        serializer = CreateOrderSerializer(data=request.data, context={})
        serializer.is_valid(raise_exception=True)
        listing = serializer.context["listing"]
        provider = serializer.validated_data["payment_provider"]

        if provider == Order.PaymentProvider.NALA:
            order, txn, payment_gateway_url = create_nala_collection_and_initiate_payment(
                buyer=request.user, listing=listing, currency=serializer.validated_data.get("currency", "USD"),
            )
            return Response(
                {
                    "order": OrderSerializer(order).data,
                    "payment_gateway_url": payment_gateway_url,
                    "nala_collection_id": txn.nala_collection_id,
                },
                status=status.HTTP_201_CREATED,
            )

        order, txn, payment_gateway_url = create_order_and_initiate_payment(
            buyer=request.user,
            listing=listing,
            channel=serializer.validated_data["channel"],
            msisdn=serializer.validated_data.get("msisdn", ""),
        )

        return Response(
            {
                "order": OrderSerializer(order).data,
                "payment_gateway_url": payment_gateway_url,
                "selcom_order_id": txn.selcom_order_id,
            },
            status=status.HTTP_201_CREATED,
        )


class MyOrdersView(generics.ListAPIView):
    """GET /api/payments/orders/mine/ -- the current user's purchase history."""

    serializer_class = OrderSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        return Order.objects.filter(buyer=self.request.user).select_related("listing").prefetch_related("transactions")


class OrderDetailView(generics.RetrieveAPIView):
    """GET /api/payments/orders/<id>/ -- buyer or staff only."""

    serializer_class = OrderSerializer
    permission_classes = [permissions.IsAuthenticated]
    lookup_field = "id"

    def get_queryset(self):
        user = self.request.user
        qs = Order.objects.select_related("listing").prefetch_related("transactions")
        if user.role in {user.Role.ADMIN, user.Role.SUPER_ADMIN} or user.is_staff:
            return qs
        return qs.filter(buyer=user)


class AllOrdersListView(generics.ListAPIView):
    """GET /api/payments/orders/all/ -- moderator/admin only. Every sale
    on the platform, for the moderation dashboard's Transactions tab.
    Filterable by ?status= and ?provider=, searchable by ?q= (buyer/
    seller username or listing title)."""

    serializer_class = ModeratorOrderSerializer
    permission_classes = [IsModeratorOrAbove]

    def get_queryset(self):
        qs = Order.objects.select_related("listing", "listing__seller", "buyer").order_by("-created_at")
        params = self.request.query_params
        status_filter = params.get("status")
        provider = params.get("provider")
        q = params.get("q")

        if status_filter:
            qs = qs.filter(status=status_filter)
        if provider:
            qs = qs.filter(payment_provider=provider)
        if q:
            from django.db.models import Q
            qs = qs.filter(
                Q(buyer__username__icontains=q) | Q(listing__seller__username__icontains=q)
                | Q(listing__title__icontains=q)
            )
        return qs


class OrderStatusPollView(APIView):
    """GET /api/payments/orders/<id>/status/ -- lightweight poll endpoint
    for a checkout page waiting on webhook confirmation."""

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, id=None):
        try:
            order = Order.objects.get(pk=id, buyer=request.user)
        except Order.DoesNotExist:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
        return Response({"status": order.status})


@method_decorator(csrf_exempt, name="dispatch")
class SelcomWebhookView(APIView):
    """POST /api/payments/webhooks/selcom/ -- called by Selcom's servers,
    not by our own frontend. No session/CSRF; authenticated purely by
    HMAC signature (+ optional IP allowlist)."""

    permission_classes = [permissions.AllowAny]
    authentication_classes = []
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "payment_webhook"

    def post(self, request):
        try:
            payload = json.loads(request.body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return Response({"detail": "Invalid JSON body."}, status=status.HTTP_400_BAD_REQUEST)

        source_ip = request.META.get("REMOTE_ADDR")
        verdict, txn = handle_selcom_webhook(payload=payload, headers=dict(request.headers), source_ip=source_ip)

        if verdict == "signature_invalid":
            return Response({"detail": "Invalid signature."}, status=status.HTTP_401_UNAUTHORIZED)
        if verdict == "unknown_order":
            # 200 here on purpose -- Selcom shouldn't keep retrying forever
            # for an order_id that will never exist on our side.
            return Response({"detail": "Unknown order; acknowledged."}, status=status.HTTP_200_OK)
        return Response({"detail": "Processed.", "verdict": verdict}, status=status.HTTP_200_OK)


class NalaWebhookView(APIView):
    """POST /api/payments/webhooks/nala/ -- called by Nala/Rafiki's
    servers. PLACEHOLDER signature header name (X-Nala-Signature) --
    update once real docs confirm the actual header Nala uses."""

    permission_classes = [permissions.AllowAny]
    authentication_classes = []
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "payment_webhook"

    def post(self, request):
        try:
            payload = json.loads(request.body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return Response({"detail": "Invalid JSON body."}, status=status.HTTP_400_BAD_REQUEST)

        source_ip = request.META.get("REMOTE_ADDR")
        signature_header = request.headers.get("X-Nala-Signature", "")  # PLACEHOLDER header name
        verdict, txn = handle_nala_webhook(
            payload=payload, raw_body=request.body, signature_header=signature_header, source_ip=source_ip,
        )

        if verdict == "signature_invalid":
            return Response({"detail": "Invalid signature."}, status=status.HTTP_401_UNAUTHORIZED)
        if verdict == "unknown_order":
            return Response({"detail": "Unknown order; acknowledged."}, status=status.HTTP_200_OK)
        return Response({"detail": "Processed.", "verdict": verdict}, status=status.HTTP_200_OK)


class RefundRequestCreateView(generics.CreateAPIView):
    serializer_class = RefundRequestSerializer
    permission_classes = [permissions.IsAuthenticated]


class RefundRequestQueueView(generics.ListAPIView):
    """GET /api/payments/refunds/queue/ -- moderator/admin view of pending refund requests."""

    serializer_class = RefundRequestSerializer
    permission_classes = [IsModeratorOrAbove]

    def get_queryset(self):
        return RefundRequest.objects.filter(status=RefundRequest.Status.REQUESTED).select_related("order")


class RefundRequestReviewView(APIView):
    """POST /api/payments/refunds/<id>/review/ -- moderator approves/
    rejects/completes a refund. Completing it actually reverses the
    seller + platform wallet ledger entries from the original sale."""

    permission_classes = [IsModeratorOrAbove]

    def post(self, request, pk=None):
        refund = get_object_or_404(RefundRequest, pk=pk)
        serializer = RefundReviewSerializer(data=request.data, context={"refund": refund, "request": request})
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(RefundRequestSerializer(refund).data)
