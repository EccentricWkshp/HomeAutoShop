"""DVI: thresholds, positional expansion, snapshots, and wear projection (SPEC §7.8)."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.test import TestCase

from homeautoshop.accounts.models import User
from homeautoshop.assets.models import Asset
from homeautoshop.assets.services import record_reading
from homeautoshop.work.models import WorkOrder

from .models import (
    Inspection,
    InspectionPoint,
    InspectionResult,
    InspectionTemplate,
    PhotoRequirement,
    ResultStatus,
)
from .seed import install as install_templates
from .services import (
    compare,
    complete,
    convert_to_work_order,
    evaluate,
    record,
    start,
    wear_projection,
)

TREAD_THRESHOLDS = {"fail": {"lte": 2}, "attention": {"lte": 4}, "pass": {"gt": 4}}


def template_with_tread() -> InspectionTemplate:
    template = InspectionTemplate.objects.create(name="Tires", slug="tires")
    InspectionPoint.objects.create(
        template=template,
        name="Tire tread depth",
        area="tires_wheels",
        result_type=InspectionPoint.ResultType.MEASUREMENT,
        measurement_unit="/32in",
        positions=["LF", "RF", "LR", "RR"],
        sub_positions=["outer", "center", "inner"],
        thresholds=TREAD_THRESHOLDS,
        is_safety_critical=True,
    )
    return template


class ThresholdTests(TestCase):
    """FR-DVI-4 — most severe first, and silence when there is nothing to decide."""

    def test_evaluates_most_severe_first(self):
        self.assertEqual(evaluate(1, TREAD_THRESHOLDS), ResultStatus.FAIL)
        self.assertEqual(evaluate(2, TREAD_THRESHOLDS), ResultStatus.FAIL)
        self.assertEqual(evaluate(3, TREAD_THRESHOLDS), ResultStatus.ATTENTION)
        self.assertEqual(evaluate(4, TREAD_THRESHOLDS), ResultStatus.ATTENTION)
        self.assertEqual(evaluate(8, TREAD_THRESHOLDS), ResultStatus.PASS)

    def test_no_thresholds_means_no_opinion(self):
        # A rule that has nothing to say must not invent authority.
        self.assertEqual(evaluate(5, {}), "")
        self.assertEqual(evaluate(None, TREAD_THRESHOLDS), "")

    def test_between_and_directional_comparators(self):
        self.assertEqual(evaluate(12.2, {"fail": {"lt": 12.0}, "pass": {"gte": 12.0}}), ResultStatus.PASS)
        self.assertEqual(evaluate(11.5, {"fail": {"lt": 12.0}}), ResultStatus.FAIL)
        self.assertEqual(evaluate(5, {"attention": {"between": [4, 6]}}), ResultStatus.ATTENTION)
        self.assertEqual(evaluate(7, {"attention": {"between": [4, 6]}}), "")


class PositionalExpansionTests(TestCase):
    """FR-DVI-3 — twelve slots without defining twelve points by hand."""

    def setUp(self):
        self.asset = Asset.objects.create(nickname="Truck", meter_unit="mi")
        self.template = template_with_tread()

    def test_one_point_becomes_twelve_slots(self):
        inspection = start(self.asset, self.template)
        self.assertEqual(inspection.results.count(), 12)
        positions = set(inspection.results.values_list("position", flat=True))
        self.assertIn("LF/outer", positions)
        self.assertIn("RR/inner", positions)

    def test_a_point_with_no_positions_makes_one_slot(self):
        template = InspectionTemplate.objects.create(name="Simple", slug="simple")
        InspectionPoint.objects.create(template=template, name="Cold start", area="road_test")
        self.assertEqual(start(self.asset, template).results.count(), 1)

    def test_positions_without_sub_positions(self):
        template = InspectionTemplate.objects.create(name="Corners", slug="corners")
        InspectionPoint.objects.create(
            template=template, name="Rotor", area="brakes", positions=["LF", "RF", "LR", "RR"]
        )
        self.assertEqual(start(self.asset, template).results.count(), 4)


class SnapshotTests(TestCase):
    """FR-DVI-6 — editing a template must not rewrite an old inspection."""

    def setUp(self):
        self.asset = Asset.objects.create(nickname="Truck")
        self.template = template_with_tread()

    def test_the_template_is_frozen_onto_the_inspection(self):
        inspection = start(self.asset, self.template)
        point = self.template.points.get()
        point.name = "Tread (renamed)"
        point.thresholds = {"fail": {"lte": 1}}
        point.save()

        inspection.refresh_from_db()
        result = inspection.results.first()
        self.assertEqual(result.name, "Tire tread depth")
        self.assertEqual(result.point_snapshot["thresholds"], TREAD_THRESHOLDS)

    def test_deleting_the_template_leaves_the_inspection_readable(self):
        inspection = start(self.asset, self.template)
        self.template.delete(hard=True)
        inspection.refresh_from_db()
        self.assertEqual(inspection.template_name, "Tires")
        self.assertEqual(inspection.results.first().name, "Tire tread depth")


class RecordingTests(TestCase):
    def setUp(self):
        self.asset = Asset.objects.create(nickname="Truck")
        self.inspection = start(self.asset, template_with_tread())
        self.result = self.inspection.results.get(position="LF/outer")

    def test_a_measurement_sets_the_status_from_the_thresholds(self):
        record(self.result, value=3)
        self.assertEqual(self.result.auto_status, ResultStatus.ATTENTION)
        self.assertEqual(self.result.status, ResultStatus.ATTENTION)
        self.assertFalse(self.result.status_overridden)

    def test_an_override_is_recorded_alongside_the_rule(self):
        """The disagreement is the most interesting thing on the record."""
        record(self.result, value=3, status=ResultStatus.PASS, note="Selling it next month")
        self.assertEqual(self.result.auto_status, ResultStatus.ATTENTION)
        self.assertEqual(self.result.status, ResultStatus.PASS)
        self.assertTrue(self.result.status_overridden)
        self.assertTrue(self.result.disagreed)

    def test_agreeing_with_the_rule_is_not_an_override(self):
        record(self.result, value=3, status=ResultStatus.ATTENTION)
        self.assertFalse(self.result.status_overridden)
        self.assertFalse(self.result.disagreed)

    def test_a_negative_measurement_is_refused(self):
        self.result.measured_value = Decimal(-1)
        with self.assertRaises(ValidationError):
            self.result.full_clean()


class CompletionTests(TestCase):
    def setUp(self):
        self.asset = Asset.objects.create(nickname="Truck")
        self.template = template_with_tread()

    def test_unanswered_points_are_marked_not_inspected(self):
        inspection = start(self.asset, self.template)
        record(inspection.results.first(), value=8)
        complete(inspection)
        self.assertEqual(
            inspection.results.filter(status=ResultStatus.NOT_INSPECTED).count(), 11
        )

    def test_overall_is_the_worst_result(self):
        inspection = start(self.asset, self.template)
        for result in inspection.results.all():
            record(result, value=8)
        record(inspection.results.get(position="LR/inner"), value=1)
        complete(inspection)
        self.assertEqual(inspection.overall, ResultStatus.FAIL)

    def test_a_required_photo_blocks_sign_off(self):
        """FR-DVI-5 — a point whose template demands evidence must have it."""
        template = InspectionTemplate.objects.create(name="Photo", slug="photo")
        InspectionPoint.objects.create(
            template=template, name="DOT code", area="tires_wheels",
            photo_required=PhotoRequirement.ALWAYS,
        )
        inspection = start(self.asset, template)
        record(inspection.results.first(), status=ResultStatus.PASS)
        with self.assertRaises(ValidationError):
            complete(inspection)
        complete(inspection, force=True)  # the operator may still override
        self.assertEqual(inspection.status, Inspection.Status.COMPLETE)


class ConversionTests(TestCase):
    """FR-DVI-8 — flagged results become work, carrying their detail across."""

    def setUp(self):
        self.asset = Asset.objects.create(nickname="Truck", meter_unit="mi")
        record_reading(self.asset, 100_000)
        self.inspection = start(self.asset, template_with_tread())
        for result in self.inspection.results.all():
            record(result, value=8)
        record(self.inspection.results.get(position="LF/outer"), value=2, note="Corded on the edge")
        record(self.inspection.results.get(position="RF/outer"), value=4)
        complete(self.inspection)

    def test_flagged_results_become_job_items(self):
        work_order, items = convert_to_work_order(self.inspection)
        self.assertEqual(len(items), 2)
        self.assertEqual(work_order.asset, self.asset)
        self.assertTrue(work_order.is_safety_critical)
        self.assertEqual(work_order.odometer_in, Decimal(100_000))

    def test_the_measurement_and_note_travel_with_the_item(self):
        _wo, items = convert_to_work_order(self.inspection)
        failed = next(i for i in items if "LF/outer" in i.title)
        self.assertIn("2", failed.description)
        self.assertIn("Corded", failed.description)

    def test_converting_twice_does_not_duplicate(self):
        work_order, _first = convert_to_work_order(self.inspection)
        _wo, second = convert_to_work_order(self.inspection, work_order=work_order)
        self.assertEqual(second, [])
        self.assertEqual(work_order.job_items.count(), 2)

    def test_results_can_be_added_to_an_existing_work_order(self):
        existing = WorkOrder.objects.create(asset=self.asset, title="Saturday")
        work_order, items = convert_to_work_order(self.inspection, work_order=existing)
        self.assertEqual(work_order, existing)
        self.assertEqual(len(items), 2)


class WearProjectionTests(TestCase):
    """FR-DVI-11 — two readings and an odometer turn a number into a due date."""

    def setUp(self):
        self.asset = Asset.objects.create(nickname="Truck", meter_unit="mi")
        self.template = template_with_tread()

    def _inspect(self, *, on: date, usage, value) -> Inspection:
        record_reading(self.asset, usage, read_on=on)
        inspection = start(self.asset, self.template)
        inspection.performed_on = on
        inspection.odometer = usage
        inspection.save()
        record(inspection.results.get(position="LF/outer"), value=value)
        for other in inspection.results.exclude(position="LF/outer"):
            record(other, value=8)
        complete(inspection)
        return inspection

    def test_one_reading_gives_no_trend(self):
        self._inspect(on=date(2025, 1, 1), usage=100_000, value=8)
        wear = wear_projection(self.asset, "Tire tread depth", "LF/outer", unit="/32in")
        self.assertFalse(wear.is_projectable)
        self.assertIn("measure it again", wear.summary)

    def test_two_readings_produce_a_wear_rate_and_a_projection(self):
        self._inspect(on=date(2025, 1, 1), usage=100_000, value=8)
        self._inspect(on=date(2026, 1, 1), usage=120_000, value=4)

        wear = wear_projection(self.asset, "Tire tread depth", "LF/outer", unit="/32in")
        self.assertTrue(wear.is_projectable)
        # 4/32 consumed over 20,000 mi = 0.0002 per mile.
        self.assertAlmostEqual(float(wear.per_distance), 0.0002, places=6)
        # From 4/32, reaching the 2/32 fail threshold takes another 10,000 mi.
        self.assertEqual(wear.projected_usage, Decimal(130_000))
        self.assertEqual(wear.target, Decimal(2))
        self.assertIn("reaches 2", wear.summary)

    def test_no_measurable_wear_reports_honestly(self):
        self._inspect(on=date(2025, 1, 1), usage=100_000, value=8)
        self._inspect(on=date(2026, 1, 1), usage=120_000, value=8)
        wear = wear_projection(self.asset, "Tire tread depth", "LF/outer")
        self.assertFalse(wear.is_projectable)
        self.assertIn("No measurable wear", wear.summary)

    def test_already_past_the_threshold_projects_to_now(self):
        self._inspect(on=date(2025, 1, 1), usage=100_000, value=6)
        self._inspect(on=date(2026, 1, 1), usage=120_000, value=2)
        wear = wear_projection(self.asset, "Tire tread depth", "LF/outer")
        self.assertEqual(wear.projected_usage, Decimal(120_000))


class ComparisonTests(TestCase):
    """FR-DVI-12 — what changed since last time."""

    def setUp(self):
        self.asset = Asset.objects.create(nickname="Truck", meter_unit="mi")
        self.template = template_with_tread()

    def _inspect(self, on: date, value) -> Inspection:
        inspection = start(self.asset, self.template)
        inspection.performed_on = on
        inspection.save()
        for result in inspection.results.all():
            record(result, value=8)
        record(inspection.results.get(position="LF/outer"), value=value)
        return complete(inspection)

    def test_a_worsened_point_is_reported(self):
        self._inspect(date(2025, 1, 1), 8)
        latest = self._inspect(date(2026, 1, 1), 3)
        changes = compare(latest)
        self.assertEqual(len(changes), 1)
        self.assertTrue(changes[0]["worsened"])
        self.assertEqual(changes[0]["was"], ResultStatus.PASS)
        self.assertEqual(changes[0]["now"], ResultStatus.ATTENTION)

    def test_a_first_inspection_has_nothing_to_compare(self):
        self.assertEqual(compare(self._inspect(date(2026, 1, 1), 8)), [])


class ProspectTests(TestCase):
    """FR-DVI-10 — inspect a car you do not own yet."""

    def test_a_prospect_can_be_inspected_and_stays_out_of_the_fleet(self):
        prospect = Asset.objects.create(nickname="Looking at a truck", status="prospect")
        install_templates()
        ppi = InspectionTemplate.objects.get(slug="ppi")
        inspection = start(prospect, ppi, user=None)
        self.assertGreater(inspection.results.count(), 20)
        self.assertNotIn(prospect, Asset.objects.fleet())


class SeedTests(TestCase):
    def test_built_in_templates_install_and_are_idempotent(self):
        self.assertEqual(install_templates(), 4)
        first = InspectionPoint.objects.count()
        install_templates()
        self.assertEqual(InspectionPoint.objects.count(), first)

    def test_the_ppi_covers_the_cases_that_matter(self):
        install_templates()
        names = set(
            InspectionPoint.objects.filter(template__slug="ppi").values_list("name", flat=True)
        )
        self.assertIn("Frame and rocker corrosion", names)
        self.assertIn("Tire DOT date code", names)
        self.assertIn("Cold start behavior", names)


class PresentationTests(TestCase):
    """Small things that decide whether a record reads well a year later."""

    def test_measurements_lose_their_trailing_zeros(self):
        from .services import trim

        self.assertEqual(trim(Decimal("2.000")), "2")
        self.assertEqual(trim(Decimal("2.500")), "2.5")
        self.assertEqual(trim(Decimal("100")), "100")   # not 1E+2
        self.assertEqual(trim(Decimal("0.125")), "0.125")

    def test_areas_render_as_labels_not_database_keys(self):
        asset = Asset.objects.create(nickname="Truck")
        inspection = start(asset, template_with_tread())
        result = inspection.results.first()
        self.assertEqual(result.area, "tires_wheels")
        self.assertEqual(result.area_display, "Tires and wheels")
