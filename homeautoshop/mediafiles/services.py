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

#: Enough to fill the 1600px preview from a Letter page (150 dpi is about
#: 1275x1650), and a quarter of the pixels OCR needs at 300.
PREVIEW_DPI = 150

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


def _mark_derived(media: Media) -> None:
    """Record that derivation ran, whether or not it produced anything."""
    media.derived_at = timezone.now()
    media.save(update_fields=["derived_at", "updated_at"])


def _preview_source(media: Media):
    """The picture to make derivatives from, or None when there is not one.

    A PDF is a document with a picture inside it, and rasterising its first
    page turns a wall of identical file icons into receipts you can tell apart
    at a glance. Anything else that Pillow cannot open has no preview and says
    so by returning None — the original is still stored and still downloadable,
    it simply gets a labelled tile on the page instead of an `<img>`.
    """
    from PIL import Image

    if media.mime == "application/pdf":
        return _pdf_first_page(media)
    if not media.is_image:
        return None
    try:
        with media.file.open("rb") as fh:
            image = Image.open(fh)
            image.load()
        return image
    except Exception as exc:
        # A HEIC without pillow-heif, or a truncated upload. Not worth failing
        # the job over: nothing here is recoverable by retrying.
        log.warning("no preview for %s (%s): %s", media.pk, media.mime, exc)
        return None


def derive(media: Media) -> None:
    """Generate thumbnail and preview, and strip GPS EXIF. Idempotent."""

    if not media.file:
        _mark_derived(media)
        return

    try:
        from PIL import Image, ImageOps
    except ImportError:  # pragma: no cover - Pillow is a hard requirement
        log.warning("Pillow unavailable; skipping derivation for %s", media.pk)
        return

    image = _preview_source(media)
    if image is None:
        _mark_derived(media)
        return

    if media.is_image:
        # Honour the EXIF orientation tag, then discard EXIF entirely on the
        # derivatives. exif_transpose bakes the rotation into pixels, so
        # dropping the metadata afterwards cannot flip the image.
        image = ImageOps.exif_transpose(image)
        # Only for a real image: for a PDF these would be the size of the
        # bitmap we just rendered, which says nothing about the document.
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
            # Rotated and flattened before reading, like every other OCR path
            # here. A sideways photograph is not merely read badly, it is read
            # as nothing — so a receipt photographed on a bench used to be
            # filed with an empty `ocr_text` and a `done` status, which is a
            # document that quietly cannot be searched for. The same picture
            # has to read the same way whether it arrived as a document or as
            # a scan report; two answers to what one photograph says is the
            # disagreement this codebase keeps having to undo.
            text = pytesseract.image_to_string(
                _prepared(image, flatten=True), lang=_ocr_language()
            )
    except Exception as exc:
        log.warning("OCR failed for %s: %s", media.pk, exc)
        media.ocr_status = Media.OcrStatus.FAILED
        media.save(update_fields=["ocr_status", "updated_at"])
        return

    media.ocr_text = text.strip()[:200_000]
    media.ocr_status = Media.OcrStatus.DONE
    media.save(update_fields=["ocr_text", "ocr_status", "updated_at"])


#: Beyond this, an image is scaled down before OCR. Tesseract gets slower than
#: linearly with pixel count and reads no better above roughly 300 dpi of
#: document, which a modern phone passes several times over.
OCR_MAX_PIXELS = 4000


def upright(image):
    """Rotate an image the way its own metadata says to display it.

    **Mandatory, not a nicety.** A phone held sideways writes the pixels in
    sensor order and records the rotation as EXIF orientation, and Pillow's
    `Image.open` hands back the sensor order. Every photograph of a scan-tool
    printout in the sample corpus is orientation 6 — a receipt lying on a
    bench, photographed by somebody standing beside it — and OCR of a page
    rotated ninety degrees does not return poor text, it returns *no* text.
    So the one format that most needed reading was the one that could not be
    read at all, and nothing said why.

    `exif_transpose` also drops the orientation tag it has just honoured, so
    this cannot be applied twice.
    """
    from PIL import ImageOps

    try:
        return ImageOps.exif_transpose(image) or image
    except Exception:  # noqa: BLE001 - a corrupt EXIF block is not a reason to fail
        return image


#: How wide a blur has to be to hold the *lighting* and none of the text.
#: A fraction of the picture rather than a constant: the same forty pixels that
#: separate a bench shadow from a printed digit at two megapixels is inside a
#: single glyph at twelve.
FLAT_FIELD_DIVISOR = 36

#: Percent of the histogram discarded at each end before the contrast is
#: stretched. Without it a single blown-out highlight sets the white point for
#: the whole page.
AUTOCONTRAST_CUTOFF = 2


