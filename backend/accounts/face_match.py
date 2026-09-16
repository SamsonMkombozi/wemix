"""
accounts/face_match.py — selfie-vs-ID face match + liveness, via an
external KYC vendor (Jumio/Onfido/Veriff/etc).

Honest scope: there is no trained face-recognition or liveness model in
this environment (no network access to install one, and even if there
were, a hand-rolled face-match without a properly trained/calibrated
model would be worse than not scoring it at all -- a wrong "verified"
signal is more dangerous than an honest gap, same reasoning
accounts/ocr.py already applies to skipping this). This is the same
pluggable-external-service seam news/ai_verification.py uses for AI text
verification: a genuine no-op (fields stay unset, exactly as before this
existed) until KYC_FACE_MATCH_API_URL is configured with a real vendor,
at which point this wires the response into
IdentityVerification.face_match_score / liveness_passed for a human
moderator to weigh alongside everything else in the review queue --
never auto-approves or auto-rejects on its own.
"""

import logging

import requests
from django.conf import settings

from .models import IdentityVerification

logger = logging.getLogger("habari.face_match")


def is_configured() -> bool:
    return bool(settings.KYC_FACE_MATCH_API_URL)


def run_face_match(identity_verification: IdentityVerification) -> None:
    """Populates face_match_score / liveness_passed if an external
    vendor is configured. Safe to call unconditionally -- no-ops quietly
    if unconfigured, and degrades gracefully (logs, leaves fields unset)
    on any request failure, never blocking or failing a KYC submission."""
    if not is_configured():
        logger.info(
            "Face match not run for verification %s (KYC_FACE_MATCH_API_URL not configured).",
            identity_verification.id,
        )
        return

    try:
        with identity_verification.front_image.open("rb") as front_f, \
                identity_verification.selfie_image.open("rb") as selfie_f:
            resp = requests.post(
                settings.KYC_FACE_MATCH_API_URL,
                headers={"Authorization": f"Bearer {settings.KYC_FACE_MATCH_API_KEY}"},
                files={"id_photo": front_f, "selfie": selfie_f},
                timeout=20,
            )
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        logger.warning("Face match request failed for verification %s: %s", identity_verification.id, exc)
        return

    update_fields = []
    if "face_match_score" in data:
        identity_verification.face_match_score = data["face_match_score"]
        update_fields.append("face_match_score")
    if "liveness_passed" in data:
        identity_verification.liveness_passed = data["liveness_passed"]
        update_fields.append("liveness_passed")
    if update_fields:
        identity_verification.save(update_fields=update_fields + ["updated_at"])
