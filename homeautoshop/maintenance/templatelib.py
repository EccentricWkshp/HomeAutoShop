"""Schedule templates as YAML (SPEC §7.7, R-1).

§8's third supported source of maintenance schedules is *"YAML/JSON template
import/export for sharing between instances"*, and OQ-2 deferred a community
repository on the grounds that **"import/export files only in v1"** already
covered the ninety-percent case. It did for parser profiles. It did not exist
at all for schedule templates, which are the artifact the community repository
is mostly about — so the reason given for deferring R-1 was true of the wrong
half of the feature.

This is that format. A catalog is pointless without it: whatever a shared
repository serves has to arrive as something an operator could equally have
been handed on a memory stick, and has to pass through exactly the same
validation either way.

**Import is deliberately narrow**, following `diagnostics/profiles.py`:
`safe_load`, unknown keys refused rather than ignored, and everything checked
before a row is written. A template is not executable the way a parser profile
is — these are intervals, not regexes — but it is *acted on*: a bad interval
becomes a service that is due too late, and being quietly wrong about when to
change a timing belt is its own kind of dangerous.

**Definitions travel with the template.** A template item points at a
`ServiceDefinition`, and a file that assumed the receiving instance already had
"Engine oil and filter" under the same name would import as an empty schedule
on half the shops that tried it. So each item carries its definition, matched
on the receiving side by `translation_key` first and name second, and created
when neither finds anything.
"""

from __future__ import annotations

import yaml
from django.utils.text import slugify
from django.utils.translation import gettext_lazy as _

from .models import ScheduleTemplate, ServiceDefinition, Severity, TemplateItem

#: Top-level keys a file may carry. Anything else is refused rather than
#: dropped: a key nobody recognizes is either a newer format this instance
#: cannot honor or a typo, and silently ignoring it means importing a
#: schedule that differs from the one somebody wrote.
TEMPLATE_KEYS = frozenset({
    "name", "slug", "description", "asset_kinds", "vehicle_classes", "items",
    # Who wrote it. Carried through to the index and kept after install,
    # because "who said these intervals were right" is the question the
    # review section of the catalog README exists to answer, and `source =
    # imported` only says the template is not ours.
    "author",
})

ITEM_KEYS = frozenset({
    "name", "translation_key", "category", "severity", "instructions",
    "interval_distance", "interval_unit", "interval_months", "interval_hours",
})

#: Units an interval may be expressed in. A file naming anything else is
#: refused rather than coerced — "5000 furlongs" silently read as miles is a
#: schedule that is wrong by a factor nobody would spot.
UNITS = frozenset({"mi", "km", "hr"})

#: A file bigger than this is not a schedule. The cap exists because the same
#: parser is fed by an operator's upload and by a remote catalog, and only
#: one of those is somebody this shop knows.
MAX_BYTES = 512 * 1024


class TemplateInvalid(ValueError):
    """The YAML parsed, and is not a usable schedule template."""


def to_yaml(template: ScheduleTemplate) -> str:
    """One template as a portable file, definitions included."""
    items = []
    for item in template.items.select_related("definition"):
        definition = item.definition
        row = {"name": definition.name}
        if definition.translation_key:
            row["translation_key"] = definition.translation_key
        if definition.category:
            row["category"] = definition.category
        if definition.severity and definition.severity != Severity.ROUTINE:
            row["severity"] = definition.severity
        if definition.instructions:
            row["instructions"] = definition.instructions
        for field in ("interval_distance", "interval_months", "interval_hours"):
            if getattr(item, field):
                row[field] = getattr(item, field)
        if item.interval_distance:
            row["interval_unit"] = item.interval_unit
        items.append(row)

    data = {
        "name": template.name,
        "slug": template.slug,
        "author": template.author,
        "description": template.description,
        "asset_kinds": list(template.asset_kinds or []),
        "vehicle_classes": list(template.vehicle_classes or []),
        "items": items,
    }
    data = {key: value for key, value in data.items() if value not in ("", [], None)}
    return yaml.safe_dump(data, sort_keys=False, allow_unicode=True)


