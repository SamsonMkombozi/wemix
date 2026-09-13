"""
news/ai_verification.py — the Habari AI Verification Engine (text pipeline).

Analyzes a NewsListing's title/description/body and produces an
AIVerificationResult: fake-news score, misinformation score, clickbait
score, sentiment, authenticity, similarity-to-existing-content, risk, and
an overall confidence + outcome.

Two modes, chosen automatically:

1. EXTERNAL (if AI_VERIFICATION_API_URL is configured in .env): POSTs the
   listing's text to that URL and expects a JSON response with the same
   score fields. This is the integration point for a real hosted model
   (OpenAI/Anthropic/a custom classifier/etc.) if/when one is available.

2. LOCAL HEURISTIC (default, no external service required): a rule-based
   NLP-lite engine -- clickbait phrase/pattern detection, attribution-
   phrase presence, absolute-claim language, lexicon sentiment, and
   Jaccard shingle similarity against other listings for duplicate
   detection. This is NOT a trained ML model; it's honest, functional,
   deterministic scoring that gives the pipeline real teeth without
   depending on network access or an API key. Swap in a real model later
   by setting AI_VERIFICATION_API_URL -- the rest of the system (outcome
   thresholds, auto-publish, moderation queue) doesn't change either way.

Call run_ai_verification(listing) to execute the pipeline end-to-end:
creates the AIVerificationResult row AND updates the listing's
verification_status/ai_score/status based on AI_AUTO_VERIFY_THRESHOLD /
AI_AUTO_REJECT_THRESHOLD from settings.
"""

import logging
import re
from dataclasses import dataclass, asdict

from django.conf import settings
from django.utils import timezone

from .models import AIVerificationResult, NewsListing

logger = logging.getLogger("habari.ai_verification")

# ---------------------------------------------------------------------------
# Local heuristic engine
# ---------------------------------------------------------------------------

CLICKBAIT_PHRASES = [
    "you won't believe", "shocking", "gone wrong", "gone viral", "number will surprise you",
    "this is why", "what happens next", "doctors hate", "one weird trick", "the truth about",
    "they don't want you to know", "will blow your mind", "epic fail", "you'll never guess",
]

ABSOLUTE_CLAIM_PATTERNS = [
    r"\b100%\s*(proof|true|confirmed)\b", r"\beveryone knows\b", r"\bnobody is telling you\b",
    r"\bsecret\b", r"\bcover[\s-]?up\b", r"\bthey don'?t want you to know\b",
    r"\balways\b", r"\bnever\b(?!\s+the\s?less)", r"\bproves? beyond doubt\b",
]

ATTRIBUTION_PHRASES = [
    "according to", "sources say", "officials said", "officials confirmed", "reported by",
    "spokesperson", "in a statement", "confirmed by", "data from", "figures from",
    "told reporters", "press release", "ministry of", "police said",
]

POSITIVE_WORDS = {
    "approved", "growth", "success", "improve", "improved", "record", "win", "wins", "won",
    "boost", "gain", "positive", "progress", "agreement", "celebrate", "celebration", "milestone",
}
NEGATIVE_WORDS = {
    "crisis", "collapse", "death", "died", "killed", "fraud", "scandal", "corruption", "attack",
    "violence", "protest", "riot", "disaster", "outbreak", "conflict", "controversy", "banned",
}

WORD_RE = re.compile(r"[A-Za-z']+")


def _words(text: str) -> list:
    return WORD_RE.findall(text.lower())


def _shingles(text: str, n: int = 5) -> set:
    words = _words(text)
    if len(words) < n:
        return {" ".join(words)} if words else set()
    return {" ".join(words[i:i + n]) for i in range(len(words) - n + 1)}


def _jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    intersection = len(a & b)
    union = len(a | b)
    return (intersection / union) * 100 if union else 0.0


def _clickbait_score(title: str, description: str) -> float:
    text = f"{title} {description}".lower()
    score = 0.0

    for phrase in CLICKBAIT_PHRASES:
        if phrase in text:
            score += 18

    exclamations = title.count("!")
    score += min(exclamations * 12, 24)

    words = title.split()
    if words:
        caps_words = [w for w in words if len(w) > 2 and w.isupper()]
        caps_ratio = len(caps_words) / len(words)
        score += caps_ratio * 30

    if re.match(r"^\d+\s+\w", title.strip()):  # "7 reasons why..." style listicles
        score += 10

    if title.strip().endswith("?") and len(title.split()) < 12:
        score += 8

    return round(min(score, 100.0), 2)


def _attribution_count(text: str) -> int:
    lowered = text.lower()
    return sum(1 for phrase in ATTRIBUTION_PHRASES if phrase in lowered)


def _absolute_claim_count(text: str) -> int:
    lowered = text.lower()
    return sum(1 for pattern in ABSOLUTE_CLAIM_PATTERNS if re.search(pattern, lowered))


