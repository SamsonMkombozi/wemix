from django.contrib import admin

from .models import (
    AIVerificationResult,
    Bookmark,
    Category,
    Follow,
    ImageAnalysisResult,
    ListingView,
    NewsListing,
    NewsMedia,
    Review,
    Tag,
)


class NewsMediaInline(admin.TabularInline):
    model = NewsMedia
    extra = 0


class AIVerificationInline(admin.TabularInline):
    model = AIVerificationResult
    fk_name = "listing"
    extra = 0
    readonly_fields = ("created_at",)


@admin.register(NewsListing)
class NewsListingAdmin(admin.ModelAdmin):
    list_display = ("title", "seller", "news_type", "category", "price", "status", "verification_status", "ai_score")
    list_filter = ("status", "verification_status", "news_type", "category")
    search_fields = ("title", "description", "seller__username")
    inlines = [NewsMediaInline, AIVerificationInline]
    readonly_fields = ("slug", "view_count", "purchase_count")


@admin.register(Category)
class CategoryAdmin(admin.ModelAdmin):
    list_display = ("name", "slug")
    prepopulated_fields = {"slug": ("name",)}


admin.site.register(Tag)
admin.site.register(ImageAnalysisResult)
admin.site.register(Review)
admin.site.register(Bookmark)
admin.site.register(Follow)
admin.site.register(ListingView)