def parse(text: str) -> dict:
    """Read a file and check it, without touching the database.

    Separate from `load` so a catalog can show somebody what a template
    contains before installing it, and so the checks run identically whether
    the bytes came from an upload or from a repository.
    """
    if len(text.encode("utf-8")) > MAX_BYTES:
        raise TemplateInvalid(_("That file is too large to be a schedule."))
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

    raw_items = data.get("items") or []
    if not isinstance(raw_items, list) or not raw_items:
        raise TemplateInvalid(_("A template with no items is not a schedule."))

    items = []
    for index, row in enumerate(raw_items, start=1):
        if not isinstance(row, dict):
            raise TemplateInvalid(
                _("Item %(n)d is not a mapping of fields.") % {"n": index}
            )
        unknown = set(row) - ITEM_KEYS
        if unknown:
            raise TemplateInvalid(
                _("Item %(n)d has unrecognized field(s): %(keys)s")
                % {"n": index, "keys": ", ".join(sorted(unknown))}
            )
        if not str(row.get("name", "")).strip():
            raise TemplateInvalid(_("Item %(n)d needs a name.") % {"n": index})

        unit = str(row.get("interval_unit") or "mi")
        if unit not in UNITS:
            raise TemplateInvalid(
                _("Item %(n)d uses an unknown interval unit: %(unit)s")
                % {"n": index, "unit": unit}
            )
        for field in ("interval_distance", "interval_months", "interval_hours"):
            value = row.get(field)
            if value is None:
                continue
            if not isinstance(value, int) or value <= 0:
                raise TemplateInvalid(
                    _("Item %(n)d has a %(field)s that is not a positive whole number.")
                    % {"n": index, "field": field}
                )
        if not any(row.get(f) for f in ("interval_distance", "interval_months", "interval_hours")):
            # The same rule `AssetServiceItem.clean` enforces. Catching it here
            # means the file is refused with a line number rather than half
            # imported and then rejected row by row.
            raise TemplateInvalid(
                _("Item %(n)d has no interval — give it distance, time or hours.")
                % {"n": index}
            )

        severity = str(row.get("severity") or Severity.ROUTINE)
        if severity not in Severity.values:
            raise TemplateInvalid(
                _("Item %(n)d has an unknown severity: %(value)s")
                % {"n": index, "value": severity}
            )
        items.append({**row, "interval_unit": unit, "severity": severity})

    return {**data, "items": items}


def load(text: str, *, source: str = ScheduleTemplate.Source.IMPORTED) -> ScheduleTemplate:
    """Check a file and write it, definitions and all.

    A name already in use is a refusal rather than an overwrite: a template is
    applied to vehicles, and quietly replacing one with a stranger's file would
    change what the shop believes is due without anybody deciding to.
    """
    data = parse(text)
    name = str(data["name"]).strip()[:120]
    slug = str(data.get("slug") or slugify(name))[:64]

    if ScheduleTemplate.all_objects.filter(name=name).exists():
        raise TemplateInvalid(
            _("A template called %(name)s is already here. Rename one of them.")
            % {"name": name}
        )
    if ScheduleTemplate.all_objects.filter(slug=slug).exists():
        slug = f"{slug[:56]}-{ScheduleTemplate.all_objects.count() + 1}"

    template = ScheduleTemplate.objects.create(
        name=name,
        slug=slug,
        description=str(data.get("description") or "")[:2000],
        source=source,
        author=str(data.get("author") or "")[:80],
        asset_kinds=list(data.get("asset_kinds") or []),
        vehicle_classes=list(data.get("vehicle_classes") or []),
    )

    for sequence, row in enumerate(data["items"]):
        TemplateItem.objects.create(
            template=template,
            definition=_definition_for(row),
            interval_distance=row.get("interval_distance"),
            interval_unit=row["interval_unit"],
            interval_months=row.get("interval_months"),
            interval_hours=row.get("interval_hours"),
            sequence=sequence,
        )
    return template


def _definition_for(row: dict) -> ServiceDefinition:
    """The receiving instance's own definition for this item, or a new one.

    Matched on `translation_key` before name, because the key is stable across
    languages and the name is not: a shop running in French has "Vidange
    d'huile moteur" for the same shipped item, and matching on the name alone
    would give it a duplicate in English every time it imported anything.
    """
    key = str(row.get("translation_key") or "").strip()
    if key:
        found = ServiceDefinition.objects.filter(translation_key=key).first()
        if found is not None:
            return found

    name = str(row["name"]).strip()[:120]
    found = ServiceDefinition.objects.filter(name=name).first()
    if found is not None:
        return found

    return ServiceDefinition.objects.create(
        name=name,
        translation_key=key[:80],
        category=str(row.get("category") or "")[:48],
        severity=row["severity"],
        instructions=str(row.get("instructions") or ""),
    )
