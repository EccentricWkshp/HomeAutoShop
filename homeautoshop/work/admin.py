from django.contrib import admin

from .models import JobItem, WorkOrder, WorkOrderNote


class JobItemInline(admin.TabularInline):
    model = JobItem
    extra = 0


@admin.register(WorkOrder)
class WorkOrderAdmin(admin.ModelAdmin):
    list_display = ("number", "title", "asset", "type", "status", "opened_at")
    list_filter = ("status", "type", "is_safety_critical")
    search_fields = ("number", "title", "complaint", "cause", "correction")
    inlines = [JobItemInline]
    readonly_fields = ("number", "revision")


admin.site.register(WorkOrderNote)
