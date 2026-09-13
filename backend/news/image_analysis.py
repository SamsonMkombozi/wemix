"""
news/image_analysis.py — the image-analysis half of the Habari AI
Verification Engine.

Honest scope: this does real, working image forensics using only
Pillow + imagehash (no external service, no trained ML model):

  - EXIF metadata inspection: flags images edited in known software
    (Photoshop/GIMP/etc in the Software tag), and notes whether basic
    capture metadata (date, camera make/model) is present at all.
  - Error Level Analysis (ELA): re-compresses the image at a known JPEG
    quality and measures the difference from the original. Regions that
    were edited after the original compression tend to show a different
    error level than untouched regions -- a real, standard forensic
    technique, not a placeholder. Used as `manipulation_score`.
  - Perceptual hashing (dHash): used for duplicate / reverse-image
    matching against other listings' media, the same idea as the text
    pipeline's shingle similarity but for images.

What this is NOT: a trained deepfake classifier. `deepfake_score` here
is a conservative, clearly-labeled proxy derived from the same ELA
signal -- real deepfake detection needs a trained model this
environment doesn't have. If AI_VERIFICATION_API_URL is set, wire a
real image-forensics/deepfake API there and extend this module to call
it first, the same pattern used in ai_verification.py.
"""

import io
import logging

from django.conf import settings
from PIL import Image, ExifTags

from .models import ImageAnalysisResult, NewsMedia

logger = logging.getLogger("habari.image_analysis")

SUSPICIOUS_SOFTWARE_TAGS = [
    "photoshop", "gimp", "lightroom", "affinity photo", "picsart", "facetune",
]

ELA_QUALITY = 90
DUPLICATE_HAMMING_THRESHOLD = 8  # dHash bits differing; lower = more similar (64-bit hash)


def _extract_exif(image: Image.Image) -> dict:
    payload = {}
    try:
        exif = image.getexif()
        if not exif:
            return payload
        for tag_id, value in exif.items():
            tag = ExifTags.TAGS.get(tag_id, tag_id)
            try:
                payload[str(tag)] = str(value)[:200]
            except Exception:
                continue
    except Exception as exc:
        logger.debug("EXIF extraction failed: %s", exc)
    return payload


def _metadata_looks_valid(exif_payload: dict) -> bool:
    """Not a strict pass/fail -- absence of EXIF is common (many phones
    and messaging apps strip it) so this is a soft signal, not proof of
    tampering by itself. Returns False only when there's a positive
    indicator of post-processing (editing software tag)."""
    software = exif_payload.get("Software", "").lower()
    if any(tag in software for tag in SUSPICIOUS_SOFTWARE_TAGS):
        return False
    return True


