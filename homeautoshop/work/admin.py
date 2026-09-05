from django.contrib import admin

from homeautoshop.core.admin import SoftDeleteAdmin

from .models import (
    JobItem, JobItemTool, PartRequirement, ShopTool, TimeEntry, WorkOrder, WorkOrderNote,
)


class JobItemInline(admin.TabularInline):
    model = JobItem
    extra = 0


@admin.register(WorkOrder)
class WorkOrderAdmin(SoftDeleteAdmin):
    list_display = ("number", "title", "asset", "type", "status", "opened_at")
    list_filter = ("status", "type", "is_safety_critical")
    search_fields = ("number", "title", "complaint", "cause", "correction")
    inlines = [JobItemInline]
    readonly_fields = ("number", "revision")


@admin.register(JobItem)
class JobItemAdmin(SoftDeleteAdmin):
    list_display = ("work_order", "sequence", "title", "status", "assigned_to", "completed_at")
    list_filter = ("status",)
    search_fields = ("title", "description", "work_order__number")
    raw_id_fields = ("work_order", "assigned_to", "service_item")


@admin.register(PartRequirement)
class PartRequirementAdmin(SoftDeleteAdmin):
    list_display = ("work_order", "job_item", "part", "qty", "origin")
    list_filter = ("origin",)
    search_fields = ("part__name", "work_order__number")
    raw_id_fields = ("work_order", "job_item", "part")


@admin.register(TimeEntry)
class TimeEntryAdmin(SoftDeleteAdmin):
    list_display = ("work_order", "job_item", "user", "started_at", "ended_at", "minutes", "category")
    list_filter = ("category",)
    raw_id_fields = ("work_order", "job_item", "user")


@admin.register(ShopTool)
class ShopToolAdmin(SoftDeleteAdmin):
    list_display = ("tool_id", "name", "brand", "model", "lifecycle", "on_loan_to", "loan_due_on")
    list_filter = ("lifecycle",)
    search_fields = ("tool_id", "name", "brand", "model")


@admin.register(JobItemTool)
class JobItemToolAdmin(SoftDeleteAdmin):
    list_display = ("job_item", "tool")
    raw_id_fields = ("job_item", "tool")


@admin.register(WorkOrderNote)
class WorkOrderNoteAdmin(SoftDeleteAdmin):
    list_display = ("work_order", "author", "noted_at")
    search_fields = ("body",)
    raw_id_fields = ("work_order", "author")
