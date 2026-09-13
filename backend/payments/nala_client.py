"""
Nala/Rafiki payment client for international (non-TZS) purchases.

HONESTY NOTE -- READ BEFORE USING IN PRODUCTION:

Unlike selcom_client.py (which implements Selcom's publicly documented
signing convention -- verified against real docs, just not a live
sandbox), this client is built from ZERO confirmed technical
specification. NALA's B2B payments product ("Rafiki", at rafiki.com) is
real and does offer international collections into Africa/Asia, but
their actual API reference -- authentication scheme, endpoint paths,
request/response JSON shapes, webhook payload format -- is not publicly
documented. It appears to sit behind a business account signup, not an
open developer portal.

(Worth flagging: there is a completely unrelated open-source project
also called "Rafiki" at rafiki.dev, implementing the Interledger/Open
Payments protocol with GraphQL APIs. That is NOT this. Building against
rafiki.dev's API would integrate the wrong product entirely.)

What this file implements instead: a conventional REST + Bearer-token
pattern that's extremely common across fintech collection APIs
(Stripe-like), so the SHAPE of the integration (create a collection
request, poll or receive a webhook for status, verify the webhook
signature) is sound even though the specific field names, endpoint
paths, and signing details are placeholders. Every place a real detail
is guessed is marked "PLACEHOLDER" below.

Before this can process a real payment:
  1. Get a real NALA/Rafiki business account and their actual API reference.
  2. Update NALA_API_BASE_URL and every endpoint path below.
  3. Update the authentication method if it's not a simple Bearer token.
  4. Update the request/response field names to match their real schema.
  5. Update verify_webhook_signature() to match their real signing scheme
     (HMAC-SHA256 over the raw body is the most common convention, used
     here as the placeholder, but confirm this against their real docs).
  6. Test against their real sandbox before any real money moves through it.

Until then, this integration will not work against Nala's real API --
it exists so the rest of the system (Order routing, webhook handling,
wallet crediting) is fully built and tested NOW, and swapping in the
real request/response details later is a small, contained change to
this one file rather than a system-wide refactor.
"""

import hashlib
import hmac
import json
import logging
import uuid

import requests
from django.conf import settings

logger = logging.getLogger("habari.nala")


class NalaAPIError(Exception):
    """Raised for any non-successful response from the Nala/Rafiki API."""


class NalaClient:
    def __init__(self):
        from core.credentials import get_credential

        self.base_url = get_credential("nala", "base_url", settings.NALA_API_BASE_URL).rstrip("/")
        self.api_key = get_credential("nala", "api_key", settings.NALA_API_KEY)
        self.api_secret = get_credential("nala", "api_secret", settings.NALA_API_SECRET)
        self.webhook_secret = get_credential("nala", "webhook_secret", settings.NALA_WEBHOOK_SECRET)

    def _headers(self) -> dict:
        # PLACEHOLDER: simple Bearer token, the most common convention.
        # Confirm against real docs -- some providers use a custom scheme
        # (like Selcom's "Authorization: SELCOM <base64 key>") instead.
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    def create_collection(self, *, reference: str, amount, currency: str, description: str, webhook_url: str) -> dict:
        """PLACEHOLDER endpoint/payload shape. Creates a request for Nala
        to collect payment from an international buyer. Returns the raw
        parsed JSON response so the caller can pull out whatever the
        real field names turn out to be -- don't assume the field names
        used here (`id`, `status`, `payment_url`) are correct until
        confirmed against real docs."""
        url = f"{self.base_url}/v1/collections"  # PLACEHOLDER path
        payload = {
            "reference": reference,
            "amount": str(amount),
            "currency": currency,
            "description": description,
            "webhook_url": webhook_url,
        }
        logger.info("Nala create_collection (PLACEHOLDER integration): %s", json.dumps(payload))
        try:
            resp = requests.post(url, json=payload, headers=self._headers(), timeout=20)
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as exc:
            raise NalaAPIError(f"Nala create_collection failed: {exc}") from exc

    def get_collection_status(self, nala_collection_id: str) -> dict:
        """PLACEHOLDER endpoint. Polls the status of a collection request."""
        url = f"{self.base_url}/v1/collections/{nala_collection_id}"  # PLACEHOLDER path
        try:
            resp = requests.get(url, headers=self._headers(), timeout=20)
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as exc:
            raise NalaAPIError(f"Nala get_collection_status failed: {exc}") from exc

    def verify_webhook_signature(self, raw_body: bytes, signature_header: str) -> bool:
        """PLACEHOLDER signing scheme: HMAC-SHA256 over the raw request
        body, hex-encoded, sent in a signature header -- this is the most
        common convention (Stripe, many others use it) but has NOT been
        confirmed against Nala's real docs. Update this to match their
        actual scheme before relying on it -- an unverified webhook
        signature check is a real security gap, not just a functional one."""
        if not self.webhook_secret or not signature_header:
            return False
        computed = hmac.new(self.webhook_secret.encode(), raw_body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(computed, signature_header)

    @staticmethod
    def generate_reference() -> str:
        return f"HBN-{uuid.uuid4().hex[:16]}"