def _named_entity_proxy_count(text: str) -> int:
    """Crude proxy for named entities: counts capitalized word sequences
    (e.g. 'Dar es Salaam', 'Bank of Tanzania') outside of sentence starts."""
    matches = re.findall(r"(?<!\. )(?<!^)\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,3})\b", text)
    return len(set(matches))


def _fake_news_score(title: str, description: str, body: str) -> float:
    full_text = f"{title} {description} {body}"
    word_count = len(_words(full_text))

    score = 0.0
    absolute_claims = _absolute_claim_count(full_text)
    score += min(absolute_claims * 16, 55)

    attributions = _attribution_count(full_text)
    if attributions == 0:
        score += 20
    elif attributions == 1:
        score += 8

    entities = _named_entity_proxy_count(body)
    if word_count > 40:
        if entities == 0:
            score += 15
        elif entities == 1:
            score += 8

    if word_count < 30:
        score += 15  # too short to substantiate claims

    clickbait = _clickbait_score(title, description)
    score += clickbait * 0.25

    return round(min(score, 100.0), 2)


def _misinformation_score(fake_news_score: float, absolute_claims: int) -> float:
    return round(min(fake_news_score * 0.7 + absolute_claims * 5, 100.0), 2)


def _sentiment_score(text: str) -> float:
    words = _words(text)
    if not words:
        return 0.0
    pos = sum(1 for w in words if w in POSITIVE_WORDS)
    neg = sum(1 for w in words if w in NEGATIVE_WORDS)
    if pos + neg == 0:
        return 0.0
    return round((pos - neg) / (pos + neg), 3)


def _authenticity_score(fake_news_score: float, attributions: int, entities: int) -> float:
    base = 100 - fake_news_score
    base += min(attributions * 4, 16)
    base += min(entities * 2, 10)
    return round(max(0.0, min(base, 100.0)), 2)


@dataclass
class HeuristicResult:
    fake_news_score: float
    misinformation_score: float
    clickbait_score: float
    authenticity_score: float
    sentiment_score: float
    similarity_score: float
    duplicate_of_id: str
    risk_score: float
    confidence_score: float


def _find_most_similar(listing: NewsListing):
    """Compares this listing's body against other PUBLISHED/SUBMITTED
    listings (excluding itself) using 5-word shingle Jaccard similarity.
    Returns (best_score, best_match_listing_or_None)."""
    my_shingles = _shingles(f"{listing.title} {listing.body}")
    if not my_shingles:
        return 0.0, None

    candidates = NewsListing.objects.exclude(pk=listing.pk).filter(
        status__in=[NewsListing.ListingStatus.PUBLISHED, NewsListing.ListingStatus.SUBMITTED]
    ).only("id", "title", "body")

    best_score = 0.0
    best_match = None
    for other in candidates:
        other_shingles = _shingles(f"{other.title} {other.body}")
        score = _jaccard(my_shingles, other_shingles)
        if score > best_score:
            best_score = score
            best_match = other

    return round(best_score, 2), best_match


def run_local_heuristic_engine(listing: NewsListing) -> HeuristicResult:
    title, description, body = listing.title, listing.description, listing.body
    full_text = f"{title} {description} {body}"

    fake_news = _fake_news_score(title, description, body)
    clickbait = _clickbait_score(title, description)
    absolute_claims = _absolute_claim_count(full_text)
    attributions = _attribution_count(full_text)
    entities = _named_entity_proxy_count(body)

    misinformation = _misinformation_score(fake_news, absolute_claims)
    sentiment = _sentiment_score(full_text)
    authenticity = _authenticity_score(fake_news, attributions, entities)
    similarity, duplicate_match = _find_most_similar(listing)

    risk = round(min((fake_news * 0.5 + clickbait * 0.3 + similarity * 0.2), 100.0), 2)
    confidence = round(min(95.0, 40 + abs(fake_news - 50) * 1.1), 2)

    return HeuristicResult(
        fake_news_score=fake_news,
        misinformation_score=misinformation,
        clickbait_score=clickbait,
        authenticity_score=authenticity,
        sentiment_score=sentiment,
        similarity_score=similarity,
        duplicate_of_id=str(duplicate_match.id) if duplicate_match and similarity >= 85 else "",
        risk_score=risk,
        confidence_score=confidence,
    )


# ---------------------------------------------------------------------------
# Optional external API mode
# ---------------------------------------------------------------------------

