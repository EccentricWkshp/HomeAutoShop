from django.contrib import admin

from homeautoshop.core.admin import SoftDeleteAdmin

from .models import Inspection, InspectionPoint, InspectionResult, InspectionTemplate


class PointInline(admin.TabularInline):
    model = InspectionPoint
    extra = 0


@admin.register(InspectionTemplate)
class InspectionTemplateAdmin(SoftDeleteAdmin):
    list_display = ("name", "slug", "source", "version", "is_active")
    list_filter = ("source", "is_active")
    inlines = [PointInline]


@admin.register(Inspection)
class InspectionAdmin(SoftDeleteAdmin):
    list_display = ("template_name", "asset", "performed_on", "status", "overall", "performed_by")
    list_filter = ("status", "overall", "template_name")
    search_fields = ("asset__nickname", "template_name")
    # The snapshot is the whole point: it must not be editable after the fact.
    readonly_fields = ("points_snapshot", "template_version", "overall")


@admin.register(InspectionResult)
class InspectionResultAdmin(SoftDeleteAdmin):
    list_display = ("inspection", "name", "position", "status", "auto_status", "measured_value")
    list_filter = ("status", "status_overridden")


@admin.register(InspectionPoint)
class InspectionPointAdmin(SoftDeleteAdmin):
    list_display = (
        "template", "area", "sequence", "name", "result_type",
        "is_safety_critical", "is_optional",
    )
    list_filter = ("result_type", "is_safety_critical", "is_optional", "template")
    search_fields = ("name", "area", "translation_key")
    raw_id_fields = ("template",)
