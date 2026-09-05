from django.contrib import admin

from .models import (
    Location, Part, PartCrossRef, PartFitment, PartUsage, StockLot, StockTransaction,
)


class CrossRefInline(admin.TabularInline):
    model = PartCrossRef
    extra = 0


class FitmentInline(admin.TabularInline):
    model = PartFitment
    extra = 0


@admin.register(Part)
class PartAdmin(admin.ModelAdmin):
    list_display = ("name", "manufacturer", "part_number", "on_hand", "is_low")
    list_filter = ("categories", "part_type", "is_consumable", "has_core")
    search_fields = ("name", "manufacturer", "part_number", "cross_refs__value")
    inlines = [CrossRefInline, FitmentInline]


@admin.register(StockLot)
class StockLotAdmin(admin.ModelAdmin):
    list_display = ("part", "location", "qty_on_hand", "unit_cost_minor", "acquired_on", "expires_on")
    list_filter = ("location",)


@admin.register(StockTransaction)
class StockTransactionAdmin(admin.ModelAdmin):
    list_display = ("created_at", "stock_lot", "delta", "reason", "work_order")
    list_filter = ("reason",)

    def has_change_permission(self, request, obj=None):
        # The ledger is append-only; editing it would defeat its purpose.
        return False


@admin.register(PartUsage)
class PartUsageAdmin(admin.ModelAdmin):
    list_display = ("part", "work_order", "qty", "source", "installed_at", "core_returned")
    list_filter = ("source", "core_returned")


admin.site.register(Location)
