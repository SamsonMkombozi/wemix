"""
URL configuration for habari_platform project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/5.1/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""
import re

from django.conf import settings
from django.contrib import admin
from django.http import JsonResponse
from django.urls import include, path, re_path
from django.views.static import serve as serve_static

from news.views import MarketplaceRssFeedView, MarketplaceSitemapView


def api_root(request):
    return JsonResponse(
        {
            "service": "Habari Platform API",
            "status": "ok",
            "endpoints": {
                "admin": "/admin/",
                "accounts": "/api/accounts/",
                "news": "/api/news/",
                "payments": "/api/payments/",
                "wallet": "/api/wallet/",
                "moderation": "/api/moderation/",
                "notifications": "/api/notifications/",
            },
        }
    )


urlpatterns = [
    path("", api_root, name="api-root"),
    path("sitemap.xml", MarketplaceSitemapView.as_view(), name="sitemap"),
    path("feed.xml", MarketplaceRssFeedView.as_view(), name="rss-feed"),
    path("admin/", admin.site.urls),
    path("api/accounts/", include("accounts.urls")),
    path("api/news/", include("news.urls")),
    path("api/payments/", include("payments.urls")),
    path("api/wallet/", include("wallet.urls")),
    path("api/moderation/", include("moderation.urls")),
    path("api/", include("core.urls")),
]

# Wired directly to django.views.static.serve rather than the usual
# django.conf.urls.static.static() helper: that helper is hardcoded to
# no-op whenever DEBUG is False (`elif not settings.DEBUG or ...: return []`),
# regardless of any condition it's called under, specifically to stop people
# from doing exactly this by accident. It's a deliberate override here, not
# an oversight: this deployment has no separate media server, CDN, or object
# storage in front of Django -- unlike STATIC_URL (served by WhiteNoise, see
# settings.py), nothing else serves user-uploaded media (avatars, listing
# images, KYC documents) in production. Django's docs call this view
# inefficient at scale, a real tradeoff to revisit if traffic grows, but the
# alternative today is every media URL 404ing.
urlpatterns += [
    re_path(r"^%s(?P<path>.*)$" % re.escape(settings.MEDIA_URL.lstrip("/")),
            serve_static, {"document_root": settings.MEDIA_ROOT}),
]
