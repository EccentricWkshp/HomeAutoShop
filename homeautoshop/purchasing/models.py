"""
Vendors, purchases, and non-part costs (SPEC §6.2, §7.5, §7.6).

Receiving is where most of the value is: it turns an order into stock at the
*landed* cost, opens the return window, and starts the core clock. Getting that
one transition right is what makes the cost reports true.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from homeautoshop.core.measurements import Money
from homeautoshop.core.models import BaseModel, RevisionedModel
from homeautoshop.core.money import money, money_columns
from homeautoshop.core.runtime import conf


class Vendor(RevisionedModel):
    class Type(models.TextChoices):
        ONLINE = "online", _("Online")
        LOCAL = "local_store", _("Local store")
        DEALER = "dealer", _("Dealer")
        SALVAGE = "salvage", _("Salvage yard")
        MACHINE_SHOP = "machine_shop", _("Machine shop")
        INDIVIDUAL = "individual", _("Individual")

    name = models.CharField(max_length=120)
    type = models.CharField(max_length=16, choices=Type.choices, default=Type.ONLINE)
    url = models.URLField(blank=True)
    account_number = models.CharField(max_length=64, blank=True)
    phone = models.CharField(max_length=40, blank=True)
    return_window_days = models.PositiveIntegerField(
        null=True, blank=True, help_text=_("Used to warn before the window closes.")
    )
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name


class PurchaseStatus(models.TextChoices):
    CART = "cart", _("Cart")
    ORDERED = "ordered", _("Ordered")
    PARTIAL = "partial", _("Partially received")
    RECEIVED = "received", _("Received")
    RETURNED = "returned", _("Returned")
    # The stored value keeps the British spelling it was created with: it is
    # a column value in every existing row, and renaming it would need a data
    # migration to change nothing anybody sees. Only the label is displayed.
    CANCELLED = "cancelled", _("Canceled")


class Purchase(RevisionedModel):
    vendor = models.ForeignKey(Vendor, on_delete=models.PROTECT, related_name="purchases")
    order_number = models.CharField(max_length=64, blank=True)
    status = models.CharField(max_length=12, choices=PurchaseStatus.choices, default=PurchaseStatus.ORDERED)
    ordered_on = models.DateField(default=timezone.localdate)
    received_on = models.DateField(null=True, blank=True)
    work_order = models.ForeignKey(
        "work.WorkOrder", null=True, blank=True, on_delete=models.SET_NULL, related_name="purchases",
        help_text=_("Set when this was bought for a specific job; drives the blocked list."),
    )

    tax_minor, tax_currency = money_columns("tax")
    shipping_minor, shipping_currency = money_columns("shipping")
    discount_minor, discount_currency = money_columns("discount")
    payment_method = models.CharField(max_length=40, blank=True)
    notes = models.TextField(blank=True)

    tax = money("tax")
    shipping = money("shipping")
    discount = money("discount")

    class Meta:
        ordering = ["-ordered_on"]

    def __str__(self) -> str:
        return f"{self.vendor} {self.order_number or self.ordered_on}"

    @property
    def currency(self) -> str:
        return self.tax_currency or "USD"

    @property
    def subtotal_minor(self) -> int:
        return sum(line.line_total_minor for line in self.lines.all())

    @property
    def subtotal(self):
        return Money(self.subtotal_minor, self.currency)

    @property
    def total(self):
        """The same number the screens were printing in cents.

        `total_minor` is the storage form (§5.5); templates rendered it raw, so
        a $155.87 order displayed as `15587`.
        """
        return Money(self.total_minor, self.currency)

    @property
    def total_minor(self) -> int:
        return (
            self.subtotal_minor
            + (self.tax_minor or 0)
            + (self.shipping_minor or 0)
            - (self.discount_minor or 0)
        )

    @property
    def return_by(self):
        """FR-PUR-5 — the window runs from receipt, not from the order."""
        if not self.received_on or not self.vendor.return_window_days:
            return None
        return self.received_on + timedelta(days=self.vendor.return_window_days)

    @property
    def return_window_closing(self) -> bool:
        deadline = self.return_by
        return bool(deadline and 0 <= (deadline - timezone.localdate()).days <= 7)

    def recompute_status(self) -> None:
        lines = list(self.lines.all())
        if not lines or self.status in (PurchaseStatus.RETURNED, PurchaseStatus.CANCELLED):
            return
        received = sum(1 for line in lines if line.qty_received >= line.qty_ordered)
        partial = any(line.qty_received > 0 for line in lines)
        if received == len(lines):
            self.status = PurchaseStatus.RECEIVED
            self.received_on = self.received_on or timezone.localdate()
        elif partial:
            self.status = PurchaseStatus.PARTIAL
        else:
            # Back down, which this used to have no way of doing: receiving
            # only ever ratcheted the status upward, so a receipt taken back
            # left a purchase reading "Received" with nothing received on it,
            # and a return window counting down from a date that no longer
            # meant anything.
            self.status = PurchaseStatus.ORDERED
            self.received_on = None
        self.save()


class PurchaseLine(BaseModel):
    purchase = models.ForeignKey(Purchase, on_delete=models.CASCADE, related_name="lines")
    part = models.ForeignKey(
        "parts.Part", null=True, blank=True, on_delete=models.SET_NULL, related_name="purchase_lines"
    )
    description_as_ordered = models.CharField(max_length=200, blank=True)
    qty_ordered = models.DecimalField(max_digits=12, decimal_places=3, default=1)
    qty_received = models.DecimalField(max_digits=12, decimal_places=3, default=0)
    unit_price_minor, unit_price_currency = money_columns("unit_price")
    core_charge_minor, core_charge_currency = money_columns("core_charge", default=0)

    unit_price = money("unit_price")
    core_charge = money("core_charge")

    class Meta:
        ordering = ["created_at"]

    def __str__(self) -> str:
        return self.description_as_ordered or (str(self.part) if self.part else "line")

    @property
    def line_total_minor(self) -> int:
        return int(
            (Decimal(self.unit_price_minor or 0) * Decimal(str(self.qty_ordered)))
            + Decimal(self.core_charge_minor or 0)
        )

    @property
    def outstanding(self) -> Decimal:
        return Decimal(str(self.qty_ordered)) - Decimal(str(self.qty_received))

    @transaction.atomic
    def receive(self, qty=None, *, location=None, user=None, allocate_overheads: bool = True):
        """Receive stock at landed cost (FR-PUR-2/3).

        Tax and shipping are spread across lines in proportion to their value,
        because a $4 gasket that shipped in a $30 box did not cost $4.
        """
        from homeautoshop.parts.models import StockLot, StockTransaction

        if self.part is None:
            raise ValidationError(_("Link this line to a part before receiving it."))

        qty = Decimal(str(qty if qty is not None else self.outstanding))
        if qty <= 0:
            raise ValidationError(_("Nothing left to receive on this line."))
        if qty > self.outstanding:
            raise ValidationError(_("That is more than was ordered."))

        unit_cost = Decimal(self.unit_price_minor or 0)
        if allocate_overheads:
            unit_cost += self._overhead_per_unit()

        lot = StockLot.objects.create(
            part=self.part,
            location=location,
            qty_on_hand=0,
            unit_cost_minor=int(unit_cost),
            unit_cost_currency=self.unit_price_currency or "USD",
            purchase_line=self,
            acquired_on=timezone.localdate(),
            created_by=user if getattr(user, "pk", None) else None,
        )
        StockTransaction.record(
            lot, qty, StockTransaction.Reason.RECEIVE, note=str(self.purchase), user=user
        )

        self.qty_received = Decimal(str(self.qty_received)) + qty
        self.save()
        self.purchase.recompute_status()
        return lot

    @transaction.atomic
    def unreceive(self, qty=None, *, user=None):
        """Take back a receipt that should not have been recorded.

        Nothing is deleted. `StockTransaction` is the append-only ledger that
        `qty_on_hand` is projected from (FR-INV-1), so undoing a receipt means
        writing the opposite movement, not erasing the first one — a shelf that
        disagrees with the book has to be explainable, and an erased row
        explains nothing.

        **It refuses rather than going negative.** Stock received and then used
        on a job is gone: the parts are in a car. Reversing the paperwork would
        leave a lot holding minus two of something, every cost rollup drawing
        from it wrong, and no record of which of the two facts was the mistake.
        So the reversal is capped at what is still on the shelf from this line's
        own lots, and asks for a return or a scrap instead when that is short.
        """
        from homeautoshop.parts.models import StockTransaction

        received = Decimal(str(self.qty_received))
        qty = Decimal(str(qty if qty is not None else received))
        if qty <= 0:
            raise ValidationError(_("Nothing to take back on this line."))
        if qty > received:
            raise ValidationError(_("That is more than was received."))

        # Newest lot first: the last receipt is overwhelmingly the one being
        # corrected, and taking it from the oldest would leave the shelf's FIFO
        # order describing a receipt that no longer exists.
        lots = list(self.lots.order_by("-acquired_on", "-created_at"))
        available = sum((Decimal(str(lot.qty_on_hand)) for lot in lots), Decimal(0))
        if available < qty:
            raise ValidationError(
                _(
                    "Only %(n)s of these are still on the shelf; the rest have been used or "
                    "moved. Record a return to the vendor or a scrap instead."
                )
                % {"n": available}
            )

        remaining = qty
        for lot in lots:
            if remaining <= 0:
                break
            take = min(remaining, Decimal(str(lot.qty_on_hand)))
            if take <= 0:
                continue
            StockTransaction.record(
                lot, -take, StockTransaction.Reason.UNRECEIVE,
                note=str(self.purchase), user=user,
            )
            remaining -= take

        self.qty_received = received - qty
        self.save()
        self.purchase.recompute_status()
        return qty

    def _overhead_per_unit(self) -> Decimal:
        """This line's share of tax and shipping, per unit."""
        purchase = self.purchase
        overhead = Decimal((purchase.tax_minor or 0) + (purchase.shipping_minor or 0))
        if overhead <= 0:
            return Decimal(0)
        subtotal = Decimal(purchase.subtotal_minor or 0)
        if subtotal <= 0:
            return Decimal(0)
        share = overhead * (Decimal(self.line_total_minor) / subtotal)
        qty = Decimal(str(self.qty_ordered)) or Decimal(1)
        return share / qty


