from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import generics, permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import AuditLog, Notification
from .permissions import IsAdminOrAbove, IsModeratorOrAbove
from .serializers import AuditLogSerializer, NotificationSerializer


class NotificationListView(generics.ListAPIView):
    """GET /api/notifications/ -- the current user's notifications, most
    recent first. Filter with ?unread=1 for just the unread ones."""

    serializer_class = NotificationSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        qs = Notification.objects.filter(user=self.request.user)
        if self.request.query_params.get("unread") == "1":
            qs = qs.filter(is_read=False)
        return qs


class NotificationUnreadCountView(APIView):
    """GET /api/notifications/unread-count/ -- lightweight endpoint for a
    nav-bar bell badge, so the frontend doesn't have to fetch and count
    the full list on every page load."""

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        count = Notification.objects.filter(user=request.user, is_read=False).count()
        return Response({"unread_count": count})


class NotificationMarkReadView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, pk=None):
        notification = get_object_or_404(Notification, pk=pk, user=request.user)
        if not notification.is_read:
            notification.is_read = True
            notification.read_at = timezone.now()
            notification.save(update_fields=["is_read", "read_at", "updated_at"])
        return Response(NotificationSerializer(notification).data)


class NotificationMarkAllReadView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        updated = Notification.objects.filter(user=request.user, is_read=False).update(
            is_read=True, read_at=timezone.now()
        )
        return Response({"marked_read": updated})


PROVIDER_FIELDS = {
    "selcom": ["api_key", "api_secret", "vendor_id", "base_url"],
    "nala": ["api_key", "api_secret", "webhook_secret", "base_url"],
    "ai_verification": ["api_url"],
    "email": ["host", "port", "username", "password", "use_tls", "from_email"],
}


class SystemStatusView(APIView):
    """GET /api/system-status/ -- moderator/admin: per-field status
    (set/unset, source, and a masked value for secrets) for every
    integration, for the Settings tab. Never returns a real secret
    value -- see core/credentials.py's masking."""

    permission_classes = [IsModeratorOrAbove]

    def get(self, request):
        from django.conf import settings

        from .credentials import list_provider_status

        return Response({
            "selcom": list_provider_status("selcom", PROVIDER_FIELDS["selcom"]),
            "nala": list_provider_status("nala", PROVIDER_FIELDS["nala"]),
            "ai_verification": list_provider_status("ai_verification", PROVIDER_FIELDS["ai_verification"]),
            "email": list_provider_status("email", PROVIDER_FIELDS["email"]),
            "debug_mode": settings.DEBUG,
            "nala_note": "Placeholder integration -- see payments/nala_client.py. Being 'configured' here "
                         "only means credentials are set, not that they've been verified against a real Nala API.",
        })


class IntegrationCredentialSaveView(APIView):
    """POST /api/integration-credentials/<provider>/  {field_name: value, ...}
    -- admin/super_admin only (more sensitive than general moderation,
    so gated tighter than the Users tab's moderator-level actions).
    Only touches fields actually present in the request body -- a
    partial update, so re-saving one field doesn't blank the others.
    An empty string for a field clears the DB override, reverting that
    field back to its .env/settings.py fallback."""

    permission_classes = [IsAdminOrAbove]

    def post(self, request, provider=None):
        from .credentials import set_credential
        from .models import AuditLog

        if provider not in PROVIDER_FIELDS:
            return Response({"detail": f"Unknown provider '{provider}'."}, status=status.HTTP_400_BAD_REQUEST)

        valid_fields = set(PROVIDER_FIELDS[provider])
        submitted = {k: v for k, v in request.data.items() if k in valid_fields}
        if not submitted:
            return Response({"detail": "No recognized fields in the request body."}, status=status.HTTP_400_BAD_REQUEST)

        changed_fields = []
        for field_name, value in submitted.items():
            set_credential(provider, field_name, str(value) if value is not None else "", user=request.user)
            changed_fields.append(field_name)

        AuditLog.objects.create(
            actor=request.user, action=AuditLog.Action.UPDATE, target_model="IntegrationCredential",
            target_id="", description=f"Updated {provider} credential fields: {changed_fields} (values not logged).",
        )

        from .credentials import list_provider_status
        return Response(list_provider_status(provider, PROVIDER_FIELDS[provider]))


