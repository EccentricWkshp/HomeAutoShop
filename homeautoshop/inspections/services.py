"""
Threshold evaluation, inspection lifecycle, and wear projection (SPEC §7.8).

`evaluate` is deliberately small and pure: thresholds propose a status, and the
result records it as `auto_status` **alongside** whatever the human chose. The
two are never collapsed, because next year the reason for a disagreement will
not be obvious, and that reason is exactly what makes the record worth keeping.

`wear_projection` is the feature that justifies the module. A tread depth
reading is a number; the same reading taken twice, against odometer history,
is a wear rate — and a wear rate is a due date.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext as _

from .models import (
    SEVERITY_ORDER,
    Inspection,
    InspectionResult,
    InspectionTemplate,
    PhotoRequirement,
    ResultStatus,
)

# Most severe first: the first rule that matches wins.
EVALUATION_ORDER = (ResultStatus.FAIL, ResultStatus.ATTENTION, ResultStatus.PASS)

def trim(value) -> str:
    """Render a measurement without trailing zeros or exponent notation.

    `2.000 mm` reads as noise; `2 mm` reads as a measurement. Decimal's `g`
    format would turn 100 into 1E+2, so this does it by hand.
    """
    text = f"{Decimal(str(value)):f}"
    return text.rstrip("0").rstrip(".") if "." in text else text


COMPARATORS = {
    "lt": lambda v, t: v < t,
    "lte": lambda v, t: v <= t,
    "gt": lambda v, t: v > t,
    "gte": lambda v, t: v >= t,
    "eq": lambda v, t: v == t,
}


def evaluate(value, thresholds: dict | None) -> str:
    """Return the status a set of thresholds implies for `value`.

    Returns "" when there is nothing to decide — a point with no thresholds is
    scored by the human, and pretending otherwise would invent authority the
    rule does not have.
    """
    if value is None or not thresholds:
        return ""
    value = Decimal(str(value))

    for status in EVALUATION_ORDER:
        rule = thresholds.get(status)
        if not rule:
            continue
        if _matches(value, rule):
            return status
    return ""


def _matches(value: Decimal, rule: dict) -> bool:
    if "between" in rule:
        low, high = rule["between"]
        return Decimal(str(low)) <= value <= Decimal(str(high))
    for operator, threshold in rule.items():
        check = COMPARATORS.get(operator)
        if check is None:
            continue
        if not check(value, Decimal(str(threshold))):
            return False
    return bool(rule)


@transaction.atomic
def start(asset, template: InspectionTemplate, *, user=None, work_order=None) -> Inspection:
    """Begin an inspection, snapshotting the template as it stands (FR-DVI-6)."""
    snapshot = template.snapshot()
    inspection = Inspection.objects.create(
        asset=asset,
        template=template,
        template_name=template.name,
        template_version=template.version,
        points_snapshot=snapshot,
        performed_by=user if getattr(user, "pk", None) else None,
        work_order=work_order,
        odometer=asset.current_usage,
    )

    rows = []
    for point in template.points.all():
        for position in point.expanded_positions():
            rows.append(
                InspectionResult(
                    inspection=inspection,
                    point_id=point.pk,
                    point_snapshot=point.as_dict(),
                    position=position,
                    status="",
                    unit=point.measurement_unit,
                )
            )
    InspectionResult.objects.bulk_create(rows)
    return inspection


def record(result: InspectionResult, *, value=None, status: str = "", note: str = "", action: str = ""):
    """Record one observation. Thresholds propose; the human disposes (FR-DVI-4)."""
    if value not in (None, ""):
        result.measured_value = Decimal(str(value))
    elif value == "":
        result.measured_value = None

    result.auto_status = evaluate(result.measured_value, (result.point_snapshot or {}).get("thresholds"))

    if status:
        result.status = status
        result.status_overridden = bool(result.auto_status and result.auto_status != status)
    elif result.auto_status:
        result.status = result.auto_status
        result.status_overridden = False

    if note:
        result.note = note[:300]
    if action:
        result.recommended_action = action[:200]
    result.save()
    return result


@transaction.atomic
def complete(inspection: Inspection, *, force: bool = False) -> Inspection:
    """Close the inspection out, refusing if required evidence is missing."""
    missing = inspection.missing_required_photos
    if missing and not force:
        from django.core.exceptions import ValidationError

        raise ValidationError(
            _("%(n)d point(s) need a photo before this can be signed off.") % {"n": len(missing)}
        )

    unanswered = inspection.results.filter(status="")
    unanswered.update(status=ResultStatus.NOT_INSPECTED)

    inspection.status = Inspection.Status.COMPLETE
    inspection.overall = inspection.worst_status() or ResultStatus.PASS
    inspection.save()
    return inspection


def abandon(inspection: Inspection) -> Inspection:
    """Stop working on an inspection without discarding what was recorded.

    Distinct from deleting it. A walk you started and thought better of is
    still a fact about the vehicle — that someone looked on that date and
    stopped — and an abandoned record can be resumed. Use delete for the one
    you opened by mistake.
    """
    if inspection.status != Inspection.Status.DRAFT:
        from django.core.exceptions import ValidationError

        raise ValidationError(_("Only an inspection in progress can be abandoned."))
    inspection.status = Inspection.Status.ABANDONED
    inspection.save()
    return inspection


def resume(inspection: Inspection) -> Inspection:
    """Pick an abandoned inspection back up."""
    if inspection.status != Inspection.Status.ABANDONED:
        from django.core.exceptions import ValidationError

        raise ValidationError(_("That inspection is not abandoned."))
    inspection.status = Inspection.Status.DRAFT
    inspection.save()
    return inspection


def add_check(
    inspection: Inspection,
    *,
    name: str,
    area: str,
    unit: str = "",
    guidance: str = "",
    is_safety_critical: bool = False,
) -> InspectionResult:
    """Add a one-off check to an inspection in progress (FR-DVI-3).

    No template is edited and no other inspection changes. A built-in checklist
    cannot anticipate every drivetrain, and the alternative to an escape hatch
    here is someone writing the finding into a note field where nothing can
    compare it next year.

    The synthetic snapshot has the same shape a template point produces, so
    everything downstream — thresholds, photo rules, comparison, conversion to
    work — treats it like any other result.
    """
    if not inspection.is_draft:
        from django.core.exceptions import ValidationError

        raise ValidationError(_("An inspection has to be in progress to add a check."))

    name = (name or "").strip()[:160]
    if not name:
        from django.core.exceptions import ValidationError

        raise ValidationError(_("Give the check a name."))

    unit = (unit or "").strip()[:16]
    return InspectionResult.objects.create(
        inspection=inspection,
        point_id=None,
        unit=unit,
        point_snapshot={
            "id": None,
            "area": area,
            # Sorts after the template's own points within the same area, so an
            # addition lands at the end of its section rather than mid-walk.
            "sequence": 10_000 + inspection.results.count(),
            "name": name,
            "guidance": (guidance or "").strip()[:500],
            "result_type": "both" if unit else "status",
            "measurement_unit": unit,
            "positions": [],
            "sub_positions": [],
            "thresholds": {},
            "photo_required": PhotoRequirement.NEVER,
            "is_safety_critical": bool(is_safety_critical),
            "is_optional": False,
            "is_ad_hoc": True,
        },
    )


@transaction.atomic
def convert_to_work_order(inspection: Inspection, *, user=None, work_order=None):
    """Turn every flagged result into work (FR-DVI-8).

    Notes travel with the item, because "left front tire at 2/32" is the whole
    reason the job exists and retyping it is how detail gets lost.
    """
    from homeautoshop.work.models import JobItem, WorkOrder

    flagged = [r for r in inspection.needs_attention if r.converted_to_job_item_id is None]
    if not flagged:
        return work_order, []

    if work_order is None:
        work_order = WorkOrder.objects.create(
            asset=inspection.asset,
            title=_("From %(name)s") % {"name": inspection.template_name},
            type="repair",
            complaint=_("Raised by inspection on %(date)s")
            % {"date": inspection.performed_on.isoformat()},
            odometer_in=inspection.odometer,
            is_safety_critical=any(r.is_safety_critical for r in flagged),
        )

    created = []
    for index, result in enumerate(flagged):
        detail = result.note or result.recommended_action
        if result.measured_value is not None:
            measurement = f"{trim(result.measured_value)} {result.unit}".strip()
            detail = f"{measurement}{' — ' + detail if detail else ''}"
        item = JobItem.objects.create(
            work_order=work_order,
            sequence=work_order.job_items.count() + index,
            title=f"{result.name} {result.position}".strip()[:200],
            description=detail,
        )
        result.converted_to_job_item = item
        result.save()
        created.append(item)
    return work_order, created


# ---------------------------------------------------------------------------
# Wear projection (FR-DVI-11)
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class Measurement:
    on: date
    usage: Decimal | None
    value: Decimal


@dataclass(slots=True)
class Wear:
    point_name: str
    position: str
    unit: str
    readings: list[Measurement]
    per_distance: Decimal | None = None
    distance_unit: str = ""
    projected_usage: Decimal | None = None
    projected_date: date | None = None
    target: Decimal | None = None

    @property
    def is_projectable(self) -> bool:
        return self.per_distance is not None and self.per_distance > 0

    @property
    def summary(self) -> str:
        if len(self.readings) < 2:
            return str(_("One reading so far — measure it again to see a trend."))
        if not self.is_projectable:
            return str(_("No measurable wear between readings yet."))
        rate = f"{self.per_distance:.3f}"
        text = str(_("Wearing %(rate)s %(unit)s per %(distance_unit)s")) % {
            "rate": rate, "unit": self.unit, "distance_unit": self.distance_unit,
        }
        if self.projected_date:
            text += " · " + str(_("reaches %(target)s around %(date)s")) % {
                "target": f"{self.target:g}", "date": self.projected_date.isoformat(),
            }
        return text


def measurement_history(asset, point_name: str, position: str = "") -> list[Measurement]:
    """Every recorded value for one point on one asset, oldest first."""
    query = InspectionResult.objects.filter(
        inspection__asset=asset,
        inspection__status=Inspection.Status.COMPLETE,
        measured_value__isnull=False,
        point_snapshot__name=point_name,
    ).select_related("inspection")
    if position:
        query = query.filter(position=position)
    rows = [
        Measurement(
            on=r.inspection.performed_on,
            usage=r.inspection.odometer,
            value=Decimal(str(r.measured_value)),
        )
        for r in query
    ]
    return sorted(rows, key=lambda m: m.on)


def wear_projection(asset, point_name: str, position: str = "", *, unit: str = "") -> Wear:
    """Project when a wearing measurement reaches its fail threshold.

    This is what makes a DVI worth repeating: two readings and an odometer
    turn "3/32 today" into "below the legal limit around March".
    """
    readings = measurement_history(asset, point_name, position)
    wear = Wear(
        point_name=point_name,
        position=position,
        unit=unit,
        readings=readings,
        distance_unit=asset.meter_unit,
    )
    if len(readings) < 2:
        return wear

    first, last = readings[0], readings[-1]
    if first.usage is None or last.usage is None:
        return wear

    distance = Decimal(str(last.usage)) - Decimal(str(first.usage))
    consumed = Decimal(str(first.value)) - Decimal(str(last.value))
    if distance <= 0 or consumed <= 0:
        return wear

    wear.per_distance = consumed / distance

    # Project to the fail threshold recorded on the point itself.
    sample = (
        InspectionResult.objects.filter(
            inspection__asset=asset, point_snapshot__name=point_name
        ).order_by("-created_at").first()
    )
    thresholds = (sample.point_snapshot or {}).get("thresholds") if sample else None
    target = None
    if thresholds and isinstance(thresholds.get("fail"), dict):
        rule = thresholds["fail"]
        for key in ("lte", "lt"):
            if key in rule:
                target = Decimal(str(rule[key]))
                break
    if target is None:
        return wear

    wear.target = target
    remaining = Decimal(str(last.value)) - target
    if remaining <= 0:
        wear.projected_usage = Decimal(str(last.usage))
        wear.projected_date = last.on
        return wear

    distance_left = remaining / wear.per_distance
    wear.projected_usage = Decimal(str(last.usage)) + distance_left

    from homeautoshop.maintenance.services import usage_rate

    rate = usage_rate(asset)
    if rate.per_day > 0:
        current = asset.current_usage
        if current is not None:
            to_go = wear.projected_usage - Decimal(str(current))
            days = int(to_go / rate.per_day)
            wear.projected_date = timezone.localdate() + timedelta(days=max(days, 0))
    return wear


def compare(inspection: Inspection) -> list[dict]:
    """What changed since the previous inspection of the same template (FR-DVI-12)."""
    previous = (
        Inspection.objects.filter(
            asset=inspection.asset,
            template_name=inspection.template_name,
            status=Inspection.Status.COMPLETE,
            performed_on__lt=inspection.performed_on,
        )
        .order_by("-performed_on")
        .first()
    )
    if previous is None:
        return []

    before = {
        (r.name, r.position): r for r in previous.results.all()
    }
    changes = []
    for result in inspection.results.all():
        old = before.get((result.name, result.position))
        if old is None or old.status == result.status:
            continue
        changes.append(
            {
                "result": result,
                "was": old.status,
                "now": result.status,
                "worsened": SEVERITY_ORDER.get(result.status, 9) < SEVERITY_ORDER.get(old.status, 9),
                "old_value": old.measured_value,
            }
        )
    return changes
