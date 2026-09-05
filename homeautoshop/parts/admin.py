from django.contrib import admin

from homeautoshop.core.admin import SoftDeleteAdmin

from .models import (
    Category, Location, Part, PartCrossRef, PartFitment, PartKitItem, PartUsage,
    StockLot, StockTransaction,
)


class CrossRefInline(admin.TabularInline):
    model = PartCrossRef
    extra = 0


class FitmentInline(admin.TabularInline):
    model = PartFitment
    extra = 0


class KitItemInline(admin.TabularInline):
    model = PartKitItem
    extra = 0
    fk_name = "kit"


@admin.register(Part)
class PartAdmin(SoftDeleteAdmin):
    list_display = ("name", "manufacturer", "part_number", "on_hand", "is_low")
    list_filter = ("categories", "part_type", "is_consumable", "has_core")
    search_fields = ("name", "manufacturer", "part_number", "cross_refs__value")
    inlines = [CrossRefInline, FitmentInline, KitItemInline]


@admin.register(StockLot)
class StockLotAdmin(SoftDeleteAdmin):
    """Where a purchase's remnants show up as inventory.

    A lot points at the `PurchaseLine` it was received against with `SET_NULL`,
    which only fires on a real DELETE — so a soft-deleted order leaves its lots
    on hand, at cost, still counted in inventory value. `purchase_line` is on
    the list so those lots can be found from here.
    """

    list_display = (
        "part", "location", "qty_on_hand", "unit_cost_minor",
        "purchase_line", "acquired_on", "expires_on",
    )
    list_filter = ("location",)
    search_fields = ("part__name", "part__part_number")
    raw_id_fields = ("part", "location", "purchase_line", "from_kit_lot")


@admin.register(StockTransaction)
class StockTransactionAdmin(SoftDeleteAdmin):
    list_display = ("created_at", "stock_lot", "delta", "reason", "work_order")
    list_filter = ("reason",)
    raw_id_fields = ("stock_lot", "work_order")

    def has_change_permission(self, request, obj=None):
        # The ledger is append-only; editing it would defeat its purpose.
        # Removing a row outright is still allowed, because clearing up after a
        # bad import has to be possible somewhere.
        return False


@admin.register(PartUsage)
class PartUsageAdmin(SoftDeleteAdmin):
    list_display = ("part", "work_order", "qty", "source", "installed_at", "core_returned")
    list_filter = ("source", "core_returned")
    raw_id_fields = ("part", "work_order")


@admin.register(PartCrossRef)
class PartCrossRefAdmin(SoftDeleteAdmin):
    list_display = ("part", "system", "value")
    list_filter = ("system",)
    search_fields = ("value", "part__name")
    raw_id_fields = ("part",)


@admin.register(PartFitment)
class PartFitmentAdmin(SoftDeleteAdmin):
    list_display = ("part", "asset", "confidence")
    list_filter = ("confidence",)
    search_fields = ("part__name", "asset__nickname")
    raw_id_fields = ("part", "asset")


@admin.register(PartKitItem)
class PartKitItemAdmin(SoftDeleteAdmin):
    list_display = ("kit", "part", "quantity")
    search_fields = ("kit__name", "part__name")
    raw_id_fields = ("kit", "part")


@admin.register(Location)
class LocationAdmin(SoftDeleteAdmin):
    list_display = ("name", "parent")
    search_fields = ("name",)
    raw_id_fields = ("parent",)


@admin.register(Category)
class CategoryAdmin(admin.ModelAdmin):
    list_display = ("name", "created_at")
    search_fields = ("name",)
