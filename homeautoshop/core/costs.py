"""
Cost rollups (SPEC §7.6, FR-COST-1..6).

Three rules keep these numbers honest:

* **Parts are valued at the lot cost actually consumed**, not at today's price.
* **Tooling is excluded from per-asset cost** unless the operator opts in (OQ-4).
* **Labour is an estimate and is labeled as one.** It is included only when a
  rate is configured, and never rendered as a bill (NG-1).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from django.db.models import Q, Sum
from django.utils.translation import gettext_lazy as _

from .measurements import Money
from .runtime import conf


@dataclass(slots=True)
class CostLine:
    label: str
    amount_minor: int
    detail: str = ""

    @property
    def money(self) -> Money:
        return Money(self.amount_minor, conf.CURRENCY_REPORTING)


@dataclass(slots=True)
class Rollup:
    currency: str
    lines: list[CostLine] = field(default_factory=list)
    labour_minutes: int = 0
    labour_is_estimate: bool = True

    @property
    def total_minor(self) -> int:
        return sum(line.amount_minor for line in self.lines)

    @property
    def total(self) -> Money:
        return Money(self.total_minor, self.currency)

    @property
    def labour_hours(self) -> float:
        return round(self.labour_minutes / 60, 2)

    def add(self, label: str, amount_minor: int, detail: str = "") -> None:
        if amount_minor:
            self.lines.append(CostLine(label, amount_minor, detail))


def _labour_minor(minutes: int) -> int:
    rate = conf.LABOR_RATE_MINOR
    if not rate or not minutes:
        return 0
    return int(Decimal(rate) * Decimal(minutes) / Decimal(60))


def work_order_cost(work_order) -> Rollup:
    """Itemised cost of one job (FR-COST-1)."""
    rollup = Rollup(currency=conf.CURRENCY_REPORTING)

    parts_minor = sum(u.line_total_minor for u in work_order.part_usages.select_related("part"))
    rollup.add(str(_("Parts")), parts_minor, f"{work_order.part_usages.count()} line(s)")

    for expense in work_order.expenses.all():
        rollup.add(expense.get_category_display(), expense.amount_minor or 0, expense.description)

    minutes = work_order.time_entries.aggregate(n=Sum("minutes"))["n"] or 0
    rollup.labour_minutes = minutes
    if labour := _labour_minor(minutes):
        rollup.add(str(_("Time (estimated)")), labour, f"{round(minutes / 60, 2)} h")

    return rollup


def asset_cost(asset, *, include_tooling: bool | None = None) -> Rollup:
    """Lifetime cost of ownership so far (FR-COST-2)."""
    from homeautoshop.purchasing.models import EXCLUDED_FROM_ASSET_COST, Expense

    if include_tooling is None:
        include_tooling = conf.COST_INCLUDE_TOOLING

    rollup = Rollup(currency=conf.CURRENCY_REPORTING)

    # Every part that went on this vehicle, whether or not a job records it.
    # Reading only `asset.work_orders` was the bug: a part used against the
    # vehicle with no work order behind it (FR-INV-10) left the shelf, cost real
    # money, and appeared in no total anywhere — the one number the whole
    # feature exists to make true was the one it missed.
    from homeautoshop.parts.models import PartUsage

    usages = PartUsage.objects.filter(
        Q(work_order__asset=asset) | Q(work_order__isnull=True, asset=asset)
    )
    parts_minor = sum(usage.line_total_minor for usage in usages)
    rollup.add(str(_("Parts")), parts_minor)

    expenses = Expense.objects.filter(asset=asset)
    if not include_tooling:
        expenses = expenses.exclude(category__in=EXCLUDED_FROM_ASSET_COST)
    by_category: dict[str, int] = {}
    for expense in expenses:
        by_category.setdefault(expense.get_category_display(), 0)
        by_category[expense.get_category_display()] += expense.amount_minor or 0
    for label, amount in sorted(by_category.items()):
        rollup.add(label, amount)

    minutes = 0
    from homeautoshop.work.models import TimeEntry

    minutes = TimeEntry.objects.filter(work_order__asset=asset).aggregate(n=Sum("minutes"))["n"] or 0
    rollup.labour_minutes = minutes
    if labour := _labour_minor(minutes):
        rollup.add(str(_("Time (estimated)")), labour, f"{round(minutes / 60, 2)} h")

    return rollup


@dataclass(slots=True)
class PerDistance:
    """Repair cost per distance — fuel excluded by design (OQ-3/NG-7)."""

    cost_minor: int
    distance: Decimal
    unit: str
    from_date: date | None
    to_date: date | None
    currency: str = "USD"

    @property
    def per_unit(self) -> "Money":
        """The figure as money, because `0.20 minor units per mi` is not a price.

        Rounded to the smallest coin: a per-mile cost carried to more places
        than the currency has would be false precision on top of an estimate
        that already depends on odometer readings somebody typed.
        """
        from .measurements import Money

        return Money(int(self.minor_per_unit.to_integral_value()), self.currency)

    @property
    def is_computable(self) -> bool:
        return self.distance > 0

    @property
    def minor_per_unit(self) -> Decimal:
        if not self.is_computable:
            return Decimal(0)
        return Decimal(self.cost_minor) / self.distance

    @property
    def basis(self) -> str:
        """State the interval used, rather than presenting a bare number."""
        if not self.is_computable:
            return str(_("Not enough odometer history yet."))
        return str(
            _("%(distance)s %(unit)s between %(start)s and %(end)s")
        ) % {
            "distance": f"{self.distance:,.0f}",
            "unit": self.unit,
            "start": self.from_date,
            "end": self.to_date,
        }


def cost_per_distance(asset, *, since: date | None = None) -> PerDistance:
    """FR-COST-3, computed over observed meter history.

    This deliberately **excludes fuel**: fuel is not a repair function and is
    out of scope for good, so the number is repair-and-ownership cost per
    distance. The report says so rather than quietly omitting it.
    """
    readings = asset.usage_readings.filter(meter="odometer").order_by("read_on")
    if since:
        readings = readings.filter(read_on__gte=since)
    rows = list(readings)
    if len(rows) < 2:
        return PerDistance(0, Decimal(0), asset.meter_unit, None, None)

    first, last = rows[0], rows[-1]
    distance = Decimal(str(last.value)) - Decimal(str(first.value))
    if distance <= 0:
        return PerDistance(0, Decimal(0), asset.meter_unit, first.read_on, last.read_on)

    rollup = asset_cost(asset)
    return PerDistance(
        cost_minor=rollup.total_minor,
        distance=distance,
        unit=asset.meter_unit,
        from_date=first.read_on,
        to_date=last.read_on,
    )


def spend_by_month(*, months: int = 12) -> list[dict]:
    """Shop spend over time (FR-COST-5, FR-REP-3)."""
    from django.utils import timezone

    from homeautoshop.purchasing.models import Expense

    start = timezone.localdate() - timedelta(days=30 * months)
    buckets: dict[str, int] = {}

    for expense in Expense.objects.filter(incurred_on__gte=start):
        key = expense.incurred_on.strftime("%Y-%m")
        buckets[key] = buckets.get(key, 0) + (expense.amount_minor or 0)

    from homeautoshop.parts.models import PartUsage

    for usage in PartUsage.objects.filter(installed_at__gte=start).select_related("part"):
        key = usage.installed_at.strftime("%Y-%m")
        buckets[key] = buckets.get(key, 0) + usage.line_total_minor

    return [
        {"month": month, "amount_minor": amount, "money": Money(amount, conf.CURRENCY_REPORTING)}
        for month, amount in sorted(buckets.items())
    ]


def inventory_value() -> Money:
    """What is on the shelf, at what it actually cost (FR-REP-3)."""
    from homeautoshop.parts.models import StockLot

    total = 0
    for lot in StockLot.objects.filter(qty_on_hand__gt=0):
        total += int(Decimal(lot.unit_cost_minor or 0) * Decimal(str(lot.qty_on_hand)))
    return Money(total, conf.CURRENCY_REPORTING)


def active_warranties():
    """Parts still under warranty — data already collected, never surfaced (C-3)."""
    from homeautoshop.parts.models import PartUsage

    rows = PartUsage.objects.select_related(
        "part", "work_order", "work_order__asset", "asset"
    ).filter(
        warranty_months__isnull=False
    )
    return sorted(
        (u for u in rows if u.under_warranty),
        key=lambda u: u.warranty_expires_on,
    )
