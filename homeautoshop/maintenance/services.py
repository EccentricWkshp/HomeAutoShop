"""
Interval math and due projection (SPEC FR-MAINT-3/4/5, §7.7).

Two ideas do the work:

* **First to arrive wins.** An item with a distance, a time, and an hours
  interval is due when the earliest of them lands. Anything else would let a
  car that sits for two years pass its oil change unnoticed.
* **Project a date from observed usage.** "Due in 900 mi" is not actionable on
  a Saturday morning; "due in about 3 weeks" is. The projection uses the
  asset's own trailing usage rate, and says so, rather than a global guess.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from homeautoshop.core.measurements import convert
from homeautoshop.core.runtime import conf

from .models import (
    AssetServiceItem,
    ScheduleTemplate,
    ServiceCompletion,
    ServiceStatus,
)

TRAILING_WINDOW_DAYS = 182  # roughly six months


@dataclass(slots=True)
class UsageRate:
    per_day: Decimal
    unit: str
    observed: bool
    span_days: int = 0

    @property
    def basis(self) -> str:
        if not self.observed:
            return str(_("Estimated — not enough meter history yet."))
        return str(_("From %(n)d days of readings.")) % {"n": self.span_days}


def usage_rate(asset, *, today: date | None = None) -> UsageRate:
    """Observed distance (or hours) per day, from the asset's own history."""
    today = today or timezone.localdate()
    unit = asset.meter_unit or "mi"
    default = UsageRate(
        Decimal(conf.DEFAULT_DISTANCE_PER_DAY), unit, observed=False
    )
    if not asset.has_meter:
        return UsageRate(Decimal(0), unit, observed=False)

    readings = list(
        asset.usage_readings.filter(
            meter=asset.meter, read_on__gte=today - timedelta(days=TRAILING_WINDOW_DAYS)
        ).order_by("read_on")
    )
    if len(readings) < 2:
        # Fall back to the whole history before giving up on observation.
        readings = list(asset.usage_readings.filter(meter=asset.meter).order_by("read_on"))
    if len(readings) < 2:
        return default

    first, last = readings[0], readings[-1]
    span = (last.read_on - first.read_on).days
    delta = Decimal(str(last.value)) - Decimal(str(first.value))
    if span <= 0 or delta <= 0:
        return default
    return UsageRate(delta / Decimal(span), unit, observed=True, span_days=span)


def _interval_in_asset_units(item: AssetServiceItem, asset) -> Decimal | None:
    """The item's distance interval expressed in the asset's meter unit."""
    if not item.interval_distance:
        return None
    if item.interval_unit == asset.meter_unit:
        return Decimal(item.interval_distance)
    try:
        return convert(item.interval_distance, item.interval_unit, asset.meter_unit)
    except Exception:
        return Decimal(item.interval_distance)


def recalculate(item: AssetServiceItem, *, today: date | None = None, save: bool = True) -> AssetServiceItem:
    """Recompute next-due and status for one item."""
    today = today or timezone.localdate()
    asset = item.asset

    if item.status == ServiceStatus.DISABLED:
        return item

    # Distance
    item.next_due_usage = None
    interval = _interval_in_asset_units(item, asset)
    if interval and item.last_done_usage is not None:
        item.next_due_usage = Decimal(str(item.last_done_usage)) + interval
    elif interval and asset.current_usage is not None and item.last_done_usage is None:
        # Never done: due an interval from where the meter is now, which is the
        # least alarming honest assumption for a vehicle new to the shop.
        item.next_due_usage = Decimal(str(asset.current_usage)) + interval

    # Time
    item.next_due_on = None
    if item.interval_months:
        anchor = item.last_done_on or today
        item.next_due_on = _add_months(anchor, item.interval_months)

    # Hours behave like distance on an hour-metered asset; the meter unit
    # already distinguishes them, so no separate branch is needed beyond the
    # interval field the operator filled in.
    if item.interval_hours and asset.meter == "engine_hours":
        base = item.last_done_usage if item.last_done_usage is not None else asset.current_usage
        if base is not None:
            hours_due = Decimal(str(base)) + Decimal(item.interval_hours)
            if item.next_due_usage is None or hours_due < item.next_due_usage:
                item.next_due_usage = hours_due

    item.status = _status_for(item, asset, today)
    if save:
        item.save()
    return item


