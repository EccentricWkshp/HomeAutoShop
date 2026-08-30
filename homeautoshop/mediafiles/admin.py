from django.contrib import admin

from .models import Media, MediaLink


@admin.register(Media)
class MediaAdmin(admin.ModelAdmin):
    list_display = ("original_filename", "kind", "mime", "bytes", "derived_at", "created_at")
    list_filter = ("kind", "ocr_status")
    readonly_fields = ("sha256", "bytes", "width", "height", "derived_at")


admin.site.register(MediaLink)
