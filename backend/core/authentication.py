from django.utils import timezone
from rest_framework.authentication import BaseAuthentication
from rest_framework.exceptions import AuthenticationFailed


class APIKeyAuthentication(BaseAuthentication):
    """
    Reads `Authorization: Api-Key <raw key>` and authenticates as that
    key's owning user -- the B2B/enterprise access path, for a script or
    newsroom system that shouldn't have to run the interactive JWT
    login+refresh flow. Added alongside (not instead of) JWT/session
    auth in REST_FRAMEWORK settings, so nothing about the normal browser
    flow changes.
    """

    keyword = "Api-Key"

    def authenticate(self, request):
        from .models import APIKey, _hash_api_key

        header = request.META.get("HTTP_AUTHORIZATION", "")
        if not header.startswith(f"{self.keyword} "):
            return None  # let other authentication classes try

        raw_key = header[len(self.keyword) + 1:].strip()
        if not raw_key:
            raise AuthenticationFailed("Empty API key.")

        try:
            api_key = APIKey.objects.select_related("user").get(key_hash=_hash_api_key(raw_key), is_active=True)
        except APIKey.DoesNotExist:
            raise AuthenticationFailed("Invalid or revoked API key.")

        user = api_key.user
        if user.is_banned or user.is_suspended or user.is_deleted:
            raise AuthenticationFailed("This account can no longer authenticate.")

        APIKey.objects.filter(pk=api_key.pk).update(last_used_at=timezone.now())
        return (user, api_key)

    def authenticate_header(self, request):
        return self.keyword
