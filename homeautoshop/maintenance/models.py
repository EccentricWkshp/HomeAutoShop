"""
Maintenance schedules (SPEC §6.2, §7.7).

The join that makes this module self-maintaining is `ServiceCompletion`:
**doing the work in a work order is the act of resetting the schedule.** There
is no second place to remember to update, which is the failure mode of every
spreadsheet this replaces.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from homeautoshop.core.measurements import to_canonical
from homeautoshop.core.models import BaseModel, RevisionedModel, alive_manager


class Severity(models.TextChoices):
    ROUTINE = "routine", _("Routine")
    SAFETY = "safety", _("Safety")
    EMISSIONS = "emissions", _("Emissions")


class ServiceDefinition(BaseModel):
    """A reusable maintenance item — "Engine oil and filter"."""

    name = models.CharField(max_length=120)
    translation_key = models.CharField(
        max_length=80, blank=True,
        help_text=_("Stable key so shipped items can be translated (SPEC §5.6)."),
    )
    category = models.CharField(max_length=48, blank=True)
    severity = models.CharField(max_length=12, choices=Severity.choices, default=Severity.ROUTINE)
    default_interval_distance = models.PositiveIntegerField(null=True, blank=True)
    default_interval_unit = models.CharField(max_length=8, default="mi")
    default_interval_months = models.PositiveIntegerField(null=True, blank=True)
    default_interval_hours = models.PositiveIntegerField(null=True, blank=True)
    instructions = models.TextField(blank=True)

    class Meta:
        ordering = ["category", "name"]

    def __str__(self) -> str:
        return self.name


class ScheduleTemplate(BaseModel):
    """A named set of service definitions — "Generic gasoline, severe service"."""

    class Source(models.TextChoices):
        BUILTIN = "builtin", _("Built in")
        USER = "user", _("Yours")
        IMPORTED = "imported", _("Imported")

    name = models.CharField(max_length=120, unique=True)
    slug = models.SlugField(max_length=64, unique=True)
    translation_key = models.CharField(max_length=80, blank=True)
    description = models.TextField(blank=True)
    source = models.CharField(max_length=12, choices=Source.choices, default=Source.BUILTIN)
    asset_kinds = models.JSONField(default=list, blank=True)
    author = models.CharField(
        max_length=80,
        blank=True,
        help_text=_("Who published this, where it came from a shared catalog."),
    )
    vehicle_classes = models.JSONField(default=list, blank=True)
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


class TemplateItem(BaseModel):
    """One service definition inside a template, with the template's intervals."""

    template = models.ForeignKey(ScheduleTemplate, on_delete=models.CASCADE, related_name="items")
    definition = models.ForeignKey(ServiceDefinition, on_delete=models.CASCADE, related_name="+")
    interval_distance = models.PositiveIntegerField(null=True, blank=True)
    interval_unit = models.CharField(max_length=8, default="mi")
    interval_months = models.PositiveIntegerField(null=True, blank=True)
    interval_hours = models.PositiveIntegerField(null=True, blank=True)
    sequence = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["sequence", "definition__name"]
        constraints = [
            models.UniqueConstraint(
                fields=["template", "definition"],
                name="unique_template_item",
                condition=models.Q(deleted_at__isnull=True),
            ),
        ]

    def __str__(self) -> str:
        return f"{self.template} — {self.definition}"


class ServiceStatus(models.TextChoices):
    OK = "ok", _("OK")
    DUE_SOON = "due_soon", _("Due soon")
    OVERDUE = "overdue", _("Overdue")
    SNOOZED = "snoozed", _("Snoozed")
    DISABLED = "disabled", _("Not tracked")


class AssetServiceItemQuerySet(models.QuerySet):
    def live(self):
        return self.exclude(status__in=[ServiceStatus.DISABLED])

    def needing_attention(self):
        return self.filter(status__in=[ServiceStatus.OVERDUE, ServiceStatus.DUE_SOON])


