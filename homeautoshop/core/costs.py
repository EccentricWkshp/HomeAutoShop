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


def _add_months(anchor: date, months: int) -> date:
    """Calendar months, clamped to the shortest — 31 Jan plus one is 28 Feb."""
    import calendar

    month = anchor.month - 1 + months
    year = anchor.year + month // 12
    month = month % 12 + 1
    return anchor.replace(
        year=year, month=month, day=min(anchor.day, calendar.monthrange(year, month)[1])
    )


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


# --------------------------------------------------------------------------
# Forecasting (SPEC R-7)
# --------------------------------------------------------------------------
#
# `spend_by_month` looks backwards. This is the same shape looking forwards,
# and it is built from two things already modeled: the schedule knows *when*
# each service comes round, and the completion history knows what that service
# has actually cost in this shop.
#
# The honesty rule it inherits from the rest of this module: **an item with no
# cost history is not priced at zero.** It is counted, named, and left out of
# the total, so the figure is a floor with its own shortfall stated beside it.
# Treating unknown as nothing would understate exactly the thing the report
# exists to warn about — the mistake the cores total already avoids.

#: How far ahead to look. A year covers every common interval at least once and
#: is short enough that the usage rate behind it is still worth trusting.
FORECAST_MONTHS = 12


@dataclass(slots=True)
class ForecastItem:
    """One occurrence of one service, on the date it is expected to land."""

    item: object
    due_on: date
    amount_minor: int | None

    @property
    def money(self) -> Money | None:
        if self.amount_minor is None:
            return None
        return Money(self.amount_minor, conf.CURRENCY_REPORTING)


@dataclass(slots=True)
class ForecastMonth:
    month: str
    entries: list[ForecastItem] = field(default_factory=list)

    @property
    def amount_minor(self) -> int:
        return sum(entry.amount_minor or 0 for entry in self.entries)

    @property
    def money(self) -> Money:
        return Money(self.amount_minor, conf.CURRENCY_REPORTING)

    @property
    def unpriced(self) -> int:
        return sum(1 for entry in self.entries if entry.amount_minor is None)


@dataclass(slots=True)
class Forecast:
    months: list[ForecastMonth]
    currency: str
    horizon: int = FORECAST_MONTHS

    @property
    def total_minor(self) -> int:
        return sum(month.amount_minor for month in self.months)

    @property
    def total(self) -> Money:
        return Money(self.total_minor, self.currency)

    @property
    def unpriced(self) -> list[ForecastItem]:
        """Occurrences this shop has never done, so has no price for."""
        return [e for m in self.months for e in m.entries if e.amount_minor is None]

    @property
    def is_complete(self) -> bool:
        return not self.unpriced

    @property
    def basis(self) -> str:
        """State what the number rests on rather than presenting it bare."""
        if not self.months:
            return str(
                _("Nothing is due in the next %(n)d months.") % {"n": self.horizon}
            )
        if self.is_complete:
            return str(_("From what this shop has paid for the same work before."))
        return str(
            _(
                "A floor rather than a total: %(n)d of these have never been done "
                "here, so there is no price to go on and they are left out."
            )
            % {"n": len(self.unpriced)}
        )


def typical_costs() -> dict:
    """What each service definition has actually cost, keyed by definition id.

    Read off completed work rather than off a price list, because a price list
    is a thing nobody maintains. A completion names the work order it was done
    on, and that work order already knows its own total.

    **Shared cost is split, not counted twice.** One Saturday's work order that
    closed an oil change, an air filter and a tyre rotation is not three
    separate bills, so its total is divided by the number of services it
    completed. That under-attributes the expensive one and over-attributes the
    cheap ones; it is the best answer available without asking somebody to
    itemise their weekend, and it is right in aggregate, which is what a
    forecast is.

    The median is taken rather than the mean, so one brake job where the
    caliper also had to be replaced does not raise the expected price of every
    future brake job.
    """
    import statistics
    from collections import Counter, defaultdict

    from homeautoshop.maintenance.models import ServiceCompletion

    completions = list(
        ServiceCompletion.objects.filter(work_order__isnull=False).select_related(
            "service_item", "work_order"
        )
    )
    per_order = Counter(c.work_order_id for c in completions)
    totals: dict = {}
    shares: dict = defaultdict(list)

    for completion in completions:
        order_id = completion.work_order_id
        if order_id not in totals:
            totals[order_id] = work_order_cost(completion.work_order).total_minor
        share = totals[order_id] // per_order[order_id]
        # A work order that cost nothing prices nothing. It is far likelier to
        # be one where the parts were never recorded than a service that is
        # genuinely free, and reading it as free drags the median down.
        if share > 0:
            shares[completion.service_item.definition_id].append(share)

    return {
        definition_id: int(statistics.median(values))
        for definition_id, values in shares.items()
    }


def forecast(*, months: int = FORECAST_MONTHS, today: date | None = None) -> Forecast:
    """What the fleet is likely to cost over the next year (R-7).

    Every service due in the window, at the price this shop has paid for it
    before, bucketed by the month it is expected to land in. Recurring items
    appear as often as they recur — an oil change on a daily driver is three
    entries in a year, and counting it once is how a forecast ends up being
    half of what actually happens.
    """
    from django.utils import timezone

    from homeautoshop.assets.models import Asset
    from homeautoshop.maintenance.models import AssetServiceItem, ServiceStatus
    from homeautoshop.maintenance.services import project, recurrence_days

    today = today or timezone.localdate()
    horizon = _add_months(today, months)
    prices = typical_costs()
    buckets: dict[str, ForecastMonth] = {}

    rows = (
        AssetServiceItem.objects.filter(asset__in=Asset.objects.fleet())
        .exclude(status=ServiceStatus.DISABLED)
        .select_related("asset", "definition")
    )

    for item in rows:
        if item.is_snoozed:
            continue
        due = project(item, today=today).projected_date
        if due is None:
            continue
        # Something already overdue is spend that has not happened yet, so it
        # belongs in the month ahead rather than in one that has gone.
        due = max(due, today)
        every = recurrence_days(item, today=today)
        price = prices.get(item.definition_id)

        while due < horizon:
            key = due.strftime("%Y-%m")
            month = buckets.setdefault(key, ForecastMonth(key))
            month.entries.append(ForecastItem(item=item, due_on=due, amount_minor=price))
            if not every:
                break
            due = due + timedelta(days=every)

    return Forecast(
        months=[buckets[key] for key in sorted(buckets)],
        currency=conf.CURRENCY_REPORTING,
        horizon=months,
    )