def _run_external_api(listing: NewsListing) -> dict:
    import requests

    payload = {
        "title": listing.title,
        "description": listing.description,
        "body": listing.body,
        "news_type": listing.news_type,
    }
    headers = {"Authorization": f"Bearer {settings.AI_VERIFICATION_API_KEY}"} if settings.AI_VERIFICATION_API_KEY else {}
    resp = requests.post(settings.AI_VERIFICATION_API_URL, json=payload, headers=headers, timeout=15)
    resp.raise_for_status()
    return resp.json()


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def _decide_outcome(fake_news_score: float, similarity_score: float, risk_score: float) -> str:
    reject_threshold = settings.AI_AUTO_REJECT_THRESHOLD
    verify_threshold = settings.AI_AUTO_VERIFY_THRESHOLD

    if similarity_score >= 85:
        return AIVerificationResult.Outcome.REJECTED
    if fake_news_score >= reject_threshold:
        return AIVerificationResult.Outcome.REJECTED
    if fake_news_score <= verify_threshold and risk_score < 40:
        return AIVerificationResult.Outcome.VERIFIED
    if fake_news_score < (reject_threshold + verify_threshold) / 2:
        return AIVerificationResult.Outcome.PARTIALLY_VERIFIED
    return AIVerificationResult.Outcome.NEEDS_HUMAN_REVIEW


def run_ai_verification(listing: NewsListing) -> AIVerificationResult:
    """Runs the verification pipeline for a listing, saves the result, and
    updates the listing's verification_status/ai_score. Auto-publishes on
    a VERIFIED outcome; leaves status alone (awaiting moderation) for any
    other outcome."""

    used_external = False
    if settings.AI_VERIFICATION_API_URL:
        try:
            data = _run_external_api(listing)
            used_external = True
            result_kwargs = {
                "fake_news_score": data["fake_news_score"],
                "misinformation_score": data.get("misinformation_score", 0),
                "clickbait_score": data.get("clickbait_score", 0),
                "authenticity_score": data.get("authenticity_score", 0),
                "sentiment_score": data.get("sentiment_score", 0),
                "similarity_score": data.get("similarity_score", 0),
                "risk_score": data.get("risk_score", 0),
                "confidence_score": data.get("confidence_score", 50),
                "raw_response": data,
                "model_name": data.get("model_name", "external"),
                "model_version": data.get("model_version", ""),
            }
            duplicate_of_id = data.get("duplicate_of_id") or None
        except Exception as exc:
            logger.warning("External AI verification API failed (%s); falling back to local heuristic engine.", exc)
            used_external = False

    if not used_external:
        heuristic = run_local_heuristic_engine(listing)
        result_kwargs = {
            "fake_news_score": heuristic.fake_news_score,
            "misinformation_score": heuristic.misinformation_score,
            "clickbait_score": heuristic.clickbait_score,
            "authenticity_score": heuristic.authenticity_score,
            "sentiment_score": heuristic.sentiment_score,
            "similarity_score": heuristic.similarity_score,
            "risk_score": heuristic.risk_score,
            "confidence_score": heuristic.confidence_score,
            "raw_response": asdict(heuristic),
            "model_name": "habari-local-heuristic-engine",
            "model_version": "1.0.0",
        }
        duplicate_of_id = heuristic.duplicate_of_id or None

    outcome = _decide_outcome(result_kwargs["fake_news_score"], result_kwargs["similarity_score"], result_kwargs["risk_score"])

    result = AIVerificationResult.objects.create(
        listing=listing,
        outcome=outcome,
        duplicate_of_id=duplicate_of_id,
        **result_kwargs,
    )

    listing.verification_status = outcome
    listing.ai_score = round(100 - result_kwargs["fake_news_score"], 2)
    if outcome == AIVerificationResult.Outcome.VERIFIED:
        listing.status = NewsListing.ListingStatus.PUBLISHED
        listing.published_at = listing.published_at or timezone.now()
    listing.save(update_fields=["verification_status", "ai_score", "status", "published_at", "updated_at"])

    notify_verification_outcome(listing, outcome)

    return result


def notify_verification_outcome(listing: NewsListing, outcome: str) -> None:
    from core.models import Notification
    from core.notifications import send_notification

    if outcome == AIVerificationResult.Outcome.VERIFIED:
        send_notification(
            user=listing.seller, notification_type=Notification.NotificationType.LISTING_APPROVED,
            title=f"'{listing.title}' was approved and published",
            message="Your listing passed AI verification and is now live on the marketplace.",
            link_path=f"listing.html?slug={listing.slug}",
        )
    elif outcome == AIVerificationResult.Outcome.REJECTED:
        send_notification(
            user=listing.seller, notification_type=Notification.NotificationType.LISTING_REJECTED,
            title=f"'{listing.title}' was rejected",
            message="Your listing did not pass AI verification. Review the content and try resubmitting.",
            link_path="dashboard.html?tab=listings",
        )
    elif outcome == AIVerificationResult.Outcome.NEEDS_HUMAN_REVIEW:
        send_notification(
            user=listing.seller, notification_type=Notification.NotificationType.LISTING_NEEDS_REVIEW,
            title=f"'{listing.title}' needs manual review",
            message="Your listing is under moderator review before it can be published.",
            link_path="dashboard.html?tab=listings",
            send_email=False,  # softer outcome, don't email-spam for this one
        )
