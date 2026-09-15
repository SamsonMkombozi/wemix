import io

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase
from rest_framework.exceptions import ValidationError

from .validators import validate_document_upload, validate_image_upload, validate_video_upload


def make_real_png_bytes():
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (10, 10), color="red").save(buf, format="PNG")
    return buf.getvalue()


class ImageUploadValidatorTests(SimpleTestCase):
    def test_accepts_real_png(self):
        f = SimpleUploadedFile("a.png", make_real_png_bytes(), content_type="image/png")
        validate_image_upload(f)  # should not raise

    def test_rejects_non_image_bytes_disguised_as_image(self):
        f = SimpleUploadedFile("a.png", b"this is not an image at all", content_type="image/png")
        with self.assertRaises(ValidationError):
            validate_image_upload(f)

    def test_rejects_oversized_image(self):
        from core import validators

        f = SimpleUploadedFile("a.png", make_real_png_bytes(), content_type="image/png")
        f.size = validators.MAX_IMAGE_BYTES + 1
        with self.assertRaises(ValidationError):
            validate_image_upload(f)


class VideoUploadValidatorTests(SimpleTestCase):
    def test_accepts_mp4_signature(self):
        content = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 100
        f = SimpleUploadedFile("a.mp4", content, content_type="video/mp4")
        validate_video_upload(f)  # should not raise

    def test_rejects_non_video_bytes(self):
        f = SimpleUploadedFile("a.mp4", b"definitely not a video", content_type="video/mp4")
        with self.assertRaises(ValidationError):
            validate_video_upload(f)


class DocumentUploadValidatorTests(SimpleTestCase):
    def test_accepts_pdf_signature(self):
        f = SimpleUploadedFile("a.pdf", b"%PDF-1.4\nrest of file", content_type="application/pdf")
        validate_document_upload(f)  # should not raise

    def test_rejects_non_pdf_bytes(self):
        f = SimpleUploadedFile("a.pdf", b"not a pdf", content_type="application/pdf")
        with self.assertRaises(ValidationError):
            validate_document_upload(f)
