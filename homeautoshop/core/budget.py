"""
Project budget burn-down (SPEC §7.6b, FR-COST-8, roadmap R-6).

A home build overruns. Watching it overrun is the point, and the reason this
is worth building is that nothing else in the application answers *"how much
of what I said I would spend is left"* — `asset_cost` answers a lifetime
question and `work_order_cost` answers a one-afternoon question, and an engine
swap is neither.

Four decisions carry it, and each one is a place where the obvious
implementation is wrong.

**The tree, not the row.** FR-WO-8 has always said costs roll up to the parent
and nothing ever did it: `work_order_cost` reads one work order's own parts,
expenses and time, so a project whose teardown, machine work and reassembly
are its children reported almost nothing. A burn-down built on that would be
worse than no burn-down, because it would say a project was on budget by
failing to look at where the money went. `WorkOrder.tree_ids()` was the
missing half, and `descendant_ids` had been sitting there since the parent
picker was written.

**Three kinds of money, kept apart.** "What has this cost me" and "what is on
the vehicle" are different questions with different answers, and a single
number has to lie about one of them:

* **Spent** — parts fitted at the lot cost actually consumed, plus expenses.
  This is the burn-down line, and it is the same basis as `spend_by_month`
  (FR-COST-5) so the two reports cannot disagree.
* **On the shelf** — bought against this job, arrived, not yet fitted. Real
  money that has left the household and is not yet on the car.
* **On order** — the un-received part of the job's open purchases. Promised,
  not yet gone.

Collapsing the last two into "spent" would double-count them the moment the
parts are fitted. Dropping them lets a project read as comfortably under
budget on the day before a pallet arrives. So all three are shown, `remaining`
is measured against the sum, and the burn-down line tracks only the first.

**Your own time is not budgeted.** `LABOR_RATE_MINOR` values the operator's
Saturdays for reporting (FR-TIME-3), and a household budget is cash. Charging
a notional rate against it would report an overrun that never left anybody's
bank account — and on a project, which is where the hours are, it would
dominate. Hours are shown beside the figures; they never move the bar.

**A quiet month is still a month.** The series is filled from the project's
first activity to today rather than assembled from the months that happen to
have rows in them, because a burn-down is about *pace* and a chart that
silently omits the two months nothing happened draws a slope that is wrong.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from django.db.models import Sum
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from .costs import Rollup, _labour_minor, parts_by_job
from .measurements import Money
from .runtime import conf


@dataclass(slots=True)
class BurnMonth:
    """One month of the line."""

    month: str
    spent_minor: int
    cumulative_minor: int
    currency: str = "USD"

    @property
    def spent(self) -> Money:
        return Money(self.spent_minor, self.currency)

    @property
    def cumulative(self) -> Money:
        return Money(self.cumulative_minor, self.currency)


@dataclass(slots=True)
class Budget:
    """What was said, what has gone, and what is still in the air."""

    currency: str
    budget_minor: int
    spent_minor: int
    on_shelf_minor: int
    on_order_minor: int
    labour_minutes: int = 0
    child_count: int = 0
    months: list[BurnMonth] = field(default_factory=list)

    # -- the three kinds of money ----------------------------------------

    @property
    def budget(self) -> Money:
        return Money(self.budget_minor, self.currency)

    @property
    def spent(self) -> Money:
        return Money(self.spent_minor, self.currency)

    @property
    def on_shelf(self) -> Money:
        return Money(self.on_shelf_minor, self.currency)

    @property
    def on_order(self) -> Money:
        return Money(self.on_order_minor, self.currency)

    @property
    def committed_minor(self) -> int:
        """Everything the budget has actually been drawn against."""
        return self.spent_minor + self.on_shelf_minor + self.on_order_minor

    @property
    def committed(self) -> Money:
        return Money(self.committed_minor, self.currency)

    # -- where that leaves it ---------------------------------------------

    @property
    def remaining_minor(self) -> int:
        """Negative when it is over, which is the number worth showing."""
        return self.budget_minor - self.committed_minor

    @property
    def remaining(self) -> Money:
        return Money(self.remaining_minor, self.currency)

    @property
    def over_minor(self) -> int:
        return max(0, -self.remaining_minor)

    @property
    def over(self) -> Money:
        return Money(self.over_minor, self.currency)

    @property
    def is_over(self) -> bool:
        return self.remaining_minor < 0

    @property
    def used_percent(self) -> int:
        """Uncapped on purpose — 140% is the fact, not an error to clamp."""
        if self.budget_minor <= 0:
            return 0
        return round(self.committed_minor * 100 / self.budget_minor)

    # -- the bar ----------------------------------------------------------
    #
    # Widths are against the *larger* of the budget and what has been
    # committed, so an overrun draws past the budget marker instead of
    # rescaling the whole bar and hiding that it happened.

    @property
    def _scale(self) -> int:
        return max(self.budget_minor, self.committed_minor) or 1

    @property
    def spent_percent(self) -> float:
        return round(self.spent_minor * 100 / self._scale, 2)

    @property
    def on_shelf_percent(self) -> float:
        return round(self.on_shelf_minor * 100 / self._scale, 2)

    @property
    def on_order_percent(self) -> float:
        return round(self.on_order_minor * 100 / self._scale, 2)

    @property
    def budget_marker_percent(self) -> float:
        """Where the budget sits on that bar."""
        return round(self.budget_minor * 100 / self._scale, 2)

    # -- the hours, which never move the bar -------------------------------

    @property
    def labour_hours(self) -> float:
        return round(self.labour_minutes / 60, 2)

    @property
    def peak_month_minor(self) -> int:
        return max((month.spent_minor for month in self.months), default=0)


def project_cost(work_order) -> Rollup:
    """Cost of a work order **and everything under it** (FR-WO-8).

    `work_order_cost` deliberately keeps answering for one row — the costs
    card on a leaf job means what it always meant. This is the other question,
    and until now the application could not answer it.
    """
    from homeautoshop.parts.models import PartUsage
    from homeautoshop.purchasing.models import Expense
    from homeautoshop.work.models import TimeEntry

    ids = work_order.tree_ids()
    rollup = Rollup(currency=conf.CURRENCY_REPORTING)

    usages = PartUsage.objects.filter(work_order_id__in=ids).select_related(
        "part", "work_order"
    )
    parts_minor = sum(usage.line_total_minor for usage in usages)
    rollup.add(str(_("Parts")), parts_minor, breakdown=parts_by_job(usages))

    by_category: dict[str, int] = {}
    for expense in Expense.objects.filter(work_order_id__in=ids):
        label = expense.get_category_display()
        by_category[label] = by_category.get(label, 0) + (expense.amount_minor or 0)
    for label, amount in sorted(by_category.items()):
        rollup.add(label, amount)

    minutes = (
        TimeEntry.objects.filter(work_order_id__in=ids).aggregate(n=Sum("minutes"))["n"] or 0
    )
    rollup.labour_minutes = minutes
    if labour := _labour_minor(minutes):
        rollup.add(str(_("Time (estimated)")), labour, f"{round(minutes / 60, 2)} h")

    return rollup


def _spent_minor(ids) -> int:
    """Parts fitted and expenses incurred, across the tree."""
    from homeautoshop.parts.models import PartUsage
    from homeautoshop.purchasing.models import Expense

    parts = sum(
        usage.line_total_minor
        for usage in PartUsage.objects.filter(work_order_id__in=ids).select_related("part")
    )
    expenses = (
        Expense.objects.filter(work_order_id__in=ids).aggregate(n=Sum("amount_minor"))["n"] or 0
    )
    return parts + expenses


def on_shelf_minor(ids) -> int:
    """Bought for this job, arrived, not yet fitted.

    Traced through `StockLot.purchase_line`, which is the only thing that
    remembers *why* a box is on the shelf. A lot drawn down to nothing drops
    out on its own, because what was drawn from it is already in `spent`.
    """
    from homeautoshop.parts.models import StockLot

    lots = StockLot.objects.filter(
        purchase_line__purchase__work_order_id__in=ids, qty_on_hand__gt=0
    )
    return sum(
        int(Decimal(lot.unit_cost_minor or 0) * Decimal(str(lot.qty_on_hand))) for lot in lots
    )


def on_order_minor(ids) -> int:
    """The un-received part of the job's open purchases.

    Valued by scaling the order's total — tax, shipping and discount
    included — by the share of its line value still outstanding. Those
    overheads are apportioned at receiving (`PurchaseLine.receive`), so
    pricing an undelivered line at its bare unit price would understate it by
    exactly the shipping that is already on the invoice.
    """
    from homeautoshop.purchasing.models import Purchase, PurchaseStatus

    total = 0
    open_orders = Purchase.objects.filter(
        work_order_id__in=ids,
        status__in=(PurchaseStatus.ORDERED, PurchaseStatus.PARTIAL),
    ).prefetch_related("lines")

    for purchase in open_orders:
        lines = list(purchase.lines.all())
        ordered_value = sum(line.line_total_minor for line in lines)
        if ordered_value <= 0:
            continue
        outstanding_value = sum(
            int(line.unit_price_exact * line.outstanding)
            for line in lines
            if line.outstanding > 0
        )
        if outstanding_value <= 0:
            continue
        total += int(
            Decimal(purchase.total_minor)
            * Decimal(outstanding_value)
            / Decimal(ordered_value)
        )
    return total


def _months_between(first: date, last: date) -> list[str]:
    """Every month from one to the other, inclusive, with none skipped."""
    out: list[str] = []
    year, month = first.year, first.month
    while (year, month) <= (last.year, last.month):
        out.append(f"{year:04d}-{month:02d}")
        month += 1
        if month == 13:
            year, month = year + 1, 1
    return out


def burn_months(work_order, *, today: date | None = None) -> list[BurnMonth]:
    """Cumulative spend, one row per calendar month, gaps filled."""
    from homeautoshop.parts.models import PartUsage
    from homeautoshop.purchasing.models import Expense

    today = today or timezone.localdate()
    ids = work_order.tree_ids()
    currency = conf.CURRENCY_REPORTING

    buckets: dict[str, int] = {}
    dates: list[date] = []
    for usage in PartUsage.objects.filter(work_order_id__in=ids).select_related("part"):
        key = usage.installed_at.strftime("%Y-%m")
        buckets[key] = buckets.get(key, 0) + usage.line_total_minor
        dates.append(usage.installed_at)
    for expense in Expense.objects.filter(work_order_id__in=ids):
        key = expense.incurred_on.strftime("%Y-%m")
        buckets[key] = buckets.get(key, 0) + (expense.amount_minor or 0)
        dates.append(expense.incurred_on)

    if not buckets:
        return []

    # Spend can predate the work order — somebody who opens the record after
    # buying the engine is doing the normal thing, not the wrong thing — so
    # the window starts at whichever came first.
    opened = timezone.localtime(work_order.opened_at).date()
    start = min([opened, *dates])
    end = max([today, *dates])

    rows: list[BurnMonth] = []
    running = 0
    for key in _months_between(start, end):
        spent = buckets.get(key, 0)
        running += spent
        rows.append(BurnMonth(key, spent, running, currency))
    return rows


def budget_burndown(work_order, *, today: date | None = None) -> Budget | None:
    """The whole picture, or `None` when no budget was set.

    A work order with no budget gets no card: an empty burn-down is a screen
    telling somebody they have spent 0% of nothing.
    """
    if work_order.budget_minor is None:
        return None

    ids = work_order.tree_ids()
    from homeautoshop.work.models import TimeEntry

    return Budget(
        currency=conf.CURRENCY_REPORTING,
        budget_minor=work_order.budget_minor,
        spent_minor=_spent_minor(ids),
        on_shelf_minor=on_shelf_minor(ids),
        on_order_minor=on_order_minor(ids),
        labour_minutes=(
            TimeEntry.objects.filter(work_order_id__in=ids).aggregate(n=Sum("minutes"))["n"] or 0
        ),
        child_count=len(ids) - 1,
        months=burn_months(work_order, today=today),
    )
