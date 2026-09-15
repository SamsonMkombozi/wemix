from rest_framework import serializers

from news.models import NewsListing

from .models import Order, RefundRequest, SelcomTransaction, Subscription


class SelcomTransactionSerializer(serializers.ModelSerializer):
    class Meta:
        model = SelcomTransaction
        fields = [
            "id",
            "selcom_order_id",
            "reference",
            "channel",
            "amount",
            "currency",
            "status",
            "failure_reason",
            "initiated_at",
            "completed_at",
        ]


class ModeratorOrderSerializer(serializers.ModelSerializer):
    """Fuller order view for the moderation dashboard's Transactions tab
    -- includes buyer/seller identity that the buyer-facing
    OrderSerializer doesn't need."""

    listing_title = serializers.CharField(source="listing.title", read_only=True)
    listing_slug = serializers.CharField(source="listing.slug", read_only=True)
    buyer_username = serializers.CharField(source="buyer.username", read_only=True)
    seller_username = serializers.CharField(source="listing.seller.username", read_only=True)

    class Meta:
        model = Order
        fields = [
            "id", "listing", "listing_title", "listing_slug", "buyer_username", "seller_username",
            "amount", "currency", "status", "payment_provider", "access_granted_at", "created_at",
        ]
        read_only_fields = fields


class OrderSerializer(serializers.ModelSerializer):
    listing_title = serializers.CharField(source="listing.title", read_only=True)
    listing_slug = serializers.CharField(source="listing.slug", read_only=True)
    transactions = SelcomTransactionSerializer(many=True, read_only=True)
    has_review = serializers.SerializerMethodField()

    class Meta:
        model = Order
        fields = [
            "id",
            "listing",
            "listing_title",
            "listing_slug",
            "amount",
            "currency",
            "status",
            "payment_provider",
            "access_granted_at",
            "transactions",
            "has_review",
            "created_at",
        ]
        read_only_fields = fields

    def get_has_review(self, obj):
        return hasattr(obj, "review")


class CreateOrderSerializer(serializers.Serializer):
    listing_slug = serializers.SlugField()
    payment_provider = serializers.ChoiceField(choices=Order.PaymentProvider.choices, default=Order.PaymentProvider.SELCOM)
    channel = serializers.ChoiceField(choices=SelcomTransaction.Channel.choices, required=False)
    msisdn = serializers.CharField(required=False, allow_blank=True, max_length=20)
    currency = serializers.CharField(required=False, default="USD", max_length=3)

    def validate_listing_slug(self, value):
        try:
            listing = NewsListing.objects.get(slug=value)
        except NewsListing.DoesNotExist:
            raise serializers.ValidationError("Listing not found.")
        self.context["listing"] = listing
        return value

    def validate(self, attrs):
        provider = attrs.get("payment_provider", Order.PaymentProvider.SELCOM)
        if provider == Order.PaymentProvider.SELCOM:
            channel = attrs.get("channel")
            msisdn = attrs.get("msisdn", "")
            if not channel:
                raise serializers.ValidationError({"channel": "Required for Selcom payments."})
            if channel == SelcomTransaction.Channel.MOBILE_MONEY and not msisdn:
                raise serializers.ValidationError({"msisdn": "Phone number is required for mobile money payments."})
        return attrs


class RefundRequestSerializer(serializers.ModelSerializer):
    amount = serializers.DecimalField(max_digits=12, decimal_places=2, required=False)

    class Meta:
        model = RefundRequest
        fields = ["id", "order", "reason", "amount", "status", "created_at"]
        read_only_fields = ["id", "status", "created_at"]

    def validate_order(self, order):
        request = self.context["request"]
        if order.buyer_id != request.user.id:
            raise serializers.ValidationError("You can only request a refund for your own orders.")
        if order.status != Order.Status.PAID:
            raise serializers.ValidationError("Only paid orders can be refunded.")
        return order

    def create(self, validated_data):
        validated_data["requested_by"] = self.context["request"].user
        validated_data.setdefault("amount", validated_data["order"].amount)
        return RefundRequest.objects.create(**validated_data)


class RefundReviewSerializer(serializers.Serializer):
    STATUS_CHOICES = ["approved", "rejected", "completed", "failed"]
    status = serializers.ChoiceField(choices=STATUS_CHOICES)
    selcom_refund_reference = serializers.CharField(required=False, allow_blank=True, max_length=100)

    def save(self, **kwargs):
        from django.utils import timezone

        from .services import process_refund

        refund = self.context["refund"]
        reviewer = self.context["request"].user
        new_status = self.validated_data["status"]
        previous_status = refund.status

        refund.status = new_status
        refund.reviewed_by = reviewer
        refund.reviewed_at = timezone.now()
        if self.validated_data.get("selcom_refund_reference"):
            refund.selcom_refund_reference = self.validated_data["selcom_refund_reference"]
        refund.save(update_fields=["status", "reviewed_by", "reviewed_at", "selcom_refund_reference", "updated_at"])

        if new_status == "completed":
            process_refund(refund)

            if previous_status != "completed":
                from core.models import Notification
                from core.notifications import send_notification

                send_notification(
                    user=refund.order.buyer, notification_type=Notification.NotificationType.REFUND_PROCESSED,
                    title="Your refund has been processed",
                    message=f"{refund.amount} {refund.order.currency} has been refunded for '{refund.order.listing.title}'.",
                    link_path="dashboard.html?tab=purchases",
                )
                send_notification(
                    user=refund.order.listing.seller, notification_type=Notification.NotificationType.REFUND_PROCESSED,
                    title=f"A refund was processed for '{refund.order.listing.title}'",
                    message=f"{refund.amount} {refund.order.currency} was deducted from your wallet due to a buyer refund.",
                    link_path="dashboard.html?tab=withdrawals",
                )

        return refund


class SubscriptionSerializer(serializers.ModelSerializer):
    seller_username = serializers.CharField(source="seller.username", read_only=True)
    subscriber_username = serializers.CharField(source="subscriber.username", read_only=True)
    is_currently_active = serializers.SerializerMethodField()

    class Meta:
        model = Subscription
        fields = [
            "id", "seller", "seller_username", "subscriber_username", "price", "currency",
            "status", "current_period_end", "cancelled_at", "is_currently_active", "created_at",
        ]
        read_only_fields = fields

    def get_is_currently_active(self, obj):
        return obj.is_currently_active()
