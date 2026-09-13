"""
accounts/ocr.py — OCR pre-fill for National ID verification submissions.

Honest scope: this runs Tesseract OCR (via pytesseract) on the uploaded
ID front image and does best-effort regex extraction of a name, ID
number, and date of birth from the recognized text. It PRE-FILLS the
ocr_* fields on an IdentityVerification for a human moderator to review
faster -- it does NOT auto-approve identity. Verification status stays
exactly as it was (pending/awaiting_review); a moderator still makes the
approve/reject call, same as before this existed.

What this does NOT do: face matching or liveness detection between the
selfie and ID photo (face_match_score / liveness_passed stay unset).
That needs a face-recognition model this environment doesn't have --
left as an honest gap rather than faked with a placeholder number.

Requires the `tesseract-ocr` system package (not just the pytesseract
pip package) to actually run. If it's not installed, this degrades
gracefully: logs a warning and leaves the OCR fields blank, exactly as
before -- it never blocks or breaks a KYC submission.

    sudo apt install tesseract-ocr
"""

import logging
import re

logger = logging.getLogger("habari.ocr")

ID_NUMBER_RE = re.compile(r"\b\d{8,25}\b")
DOB_PATTERNS = [
    re.compile(r"\b(\d{1,2})[/\-.](\d{1,2})[/\-.](\d{4})\b"),  # DD/MM/YYYY or DD-MM-YYYY
    re.compile(r"\b(\d{4})[/\-.](\d{1,2})[/\-.](\d{1,2})\b"),  # YYYY-MM-DD
]
NAME_LABEL_RE = re.compile(
    r"(?:name|jina)\s*[:\-]?[ \t]*([A-Z][A-Za-z'\-]+(?:[ \t]+[A-Z][A-Za-z'\-]+){0,3})",
    re.IGNORECASE,
)


def _parse_date_of_birth(text: str):
    from datetime import date

    for pattern in DOB_PATTERNS:
        match = pattern.search(text)
        if not match:
            continue
        groups = [int(g) for g in match.groups()]
        # Figure out which group is the 4-digit year.
        if groups[0] > 31:  # YYYY-MM-DD form
            year, month, day = groups
        else:  # DD/MM/YYYY form
            day, month, year = groups
        try:
            return date(year, month, day)
        except ValueError:
            continue
    return None


def _extract_name(text: str):
    match = NAME_LABEL_RE.search(text)
    if match:
        return match.group(1).strip()

    # Fallback: the longest all-caps-ish line is often the printed name
    # on a national ID card layout.
    best_line = ""
    for line in text.splitlines():
        cleaned = line.strip()
        if len(cleaned) < 4 or len(cleaned) > 60:
            continue
        letters = [c for c in cleaned if c.isalpha()]
        if not letters:
            continue
        upper_ratio = sum(1 for c in letters if c.isupper()) / len(letters)
        if upper_ratio > 0.7 and len(cleaned) > len(best_line):
            best_line = cleaned
    return best_line or ""


def run_ocr_on_verification(identity_verification) -> bool:
    """Runs OCR on the front ID image and fills ocr_* fields on the
    IdentityVerification instance in place, then saves it. Returns True
    if OCR actually ran, False if it was skipped (e.g. tesseract not
    installed) -- callers can use this to decide whether to note the gap
    somewhere, but should never treat False as an error."""

    try:
        import pytesseract
        from PIL import Image
    except ImportError:
        logger.warning("pytesseract/Pillow not available; skipping OCR.")
        return False

    try:
        image = Image.open(identity_verification.front_image)
        image.load()
    except Exception as exc:
        logger.warning("Could not open front_image for OCR (verification %s): %s", identity_verification.id, exc)
        return False

    try:
        data = pytesseract.image_to_data(image, output_type=pytesseract.Output.DICT)
        text = pytesseract.image_to_string(image)
    except Exception as exc:
        # Most commonly: tesseract-ocr system binary isn't installed.
        # This must never break KYC submission -- OCR is a convenience
        # pre-fill, not a required step.
        logger.warning("OCR failed for verification %s (tesseract may not be installed): %s", identity_verification.id, exc)
        return False

    confidences = [int(c) for c in data.get("conf", []) if str(c).lstrip("-").isdigit() and int(c) >= 0]
    avg_confidence = round(sum(confidences) / len(confidences) / 100, 3) if confidences else None

    identity_verification.ocr_full_name = _extract_name(text)[:255]
    id_match = ID_NUMBER_RE.search(text.replace(" ", ""))
    identity_verification.ocr_id_number = id_match.group(0)[:64] if id_match else ""
    identity_verification.ocr_date_of_birth = _parse_date_of_birth(text)
    identity_verification.ocr_raw_payload = {"raw_text": text[:2000]}
    identity_verification.ocr_confidence = avg_confidence
    identity_verification.status = identity_verification.Status.AWAITING_REVIEW
    identity_verification.save(update_fields=[
        "ocr_full_name", "ocr_id_number", "ocr_date_of_birth", "ocr_raw_payload",
        "ocr_confidence", "status", "updated_at",
    ])
    return True
