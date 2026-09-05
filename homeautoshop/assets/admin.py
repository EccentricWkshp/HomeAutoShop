from django.contrib import admin

from homeautoshop.core.admin import SoftDeleteAdmin

from .models import (
    Asset, AssetCardPreference, AssetLink, AssetOwnership, AssetServiceInfoLink,
    AssetSpec, Recall, ServiceInfoProvider, UsageReading,
)


class OwnershipInline(admin.TabularInline):
    model = AssetOwnership
    extra = 0


@admin.register(Asset)
class AssetAdmin(SoftDeleteAdmin):
    list_display = ("nickname", "asset_kind", "descriptor", "status", "masked_vin", "revision")
    list_filter = ("asset_kind", "status", "vehicle_class")
    search_fields = ("nickname", "vin", "plate", "make", "model", "serial_number")
    inlines = [OwnershipInline]
    readonly_fields = ("revision", "decoded_at", "decoded_raw", "field_overrides")


@admin.register(UsageReading)
class UsageReadingAdmin(SoftDeleteAdmin):
    list_display = ("asset", "value", "unit", "read_on", "source", "is_rollback")
    list_filter = ("source", "is_rollback", "meter")


@admin.register(ServiceInfoProvider)
class ServiceInfoProviderAdmin(SoftDeleteAdmin):
    list_display = ("name", "slug", "access", "is_enabled", "sort_order")


@admin.register(AssetServiceInfoLink)
class AssetServiceInfoLinkAdmin(SoftDeleteAdmin):
    list_display = ("asset", "provider", "label", "is_hidden", "subscription_status")
    list_filter = ("is_hidden", "subscription_status")
    raw_id_fields = ("asset", "provider")


@admin.register(AssetSpec)
class AssetSpecAdmin(SoftDeleteAdmin):
    list_display = ("asset", "group", "name", "display_value", "is_pinned", "is_sensitive")
    list_filter = ("group", "is_sensitive", "is_pinned")
    search_fields = ("name", "value", "asset__nickname")


@admin.register(Recall)
class RecallAdmin(SoftDeleteAdmin):
    list_display = ("campaign_number", "asset", "component", "owner_status", "reported_on")
    list_filter = ("owner_status", "source")
    search_fields = ("campaign_number", "component", "asset__nickname")


@admin.register(AssetOwnership)
class AssetOwnershipAdmin(SoftDeleteAdmin):
    list_display = ("asset", "person", "role", "from_date", "to_date")
    list_filter = ("role",)
    raw_id_fields = ("asset", "person")


@admin.register(AssetLink)
class AssetLinkAdmin(SoftDeleteAdmin):
    list_display = ("asset", "label", "url")
    search_fields = ("label", "url")
    raw_id_fields = ("asset",)


@admin.register(AssetCardPreference)
class AssetCardPreferenceAdmin(admin.ModelAdmin):
    list_display = ("user", "asset", "board_order", "color", "updated_at")
    raw_id_fields = ("user", "asset")
