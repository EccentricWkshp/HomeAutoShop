"""
LubeLogger import (SPEC §8.6, INTEGRATION-LUBELOGGER.md).

There is no live instance to test against, so the client is driven by a stub
that returns the documented record shapes. That is enough to prove the parts
that actually carry risk: the locale guard, idempotency, dry-run safety, the
vehicle-matching rules, and the promise that nothing here can delete history.
"""

from __future__ import annotations

from decimal import Decimal

from django.test import TestCase

from homeautoshop.assets.models import Asset, UsageReading
from homeautoshop.core.integrations.importer import Importer, run_import
from homeautoshop.core.integrations.lubelogger import (
    LocaleFormatError,
    parse_date,
    parse_number,
    pick,
)
from homeautoshop.core.models import ExternalRef
from homeautoshop.purchasing.models import Expense
from homeautoshop.work.models import WorkOrder

VIN = "1M8GDM9AXKP042788"


class StubClient:
    """Stands in for a LubeLogger instance, returning documented shapes."""

    base_url = "https://lubelog.example.test"

    def __init__(self, vehicles=None, records=None):
        self._vehicles = vehicles if vehicles is not None else [
            {"id": 1, "year": 2004, "make": "Ford", "model": "F-150", "vin": VIN}
        ]
        self._records = records or {}
        self.calls: list[tuple[str, str]] = []

    def vehicles(self):
        return self._vehicles

    def records(self, kind, vehicle_id):
        self.calls.append((kind, str(vehicle_id)))
        return self._records.get(kind, [])


class NumberParsingTests(TestCase):
    """The locale hazard — the single most dangerous thing in this integration."""

    def test_invariant_numbers_parse(self):
        self.assertEqual(parse_number("1234.56"), Decimal("1234.56"))
        self.assertEqual(parse_number("1,234.56"), Decimal("1234.56"))
        self.assertEqual(parse_number(42), Decimal(42))
        self.assertEqual(parse_number(""), Decimal(0))

    def test_a_comma_decimal_is_refused_rather_than_truncated(self):
        # This is the bug the whole design exists to prevent: 1.234,56 quietly
        # becoming 1.23 and corrupting every cost report downstream.
        for value in ("1.234,56", "1234,56", "12,50"):
            with self.subTest(value=value):
                with self.assertRaises(LocaleFormatError):
                    parse_number(value, field_name="cost")

    def test_garbage_is_refused(self):
        with self.assertRaises(LocaleFormatError):
            parse_number("not a number")

    def test_dates_parse_or_refuse(self):
        self.assertEqual(str(parse_date("2026-08-29")), "2026-08-29")
        self.assertIsNone(parse_date(""))
        with self.assertRaises(LocaleFormatError):
            parse_date("29 Aug 26")

    def test_field_names_survive_casing_differences(self):
        row = {"VehicleId": 7, "odometer": 100}
        self.assertEqual(pick(row, "vehicleId"), 7)
        self.assertEqual(pick(row, "mileage", "odometer"), 100)
        self.assertIsNone(pick(row, "missing"))


class VehicleMatchingTests(TestCase):
    def test_matched_by_vin(self):
        asset = Asset.objects.create(nickname="Red truck", vin=VIN)
        importer = Importer(StubClient(), dry_run=False)
        matches = importer.match_vehicles()
        self.assertEqual(matches[0].asset, asset)
        self.assertEqual(matches[0].how, "vin")

    def test_a_second_run_matches_by_external_ref(self):
        Asset.objects.create(nickname="Red truck", vin=VIN)
        Importer(StubClient(), dry_run=False).match_vehicles()
        second = Importer(StubClient(), dry_run=False).match_vehicles()
        self.assertEqual(second[0].how, "external_ref")
        self.assertEqual(ExternalRef.objects.filter(external_type="vehicle").count(), 1)

    def test_a_fuzzy_match_is_reported_not_merged(self):
        """A wrong link writes another vehicle's history into this one."""
        Asset.objects.create(nickname="Other truck", make="Ford", model="F-150", year=2004)
        importer = Importer(StubClient(vehicles=[
            {"id": 1, "year": 2004, "make": "Ford", "model": "F-150", "vin": ""}
        ]), dry_run=False)
        matches = importer.match_vehicles()
        self.assertIsNone(matches[0].asset)
        self.assertEqual(matches[0].how, "ambiguous")

    def test_unmatched_vehicles_are_reported_by_default(self):
        importer = Importer(StubClient(), dry_run=False)
        importer.match_vehicles()
        self.assertEqual(len(importer.report.unmatched), 1)
        self.assertEqual(Asset.objects.count(), 0)

    def test_create_missing_creates_them(self):
        importer = Importer(StubClient(), dry_run=False, create_missing=True)
        importer.match_vehicles()
        asset = Asset.objects.get()
        self.assertEqual(asset.vin, VIN)
        self.assertEqual(asset.make, "Ford")