class ExpenseCategory(models.TextChoices):
    SHOP_SUPPLIES = "shop_supplies", _("Shop supplies")
    OUTSOURCED_LABOR = "outsourced_labor", _("Outsourced labour")
    MACHINE_WORK = "machine_work", _("Machine work")
    TOWING = "towing", _("Towing")
    DISPOSAL = "disposal", _("Disposal")
    TOOLING = "tooling", _("Tooling")
    REGISTRATION = "registration", _("Registration")
    INSPECTION = "inspection", _("Inspection")
    INSURANCE = "insurance", _("Insurance")
    FUEL = "fuel", _("Fuel")
    OTHER = "other", _("Other")


# Tooling is tracked and exported, but excluded from per-asset cost unless the
# operator opts in: a torque wrench is not a cost of the Civic (OQ-4).
EXCLUDED_FROM_ASSET_COST = (ExpenseCategory.TOOLING,)


class Expense(RevisionedModel):
    asset = models.ForeignKey(
        "assets.Asset", null=True, blank=True, on_delete=models.CASCADE, related_name="expenses"
    )
    work_order = models.ForeignKey(
        "work.WorkOrder", null=True, blank=True, on_delete=models.CASCADE, related_name="expenses"
    )
    vendor = models.ForeignKey(
        Vendor, null=True, blank=True, on_delete=models.SET_NULL, related_name="expenses"
    )
    category = models.CharField(
        max_length=20, choices=ExpenseCategory.choices, default=ExpenseCategory.OTHER, db_index=True
    )
    amount_minor, amount_currency = money_columns("amount")
    incurred_on = models.DateField(default=timezone.localdate, db_index=True)
    description = models.CharField(max_length=200, blank=True)

    amount = money("amount")

    class Meta:
        ordering = ["-incurred_on"]
        indexes = [models.Index(fields=["asset", "-incurred_on"])]

    def __str__(self) -> str:
        return f"{self.get_category_display()} {self.description}".strip()

    def save(self, *args, **kwargs):
        # An expense on a work order belongs to that work order's asset, so
        # per-vehicle rollups do not depend on remembering to set both.
        if self.work_order_id and not self.asset_id:
            self.asset_id = self.work_order.asset_id
        return super().save(*args, **kwargs)

    @property
    def counts_toward_asset_cost(self) -> bool:
        
        if self.category in EXCLUDED_FROM_ASSET_COST:
            return conf.COST_INCLUDE_TOOLING
        return True
