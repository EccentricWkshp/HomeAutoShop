from django.contrib import admin

from .models import Inspection, InspectionPoint, InspectionResult, InspectionTemplate


class PointInline(admin.TabularInline):
    model = InspectionPoint
    extra = 0


@admin.register(InspectionTemplate)
class InspectionTemplateAdmin(admin.ModelAdmin):
    list_display = ("name", "slug", "source", "version", "is_active")
    list_filter = ("source", "is_active")
    inlines = [PointInline]


@admin.register(Inspection)
class InspectionAdmin(admin.ModelAdmin):
    list_display = ("template_name", "asset", "performed_on", "status", "overall", "performed_by")
    list_filter = ("status", "overall", "template_name")
    search_fields = ("asset__nickname", "template_name")
    # The snapshot is the whole point: it must not be editable after the fact.
    readonly_fields = ("points_snapshot", "template_version", "overall")


@admin.register(InspectionResult)
class InspectionResultAdmin(admin.ModelAdmin):
    list_display = ("inspection", "name", "position", "status", "auto_status", "measured_value")
    list_filter = ("status", "status_overridden")
