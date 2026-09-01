"""Inspection templates as YAML (FR-DVI-13, SCHEMA-INSPECTION-TEMPLATES.md).

`FR-DVI-13` says templates are importable and exportable as YAML, and
`SCHEMA-INSPECTION-TEMPLATES.md` documents the format in full, down to worked
examples. Neither had ever been implemented — the third capability this
project describes as existing that was never built, after schedule template
import/export and the per-vehicle authorization scaffold.

So this is written against that document rather than invented: the field
names, the threshold operators and the worked example in §1 are the contract,
and a file somebody wrote from reading it imports here.

**Thresholds are the part worth care.** They decide `auto_status` — the
machine's opinion of a measurement — and a threshold that parses but means
something other than it says produces a confident wrong answer about a brake
pad. So the operators are checked against the documented set, their operands
must be numbers, and `between` must carry two of them in the right order. A
file that fails any of that is refused with the point named, rather than
imported and discovered on a driveway.

The same narrowness as `maintenance/templatelib.py` and
`diagnostics/profiles.py`: `safe_load`, unknown keys refused rather than
ignored, everything validated before a row is written.
"""

from __future__ import annotations

import yaml
from django.utils.text import slugify
from django.utils.translation import gettext_lazy as _

from .models import (
    Area,
    InspectionPoint,
    InspectionTemplate,
    PhotoRequirement,
    ResultStatus,
)

#: Nested on the model rather than module-level, unlike its siblings.
ResultType = InspectionPoint.ResultType

TEMPLATE_KEYS = frozenset({
    "name", "slug", "translation_key", "description", "asset_kinds",
    "vehicle_classes", "version", "points",
    # See the note in maintenance/templatelib.py — same field, same reason.
    "author",
})

POINT_KEYS = frozenset({
    "area", "name", "translation_key", "guidance", "result_type",
    "measurement_unit", "positions", "sub_positions", "thresholds",
    "photo_required", "is_safety_critical", "is_optional",
})

#: The comparison operators §2 of the schema document defines. Anything else
#: is refused: an unknown operator silently ignored would leave a threshold
#: that looks like it grades a measurement and does not.
OPERATORS = frozenset({"lt", "lte", "gt", "gte", "between"})

#: Which statuses a threshold may assign. `not_applicable` and
#: `not_inspected` are answers a person gives, never ones a rule computes.
THRESHOLD_STATUSES = (ResultStatus.FAIL, ResultStatus.ATTENTION, ResultStatus.PASS)

MAX_BYTES = 512 * 1024


class TemplateInvalid(ValueError):
    """The YAML parsed, and is not a usable inspection template."""


def to_yaml(template: InspectionTemplate) -> str:
    points = []
    for point in template.points.all():
        row = {"area": point.area, "name": point.name}
        for field in ("translation_key", "guidance", "measurement_unit"):
            if getattr(point, field):
                row[field] = getattr(point, field)
        row["result_type"] = point.result_type
        for field in ("positions", "sub_positions"):
            if getattr(point, field):
                row[field] = list(getattr(point, field))
        if point.thresholds:
            row["thresholds"] = point.thresholds
        if point.photo_required != PhotoRequirement.NEVER:
            row["photo_required"] = point.photo_required
        for flag in ("is_safety_critical", "is_optional"):
            if getattr(point, flag):
                row[flag] = True
        points.append(row)

    data = {
        "name": template.name,
        "slug": template.slug,
        "author": template.author,
        "translation_key": template.translation_key,
        "description": template.description,
        "asset_kinds": list(template.asset_kinds or []),
        "vehicle_classes": list(template.vehicle_classes or []),
        "version": template.version,
        "points": points,
    }
    data = {k: v for k, v in data.items() if v not in ("", [], None)}
    return yaml.safe_dump(data, sort_keys=False, allow_unicode=True)


def parse(text: str) -> dict:
    """Read and check a template file without writing anything."""
    if len(text.encode("utf-8")) > MAX_BYTES:
        raise TemplateInvalid(_("That file is too large to be a checklist."))
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise TemplateInvalid(_("That is not readable YAML: %(detail)s") % {"detail": exc})

    if not isinstance(data, dict):
        raise TemplateInvalid(_("A template is a mapping of fields, not a list."))
    unknown = set(data) - TEMPLATE_KEYS
    if unknown:
        raise TemplateInvalid(
            _("Unrecognized field(s): %(keys)s") % {"keys": ", ".join(sorted(unknown))}
        )
    if not str(data.get("name", "")).strip():
        raise TemplateInvalid(_("A template needs a name."))

    raw_points = data.get("points") or []
    if not isinstance(raw_points, list) or not raw_points:
        raise TemplateInvalid(_("A template with no points is not a checklist."))

    points = [_point(row, index) for index, row in enumerate(raw_points, start=1)]
    return {**data, "points": points}


