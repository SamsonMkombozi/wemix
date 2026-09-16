import json

from django.shortcuts import get_object_or_404
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import csrf_exempt
from rest_framework import generics, permissions, status
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from core.permissions import IsModeratorOrAbove

from .models import Order, RefundRequest, Subscription
from .serializers import (
    CreateOrderSerializer,
    ModeratorOrderSerializer,
    OrderSerializer,
    RefundRequestSerializer,
    RefundReviewSerializer,
    SubscriptionSerializer,
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


class CreateSubscriptionView(APIView):
    """POST /api/payments/subscriptions/ {"seller": "<uuid>", "channel":
    "mobile_money", "msisdn": "..."} -- subscribe (or renew a lapsed/
    cancelled subscription to the same seller)."""

    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        from accounts.models import User

        from .subscription_services import create_subscription_and_initiate_payment

        seller_id = request.data.get("seller")
        seller = get_object_or_404(User, pk=seller_id)
        subscription, txn, payment_gateway_url = create_subscription_and_initiate_payment(
            subscriber=request.user, seller=seller,
            channel=request.data.get("channel", "mobile_money"), msisdn=request.data.get("msisdn", ""),
        )
        return Response(
            {"subscription": SubscriptionSerializer(subscription).data, "payment_gateway_url": payment_gateway_url},
            status=status.HTTP_201_CREATED,
        )


class MySubscriptionsView(generics.ListAPIView):
    """GET /api/payments/subscriptions/mine/ -- sellers I subscribe to."""

    serializer_class = SubscriptionSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        return Subscription.objects.filter(subscriber=self.request.user).select_related("seller")


class MySubscribersView(generics.ListAPIView):
    """GET /api/payments/subscribers/mine/ -- seller-facing: who
    subscribes to me."""

    serializer_class = SubscriptionSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        return Subscription.objects.filter(seller=self.request.user, status=Subscription.Status.ACTIVE).select_related("subscriber")


class SubscriptionCancelView(APIView):
    """POST /api/payments/subscriptions/<id>/cancel/ -- access continues
    until current_period_end; no partial refund (matches how most
    subscription products handle mid-period cancellation)."""

    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, pk=None):
        from django.utils import timezone

        subscription = get_object_or_404(Subscription, pk=pk, subscriber=request.user)
        if subscription.status != Subscription.Status.ACTIVE:
            return Response({"detail": "Only an active subscription can be cancelled."}, status=status.HTTP_400_BAD_REQUEST)
        subscription.status = Subscription.Status.CANCELLED
        subscription.cancelled_at = timezone.now()
        subscription.save(update_fields=["status", "cancelled_at", "updated_at"])
        return Response(SubscriptionSerializer(subscription).data)


class AllSubscriptionsListView(generics.ListAPIView):
    """GET /api/payments/subscriptions/all/ -- moderator/admin: every
    subscription platform-wide, for support/dispute resolution (e.g. a
    buyer disputes a charge, a seller asks why a subscriber's access
    lapsed)."""

    serializer_class = SubscriptionSerializer
    permission_classes = [IsModeratorOrAbove]

    def get_queryset(self):
        qs = Subscription.objects.select_related("subscriber", "seller").order_by("-created_at")
        status_param = self.request.query_params.get("status")
        if status_param:
            qs = qs.filter(status=status_param)
        return qs


