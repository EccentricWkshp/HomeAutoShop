"""
Digital Vehicle Inspection (SPEC §7.8, SCHEMA-INSPECTION-TEMPLATES.md).

For a commercial shop a DVI is a sales document. For a home shop its value is
different and better: it is the only feature that says what is going to need
attention **before** it strands someone — and because measurements repeat over
time against a known installed component, it turns observation into prediction.

Two rules shape everything here:

* **The template is snapshotted onto the inspection.** Editing a template must
  never rewrite the meaning of a two-year-old inspection.
* **`auto_status` and `status` are stored separately and never collapsed.**
  Thresholds propose; the human disposes; the disagreement is the most
  interesting thing on the record.
"""

from __future__ import annotations

from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from homeautoshop.core.models import BaseModel, RevisionedModel


class Area(models.TextChoices):
    ROAD_TEST = "road_test", _("Road test")
    UNDER_HOOD = "under_hood", _("Under hood")
    UNDER_VEHICLE = "under_vehicle", _("Under vehicle")
    BRAKES = "brakes", _("Brakes")
    TIRES_WHEELS = "tires_wheels", _("Tires and wheels")
    SUSPENSION = "suspension_steering", _("Suspension and steering")
    ELECTRICAL = "lighting_electrical", _("Lighting and electrical")
    BODY_GLASS = "body_glass", _("Body and glass")
    INTERIOR = "interior", _("Interior")
    EXHAUST = "exhaust_emissions", _("Exhaust and emissions")

    # There is no "Fluids" area on purpose. An area is *where you are standing*,
    # so engine oil and coolant belong under the hood and differential fluid
    # belongs under the vehicle. Filing them by substance sent you round the
    # car twice. Inspections recorded before this changed still carry
    # `area = "fluids"` in their snapshot and still render — see area_display.


class ResultStatus(models.TextChoices):
    PASS = "pass", _("Pass")
    ATTENTION = "attention", _("Attention")
    FAIL = "fail", _("Fail")
    NOT_APPLICABLE = "not_applicable", _("Not applicable")
    NOT_INSPECTED = "not_inspected", _("Not inspected")


SEVERITY_ORDER = {
    ResultStatus.FAIL: 0,
    ResultStatus.ATTENTION: 1,
    ResultStatus.PASS: 2,
    ResultStatus.NOT_APPLICABLE: 3,
    ResultStatus.NOT_INSPECTED: 4,
}


class PhotoRequirement(models.TextChoices):
    NEVER = "never", _("Not needed")
    ON_ATTENTION = "on_attention", _("When flagged")
    ALWAYS = "always", _("Always")


class InspectionTemplate(BaseModel):
    class Source(models.TextChoices):
        BUILTIN = "builtin", _("Built in")
        USER = "user", _("Yours")
        IMPORTED = "imported", _("Imported")

    name = models.CharField(max_length=120, unique=True)
    slug = models.SlugField(max_length=64, unique=True)
    translation_key = models.CharField(max_length=80, blank=True)
    description = models.TextField(blank=True)
    asset_kinds = models.JSONField(default=list, blank=True)
    vehicle_classes = models.JSONField(default=list, blank=True)
    source = models.CharField(max_length=12, choices=Source.choices, default=Source.BUILTIN)
    version = models.PositiveIntegerField(default=1)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name

    def applies_to(self, asset) -> bool:
        if self.asset_kinds and asset.asset_kind not in self.asset_kinds:
            return False
        if self.vehicle_classes and asset.vehicle_class not in self.vehicle_classes:
            return False
        return True

    def snapshot(self) -> list[dict]:
        """Freeze the template's points as they stand right now."""
        return [point.as_dict() for point in self.points.all()]