class AssetServiceItem(RevisionedModel):
    """The live schedule entry on one asset.

    Per-asset intervals are editable: the operator's severe-service judgment
    beats a template, and a shipped default that cannot be changed is a default
    people work around rather than with.
    """

    asset = models.ForeignKey("assets.Asset", on_delete=models.CASCADE, related_name="service_items")
    definition = models.ForeignKey(ServiceDefinition, on_delete=models.PROTECT, related_name="instances")
    interval_distance = models.PositiveIntegerField(null=True, blank=True)
    interval_unit = models.CharField(max_length=8, default="mi")
    interval_months = models.PositiveIntegerField(null=True, blank=True)
    interval_hours = models.PositiveIntegerField(null=True, blank=True)

    last_done_on = models.DateField(null=True, blank=True)
    last_done_usage = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)

    next_due_on = models.DateField(null=True, blank=True, db_index=True)
    next_due_usage = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    status = models.CharField(
        max_length=12, choices=ServiceStatus.choices, default=ServiceStatus.OK, db_index=True
    )
    snooze_until = models.DateField(null=True, blank=True)
    snooze_reason = models.CharField(max_length=200, blank=True)
    notes = models.TextField(blank=True)

    # `alive_manager`, not a plain `Manager.from_queryset` — the difference is
    # the whole soft-delete contract, and this model got it wrong. A plain
    # manager shadows `AliveManager` and shows removed rows everywhere, which
    # cost nothing while nothing could remove one and became the bug the
    # moment removal existed.
    objects = alive_manager(AssetServiceItemQuerySet)
    all_objects = models.Manager.from_queryset(AssetServiceItemQuerySet)()

    class Meta:
        ordering = ["next_due_on", "definition__name"]
        constraints = [
            models.UniqueConstraint(
                fields=["asset", "definition"],
                name="unique_asset_service_item",
                condition=models.Q(deleted_at__isnull=True),
            ),
        ]
        indexes = [models.Index(fields=["asset", "status"])]

    def __str__(self) -> str:
        return f"{self.definition} — {self.asset}"

    @property
    def has_any_interval(self) -> bool:
        return bool(self.interval_distance or self.interval_months or self.interval_hours)

    @property
    def is_snoozed(self) -> bool:
        return bool(self.snooze_until and self.snooze_until >= timezone.localdate())

    @property
    def is_safety(self) -> bool:
        return self.definition.severity == Severity.SAFETY

    @property
    def is_removable(self) -> bool:
        """Nothing has ever been recorded against it, so it is a plan, not a record.

        The line removal is allowed to cross. Taking an item off a vehicle
        loses the intention to service it, which is the operator's to change;
        it must not lose the fact that a service happened, which is not.

        `times_done` is the annotation the schedule page adds so a list of
        thirty items costs one query rather than thirty-one. Without it this
        falls back to asking, so a caller holding a bare instance still gets
        the right answer.
        """
        counted = getattr(self, "times_done", None)
        return (self.completions.count() if counted is None else counted) == 0

    def clean(self):
        super().clean()
        if not self.has_any_interval and self.status != ServiceStatus.DISABLED:
            raise ValidationError(
                _("Give this item at least one interval — distance, time, or hours.")
            )

    @property
    def interval_distance_canonical(self) -> Decimal | None:
        if not self.interval_distance:
            return None
        return to_canonical(self.interval_distance, self.interval_unit)

    def distance_remaining(self, current_usage) -> Decimal | None:
        if self.next_due_usage is None or current_usage is None:
            return None
        return Decimal(str(self.next_due_usage)) - Decimal(str(current_usage))

    def days_remaining(self, today: date | None = None) -> int | None:
        if self.next_due_on is None:
            return None
        return (self.next_due_on - (today or timezone.localdate())).days


class ServiceCompletion(BaseModel):
    """The join that makes the schedule self-maintaining (SPEC §6.2).

    Completing a job item linked to a service item writes one of these and
    rolls the interval forward. Reopening a work order does not delete it — a
    later correction is a new completion, not a rewrite of history.
    """

    service_item = models.ForeignKey(
        AssetServiceItem, on_delete=models.CASCADE, related_name="completions"
    )
    job_item = models.ForeignKey(
        "work.JobItem", null=True, blank=True, on_delete=models.SET_NULL, related_name="service_completions"
    )
    work_order = models.ForeignKey(
        "work.WorkOrder", null=True, blank=True, on_delete=models.SET_NULL, related_name="service_completions"
    )
    completed_on = models.DateField(default=timezone.localdate)
    usage = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    note = models.CharField(max_length=200, blank=True)
    is_backfill = models.BooleanField(
        default=False,
        help_text=_("Recorded from history rather than from work done here (FR-MAINT-6)."),
    )

    class Meta:
        ordering = ["-completed_on"]

    def __str__(self) -> str:
        return f"{self.service_item.definition} on {self.completed_on}"