def flatten_lighting(grey):
    """Give every part of the page its own black and white points.

    Subtracting a heavily blurred copy of the picture from itself leaves what
    differs from its own surroundings, which is the ink — and removes what
    varies slowly across the frame, which is the light. Global autocontrast
    cannot do this by construction: it has one curve for the whole image, so a
    strip of paper that is bright at the top and shadowed at the bottom gets a
    curve that is wrong at both ends.

    **This is the difference between reading these reports and not.** Measured
    over the five BT600 Plus photographs against the values a person can read
    off the paper: 73 of 86 without it, 84 with. The seven that appear only
    with it are the whole second half of `20260830_105647` — a charging test
    that lay in the shadow of the photographer.

    Nothing is thresholded. Thin thermal glyphs and a one-pixel graph trace are
    the first things a threshold eats, and the trace is what tells a reviewer
    they are looking at the right receipt.
    """
    from PIL import ImageChops, ImageFilter, ImageOps

    radius = max(12, round(min(grey.size) / FLAT_FIELD_DIVISOR))
    background = grey.filter(ImageFilter.GaussianBlur(radius=radius))
    return ImageOps.invert(ImageChops.subtract(background, grey))


def image_size(raw: bytes) -> tuple[int, int]:
    """How big a picture is once it is the right way up.

    Reads the header only — Pillow does not decode pixels for `.size` — so this
    is cheap enough to ask alongside OCR rather than threading the answer back
    out of it. `(0, 0)` where the bytes are not an image this build can open,
    which a caller treats as "no frame", not as a frame of no size.
    """
    from PIL import Image

    try:
        image = Image.open(BytesIO(raw))
        width, height = image.size
        if (image.getexif().get(0x0112) or 1) in (5, 6, 7, 8):
            width, height = height, width
        if max(width, height) > OCR_MAX_PIXELS:
            scale = OCR_MAX_PIXELS / max(width, height)
            width, height = int(width * scale), int(height * scale)
        return width, height
    except Exception:  # noqa: BLE001
        return (0, 0)


def _prepared(image, *, flatten: bool = False):
    """An image as OCR wants to see it.

    Rotation and the size cap apply to everything, because both are about the
    pixels being wrong rather than about what is in them. The rest is asked for
    by the caller: it is what a photograph of paper needs and a page rendered
    out of a PDF does not, being already evenly lit by construction.

    Deliberately **not** here: upscaling and sharpening, both of which were
    tried and both of which made these five worse — twice the pixels scored 55
    of 86 against 66 at native size, because interpolation softens a thermal
    glyph that was already only two pixels wide. And cropping to the paper,
    which scored exactly what flattening alone scores and adds a way to fail:
    a bright window behind the bench is a brighter rectangle than the receipt.
    """
    from PIL import ImageOps

    image = upright(image)
    if max(image.size) > OCR_MAX_PIXELS:
        image.thumbnail((OCR_MAX_PIXELS, OCR_MAX_PIXELS))
    if flatten:
        image = ImageOps.autocontrast(
            flatten_lighting(image.convert("L")), cutoff=AUTOCONTRAST_CUTOFF
        )
    return image


def read_image_text(raw: bytes) -> str:
    """OCR an image already in memory (§7.9).

    Split out from `ocr()` because the scan-report import has bytes and a
    person waiting, not a `Media` row and a queue. Returns empty rather than
    raising: a report that could not be read still becomes a session the
    operator can map by hand (FR-INT-6), and that is a better outcome than an
    error page.

    Reads the words and joins them, rather than asking for a string, so that
    the text a profile is scored against and the geometry a built-in parser
    reads are the *same reading* of the *same picture*. They were not: the
    scored text came from `image_to_string` and there was no geometry at all.
    """
    pages = read_image_words(raw)
    if not pages:
        return ""
    from homeautoshop.diagnostics.engine import lines_from_words

    return "\n".join(lines_from_words(pages))


#: Tesseract page-segmentation modes, tried in this order, and the order was
#: measured rather than reasoned. 6 — "a single uniform block of text" — is the
#: obvious choice for a receipt and is the *worst* of the three on these
#: photographs (77 of 86), because a receipt photographed on a bench is not a
#: uniform block: it is a strip of paper with two bar graphs, a voltage trace
#: and a wooden table around it. 3, full automatic segmentation, handles that
#: (84). 11, sparse text, is the fallback for a page 3 finds no structure in.
SEGMENTATION_MODES = (3, 11)

#: Confidence at or above which a word counts as read rather than guessed at,
#: for the purpose of choosing between two passes.
USABLE_CONFIDENCE = 60