class RecordMappingTests(TestCase):
    def setUp(self):
        self.asset = Asset.objects.create(nickname="Red truck", vin=VIN, meter_unit="mi")

    def _client(self, **records) -> StubClient:
        return StubClient(records=records)

    def test_odometer_becomes_a_usage_reading(self):
        run_import(dry_run=False, client=self._client(
            odometer=[{"id": 10, "date": "2026-01-05", "odometer": "142000", "notes": "oil change"}]
        ))
        reading = UsageReading.objects.get()
        self.assertEqual(reading.value, Decimal("142000"))
        self.assertEqual(reading.source, UsageReading.Source.IMPORT)
        self.assertEqual(str(reading.read_on), "2026-01-05")

    def test_fuel_becomes_a_reading_plus_an_expense(self):
        """No native fuel logging (OQ-3), but the odometer series stays dense."""
        run_import(dry_run=False, client=self._client(
            fuel=[{"id": 20, "date": "2026-02-01", "odometer": "142500", "cost": "62.40"}]
        ))
        self.assertEqual(UsageReading.objects.count(), 1)
        expense = Expense.objects.get()
        self.assertEqual(expense.category, "fuel")
        self.assertEqual(expense.amount_minor, 6240)

    def test_service_becomes_a_completed_work_order(self):
        run_import(dry_run=False, client=self._client(
            service=[{"id": 30, "date": "2026-03-01", "description": "Oil and filter",
                      "cost": "89.50", "odometer": "143000"}]
        ))
        work_order = WorkOrder.objects.get()
        self.assertEqual(work_order.type, "maintenance")
        self.assertEqual(work_order.status, "complete")
        self.assertEqual(work_order.title, "Oil and filter")
        self.assertEqual(work_order.job_items.count(), 1)
        # The imported total is recorded as one expense; inventing part lines
        # would be fabrication.
        self.assertEqual(work_order.expenses.get().amount_minor, 8950)

    def test_repair_and_upgrade_map_to_their_own_types(self):
        run_import(dry_run=False, client=self._client(
            repair=[{"id": 40, "date": "2026-03-02", "description": "Alternator", "cost": "0"}],
            upgrade=[{"id": 41, "date": "2026-03-03", "description": "LED headlights", "cost": "0"}],
        ))
        self.assertEqual(WorkOrder.objects.filter(type="repair").count(), 1)
        self.assertEqual(WorkOrder.objects.filter(type="modification").count(), 1)

    def test_tax_records_are_categorised_from_their_description(self):
        run_import(dry_run=False, client=self._client(
            tax=[
                {"id": 50, "date": "2026-01-01", "description": "Plate renewal", "cost": "95.00"},
                {"id": 51, "date": "2026-01-02", "description": "Safety inspection", "cost": "35.00"},
                {"id": 52, "date": "2026-01-03", "description": "Something else", "cost": "10.00"},
            ]
        ))
        categories = set(Expense.objects.values_list("category", flat=True))
        self.assertEqual(categories, {"registration", "inspection", "other"})

    def test_plans_become_planned_work_orders(self):
        run_import(dry_run=False, client=self._client(
            plan=[{"id": 60, "description": "Replace timing belt"}]
        ))
        self.assertEqual(WorkOrder.objects.get().status, "planned")

    def test_a_locale_formatted_cost_stops_the_import(self):
        """Refusing beats importing wrong money."""
        report = run_import(dry_run=False, client=self._client(
            service=[{"id": 70, "date": "2026-03-01", "description": "Oil", "cost": "1.234,56"}]
        ))
        self.assertTrue(report.errors)
        self.assertIn("locale-formatted", report.errors[0])
        self.assertEqual(WorkOrder.objects.count(), 0)


