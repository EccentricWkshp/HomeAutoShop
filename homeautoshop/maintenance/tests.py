"""Interval math, projection, and completion linkage (SPEC §7.7, FR-CMP-*)."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings
from django.utils import timezone

from homeautoshop.assets.models import Asset
from homeautoshop.assets.services import record_reading
from homeautoshop.work.models import JobItem, WorkOrder

from .models import (
    AssetComponent,
    AssetServiceItem,
    ScheduleTemplate,
    ServiceCompletion,
    ServiceDefinition,
    ServiceStatus,
    Severity,
    TemplateItem,
)
from .services import apply_template, complete, due_dashboard, project, recalculate, usage_rate

TODAY = date(2026, 6, 1)


def oil_change(**kwargs) -> ServiceDefinition:
    defaults = {
        "name": "Engine oil and filter",
        "default_interval_distance": 5000,
        "default_interval_unit": "mi",
        "default_interval_months": 6,
    }
    return ServiceDefinition.objects.create(**{**defaults, **kwargs})


class IntervalMathTests(TestCase):
    def setUp(self):
        self.asset = Asset.objects.create(nickname="Truck", meter_unit="mi")
        self.definition = oil_change()

    def _item(self, **kwargs) -> AssetServiceItem:
        defaults = {"interval_distance": 5000, "interval_unit": "mi", "interval_months": 6}
        return AssetServiceItem.objects.create(
            asset=self.asset, definition=self.definition, **{**defaults, **kwargs}
        )

    def test_next_due_is_last_done_plus_the_interval(self):
        item = self._item(last_done_on=date(2026, 1, 1), last_done_usage=100_000)
        recalculate(item, today=TODAY)
        self.assertEqual(item.next_due_usage, Decimal(105_000))
        self.assertEqual(item.next_due_on, date(2026, 7, 1))

    def test_a_never_done_item_is_due_an_interval_from_here(self):
        """The least alarming honest assumption for a vehicle new to the shop."""
        record_reading(self.asset, 80_000)
        item = self._item()
        recalculate(item, today=TODAY)
        self.assertEqual(item.next_due_usage, Decimal(85_000))

    def test_whichever_arrives_first_wins(self):
        """FR-MAINT-3 — a car that sits still still needs its oil changed."""
        record_reading(self.asset, 100_100)  # only 100 mi since service
        item = self._item(last_done_on=date(2025, 1, 1), last_done_usage=100_000)
        recalculate(item, today=TODAY)
        # Distance is nowhere near due, but the 6-month clock ran out in 2025.
        self.assertEqual(item.status, ServiceStatus.OVERDUE)

    def test_distance_can_be_the_first_to_arrive(self):
        record_reading(self.asset, 106_000)
        item = self._item(last_done_on=TODAY - timedelta(days=10), last_done_usage=100_000)
        recalculate(item, today=TODAY)
        self.assertEqual(item.status, ServiceStatus.OVERDUE)

    @override_settings(DUE_SOON_DISTANCE=500, DUE_SOON_DAYS=30)
    def test_due_soon_uses_our_own_lead_window(self):
        record_reading(self.asset, 104_700)  # 300 mi to go
        item = self._item(last_done_on=TODAY, last_done_usage=100_000, interval_months=None)
        recalculate(item, today=TODAY)
        self.assertEqual(item.status, ServiceStatus.DUE_SOON)

    def test_month_arithmetic_clamps_short_months(self):
        item = self._item(last_done_on=date(2026, 1, 31), interval_months=1, interval_distance=None)
        recalculate(item, today=TODAY)
        self.assertEqual(item.next_due_on, date(2026, 2, 28))

    def test_intervals_convert_into_the_asset_meter_unit(self):
        metric = Asset.objects.create(nickname="Euro car", meter_unit="km")
        item = AssetServiceItem.objects.create(
            asset=metric, definition=self.definition,
            interval_distance=5000, interval_unit="mi", last_done_usage=0,
        )
        recalculate(item, today=TODAY)
        self.assertAlmostEqual(float(item.next_due_usage), 8046.72, places=1)

    def test_an_item_with_no_interval_is_refused(self):
        item = AssetServiceItem(asset=self.asset, definition=self.definition)
        with self.assertRaises(ValidationError):
            item.full_clean()

    def test_snoozed_items_report_as_snoozed(self):
        item = self._item(
            last_done_on=date(2020, 1, 1), last_done_usage=0,
            snooze_until=TODAY + timedelta(days=30), snooze_reason="Selling it",
        )
        recalculate(item, today=TODAY)
        self.assertEqual(item.status, ServiceStatus.SNOOZED)

    def test_disabled_items_are_left_alone(self):
        item = self._item(status=ServiceStatus.DISABLED, last_done_usage=0)
        recalculate(item, today=TODAY)
        self.assertEqual(item.status, ServiceStatus.DISABLED)


class UsageRateTests(TestCase):
    def setUp(self):
        self.asset = Asset.objects.create(nickname="Truck", meter_unit="mi")

    def test_rate_is_observed_from_the_asset_history(self):
        record_reading(self.asset, 100_000, read_on=TODAY - timedelta(days=100))
        record_reading(self.asset, 103_000, read_on=TODAY)
        rate = usage_rate(self.asset, today=TODAY)
        self.assertTrue(rate.observed)
        self.assertAlmostEqual(float(rate.per_day), 30.0, places=1)
        self.assertIn("100 days", rate.basis)

    def test_too_little_history_falls_back_and_says_so(self):
        rate = usage_rate(self.asset, today=TODAY)
        self.assertFalse(rate.observed)
        self.assertIn("Estimated", rate.basis)

    def test_a_meterless_asset_has_no_rate(self):
        trailer = Asset.objects.create(nickname="Trailer", meter="none")
        self.assertEqual(usage_rate(trailer, today=TODAY).per_day, Decimal(0))


class ProjectionTests(TestCase):
    """FR-MAINT-4 — 'due in about 3 weeks' beats 'due in 900 mi' on a Saturday."""

    def setUp(self):
        self.asset = Asset.objects.create(nickname="Truck", meter_unit="mi")
        record_reading(self.asset, 100_000, read_on=TODAY - timedelta(days=100))
        record_reading(self.asset, 103_000, read_on=TODAY)  # 30 mi/day
        self.item = AssetServiceItem.objects.create(
            asset=self.asset, definition=oil_change(),
            interval_distance=5000, interval_unit="mi",
            last_done_on=TODAY - timedelta(days=100), last_done_usage=100_000,
        )
        recalculate(self.item, today=TODAY)

    def test_projects_a_date_from_the_observed_rate(self):
        projection = project(self.item, today=TODAY)
        # 2,000 mi remaining at 30 mi/day is about 66 days.
        self.assertEqual(projection.distance_remaining, Decimal(2000))
        self.assertEqual(projection.projected_date, TODAY + timedelta(days=66))

    def test_the_summary_states_its_basis(self):
        summary = project(self.item, today=TODAY).summary
        self.assertIn("2,000 mi", summary)
        self.assertIn("around", summary)

    def test_a_time_interval_can_beat_the_distance_projection(self):
        self.item.interval_months = 1
        recalculate(self.item, today=TODAY)
        projection = project(self.item, today=TODAY)
        self.assertEqual(projection.projected_date, self.item.next_due_on)


class CompletionTests(TestCase):
    """FR-MAINT-5 — doing the work IS resetting the schedule."""

    def setUp(self):
        self.asset = Asset.objects.create(nickname="Truck", meter_unit="mi")
        record_reading(self.asset, 105_000)
        self.item = AssetServiceItem.objects.create(
            asset=self.asset, definition=oil_change(),
            interval_distance=5000, interval_unit="mi", interval_months=6,
            last_done_on=date(2025, 1, 1), last_done_usage=100_000,
        )
        recalculate(self.item, today=TODAY)

    def test_completion_rolls_the_interval_forward(self):
        self.assertEqual(self.item.status, ServiceStatus.OVERDUE)
        complete(self.item, on=TODAY, usage=105_000)
        self.item.refresh_from_db()
        self.assertEqual(self.item.last_done_usage, Decimal(105_000))
        self.assertEqual(self.item.next_due_usage, Decimal(110_000))
        self.assertEqual(self.item.status, ServiceStatus.OK)

    def test_completion_clears_a_snooze(self):
        self.item.snooze_until = TODAY + timedelta(days=10)
        self.item.save()
        complete(self.item, on=TODAY, usage=105_000)
        self.item.refresh_from_db()
        self.assertIsNone(self.item.snooze_until)

    def test_backfilling_older_history_does_not_rewind_the_schedule(self):
        """FR-MAINT-6 — recording a past service must not undo a later one."""
        complete(self.item, on=TODAY, usage=105_000)
        complete(self.item, on=date(2024, 6, 1), usage=90_000, backfill=True)
        self.item.refresh_from_db()
        self.assertEqual(self.item.last_done_usage, Decimal(105_000))
        self.assertEqual(ServiceCompletion.objects.count(), 2)

    def test_completion_records_its_link_to_the_job(self):
        wo = WorkOrder.objects.create(asset=self.asset, title="Service")
        job = JobItem.objects.create(work_order=wo, title="Oil change")
        completion = complete(self.item, job_item=job, work_order=wo, usage=105_000)
        self.assertEqual(completion.job_item, job)
        self.assertEqual(completion.work_order, wo)


class TemplateTests(TestCase):
    def setUp(self):
        self.asset = Asset.objects.create(nickname="Truck", meter_unit="mi")
        self.template = ScheduleTemplate.objects.create(
            name="Gasoline — severe", slug="gas-severe", asset_kinds=["vehicle"]
        )
        TemplateItem.objects.create(
            template=self.template, definition=oil_change(), interval_distance=3000, interval_months=6
        )
        TemplateItem.objects.create(
            template=self.template,
            definition=ServiceDefinition.objects.create(name="Rotate tires", severity=Severity.SAFETY),
            interval_distance=6000,
        )

    def test_applying_a_template_creates_editable_items(self):
        items = apply_template(self.asset, self.template)
        self.assertEqual(len(items), 2)
        self.assertEqual(self.asset.service_items.count(), 2)
        # The template's interval wins over the definition's default.
        oil = self.asset.service_items.get(definition__name="Engine oil and filter")
        self.assertEqual(oil.interval_distance, 3000)

    def test_reapplying_does_not_clobber_an_edited_interval(self):
        apply_template(self.asset, self.template)
        oil = self.asset.service_items.get(definition__name="Engine oil and filter")
        oil.interval_distance = 4000
        oil.save()

        apply_template(self.asset, self.template)
        oil.refresh_from_db()
        self.assertEqual(oil.interval_distance, 4000)

    def test_templates_are_scoped_by_asset_kind(self):
        mower = Asset.objects.create(nickname="Mower", asset_kind="equipment")
        self.assertTrue(self.template.applies_to(self.asset))
        self.assertFalse(self.template.applies_to(mower))


class DashboardTests(TestCase):
    """FR-MAINT-7 — overdue safety items lead."""

    def test_ordering_puts_overdue_safety_first(self):
        asset = Asset.objects.create(nickname="Truck", meter_unit="mi")
        record_reading(asset, 100_000)

        routine = AssetServiceItem.objects.create(
            asset=asset, definition=oil_change(name="Oil"),
            interval_months=1, last_done_on=date(2025, 1, 1),
        )
        safety = AssetServiceItem.objects.create(
            asset=asset,
            definition=ServiceDefinition.objects.create(name="Brakes", severity=Severity.SAFETY),
            interval_months=1, last_done_on=date(2025, 1, 1),
        )
        for item in (routine, safety):
            recalculate(item, today=TODAY)

        rows = due_dashboard()
        self.assertEqual(rows[0], safety)

    def test_sold_vehicles_are_not_nagged_about(self):
        asset = Asset.objects.create(nickname="Gone", status="sold")
        item = AssetServiceItem.objects.create(
            asset=asset, definition=oil_change(), interval_months=1, last_done_on=date(2020, 1, 1)
        )
        recalculate(item, today=TODAY)
        self.assertNotIn(item, due_dashboard())


class ComponentTests(TestCase):
    """FR-CMP-* — what turns a repeated measurement into a wear rate."""

    def setUp(self):
        self.asset = Asset.objects.create(nickname="Truck", meter_unit="mi")

    def test_distance_covered_since_installation(self):
        component = AssetComponent.objects.create(
            asset=self.asset, component_type="tire", position="LF",
            installed_on=date(2024, 1, 1), installed_usage=100_000,
        )
        record_reading(self.asset, 131_000)
        self.assertEqual(component.distance_covered(self.asset.current_usage), Decimal(31_000))

    def test_a_removed_component_measures_to_its_removal(self):
        component = AssetComponent.objects.create(
            asset=self.asset, component_type="battery",
            installed_usage=100_000, removed_usage=118_000, removed_on=date(2026, 1, 1),
        )
        self.assertEqual(component.distance_covered(999_999), Decimal(18_000))
        self.assertFalse(component.is_installed)

    def test_dot_code_condemns_an_old_tire_regardless_of_tread(self):
        """FR-CMP-6 — full tread and a ten-year-old date code is a failed tire."""
        old = AssetComponent.objects.create(
            asset=self.asset, component_type="tire", serial_or_dot_code="0114"
        )
        self.assertGreater(old.dot_age_years, 10)
        self.assertEqual(old.dot_verdict, "fail")

    def test_a_six_year_old_tire_warrants_attention(self):
        year = timezone.localdate().year - 2007  # ~7 years old in 2026
        component = AssetComponent.objects.create(
            asset=self.asset, component_type="tire", serial_or_dot_code=f"01{year:02d}"
        )
        self.assertEqual(component.dot_verdict, "attention")

    def test_a_missing_or_malformed_code_is_simply_unknown(self):
        for code in ("", "abcd", "9999"):
            with self.subTest(code=code):
                component = AssetComponent(
                    asset=self.asset, component_type="tire", serial_or_dot_code=code
                )
                self.assertIsNone(component.dot_verdict)


class WorkOrderLinkageTests(TestCase):
    """FR-WO-7 / FR-MAINT-5 — completing the job item IS resetting the schedule."""

    def setUp(self):
        self.asset = Asset.objects.create(nickname="Truck", meter_unit="mi")
        record_reading(self.asset, 105_000)
        self.item = AssetServiceItem.objects.create(
            asset=self.asset, definition=oil_change(),
            interval_distance=5000, interval_unit="mi",
            last_done_on=date(2025, 1, 1), last_done_usage=100_000,
        )
        recalculate(self.item, today=TODAY)
        self.wo = WorkOrder.objects.create(asset=self.asset, title="Saturday service")

    def test_completing_a_linked_job_item_rolls_the_interval(self):
        job = JobItem.objects.create(
            work_order=self.wo, title="Oil change", service_item=self.item
        )
        job.status = JobItem.Status.DONE
        job.save()

        self.item.refresh_from_db()
        self.assertEqual(self.item.last_done_usage, Decimal(105_000))
        self.assertEqual(self.item.next_due_usage, Decimal(110_000))
        self.assertEqual(ServiceCompletion.objects.count(), 1)

    def test_an_unlinked_job_item_touches_no_schedule(self):
        job = JobItem.objects.create(work_order=self.wo, title="Chase a rattle")
        job.status = JobItem.Status.DONE
        job.save()
        self.assertEqual(ServiceCompletion.objects.count(), 0)

    def test_toggling_done_twice_does_not_double_complete(self):
        job = JobItem.objects.create(
            work_order=self.wo, title="Oil change", service_item=self.item
        )
        job.status = JobItem.Status.DONE
        job.save()
        job.status = JobItem.Status.TODO
        job.save()
        job.status = JobItem.Status.DONE
        job.save()
        self.assertEqual(ServiceCompletion.objects.count(), 1)
