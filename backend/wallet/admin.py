from django.contrib import admin

from .models import PayoutAccount, Wallet, WalletTransaction, WithdrawalRequest


@admin.register(Wallet)
class WalletAdmin(admin.ModelAdmin):
    list_display = ("id", "owner", "is_platform", "balance", "pending_balance", "is_frozen")
    list_filter = ("is_platform", "is_frozen")
    search_fields = ("owner__username",)


@admin.register(WalletTransaction)
class WalletTransactionAdmin(admin.ModelAdmin):
    list_display = ("wallet", "entry_type", "amount", "balance_after", "created_at")
    list_filter = ("entry_type",)
    readonly_fields = [f.name for f in WalletTransaction._meta.fields]


@admin.register(WithdrawalRequest)
class WithdrawalRequestAdmin(admin.ModelAdmin):
    list_display = ("wallet", "amount", "destination_type", "status", "created_at")
    list_filter = ("status", "destination_type")


@admin.register(PayoutAccount)
class PayoutAccountAdmin(admin.ModelAdmin):
    list_display = ("user", "account_type", "account_number", "status", "is_default", "created_at")
    list_filter = ("status", "account_type")
    search_fields = ("user__username", "account_number", "account_name")