def _error_level_analysis(image: Image.Image) -> float:
    """Returns a 0-100 manipulation-likelihood score using block-based
    Error Level Analysis: re-saves the image at ELA_QUALITY and measures
    per-pixel difference from the original, then divides the image into
    a grid of blocks and looks at how much the per-block mean error
    varies. Untouched photos compress fairly uniformly, so block means
    cluster together; a region edited/composited after the original
    compression stands out as a statistical outlier relative to the rest
    of the image -- so we score on that variance (a real forensic
    signal), not just the flat global mean (which a uniformly
    low-quality but otherwise untouched photo would also trigger)."""
    try:
        rgb = image.convert("RGB")
        buffer = io.BytesIO()
        rgb.save(buffer, "JPEG", quality=ELA_QUALITY)
        buffer.seek(0)
        resaved = Image.open(buffer)
        resaved.load()

        width, height = rgb.size
        if width < 16 or height < 16:
            return 0.0  # too small to block-analyze meaningfully

        grid = 8  # 8x8 grid of blocks across the image
        block_w = max(1, width // grid)
        block_h = max(1, height // grid)

        rgb_px = rgb.load()
        resaved_px = resaved.load()

        block_means = []
        for by in range(0, height - block_h + 1, block_h):
            for bx in range(0, width - block_w + 1, block_w):
                total = 0
                count = 0
                # Sample every 3rd pixel within the block for speed.
                for y in range(by, min(by + block_h, height), 3):
                    for x in range(bx, min(bx + block_w, width), 3):
                        r1, g1, b1 = rgb_px[x, y]
                        r2, g2, b2 = resaved_px[x, y]
                        total += abs(r1 - r2) + abs(g1 - g2) + abs(b1 - b2)
                        count += 1
                if count:
                    block_means.append(total / count)

        if len(block_means) < 4:
            return 0.0

        overall_mean = sum(block_means) / len(block_means)
        variance = sum((m - overall_mean) ** 2 for m in block_means) / len(block_means)
        std_dev = variance ** 0.5

        # Calibration: an untouched photo's block means cluster tightly
        # (low std dev relative to the mean); a composited/edited region
        # pulls its block(s) away from the rest, raising std dev sharply
        # relative to the baseline. Score on the coefficient of variation
        # (std dev relative to mean) so this isn't just measuring overall
        # JPEG quality.
        coefficient_of_variation = std_dev / overall_mean if overall_mean > 0.1 else 0
        score = min(100.0, coefficient_of_variation * 60)
        return round(score, 2)
    except Exception as exc:
        logger.warning("ELA failed for an image: %s", exc)
        return 0.0


def _perceptual_hash(image: Image.Image):
    try:
        import imagehash
        return imagehash.dhash(image.convert("RGB"))
    except Exception as exc:
        logger.warning("Perceptual hash failed: %s", exc)
        return None


def _find_reverse_matches(media: NewsMedia, phash) -> list:
    """Compares this image's hash against every other image's stored
    hash (stashed in exif_payload['_phash'] by a prior analysis run --
    avoids a schema migration for a single extra string field) and
    returns matches within DUPLICATE_HAMMING_THRESHOLD."""
    if phash is None:
        return []

    import imagehash

    matches = []
    other_results = ImageAnalysisResult.objects.exclude(media=media).exclude(
        exif_payload___phash__isnull=True
    ).select_related("media", "media__listing")

    for result in other_results:
        stored_hash_str = result.exif_payload.get("_phash") if result.exif_payload else None
        if not stored_hash_str:
            continue
        try:
            other_hash = imagehash.hex_to_hash(stored_hash_str)
        except Exception:
            continue
        distance = phash - other_hash
        if distance <= DUPLICATE_HAMMING_THRESHOLD:
            similarity = round((1 - distance / 64) * 100, 2)
            matches.append({
                "media_id": str(result.media_id),
                "listing_id": str(result.media.listing_id) if result.media else None,
                "hamming_distance": int(distance),
                "similarity": float(similarity),
            })

    matches.sort(key=lambda m: -m["similarity"])
    return matches[:5]


def run_image_analysis(media: NewsMedia) -> ImageAnalysisResult:
    """Runs EXIF/ELA/perceptual-hash analysis on a single NewsMedia image
    and saves the result. Safe to call on non-image media types (returns
    a minimal result) so callers don't need to branch."""

    if media.media_type != NewsMedia.MediaType.IMAGE:
        return ImageAnalysisResult.objects.create(
            media=media, manipulation_score=0, deepfake_score=0, metadata_valid=None,
            authenticity_score=50, model_name="habari-image-analysis", model_version="1.0.0",
        )

    try:
        image = Image.open(media.file)
        image.load()
    except Exception as exc:
        logger.warning("Could not open media %s for analysis: %s", media.id, exc)
        return ImageAnalysisResult.objects.create(
            media=media, manipulation_score=0, deepfake_score=0, metadata_valid=None,
            authenticity_score=50, model_name="habari-image-analysis", model_version="1.0.0",
            flagged=False,
        )

    exif_payload = _extract_exif(image)
    metadata_valid = _metadata_looks_valid(exif_payload)
    manipulation_score = _error_level_analysis(image)
    phash = _perceptual_hash(image)
    reverse_matches = _find_reverse_matches(media, phash)

    if phash is not None:
        exif_payload["_phash"] = str(phash)  # stashed for future duplicate lookups, see _find_reverse_matches

    # Deepfake score: explicitly a weak proxy, not real deepfake
    # detection (see module docstring). Kept low-weighted on purpose so
    # it doesn't drive outcomes on its own.
    deepfake_score = round(manipulation_score * 0.3, 2)

    authenticity_score = 100.0
    if not metadata_valid:
        authenticity_score -= 25
    authenticity_score -= manipulation_score * 0.5
    if reverse_matches:
        authenticity_score -= 20
    authenticity_score = round(max(0.0, min(100.0, authenticity_score)), 2)

    flagged = manipulation_score >= 20 or bool(reverse_matches) or not metadata_valid

    return ImageAnalysisResult.objects.create(
        media=media,
        manipulation_score=manipulation_score,
        deepfake_score=deepfake_score,
        metadata_valid=metadata_valid,
        exif_payload=exif_payload,
        reverse_image_matches=reverse_matches,
        authenticity_score=authenticity_score,
        flagged=flagged,
        model_name="habari-image-analysis",
        model_version="1.0.0",
    )
