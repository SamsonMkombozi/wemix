from django.contrib import admin

from .models import NalaTransaction, Order, PaymentWebhookLog, RefundRequest, SelcomTransaction


@admin.register(Order)
class OrderAdmin(admin.ModelAdmin):
    list_display = ("id", "buyer", "listing", "amount", "status", "payment_provider", "created_at")
    list_filter = ("status", "payment_provider")
    search_fields = ("buyer__username", "listing__title")


@admin.register(SelcomTransaction)
class SelcomTransactionAdmin(admin.ModelAdmin):
    list_display = ("selcom_order_id", "order", "channel", "amount", "status", "created_at")
    list_filter = ("status", "channel")
    search_fields = ("selcom_order_id", "reference", "selcom_transaction_id")


@admin.register(NalaTransaction)
class NalaTransactionAdmin(admin.ModelAdmin):
    list_display = ("nala_collection_id", "order", "channel", "amount", "currency", "status", "created_at")
    list_filter = ("status", "channel")
    search_fields = ("nala_collection_id", "reference", "nala_transaction_id")


admin.site.register(PaymentWebhookLog)
admin.site.register(RefundRequest)
