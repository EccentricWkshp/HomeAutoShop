"""
Turning a read order into records (FR-PUR-1, FR-PART-2, FR-PART-3, §6.2).

The parser says what the document contains. This decides what that means for a
shop that already has parts, vendors and vehicles in it — which is a different
and more careful question, and it is where an import earns trust or loses it.

Three rules, all of them about not overwriting what somebody already knows:

* **Match before creating.** A part is found by manufacturer and part number
  first, then by any cross-reference carrying that number. Only a genuine miss
  creates a row, and every match is reported so the review screen can show
  which lines are new and which are the shelf you already have.
* **Never silently re-import.** `external_ref` (§6.2) carries the order number,
  so running the same file twice updates one purchase instead of making a
  second. That is the same mechanism the LubeLogger import uses and for the
  same reason.
* **Fitment is a claim with a source.** RockAuto grouped these parts under
  `2004 SUZUKI AERIO 2.3L L4` because that is the vehicle they were looked up
  against, which is worth recording — but it is the vendor's claim, not a
  confirmed installation, and it is stored as `stated_by_vendor` accordingly
  (FR-PART-4). It becomes `confirmed_installed` when the part is actually
  fitted to a work order, which is somebody's own observation.

`run` is a dry run unless told otherwise, and the dry run and the real one walk
exactly the same code — a preview produced by a second implementation is a
preview of nothing.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils.translation import gettext as _

from homeautoshop.core.models import ExternalRef
from homeautoshop.parts.models import (
    Part, PartCrossRef, PartFitment, PartKitItem, PartType,
)
from homeautoshop.purchasing.models import Purchase, PurchaseLine, Vendor

from . import rockauto

log = logging.getLogger(__name__)

SOURCE = "rockauto"

#: `2004 SUZUKI AERIO 2.3L L4` → year, make, model, engine.
VEHICLE = re.compile(
    r"^(?P<year>\d{4})\s+(?P<make>[A-Z][A-Z0-9-]*)\s+(?P<rest>.+?)"
    r"(?:\s+(?P<engine>[\d.]+L(?:\s+\S+)?))?$"
)


@dataclass(slots=True)
class LineOutcome:
    line: rockauto.OrderLine
    part: Part | None = None
    created_part: bool = False
    matched_on: str = ""
    fitment_for: str = ""
    charged: bool = True
    #: The kit this line was recorded as being inside, when it is a component.
    inside_kit: Part | None = None

    @property
    def status(self) -> str:
        if self.created_part:
            return _("new part")
        if self.part is not None:
            return _("matched %(how)s") % {"how": self.matched_on}
        return _("no part")


@dataclass(slots=True)
class ImportReport:
    order: rockauto.ParsedOrder
    dry_run: bool = True
    purchase: Purchase | None = None
    already_imported: bool = False
    outcomes: list[LineOutcome] = field(default_factory=list)
    parts_created: int = 0
    parts_matched: int = 0
    fitments_recorded: int = 0
    kit_items_recorded: int = 0
    vehicles_matched: dict = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


def parse_vehicle(text: str) -> dict:
    """Split a group heading into something the fitment table can hold."""
    found = VEHICLE.match((text or "").strip())
    if not found:
        return {}
    rest = found.group("rest").strip()
    engine = (found.group("engine") or "").strip()
    return {
        "year": int(found.group("year")),
        "make": found.group("make").title(),
        "model": rest.title(),
        "engine": engine,
    }


def _find_asset(details: dict):
    """The operator's own vehicle this heading refers to, if there is one."""
    from homeautoshop.assets.models import Asset

    if not details:
        return None
    candidates = Asset.objects.filter(
        make__iexact=details["make"], model__iexact=details["model"]
    )
    if details.get("year"):
        exact = candidates.filter(year=details["year"])
        if exact.exists():
            candidates = exact
    return candidates.first()


