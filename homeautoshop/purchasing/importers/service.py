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
from decimal import ROUND_HALF_UP, Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext as _

from homeautoshop.core.models import ExternalRef
from homeautoshop.parts.models import (
    Part, PartCrossRef, PartFitment, PartKitItem, PartType,
)
from homeautoshop.purchasing.models import Purchase, PurchaseLine, Vendor

from . import orders as order_shapes
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
    #: Deliberately left out on the review screen — not a part, and not money
    #: the shop spent either. Reported rather than dropped, so the screen says
    #: what was left behind instead of quietly returning fewer lines than the
    #: document had.
    skipped: bool = False
    #: Recorded as a tooling expense instead of a part (OQ-4). Not an
    #: inventory record of any kind — see `run`.
    tooling: bool = False
    #: The kit this line was recorded as being inside, when it is a component.
    inside_kit: Part | None = None
    #: **How many of the part this line turned out to be for.**
    #:
    #: The document's own count unless somebody said otherwise on the review
    #: screen. Amazon sold a two-pack of relays as `1 of:` for $14.24, and one
    #: line-item is not one relay — so the count is asked for and the money is
    #: not, because the money is the one thing the invoice is unambiguous
    #: about. Recorded here rather than written back onto the parsed line so
    #: that `charged_minor` and every total built on it stay exactly what the
    #: document said, and the order still reconciles against its own printed
    #: total afterwards.
    units: Decimal = Decimal(1)

    @property
    def unit_cost_shown(self) -> str:
        """What one of them cost, once the count is known.

        Through the same `format_unit_price` the purchase screen uses, so a
        pack of three at $14.24 reads `4.7467` in the preview and `4.7467` on
        the line it becomes, rather than a rounded `$4.75` that multiplies back
        to a total the invoice never printed.
        """
        from homeautoshop.core.measurements import format_unit_price

        return format_unit_price(self.line.charged_minor, self.units)

    @property
    def repacked(self) -> bool:
        """Whether this line holds a different number than the vendor counted."""
        return Decimal(str(self.units)) != Decimal(str(self.line.quantity or 1))

    @property
    def status(self) -> str:
        if self.tooling:
            return _("tooling")
        if self.skipped:
            return _("left out")
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
    #: Money recorded as tooling rather than as parts, in minor units.
    tooling_recorded: int = 0
    vehicles_matched: dict = field(default_factory=dict)

    @property
    def shows_identifiers(self) -> bool:
        """Whether this document states a brand or a part number for anything.

        A parts supplier states both on every line. A general retailer states
        neither — an Amazon invoice has a product title and a marketplace
        seller and nothing else — so the Part column on the review screen has
        nothing to put in it, and a column of empty cells is width taken from
        the description, which is the only identity such a line has.
        """
        return any(
            outcome.line.brand or outcome.line.part_number
            for outcome in self.outcomes
        )

    @property
    def any_pack_notes(self) -> bool:
        """Whether any line's description talks about being several of a thing."""
        return any(outcome.line.pack_note for outcome in self.outcomes)

    @property
    def tooling(self):
        """What was recorded as tooling, as money rather than as cents."""
        from homeautoshop.core.measurements import Money

        return Money(self.tooling_recorded, "USD")
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


def _overheads_per_line(order) -> list[tuple[int, int, int]]:
    """Tax, shipping and discount split across the lines, exact to the cent.

    Through `split_kit_cost`, which is the largest-remainder allocator the kit
    split already uses, rather than a fresh one. Rounding each share on its own
    destroys cents — four equal shares of $436.54 come to $436.52 — and this
    application has spent enough of this week on money that arithmetic invented
    or lost.

    Allocated across **every charged line**, before anything is chosen. That is
    what lets the same numbers answer two questions without disagreeing: the
    purchase takes the shares of the lines kept as parts, and a tooling expense
    takes its own line's share, so what the order recorded adds back up to what
    the order cost.
    """
    from homeautoshop.parts.services import split_kit_cost

    weights = [
        Decimal(line.charged_minor + (line.core_minor or 0))
        if not line.is_kit_component
        else Decimal(0)
        for line in order.lines
    ]
    if not weights:
        return []
    taxes = split_kit_cost(order.tax_minor or 0, weights)
    shipping = split_kit_cost(order.shipping_minor or 0, weights)
    discounts = split_kit_cost(order.discount_minor or 0, weights)
    return list(zip(taxes, shipping, discounts))



#: How a tooling expense is filed, so re-reading the same order finds the one
#: it wrote last time instead of writing a second.
TOOLING_REF = "tooling-line"


