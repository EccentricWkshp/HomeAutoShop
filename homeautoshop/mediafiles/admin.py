from django.contrib import admin

from homeautoshop.core.admin import SoftDeleteAdmin

from .models import Media, MediaLink


@admin.register(Media)
class MediaAdmin(SoftDeleteAdmin):
    list_display = ("original_filename", "kind", "mime", "bytes", "sha256", "created_at")
    list_filter = ("kind", "ocr_status", "gps_stripped")
    search_fields = ("original_filename", "sha256", "ocr_text")
    readonly_fields = ("sha256", "bytes", "width", "height", "derived_at")


@admin.register(MediaLink)
class MediaLinkAdmin(SoftDeleteAdmin):
    """The generic join, which nothing else can follow backwards.

    `entity_type`/`entity_id` is a loose reference rather than a foreign key, so
    no cascade reaches it and no related manager lists it. When the thing it
    points at is deleted the link stays, which is exactly the kind of leftover
    this page exists to let somebody find — search by the entity's id.
    """

    list_display = ("media", "entity_type", "entity_id", "role", "sort_order")
    list_filter = ("entity_type", "role")
    search_fields = ("entity_id", "caption")
    raw_id_fields = ("media",)
