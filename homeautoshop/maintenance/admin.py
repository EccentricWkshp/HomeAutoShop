from django.contrib import admin

from .models import (
    AssetComponent, AssetServiceItem, ScheduleTemplate, ServiceCompletion,
    ServiceDefinition, TemplateItem,
)


class TemplateItemInline(admin.TabularInline):
    model = TemplateItem
    extra = 0


@admin.register(ScheduleTemplate)
class ScheduleTemplateAdmin(admin.ModelAdmin):
    list_display = ("name", "slug", "source", "is_active")
    list_filter = ("source", "is_active")
    inlines = [TemplateItemInline]


@admin.register(ServiceDefinition)
class ServiceDefinitionAdmin(admin.ModelAdmin):
    list_display = (
        "name", "category", "severity",
        "default_interval_distance", "default_interval_months", "default_interval_hours",
    )
    list_filter = ("severity", "category")
    search_fields = ("name",)


@admin.register(AssetServiceItem)
class AssetServiceItemAdmin(admin.ModelAdmin):
    list_display = ("definition", "asset", "status", "next_due_on", "next_due_usage", "last_done_on")
    list_filter = ("status", "definition__severity")
    search_fields = ("asset__nickname", "definition__name")
    readonly_fields = ("next_due_on", "next_due_usage", "status")


@admin.register(ServiceCompletion)
class ServiceCompletionAdmin(admin.ModelAdmin):
    list_display = ("service_item", "completed_on", "usage", "is_backfill", "work_order")
    list_filter = ("is_backfill",)


@admin.register(AssetComponent)
class AssetComponentAdmin(admin.ModelAdmin):
    list_display = (
        "asset", "component_type", "position", "installed_on",
        "removed_on", "serial_or_dot_code", "dot_verdict",
    )
    list_filter = ("component_type", "removal_reason")