def _record_kit_item(kit: Part, part: Part, line, kit_quantity: Decimal) -> bool:
    """Record a `[Kit Component]` line as a part inside the kit above it.

    The confirmation states both halves of what a kit needs and neither was
    being kept: which parts are in the box, and — in the `Price EA` column that
    is printed even though the line is not charged — what the vendor says each
    one is worth. Three components at $8.46, $175.26 and $175.07 add up to the
    kit's own $358.79, so those prices are the cost split (FR-INV-9) already
    made by the only party who knows it. Anything else is somebody guessing at
    a number the document was holding all along.

    The price is recorded twice on purpose, because it answers two questions.
    On the kit row it is what the vendor charged for that component *in this
    box*, which is what the cost split uses. On the part it is the only price
    that part has anywhere — a component line is never charged, so it produces
    no purchase line — and without it the part would show no price at all on
    its own page.

    What survives the split is the ratio rather than the amount: a kit's landed
    cost carries tax and shipping these figures do not, so the components add up
    to the kit's list price and not to what it actually cost to get here.

    Quantities on this document are counts of what ships, so two of a kit prints
    two of each component; per-box is that divided by the kit's own quantity.
    """
    if kit.pk == part.pk:
        return False

    # The part's own price, learned wherever it was still unknown — the same
    # rule as the core charge below: fill a blank, never overwrite an answer
    # somebody already gave.
    if line.unit_price_minor and part.typical_cost_minor is None:
        part.typical_cost_minor = line.unit_price_minor
        part.typical_cost_currency = "USD"
        part.save(update_fields=["typical_cost_minor", "typical_cost_currency", "updated_at"])

    # `all_objects`: a component the operator has removed from the kit stays
    # removed, for the same reason a fitment they deleted stays deleted.
    if PartKitItem.all_objects.filter(kit=kit, part=part).exists():
        return False

    per_box = Decimal(str(line.quantity or 1))
    if kit_quantity and kit_quantity > 0:
        per_box = (per_box / kit_quantity).quantize(Decimal("0.001"))
    if per_box <= 0:
        return False

    item = PartKitItem(
        kit=kit,
        part=part,
        quantity=per_box,
        # Left blank when the column was blank, so the row falls back to the
        # part's own price rather than claiming the vendor said zero.
        value_minor=line.unit_price_minor or None,
        value_currency="USD",
        notes=_("From the vendor's kit listing."),
    )
    try:
        item.clean()
    except ValidationError:
        # A cycle, which a kit listing should never contain. Skip the row rather
        # than refuse the whole import over one line of somebody's receipt.
        return False
    item.save()
    return True


def _find_part(line: rockauto.OrderLine) -> tuple[Part | None, str]:
    number = (line.part_number or "").strip()
    if not number:
        return None, ""

    exact = Part.objects.filter(
        part_number__iexact=number, manufacturer__iexact=line.brand
    ).first()
    if exact is not None:
        return exact, _("on brand and part number")

    same_number = Part.objects.filter(part_number__iexact=number).first()
    if same_number is not None:
        return same_number, _("on part number")

    # A number this shop already records as an interchange for something else.
    cross = (
        PartCrossRef.objects.filter(value__iexact=number)
        .select_related("part")
        .first()
    )
    if cross is not None:
        return cross.part, _("on a cross-reference")
    return None, ""


