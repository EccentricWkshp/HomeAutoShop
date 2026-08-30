"""
Media — photos and documents (SPEC §6.2, §7.9).

Media is append-only and originals are preserved byte-for-byte; derivatives are
always regenerable. A photo with no caption is a complete, valid record — the
capture must never be blocked on metadata.
"""

from __future__ import annotations

import hashlib

from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from homeautoshop.core.models import AppendOnlyModel, BaseModel, uuid7


def upload_to(instance: "Media", filename: str) -> str:
    stamp = timezone.now()
    return f"originals/{stamp:%Y/%m}/{instance.pk}/{filename}"


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

    def __str__(self) -> str:
        return self.original_filename or str(self.pk)

    @property
    def is_image(self) -> bool:
        return self.kind == self.Kind.PHOTO or self.mime.startswith("image/")

    @property
    def display_url(self) -> str:
        """Thumbnail if derived, else the original. Never blocks on processing."""
        if self.thumb:
            return self.thumb.url
        return self.file.url if self.file else ""

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
