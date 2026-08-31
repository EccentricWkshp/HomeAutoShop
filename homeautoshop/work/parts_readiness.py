"""
Can this job start? — answered about parts (SPEC FR-WO-2, FR-WO-11, FR-INV-1).

The sibling of `readiness.py`, which asks the same question about tools. That
one warns when the breaker bar is at Dave's; this one warns when the caliper
bolts are still at RockAuto.

**Nothing here moves stock.** A part spoken for by a planned job is not
consumed, and writing a `StockTransaction` to reserve it would make it look
like it was: `qty_on_hand` is a projection of the ledger and nothing else gets
to move it (FR-INV-1). So a claim is a `PartRequirement` row, and availability
is arithmetic:

    free = on hand − what other open jobs have claimed and not yet used

That subtraction is the whole point of the feature. Two brake jobs planned for
the same weekend, one box of pads on the shelf: without it both look ready,
and the second one finds out on Saturday.

**The box goes to whoever asked first.** Only claims made *before* this job's
were counted at first, and the alternative — every job counting every other
job's claim — tells both of those brake jobs to buy a box, which is one box
too many. Ordering by when the claim was made is also the answer a person
would give: you earmarked it for the front brakes a week ago, so it is spoken
for, and the job you thought of this morning is the one that needs a trip to
the store.

**It is a warning, never a block** — the same rule as tools, for a better
reason here: the operator may be about to drive to the parts store, may have a
box in the truck that has not been received yet, or may simply be starting the
half of the job that does not need it. Being told is useful; being stopped is
not.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

ZERO = Decimal("0")


@dataclass(slots=True)
class PartLine:
    """One part this job wants, and whether it can have it."""

    part: object
    requirements: list = field(default_factory=list)
    needed: Decimal = ZERO
    used: Decimal = ZERO
    on_hand: Decimal = ZERO
    committed_elsewhere: Decimal = ZERO
    on_order: Decimal = ZERO
    drafts: list = field(default_factory=list)

    @property
    def outstanding(self) -> Decimal:
        """Still to come off the shelf. Using a part settles the claim."""
        return max(ZERO, self.needed - self.used)

    @property
    def free(self) -> Decimal:
        """On the shelf and not already spoken for by another open job."""
        return max(ZERO, self.on_hand - self.committed_elsewhere)

    @property
    def short(self) -> Decimal:
        """What would have to be bought. On order counts — it is coming."""
        return max(ZERO, self.outstanding - self.free - self.on_order)

    @property
    def is_ready(self) -> bool:
        return self.short == ZERO

    @property
    def is_covered_by_an_order(self) -> bool:
        """Not on the shelf, but bought — which is a different kind of waiting."""
        return self.short == ZERO and self.outstanding > self.free

    @property
    def is_satisfied(self) -> bool:
        """Already used. Kept on the list as the record of what was planned."""
        return self.outstanding == ZERO

    @property
    def state(self) -> str:
        """One word for the template, so the rule lives here and not in HTML."""
        if self.is_satisfied:
            return "used"
        if self.is_covered_by_an_order:
            return "on_order"
        if self.is_ready:
            return "on_hand"
        return "short"


@dataclass(slots=True)
class Readiness:
    lines: list[PartLine] = field(default_factory=list)

    @property
    def shortfalls(self) -> list[PartLine]:
        return [line for line in self.lines if not line.is_ready]

    @property
    def is_ready(self) -> bool:
        """Vacuously true with nothing planned, which is the honest answer.

        A job nobody has listed parts for is not blocked by parts. It may
        still be a job that needed the list.
        """
        return not self.shortfalls

    def __bool__(self) -> bool:
        return bool(self.lines)


def for_work_order(work_order) -> Readiness:
    """What this job wants, against what it can actually have."""
    from django.db.models import Sum

    from homeautoshop.parts.models import PartUsage

    from .models import PartRequirement

    requirements = list(
        PartRequirement.objects.filter(work_order=work_order)
        .select_related("part", "job_item")
        .order_by("created_at")
    )
    if not requirements:
        return Readiness()

    parts = {requirement.part_id: requirement.part for requirement in requirements}

    used = dict(
        PartUsage.objects.filter(work_order=work_order, part_id__in=parts)
        .values_list("part_id")
        .annotate(total=Sum("qty"))
    )
    # When this job staked its claim on each part, which decides who the
    # shelf belongs to when there is not enough to go round.
    asked_at = {}
    for requirement in requirements:
        first = asked_at.get(requirement.part_id)
        if first is None or requirement.created_at < first:
            asked_at[requirement.part_id] = requirement.created_at

    on_hand = _on_hand(parts)
    elsewhere = _committed_elsewhere(parts, exclude=work_order, before=asked_at)
    on_order = _on_order(parts, work_order=work_order)
    drafts = _on_a_list(parts, work_order=work_order)

    lines: dict[object, PartLine] = {}
    for requirement in requirements:
        line = lines.get(requirement.part_id)
        if line is None:
            line = lines[requirement.part_id] = PartLine(
                part=requirement.part,
                used=Decimal(str(used.get(requirement.part_id) or 0)),
                on_hand=on_hand.get(requirement.part_id, ZERO),
                committed_elsewhere=elsewhere.get(requirement.part_id, ZERO),
                on_order=on_order.get(requirement.part_id, ZERO),
                drafts=drafts.get(requirement.part_id, []),
            )
        line.requirements.append(requirement)
        line.needed += Decimal(str(requirement.qty))

    return Readiness(lines=list(lines.values()))


def _on_hand(parts) -> dict:
    from django.db.models import Sum

    from homeautoshop.parts.models import StockLot

    rows = (
        StockLot.objects.filter(part_id__in=parts)
        .values_list("part_id")
        .annotate(total=Sum("qty_on_hand"))
    )
    return {part_id: Decimal(str(total or 0)) for part_id, total in rows}


def _committed_elsewhere(parts, *, exclude, before) -> dict:
    """What earlier open jobs have claimed and not yet drawn.

    Two subtleties, both of which produce a wrong number if missed:

    * **A claim already drawn stops counting.** The stock left `qty_on_hand`
      when it was consumed, so continuing to subtract the claim would take it
      off the shelf twice. That is why this nets usage off per work order
      rather than summing requirements alone.
    * **Only claims older than this job's count.** Otherwise two jobs wanting
      the same last box each see the other's claim, both report a shortfall,
      and somebody buys two.
    """
    from django.db.models import Min, Sum

    from homeautoshop.parts.models import PartUsage

    from .models import OPEN_STATUSES, PartRequirement

    claimed: dict[tuple, Decimal] = {}
    rows = (
        PartRequirement.objects.filter(part_id__in=parts, work_order__status__in=OPEN_STATUSES)
        .exclude(work_order_id=exclude.pk)
        .values_list("work_order_id", "part_id")
        .annotate(total=Sum("qty"), first_asked=Min("created_at"))
    )
    for work_order_id, part_id, total, first_asked in rows:
        ours = before.get(part_id)
        if ours is not None and first_asked >= ours:
            # They asked after we did, so the shelf is ours to count on.
            continue
        claimed[(work_order_id, part_id)] = Decimal(str(total or 0))

    if not claimed:
        return {}

    drawn: dict[tuple, Decimal] = {}
    usage_rows = (
        PartUsage.objects.filter(
            part_id__in=parts, work_order__status__in=OPEN_STATUSES
        )
        .exclude(work_order_id=exclude.pk)
        .values_list("work_order_id", "part_id")
        .annotate(total=Sum("qty"))
    )
    for work_order_id, part_id, total in usage_rows:
        drawn[(work_order_id, part_id)] = Decimal(str(total or 0))

    totals: dict = {}
    for (work_order_id, part_id), qty in claimed.items():
        outstanding = max(ZERO, qty - drawn.get((work_order_id, part_id), ZERO))
        totals[part_id] = totals.get(part_id, ZERO) + outstanding
    return totals


def _on_order(parts, *, work_order) -> dict:
    """Bought for this job and not yet received (FR-WO-2).

    Only lines tied to this work order count. A part on a general restocking
    order is not earmarked, and treating it as though it were would report a
    job as ready on the strength of a box somebody else may take.

    A **cart is not an order.** Nobody has been asked to send anything, so
    counting it here would clear a shortfall on the strength of a list — see
    `_on_a_list`, which reports drafts separately and does not cancel the
    shortfall.
    """
    from django.db.models import F, Sum

    from homeautoshop.purchasing.models import PurchaseLine, PurchaseStatus

    rows = (
        PurchaseLine.objects.filter(
            part_id__in=parts,
            purchase__work_order=work_order,
        )
        .exclude(
            purchase__status__in=(
                PurchaseStatus.CART,
                PurchaseStatus.CANCELLED,
                PurchaseStatus.RETURNED,
            )
        )
        .filter(qty_ordered__gt=F("qty_received"))
        .values_list("part_id")
        .annotate(ordered=Sum("qty_ordered"), received=Sum("qty_received"))
    )
    return {
        part_id: max(ZERO, Decimal(str(ordered or 0)) - Decimal(str(received or 0)))
        for part_id, ordered, received in rows
    }


def _on_a_list(parts, *, work_order) -> dict:
    """Drafted onto a cart for this job, which is not the same as ordered.

    Shown so that pressing "Order what is missing" leaves a visible trace,
    and deliberately not subtracted from the shortfall: a cart nobody has
    placed will not arrive.
    """
    from homeautoshop.purchasing.models import PurchaseLine, PurchaseStatus

    drafts: dict = {}
    lines = PurchaseLine.objects.filter(
        part_id__in=parts,
        purchase__work_order=work_order,
        purchase__status=PurchaseStatus.CART,
    ).select_related("purchase")
    for line in lines:
        drafts.setdefault(line.part_id, []).append(line.purchase)
    return drafts


def blocked_by_parts(limit: int = 20) -> list:
    """Open jobs that are short of something, for the planning screens.

    Sits beside the tool version for the same reason: it is the same kind of
    fact — work you cannot finish yet, and what is missing.
    """
    from .models import OPEN_STATUSES, WorkOrder

    candidates = (
        WorkOrder.objects.filter(status__in=OPEN_STATUSES)
        .filter(part_requirements__isnull=False)
        .distinct()[:limit]
    )
    blocked = []
    for work_order in candidates:
        readiness = for_work_order(work_order)
        if readiness.shortfalls:
            blocked.append((work_order, readiness))
    return blocked
