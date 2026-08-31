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
import shutil
from functools import lru_cache
from io import BytesIO

from django.core.files.base import ContentFile
from django.utils import timezone

from homeautoshop.core.models import Job
from homeautoshop.core.runtime import conf

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
    # `pending` is recorded even when OCR is switched off, and the job is not.
    # The status says what this file *wants*, so turning OCR on later finds a
    # backlog to work through (`media.ocr_sweep`) rather than a set of rows that
    # claim to have been tried and failed.
    if media.ocr_status == Media.OcrStatus.PENDING and conf.OCR_ENABLED:
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

    media.gps_stripped = conf.STRIP_GPS_EXIF
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


def tesseract_status() -> dict:
    """What OCR can actually do on this instance, for the health screen.

    The README says OCR "needs Tesseract" and until now there was no way to
    find out from inside the application whether it was there. A capability
    nobody can check is one that gets discovered as a page of receipts that
    never became searchable.
    """
    status = {
        "enabled": bool(conf.OCR_ENABLED),
        "configured": conf.OCR_LANGUAGES.split("+"),
        "binary": shutil.which("tesseract") or "",
        "version": "",
        "installed": [],
        "missing": [],
    }
    try:
        import pytesseract
    except ImportError:
        return status

    try:
        status["version"] = str(pytesseract.get_tesseract_version())
        status["installed"] = sorted(pytesseract.get_languages(config=""))
    except Exception as exc:  # the binary is absent, or refuses to answer
        log.info("tesseract unavailable: %s", exc)
        return status

    status["missing"] = [
        lang for lang in status["configured"] if lang not in status["installed"]
    ]
    return status


@lru_cache(maxsize=8)
def _usable_languages(configured: str, installed: tuple[str, ...]) -> str:
    """The configured languages that this build actually has.

    Tesseract fails the whole call for one missing language rather than
    skipping it, so a fourth language added to the compose file without a
    rebuild would take the other three down with it. Narrowing to what is
    installed keeps OCR working and says once what is being ignored.
    """
    wanted = [lang for lang in configured.split("+") if lang]
    usable = [lang for lang in wanted if lang in installed]
    if not usable:
        # Better a wrong-language pass than none: Tesseract with the Latin
        # alphabet still reads part numbers, prices and dates off a receipt.
        return "eng" if "eng" in installed else (installed[0] if installed else "eng")
    if len(usable) != len(wanted):
        log.warning(
            "OCR languages %s are configured but not installed; using %s",
            ", ".join(sorted(set(wanted) - set(usable))),
            "+".join(usable),
        )
    return "+".join(usable)


def _ocr_language() -> str:
    status = tesseract_status()
    return _usable_languages(conf.OCR_LANGUAGES, tuple(status["installed"]))


def ocr(media: Media) -> None:
    """Extract text so documents are searchable (FR-DOC-5).

    Tesseract is an optional system dependency. Where it is absent the media is
    marked `failed` with an explanatory note and everything else keeps working —
    an un-OCR'd receipt is still a receipt.
    """
    if not media.file:
        return

    if not conf.OCR_ENABLED:
        # Left `pending` deliberately — see `ingest`. Nothing is marked failed
        # for a thing that was never attempted.
        log.info("OCR disabled; %s left pending", media.pk)
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
                # An image-only PDF — a scanner's output, and most of the
                # scan-tool reports (§8.3a). Pillow cannot open a PDF at all,
                # so without this the whole file failed on the page that was
                # the entire point of OCR-ing it.
                text = "\n".join(
                    pytesseract.image_to_string(page, lang=_ocr_language())
                    for page in _pdf_pages(media)
                )
        else:
            with media.file.open("rb") as fh:
                image = Image.open(fh)
                image.load()
            text = pytesseract.image_to_string(image, lang=_ocr_language())
    except Exception as exc:
        log.warning("OCR failed for %s: %s", media.pk, exc)
        media.ocr_status = Media.OcrStatus.FAILED
        media.save(update_fields=["ocr_status", "updated_at"])
        return

    media.ocr_text = text.strip()[:200_000]
    media.ocr_status = Media.OcrStatus.DONE
    media.save(update_fields=["ocr_text", "ocr_status", "updated_at"])


def read_image_text(raw: bytes) -> str:
    """OCR an image already in memory (§7.9).

    Split out from `ocr()` because the scan-report import has bytes and a
    person waiting, not a `Media` row and a queue. Returns empty rather than
    raising: a report that could not be read still becomes a session the
    operator can map by hand (FR-INT-6), and that is a better outcome than an
    error page.
    """
    if not conf.OCR_ENABLED:
        return ""
    try:
        import pytesseract
        from PIL import Image

        image = Image.open(BytesIO(raw))
        image.load()
        return pytesseract.image_to_string(image, lang=_ocr_language())
    except Exception as exc:  # noqa: BLE001
        log.warning("could not read text from an image: %s", exc)
        return ""


def read_pdf_text_by_ocr(raw: bytes) -> str:
    """OCR every page of an image-only PDF already in memory."""
    if not conf.OCR_ENABLED:
        return ""
    try:
        import pypdfium2
        import pytesseract
    except ImportError:
        return ""

    try:
        document = pypdfium2.PdfDocument(raw)
    except Exception as exc:  # noqa: BLE001
        log.warning("could not open a PDF for OCR: %s", exc)
        return ""

    found = []
    try:
        for index in range(min(len(document), conf.OCR_PDF_MAX_PAGES)):
            page = document[index]
            bitmap = page.render(scale=300 / 72)
            try:
                found.append(pytesseract.image_to_string(bitmap.to_pil(), lang=_ocr_language()))
            finally:
                bitmap.close()
                page.close()
    except Exception as exc:  # noqa: BLE001
        log.warning("could not read text from a scanned PDF: %s", exc)
    finally:
        document.close()
    return "\n".join(found)


def _pdf_pages(media: Media, *, dpi: int = 300):
    """Rasterise a PDF for OCR, a page at a time.

    `pypdfium2` arrives with `pdfplumber` and carries its own PDFium build, so
    this adds no system package — which matters, because the alternatives
    (poppler via pdf2image, or ImageMagick) would each be another apt line in
    the image for the same result.

    300 DPI because Tesseract's accuracy falls off sharply below it, and a page
    cap because a hundred-page manual dropped on the upload form should not
    become an hour of worker time and several gigabytes of bitmaps.
    """
    try:
        import pypdfium2
    except ImportError:
        log.info("image-only PDF %s not rasterised: pypdfium2 not installed", media.pk)
        return

    with media.file.open("rb") as fh:
        data = fh.read()
    document = pypdfium2.PdfDocument(data)
    try:
        limit = min(len(document), conf.OCR_PDF_MAX_PAGES)
        if len(document) > limit:
            log.info("OCR reading first %s of %s pages of %s", limit, len(document), media.pk)
        for index in range(limit):
            page = document[index]
            bitmap = page.render(scale=dpi / 72)
            try:
                yield bitmap.to_pil()
            finally:
                bitmap.close()
                page.close()
    finally:
        document.close()


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
