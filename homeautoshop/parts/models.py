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

from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.db.models import F, Sum
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from homeautoshop.core.models import AppendOnlyModel, BaseModel, RevisionedModel
from homeautoshop.core.money import money, money_columns


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
    unit = models.CharField(
        max_length=8,
        default="each",
        choices=[("each", _("each")), ("L", _("litres")), ("kg", _("kilograms")), ("ft", _("feet"))],
    )
    is_consumable = models.BooleanField(default=False)
    has_core = models.BooleanField(default=False)
    core_value_minor, core_value_currency = money_columns("core_value", null=True)
    hazmat_class = models.CharField(max_length=32, blank=True)
    min_quantity = models.DecimalField(
        max_digits=10, decimal_places=2, null=True, blank=True,
        help_text=_("Restock below this. Blank means never prompt."),
    )
    notes = models.TextField(blank=True)
    spec = models.JSONField(default=dict, blank=True, help_text=_("Viscosity, thread pitch, dimensions."))

    core_value = money("core_value")

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

    def all_numbers(self) -> list[str]:
        return [self.part_number, *self.cross_refs.values_list("value", flat=True)]


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
    work_order = models.ForeignKey(
        "work.WorkOrder", on_delete=models.CASCADE, related_name="part_usages"
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
