from django.contrib import admin

from .models import (
    Asset, AssetOwnership, AssetServiceInfoLink, AssetSpec, Recall,
    ServiceInfoProvider, UsageReading,
)


class OwnershipInline(admin.TabularInline):
    model = AssetOwnership
    extra = 0


@admin.register(Asset)
class AssetAdmin(admin.ModelAdmin):
    list_display = ("nickname", "asset_kind", "descriptor", "status", "masked_vin", "revision")
    list_filter = ("asset_kind", "status", "vehicle_class")
    search_fields = ("nickname", "vin", "plate", "make", "model", "serial_number")
    inlines = [OwnershipInline]
    readonly_fields = ("revision", "decoded_at", "decoded_raw", "field_overrides")


@admin.register(UsageReading)
class UsageReadingAdmin(admin.ModelAdmin):
    list_display = ("asset", "value", "unit", "read_on", "source", "is_rollback")
    list_filter = ("source", "is_rollback", "meter")


@admin.register(ServiceInfoProvider)
class ServiceInfoProviderAdmin(admin.ModelAdmin):
    list_display = ("name", "slug", "access", "is_enabled", "sort_order")


admin.site.register(AssetServiceInfoLink)


@admin.register(AssetSpec)
class AssetSpecAdmin(admin.ModelAdmin):
    list_display = ("asset", "group", "name", "display_value", "is_pinned", "is_sensitive")
    list_filter = ("group", "is_sensitive", "is_pinned")
    search_fields = ("name", "value", "asset__nickname")


@admin.register(Recall)
class RecallAdmin(admin.ModelAdmin):
    list_display = ("campaign_number", "asset", "component", "owner_status", "reported_on")
    list_filter = ("owner_status", "source")
    search_fields = ("campaign_number", "component", "asset__nickname")
