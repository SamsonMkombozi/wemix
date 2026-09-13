"""
moderation/circumvention_scanner.py — the automatic detection half of
the anti-circumvention system (the enforcement half already existed:
FlagReviewSerializer / apply_enforcement_action).

scan_listing(listing) reads the listing's title/description/body and:
  - detects phone numbers, email addresses, WhatsApp/Telegram handles or
    links, and other social media handles via regex (high confidence --
    these are unambiguous once matched)
  - detects off-platform-payment language via a phrase list (lower
    confidence -- natural language is fuzzier, more false-positive risk)
  - creates an AntiCircumventionFlag per detection (idempotent: won't
    duplicate the same match on the same listing across repeated scans)
  - for HIGH-confidence contact-info matches only, automatically applies
    graduated enforcement (warning -> temporary suspension -> permanent
    ban) using the same logic a human moderator's decision would use.
    Off-platform-language matches are created but left for a human to
    review (action_taken stays "none") since that detection is fuzzier.

Matched text is stored partially masked (e.g. "+2557*******") rather
than in full, consistent with how this project already redacts contact
info elsewhere (see the seed data and model docstring).
"""

import re

from .models import AntiCircumventionFlag
from .services import apply_enforcement_action, next_graduated_action

# ---------------------------------------------------------------------------
# High-confidence, unambiguous patterns (regex)
# ---------------------------------------------------------------------------

PHONE_RE = re.compile(r"(\+?255|0)[\s\-]?7\d{2}[\s\-]?\d{3}[\s\-]?\d{3}")
EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")
WHATSAPP_RE = re.compile(r"(wa\.me/\+?\d+|whatsapp[:\s]*\+?\d{7,}|\bwhats\s*app\s*me\b)", re.IGNORECASE)
TELEGRAM_RE = re.compile(r"(t\.me/[A-Za-z0-9_]+|telegram[:\s]*@?[A-Za-z0-9_]{4,}|\btelegram\s*me\b)", re.IGNORECASE)
SOCIAL_HANDLE_RE = re.compile(
    r"(instagram\.com/[A-Za-z0-9_.]+|facebook\.com/[A-Za-z0-9_.]+|twitter\.com/[A-Za-z0-9_]+|"
    r"x\.com/[A-Za-z0-9_]+|\B@[A-Za-z][A-Za-z0-9_]{3,})"
)

# ---------------------------------------------------------------------------
# Lower-confidence, fuzzy off-platform-payment language
# ---------------------------------------------------------------------------

OFF_PLATFORM_PHRASES = [
    "pay me directly", "pay directly", "skip the fees", "skip the commission", "avoid the fees",
    "avoid commission", "outside the platform", "off platform", "off the app", "cash only",
    "send money directly", "contact me outside", "call me instead", "message me instead",
    "cheaper if you pay direct", "no commission if", "deal directly", "let's deal directly",
]


def _mask(text: str) -> str:
    """Keeps a small readable prefix, masks the rest -- enough for a
    moderator to recognize the pattern without the full raw contact
    detail sitting in a matched_text column."""
    text = text.strip()
    if len(text) <= 5:
        return text[:2] + "*" * max(0, len(text) - 2)
    return text[:5] + "*" * min(10, len(text) - 5) + ("..." if len(text) > 15 else "")


def scan_text(text: str) -> list:
    """Returns a list of (detected_pattern, matched_text, confidence) for
    every match found in `text`. Confidence is 0-100."""
    findings = []

    for match in PHONE_RE.finditer(text):
        findings.append((AntiCircumventionFlag.DetectedPattern.PHONE_NUMBER, match.group(0), 92.0))

    for match in EMAIL_RE.finditer(text):
        findings.append((AntiCircumventionFlag.DetectedPattern.EMAIL, match.group(0), 90.0))

    for match in WHATSAPP_RE.finditer(text):
        findings.append((AntiCircumventionFlag.DetectedPattern.WHATSAPP, match.group(0), 88.0))

    for match in TELEGRAM_RE.finditer(text):
        findings.append((AntiCircumventionFlag.DetectedPattern.TELEGRAM, match.group(0), 85.0))

    for match in SOCIAL_HANDLE_RE.finditer(text):
        findings.append((AntiCircumventionFlag.DetectedPattern.SOCIAL_HANDLE, match.group(0), 65.0))

    lowered = text.lower()
    for phrase in OFF_PLATFORM_PHRASES:
        if phrase in lowered:
            findings.append((AntiCircumventionFlag.DetectedPattern.OFF_PLATFORM_LANGUAGE, phrase, 55.0))

    return findings


HIGH_CONFIDENCE_PATTERNS = {
    AntiCircumventionFlag.DetectedPattern.PHONE_NUMBER,
    AntiCircumventionFlag.DetectedPattern.EMAIL,
    AntiCircumventionFlag.DetectedPattern.WHATSAPP,
    AntiCircumventionFlag.DetectedPattern.TELEGRAM,
}


def _severity_for(pattern: str, confidence: float) -> str:
    if pattern in HIGH_CONFIDENCE_PATTERNS:
        return AntiCircumventionFlag.Severity.HIGH if confidence >= 90 else AntiCircumventionFlag.Severity.MEDIUM
    if pattern == AntiCircumventionFlag.DetectedPattern.SOCIAL_HANDLE:
        return AntiCircumventionFlag.Severity.LOW
    return AntiCircumventionFlag.Severity.MEDIUM


def scan_listing(listing, target_type=None) -> list:
    """Scans a NewsListing's description + body for circumvention
    attempts, creates AntiCircumventionFlag rows, and auto-applies
    graduated enforcement for high-confidence contact-info matches.
    Returns the list of AntiCircumventionFlag rows created (empty list
    if nothing new was found)."""
    from .models import ContentScanTarget

    combined_text = f"{listing.description}\n{listing.body}"
    findings = scan_text(combined_text)

    created_flags = []
    for pattern, matched_text, confidence in findings:
        masked = _mask(matched_text)
        severity = _severity_for(pattern, confidence)

        flag, created = AntiCircumventionFlag.objects.get_or_create(
            user=listing.seller,
            listing=listing,
            detected_pattern=pattern,
            matched_text=masked,
            defaults={
                "target_type": ContentScanTarget.LISTING_BODY,
                "confidence": confidence,
                "severity": severity,
            },
        )
        if not created:
            continue  # already flagged this exact match on this listing before

        created_flags.append(flag)

        if pattern in HIGH_CONFIDENCE_PATTERNS:
            action = next_graduated_action(listing.seller)
            apply_enforcement_action(flag, action, reviewed_by_human=False, reviewer=None)

    return created_flags
