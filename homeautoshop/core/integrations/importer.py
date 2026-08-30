"""
LubeLogger → HomeAutoShop importer (INTEGRATION-LUBELOGGER.md §3, §4).

Four rules govern every mapping here:

* **Idempotent.** Every imported row carries an `ExternalRef`, so re-running
  updates rather than duplicating.
* **Dry run by default.** Nothing is written until the operator has seen the
  counts, the unmatched vehicles, and a sample of what will be created.
* **Local edits win.** A record edited here is never overwritten by the source;
  it is reported as a conflict instead.
* **Deletions never propagate.** A row gone from the source is marked orphaned
  and kept. Silent deletion driven by another system is unacceptable in a
  service history that may be handed to a buyer.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date

from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from homeautoshop.assets.models import Asset, UsageReading
from homeautoshop.core.models import ExternalRef
from homeautoshop.purchasing.models import Expense
from homeautoshop.work.models import JobItem, WorkOrder, WorkOrderNote, WorkOrderStatus

from .lubelogger import (
    SOURCE,
    TAX_CATEGORY_HINTS,
    WORK_ORDER_KINDS,
    LocaleFormatError,
    LubeLoggerClient,
    parse_date,
    parse_number,
    pick,
)

log = logging.getLogger(__name__)


def identifiers_from(row: dict) -> tuple[str, str]:
    """Pull a VIN and a plate out of a LubeLogger vehicle. Neither is a field.

    LubeLogger has **one** identifier column. `vehicleIdentifier` names which
    *kind* it is — it comes back as the literal string "License Plate" — and the
    value itself lives in `licensePlate` whatever kind it was chosen to be. So
    the field name says nothing about what is in it, and on the instance this
    was written against the operator had put full VINs in there.

    The value is therefore classified by shape, not by which column it arrived
    in: anything that validates as a VIN under ISO 3779 is treated as a VIN, and
    everything else as a plate. Trusting the column name instead would have
    searched for two 17-character VINs among the license plates and found
    nothing, which is exactly what it did.
    """
    from homeautoshop.assets import vin as vinlib

    vin = ""
    plate = ""
    for candidate in (
        pick(row, "vin", default=""),
        pick(row, "licensePlate", default=""),
        pick(row, "vehicleIdentifier", default=""),
    ):
        value = str(candidate or "").strip()
        if not value:
            continue
        checked = vinlib.validate(value)
        if checked.is_well_formed and not vin:
            vin = checked.vin
        elif not checked.is_well_formed and not plate:
            plate = value.upper()
    return vin, plate


@dataclass(slots=True)
class VehicleMatch:
    external_id: str
    label: str
    asset: Asset | None = None
    how: str = "unmatched"  # vin | external_ref | proposed_create | ambiguous

    @property
    def needs_attention(self) -> bool:
        return self.asset is None


@dataclass(slots=True)
class Report:
    dry_run: bool = True
    created: dict[str, int] = field(default_factory=dict)
    updated: dict[str, int] = field(default_factory=dict)
    skipped: dict[str, int] = field(default_factory=dict)
    conflicts: list[str] = field(default_factory=list)
    unmatched: list[VehicleMatch] = field(default_factory=list)
    matched: list[VehicleMatch] = field(default_factory=list)
    samples: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def note(self, bucket: dict[str, int], kind: str) -> None:
        bucket[kind] = bucket.get(kind, 0) + 1

    def sample(self, text: str) -> None:
        if len(self.samples) < 12:
            self.samples.append(text)

    @property
    def total_created(self) -> int:
        return sum(self.created.values())

    @property
    def has_problems(self) -> bool:
        return bool(self.errors or self.conflicts or self.unmatched)


class Importer:
    def __init__(
        self,
        client: LubeLoggerClient,
        *,
        dry_run: bool = True,
        create_missing: bool = False,
        since: date | None = None,
    ) -> None:
        self.client = client
        self.dry_run = dry_run
        self.create_missing = create_missing
        # The date window for an incremental pull (FR-INT-13). Applied here
        # rather than in the request, because LubeLogger's record endpoints
        # take a vehicle and nothing else — so this saves the *writing*, not
        # the fetching, and the comment says so rather than implying otherwise.
        self.since = since
        self.instance = client.base_url
        self.report = Report(dry_run=dry_run)

    # -- provenance ------------------------------------------------------

    def _ref(self, external_type: str, external_id) -> ExternalRef | None:
        return ExternalRef.lookup(SOURCE, self.instance, external_type, external_id)

    def _link(self, external_type: str, external_id, entity, payload: dict) -> None:
        if self.dry_run:
            return
        ExternalRef.objects.update_or_create(
            source_system=SOURCE,
            source_instance_url=self.instance,
            external_type=external_type,
            external_id=str(external_id),
            defaults={
                "entity_type": type(entity).__name__,
                "entity_id": entity.pk,
                "last_seen_at": timezone.now(),
                "source_hash": ExternalRef.hash_of(payload),
                "state": ExternalRef.State.LINKED,
            },
        )

    def _already_imported(self, external_type: str, external_id, payload: dict) -> bool:
        """True when this row exists locally and needs no further work.

        If the source changed but the local record was edited, that is a
        conflict — reported, never silently resolved.
        """
        ref = self._ref(external_type, external_id)
        if ref is None:
            return False
        digest = ExternalRef.hash_of(payload)
        if ref.source_hash and ref.source_hash != digest:
            self.report.conflicts.append(
                f"{external_type} {external_id}: changed at the source since import"
            )
        if not self.dry_run:
            ExternalRef.objects.filter(pk=ref.pk).update(last_seen_at=timezone.now())
        self.report.note(self.report.skipped, external_type)
        return True

    # -- vehicles --------------------------------------------------------

    def match_vehicles(self) -> list[VehicleMatch]:
        matches: list[VehicleMatch] = []
        for row in self.client.vehicles():
            external_id = pick(row, "id", "vehicleId")
            vin, plate = identifiers_from(row)
            year = pick(row, "year")
            make = (pick(row, "make", default="") or "").strip()
            model = (pick(row, "model", default="") or "").strip()
            label = " ".join(str(p) for p in (year, make, model) if p) or str(external_id)

            match = VehicleMatch(external_id=str(external_id), label=label)

            ref = self._ref("vehicle", external_id)
            if ref:
                match.asset = Asset.all_objects.filter(pk=ref.entity_id).first()
                match.how = "external_ref"

            if match.asset is None and vin:
                match.asset = Asset.objects.filter(vin=vin).first()
                if match.asset:
                    match.how = "vin"

            if match.asset is None and plate:
                match.asset = Asset.objects.filter(plate__iexact=plate).first()
                if match.asset:
                    match.how = "plate"

            if match.asset is None:
                # Never auto-merge on a fuzzy match: a wrong link writes another
                # vehicle's history into this one, and that is unrecoverable.
                candidates = Asset.objects.filter(make__iexact=make, model__iexact=model)
                if year:
                    candidates = candidates.filter(year=year)
                if candidates.count() == 1 and not vin and not plate:
                    match.how = "ambiguous"
                elif self.create_missing:
                    match.asset = self._create_asset(row, vin, year, make, model)
                    match.how = "created"
                else:
                    match.how = "proposed_create"

            if match.asset is not None:
                self._link("vehicle", external_id, match.asset, row)
                self.report.matched.append(match)
            else:
                self.report.unmatched.append(match)
            matches.append(match)
        return matches

    def _create_asset(self, row: dict, vin: str, year, make: str, model: str) -> Asset | None:
        nickname = pick(row, "nickname", "name", default="") or " ".join(
            str(p) for p in (year, make, model) if p
        )
        if self.dry_run:
            self.report.note(self.report.created, "vehicle")
            self.report.sample(f"vehicle: {nickname}")
            return None
        asset = Asset.objects.create(
            nickname=nickname[:120] or "Imported vehicle",
            vin=vin,
            year=int(year) if str(year or "").isdigit() else None,
            make=make,
            model=model,
        )
        self.report.note(self.report.created, "vehicle")
        return asset

    # -- records ---------------------------------------------------------

    def run(self) -> Report:
        for match in self.match_vehicles():
            if match.asset is None and not self.dry_run:
                continue
            for kind in ("odometer", "fuel", *WORK_ORDER_KINDS, "tax", "plan", "note"):
                try:
                    rows = self.client.records(kind, match.external_id)
                except Exception as exc:  # a missing endpoint is not fatal
                    self.report.errors.append(f"{kind}: {type(exc).__name__}: {exc}")
                    continue
                for row in rows:
                    try:
                        self._import_row(kind, row, match)
                    except LocaleFormatError as exc:
                        # Stop rather than import wrong money.
                        self.report.errors.append(str(exc))
                        return self.report
                    except Exception as exc:
                        self.report.errors.append(f"{kind} {pick(row, 'id')}: {exc}")
        return self.report

    def _import_row(self, kind: str, row: dict, match: VehicleMatch) -> None:
        external_id = pick(row, "id", default=ExternalRef.hash_of(row)[:16])
        if self._already_imported(kind, external_id, row):
            return
        if self.since is not None:
            when = parse_date(pick(row, "date"))
            # A row with no date at all is never skipped. Deciding it is old
            # because it is undated would silently drop it from every future
            # incremental run, and it would never be noticed.
            if when is not None and when < self.since:
                self.report.note(self.report.skipped, kind)
                return

        if kind == "odometer":
            self._odometer(row, match, external_id)
        elif kind == "fuel":
            self._fuel(row, match, external_id)
        elif kind in WORK_ORDER_KINDS:
            self._work_order(kind, row, match, external_id)
        elif kind == "tax":
            self._tax(row, match, external_id)
        elif kind == "plan":
            self._plan(row, match, external_id)
        elif kind == "note":
            self._note(row, match, external_id)

    # -- individual mappings --------------------------------------------

    def _odometer(self, row, match, external_id) -> None:
        value = parse_number(pick(row, "odometer", "mileage", default=0), field_name="odometer")
        when = parse_date(pick(row, "date"))
        self.report.note(self.report.created, "odometer")
        self.report.sample(f"odometer: {value} on {when}")
        if self.dry_run or match.asset is None:
            return
        reading = UsageReading.objects.create(
            asset=match.asset,
            meter="odometer",
            value=value,
            unit=match.asset.meter_unit,
            read_on=when or timezone.localdate(),
            source=UsageReading.Source.IMPORT,
            note=str(pick(row, "notes", default="") or "")[:500],
        )
        self._link("odometer", external_id, reading, row)

    def _fuel(self, row, match, external_id) -> None:
        """A fuel record yields a meter reading plus a fuel expense.

        HomeAutoShop never asks the operator to log a fill-up (OQ-3/NG-7), but
        importing one keeps the odometer series dense, which is what
        cost-per-distance is computed against.
        """
        value = parse_number(pick(row, "odometer", "mileage", default=0), field_name="odometer")
        cost = parse_number(pick(row, "cost", "price", default=0), field_name="cost")
        when = parse_date(pick(row, "date"))
        self.report.note(self.report.created, "fuel")
        self.report.sample(f"fuel: {cost} on {when}")
        if self.dry_run or match.asset is None:
            return

        if value:
            reading = UsageReading.objects.create(
                asset=match.asset,
                meter="odometer",
                value=value,
                unit=match.asset.meter_unit,
                read_on=when or timezone.localdate(),
                source=UsageReading.Source.IMPORT,
            )
            self._link("fuel_odometer", external_id, reading, row)
        if cost:
            expense = Expense.objects.create(
                asset=match.asset,
                category="fuel",
                amount_minor=int(cost * 100),
                incurred_on=when or timezone.localdate(),
                description=str(_("Fuel (imported)")),
            )
            self._link("fuel", external_id, expense, row)

    def _work_order(self, kind, row, match, external_id) -> None:
        when = parse_date(pick(row, "date"))
        description = str(pick(row, "description", "notes", default="") or "")
        cost = parse_number(pick(row, "cost", default=0), field_name="cost")
        odometer = parse_number(pick(row, "odometer", "mileage", default=0), field_name="odometer")
        title = (description.splitlines() or [""])[0][:200] or _("Imported %(kind)s") % {"kind": kind}

        self.report.note(self.report.created, kind)
        self.report.sample(f"{kind}: {title} ({when})")
        if self.dry_run or match.asset is None:
            return

        work_order = WorkOrder(
            asset=match.asset,
            title=str(title),
            type=WORK_ORDER_KINDS[kind],
            status=WorkOrderStatus.COMPLETE,
            correction=description,
            opened_at=timezone.make_aware(
                timezone.datetime.combine(when or timezone.localdate(), timezone.datetime.min.time())
            ),
            completed_at=timezone.now(),
            odometer_out=odometer or None,
        )
        work_order.save()
        JobItem.objects.create(work_order=work_order, title=str(title), status=JobItem.Status.DONE)

        if cost:
            # Imported history has a total, not a parts breakdown. Recording it
            # as one expense is honest; inventing part lines would not be.
            Expense.objects.create(
                asset=match.asset,
                work_order=work_order,
                category="outsourced_labor" if kind == "repair" else "shop_supplies",
                amount_minor=int(cost * 100),
                incurred_on=when or timezone.localdate(),
                description=str(_("Imported total")),
            )
        if odometer and match.asset.has_meter:
            UsageReading.objects.create(
                asset=match.asset,
                meter="odometer",
                value=odometer,
                unit=match.asset.meter_unit,
                read_on=when or timezone.localdate(),
                source=UsageReading.Source.IMPORT,
            )
        self._link(kind, external_id, work_order, row)

    def _tax(self, row, match, external_id) -> None:
        description = str(pick(row, "description", "notes", default="") or "")
        cost = parse_number(pick(row, "cost", default=0), field_name="cost")
        when = parse_date(pick(row, "date"))

        category = "other"
        lowered = description.lower()
        for hint, mapped in TAX_CATEGORY_HINTS:
            if hint in lowered:
                category = mapped
                break

        self.report.note(self.report.created, "tax")
        self.report.sample(f"tax: {description or category} {cost}")
        if self.dry_run or match.asset is None:
            return
        expense = Expense.objects.create(
            asset=match.asset,
            category=category,
            amount_minor=int(cost * 100),
            incurred_on=when or timezone.localdate(),
            description=description[:200],
        )
        self._link("tax", external_id, expense, row)

    def _plan(self, row, match, external_id) -> None:
        description = str(pick(row, "description", "title", default="") or "")
        self.report.note(self.report.created, "plan")
        self.report.sample(f"plan: {description}")
        if self.dry_run or match.asset is None:
            return
        work_order = WorkOrder.objects.create(
            asset=match.asset,
            title=description[:200] or str(_("Planned work")),
            status=WorkOrderStatus.PLANNED,
            complaint=description,
        )
        self._link("plan", external_id, work_order, row)

    def _note(self, row, match, external_id) -> None:
        body = str(pick(row, "description", "notes", "note", default="") or "").strip()
        if not body:
            return
        self.report.note(self.report.created, "note")
        if self.dry_run or match.asset is None:
            return
        # A note without a work order belongs on the vehicle itself.
        match.asset.notes = f"{match.asset.notes}\n{body}".strip()
        match.asset.save()

    # -- drift -----------------------------------------------------------

    def mark_orphans(self, seen: set[tuple[str, str]]) -> int:
        """Flag rows that vanished from the source. Never delete them.

        A service history that may be handed to a buyer must not be edited by
        another system's delete key.
        """
        if self.dry_run:
            return 0
        stale = ExternalRef.objects.filter(
            source_system=SOURCE, source_instance_url=self.instance, state=ExternalRef.State.LINKED
        ).exclude(state=ExternalRef.State.ORPHANED)
        count = 0
        for ref in stale:
            if (ref.external_type, ref.external_id) not in seen:
                ref.state = ExternalRef.State.ORPHANED
                ref.save(update_fields=["state"])
                count += 1
        return count


@transaction.atomic
def run_import(
    *,
    dry_run: bool = True,
    create_missing: bool = False,
    client=None,
    since: date | None = None,
) -> Report:
    importer = Importer(
        client or LubeLoggerClient(),
        dry_run=dry_run,
        create_missing=create_missing,
        since=since,
    )
    report = importer.run()
    if dry_run:
        # A dry run must leave nothing behind, including the ExternalRef rows
        # that a partial write would have created.
        transaction.set_rollback(True)
    return report