class SelcomSubscriptionWebhookView(APIView):
    """POST /api/payments/webhooks/selcom-subscription/ -- separate from
    the listing-purchase webhook so this newer path can't regress that
    one."""

    permission_classes = [permissions.AllowAny]
    authentication_classes = []
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "payment_webhook"

    def post(self, request):
        from .subscription_services import handle_selcom_subscription_webhook

        try:
            payload = json.loads(request.body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return Response({"detail": "Invalid JSON body."}, status=status.HTTP_400_BAD_REQUEST)

        source_ip = request.META.get("REMOTE_ADDR")
        verdict, txn = handle_selcom_subscription_webhook(payload=payload, headers=dict(request.headers), source_ip=source_ip)

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


class FinancialReportView(APIView):
    """GET /api/payments/reports/?period=daily|weekly|monthly|yearly
    (&from=YYYY-MM-DD&to=YYYY-MM-DD) -- rolled-up gross sales, platform
    commission, and refunds per period bucket, for the moderation
    dashboard's Reports > Financials screen. No prior endpoint answered
    'how much revenue this week/month/year' -- only raw per-transaction
    lists existed."""

    permission_classes = [IsModeratorOrAbove]

    def get(self, request):
        from datetime import datetime

        from django.conf import settings
        from django.db.models import Count, Sum
        from django.db.models.functions import TruncDate, TruncMonth, TruncWeek, TruncYear

        trunc_fn = {"daily": TruncDate, "weekly": TruncWeek, "monthly": TruncMonth, "yearly": TruncYear}
        period = request.query_params.get("period", "daily")
        if period not in trunc_fn:
            return Response({"detail": f"period must be one of {list(trunc_fn)}."}, status=status.HTTP_400_BAD_REQUEST)

        qs = Order.objects.filter(status=Order.Status.PAID)
        date_from = request.query_params.get("from")
        date_to = request.query_params.get("to")
        if date_from:
            qs = qs.filter(created_at__date__gte=datetime.fromisoformat(date_from).date())
        if date_to:
            qs = qs.filter(created_at__date__lte=datetime.fromisoformat(date_to).date())

        rows = (
            qs.annotate(bucket=trunc_fn[period]("created_at"))
            .values("bucket")
            .annotate(gross_sales=Sum("amount"), order_count=Count("id"))
            .order_by("bucket")
        )
        commission_rate = float(settings.PLATFORM_COMMISSION_RATE)

        refund_rows = (
            RefundRequest.objects.filter(status=RefundRequest.Status.COMPLETED)
            .annotate(bucket=trunc_fn[period]("reviewed_at"))
            .values("bucket")
            .annotate(refunded_amount=Sum("amount"))
        )
        refund_by_bucket = {r["bucket"]: r["refunded_amount"] for r in refund_rows if r["bucket"]}

        results = [
            {
                "period": row["bucket"].isoformat() if hasattr(row["bucket"], "isoformat") else str(row["bucket"]),
                "gross_sales": row["gross_sales"],
                "platform_commission": round(float(row["gross_sales"]) * commission_rate, 2),
                "order_count": row["order_count"],
                "refunded_amount": refund_by_bucket.get(row["bucket"], 0),
            }
            for row in rows
        ]

        return Response({
            "period": period,
            "results": results,
            "totals": {
                "gross_sales": sum(r["gross_sales"] for r in results),
                "platform_commission": round(sum(r["platform_commission"] for r in results), 2),
                "order_count": sum(r["order_count"] for r in results),
                "refunded_amount": sum(float(r["refunded_amount"] or 0) for r in results),
            },
        })


class FinancialReportExportView(APIView):
    """GET /api/payments/reports/export/?period=... -- same data as
    FinancialReportView, as a downloadable CSV (no new dependency --
    Python's stdlib csv module, consistent with this project's
    no-build-step, minimal-dependency frontend/backend philosophy)."""

    permission_classes = [IsModeratorOrAbove]

    def get(self, request):
        import csv

        from django.http import HttpResponse

        report_view = FinancialReportView()
        report_view.request = request
        inner_response = report_view.get(request)
        if inner_response.status_code != 200:
            return inner_response
        data = inner_response.data

        response = HttpResponse(content_type="text/csv")
        response["Content-Disposition"] = f'attachment; filename="wemix-financial-report-{data["period"]}.csv"'
        writer = csv.writer(response)
        writer.writerow(["Period", "Gross Sales", "Platform Commission", "Order Count", "Refunded Amount"])
        for row in data["results"]:
            writer.writerow([row["period"], row["gross_sales"], row["platform_commission"], row["order_count"], row["refunded_amount"]])
        writer.writerow([])
        writer.writerow(["TOTAL", data["totals"]["gross_sales"], data["totals"]["platform_commission"], data["totals"]["order_count"], data["totals"]["refunded_amount"]])
        return response


class FinancialReportPDFExportView(APIView):
    """GET /api/payments/reports/export-pdf/?period=... -- same data as
    FinancialReportView, as a printable one-page PDF summary (totals +
    per-period table) for sharing with people who don't want a raw CSV."""

    permission_classes = [IsModeratorOrAbove]

    def get(self, request):
        from io import BytesIO

        from django.http import HttpResponse
        from reportlab.lib import colors
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.units import mm
        from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
        from reportlab.lib.styles import getSampleStyleSheet

        report_view = FinancialReportView()
        report_view.request = request
        inner_response = report_view.get(request)
        if inner_response.status_code != 200:
            return inner_response
        data = inner_response.data

        buffer = BytesIO()
        doc = SimpleDocTemplate(buffer, pagesize=A4, topMargin=20 * mm, bottomMargin=20 * mm)
        styles = getSampleStyleSheet()
        elements = [
            Paragraph("WEMIX -- Financial Report", styles["Title"]),
            Paragraph(f"Period: {data['period'].capitalize()} &middot; Generated by {request.user.username}", styles["Normal"]),
            Spacer(1, 10 * mm),
        ]

        totals = data["totals"]
        totals_table = Table(
            [
                ["Gross Sales", "Platform Commission", "Orders", "Refunded"],
                [
                    f"{totals['gross_sales']:,.2f}", f"{totals['platform_commission']:,.2f}",
                    str(totals["order_count"]), f"{totals['refunded_amount']:,.2f}",
                ],
            ],
            colWidths=[45 * mm] * 4,
        )
        totals_table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#0d1b3d")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTSIZE", (0, 0), (-1, -1), 9),
            ("ALIGN", (0, 0), (-1, -1), "CENTER"),
            ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#dddddd")),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ("TOPPADDING", (0, 0), (-1, -1), 6),
        ]))
        elements.append(totals_table)
        elements.append(Spacer(1, 10 * mm))

        rows = [["Period", "Gross Sales", "Commission", "Orders", "Refunded"]]
        for row in data["results"]:
            rows.append([
                row["period"], f"{row['gross_sales']:,.2f}", f"{row['platform_commission']:,.2f}",
                str(row["order_count"]), f"{row['refunded_amount']:,.2f}",
            ])
        detail_table = Table(rows, colWidths=[35 * mm, 32 * mm, 32 * mm, 22 * mm, 32 * mm], repeatRows=1)
        detail_table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#f0eee8")),
            ("FONTSIZE", (0, 0), (-1, -1), 8),
            ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#dddddd")),
            ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ("TOPPADDING", (0, 0), (-1, -1), 4),
        ]))
        elements.append(detail_table)

        doc.build(elements)
        pdf_bytes = buffer.getvalue()
        buffer.close()

        response = HttpResponse(pdf_bytes, content_type="application/pdf")
        response["Content-Disposition"] = f'attachment; filename="wemix-financial-report-{data["period"]}.pdf"'
        return response
