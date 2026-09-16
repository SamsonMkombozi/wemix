"""
core/sms_client.py — Africa's Talking Bulk SMS client.

Implements Africa's Talking's publicly documented messaging API: a
form-encoded POST to /messaging with the API key as a request header
and the account username in the body. Same confidence tier as
payments/selcom_client.py -- built from public documentation, not
verified against a live account from this sandbox (no network access to
africastalking.com to confirm the exact response shape). Run a real
sandbox message through it before relying on delivery in production.

Sandbox base URL: https://api.sandbox.africastalking.com/version1
Production base URL: https://api.africastalking.com/version1
"""

import logging

import requests
from django.conf import settings

logger = logging.getLogger("habari.sms")


def is_configured() -> bool:
    return bool(settings.AFRICASTALKING_API_KEY and settings.AFRICASTALKING_USERNAME)


def send_sms(to: str, message: str) -> bool:
    """Sends one SMS. Returns True only on a confirmed-accepted send,
    False for absolutely everything else (not configured, network
    failure, provider-reported failure) -- callers should treat this
    exactly like email sending: best-effort, never raises, never blocks
    the action that triggered it."""
    if not is_configured():
        logger.info("SMS not sent (Africa's Talking not configured) -- would have sent to %s: %s", to, message[:80])
        return False

    url = f"{settings.AFRICASTALKING_BASE_URL}/messaging"
    headers = {"apiKey": settings.AFRICASTALKING_API_KEY, "Accept": "application/json"}
    payload = {"username": settings.AFRICASTALKING_USERNAME, "to": to, "message": message}
    if settings.AFRICASTALKING_SENDER_ID:
        payload["from"] = settings.AFRICASTALKING_SENDER_ID

    try:
        resp = requests.post(url, headers=headers, data=payload, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        recipients = data.get("SMSMessageData", {}).get("Recipients", [])
        success = any(r.get("status") == "Success" for r in recipients)
        if not success:
            logger.warning("Africa's Talking accepted the request but reported no successful recipient: %s", data)
        return success
    except Exception as exc:
        logger.warning("SMS send to %s failed: %s", to, exc)
        return False