def _record_tooling(order, line, overhead, *, vendor, source, index, user) -> int:
    """One expense row for one line, and nothing else.

    **The whole of what this is allowed to do.** NG-8 and NG-9 put tool and
    toolbox tracking permanently out of scope — what you own, what it is worth,
    which drawer it lives in — and WrenchLedger is where that lives. So there
    is no part created here, no `ShopTool` touched, no location, no serial, no
    model, and nothing to browse afterwards. What is recorded is that the shop
    spent money at a vendor on a day, which is what OQ-4 keeps
    `expense.category = tooling` for.

    `asset` is left null on purpose and is the same rule stated as data: a
    torque wrench is not a cost of the Civic. Per-vehicle rollups exclude
    tooling by default (`COST_INCLUDE_TOOLING`), and an expense attached to no
    vehicle cannot be pulled into one by a later change of mind about that
    setting.

    The line's share of tax and shipping goes with it. Leaving it on the
    purchase would put the tax on a tool into the parts spend, which is the
    thing the choice was made to avoid.
    """
    from homeautoshop.purchasing.models import Expense, ExpenseCategory

    tax, shipping, discount = overhead
    amount = line.charged_minor + (line.core_minor or 0) + tax + shipping - discount
    if amount <= 0:
        return 0

    expense = Expense.objects.create(
        # Not attached to a vehicle, and not to a work order either.
        asset=None,
        work_order=None,
        vendor=vendor,
        category=ExpenseCategory.TOOLING,
        amount_minor=amount,
        amount_currency="USD",
        incurred_on=order.ordered_on or timezone.localdate(),
        # What the receipt called it. A description of a transaction, which is
        # what this field is for — not a name anything is filed under.
        description=(line.description or line.label)[:200],
        created_by=user if getattr(user, "pk", None) else None,
    )
    ExternalRef.objects.update_or_create(
        source_system=source,
        source_instance_url="",
        external_type=TOOLING_REF,
        external_id=f"{order.order_number}:{index}",
        defaults={
            "entity_type": "Expense",
            "entity_id": expense.pk,
            "last_seen_at": timezone.now(),
        },
    )
    return amount


def _clear_tooling(order, source: str) -> None:
    """Drop what a previous read of this order recorded as tooling.

    The lines are already replaced rather than added to on a re-import, and
    these have to go the same way or changing your mind about one item leaves
    the first answer behind as a second expense.
    """
    from homeautoshop.purchasing.models import Expense

    refs = list(
        ExternalRef.objects.filter(
            source_system=source,
            external_type=TOOLING_REF,
            external_id__startswith=f"{order.order_number}:",
        )
    )
    if not refs:
        return
    Expense.objects.filter(pk__in=[ref.entity_id for ref in refs]).delete()
    ExternalRef.objects.filter(pk__in=[ref.pk for ref in refs]).delete()