# Per-*character* confidence was tried here and is deliberately not used.
# Tesseract will report it — `-c lstm_choice_mode=2` with hOCR output, at no
# extra cost, returning the same words in the same order as `image_to_data` —
# and on a damaged glyph the character stream does show a run of low-confidence
# marks that never reach the decoded word. It looked like exactly the signal
# for "this value is printed over something".
#
# It is not. On one BT600 Plus photograph it marks 21 of 41 words, including
# `79%`, `100%` and `SN:`, every one of which was read perfectly; what it is
# actually detecting is the texture of photographed thermal paper. A review
# screen that flags half of what it shows teaches the reader to stop looking,
# which is worse than flagging nothing. See SPEC §19.

#: Enough confident words to be a report rather than a caption. Below this the
#: second segmentation mode is worth what it costs.
ENOUGH_WORDS = 20


def read_image_words(raw: bytes) -> list[list[dict]]:
    """OCR an image and keep where every word was.

    One page, because an image is one page. Each word carries its box and
    Tesseract's own confidence for it, and both are load-bearing further on: a
    parser groups words into printed lines by position, and a review screen
    shows the crop of the paper a doubtful value came off. All of that used to
    be thrown away the moment `image_to_string` returned.

    Two passes at most, and the better one wins. "Better" is measured by how
    much of the recognized text a caller could use, not by Tesseract's own
    average confidence, which rewards a pass that found three clean words and
    missed the report.
    """
    if not conf.OCR_ENABLED:
        return []
    try:
        import pytesseract
        from PIL import Image
    except ImportError:
        log.info("OCR skipped: pytesseract is not installed")
        return []

    try:
        image = Image.open(BytesIO(raw))
        image.load()
        image = _prepared(image, flatten=True)
    except Exception as exc:  # noqa: BLE001
        log.warning("could not open an image for OCR: %s", exc)
        return []

    best: list[dict] = []
    best_score = -1
    for mode in SEGMENTATION_MODES:
        try:
            data = pytesseract.image_to_data(
                image,
                lang=_ocr_language(),
                config=f"--psm {mode}",
                output_type=pytesseract.Output.DICT,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("could not read words from an image: %s", exc)
            return []
        words = _words_from(data)
        # Confident words, not Tesseract's mean confidence. The mean rewards a
        # pass that found three clean words and missed the report, which is
        # exactly the pass a fallback exists to replace.
        confident = sum(1 for word in words if word["conf"] >= USABLE_CONFIDENCE)
        if confident > best_score:
            best, best_score = words, confident
        if best_score >= ENOUGH_WORDS:
            break
    return [best] if best else []


def _words_from(data: dict) -> list[dict]:
    out: list[dict] = []
    for index, text in enumerate(data.get("text") or []):
        stripped = (text or "").strip()
        if not stripped:
            continue
        try:
            confidence = float(data["conf"][index])
        except (KeyError, IndexError, TypeError, ValueError):
            confidence = -1.0
        left, top = float(data["left"][index]), float(data["top"][index])
        out.append(
            {
                "text": stripped,
                "x0": round(left, 1),
                "x1": round(left + float(data["width"][index]), 1),
                "top": round(top, 1),
                "bottom": round(top + float(data["height"][index]), 1),
                "conf": round(confidence, 1),
                # Tesseract's own idea of which printed line this is. Carried
                # for a person reading the capture; nothing here relies on it,
                # because its line breaks on a curled receipt are its weakest
                # answer and the geometry is right there.
                "line": int(data.get("line_num", [0] * (index + 1))[index] or 0),
            }
        )
    return out


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


def _pdf_first_page(media: Media, *, dpi: int = PREVIEW_DPI):
    """Rasterise page one of a PDF, to use as its thumbnail.

    Written out rather than calling `next()` on `_pdf_pages`: that generator
    closes each bitmap as soon as its consumer is finished, and the PIL image
    it yields borrows that buffer. Pulling one page out of it and using the
    image afterwards would be reading freed memory. Here the copy is taken
    before anything is closed.
    """
    try:
        import pypdfium2
    except ImportError:
        log.info("no preview for PDF %s: pypdfium2 not installed", media.pk)
        return None

    try:
        with media.file.open("rb") as fh:
            data = fh.read()
        document = pypdfium2.PdfDocument(data)
        try:
            if not len(document):
                return None
            page = document[0]
            bitmap = page.render(scale=dpi / 72)
            try:
                return bitmap.to_pil().copy()
            finally:
                bitmap.close()
                page.close()
        finally:
            document.close()
    except Exception as exc:
        # Encrypted, or malformed. Still stored, still downloadable, no picture.
        log.warning("could not render PDF %s: %s", media.pk, exc)
        return None


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
