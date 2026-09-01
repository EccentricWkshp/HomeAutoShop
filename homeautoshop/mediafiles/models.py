"""
Media — photos and documents (SPEC §6.2, §7.9).

Media is append-only and originals are preserved byte-for-byte; derivatives are
always regenerable. A photo with no caption is a complete, valid record — the
capture must never be blocked on metadata.
"""

from __future__ import annotations

import hashlib
import pathlib

from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from homeautoshop.core.models import AppendOnlyModel, BaseModel, uuid7


def upload_to(instance: "Media", filename: str) -> str:
    stamp = timezone.now()
    return f"originals/{stamp:%Y/%m}/{instance.pk}/{filename}"


#: What a browser will render in an `<img>`. Deliberately narrower than what
#: we accept: HEIC is a fine original and an undrawable one, and SVG is left
#: out because it can carry script and these bytes are served inline from the
#: application's own origin.
BROWSER_IMAGE_MIMES = frozenset(
    {"image/jpeg", "image/png", "image/webp", "image/gif", "image/avif"}
)


class Media(AppendOnlyModel):
    class Kind(models.TextChoices):
        PHOTO = "photo", _("Photo")
        DOCUMENT = "document", _("Document")
        SCAN_EXPORT = "scan_export", _("Scan tool report")
        AUDIO_NOTE = "audio_note", _("Audio note")

    class OcrStatus(models.TextChoices):
        NOT_APPLICABLE = "n/a", _("Not applicable")
        PENDING = "pending", _("Pending")
        DONE = "done", _("Done")
        FAILED = "failed", _("Failed")

    kind = models.CharField(max_length=16, choices=Kind.choices, default=Kind.PHOTO)
    file = models.FileField(upload_to=upload_to, max_length=500)
    original_filename = models.CharField(max_length=255, blank=True)
    mime = models.CharField(max_length=100, blank=True)
    bytes = models.PositiveBigIntegerField(default=0)
    sha256 = models.CharField(max_length=64, db_index=True, blank=True)
    width = models.PositiveIntegerField(null=True, blank=True)
    height = models.PositiveIntegerField(null=True, blank=True)
    captured_at = models.DateTimeField(null=True, blank=True)
    thumb = models.ImageField(upload_to="derivatives/thumb/", null=True, blank=True, max_length=500)
    preview = models.ImageField(upload_to="derivatives/preview/", null=True, blank=True, max_length=500)
    derived_at = models.DateTimeField(null=True, blank=True)
    ocr_text = models.TextField(blank=True)
    ocr_status = models.CharField(max_length=10, choices=OcrStatus.choices, default=OcrStatus.NOT_APPLICABLE)
    gps_stripped = models.BooleanField(
        default=False, help_text=_("A photo of a car in a driveway geotags a home address.")
    )

    # The file itself is immutable; these are regenerable derivatives.
    server_writable_fields = frozenset(
        {
            "thumb",
            "preview",
            "width",
            "height",
            "captured_at",
            "gps_stripped",
            "derived_at",
            "ocr_text",
            "ocr_status",
        }
    )

    class Meta:
        ordering = ["-created_at"]
        # "medias" otherwise, which is nobody's plural of anything.
        verbose_name_plural = _("media files")

    def __str__(self) -> str:
        return self.original_filename or str(self.pk)

    @property
    def is_image(self) -> bool:
        return self.kind == self.Kind.PHOTO or self.mime.startswith("image/")

    @property
    def has_image_preview(self) -> bool:
        """Whether there is something a browser will actually draw.

        A different question from `is_image`, and the distinction is the whole
        bug: a PDF receipt is not an image and used to be handed to an `<img>`
        anyway, and a HEIC straight off a phone *is* an image that Chrome and
        Firefox refuse to draw. Both produced a broken-image icon where the
        receipt should have been.
        """
        if self.thumb or self.preview:
            return True
        return bool(self.file) and self.mime in BROWSER_IMAGE_MIMES

    @property
    def display_url(self) -> str:
        """A URL for an `<img>`, or empty when there is no picture to show.

        Empty rather than falling back to the original, so that a caller which
        forgets to check renders no image instead of a broken one, and so that
        a plain `{% if %}` in a template does the right thing without the
        template knowing any of this.
        """
        if not self.has_image_preview:
            return ""
        if self.thumb:
            return self.url_for("thumb")
        if self.preview:
            return self.url_for("preview")
        return self.url_for("original")

    @property
    def lightbox_url(self) -> str:
        """The version to enlarge in place, or empty for a file to open instead.

        The preview in preference to the original, and the difference is not
        small: originals off a phone are several megabytes each, previews are
        1600px on the long edge, and a lightbox that stalls on a garage's Wi-Fi
        is one nobody waits for. The original stays one click away inside it.

        A HEIC gets a URL here even though no browser will draw the original,
        because the preview `derive()` wrote is a JPEG. Empty until that job has
        run, which is the honest answer: there is nothing to enlarge yet.
        """
        if not self.file:
            return ""
        if self.preview:
            return self.url_for("preview")
        return self.url_for("original") if self.mime in BROWSER_IMAGE_MIMES else ""

    @property
    def opens_in_lightbox(self) -> bool:
        """Whether clicking this should enlarge it rather than open the file.

        Deliberately keyed on `is_image` rather than on having a picture to
        show. A PDF receipt has a picture — `derive()` renders its first page —
        and enlarging that page is the wrong answer to the click: somebody
        opening a receipt wants the document, scrollable and searchable, not a
        photograph of its first page with the other three unreachable.
        """
        return self.is_image and bool(self.lightbox_url)

    @property
    def extension_label(self) -> str:
        """A short badge for a file with no picture: `PDF`, `CSV`, `FILE`."""
        suffix = pathlib.Path(self.original_filename).suffix.lstrip(".")
        return (suffix or self.mime.rsplit("/", 1)[-1] or "file")[:4].upper()

    def url_for(self, variant: str = "original") -> str:
        """Where a browser should ask for this file.

        Never the storage backend's own URL. With object storage that is a
        presigned link signed against whatever address the application reaches
        the store at, which the browser frequently cannot — a broken image and
        a dead link on every page. See `mediafiles/views.py` for why the answer
        is a route here rather than a second published port.
        """
        from django.urls import reverse

        if not self.file:
            return ""
        if variant == "original":
            return reverse("media_file", args=[self.pk])
        return reverse("media_file_variant", args=[self.pk, variant])

    @staticmethod
    def hash_bytes(data: bytes) -> str:
        return hashlib.sha256(data).hexdigest()