class InspectionPoint(BaseModel):
    """One thing you look at."""

    class ResultType(models.TextChoices):
        STATUS = "status", _("Pass / attention / fail")
        MEASUREMENT = "measurement", _("A number")
        BOTH = "both", _("Both")

    template = models.ForeignKey(InspectionTemplate, on_delete=models.CASCADE, related_name="points")
    area = models.CharField(max_length=24, choices=Area.choices, default=Area.UNDER_HOOD)
    sequence = models.PositiveIntegerField(default=0)
    name = models.CharField(max_length=160)
    translation_key = models.CharField(max_length=80, blank=True)
    guidance = models.TextField(blank=True, help_text=_("What 'good' looks like."))
    result_type = models.CharField(max_length=12, choices=ResultType.choices, default=ResultType.STATUS)
    measurement_unit = models.CharField(max_length=16, blank=True)

    # A tire tread point is 4 wheels x 3 positions. Defining twelve points by
    # hand is how checklist systems become unusable (FR-DVI-3).
    positions = models.JSONField(default=list, blank=True)
    sub_positions = models.JSONField(default=list, blank=True)

    thresholds = models.JSONField(default=dict, blank=True)
    photo_required = models.CharField(
        max_length=12, choices=PhotoRequirement.choices, default=PhotoRequirement.NEVER
    )
    is_safety_critical = models.BooleanField(default=False)
    is_optional = models.BooleanField(default=False)

    class Meta:
        ordering = ["area", "sequence", "name"]

    def __str__(self) -> str:
        return self.name

    def expanded_positions(self) -> list[str]:
        """Every slot this point produces, as position labels."""
        if not self.positions:
            return [""]
        if not self.sub_positions:
            return list(self.positions)
        return [f"{p}/{s}" for p in self.positions for s in self.sub_positions]

    def as_dict(self) -> dict:
        return {
            "id": str(self.pk),
            "area": self.area,
            "sequence": self.sequence,
            "name": self.name,
            "guidance": self.guidance,
            "result_type": self.result_type,
            "measurement_unit": self.measurement_unit,
            "positions": self.positions,
            "sub_positions": self.sub_positions,
            "thresholds": self.thresholds,
            "photo_required": self.photo_required,
            "is_safety_critical": self.is_safety_critical,
            "is_optional": self.is_optional,
        }


