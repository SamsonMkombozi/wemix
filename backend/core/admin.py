from django.contrib import admin

from .models import AuditLog, IntegrationCredential, Notification, PlatformSetting


@admin.register(AuditLog)
class AuditLogAdmin(admin.ModelAdmin):
    list_display = ("action", "actor", "target_model", "target_id", "created_at")
    list_filter = ("action",)
    search_fields = ("target_model", "target_id", "actor__username")
    readonly_fields = [f.name for f in AuditLog._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


admin.site.register(PlatformSetting)


@admin.register(IntegrationCredential)
class IntegrationCredentialAdmin(admin.ModelAdmin):
    # encrypted_value deliberately excluded from every list/detail view --
    # Django admin has no legitimate reason to display even the ciphertext.
    list_display = ("provider", "field_name", "is_secret", "updated_by", "updated_at")
    list_filter = ("provider", "is_secret")
    readonly_fields = ("provider", "field_name", "is_secret", "updated_by", "updated_at", "created_at")
    fields = ("provider", "field_name", "is_secret", "updated_by", "updated_at", "created_at")

    def has_add_permission(self, request):
        return False  # created only via the API's set_credential(), never directly


@admin.register(Notification)
class NotificationAdmin(admin.ModelAdmin):
    list_display = ("notification_type", "user", "title", "is_read", "email_sent", "created_at")
    list_filter = ("notification_type", "is_read", "email_sent")
    search_fields = ("user__username", "title")