def _point(row, index: int) -> dict:
    if not isinstance(row, dict):
        raise TemplateInvalid(_("Point %(n)d is not a mapping of fields.") % {"n": index})
    unknown = set(row) - POINT_KEYS
    if unknown:
        raise TemplateInvalid(
            _("Point %(n)d has unrecognized field(s): %(keys)s")
            % {"n": index, "keys": ", ".join(sorted(unknown))}
        )
    name = str(row.get("name", "")).strip()
    if not name:
        raise TemplateInvalid(_("Point %(n)d needs a name.") % {"n": index})

    # An unknown area is kept rather than refused. The `fluids` area was
    # retired and inspections recorded under it still render (see `Area`), so
    # a template written against an older vocabulary is readable rather than
    # rejected — but it is reported, so nobody wonders why it groups oddly.
    area = str(row.get("area") or Area.UNDER_HOOD)

    result_type = str(row.get("result_type") or ResultType.STATUS)
    if result_type not in ResultType.values:
        raise TemplateInvalid(
            _("Point %(n)d has an unknown result type: %(value)s")
            % {"n": index, "value": result_type}
        )

    photo = str(row.get("photo_required") or PhotoRequirement.NEVER)
    if photo not in PhotoRequirement.values:
        raise TemplateInvalid(
            _("Point %(n)d has an unknown photo rule: %(value)s")
            % {"n": index, "value": photo}
        )

    for field in ("positions", "sub_positions"):
        value = row.get(field)
        if value is not None and not isinstance(value, list):
            raise TemplateInvalid(
                _("Point %(n)d has a %(field)s that is not a list.")
                % {"n": index, "field": field}
            )

    thresholds = _thresholds(row.get("thresholds"), index)
    if thresholds and result_type == ResultType.STATUS:
        # A threshold grades a number, and a status-only point never records
        # one. Importing this quietly would leave a rule that can never fire.
        raise TemplateInvalid(
            _("Point %(n)d has thresholds but records no measurement.") % {"n": index}
        )

    return {
        "area": area,
        "name": name[:160],
        "translation_key": str(row.get("translation_key") or "")[:80],
        "guidance": str(row.get("guidance") or ""),
        "result_type": result_type,
        "measurement_unit": str(row.get("measurement_unit") or "")[:16],
        "positions": list(row.get("positions") or []),
        "sub_positions": list(row.get("sub_positions") or []),
        "thresholds": thresholds,
        "photo_required": photo,
        "is_safety_critical": bool(row.get("is_safety_critical")),
        "is_optional": bool(row.get("is_optional")),
    }


def _thresholds(raw, index: int) -> dict:
    """Check a threshold block against §2 of the schema document.

    The most load-bearing validation in this file. A threshold decides
    `auto_status`, so one that parses and means something other than it says
    is a confident wrong answer about a brake pad — and it would be found on a
    driveway rather than here.
    """
    if raw in (None, {}):
        return {}
    if not isinstance(raw, dict):
        raise TemplateInvalid(
            _("Point %(n)d has thresholds that are not a mapping.") % {"n": index}
        )

    checked: dict = {}
    for status, rule in raw.items():
        if status not in THRESHOLD_STATUSES:
            raise TemplateInvalid(
                _("Point %(n)d grades to %(status)s, which is not something a "
                  "rule may decide.")
                % {"n": index, "status": status}
            )
        if not isinstance(rule, dict) or not rule:
            raise TemplateInvalid(
                _("Point %(n)d has an empty rule for %(status)s.")
                % {"n": index, "status": status}
            )
        for operator, operand in rule.items():
            if operator not in OPERATORS:
                raise TemplateInvalid(
                    _("Point %(n)d uses an unknown comparison: %(op)s")
                    % {"n": index, "op": operator}
                )
            if operator == "between":
                if (
                    not isinstance(operand, (list, tuple))
                    or len(operand) != 2
                    or not all(isinstance(v, (int, float)) for v in operand)
                    or operand[0] >= operand[1]
                ):
                    raise TemplateInvalid(
                        _("Point %(n)d has a `between` that is not a low and a "
                          "high number.") % {"n": index}
                    )
            elif not isinstance(operand, (int, float)) or isinstance(operand, bool):
                raise TemplateInvalid(
                    _("Point %(n)d compares %(op)s against something that is "
                      "not a number.") % {"n": index, "op": operator}
                )
        checked[status] = dict(rule)
    return checked


def load(text: str, *, source: str = InspectionTemplate.Source.IMPORTED) -> InspectionTemplate:
    """Check a file and write it.

    A name already in use is refused rather than overwritten. A template is
    snapshotted onto every inspection that uses it (FR-DVI-6), so replacing
    one does not rewrite history — but it would silently change what the next
    inspection asks, which is not something a file should do on its own.
    """
    data = parse(text)
    name = str(data["name"]).strip()[:120]
    slug = str(data.get("slug") or slugify(name))[:64]

    if InspectionTemplate.all_objects.filter(name=name).exists():
        raise TemplateInvalid(
            _("A checklist called %(name)s is already here. Rename one of them.")
            % {"name": name}
        )
    if InspectionTemplate.all_objects.filter(slug=slug).exists():
        slug = f"{slug[:56]}-{InspectionTemplate.all_objects.count() + 1}"

    template = InspectionTemplate.objects.create(
        name=name,
        slug=slug,
        translation_key=str(data.get("translation_key") or "")[:80],
        description=str(data.get("description") or ""),
        author=str(data.get("author") or "")[:80],
        asset_kinds=list(data.get("asset_kinds") or []),
        vehicle_classes=list(data.get("vehicle_classes") or []),
        version=int(data.get("version") or 1),
        source=source,
    )
    for sequence, row in enumerate(data["points"]):
        InspectionPoint.objects.create(template=template, sequence=sequence, **row)
    return template


def unknown_areas(data: dict) -> list[str]:
    """Areas in a parsed template this version does not know about.

    Reported rather than refused — see `_point`. Kept separate so a screen can
    say so without the parser having to decide what to do about it.
    """
    return sorted(
        {p["area"] for p in data.get("points", []) if p["area"] not in Area.values}
    )
