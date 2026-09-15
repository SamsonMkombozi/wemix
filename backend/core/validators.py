"""
Upload validation shared by every app that accepts a file (news media,
KYC documents, corporate verification docs). Wires up
settings.ALLOWED_UPLOAD_IMAGE_TYPES / ALLOWED_UPLOAD_VIDEO_TYPES, which
existed as configuration but were never actually referenced anywhere --
every upload was previously trusted based on its filename extension and
the client-supplied Content-Type header alone, both of which a malicious
client can set to anything regardless of the file's real content.

This does real content sniffing (Pillow decode for images, magic-byte
checks for video/PDF) rather than trusting labels, but it is not a
substitute for an actual antivirus/malware scanner -- that's a separate,
larger integration (e.g. ClamAV) this doesn't attempt.
"""

from django.conf import settings
from rest_framework import serializers

MAX_IMAGE_BYTES = 10 * 1024 * 1024  # 10 MB
MAX_VIDEO_BYTES = 100 * 1024 * 1024  # 100 MB
MAX_DOCUMENT_BYTES = 15 * 1024 * 1024  # 15 MB

# Magic-byte signatures for formats we don't have a decoder library for.
# Checked against the first chunk of the file, not the filename/extension.
_VIDEO_SIGNATURES = (
    (4, b"ftyp"),  # MP4 / MOV (ISO base media container) -- signature sits at offset 4
)
_PDF_SIGNATURE = b"%PDF-"


def validate_image_upload(file_obj) -> None:
    """Raises serializers.ValidationError unless `file_obj` is a real,
    decodable image in an allowed format and under the size cap."""
    if file_obj.size > MAX_IMAGE_BYTES:
        raise serializers.ValidationError(f"Image is too large (max {MAX_IMAGE_BYTES // (1024 * 1024)}MB).")

    from PIL import Image, UnidentifiedImageError

    file_obj.seek(0)
    try:
        img = Image.open(file_obj)
        img.verify()
    except (UnidentifiedImageError, OSError, ValueError):
        raise serializers.ValidationError("This file isn't a valid image (or is corrupted).")
    finally:
        file_obj.seek(0)

    pil_format_to_mime = {"JPEG": "image/jpeg", "PNG": "image/png", "WEBP": "image/webp"}
    detected_mime = pil_format_to_mime.get(img.format)
    if detected_mime not in settings.ALLOWED_UPLOAD_IMAGE_TYPES:
        raise serializers.ValidationError(
            f"Unsupported image format ({img.format or 'unknown'}). Allowed: {', '.join(settings.ALLOWED_UPLOAD_IMAGE_TYPES)}."
        )


def validate_video_upload(file_obj) -> None:
    """Raises serializers.ValidationError unless `file_obj` looks like a
    real video container matching an allowed type, under the size cap.
    Magic-byte check only -- doesn't decode/transcode to confirm playability."""
    if file_obj.size > MAX_VIDEO_BYTES:
        raise serializers.ValidationError(f"Video is too large (max {MAX_VIDEO_BYTES // (1024 * 1024)}MB).")

    file_obj.seek(0)
    header = file_obj.read(16)
    file_obj.seek(0)

    if not any(header[offset:offset + len(sig)] == sig for offset, sig in _VIDEO_SIGNATURES):
        raise serializers.ValidationError("This file doesn't look like a valid MP4/MOV video.")


def validate_document_upload(file_obj) -> None:
    """Raises serializers.ValidationError unless `file_obj` is a real PDF
    under the size cap. KYC/corporate docs are expected as PDF or image;
    images go through validate_image_upload instead."""
    if file_obj.size > MAX_DOCUMENT_BYTES:
        raise serializers.ValidationError(f"Document is too large (max {MAX_DOCUMENT_BYTES // (1024 * 1024)}MB).")

    file_obj.seek(0)
    header = file_obj.read(8)
    file_obj.seek(0)

    if not header.startswith(_PDF_SIGNATURE):
        raise serializers.ValidationError("This file doesn't look like a valid PDF.")


def validate_document_or_image_upload(file_obj) -> None:
    """For fields that legitimately accept either a scanned image or a
    PDF (business license, company registration, tax documents)."""
    file_obj.seek(0)
    header = file_obj.read(8)
    file_obj.seek(0)
    if header.startswith(_PDF_SIGNATURE):
        return validate_document_upload(file_obj)
    return validate_image_upload(file_obj)