class AssetComponent(BaseModel):
    """An installed part instance with a life of its own (SPEC §6.2, FR-CMP-*).

    This is what turns a repeated measurement into a wear rate. A tread depth
    reading is a number; a tread depth reading against a component installed
    31,000 miles ago is a rate, and a rate is a due date.
    """

    class Type(models.TextChoices):
        TIRE = "tire", _("Tire")
        BATTERY = "battery", _("Battery")
        BRAKE_PAD = "brake_pad", _("Brake pad")
        BRAKE_ROTOR = "brake_rotor", _("Brake rotor")
        BELT = "belt", _("Belt")
        FILTER = "filter", _("Filter")
        WIPER = "wiper", _("Wiper")
        BULB = "bulb", _("Bulb")
        OTHER = "other", _("Other")

    class RemovalReason(models.TextChoices):
        WORN = "worn", _("Worn out")
        FAILED = "failed", _("Failed")
        UPGRADED = "upgraded", _("Upgraded")
        SEASONAL = "seasonal", _("Seasonal change")

    asset = models.ForeignKey("assets.Asset", on_delete=models.CASCADE, related_name="components")
    part = models.ForeignKey(
        "parts.Part", null=True, blank=True, on_delete=models.SET_NULL, related_name="components",
        help_text=_("Optional: a component may predate any part record."),
    )
    component_type = models.CharField(max_length=16, choices=Type.choices, default=Type.OTHER)
    label = models.CharField(max_length=120, blank=True)
    position = models.CharField(max_length=16, blank=True, help_text=_("LF, RF, LR, RR…"))

    installed_on = models.DateField(default=timezone.localdate)
    installed_usage = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    installed_by_job_item = models.ForeignKey(
        "work.JobItem", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    removed_on = models.DateField(null=True, blank=True)
    removed_usage = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    removal_reason = models.CharField(max_length=12, choices=RemovalReason.choices, blank=True)

    serial_or_dot_code = models.CharField(
        max_length=32, blank=True,
        help_text=_("Tire DOT code: week and year of manufacture."),
    )
    warranty_months = models.PositiveIntegerField(null=True, blank=True)
    warranty_distance = models.PositiveIntegerField(null=True, blank=True)
    expected_life_distance = models.PositiveIntegerField(null=True, blank=True)
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ["component_type", "position", "-installed_on"]
        indexes = [models.Index(fields=["asset", "component_type"])]

    def __str__(self) -> str:
        bits = [self.get_component_type_display(), self.position, self.label]
        return " ".join(b for b in bits if b)

    @property
    def is_installed(self) -> bool:
        return self.removed_on is None

    @property
    def age_days(self) -> int:
        end = self.removed_on or timezone.localdate()
        return (end - self.installed_on).days

    def distance_covered(self, current_usage=None) -> Decimal | None:
        """How far this component has travelled since it went on."""
        if self.installed_usage is None:
            return None
        end = self.removed_usage if self.removed_usage is not None else current_usage
        if end is None:
            return None
        covered = Decimal(str(end)) - Decimal(str(self.installed_usage))
        return covered if covered >= 0 else None

    @property
    def dot_age_years(self) -> float | None:
        """Age from a tire DOT date code, independent of tread.

        FR-CMP-6: a tire with full tread and a ten-year-old date code is a
        failed tire, and nothing else in the system would catch that.
        """
        code = (self.serial_or_dot_code or "").strip()
        if len(code) != 4 or not code.isdigit():
            return None
        week, year = int(code[:2]), int(code[2:])
        if not 1 <= week <= 53:
            return None
        # Two-digit year, 2000s era. Pre-2000 tires used a three-digit code and
        # are old enough that the exact year hardly matters.
        made = date(2000 + year, 1, 1) + timedelta(weeks=week - 1)
        return round((timezone.localdate() - made).days / 365.25, 1)

    @property
    def dot_verdict(self) -> str | None:
        age = self.dot_age_years
        if age is None:
            return None
        if age >= 10:
            return "fail"
        if age >= 6:
            return "attention"
        return "ok"
