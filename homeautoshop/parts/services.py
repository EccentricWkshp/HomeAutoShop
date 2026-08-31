"""
Stock consumption and part lookup (SPEC FR-INV-5, FR-PART-3/4).

FIFO by acquisition date, at each lot's *actual* cost. Averaging would be
simpler and would quietly lie about what a job cost when the same part was
bought twice at different prices.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from uuid import UUID

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q, Sum
from django.utils.translation import gettext_lazy as _

from .models import Part, PartFitment, PartUsage, StockLot, StockTransaction


class InsufficientStock(ValidationError):
    pass


@dataclass(slots=True)
class Consumption:
    """What a FIFO draw actually took, and what it cost."""

    usages: list = field(default_factory=list)
    total_minor: int = 0
    shortfall: Decimal = Decimal(0)

    @property
    def took_everything(self) -> bool:
        return self.shortfall == 0


@transaction.atomic
def consume(
    part: Part,
    qty,
    *,
    work_order=None,
    job_item=None,
    asset=None,
    installed_at=None,
    note: str = "",
    user=None,
    allow_short: bool = False,
    source: str = PartUsage.Source.FROM_STOCK,
) -> Consumption:
    """Draw `qty` of `part` from stock, oldest lot first.

    Returns one `PartUsage` per lot touched, so a draw that spans two purchase
    prices produces an honest cost rather than a blended guess.

    `work_order` is optional. A home shop has fitted a great many parts that
    were never a job in here, and "I installed that fuel pump, I bought it in
    June" is a complete and useful statement — one the shelf needs to hear, or
    the pump stays on it for ever. `asset` names the vehicle when it is known
    without a job to ask, which is what lets the fitment still record itself.
    """
    wanted = Decimal(str(qty))
    if wanted <= 0:
        raise ValidationError(_("Quantity must be positive."))

    vehicle = work_order.asset if work_order is not None else asset
    # Left out rather than passed as `None`, so the field's own default — today
    # — still applies when nobody said when.
    when = {"installed_at": installed_at} if installed_at else {}

    result = Consumption()
    lots = (
        StockLot.objects.select_for_update()
        .filter(part=part, qty_on_hand__gt=0)
        .order_by("acquired_on", "created_at")
    )

    remaining = wanted
    for lot in lots:
        if remaining <= 0:
            break
        take = min(Decimal(str(lot.qty_on_hand)), remaining)
        StockTransaction.record(
            lot, -take, StockTransaction.Reason.CONSUME,
            work_order=work_order, note=note, user=user,
        )
        usage = PartUsage.objects.create(
            work_order=work_order,
            job_item=job_item,
            asset=vehicle,
            part=part,
            qty=take,
            unit_cost_minor=lot.unit_cost_minor,
            unit_cost_currency=lot.unit_cost_currency or "USD",
            source=source,
            stock_lot=lot,
            created_by=user if getattr(user, "pk", None) else None,
            **when,
        )
        result.usages.append(usage)
        result.total_minor += usage.line_total_minor
        remaining -= take

    if remaining > 0:
        if not allow_short:
            raise InsufficientStock(
                _("Only %(have)s of %(part)s on the shelf; %(want)s needed.")
                % {"have": wanted - remaining, "part": part, "want": wanted}
            )
        # Bought-for-job: the shortfall is still installed, just not from stock.
        usage = PartUsage.objects.create(
            work_order=work_order,
            job_item=job_item,
            asset=vehicle,
            part=part,
            qty=remaining,
            source=PartUsage.Source.PURCHASED,
            created_by=user if getattr(user, "pk", None) else None,
            **when,
        )
        result.usages.append(usage)
        result.shortfall = remaining

    record_confirmed_fitment(part, vehicle)
    return result


def record_confirmed_fitment(part: Part, asset) -> PartFitment | None:
    """The shop's own history becomes its fitment database (FR-PART-3).

    A vendor's fitment claim is a claim. A part you actually installed on that
    vehicle is a fact, and it is the only fitment data that is ever fully
    trustworthy — so installing one records it without being asked.
    """
    if asset is None:
        return None
    fitment, created = PartFitment.objects.get_or_create(
        part=part,
        asset=asset,
        defaults={"confidence": PartFitment.Confidence.CONFIRMED},
    )
    if not created and fitment.confidence != PartFitment.Confidence.CONFIRMED:
        fitment.confidence = PartFitment.Confidence.CONFIRMED
        fitment.save()
    return fitment


def fits(asset) -> list[Part]:
    """Parts known to fit this asset, confirmed-installed first (FR-PART-4)."""
    candidates = PartFitment.objects.select_related("part").filter(
        Q(asset=asset)
        | (
            Q(asset__isnull=True)
            & (Q(make__iexact=asset.make) | Q(make=""))
            & (Q(model__iexact=asset.model) | Q(model=""))
        )
    )
    seen: dict = {}
    disproved: set = set()
    for fitment in candidates:
        if fitment.asset_id != asset.pk and not fitment.matches(asset):
            continue
        if fitment.confidence == PartFitment.Confidence.DOES_NOT_FIT:
            # Somebody held this part up against this vehicle and it was wrong.
            # That outranks any number of vendor claims for the same part, so
            # the part leaves the list rather than merely losing its place in
            # it — the whole value of recording the failure is not being
            # offered the part again.
            disproved.add(fitment.part_id)
            continue
        current = seen.get(fitment.part_id)
        if current is None or fitment.confidence == PartFitment.Confidence.CONFIRMED:
            seen[fitment.part_id] = fitment
    ordered = sorted(
        (fitment for part_id, fitment in seen.items() if part_id not in disproved),
        key=lambda f: (f.confidence != PartFitment.Confidence.CONFIRMED, str(f.part)),
    )
    return [f.part for f in ordered]


def find(query: str, limit: int = 25) -> list[Part]:
    """One search box, every identifier (FR-PART-1)."""
    query = (query or "").strip()
    if len(query) < 2:
        return []
    return list(
        Part.objects.filter(
            Q(name__icontains=query)
            | Q(manufacturer__icontains=query)
            | Q(part_number__icontains=query)
            | Q(category__icontains=query)
            | Q(cross_refs__value__icontains=query)
        ).distinct()[:limit]
    )


# --------------------------------------------------------------------------
# What a part chooser offers (SPEC §7.2, FR-PART-1)
# --------------------------------------------------------------------------

#: How many parts a chooser rests on before anybody has typed anything.
#: Deliberately small. The resting state is a shortlist, not a catalogue.
SHORTLIST = 8

#: And how many a search answers with. Past this the answer is a narrower
#: query, not a longer list — scrolling twenty-five rows is already a failure.
CHOOSER_LIMIT = 25


@dataclass(frozen=True, slots=True)
class PartChoice:
    """One row in a part chooser, and the two facts that place it.

    Both facts are shown, not just used for sorting. A chooser that quietly
    reorders itself teaches nobody anything; one that says "3 on hand · fits
    this vehicle" beside the name answers the question that was going to be
    asked next anyway.
    """

    part: Part
    on_hand: Decimal
    fits: bool

    @property
    def tier(self) -> int:
        """Lower sorts first.

        Fitment outranks stock on purpose. **Planning is the act of finding the
        gap between what is on the shelf and what has to be bought**, so a part
        that fits this vehicle and is not in stock is not noise to be filtered
        out — it is the single most useful row on the screen. Ranking it up and
        printing "none on hand" beside it makes the gap visible at the moment
        the choice is made, instead of after the requirement is saved.
        """
        if self.fits:
            return 0 if self.on_hand > 0 else 1
        if self.on_hand > 0:
            return 2
        # Brake cleaner and zip ties fit everything and are never planned for.
        return 3 if self.part.is_consumable else 4


def candidates(
    query: str = "",
    *,
    asset=None,
    exclude=(),
    limit: int | None = None,
) -> list[PartChoice]:
    """The parts worth offering, best first.

    Replaces handing a `<select>` five hundred rows of every part ever bought.
    The catalogue only grows — nothing is removed from it when a part is used
    up — so a chooser built by listing the table gets steadily less usable for
    exactly the people using the application most, and it does so silently.

    The fix is not a narrower catalogue. Nothing here is hidden: with something
    typed, this searches everything by every identifier `find` knows. What
    changes is what is offered *before* anybody types, which is a shortlist
    assembled from relevance — the parts that fit this vehicle, the parts on
    the shelf, and the consumables — rather than the first rows of the table.
    """
    query = (query or "").strip()
    excluded = _as_pks(exclude)

    fits: set = set()
    if asset is not None:
        fits = set(
            PartFitment.objects.filter(asset=asset)
            .exclude(confidence=PartFitment.Confidence.DOES_NOT_FIT)
            .values_list("part_id", flat=True)
        )

    if query:
        cap = limit or CHOOSER_LIMIT
        # Over-fetched so the ranking below has something to rank; a search
        # that matches more than this wants a better search, not more rows.
        pool = [part for part in find(query, limit=cap * 4) if part.pk not in excluded]
    else:
        cap = limit or SHORTLIST
        pool = _shortlist(fits, excluded, cap)

    on_hand = _shelf_quantities([part.pk for part in pool])
    choices = [
        PartChoice(
            part=part, on_hand=on_hand.get(part.pk, Decimal(0)), fits=part.pk in fits
        )
        for part in pool
    ]
    choices.sort(key=lambda choice: (choice.tier, choice.part.name.lower()))
    return choices[:cap]


def _shortlist(fits: set, excluded: set, cap: int) -> list[Part]:
    """The three groups that earn a place with nothing typed, each bounded.

    Queried as three capped groups rather than one `OR` because a single query
    would have to be sliced *before* anything is ranked, and the slice would
    then decide the shortlist on row order — dropping a part that fits this
    vehicle in favour of the tenth thing on the shelf.
    """
    groups = (
        Part.objects.filter(pk__in=fits),
        Part.objects.filter(stock_lots__qty_on_hand__gt=0).distinct(),
        Part.objects.filter(is_consumable=True),
    )
    picked: dict = {}
    for group in groups:
        for part in group.exclude(pk__in=excluded)[:cap]:
            picked.setdefault(part.pk, part)
    return list(picked.values())


def _shelf_quantities(pks) -> dict:
    """On-hand for every row in one query, because the chooser is a loop."""
    if not pks:
        return {}
    rows = (
        StockLot.objects.filter(part_id__in=pks)
        .values("part_id")
        .annotate(qty=Sum("qty_on_hand"))
    )
    return {row["part_id"]: row["qty"] or Decimal(0) for row in rows}


def _as_pks(values) -> set:
    """Ids from a query string, with anything unparseable dropped.

    `exclude` arrives from a URL. A malformed uuid there is a 500 from deep
    inside the ORM, which is a poor answer to somebody editing a link.
    """
    keep = set()
    for value in values or ():
        try:
            keep.add(UUID(str(value)))
        except (ValueError, AttributeError, TypeError):
            continue
    return keep


def resolve_part(data, field: str = "part"):
    """The part a submitted chooser meant, id first and typed name second.

    The chooser is a text box with a hidden id beside it, so with script the id
    is filled in and this is a primary-key lookup. **With no script the id is
    empty and the typed text is all there is**, and resolving it here is what
    keeps the unenhanced form working rather than merely present.

    Returns `(part, error)`. An ambiguous name is not resolved to its first
    match: picking one of four things somebody might have meant, silently, is
    worse than saying so.
    """
    part = Part.objects.filter(pk=data.get(field) or None).first()
    if part is not None:
        return part, ""

    typed = (data.get(f"{field}_query") or "").strip()
    if not typed:
        return None, _("Choose a part first.")

    exact = list(Part.objects.filter(name__iexact=typed)[:2])
    matches = exact or find(typed, limit=2)
    if len(matches) == 1:
        return matches[0], ""
    if not matches:
        return None, _("No part matches “%(typed)s”.") % {"typed": typed}
    return None, _("More than one part matches “%(typed)s”. Pick one from the list.") % {
        "typed": typed
    }


def restock_list() -> list[Part]:
    """Parts at or below their minimum (FR-INV-4)."""
    return [p for p in Part.objects.filter(min_quantity__isnull=False) if p.is_low]


def expiring_lots(days: int = 60) -> list[StockLot]:
    """Brake fluid, sealants and epoxy do expire (FR-INV-6)."""
    from datetime import timedelta

    from django.utils import timezone

    cutoff = timezone.localdate() + timedelta(days=days)
    return list(
        StockLot.objects.select_related("part")
        .filter(expires_on__isnull=False, expires_on__lte=cutoff, qty_on_hand__gt=0)
        .order_by("expires_on")
    )


def outstanding_cores() -> list[PartUsage]:
    """Uncollected core charges — the money a home shop most often loses (FR-PUR-4)."""
    return list(
        PartUsage.objects.select_related(
            "part", "work_order", "work_order__asset", "asset"
        )
        .filter(part__has_core=True, core_returned=False)
        .order_by("installed_at")
    )


def split_kit_cost(total_minor: int, shares: list[Decimal]) -> list[int]:
    """Divide a kit's landed cost across its contents, to the last cent.

    Largest-remainder, not rounding each share on its own. Four equal shares of
    a $436.54 kit are $109.135 each; rounded independently that is $436.52, and
    the two cents have been destroyed by arithmetic — which is exactly the kind
    of quiet loss the FIFO ledger exists to make impossible. The remainder goes
    to the largest shares, so the money lands where most of it already is.

    The shares are weights, not prices. Nobody knows what the drier inside a
    compressor kit cost; what is known is the kit's total, and any split is an
    estimate. This makes the estimate explicit, adjustable, and exact in sum.
    """
    weights = [Decimal(str(share)) for share in shares]
    total_weight = sum(weights)
    if not weights:
        return []
    if total_weight <= 0:
        # Every weight zero or negative: fall back to an even split rather than
        # dividing by zero. The operator gets a defensible number, not a crash.
        weights = [Decimal(1)] * len(weights)
        total_weight = Decimal(len(weights))

    exact = [Decimal(total_minor) * weight / total_weight for weight in weights]
    floors = [int(value) for value in exact]
    shortfall = total_minor - sum(floors)
    # Biggest fractional part first, ties broken by position so the result is
    # the same every time it is computed.
    order = sorted(
        range(len(exact)), key=lambda i: (-(exact[i] - floors[i]), i)
    )
    for i in order[:shortfall]:
        floors[i] += 1
    return floors


def kit_weights(items) -> tuple[list[Decimal], bool]:
    """Weights for splitting a kit's cost, and whether they came from prices.

    Prices when every row has one, because that is the split the shop actually
    means and the arithmetic is nobody's problem. Even shares the moment one is
    missing — not prices-with-the-unknown-at-zero, which would hand a drier
    whose price nobody recorded a landed cost of nothing and never say so.

    The flag is returned rather than inferred later, so the screen can state
    which of the two happened instead of leaving somebody to work it out from
    the numbers.
    """
    values = [item.line_value_minor for item in items]
    if values and all(value is not None for value in values) and sum(values) > 0:
        return [Decimal(value) for value in values], True
    return [Decimal(1)] * len(items), False


@transaction.atomic
def open_kit(lot: StockLot, quantity=1, *, location=None, user=None) -> list[StockLot]:
    """Turn boxed kits on the shelf into the parts inside them (FR-INV-9).

    Until this runs, a compressor kit is one thing you can pick up and the four
    parts inside it are not on the shelf, because they are not: they are in a
    box. Opening it is a real event in the shop and it is a real event here —
    the kit leaves stock, the contents arrive, and the ledger carries both
    halves under one reason so the pair reads as the single fact it is.

    The contents arrive as their own lots, at their share of what the kit
    actually cost, so consuming one later costs what it cost rather than
    nothing. Each new lot remembers the kit lot it came out of, which is what
    makes `close_kit` possible without a second table.
    """
    from .models import PartKitItem

    quantity = Decimal(str(quantity))
    if quantity <= 0:
        raise ValidationError(_("Open at least one."))
    if quantity > Decimal(str(lot.qty_on_hand)):
        raise ValidationError(
            _("There are only %(n)s of those on the shelf.") % {"n": lot.qty_on_hand}
        )

    items = list(PartKitItem.objects.filter(kit=lot.part).select_related("part"))
    if not items:
        raise ValidationError(
            _("Nothing is recorded as being inside this. Add its contents first.")
        )

    kit_cost = int(Decimal(str(lot.unit_cost_minor or 0)) * quantity)
    weights, _priced = kit_weights(items)
    per_item = split_kit_cost(kit_cost, weights)

    StockTransaction.record(
        lot, -quantity, StockTransaction.Reason.KIT_OPENED,
        note=str(lot.part), user=user,
    )

    released = []
    for item, share_minor in zip(items, per_item):
        count = Decimal(str(item.quantity)) * quantity
        # Per unit, because a lot carries a unit cost and not a total. `count`
        # is never zero — `clean()` refuses a kit item of zero — and the
        # rounding is to nearest rather than down, so a share that does not
        # divide evenly is out by at most half a cent a unit instead of
        # systematically shaving one off every time. That residual is inherent
        # to storing a unit cost, and is the only place this is inexact: the
        # split across components above is exact to the cent.
        unit_cost = int(
            (Decimal(share_minor) / count).quantize(Decimal(1), rounding=ROUND_HALF_UP)
        )
        new_lot = StockLot.objects.create(
            part=item.part,
            location=location or lot.location,
            qty_on_hand=0,
            unit_cost_minor=unit_cost,
            unit_cost_currency=lot.unit_cost_currency or "USD",
            purchase_line=lot.purchase_line,
            from_kit_lot=lot,
            created_by=user if getattr(user, "pk", None) else None,
        )
        StockTransaction.record(
            new_lot, count, StockTransaction.Reason.KIT_OPENED,
            note=str(lot.part), user=user,
        )
        released.append(new_lot)
    return released


@transaction.atomic
def close_kit(lot: StockLot, *, user=None) -> int:
    """Put an opened kit back in its box, when it was opened by mistake.

    Refused once any of the contents have moved, and the refusal is the honest
    answer rather than a limitation: a drier fitted to a car cannot go back into
    a box, and pretending otherwise would put a kit on the shelf that is missing
    a part. Untouched means exactly one movement — the one that released it.
    """
    released = list(lot.released_lots.all())
    if not released:
        raise ValidationError(_("Nothing was opened out of this."))

    stuck = []
    for released_lot in released:
        moves = list(released_lot.transactions.all())
        if len(moves) != 1 or Decimal(str(released_lot.qty_on_hand)) != moves[0].delta:
            stuck.append(released_lot.part)
    if stuck:
        raise ValidationError(
            _("These have already been used or counted: %(parts)s. The kit cannot go back together.")
            % {"parts": ", ".join(sorted(str(part) for part in stuck))}
        )

    kits = Decimal(0)
    for released_lot in released:
        count = Decimal(str(released_lot.qty_on_hand))
        item = released_lot.part.in_kits.filter(kit=lot.part).first()
        if item and Decimal(str(item.quantity)) > 0:
            kits = max(kits, count / Decimal(str(item.quantity)))
        StockTransaction.record(
            released_lot, -count, StockTransaction.Reason.KIT_CLOSED,
            note=str(lot.part), user=user,
        )

    StockTransaction.record(
        lot, kits, StockTransaction.Reason.KIT_CLOSED, note=str(lot.part), user=user
    )
    return int(kits)


@transaction.atomic
def cycle_count(lot: StockLot, counted, *, note: str, user=None) -> StockTransaction | None:
    """Reconcile a counted quantity by writing an adjustment, never by overwriting.

    FR-INV-7: the ledger stays the source of truth, so a discrepancy leaves a
    trace instead of quietly disappearing.
    """
    counted = Decimal(str(counted))
    delta = counted - Decimal(str(lot.qty_on_hand))
    if delta == 0:
        return None
    return StockTransaction.record(
        lot, delta, StockTransaction.Reason.ADJUST, note=note, user=user
    )