class IntegrationTestConnectionView(APIView):
    """POST /api/integration-credentials/<provider>/test-connection/
    -- admin/super_admin only. Attempts a real connectivity check using
    the CURRENTLY EFFECTIVE credentials (DB override or .env fallback)
    and reports success/failure with a human-readable reason. This is a
    genuine network/connection attempt, not a mock -- against
    placeholder/unconfirmed integrations (Nala) or without real
    credentials, a failure here is expected and correct, not a bug."""

    permission_classes = [IsAdminOrAbove]

    def post(self, request, provider=None):
        if provider not in PROVIDER_FIELDS:
            return Response({"detail": f"Unknown provider '{provider}'."}, status=status.HTTP_400_BAD_REQUEST)

        method = getattr(self, f"_test_{provider}", None)
        if not method:
            return Response({"success": False, "message": "No connection test implemented for this provider."})
        return Response(method())

    def _test_selcom(self):
        from core.credentials import get_credential
        from payments.selcom_client import SelcomAPIError, SelcomClient

        api_key = get_credential("selcom", "api_key")
        api_secret = get_credential("selcom", "api_secret")
        vendor_id = get_credential("selcom", "vendor_id")
        if not (api_key and api_secret and vendor_id):
            return {"success": False, "message": "Not fully configured -- api_key, api_secret, and vendor_id are all required."}

        client = SelcomClient()
        try:
            # A minimal-order-status-style call is the lightest real request
            # most Selcom-style gateways support -- if their API differs,
            # this will fail with a clear reason rather than silently lying.
            client.order_status("connection-test-probe")
            return {"success": True, "message": "Selcom responded -- credentials appear valid."}
        except SelcomAPIError as exc:
            return {"success": False, "message": f"Selcom rejected the request: {exc}"}
        except Exception as exc:
            return {"success": False, "message": f"Could not reach Selcom: {exc}"}

    def _test_nala(self):
        from core.credentials import get_credential
        from payments.nala_client import NalaAPIError, NalaClient

        api_key = get_credential("nala", "api_key")
        api_secret = get_credential("nala", "api_secret")
        if not (api_key and api_secret):
            return {"success": False, "message": "Not fully configured -- api_key and api_secret are both required."}

        client = NalaClient()
        try:
            client.get_collection_status("connection-test-probe")
            return {"success": True, "message": "Nala/Rafiki responded -- but remember this integration is "
                                                  "built from unconfirmed placeholder API conventions (see "
                                                  "payments/nala_client.py). A response doesn't guarantee the "
                                                  "real request/response shape used elsewhere is correct."}
        except NalaAPIError as exc:
            return {"success": False, "message": f"Nala/Rafiki rejected the request (or the placeholder "
                                                    f"endpoint/auth scheme doesn't match their real API): {exc}"}
        except Exception as exc:
            return {"success": False, "message": f"Could not reach Nala/Rafiki: {exc}"}

    def _test_ai_verification(self):
        from core.credentials import get_credential

        api_url = get_credential("ai_verification", "api_url")
        if not api_url:
            return {"success": True, "message": "No external API configured -- using the built-in local heuristic engine, which is always available."}
        try:
            import requests
            resp = requests.get(api_url, timeout=10)
            return {"success": resp.status_code < 500, "message": f"Reached {api_url} -- HTTP {resp.status_code}."}
        except Exception as exc:
            return {"success": False, "message": f"Could not reach {api_url}: {exc}"}

    def _test_email(self):
        from core.credentials import get_credential, get_email_connection

        host = get_credential("email", "host")
        if not host:
            return {"success": True, "message": "No SMTP host configured -- using the console/default backend (emails print to the server log, not delivered)."}
        try:
            connection = get_email_connection()
            connection.open()
            connection.close()
            return {"success": True, "message": f"Connected to {host} and authenticated successfully."}
        except Exception as exc:
            return {"success": False, "message": f"Could not connect/authenticate to {host}: {exc}"}



class AuditLogListView(generics.ListAPIView):
    """GET /api/audit-log/ -- moderator/admin only. Recent
    security-relevant actions across the platform, for the moderation
    dashboard's Settings tab ("monitoring status")."""

    serializer_class = AuditLogSerializer
    permission_classes = [IsModeratorOrAbove]

    def get_queryset(self):
        return AuditLog.objects.select_related("actor").order_by("-created_at")[:200]