@transaction.atomic
def run(order: rockauto.ParsedOrder, *, dry_run: bool = True, user=None) -> ImportReport:
    """Apply a parsed order. Rolls back entirely when `dry_run`."""
    report = ImportReport(order=order, dry_run=dry_run, warnings=list(order.warnings))

    vendor, _created = Vendor.objects.get_or_create(
        name=rockauto.VENDOR_NAME,
        defaults={"type": Vendor.Type.ONLINE, "url": "https://www.rockauto.com"},
    )

    existing = ExternalRef.lookup(SOURCE, "", "order", order.order_number)
    purchase = None
    if existing is not None:
        purchase = Purchase.all_objects.filter(pk=existing.entity_id).first()
        report.already_imported = purchase is not None

    if purchase is None:
        purchase = Purchase(vendor=vendor)
    purchase.order_number = order.order_number
    if order.ordered_on:
        purchase.ordered_on = order.ordered_on
    purchase.tax_minor = order.tax_minor
    purchase.shipping_minor = order.shipping_minor
    purchase.discount_minor = order.discount_minor
    purchase.payment_method = order.payment_method
    for currency in ("tax_currency", "shipping_currency", "discount_currency"):
        setattr(purchase, currency, "USD")
    purchase.save()
    report.purchase = purchase

    if existing is None:
        ExternalRef.objects.create(
            source_system=SOURCE,
            source_instance_url="",
            external_type="order",
            external_id=order.order_number,
            entity_type="Purchase",
            entity_id=purchase.pk,
            source_hash=ExternalRef.hash_of({"total": order.total_minor}),
        )

    # Re-importing replaces the lines rather than adding a second copy of each.
    # Safe because a line carries no history of its own until it is received,
    # and a received purchase is refused below.
    if report.already_imported:
        received = [line for line in purchase.lines.all() if line.qty_received]
        if received:
            report.warnings.append(
                _(
                    "This order is already here and %(n)s of its lines have been "
                    "received, so nothing was changed."
                )
                % {"n": len(received)}
            )
            if dry_run:
                transaction.set_rollback(True)
            return report
        purchase.lines.all().delete()

    seen_vehicles: dict[str, object] = {}
    # A `[Kit Component]` line says which kit it belongs to only by sitting
    # under it, so the last charged line is the box these fall into.
    kit_part: Part | None = None
    kit_quantity = Decimal(1)

    for line in order.lines:
        outcome = LineOutcome(line=line, charged=not line.is_kit_component)
        part, how = _find_part(line)

        if part is None:
            part = Part.objects.create(
                name=line.description or line.label,
                manufacturer=line.brand,
                part_number=line.part_number,
                part_type=PartType.AFTERMARKET,
                has_core=bool(line.core_minor),
                created_by=user if getattr(user, "pk", None) else None,
            )
            outcome.created_part = True
            report.parts_created += 1
        else:
            outcome.matched_on = how
            report.parts_matched += 1
            # A core charge on the invoice is proof the part has one, which is
            # worth learning even when the part was already known.
            if line.core_minor and not part.has_core:
                part.has_core = True
                part.save(update_fields=["has_core", "updated_at"])

        outcome.part = part
        PartCrossRef.objects.get_or_create(
            part=part, system=PartCrossRef.System.VENDOR_SKU, value=line.part_number[:80]
        )

        if line.vehicle:
            details = parse_vehicle(line.vehicle)
            if line.vehicle not in seen_vehicles:
                seen_vehicles[line.vehicle] = _find_asset(details)
            asset = seen_vehicles[line.vehicle]
            if details:
                # `all_objects`, so a fitment somebody has already dealt with is
                # not recorded a second time. Re-importing the same order was
                # undoing the operator's own work twice over: a fitment they
                # deleted came back, because a soft-deleted row is invisible to
                # the default manager and `get_or_create` therefore made a new
                # one. Deciding a vendor's claim is wrong should survive the
                # next import of the order that made it.
                existing = PartFitment.all_objects.filter(
                    part=part,
                    asset=asset,
                    make=details["make"],
                    model=details["model"],
                    year_from=details["year"],
                    year_to=details["year"],
                ).first()
                if existing is None:
                    PartFitment.objects.create(
                        part=part,
                        asset=asset,
                        make=details["make"],
                        model=details["model"],
                        year_from=details["year"],
                        year_to=details["year"],
                        engine_code=details["engine"],
                        # The vendor's claim, not an installation somebody saw.
                        confidence=PartFitment.Confidence.VENDOR,
                        notes=_("From %(vendor)s order %(number)s.")
                        % {"vendor": rockauto.VENDOR_NAME, "number": order.order_number},
                    )
                    report.fitments_recorded += 1
                outcome.fitment_for = line.vehicle

        if line.is_kit_component:
            if kit_part is not None:
                outcome.inside_kit = kit_part
                if _record_kit_item(kit_part, part, line, kit_quantity):
                    report.kit_items_recorded += 1
        else:
            kit_part, kit_quantity = part, Decimal(str(line.quantity or 1))

        if outcome.charged:
            PurchaseLine.objects.create(
                purchase=purchase,
                part=part,
                description_as_ordered=(line.description or line.label)[:200],
                qty_ordered=line.quantity,
                unit_price_minor=line.unit_price_minor,
                unit_price_currency="USD",
                core_charge_minor=line.core_minor or 0,
                core_charge_currency="USD",
            )

        report.outcomes.append(outcome)

    report.vehicles_matched = {
        heading: asset for heading, asset in seen_vehicles.items()
    }

    if dry_run:
        # The preview and the real thing are the same code path. Rolling back
        # is the only difference, so a preview cannot disagree with what a
        # commit would do.
        transaction.set_rollback(True)
    return report


def read_and_run(upload, *, dry_run: bool = True, user=None) -> ImportReport:
    return run(rockauto.parse(upload), dry_run=dry_run, user=user)