class SafetyTests(TestCase):
    def setUp(self):
        self.asset = Asset.objects.create(nickname="Red truck", vin=VIN)
        self.records = {
            "odometer": [{"id": 10, "date": "2026-01-05", "odometer": "142000"}],
            "service": [{"id": 30, "date": "2026-03-01", "description": "Oil", "cost": "50.00"}],
        }

    def test_a_dry_run_writes_nothing(self):
        report = run_import(dry_run=True, client=StubClient(records=self.records))
        self.assertGreater(report.total_created, 0)
        self.assertEqual(UsageReading.objects.count(), 0)
        self.assertEqual(WorkOrder.objects.count(), 0)
        self.assertEqual(ExternalRef.objects.count(), 0)

    def test_re_running_does_not_duplicate(self):
        run_import(dry_run=False, client=StubClient(records=self.records))
        self.assertEqual(WorkOrder.objects.count(), 1)
        # One reading: the odometer record. The service row carries no odometer,
        # so no second reading is invented for it.
        self.assertEqual(UsageReading.objects.count(), 1)

        second = run_import(dry_run=False, client=StubClient(records=self.records))
        self.assertEqual(WorkOrder.objects.count(), 1)
        self.assertEqual(second.skipped.get("service"), 1)

    def test_a_source_change_after_import_is_reported_not_applied(self):
        run_import(dry_run=False, client=StubClient(records=self.records))
        changed = {
            "odometer": self.records["odometer"],
            "service": [{"id": 30, "date": "2026-03-01", "description": "Oil AND filter", "cost": "75.00"}],
        }
        report = run_import(dry_run=False, client=StubClient(records=changed))
        self.assertTrue(any("changed at the source" in c for c in report.conflicts))
        # The local record is untouched: conflicts are surfaced, never resolved.
        self.assertEqual(WorkOrder.objects.get().title, "Oil")

    def test_a_record_removed_at_the_source_is_not_deleted_here(self):
        """Deletions never propagate — this history may be handed to a buyer."""
        run_import(dry_run=False, client=StubClient(records=self.records))
        run_import(dry_run=False, client=StubClient(records={"odometer": self.records["odometer"]}))
        self.assertEqual(WorkOrder.objects.count(), 1)

    def test_an_unreachable_record_type_does_not_abort_the_import(self):
        class PartlyBroken(StubClient):
            def records(self, kind, vehicle_id):
                if kind == "fuel":
                    raise RuntimeError("404 Not Found")
                return super().records(kind, vehicle_id)

        report = run_import(dry_run=False, client=PartlyBroken(records=self.records))
        self.assertTrue(any("fuel" in e for e in report.errors))
        self.assertEqual(WorkOrder.objects.count(), 1)  # everything else still landed


class IdentifierShapeTests(TestCase):
    """LubeLogger has one identifier column and it is not called `vin`.

    `vehicleIdentifier` names the *kind* of identifier — it comes back as the
    literal string "License Plate" — and the value lives in `licensePlate`
    whatever kind was chosen. On the instance this was written against the
    operator had put full VINs in there, so reading `licensePlate` as a plate
    searched for two 17-character VINs among the license plates, found nothing,
    and reported both vehicles as unmatchable.

    So the value is classified by shape rather than by the column it arrived in.
    """

    def test_a_vin_in_the_plate_column_is_recognized_as_a_vin(self):
        from homeautoshop.core.integrations.importer import identifiers_from

        vin, plate = identifiers_from({"licensePlate": "1M8GDM9AXKP042788"})
        self.assertEqual(vin, "1M8GDM9AXKP042788")
        self.assertEqual(plate, "")

    def test_a_plate_in_the_plate_column_is_still_a_plate(self):
        from homeautoshop.core.integrations.importer import identifiers_from

        vin, plate = identifiers_from({"licensePlate": "ABC-1234"})
        self.assertEqual(vin, "")
        self.assertEqual(plate, "ABC-1234")

    def test_a_pre_1981_vin_shape_is_read_as_a_plate_here(self):
        """A short VIN and a plate are the same shape, and this column could
        hold either. Reading eleven characters as a VIN would file every plate
        of that length as one — so an import stays with the safer reading and
        a short VIN is typed on the vehicle instead."""
        from homeautoshop.core.integrations.importer import identifiers_from

        vin, plate = identifiers_from({"licensePlate": "F26SLU12345"})

        self.assertEqual(vin, "")
        self.assertEqual(plate, "F26SLU12345")

    def test_the_kind_label_is_not_mistaken_for_a_value(self):
        """`vehicleIdentifier` really does come back as "License Plate"."""
        from homeautoshop.core.integrations.importer import identifiers_from

        vin, plate = identifiers_from(
            {"vehicleIdentifier": "License Plate", "licensePlate": "1M8GDM9AXKP042788"}
        )
        self.assertEqual(vin, "1M8GDM9AXKP042788")

    def test_junk_is_neither(self):
        from homeautoshop.core.integrations.importer import identifiers_from

        vin, plate = identifiers_from({"licensePlate": "1"})
        self.assertEqual(vin, "")
        self.assertEqual(plate, "1")

    def test_nothing_is_neither(self):
        from homeautoshop.core.integrations.importer import identifiers_from

        self.assertEqual(identifiers_from({}), ("", ""))
