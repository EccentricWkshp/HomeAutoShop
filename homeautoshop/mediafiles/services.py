"""
Media pipeline (SPEC FR-DOC-1..9).

The rules that shape this module:

* Originals are preserved byte-for-byte; derivatives are always regenerable.
* Derivation is asynchronous — the UI is never blocked on processing.
* GPS EXIF is stripped by default: a photo of a car in a driveway geotags a
  home address.
* Duplicates are detected by SHA-256 and offered as a link rather than a
  second copy.
"""

from __future__ import annotations

import logging
from io import BytesIO

from django.core.files.base import ContentFile
from django.utils import timezone

from homeautoshop.core.models import Job

from .models import Media, MediaLink

log = logging.getLogger(__name__)

THUMB_SIZE = (400, 400)
PREVIEW_SIZE = (1600, 1600)

IMAGE_MIMES = {"image/jpeg", "image/png", "image/webp", "image/gif", "image/heic", "image/heif"}


def ingest(
    upload,
    *,
    kind: str | None = None,
    user=None,
    entity=None,
    role: str = MediaLink.Role.OTHER,
    caption: str = "",
) -> tuple[Media, bool]:
    """Store an upload, returning (media, created).

    A duplicate returns the existing media with created=False so the caller can
    offer to link it instead of storing a second copy (FR-DOC-6).
    """
    data = upload.read()
    upload.seek(0)
    digest = Media.hash_bytes(data)

    existing = Media.objects.filter(sha256=digest).first()
    if existing:
        if entity is not None:
            link(existing, entity, role=role, caption=caption)
        return existing, False

    mime = getattr(upload, "content_type", "") or ""
    if kind is None:
        kind = Media.Kind.PHOTO if mime in IMAGE_MIMES else Media.Kind.DOCUMENT

    media = Media(
        kind=kind,
        original_filename=getattr(upload, "name", "")[:255],
        mime=mime,
        bytes=len(data),
        sha256=digest,
        created_by=user if getattr(user, "pk", None) else None,
    )
    # Documents are OCR'd, and so are photos filed as receipts — in a home shop
    # a receipt is far more often photographed than scanned (FR-COST-4).
    if kind == Media.Kind.DOCUMENT or role == MediaLink.Role.RECEIPT:
        media.ocr_status = Media.OcrStatus.PENDING

    media.file.save(getattr(upload, "name", "upload.bin"), ContentFile(data), save=False)
    media.save()

    if entity is not None:
        link(media, entity, role=role, caption=caption)

    # Derivation is a job, not part of the request (FR-DOC-3, NFR-P-4).
    Job.objects.create(type="media.derive", payload={"media_id": str(media.pk)})
    if media.ocr_status == Media.OcrStatus.PENDING:
        Job.objects.create(type="media.ocr", payload={"media_id": str(media.pk)})
    return media, True


def link(media: Media, entity, *, role: str = MediaLink.Role.OTHER, caption: str = "") -> MediaLink:
    obj, _created = MediaLink.objects.get_or_create(
        media=media,
        entity_type=entity.__class__.__name__,
        entity_id=entity.pk,
        role=role,
        defaults={"caption": caption},
    )
    return obj


def derive(media: Media) -> None:
    """Generate thumbnail and preview, and strip GPS EXIF. Idempotent."""
    from django.conf import settings

    if not media.is_image or not media.file:
        media.derived_at = timezone.now()
        media.save(update_fields=["derived_at", "updated_at"])
        return

    try:
        from PIL import Image, ImageOps
    except ImportError:  # pragma: no cover - Pillow is a hard requirement
        log.warning("Pillow unavailable; skipping derivation for %s", media.pk)
        return

    with media.file.open("rb") as fh:
        image = Image.open(fh)
        image.load()

    # Honour the EXIF orientation tag, then discard EXIF entirely on the
    # derivatives. exif_transpose bakes the rotation into pixels, so dropping
    # the metadata afterwards cannot flip the image.
    image = ImageOps.exif_transpose(image)
    media.width, media.height = image.size

    exif = getattr(image, "getexif", lambda: None)()
    if exif:
        captured = exif.get(36867) or exif.get(306)
        if captured and not media.captured_at:
            try:
                from datetime import datetime

                media.captured_at = timezone.make_aware(
                    datetime.strptime(str(captured), "%Y:%m:%d %H:%M:%S")
                )
            except (ValueError, TypeError):
                pass

    if image.mode not in ("RGB", "L"):
        image = image.convert("RGB")

    for attr, size, prefix in (
        ("thumb", THUMB_SIZE, "thumb"),
        ("preview", PREVIEW_SIZE, "preview"),
    ):
        copy = image.copy()
        copy.thumbnail(size, Image.Resampling.LANCZOS)
        buffer = BytesIO()
        # No exif= argument: the derivative carries no metadata at all, which
        # is how GPS stripping is guaranteed rather than hoped for.
        copy.save(buffer, format="JPEG", quality=85, optimize=True)
        getattr(media, attr).save(
            f"{prefix}_{media.pk}.jpg", ContentFile(buffer.getvalue()), save=False
        )

    media.gps_stripped = settings.STRIP_GPS_EXIF
    media.derived_at = timezone.now()
    media.save(
        update_fields=[
            "thumb",
            "preview",
            "width",
            "height",
            "captured_at",
            "gps_stripped",
            "derived_at",
            "updated_at",
        ]
    )


def ocr(media: Media) -> None:
    """Extract text so documents are searchable (FR-DOC-5).

    Tesseract is an optional system dependency. Where it is absent the media is
    marked `failed` with an explanatory note and everything else keeps working —
    an un-OCR'd receipt is still a receipt.
    """
    if not media.file:
        return

    try:
        import pytesseract
        from PIL import Image
    except ImportError:
        log.info("OCR skipped for %s: pytesseract not installed", media.pk)
        media.ocr_status = Media.OcrStatus.FAILED
        media.ocr_text = ""
        media.save(update_fields=["ocr_status", "ocr_text", "updated_at"])
        return

    text = ""
    try:
        if media.mime == "application/pdf":
            # A PDF with a text layer needs no OCR at all, and extracting it is
            # both faster and more accurate than rasterising first.
            text = _pdf_text(media)
        if not text.strip():
            with media.file.open("rb") as fh:
                image = Image.open(fh)
                image.load()
            text = pytesseract.image_to_string(image)
    except Exception as exc:
        log.warning("OCR failed for %s: %s", media.pk, exc)
        media.ocr_status = Media.OcrStatus.FAILED
        media.save(update_fields=["ocr_status", "updated_at"])
        return

    media.ocr_text = text.strip()[:200_000]
    media.ocr_status = Media.OcrStatus.DONE
    media.save(update_fields=["ocr_text", "ocr_status", "updated_at"])


def _pdf_text(media: Media) -> str:
    """Pull an existing text layer from a PDF, if there is one."""
    try:
        import pypdf
    except ImportError:
        return ""
    try:
        with media.file.open("rb") as fh:
            reader = pypdf.PdfReader(fh)
            return "\n".join((page.extract_text() or "") for page in reader.pages)
    except Exception:
        return ""
