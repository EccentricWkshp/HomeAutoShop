from django.contrib import admin

from .models import Expense, Purchase, PurchaseLine, Vendor


class LineInline(admin.TabularInline):
    model = PurchaseLine
    extra = 0


@admin.register(Purchase)
class PurchaseAdmin(admin.ModelAdmin):
    list_display = ("vendor", "order_number", "status", "ordered_on", "received_on", "total_minor")
    list_filter = ("status", "vendor")
    inlines = [LineInline]


@admin.register(Vendor)
class VendorAdmin(admin.ModelAdmin):
    list_display = ("name", "type", "return_window_days")
    search_fields = ("name",)


@admin.register(Expense)
class ExpenseAdmin(admin.ModelAdmin):
    list_display = ("incurred_on", "category", "amount_minor", "asset", "work_order", "vendor")
    list_filter = ("category",)
