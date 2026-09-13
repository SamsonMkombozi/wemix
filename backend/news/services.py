from core.models import AuditLog

MEDIA_DOWNLOAD_TOKEN_MAX_AGE = 300  # 5 minutes -- generous enough to click through, short enough not to be a durable public link
MEDIA_DOWNLOAD_SIGNING_SALT = "news.media.download"


def make_media_download_token(media_id) -> str:
    """Signs a short-lived, URL-safe token embedding a NewsMedia id.
    Verified by MediaDownloadByTokenView with max_age=MEDIA_DOWNLOAD_TOKEN_MAX_AGE
    -- the actual access check (did this user pay for the listing?) already
    happened before this token was issued, in NewsListingViewSet.download;
    the token itself is just proof that check passed recently, not a
    fresh authorization decision."""
    from django.core import signing

    return signing.dumps({"media_id": str(media_id)}, salt=MEDIA_DOWNLOAD_SIGNING_SALT)


def read_media_download_token(token: str) -> str:
    """Returns the media_id encoded in a valid, unexpired token.
    Raises django.core.signing.BadSignature / SignatureExpired on failure
    -- let the caller (the view) decide how to translate that into a
    response."""
    from django.core import signing

    payload = signing.loads(token, salt=MEDIA_DOWNLOAD_SIGNING_SALT, max_age=MEDIA_DOWNLOAD_TOKEN_MAX_AGE)
    return payload["media_id"]


def log_news_action(*, actor, action, listing, description="", request=None, metadata=None):
    ip = None
    ua = ""
    if request is not None:
        ip = request.META.get("REMOTE_ADDR")
        ua = request.META.get("HTTP_USER_AGENT", "")[:512]
    AuditLog.objects.create(
        actor=actor,
        action=action,
        target_model="NewsListing",
        target_id=str(listing.id),
        description=description,
        ip_address=ip,
        user_agent=ua,
        metadata=metadata or {},
    )
