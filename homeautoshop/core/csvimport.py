"""
CSV import with column mapping (SPEC FR-ADM-6).

*"So the spreadsheet this replaces can actually come along."* That sentence is
the whole requirement, and it rules out the easy version: a fixed column layout
the operator has to rearrange their file into. Nobody does that. They give up
and keep the spreadsheet, and then the application is the second place they
have to look — which is worse than not having it.

So the file is taken as it is and the operator says which column is which. Three
entity types, because they are the three things a garage spreadsheet actually
holds: vehicles, parts, and service history.

Two properties that matter more than the mapping:

* **Dry run first, always.** The preview reports what would be written, what
  would be skipped and why, before a single row lands. An import that half
  worked and half did not is worse than one that refused.
* **Idempotent.** Every row is provenance-tracked through `external_ref` on a
  digest of its own content, so re-running the same file writes nothing the
  second time. People re-run imports; they add ten rows to the spreadsheet and
  import it again.
"""

from __future__ import annotations

import csv
import io
import logging
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from .models import ExternalRef

log = logging.getLogger(__name__)

SOURCE = "csv_import"


@dataclass(slots=True)
class Outcome:
    dry_run: bool = True
    kind: str = ""
    created: int = 0
    skipped: int = 0
    already: int = 0
    problems: list[str] = field(default_factory=list)
    samples: list[str] = field(default_factory=list)

    def problem(self, row_number: int, message) -> None:
        # Capped, and the cap is stated in the UI. A file with a wrong mapping
        # produces one problem per row, and five hundred identical lines tell
        # the operator nothing the first five did not.
        if len(self.problems) < 25:
            self.problems.append(f"{_('Row')} {row_number}: {message}")
        self.skipped += 1

    def sample(self, text: str) -> None:
        if len(self.samples) < 10:
            self.samples.append(text)


#: What each importable type can take, and which of those it cannot do without.
#: `required` is the honest minimum — a vehicle with only a nickname is a real
#: record somebody can build on; a service record with no date is not.
SCHEMAS: dict[str, dict] = {
    "vehicles": {
        "label": _("Vehicles"),
        "fields": (
            "nickname", "vin", "plate", "plate_region", "year", "make", "model",
            "trim", "engine", "color_exterior", "odometer", "acquired_on", "notes",
        ),
        "required": ("nickname",),
    },
    "parts": {
        "label": _("Parts"),
        "fields": (
            "name", "part_number", "brand", "category", "unit",
            "quantity", "location", "cost", "notes",
        ),
        "required": ("name",),
    },
    "service": {
        "label": _("Service history"),
        "fields": (
            "vin", "plate", "vehicle", "date", "odometer",
            "title", "description", "cost", "vendor",
        ),
        "required": ("date", "title"),
    },
}


def read(text: str) -> tuple[list[str], list[dict]]:
    # `Sniffer` rather than assuming commas: a European export is
    # semicolon-delimited and would otherwise arrive as one column per row,
    # which looks like a corrupt file rather than a delimiter mismatch.
    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    reader = csv.DictReader(io.StringIO(text), dialect=dialect)
    header = [h.strip() for h in (reader.fieldnames or []) if h and h.strip()]
    rows = [{(k or "").strip(): (v or "").strip() for k, v in row.items()} for row in reader]
    return header, rows


def guess(kind: str, header: list[str]) -> dict[str, str]:
    """Match columns to fields on name, forgivingly.

    Only the obvious ones. A guess the operator has to un-pick is worse than no
    guess, so anything ambiguous is left blank for them to set.
    """
    schema = SCHEMAS[kind]
    mapping: dict[str, str] = {}
    normalized = {column: _key(column) for column in header}
    for name in schema["fields"]:
        target = _key(name)
        for column, key in normalized.items():
            if key == target and column not in mapping.values():
                mapping[name] = column
                break
    return mapping


def _key(text: str) -> str:
    return "".join(ch for ch in (text or "").lower() if ch.isalnum())


def run(kind: str, rows: list[dict], mapping: dict[str, str], *, dry_run: bool = True, user=None) -> Outcome:
    if kind not in SCHEMAS:
        raise ValueError(f"unknown import type {kind}")

    outcome = Outcome(dry_run=dry_run, kind=kind)
    missing = [f for f in SCHEMAS[kind]["required"] if not mapping.get(f)]
    if missing:
        outcome.problems.append(
            str(_("Say which column holds: %(fields)s")) % {"fields": ", ".join(missing)}
        )
        return outcome

    handler = {"vehicles": _vehicle, "parts": _part, "service": _service}[kind]
    with transaction.atomic():
        for number, row in enumerate(rows, start=2):  # row 1 is the header
            values = {name: row.get(column, "").strip() for name, column in mapping.items()}
            if not any(values.values()):
                continue
            digest = ExternalRef.hash_of(values)
            if ExternalRef.objects.filter(
                source_system=SOURCE, external_type=kind, external_id=digest
            ).exists():
                outcome.already += 1
                continue
            try:
                handler(values, outcome, digest, dry_run=dry_run, user=user)
            except Exception as exc:  # noqa: BLE001 - per row, never fatal
                outcome.problem(number, exc)
        if dry_run:
            # A preview must leave nothing behind, provenance rows included.
            transaction.set_rollback(True)
    return outcome