def _status_for(item: AssetServiceItem, asset, today: date) -> str:
    # Compare against the `today` we were given, not the wall clock: the whole
    # calculation is meant to be a pure function of its inputs, and a branch
    # that quietly reads the real date makes it untestable and inconsistent.
    if item.snooze_until and item.snooze_until >= today:
        return ServiceStatus.SNOOZED

    lead_days = conf.DUE_SOON_DAYS
    lead_distance = Decimal(conf.DUE_SOON_DISTANCE)

    overdue = False
    due_soon = False

    days = item.days_remaining(today)
    if days is not None:
        if days < 0:
            overdue = True
        elif days <= lead_days:
            due_soon = True

    current = asset.current_usage
    remaining = item.distance_remaining(current)
    if remaining is not None:
        if remaining < 0:
            overdue = True
        elif remaining <= lead_distance:
            due_soon = True

    # First to arrive wins (FR-MAINT-3).
    if overdue:
        return ServiceStatus.OVERDUE
    return ServiceStatus.DUE_SOON if due_soon else ServiceStatus.OK


def _add_months(anchor: date, months: int) -> date:
    year = anchor.year + (anchor.month - 1 + months) // 12
    month = (anchor.month - 1 + months) % 12 + 1
    # Clamp for short months: 31 Jan + 1 month is the end of February.
    day = min(anchor.day, _days_in_month(year, month))
    return date(year, month, day)


def _days_in_month(year: int, month: int) -> int:
    import calendar

    return calendar.monthrange(year, month)[1]


@dataclass(slots=True)
class Projection:
    item: AssetServiceItem
    days_remaining: int | None
    distance_remaining: Decimal | None
    projected_date: date | None
    rate: UsageRate

    @property
    def summary(self) -> str:
        """State the basis rather than presenting a bare number."""
        parts = []
        if self.distance_remaining is not None:
            parts.append(
                str(_("in %(n)s %(unit)s"))
                % {"n": f"{self.distance_remaining:,.0f}", "unit": self.rate.unit}
            )
        if self.projected_date and self.distance_remaining is not None:
            parts.append(str(_("around %(d)s")) % {"d": self.projected_date.isoformat()})
        elif self.days_remaining is not None:
            parts.append(str(_("in %(n)d days")) % {"n": self.days_remaining})
        return " · ".join(parts) or str(_("no interval set"))


def project(item: AssetServiceItem, *, today: date | None = None) -> Projection:
    """Turn "due in 900 mi" into "due in about three weeks" (FR-MAINT-4)."""
    today = today or timezone.localdate()
    asset = item.asset
    rate = usage_rate(asset, today=today)
    remaining = item.distance_remaining(asset.current_usage)

    projected = None
    if remaining is not None and rate.per_day > 0:
        days = int(remaining / rate.per_day)
        projected = today + timedelta(days=max(days, 0))

    # Whichever arrives first is the one worth showing.
    if item.next_due_on and (projected is None or item.next_due_on < projected):
        projected = item.next_due_on

    return Projection(
        item=item,
        days_remaining=item.days_remaining(today),
        distance_remaining=remaining,
        projected_date=projected,
        rate=rate,
    )


def recurrence_days(item: AssetServiceItem, *, today: date | None = None) -> int | None:
    """How often this item comes round, in days — or `None` if it cannot say.

    `project` answers when an item is next due. This answers how long until the
    one after that, which is what a forecast needs: an oil change on a 5,000 mi
    interval lands three times in a year on a daily driver and once on a truck
    that leaves the yard twice a month, and a projection that counted each item
    once would understate exactly the vehicles that cost the most to run.

    Same rule as everywhere else in this module — **first to arrive wins**. An
    item with both a distance and a time interval recurs on whichever comes up
    sooner at this asset's observed rate.
    """
    today = today or timezone.localdate()
    asset = item.asset
    candidates: list[int] = []

    if item.interval_months:
        # Against a real anchor rather than 30-day months, so twelve monthly
        # services in a row still land on twelve distinct months.
        candidates.append((_add_months(today, item.interval_months) - today).days)

    rate = usage_rate(asset, today=today)
    if rate.per_day > 0:
        interval = _interval_in_asset_units(item, asset)
        if item.interval_hours and asset.meter == "engine_hours":
            hours = Decimal(item.interval_hours)
            interval = min(interval, hours) if interval else hours
        if interval:
            candidates.append(int(interval / rate.per_day))

    usable = [days for days in candidates if days > 0]
    return min(usable) if usable else None


