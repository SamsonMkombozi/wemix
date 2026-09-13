from core.models import AuditLog


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
