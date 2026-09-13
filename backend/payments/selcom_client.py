"""
Selcom payment gateway client.

IMPORTANT: this implements Selcom's publicly documented HMAC signing
convention (Authorization: SELCOM <base64 api key>, plus a Digest header
that's an HMAC-SHA256 over "timestamp=...&field=value&..." for the signed
fields, keyed with the API secret). The exact endpoint paths, payload
field names, and response shape should be verified against Selcom's live
API documentation / sandbox before going to production -- this sandbox
environment has no network access to selcommobile.com to verify against,
so treat the endpoint paths below as the best-effort starting point they
are, not a confirmed-working integration.

Sandbox base URL: https://apigwtest.selcommobile.com
Production base URL: https://apigw.selcommobile.com (confirm exact host
with your Selcom account manager before switching SELCOM_API_BASE_URL).
"""

import base64
import hashlib
import hmac
import json
import logging
from datetime import datetime, timezone as dt_timezone
from decimal import Decimal

import requests
from django.conf import settings

logger = logging.getLogger("habari.selcom")


class SelcomAPIError(Exception):
    """Raised for any non-successful response from the Selcom API."""


class SelcomClient:
    def __init__(self):
        from core.credentials import get_credential

        self.base_url = get_credential("selcom", "base_url", settings.SELCOM_API_BASE_URL).rstrip("/")
        self.api_key = get_credential("selcom", "api_key", settings.SELCOM_API_KEY)
        self.api_secret = get_credential("selcom", "api_secret", settings.SELCOM_API_SECRET)
        self.vendor_id = get_credential("selcom", "vendor_id", settings.SELCOM_VENDOR_ID)

        if not (self.api_key and self.api_secret and self.vendor_id):
            logger.warning(
                "Selcom credentials are not fully configured. Payment calls will fail "
                "until these are set via the Settings tab or in .env."
            )

    # ------------------------------------------------------------------
    # Signing
    # ------------------------------------------------------------------
    def _timestamp(self) -> str:
        return datetime.now(dt_timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+0000")

    def _sign(self, fields: dict, timestamp: str) -> str:
        to_sign = f"timestamp={timestamp}" + "".join(f"&{k}={v}" for k, v in fields.items())
        digest = hmac.new(self.api_secret.encode("utf-8"), to_sign.encode("utf-8"), hashlib.sha256).digest()
        return base64.b64encode(digest).decode("ascii")

    def _headers(self, fields: dict) -> dict:
        timestamp = self._timestamp()
        return {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Authorization": f"SELCOM {base64.b64encode(self.api_key.encode()).decode()}",
            "Digest-Method": "HS256",
            "Digest": self._sign(fields, timestamp),
            "Timestamp": timestamp,
            "Signed-Fields": ",".join(fields.keys()),
        }

    # ------------------------------------------------------------------
    # API calls
    # ------------------------------------------------------------------
    def create_order_minimal(
        self,
        *,
        order_id: str,
        buyer_email: str,
        buyer_name: str,
        buyer_phone: str,
        amount: Decimal,
        currency: str = "TZS",
        webhook_url: str = "",
        redirect_url: str = "",
    ) -> dict:
        """Creates a checkout order and returns Selcom's response, which
        (per their documented minimal-checkout flow) includes a
        payment_gateway_url the buyer should be redirected to."""
        payload = {
            "vendor": self.vendor_id,
            "order_id": order_id,
            "buyer_email": buyer_email,
            "buyer_name": buyer_name,
            "buyer_phone": buyer_phone,
            "amount": str(int(amount)),
            "currency": currency,
            "no_of_items": "1",
            "redirect_url": redirect_url,
            "cancel_url": redirect_url,
            "webhook": webhook_url,
        }
        headers = self._headers(payload)
        try:
            resp = requests.post(
                f"{self.base_url}/v1/checkout/create-order-minimal",
                data=json.dumps(payload),
                headers=headers,
                timeout=15,
            )
        except requests.RequestException as exc:
            raise SelcomAPIError(f"Network error calling Selcom: {exc}") from exc

        return self._parse_response(resp)

    def order_status(self, order_id: str) -> dict:
        payload = {"order_id": order_id}
        headers = self._headers(payload)
        try:
            resp = requests.get(
                f"{self.base_url}/v1/checkout/order-status",
                params=payload,
                headers=headers,
                timeout=15,
            )
        except requests.RequestException as exc:
            raise SelcomAPIError(f"Network error calling Selcom: {exc}") from exc

        return self._parse_response(resp)

    def _parse_response(self, resp) -> dict:
        try:
            data = resp.json()
        except ValueError:
            raise SelcomAPIError(f"Non-JSON response from Selcom (HTTP {resp.status_code}): {resp.text[:300]}")

        if resp.status_code >= 400:
            raise SelcomAPIError(f"Selcom returned HTTP {resp.status_code}: {data}")

        result_code = str(data.get("resultcode", "")).upper()
        # Selcom's docs describe "000"/"SUCCESS" style result codes for the
        # happy path; treat anything else as a soft failure the caller can
        # inspect via the raw response.
        if result_code not in {"", "000", "SUCCESS"}:
            raise SelcomAPIError(f"Selcom rejected the request: {data}")

        return data

    # ------------------------------------------------------------------
    # Webhook verification
    # ------------------------------------------------------------------
    def verify_webhook_signature(self, headers: dict, payload: dict) -> bool:
        """Recomputes the digest the same way _sign() does, over the
        fields Selcom says it signed (Signed-Fields header), and compares
        it to what they sent. `headers` should be case-insensitively
        accessible (e.g. a Django HttpRequest.headers object)."""
        signed_fields_header = headers.get("Signed-Fields", "")
        digest = headers.get("Digest", "")
        timestamp = headers.get("Timestamp", "")

        if not (signed_fields_header and digest and timestamp):
            return False

        field_names = [f.strip() for f in signed_fields_header.split(",") if f.strip()]
        fields = {name: payload.get(name, "") for name in field_names}
        expected = self._sign(fields, timestamp)
        return hmac.compare_digest(expected, digest)