@transaction.atomic
def apply_template(asset, template: ScheduleTemplate, *, overwrite: bool = False) -> list[AssetServiceItem]:
    """Materialise a template onto an asset as editable per-asset items."""
    created: list[AssetServiceItem] = []
    for entry in template.items.select_related("definition"):
        existing = AssetServiceItem.all_objects.filter(
            asset=asset, definition=entry.definition
        ).first()
        if existing is not None and existing.is_deleted:
            # Removed from this vehicle before, and now asked for again by
            # name. Revive the row rather than skip it or insert beside it:
            # it still holds this item's completions and its last-done date,
            # and a second row for the same definition is the thing the
            # unique constraint exists to prevent. Without this branch the
            # template would report items added and show none of them.
            existing.deleted_at = None
            if existing.status == ServiceStatus.DISABLED:
                # It is back because a template asked for it. Coming back
                # already switched off would be indistinguishable from the
                # apply having silently failed.
                existing.status = ServiceStatus.OK
        elif existing is not None and not overwrite:
            continue
        item = existing or AssetServiceItem(asset=asset, definition=entry.definition)
        item.interval_distance = entry.interval_distance or entry.definition.default_interval_distance
        item.interval_unit = entry.interval_unit or entry.definition.default_interval_unit
        item.interval_months = entry.interval_months or entry.definition.default_interval_months
        item.interval_hours = entry.interval_hours or entry.definition.default_interval_hours
        item.save()
        recalculate(item)
        created.append(item)
    return created


@transaction.atomic
def prune_to_template(asset, template: ScheduleTemplate) -> tuple[int, int]:
    """Take off whatever this template does not ask for. Returns (removed, kept).

    Switching a vehicle from one schedule to another was previously a one-way
    door: applying the new template added its items and left every item of the
    old one sitting in the list, and the only thing to do with those was
    ignore them one at a time and watch them stay on screen forever.

    What it will not remove is an item with history — the same line
    `is_removable` draws. Those are reported back as `kept` rather than
    silently spared, because a count of nine removed out of twelve invites the
    question the message should already have answered.
    """
    wanted = set(
        template.items.values_list("definition_id", flat=True)
    )
    removed = kept = 0
    for item in AssetServiceItem.objects.filter(asset=asset).exclude(definition_id__in=wanted):
        if item.is_removable:
            item.delete()
            removed += 1
        else:
            kept += 1
    return removed, kept


@transaction.atomic
def complete(
    item: AssetServiceItem,
    *,
    on: date | None = None,
    usage=None,
    job_item=None,
    work_order=None,
    note: str = "",
    backfill: bool = False,
) -> ServiceCompletion:
    """Record the work and roll the interval forward (FR-MAINT-5/6)."""
    on = on or timezone.localdate()
    if usage is None:
        usage = item.asset.current_usage

    completion = ServiceCompletion.objects.create(
        service_item=item,
        job_item=job_item,
        work_order=work_order,
        completed_on=on,
        usage=usage,
        note=note[:200],
        is_backfill=backfill,
    )

    # Only move the baseline forward: back-filling an older service must not
    # rewind a schedule that has since been serviced again.
    if item.last_done_on is None or on >= item.last_done_on:
        item.last_done_on = on
        if usage is not None:
            item.last_done_usage = usage
        item.snooze_until = None
        item.snooze_reason = ""
        recalculate(item)
    return completion


def refresh_asset(asset, *, today: date | None = None) -> int:
    for item in asset.service_items.all():
        recalculate(item, today=today)
    return asset.service_items.count()


def due_dashboard(*, limit: int | None = None, user=None) -> list[AssetServiceItem]:
    """Everything needing attention, most urgent first (FR-MAINT-7).

    Overdue safety items lead, because that is the ordering that matters when
    someone is deciding what to do with a Saturday.

    `user` narrows the fleet to what that person may see (SPEC §12.2a). It is
    optional because the reminder digest and the forecast run with nobody
    signed in and are meant to see everything; a page passes the viewer.
    """
    from homeautoshop.accounts.policy import visible_assets_for
    from homeautoshop.assets.models import Asset

    rows = AssetServiceItem.objects.filter(
        asset__in=Asset.objects.fleet()
    ).needing_attention().select_related("asset", "definition")
    if user is not None:
        rows = visible_assets_for(user, rows)
    rows = list(rows)
    rows.sort(
        key=lambda i: (
            i.status != ServiceStatus.OVERDUE,
            not i.is_safety,
            i.next_due_on or date.max,
        )
    )
    return rows[:limit] if limit else rows
