"""
news/media_preview.py — generates the watermarked preview image
NewsMedia.preview_file is supposed to hold, per the model's own
docstring ("Preview media may be watermarked or truncated; full media is
only served after purchase"). Previously that field existed but nothing
ever populated it, so the "preview vs full" split described there wasn't
actually enforced anywhere -- see NewsListingDetailSerializer.get_media,
which now withholds `file` pre-purchase and falls back to whatever this
module produces.

Honest scope: for images, this uses only Pillow (already a dependency)
to downsample and stamp a tiled watermark -- real, working image
processing, not a placeholder. Video/document media get no preview
asset (no ffmpeg in this environment to extract/stamp a frame); those
media types are simply withheld entirely pre-purchase instead.
"""

import io
import logging

from django.core.files.base import ContentFile
from PIL import Image, ImageDraw, ImageFont

from .models import NewsMedia

logger = logging.getLogger("habari.media_preview")

PREVIEW_MAX_DIMENSION = 900
WATERMARK_TEXT = "WEMIX PREVIEW — PURCHASE TO UNLOCK"

# Cover images are what every listing card/hero banner across the site
# actually displays (NewsListingSerializer.get_cover_image prefers
# preview_file), so unlike other gallery images they're cropped to one
# fixed aspect ratio here -- a seller can upload any size/shape source and
# every listing still shows a consistent, predictable cover, matching how
# every real marketplace/wire-photo site standardizes card imagery. The
# original file a paying buyer downloads is never touched -- only this
# derived preview copy is cropped.
COVER_TARGET_SIZE = (1200, 675)  # 16:9, matches the hero/card widescreen convention


def _crop_to_fill(image: Image.Image, target_w: int, target_h: int) -> Image.Image:
    """Center-crops `image` to the target aspect ratio, then resizes to
    exactly target_w x target_h -- the same "cover" behavior CSS
    object-fit: cover gives in the browser, done once server-side so the
    stored preview is already the standard size instead of relying on the
    viewport to crop an arbitrarily-shaped image down every time."""
    src_w, src_h = image.size
    target_ratio = target_w / target_h
    src_ratio = src_w / src_h
    if src_ratio > target_ratio:
        # Source is wider than target -- crop the sides.
        new_w = int(src_h * target_ratio)
        left = (src_w - new_w) // 2
        image = image.crop((left, 0, left + new_w, src_h))
    elif src_ratio < target_ratio:
        # Source is taller than target -- crop top/bottom.
        new_h = int(src_w / target_ratio)
        top = (src_h - new_h) // 2
        image = image.crop((0, top, src_w, top + new_h))
    return image.resize((target_w, target_h), Image.LANCZOS)


def generate_preview(media: NewsMedia) -> None:
    """Populates media.preview_file with a downsampled, watermarked copy
    of media.file. Safe to call on non-image media (no-op) or if Pillow
    can't open/process the file (logs and returns) -- never raises, the
    same degrade-gracefully contract as the rest of the AI verification
    pipeline (image_analysis.py, ai_verification.py)."""
    if media.media_type != NewsMedia.MediaType.IMAGE:
        return
    if media.preview_file:
        return  # already generated (e.g. a retried task)

    try:
        image = Image.open(media.file)
        image.load()
    except Exception as exc:
        logger.warning("Could not open media %s to build a preview: %s", media.id, exc)
        return

    try:
        rgb = image.convert("RGB")
        if media.is_cover:
            rgb = _crop_to_fill(rgb, *COVER_TARGET_SIZE)
        else:
            rgb.thumbnail((PREVIEW_MAX_DIMENSION, PREVIEW_MAX_DIMENSION), Image.LANCZOS)
        width, height = rgb.size

        # Tiled, rotated watermark: draw the label repeatedly onto a
        # larger transparent canvas, rotate it, then crop back down to
        # the image's own size -- avoids per-tile trigonometry while
        # still covering the whole frame at an angle, the same visual
        # convention stock-photo previews use (Getty/Shutterstock/etc).
        diag = int((width ** 2 + height ** 2) ** 0.5)
        overlay = Image.new("RGBA", (diag, diag), (0, 0, 0, 0))
        draw = ImageDraw.Draw(overlay)
        font = ImageFont.load_default(size=max(16, width // 24))

        text_w = draw.textlength(WATERMARK_TEXT, font=font)
        step_x = int(text_w) + 70
        step_y = max(70, height // 6)
        for y in range(0, diag, step_y):
            for x in range(0, diag, step_x):
                draw.text((x, y), WATERMARK_TEXT, font=font, fill=(255, 255, 255, 130))

        overlay = overlay.rotate(-30, expand=False)
        left = (diag - width) // 2
        top = (diag - height) // 2
        overlay = overlay.crop((left, top, left + width, top + height))

        watermarked = Image.alpha_composite(rgb.convert("RGBA"), overlay).convert("RGB")

        buffer = io.BytesIO()
        watermarked.save(buffer, "JPEG", quality=82)
        buffer.seek(0)
        media.preview_file.save(f"preview_{media.id.hex}.jpg", ContentFile(buffer.read()), save=True)
    except Exception as exc:
        logger.warning("Preview generation failed for media %s: %s", media.id, exc)
