from django.contrib import admin

from .models import AntiCircumventionFlag, ModerationQueueItem, UserReport, UserViolationHistory


@admin.register(AntiCircumventionFlag)
class AntiCircumventionFlagAdmin(admin.ModelAdmin):
    list_display = ("user", "detected_pattern", "severity", "action_taken", "created_at")
    list_filter = ("detected_pattern", "severity", "action_taken")
    search_fields = ("user__username",)


@admin.register(ModerationQueueItem)
class ModerationQueueItemAdmin(admin.ModelAdmin):
    list_display = ("item_type", "priority", "status", "assigned_to", "created_at")
    list_filter = ("item_type", "priority", "status")


admin.site.register(UserViolationHistory)
admin.site.register(UserReport)