class MediaLink(BaseModel):
    """Polymorphic attachment (SPEC §6.2).

    One document legitimately attaches to several places — a receipt belongs to
    both the purchase and the work order — so the link is its own row rather
    than a foreign key on media.
    """

    class Role(models.TextChoices):
        PRIMARY_PHOTO = "primary_photo", _("Primary photo")
        BEFORE = "before", _("Before")
        AFTER = "after", _("After")
        RECEIPT = "receipt", _("Receipt")
        MANUAL = "manual", _("Manual")
        TITLE = "title", _("Title")
        REGISTRATION = "registration", _("Registration")
        INSURANCE = "insurance", _("Insurance")
        OTHER = "other", _("Other")

    media = models.ForeignKey(Media, on_delete=models.CASCADE, related_name="links")
    entity_type = models.CharField(max_length=32, db_index=True)
    entity_id = models.UUIDField(db_index=True)
    role = models.CharField(max_length=20, choices=Role.choices, default=Role.OTHER)
    caption = models.CharField(max_length=255, blank=True)
    sort_order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["sort_order", "created_at"]
        indexes = [models.Index(fields=["entity_type", "entity_id"])]
        constraints = [
            models.UniqueConstraint(
                fields=["media", "entity_type", "entity_id", "role"],
                name="unique_media_link",
                condition=models.Q(deleted_at__isnull=True),
            )
        ]

    def __str__(self) -> str:
        return f"{self.media} -> {self.entity_type}:{self.entity_id}"

    @classmethod
    def for_entity(cls, entity, *, role: str | None = None):
        qs = cls.objects.filter(
            entity_type=entity.__class__.__name__, entity_id=entity.pk
        ).select_related("media")
        return qs.filter(role=role) if role else qs
