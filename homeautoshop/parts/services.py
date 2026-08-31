"""
Stock consumption and part lookup (SPEC FR-INV-5, FR-PART-3/4).

FIFO by acquisition date, at each lot's *actual* cost. Averaging would be
simpler and would quietly lie about what a job cost when the same part was
bought twice at different prices.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q
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
    work_order,
    job_item=None,
    user=None,
    allow_short: bool = False,
    source: str = PartUsage.Source.FROM_STOCK,
) -> Consumption:
    """Draw `qty` of `part` from stock, oldest lot first.

    Returns one `PartUsage` per lot touched, so a draw that spans two purchase
    prices produces an honest cost rather than a blended guess.
    """
    wanted = Decimal(str(qty))
    if wanted <= 0:
        raise ValidationError(_("Quantity must be positive."))

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
            lot, -take, StockTransaction.Reason.CONSUME, work_order=work_order, user=user
        )
        usage = PartUsage.objects.create(
            work_order=work_order,
            job_item=job_item,
            part=part,
            qty=take,
            unit_cost_minor=lot.unit_cost_minor,
            unit_cost_currency=lot.unit_cost_currency or "USD",
            source=source,
            stock_lot=lot,
            created_by=user if getattr(user, "pk", None) else None,
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
            part=part,
            qty=remaining,
            source=PartUsage.Source.PURCHASED,
            created_by=user if getattr(user, "pk", None) else None,
        )
        result.usages.append(usage)
        result.shortfall = remaining

    record_confirmed_fitment(part, work_order.asset)
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
        PartUsage.objects.select_related("part", "work_order", "work_order__asset")
        .filter(part__has_core=True, core_returned=False)
        .order_by("installed_at")
    )


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
