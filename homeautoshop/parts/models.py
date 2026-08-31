"""
Parts, fitment, locations, and stock (SPEC §6.2, §7.4).

Two ideas carry most of the value here:

* **The shop's own history is its fitment database.** A vendor's fitment data is
  a claim; a part you actually installed on that vehicle is a fact. Consuming a
  part records the fact automatically (FR-PART-3).
* **Quantity on hand is a projection of an append-only ledger**, never a number
  someone typed. Any discrepancy is then auditable rather than a mystery.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.db.models import F, Sum
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from homeautoshop.core.measurements import PART_UNITS, UNIT_LABELS
from homeautoshop.core.models import AppendOnlyModel, BaseModel, RevisionedModel
from homeautoshop.core.money import money, money_columns


#: Everything a part can be measured in, grouped so the picker reads as the
#: shop thinks: what you count, what you weigh, what you pour, what you cut off
#: a roll. Built from the conversion table rather than typed out beside it, so a
#: unit that exists here is a unit the arithmetic actually knows.
UNIT_CHOICES = [
    (_("Counted"), [("each", UNIT_LABELS["each"])]),
    (_("Weight"), [(u, UNIT_LABELS[u]) for u in PART_UNITS["mass"]]),
    (_("Volume"), [(u, UNIT_LABELS[u]) for u in PART_UNITS["volume"]]),
    (_("Length"), [(u, UNIT_LABELS[u]) for u in PART_UNITS["length"]]),
]


#: What every quantity column in this module stores. Named once so a
#: conversion rounds to exactly what the database will accept.
QUANTITY_PLACES = Decimal("0.001")


class PartType(models.TextChoices):
    OEM = "oem", _("OEM")
    OE_SUPPLIER = "oe_supplier", _("OE supplier")
    AFTERMARKET = "aftermarket", _("Aftermarket")
    REMANUFACTURED = "remanufactured", _("Remanufactured")
    USED = "used", _("Used")


class Part(RevisionedModel):
    name = models.CharField(max_length=200)
    category = models.CharField(max_length=64, blank=True, db_index=True)
    manufacturer = models.CharField(max_length=80, blank=True)
    part_number = models.CharField(max_length=80, blank=True, db_index=True)
    part_type = models.CharField(max_length=20, choices=PartType.choices, default=PartType.AFTERMARKET)
    #: What one of these is measured in. Four choices were hard-coded here —
    #: each, litres, kilograms, feet — which is not a preference so much as a
    #: guess about somebody else's catalogue: R-134a is sold in cylinders by the
    #: pound and dispensed by the ounce or the half-kilogram, and none of those
    #: were sayable. The set now covers mass, volume, length and count, and a
    #: quantity may be *entered* in any unit of the same kind and converted.
    unit = models.CharField(max_length=8, default="each", choices=UNIT_CHOICES)
    is_consumable = models.BooleanField(default=False)
    has_core = models.BooleanField(default=False)
    core_value_minor, core_value_currency = money_columns("core_value", null=True)
    #: What one of these costs, as a fact about the part rather than about any
    #: particular one on the shelf. A stock lot records what a specific batch
    #: was actually paid for; this is the figure you would quote from memory,
    #: and it is what makes a kit's cost divisible without anybody working out
    #: proportions by hand (FR-INV-9).
    typical_cost_minor, typical_cost_currency = money_columns(
        "typical_cost", null=True, verbose_name=_("usual price")
    )
    hazmat_class = models.CharField(max_length=32, blank=True)
    min_quantity = models.DecimalField(
        max_digits=10, decimal_places=2, null=True, blank=True,
        help_text=_("Restock below this. Blank means never prompt."),
    )
    notes = models.TextField(blank=True)
    spec = models.JSONField(default=dict, blank=True, help_text=_("Viscosity, thread pitch, dimensions."))

    core_value = money("core_value")
    typical_cost = money("typical_cost")

    class Meta:
        ordering = ["name"]
        indexes = [models.Index(fields=["category", "name"])]

    def __str__(self) -> str:
        bits = [self.manufacturer, self.name, self.part_number]
        return " ".join(b for b in bits if b)

    @property
    def on_hand(self) -> Decimal:
        return self.stock_lots.aggregate(n=Sum("qty_on_hand"))["n"] or Decimal(0)

    @property
    def is_low(self) -> bool:
        return self.min_quantity is not None and self.on_hand < self.min_quantity

    @property
    def is_countable(self) -> bool:
        """Whether this part comes in whole ones."""
        return self.unit == "each"

    @property
    def unit_label(self) -> str:
        from homeautoshop.core.measurements import unit_label

        return unit_label(self.unit)

    @property
    def compatible_units(self) -> tuple[str, ...]:
        """Units a quantity of this part may be typed in.

        A pound of refrigerant and an ounce of it are the same substance, so the
        entry boxes offer both and the arithmetic joins them up. `each` converts
        to nothing, which is the honest answer for a gasket.
        """
        from homeautoshop.core.measurements import compatible_units

        return compatible_units(self.unit)

    def quantity_in_stock_units(self, value, unit: str | None = None) -> Decimal:
        """`value` of `unit`, as a quantity of this part.

        Stock is always held in the part's own unit — one number per part, so
        the shelf total never depends on which box somebody typed into. Converting
        at the edge is what lets the box accept what is actually written on the
        cylinder.
        """
        from homeautoshop.core.measurements import UnknownUnitError, convert

        amount = Decimal(str(value))
        if not unit or unit == self.unit:
            return amount
        try:
            converted = Decimal(str(convert(amount, unit, self.unit)))
        except UnknownUnitError:
            # A unit the table does not know. Taking the number at face value is
            # better than refusing the whole entry over the box beside it.
            return amount
        # Quantised, because a conversion factor is irrational-looking and the
        # ledger holds three decimal places: half a kilogram is
        # 1.102311310924387903614869007 lb, which the column refuses outright.
        # Rounding here rather than at the column means the number that lands is
        # the number this returns, and the residual — under a thousandth of a
        # pound — is smaller than anything a shop scale reads.
        return converted.quantize(QUANTITY_PLACES, rounding=ROUND_HALF_UP)

    @property
    def qty_step(self) -> str:
        """The smallest sensible amount of this part, for a quantity box.

        Storage stays at three decimal places whatever this says, because oil
        and hose genuinely come in fractions. What it fixes is the spinner: a
        gasket set stepping by a thousandth is arrows nobody can use and an
        offer to record 0.003 of a gasket. A string rather than a `Decimal`, so
        localisation cannot turn the attribute into `0,001`.
        """
        return "1" if self.is_countable else "0.001"

    def all_numbers(self) -> list[str]:
        return [self.part_number, *self.cross_refs.values_list("value", flat=True)]

    @property
    def known_cost_minor(self) -> int | None:
        """What this part costs, from the best source that has an answer.

        The stated price first, because somebody typed it on purpose. Then the
        newest lot, which is what one actually cost the last time one arrived.
        Then the newest purchase line, which covers a part bought but never
        stocked. `None` when nothing anywhere knows — a real answer, and the
        one that stops a kit's cost being divided by a number nobody supplied.

        Sorted in Python rather than by the database on purpose: `.all()` uses a
        prefetch when there is one, and `.order_by()` would throw it away and
        re-query — which on a list of two hundred parts is two hundred queries
        for a figure already in memory.
        """
        if self.typical_cost_minor is not None:
            return self.typical_cost_minor
        lots = [
            lot for lot in self.stock_lots.all() if lot.unit_cost_minor is not None
        ]
        if lots:
            newest = max(lots, key=lambda lot: (lot.acquired_on, lot.created_at))
            return newest.unit_cost_minor
        lines = [
            line
            for line in self.purchase_lines.all()
            if line.unit_price_minor is not None
        ]
        if lines:
            return max(lines, key=lambda line: line.created_at).unit_price_minor
        return None

    @property
    def known_cost(self):
        from homeautoshop.core.measurements import Money

        amount = self.known_cost_minor
        if amount is None:
            return None
        return Money(amount, self.typical_cost_currency or "USD")

    @property
    def is_kit(self) -> bool:
        """Whether anything is recorded as being inside this."""
        return self.kit_items.exists()

    def available_in_kits(self) -> list[dict]:
        """Kits on the shelf that contain this part, and how many it would yield.

        The reason the kit machinery exists at all. A compressor kit sitting in
        a box reads as zero compressors, zero driers and zero O-rings on every
        screen that matters — so the drier gets ordered again, and arrives next
        to the one already in the box. Nothing here is stock: it is an answer to
        "before you buy this, look in that box".
        """
        found = []
        for item in self.in_kits.select_related("kit"):
            on_hand = item.kit.on_hand
            if on_hand > 0:
                found.append(
                    {
                        "kit": item.kit,
                        "kits_on_hand": on_hand,
                        "quantity": Decimal(str(item.quantity)) * on_hand,
                    }
                )
        return found


class PartCrossRef(BaseModel):
    """The same water pump has five numbers; all five must find it (FR-PART-2)."""

    class System(models.TextChoices):
        OEM = "oem", _("OEM")
        INTERCHANGE = "interchange", _("Interchange")
        VENDOR_SKU = "vendor_sku", _("Vendor SKU")
        UPC = "upc", _("UPC")

    part = models.ForeignKey(Part, on_delete=models.CASCADE, related_name="cross_refs")
    system = models.CharField(max_length=16, choices=System.choices, default=System.INTERCHANGE)
    value = models.CharField(max_length=80, db_index=True)

    class Meta:
        ordering = ["system", "value"]
        constraints = [
            models.UniqueConstraint(
                fields=["part", "system", "value"],
                name="unique_crossref",
                condition=models.Q(deleted_at__isnull=True),
            ),
        ]

    def __str__(self) -> str:
        return f"{self.get_system_display()}: {self.value}"


class PartKitItem(BaseModel):
    """One part inside a kit, and how many of it (FR-INV-9).

    A kit is not its own kind of record — it is a `Part` that other parts are
    recorded as being inside. That keeps a kit buyable, stockable, countable and
    searchable by every mechanism that already exists, and means "is this a
    kit?" is a question about relationships rather than a flag somebody has to
    remember to set.

    The kit is what is on the shelf while the box is closed, and that is the
    honest answer: an unopened box is one thing you can pick up, not four things
    you would have to open it to reach. `Part.available_in_kits` is how the four
    stay findable in the meantime, and `open_kit` is what turns the box into
    them for real.
    """

    kit = models.ForeignKey(Part, on_delete=models.CASCADE, related_name="kit_items")
    part = models.ForeignKey(Part, on_delete=models.CASCADE, related_name="in_kits")
    quantity = models.DecimalField(max_digits=12, decimal_places=3, default=1)
    #: What one of these is worth inside this kit, in money. Blank means "use
    #: whatever the part itself says it costs", which is the usual case and the
    #: reason this is nullable rather than defaulted.
    #:
    #: This was a relative weight, and a weight is a number nobody can supply
    #: without a calculator: told that a compressor is a 70 and an O-ring a 1,
    #: the operator's actual question is "what do I put here?" and the honest
    #: answer was "work out the proportion yourself". Prices are the thing
    #: people already know, the vendor prints them on the order, and the
    #: proportions fall out of them.
    value_minor, value_currency = money_columns(
        "value", null=True, verbose_name=_("price each")
    )
    notes = models.CharField(max_length=200, blank=True)

    value = money("value")

    class Meta:
        ordering = ["part__name"]
        constraints = [
            models.UniqueConstraint(
                fields=["kit", "part"],
                name="unique_kit_item",
                condition=models.Q(deleted_at__isnull=True),
            ),
        ]

    def __str__(self) -> str:
        return f"{self.quantity:g} × {self.part}"

    @property
    def unit_value_minor(self) -> int | None:
        """The price of one, stated here or taken from the part."""
        if self.value_minor is not None:
            return self.value_minor
        return self.part.known_cost_minor

    @property
    def unit_value(self):
        from homeautoshop.core.measurements import Money

        amount = self.unit_value_minor
        return None if amount is None else Money(amount, self.value_currency or "USD")

    @property
    def line_value_minor(self) -> int | None:
        """The price of everything this row puts in the box.

        Six O-rings at a dollar are worth six dollars against a compressor's
        hundred and seventy-five, and the split has to know that — the weight
        this replaced ignored quantity entirely.
        """
        each = self.unit_value_minor
        if each is None:
            return None
        return int(Decimal(each) * Decimal(str(self.quantity)))

    def clean(self):
        super().clean()
        if self.kit_id == self.part_id:
            raise ValidationError({"part": _("A kit cannot contain itself.")})
        if Decimal(str(self.quantity or 0)) <= 0:
            raise ValidationError({"quantity": _("A kit holds at least one of each part.")})
        # A kit may contain a kit, so the loop has to be walked rather than
        # guessed at — the same shape as Location's parent check, and for the
        # same reason: a cycle here is an infinite recursion at open time.
        seen, frontier = set(), [self.kit_id]
        while frontier:
            current = frontier.pop()
            if current in seen:
                continue
            seen.add(current)
            frontier.extend(
                PartKitItem.objects.filter(part_id=current).values_list("kit_id", flat=True)
            )
        if self.part_id in seen:
            raise ValidationError({"part": _("That would put this kit inside itself.")})


class PartFitment(BaseModel):
    """What a part fits — and how much that claim is worth (FR-PART-3/4)."""

    class Confidence(models.TextChoices):
        CONFIRMED = "confirmed_installed", _("Confirmed — installed on this vehicle")
        #: Kept rather than deleted, and the distinction earns its row. A
        #: vendor's claim that a part fits is re-recorded every time that order
        #: is imported again, so deleting a disproved one is undone by the next
        #: import. This says *we tried it and it did not fit*, which outranks
        #: the claim and survives it. It is also the more useful fact: knowing
        #: a part does not fit is what stops it being ordered twice.
        DOES_NOT_FIT = "does_not_fit", _("Does not fit — tried it")
        VENDOR = "stated_by_vendor", _("Stated by vendor")
        UNVERIFIED = "unverified", _("Unverified")

    part = models.ForeignKey(Part, on_delete=models.CASCADE, related_name="fitments")
    asset = models.ForeignKey(
        "assets.Asset", null=True, blank=True, on_delete=models.CASCADE, related_name="fitments"
    )
    make = models.CharField(max_length=64, blank=True)
    model = models.CharField(max_length=64, blank=True)
    year_from = models.PositiveIntegerField(null=True, blank=True)
    year_to = models.PositiveIntegerField(null=True, blank=True)
    engine_code = models.CharField(max_length=40, blank=True)
    position = models.CharField(max_length=32, blank=True)
    confidence = models.CharField(
        max_length=24, choices=Confidence.choices, default=Confidence.UNVERIFIED
    )
    notes = models.TextField(blank=True)

    class Meta:
        # Confirmed-installed first: the shop's own history outranks a claim.
        ordering = ["confidence", "make", "model"]

    @property
    def vehicle(self) -> str:
        """The vehicle half on its own: one of your assets, or a description.

        A fitment names two things, and which of them is worth printing depends
        entirely on where you are standing. On the part's own page the part is
        already the heading, so `__str__` there rendered every row as "GPD A/C
        Compressor & Component Kit 9642644B fits Suzuki Aerio 2004–2004" — the
        page title, the word "fits", and then the four words anybody came for.
        Read quickly it looks like the part fits *itself* and a vehicle.
        """
        if self.asset_id:
            return str(self.asset)
        span = f"{self.year_from or ''}–{self.year_to or ''}".strip("–")
        if self.year_from and self.year_from == self.year_to:
            # "2004–2004" is a range of one, written the long way.
            span = str(self.year_from)
        return " ".join(bit for bit in (self.make, self.model, span) if bit)

    @property
    def qualifiers(self) -> str:
        """Engine and position, when the fitment is narrower than the model."""
        return " · ".join(bit for bit in (self.engine_code, self.position) if bit)

    def __str__(self) -> str:
        return f"{self.part} fits {self.vehicle}".strip()

    def matches(self, asset) -> bool:
        if self.asset_id:
            return self.asset_id == asset.pk
        if self.make and self.make.lower() != (asset.make or "").lower():
            return False
        if self.model and self.model.lower() != (asset.model or "").lower():
            return False
        if asset.year:
            if self.year_from and asset.year < self.year_from:
                return False
            if self.year_to and asset.year > self.year_to:
                return False
        return bool(self.make or self.model)


class Location(BaseModel):
    """Mirrors the physical shop. Scanning a bin label opens its contents."""

    name = models.CharField(max_length=80)
    parent = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.CASCADE, related_name="children"
    )
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ["name"]

    def __str__(self) -> str:
        return self.path

    @property
    def path(self) -> str:
        node, parts = self, []
        seen = set()
        while node and node.pk not in seen:
            seen.add(node.pk)
            parts.append(node.name)
            node = node.parent
        return " / ".join(reversed(parts))

    def clean(self):
        super().clean()
        node = self.parent
        while node:
            if node.pk == self.pk:
                raise ValidationError({"parent": _("A location cannot contain itself.")})
            node = node.parent


class StockLot(BaseModel):
    """A quantity of one part acquired at one price, at one time, in one place.

    Lot-level costing is what makes consumption value correctly when the same
    part was bought twice at different prices (FR-INV-5).
    """

    part = models.ForeignKey(Part, on_delete=models.CASCADE, related_name="stock_lots")
    location = models.ForeignKey(
        Location, null=True, blank=True, on_delete=models.SET_NULL, related_name="stock_lots"
    )
    qty_on_hand = models.DecimalField(max_digits=12, decimal_places=3, default=0)
    unit_cost_minor, unit_cost_currency = money_columns("unit_cost", null=True)
    purchase_line = models.ForeignKey(
        "purchasing.PurchaseLine", null=True, blank=True, on_delete=models.SET_NULL, related_name="lots"
    )
    #: The kit lot this came out of, when it came out of one. Carried so that
    #: opening a kit by mistake is undoable without a second table recording
    #: which lots belonged to which opening — the lots say so themselves.
    from_kit_lot = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.SET_NULL, related_name="released_lots"
    )
    acquired_on = models.DateField(default=timezone.localdate)
    expires_on = models.DateField(
        null=True, blank=True, help_text=_("Brake fluid, RTV and epoxy do expire.")
    )

    unit_cost = money("unit_cost")

    class Meta:
        ordering = ["acquired_on", "created_at"]  # FIFO
        indexes = [models.Index(fields=["part", "acquired_on"])]

    def __str__(self) -> str:
        return f"{self.part} × {self.qty_on_hand} @ {self.location or '—'}"

    @property
    def is_expiring(self) -> bool:
        if not self.expires_on:
            return False
        return (self.expires_on - timezone.localdate()).days <= 60


class StockTransaction(AppendOnlyModel):
    """The append-only ledger behind `qty_on_hand` (FR-INV-1).

    Quantity on hand is a projection of this, never a number someone typed, so
    a discrepancy is always traceable to the movement that caused it.
    """

    class Reason(models.TextChoices):
        RECEIVE = "receive", _("Received")
        CONSUME = "consume", _("Used on a job")
        ADJUST = "adjust", _("Cycle count adjustment")
        RETURN = "return", _("Returned to vendor")
        SCRAP = "scrap", _("Scrapped")
        FOUND = "found", _("Found")
        #: A receipt that should not have been recorded, taken back out. Its
        #: own reason rather than an `adjust`, because the two are different
        #: facts and only one of them is about the shelf: a cycle count says
        #: the shelf disagreed with the book, and this says the book was
        #: written wrong. Filing a correction as a count would put a discrepancy
        #: in the record that nobody ever counted.
        UNRECEIVE = "unreceive", _("Receipt reversed")
        #: One event, two signs. Opening a kit takes the box off the shelf and
        #: puts its contents on it, and both halves are the same fact — so they
        #: share a reason and the delta says which side of it a row is. Reading
        #: the ledger, a matched `-1 kit` and `+1 drier` under "Kit opened" is
        #: the event; two different reasons would be two events to reconcile.
        KIT_OPENED = "kit_opened", _("Kit opened")
        KIT_CLOSED = "kit_closed", _("Kit put back together")

    stock_lot = models.ForeignKey(StockLot, on_delete=models.CASCADE, related_name="transactions")
    delta = models.DecimalField(max_digits=12, decimal_places=3)
    reason = models.CharField(max_length=12, choices=Reason.choices)
    work_order = models.ForeignKey(
        "work.WorkOrder", null=True, blank=True, on_delete=models.SET_NULL, related_name="stock_moves"
    )
    note = models.TextField(blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.get_reason_display()} {self.delta:+}"

    def clean(self):
        super().clean()
        if self.reason == self.Reason.ADJUST and not self.note.strip():
            raise ValidationError(
                {"note": _("A cycle count adjustment needs a reason — silent corrections hide problems.")}
            )

    @classmethod
    @transaction.atomic
    def record(cls, lot: StockLot, delta, reason: str, *, work_order=None, note: str = "", user=None):
        """Move stock and reproject the lot in one transaction."""
        entry = cls(
            stock_lot=lot,
            delta=Decimal(str(delta)),
            reason=reason,
            work_order=work_order,
            note=note,
            created_by=user if getattr(user, "pk", None) else None,
        )
        entry.full_clean(exclude=["created_by"])
        entry.save()
        StockLot.objects.filter(pk=lot.pk).update(qty_on_hand=F("qty_on_hand") + entry.delta)
        lot.refresh_from_db(fields=["qty_on_hand"])
        return entry


class PartUsage(BaseModel):
    """A part consumed on a job item, with the warranty clock that follows it."""

    class Source(models.TextChoices):
        FROM_STOCK = "from_stock", _("From the shelf")
        PURCHASED = "purchased_for_job", _("Bought for this job")
        REUSED = "reused", _("Reused")
        WARRANTY = "warranty", _("Warranty replacement")

    job_item = models.ForeignKey(
        "work.JobItem", null=True, blank=True, on_delete=models.CASCADE, related_name="part_usages"
    )
    #: Nullable, because plenty of what a home shop has fitted was never a job
    #: here. "I put that fuel pump in, I bought it in June" is a true and useful
    #: statement, and requiring a work order to record it means either inventing
    #: one or — far more likely — leaving the part on the shelf for ever.
    work_order = models.ForeignKey(
        "work.WorkOrder", null=True, blank=True,
        on_delete=models.CASCADE, related_name="part_usages",
    )
    #: Which vehicle, when there is no work order to ask. Usually the one thing
    #: somebody does remember, and it is what makes the fitment record itself.
    asset = models.ForeignKey(
        "assets.Asset", null=True, blank=True,
        on_delete=models.SET_NULL, related_name="part_usages",
    )
    part = models.ForeignKey(Part, on_delete=models.PROTECT, related_name="usages")
    qty = models.DecimalField(max_digits=12, decimal_places=3, default=1)
    unit_cost_minor, unit_cost_currency = money_columns("unit_cost", null=True)
    source = models.CharField(max_length=20, choices=Source.choices, default=Source.FROM_STOCK)
    stock_lot = models.ForeignKey(
        StockLot, null=True, blank=True, on_delete=models.SET_NULL, related_name="usages"
    )
    core_returned = models.BooleanField(default=False)
    core_returned_on = models.DateField(null=True, blank=True)
    warranty_months = models.PositiveIntegerField(null=True, blank=True)
    warranty_distance = models.PositiveIntegerField(null=True, blank=True)
    installed_at = models.DateField(default=timezone.localdate)

    unit_cost = money("unit_cost")

    class Meta:
        ordering = ["-installed_at"]

    def __str__(self) -> str:
        return f"{self.qty} × {self.part}"

    @property
    def vehicle(self):
        """The vehicle this went on, from the job or from the row itself."""
        if self.work_order_id is not None:
            return self.work_order.asset
        return self.asset

    @property
    def line_total_minor(self) -> int:
        if self.unit_cost_minor is None:
            return 0
        return int(Decimal(self.unit_cost_minor) * Decimal(str(self.qty)))

    @property
    def warranty_expires_on(self):
        if not self.warranty_months:
            return None
        from datetime import timedelta

        return self.installed_at + timedelta(days=30 * self.warranty_months)

    @property
    def under_warranty(self) -> bool:
        expiry = self.warranty_expires_on
        return bool(expiry and expiry >= timezone.localdate())

    @property
    def owes_core(self) -> bool:
        """An uncollected core charge is the money a home shop most often loses."""
        return self.part.has_core and not self.core_returned