@transaction.atomic
def run(
    order: order_shapes.ParsedOrder,
    *,
    dry_run: bool = True,
    user=None,
    keep: set[int] | None = None,
    as_tooling: set[int] | None = None,
    counts: dict[int, Decimal] | None = None,
) -> ImportReport:
    """Apply a parsed order. Rolls back entirely when `dry_run`.

    Three things can happen to a line, because a general retailer's order is a
    basket. `keep` names the ones that are **parts** (`None` means all of them,
    which is the truth about a parts supplier's document). `as_tooling` names
    the ones that were money the shop spent but are not parts. Anything in
    neither is left out entirely — it was never for the shop.

    **Tooling is a spend, and emphatically not an inventory.** NG-8 and NG-9
    put tool and toolbox tracking permanently out of scope, and WrenchLedger is
    where that lives; OQ-4 keeps `expense.category = tooling` as a first-class,
    always-exported category excluded from per-vehicle cost, because a torque
    wrench is not a cost of the Civic. So a tooling line becomes **one expense
    row and nothing else**: no part, no `ShopTool`, no location, no serial
    number, no model — a line on a receipt, not a thing the shop owns. The
    Expense's `description` is the item's name for the same reason the vendor
    printed it there, and `tests_orders` holds the boundary.

    The money follows the choice either way, because the alternative is a
    purchase claiming the shop paid $32.72 of tax on $14.24 of relays.

    `counts` says **how many of the part** a line turned out to be for, by
    index, where that is not the number the vendor put on it. A two-pack of
    relays is one line, one charge and two relays; without this the shelf
    gained one relay costing $14.24 and the second one did not exist. It moves
    no money at all — `extended_minor` is still the document's own figure, and
    the per-unit price is derived from it (FR-PUR-11) — so a corrected count
    changes what the shop has and never what the order cost.
    """
    report = ImportReport(order=order, dry_run=dry_run, warnings=list(order.warnings))

    vendor, _created = Vendor.objects.get_or_create(
        name=order.vendor_name or rockauto.VENDOR_NAME,
        defaults={"type": Vendor.Type.ONLINE, "url": order.vendor_url},
    )
    source = order.source or SOURCE

    existing = ExternalRef.lookup(source, "", "order", order.order_number)
    purchase = None
    if existing is not None:
        purchase = Purchase.all_objects.filter(pk=existing.entity_id).first()
        report.already_imported = purchase is not None

    if purchase is None:
        purchase = Purchase(vendor=vendor)
    purchase.order_number = order.order_number
    if order.ordered_on:
        purchase.ordered_on = order.ordered_on
    tooling = set(as_tooling or ())
    counted = dict(counts or {})
    overheads = _overheads_per_line(order)
    on_purchase = [
        index
        for index, line in enumerate(order.lines)
        if not line.is_kit_component and (keep is None or index in keep)
    ]
    purchase.tax_minor = sum(overheads[i][0] for i in on_purchase)
    purchase.shipping_minor = sum(overheads[i][1] for i in on_purchase)
    purchase.discount_minor = sum(overheads[i][2] for i in on_purchase)
    purchase.payment_method = order.payment_method
    if order.received_on:
        # The document says when it actually arrived, and that is the date the
        # return window runs from (FR-PUR-5) rather than the order date.
        purchase.received_on = order.received_on
    if keep is not None and len(on_purchase) < len(order.charged_lines):
        report.warnings.append(
            _(
                "Only some of this order became parts, so tax and shipping "
                "were shared out across the lines that did."
            )
        )
    for currency in ("tax_currency", "shipping_currency", "discount_currency"):
        setattr(purchase, currency, "USD")
    purchase.save()
    report.purchase = purchase

    if existing is None:
        ExternalRef.objects.create(
            source_system=source,
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
    _clear_tooling(order, source)

    seen_vehicles: dict[str, object] = {}
    # A `[Kit Component]` line says which kit it belongs to only by sitting
    # under it, so the last charged line is the box these fall into.
    kit_part: Part | None = None
    kit_quantity = Decimal(1)

    for index, line in enumerate(order.lines):
        if index in tooling:
            spent = _record_tooling(
                order, line, overheads[index], vendor=vendor, source=source,
                index=index, user=user,
            )
            report.tooling_recorded += spent
            report.outcomes.append(
                LineOutcome(
                    line=line, charged=False, tooling=True,
                    units=Decimal(str(counted.get(index, line.quantity) or 1)),
                )
            )
            continue
        if keep is not None and index not in keep:
            # Not a part, and not money the shop spent either. Recorded as
            # skipped so the report says what was left behind rather than
            # silently returning fewer lines than the document had.
            report.outcomes.append(
                LineOutcome(
                    line=line, charged=False, skipped=True,
                    units=Decimal(str(counted.get(index, line.quantity) or 1)),
                )
            )
            continue
        outcome = LineOutcome(line=line, charged=not line.is_kit_component)
        # A kit component keeps the vendor's count: it is not charged, and the
        # number that matters about it is how many are in the box, which the
        # kit's own quantity is already divided out of below.
        if line.is_kit_component:
            outcome.units = Decimal(str(line.quantity or 1))
        else:
            chosen = counted.get(index)
            outcome.units = (
                Decimal(str(chosen))
                if chosen is not None and Decimal(str(chosen)) > 0
                else Decimal(str(line.quantity or 1))
            )
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
                # How many of the part, which is the operator's answer where
                # they gave one and the document's count where they did not.
                qty_ordered=outcome.units,
                # `charged_minor`, which is the document's own extended figure
                # where it printed one and the multiplication where it printed
                # a price each. Doing it in the reader rather than here is what
                # lets NAPA's $182.39 survive: dividing it by five gallons and
                # multiplying back would land on $182.40.
                extended_minor=line.charged_minor,
                extended_currency="USD",
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


def read_and_run(
    upload, *, dry_run: bool = True, user=None, keep=None, as_tooling=None, counts=None
) -> ImportReport:
    """Read whatever this file turns out to be, then apply it.

    The reader is chosen by asking each one whether it recognizes the document
    rather than by making the operator say which vendor it came from — they
    know that, and being asked to classify a file before it can be read is a
    step that exists only because the software could not be bothered to look.
    """
    return run(
        order_shapes.read(upload),
        dry_run=dry_run, user=user, keep=keep, as_tooling=as_tooling, counts=counts,
    )
