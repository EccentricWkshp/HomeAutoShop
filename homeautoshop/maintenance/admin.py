from django.contrib import admin

from homeautoshop.core.admin import SoftDeleteAdmin

from .models import (
    AssetComponent, AssetServiceItem, ScheduleTemplate, ServiceCompletion,
    ServiceDefinition, TemplateItem,
)


class TemplateItemInline(admin.TabularInline):
    model = TemplateItem
    extra = 0


@admin.register(ScheduleTemplate)
class ScheduleTemplateAdmin(SoftDeleteAdmin):
    list_display = ("name", "slug", "source", "is_active")
    list_filter = ("source", "is_active")
    inlines = [TemplateItemInline]


@admin.register(ServiceDefinition)
class ServiceDefinitionAdmin(SoftDeleteAdmin):
    list_display = (
        "name", "category", "severity",
        "default_interval_distance", "default_interval_months", "default_interval_hours",
    )
    list_filter = ("severity", "category")
    search_fields = ("name",)


@admin.register(AssetServiceItem)
class AssetServiceItemAdmin(SoftDeleteAdmin):
    list_display = ("definition", "asset", "status", "next_due_on", "next_due_usage", "last_done_on")
    list_filter = ("status", "definition__severity")
    search_fields = ("asset__nickname", "definition__name")
    readonly_fields = ("next_due_on", "next_due_usage", "status")


@admin.register(ServiceCompletion)
class ServiceCompletionAdmin(SoftDeleteAdmin):
    list_display = ("service_item", "completed_on", "usage", "is_backfill", "work_order")
    list_filter = ("is_backfill",)


@admin.register(AssetComponent)
class AssetComponentAdmin(SoftDeleteAdmin):
    list_display = (
        "asset", "component_type", "position", "installed_on",
        "removed_on", "serial_or_dot_code", "dot_verdict",
    )
    list_filter = ("component_type", "removal_reason")


@admin.register(TemplateItem)
class TemplateItemAdmin(SoftDeleteAdmin):
    list_display = (
        "template", "definition", "sequence",
        "interval_distance", "interval_unit", "interval_months", "interval_hours",
    )
    list_filter = ("interval_unit", "template")
    search_fields = ("template__name", "definition__name")
    raw_id_fields = ("template", "definition")
