from django.urls import include, path
from rest_framework.routers import DefaultRouter

from . import views

app_name = "news"

router = DefaultRouter()
router.register("listings", views.NewsListingViewSet, basename="listing")

urlpatterns = [
    path("categories/", views.CategoryListCreateView.as_view(), name="categories"),
    path("tags/", views.TagListView.as_view(), name="tags"),
    path("bookmarks/", views.BookmarkListView.as_view(), name="bookmarks"),
    path("saved-searches/", views.SavedSearchListCreateView.as_view(), name="saved-search-list-create"),
    path("saved-searches/<uuid:pk>/", views.SavedSearchDetailView.as_view(), name="saved-search-detail"),
    path("following/", views.FollowingListView.as_view(), name="following"),
    path("users/<uuid:user_id>/follow/", views.FollowToggleView.as_view(), name="follow-toggle"),
    path("media-download/<str:token>/", views.MediaDownloadByTokenView.as_view(), name="media-download-by-token"),
    path("", include(router.urls)),
]
