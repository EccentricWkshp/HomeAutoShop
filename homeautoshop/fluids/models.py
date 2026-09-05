"""
Oil and fluid analysis (SPEC §7.9a, FR-FLU-*, roadmap R-5).

A lab report is a document the shop *receives*, not an inspection it performs,
which is why this is neither a DVI template nor a spec sheet. What makes it
worth a table of its own is that the numbers only mean something in a series:
one report says almost nothing, and four say when the bearings started going.

**The sample carries the interval, and that is the whole design.** `usage_at_sample`
is where the odometer stood; `fluid_usage` is how far the *oil* had gone. The
second one is what nearly every home sample omits and what every comparison
needs — 24 ppm of iron on 3,000 miles of oil and 24 ppm on 9,000 are different
statements about the same engine. A sample without it is still recorded and
still shown; it is simply left out of the rate, and the screen says which
samples it could not use rather than quietly averaging them in.

**Results are rows, not columns.** Engine oil, gear oil, coolant and brake
fluid have different analyte sets, and a lab may report something nobody here
has heard of. A column per element would be forty nullable columns and a
migration every time a lab changed its panel; a row per result costs one join
and accepts anything.

**No verdict is ever computed.** Limits are engine-specific, lab-specific and
contested, and an application that printed PASS over somebody's engine would
be asserting a threshold nobody set. The lab's own comment is stored verbatim
and shown; what this application says about a number is arithmetic on the
operator's own history — *"three times the last sample"* — never a judgment
about the machine. See §7.9a and NG-1: the same rule that keeps a time entry
from becoming a bill.
"""

from __future__ import annotations

from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from homeautoshop.core.models import BaseModel, RevisionedModel

from . import analytes


class Compartment(models.TextChoices):
    """Where the sample was drawn from.

    Not a free-text field, because the one thing every comparison depends on
    is knowing that two samples came from the same place. A differential and
    a transfer case both hold gear oil and neither tells you anything about
    the other.
    """

    ENGINE_OIL = "engine_oil", _("Engine oil")
    TRANSMISSION = "transmission", _("Transmission")
    DIFFERENTIAL = "differential", _("Differential")
    TRANSFER_CASE = "transfer_case", _("Transfer case")
    HYDRAULIC = "hydraulic", _("Hydraulic")
    COOLANT = "coolant", _("Coolant")
    BRAKE_FLUID = "brake_fluid", _("Brake fluid")
    FUEL = "fuel", _("Fuel")
    GEAR_OIL = "gear_oil", _("Gear oil")
    OTHER = "other", _("Other")


class FluidSample(RevisionedModel):
    asset = models.ForeignKey(
        "assets.Asset", on_delete=models.CASCADE, related_name="fluid_samples"
    )
    compartment = models.CharField(
        max_length=16, choices=Compartment.choices, default=Compartment.ENGINE_OIL,
        db_index=True,
    )
    #: Front, rear, #2 — following `AssetComponent.position`, because a truck
    #: has two differentials and they wear differently.
    position = models.CharField(
        max_length=16, blank=True, help_text=_("Front, rear — where there is more than one.")
    )

    sampled_on = models.DateField(default=timezone.localdate, db_index=True)
    usage_at_sample = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True,
        help_text=_("The meter reading when the sample was drawn."),
    )
    #: The number that makes the rest comparable.
    fluid_usage = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True,
        help_text=_(
            "How far the fluid itself had run — miles or hours since it was "
            "changed. Wear metals accumulate, so without this a number cannot "
            "be compared with the last one."
        ),
    )
    fluid_changed = models.BooleanField(
        default=False, help_text=_("Was the fluid changed at this sample?")
    )

    lab = models.CharField(max_length=120, blank=True)
    report_number = models.CharField(max_length=64, blank=True)
    fluid_brand = models.CharField(max_length=120, blank=True)
    fluid_grade = models.CharField(max_length=32, blank=True, help_text=_("5W-30, 75W-90…"))

    work_order = models.ForeignKey(
        "work.WorkOrder", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="fluid_samples",
    )
    #: The lab's own words, kept as written. This application does not
    #: summarize it, shorten it, or draw a conclusion from it.
    lab_comment = models.TextField(blank=True)
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ["-sampled_on", "-created_at"]
        indexes = [models.Index(fields=["asset", "compartment", "-sampled_on"])]

    def __str__(self) -> str:
        where = " ".join(bit for bit in (self.position, self.get_compartment_display()) if bit)
        return f"{where} — {self.sampled_on}"

    def clean(self):
        super().clean()
        if self.fluid_usage is not None and self.fluid_usage < 0:
            raise ValidationError(
                {"fluid_usage": _("A fluid cannot have run a negative distance.")}
            )

    @property
    def where(self) -> str:
        """Compartment and position as one phrase, for a heading."""
        return " ".join(
            bit for bit in (self.get_compartment_display(), self.position) if bit
        )

    @property
    def series_key(self) -> tuple[str, str]:
        """What makes two samples comparable: same place on the same vehicle."""
        return (self.compartment, self.position)

    @property
    def is_comparable(self) -> bool:
        """Whether this sample can take part in a rate at all."""
        return self.fluid_usage is not None and self.fluid_usage > 0


class FluidResult(BaseModel):
    """One line off the report."""

    sample = models.ForeignKey(FluidSample, on_delete=models.CASCADE, related_name="results")
    #: A registry slug where the name was recognized, otherwise whatever the
    #: report called it. Deliberately not a foreign key: a lab that adds an
    #: element must not need a migration here.
    analyte = models.CharField(max_length=48, db_index=True)
    value = models.DecimalField(max_digits=12, decimal_places=4)
    unit = models.CharField(max_length=16, blank=True)
    #: The lab's own average or limit for this engine, where the report prints
    #: one. Stored beside the value because it is *their* comparison, not ours.
    reference = models.DecimalField(max_digits=12, decimal_places=4, null=True, blank=True)
    flagged = models.BooleanField(
        default=False, help_text=_("The lab marked this line. Their judgment, not ours.")
    )

    class Meta:
        ordering = ["analyte"]
        constraints = [
            # Conditional, because this model soft-deletes: an unconditional
            # unique index keeps enforcing against rows the application has
            # stopped showing anyone, and re-recording a corrected figure then
            # fails at the database (§5.4, `core/tests_constraints.py`).
            models.UniqueConstraint(
                fields=["sample", "analyte"],
                condition=models.Q(deleted_at__isnull=True),
                name="one_result_per_analyte_per_sample",
            )
        ]

    def __str__(self) -> str:
        return f"{self.label} {self.value:g} {self.unit}".strip()

    @property
    def label(self) -> str:
        return analytes.label_for(self.analyte)

    @property
    def kind(self) -> str:
        return analytes.kind_of(self.analyte)

    @property
    def accumulates(self) -> bool:
        return analytes.accumulates(self.analyte)

    @property
    def rate_per_thousand(self) -> Decimal | None:
        """The value per 1,000 units of fluid life, where that means anything.

        `None` covers both the analyte that does not accumulate — viscosity
        per thousand miles is not a quantity — and the sample that never
        recorded how far its oil had run.
        """
        if not self.accumulates or not self.sample.is_comparable:
            return None
        return (Decimal(self.value) * 1000 / Decimal(self.sample.fluid_usage)).quantize(
            Decimal("0.01")
        )
