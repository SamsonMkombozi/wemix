from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin

from .models import IdentityVerification, LoginSession, TwoFactorRecoveryCode, User


@admin.register(User)
class UserAdmin(DjangoUserAdmin):
    list_display = ("username", "email", "role", "id_verification_status", "is_suspended", "is_banned", "is_active")
    list_filter = ("role", "id_verification_status", "is_suspended", "is_banned", "is_staff")
    search_fields = ("username", "email", "phone_number")
    fieldsets = DjangoUserAdmin.fieldsets + (
        (
            "WEMIX",
            {
                "fields": (
                    "role",
                    "phone_number",
                    "id_verification_status",
                    "is_email_verified",
                    "is_phone_verified",
                    "is_2fa_enabled",
                    "is_suspended",
                    "suspended_until",
                    "is_banned",
                    "trust_score",
                    "organization_name",
                )
            },
        ),
    )


@admin.register(IdentityVerification)
class IdentityVerificationAdmin(admin.ModelAdmin):
    list_display = ("user", "id_type", "status", "face_match_score", "created_at")
    list_filter = ("status", "id_type")
    search_fields = ("user__username", "user__email", "ocr_id_number")
    readonly_fields = ("ocr_raw_payload",)


admin.site.register(LoginSession)
admin.site.register(TwoFactorRecoveryCode)