class Inspection(RevisionedModel):
    class Status(models.TextChoices):
        DRAFT = "draft", _("In progress")
        COMPLETE = "complete", _("Complete")
        ABANDONED = "abandoned", _("Abandoned")

    asset = models.ForeignKey("assets.Asset", on_delete=models.CASCADE, related_name="inspections")
    template = models.ForeignKey(
        InspectionTemplate, null=True, blank=True, on_delete=models.SET_NULL, related_name="inspections"
    )
    template_name = models.CharField(max_length=120)
    template_version = models.PositiveIntegerField(default=1)
    points_snapshot = models.JSONField(
        default=list,
        help_text=_("The template as it stood. Later edits never rewrite this inspection."),
    )

    work_order = models.ForeignKey(
        "work.WorkOrder", null=True, blank=True, on_delete=models.SET_NULL, related_name="inspections"
    )
    performed_by = models.ForeignKey(
        "accounts.User", null=True, blank=True, on_delete=models.SET_NULL, related_name="inspections"
    )
    performed_on = models.DateField(default=timezone.localdate, db_index=True)
    odometer = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.DRAFT, db_index=True)
    overall = models.CharField(max_length=16, choices=ResultStatus.choices, blank=True)
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ["-performed_on", "-created_at"]
        indexes = [models.Index(fields=["asset", "-performed_on"])]

    def __str__(self) -> str:
        return f"{self.template_name} — {self.asset} ({self.performed_on})"

    @property
    def is_draft(self) -> bool:
        return self.status == self.Status.DRAFT

    def summarize(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for result in self.results.all():
            counts[result.status] = counts.get(result.status, 0) + 1
        return counts

    def worst_status(self) -> str:
        statuses = [r.status for r in self.results.all() if r.status]
        if not statuses:
            return ""
        return min(statuses, key=lambda s: SEVERITY_ORDER.get(s, 9))

    @property
    def needs_attention(self):
        return self.results.filter(
            status__in=[ResultStatus.FAIL, ResultStatus.ATTENTION]
        ).order_by("status")

    @property
    def missing_required_photos(self):
        """A point whose template demands evidence and has none."""
        offenders = []
        for result in self.results.all():
            requirement = (result.point_snapshot or {}).get("photo_required")
            if requirement == PhotoRequirement.ALWAYS and not result.has_photo:
                offenders.append(result)
            elif (
                requirement == PhotoRequirement.ON_ATTENTION
                and result.status in (ResultStatus.FAIL, ResultStatus.ATTENTION)
                and not result.has_photo
            ):
                offenders.append(result)
        return offenders


class InspectionResult(BaseModel):
    """One slot on one inspection."""

    inspection = models.ForeignKey(Inspection, on_delete=models.CASCADE, related_name="results")
    point_id = models.UUIDField(null=True, blank=True, db_index=True)
    point_snapshot = models.JSONField(default=dict)
    position = models.CharField(max_length=32, blank=True)

    status = models.CharField(max_length=16, choices=ResultStatus.choices, blank=True)
    auto_status = models.CharField(
        max_length=16,
        choices=ResultStatus.choices,
        blank=True,
        help_text=_("What the thresholds computed, kept alongside what the human decided."),
    )
    status_overridden = models.BooleanField(default=False)

    measured_value = models.DecimalField(max_digits=12, decimal_places=3, null=True, blank=True)
    unit = models.CharField(max_length=16, blank=True)
    note = models.CharField(max_length=300, blank=True)
    recommended_action = models.CharField(max_length=200, blank=True)

    converted_to_job_item = models.ForeignKey(
        "work.JobItem", null=True, blank=True, on_delete=models.SET_NULL, related_name="from_inspection"
    )

    class Meta:
        ordering = ["point_snapshot__area", "point_snapshot__sequence", "position"]
        indexes = [models.Index(fields=["inspection", "status"])]

    def __str__(self) -> str:
        label = (self.point_snapshot or {}).get("name", "point")
        return f"{label} {self.position}".strip()

    @property
    def name(self) -> str:
        return (self.point_snapshot or {}).get("name", "")

    @property
    def area(self) -> str:
        return (self.point_snapshot or {}).get("area", "")

    @property
    def area_display(self) -> str:
        """The area's label, not its database key — this is a heading.

        The fallback is what keeps a historical snapshot readable after an area
        is retired: an inspection filed under "fluids" still says *Fluids*,
        which is what its template said at the time. Snapshots are history and
        are never rewritten.
        """
        return dict(Area.choices).get(self.area, self.area.replace("_", " ").title())

    @property
    def guidance(self) -> str:
        return (self.point_snapshot or {}).get("guidance", "")

    @property
    def is_safety_critical(self) -> bool:
        return bool((self.point_snapshot or {}).get("is_safety_critical"))

    @property
    def is_optional(self) -> bool:
        """Not fitted to every vehicle — a transfer case, a hydraulic clutch.

        Shown so an inapplicable point reads as "does not apply here" rather
        than as something the inspector forgot.
        """
        return bool((self.point_snapshot or {}).get("is_optional"))

    @property
    def is_ad_hoc(self) -> bool:
        """Added to this inspection by hand rather than coming from a template."""
        return bool((self.point_snapshot or {}).get("is_ad_hoc"))

    @property
    def result_type(self) -> str:
        return (self.point_snapshot or {}).get("result_type", "status")

    @property
    def takes_measurement(self) -> bool:
        return self.result_type in ("measurement", "both")

    @property
    def needs_attention(self) -> bool:
        return self.status in (ResultStatus.FAIL, ResultStatus.ATTENTION)

    @property
    def has_photo(self) -> bool:
        from homeautoshop.mediafiles.models import MediaLink

        return MediaLink.objects.filter(entity_type="InspectionResult", entity_id=self.pk).exists()

    @property
    def disagreed(self) -> bool:
        """The human overrode the rule — the most interesting thing on the record."""
        return bool(self.auto_status and self.status and self.auto_status != self.status)

    def clean(self):
        super().clean()
        if self.takes_measurement and self.measured_value is not None and self.measured_value < 0:
            raise ValidationError({"measured_value": _("A measurement cannot be negative.")})