def _link(kind: str, digest: str, entity) -> None:
    ExternalRef.objects.update_or_create(
        source_system=SOURCE,
        source_instance_url="",
        external_type=kind,
        external_id=digest,
        defaults={
            "entity_type": entity.__class__.__name__,
            "entity_id": entity.pk,
            "last_seen_at": timezone.now(),
        },
    )


def _vehicle(values: dict, outcome: Outcome, digest: str, *, dry_run: bool, user) -> None:
    from homeautoshop.assets.models import Asset
    from homeautoshop.assets.services import record_reading

    nickname = values.get("nickname") or " ".join(
        v for v in (values.get("year"), values.get("make"), values.get("model")) if v
    )
    if not nickname:
        raise ValueError(_("no name and nothing to build one from"))

    vin = (values.get("vin") or "").strip().upper()
    if vin and Asset.objects.filter(vin=vin).exists():
        # Matched on VIN rather than duplicated. Two rows for one truck is the
        # single most common thing a spreadsheet import gets wrong.
        outcome.already += 1
        return

    outcome.created += 1
    outcome.sample(f"{nickname}{f' · {vin[:3]}…' if vin else ''}")
    if dry_run:
        return

    asset = Asset.objects.create(
        nickname=nickname[:120],
        vin=vin,
        plate=(values.get("plate") or "")[:16],
        plate_region=(values.get("plate_region") or "")[:8],
        year=int(values["year"]) if str(values.get("year", "")).isdigit() else None,
        make=(values.get("make") or "")[:60],
        model=(values.get("model") or "")[:60],
        trim=(values.get("trim") or "")[:60],
        engine=(values.get("engine") or "")[:60],
        color_exterior=(values.get("color_exterior") or "")[:40],
        notes=values.get("notes") or "",
        created_by=user if getattr(user, "pk", None) else None,
    )
    if reading := _decimal(values.get("odometer")):
        record_reading(asset, reading, user=user)
    _link("vehicles", digest, asset)


def _part(values: dict, outcome: Outcome, digest: str, *, dry_run: bool, user) -> None:
    from homeautoshop.parts.models import Part

    name = values.get("name")
    if not name:
        raise ValueError(_("no part name"))

    outcome.created += 1
    outcome.sample(f"{name} {values.get('part_number', '')}".strip())
    if dry_run:
        return

    part = Part.objects.create(
        name=name[:200],
        part_number=(values.get("part_number") or "")[:80],
        # "Brand" is what a spreadsheet column is called; `manufacturer` is
        # what the model calls it. The mapping is the operator's word, the
        # column is ours.
        manufacturer=(values.get("brand") or "")[:80],
        category=(values.get("category") or "")[:64],
        notes=values.get("notes") or "",
        created_by=user if getattr(user, "pk", None) else None,
    )
    _link("parts", digest, part)


def _service(values: dict, outcome: Outcome, digest: str, *, dry_run: bool, user) -> None:
    from django.utils.dateparse import parse_date

    from homeautoshop.assets.models import Asset
    from homeautoshop.work.models import WorkOrder, WorkOrderStatus

    when = parse_date(values.get("date", "")[:10])
    if when is None:
        raise ValueError(_("that date could not be read"))

    asset = _match_vehicle(values)
    if asset is None:
        # Never guessed at. Attaching a service record to the wrong vehicle is
        # invisible afterwards and corrupts every cost figure derived from it.
        raise ValueError(_("no vehicle matched — add a VIN, plate or name column"))

    outcome.created += 1
    outcome.sample(f"{when} · {asset.nickname} · {values.get('title', '')}")
    if dry_run:
        return

    order = WorkOrder.objects.create(
        asset=asset,
        title=(values.get("title") or str(_("Imported service")))[:200],
        status=WorkOrderStatus.COMPLETE,
        correction=values.get("description") or "",
        opened_at=timezone.make_aware(
            timezone.datetime.combine(when, timezone.datetime.min.time())
        ),
        completed_at=timezone.make_aware(
            timezone.datetime.combine(when, timezone.datetime.min.time())
        ),
        odometer_out=_decimal(values.get("odometer")),
        created_by=user if getattr(user, "pk", None) else None,
    )
    _link("service", digest, order)


def _match_vehicle(values: dict):
    from homeautoshop.assets.models import Asset

    if vin := (values.get("vin") or "").strip().upper():
        if found := Asset.objects.filter(vin=vin).first():
            return found
    if plate := (values.get("plate") or "").strip().upper():
        if found := Asset.objects.filter(plate__iexact=plate).first():
            return found
    if name := (values.get("vehicle") or "").strip():
        matches = Asset.objects.filter(nickname__iexact=name)
        # Exactly one, or none. Two vehicles called "the truck" is a question
        # for a person, not a coin toss.
        if matches.count() == 1:
            return matches.first()
    return None


def _decimal(value):
    if not value:
        return None
    try:
        return Decimal(str(value).replace(",", "").strip())
    except (InvalidOperation, ValueError):
        return None
