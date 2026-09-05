from django.contrib import admin, messages
from django.utils.translation import gettext_lazy as _

from homeautoshop.core.admin import SoftDeleteAdmin

from .models import Expense, Purchase, PurchaseLine, Vendor


class LineInline(admin.TabularInline):
    model = PurchaseLine
    extra = 0

    def get_queryset(self, request):
        # `all_objects`, to match the changelist. An inline reading the alive
        # manager on a trashed purchase shows an empty order that is not empty.
        return PurchaseLine.all_objects.get_queryset()


@admin.register(Purchase)
class PurchaseAdmin(SoftDeleteAdmin):
    """Orders, including the trashed ones.

    Deleting from here is **not** the same as deleting from the purchase screen.
    `purchase_delete` refuses while any line is received, because a stock lot's
    landed cost points back through its line at this order; the admin has no
    such guard, so `delete_selected` here can put an order in the trash with
    received lines still hanging off it. `warn_if_received` says so rather than
    leaving it to be discovered by an importer months later.
    """

    list_display = (
        "vendor", "order_number", "status", "ordered_on", "received_on", "total_minor",
        "line_count", "received_lines",
    )
    list_filter = ("status", "vendor")
    search_fields = ("order_number", "vendor__name", "notes")
    inlines = [LineInline]

    @admin.display(description=_("lines"))
    def line_count(self, obj) -> int:
        return obj.lines(manager="all_objects").count()

    @admin.display(description=_("received lines"))
    def received_lines(self, obj) -> int:
        return obj.lines(manager="all_objects").filter(qty_received__gt=0).count()

    def delete_queryset(self, request, queryset):
        self._warn_if_received(request, queryset)
        super().delete_queryset(request, queryset)

    def delete_model(self, request, obj):
        self._warn_if_received(request, [obj])
        super().delete_model(request, obj)

    def _warn_if_received(self, request, purchases) -> None:
        stuck = [
            purchase
            for purchase in purchases
            if purchase.lines(manager="all_objects").filter(qty_received__gt=0).exists()
        ]
        if not stuck:
            return
        self.message_user(
            request,
            _(
                "%(n)s of these had received lines. The stock those receipts created is "
                "still on the shelf and still points at this order for its cost — "
                "un-receive it, or the cost rollups that read through it go quietly wrong."
            )
            % {"n": len(stuck)},
            level=messages.WARNING,
        )


@admin.register(PurchaseLine)
class PurchaseLineAdmin(SoftDeleteAdmin):
    """Reachable on its own, which it was not before.

    A line used to be visible only through the inline on its purchase, so a
    purchase in the trash took its lines out of every view in the application
    while leaving them alive in the database — the state that makes an order
    impossible to re-read.
    """

    list_display = (
        "purchase", "description_as_ordered", "part",
        "qty_ordered", "qty_received", "extended_minor",
    )
    list_filter = ("purchase__vendor",)
    search_fields = ("description_as_ordered", "purchase__order_number", "part__part_number")
    raw_id_fields = ("purchase", "part")


@admin.register(Vendor)
class VendorAdmin(SoftDeleteAdmin):
    list_display = ("name", "type", "return_window_days")
    search_fields = ("name",)


@admin.register(Expense)
class ExpenseAdmin(SoftDeleteAdmin):
    list_display = ("incurred_on", "category", "amount_minor", "asset", "work_order", "vendor")
    list_filter = ("category",)
    search_fields = ("description",)
