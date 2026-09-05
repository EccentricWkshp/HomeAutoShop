"""Interval math, projection, and completion linkage (SPEC §7.7, FR-CMP-*)."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings
from django.urls import reverse
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


class RemovingAScheduledItemTests(TestCase):
    """Reported as: switching templates leaves the old one's items on screen.

    It did, and there was nothing to do about it. `Ignore` was the only way to
    say no to a scheduled item, and an ignored item stays in the list forever
    — so a vehicle that had been through two templates showed both, and the
    list only ever grew.

    The rule these tests pin down is where removal stops: an item nobody has
    ever completed is a plan, and a plan belongs to whoever owns the vehicle;
    an item with completions is a record, and removing it would take the
    record with it.
    """

    def setUp(self):
        from homeautoshop.accounts.models import Role, User

        self.user = User.objects.create_user(
            username="andy", password="x" * 16, role=Role.ADMIN
        )
        self.client.force_login(self.user)
        self.asset = Asset.objects.create(nickname="Truck", meter_unit="mi")
        self.item = AssetServiceItem.objects.create(
            asset=self.asset, definition=oil_change(), interval_distance=5000
        )

    def url(self, item=None):
        from django.urls import reverse

        return reverse("service_item_remove", args=[self.asset.pk, (item or self.item).pk])

    def test_an_item_never_done_is_removed(self):
        self.client.post(self.url())
        self.assertFalse(AssetServiceItem.objects.filter(pk=self.item.pk).exists())

    def test_removal_is_a_soft_delete(self):
        """Nothing in this application destroys a row, and this is no exception."""
        self.client.post(self.url())
        self.assertTrue(AssetServiceItem.all_objects.filter(pk=self.item.pk).exists())

    def test_a_removed_item_is_gone_from_every_listing(self):
        """The half that would have gone wrong silently.

        `AssetServiceItem.objects` was a plain manager, not an alive one, so a
        soft-deleted item stayed visible on the schedule, on the due list and
        in the forecast. Harmless while nothing could delete one; the whole
        feature the moment something could.
        """
        self.item.status = ServiceStatus.OVERDUE
        self.item.save()
        self.client.post(self.url())
        self.assertEqual(self.asset.service_items.count(), 0)
        self.assertEqual(due_dashboard(), [])

    def test_an_item_with_history_is_refused_and_told_why(self):
        complete(self.item, on=TODAY, usage=100_000)
        response = self.client.post(self.url(), follow=True)
        self.assertTrue(AssetServiceItem.objects.filter(pk=self.item.pk).exists())
        said = " ".join(str(m) for m in response.context["messages"])
        self.assertIn("Ignore it instead", said)

    def test_ignoring_still_works_on_an_item_with_history(self):
        """The alternative the refusal names has to actually be there."""
        from django.urls import reverse

        complete(self.item, on=TODAY, usage=100_000)
        self.client.post(
            reverse("service_item_snooze", args=[self.asset.pk, self.item.pk]),
            {"action": "disable"},
        )
        self.item.refresh_from_db()
        self.assertEqual(self.item.status, ServiceStatus.DISABLED)

    def test_the_screen_separates_tracked_from_ignored(self):
        from django.urls import reverse

        other = AssetServiceItem.objects.create(
            asset=self.asset,
            definition=ServiceDefinition.objects.create(name="Rotate tires"),
            interval_distance=6000,
            status=ServiceStatus.DISABLED,
        )
        page = self.client.get(reverse("asset_schedule", args=[self.asset.pk]))
        self.assertEqual([i for i, _ in page.context["rows"]], [self.item])
        self.assertEqual(list(page.context["ignored"]), [other])


class SwitchingTemplatesTests(TestCase):
    """FR-MAINT-12 — applying a template can replace rather than only add."""

    def setUp(self):
        from homeautoshop.accounts.models import Role, User

        self.user = User.objects.create_user(
            username="andy", password="x" * 16, role=Role.ADMIN
        )
        self.client.force_login(self.user)
        self.asset = Asset.objects.create(nickname="Truck", meter_unit="mi")
        self.oil = oil_change()
        self.tires = ServiceDefinition.objects.create(name="Rotate tires")
        self.coolant = ServiceDefinition.objects.create(name="Coolant")

        self.old = ScheduleTemplate.objects.create(name="Old", slug="old")
        TemplateItem.objects.create(template=self.old, definition=self.oil, interval_distance=5000)
        TemplateItem.objects.create(template=self.old, definition=self.coolant, interval_months=48)

        self.new = ScheduleTemplate.objects.create(name="New", slug="new")
        TemplateItem.objects.create(template=self.new, definition=self.oil, interval_distance=3000)
        TemplateItem.objects.create(template=self.new, definition=self.tires, interval_distance=6000)

    def apply(self, template, **extra):
        from django.urls import reverse

        return self.client.post(
            reverse("apply_schedule_template", args=[self.asset.pk]),
            {"template": str(template.pk), **extra},
            follow=True,
        )

    def names(self):
        return sorted(self.asset.service_items.values_list("definition__name", flat=True))

    def test_applying_without_replace_still_only_adds(self):
        self.apply(self.old)
        self.apply(self.new)
        self.assertEqual(self.names(), ["Coolant", "Engine oil and filter", "Rotate tires"])

    def test_replacing_drops_what_the_new_template_does_not_want(self):
        self.apply(self.old)
        self.apply(self.new, replace="1")
        self.assertEqual(self.names(), ["Engine oil and filter", "Rotate tires"])

    def test_replacing_keeps_anything_with_history_and_says_so(self):
        self.apply(self.old)
        complete(
            self.asset.service_items.get(definition=self.coolant), on=TODAY, usage=100_000
        )
        response = self.apply(self.new, replace="1")
        self.assertIn("Coolant", self.names())
        said = " ".join(str(m) for m in response.context["messages"])
        self.assertIn("they have been done before", said)

    def test_reapplying_a_template_brings_a_removed_item_back(self):
        """Otherwise removal is a trap: the template reports items added and
        the item does not appear, because the soft-deleted row was found and
        left deleted."""
        self.apply(self.old)
        item = self.asset.service_items.get(definition=self.coolant)
        item.delete()
        self.assertNotIn("Coolant", self.names())

        self.apply(self.old)
        self.assertIn("Coolant", self.names())

    def test_a_revived_item_keeps_its_history(self):
        """The same row comes back, not a second one beside it."""
        self.apply(self.old)
        item = self.asset.service_items.get(definition=self.coolant)
        complete(item, on=TODAY, usage=100_000)
        item.delete()

        self.apply(self.old)
        revived = self.asset.service_items.get(definition=self.coolant)
        self.assertEqual(revived.pk, item.pk)
        self.assertEqual(revived.last_done_on, TODAY)
        self.assertEqual(
            AssetServiceItem.all_objects.filter(
                asset=self.asset, definition=self.coolant
            ).count(),
            1,
        )

    def test_a_revived_item_comes_back_tracked(self):
        self.apply(self.old)
        item = self.asset.service_items.get(definition=self.coolant)
        item.status = ServiceStatus.DISABLED
        item.save()
        item.delete()

        self.apply(self.old)
        item.refresh_from_db()
        self.assertEqual(item.status, ServiceStatus.OK)


class TheScheduleChecksWhoseVehicleItIsTests(TestCase):
    """Every write on this screen names its vehicle, not just its URL.

    The helper gate is an allow-list of URL names, and `service_item_update`
    was on it — as it should be, since a helper does maintain the vehicle they
    were given. What none of these views did was call `require()` with the
    vehicle in hand, so the allow-list let a helper POST to *any* vehicle's
    schedule, including ones they had never been granted.
    """

    def setUp(self):
        from homeautoshop.accounts.models import AssetAccess, Role, User

        self.helper = User.objects.create_user(
            username="sam", password="x" * 16, role=Role.HELPER
        )
        self.client.force_login(self.helper)
        self.mine = Asset.objects.create(nickname="Mine", meter_unit="mi")
        self.theirs = Asset.objects.create(nickname="Theirs", meter_unit="mi")
        AssetAccess.objects.create(user=self.helper, asset=self.mine, level="write")
        self.item = AssetServiceItem.objects.create(
            asset=self.theirs, definition=oil_change(), interval_distance=5000
        )

    def post(self, name, *args, **data):
        from django.urls import reverse

        return self.client.post(reverse(name, args=args), data)

    def test_a_helper_cannot_remove_an_item_from_a_vehicle_they_were_not_given(self):
        self.assertEqual(
            self.post("service_item_remove", self.theirs.pk, self.item.pk).status_code, 403
        )
        self.assertTrue(AssetServiceItem.objects.filter(pk=self.item.pk).exists())

    def test_nor_edit_its_intervals(self):
        self.assertEqual(
            self.post(
                "service_item_update", self.theirs.pk, self.item.pk, interval_distance="1"
            ).status_code,
            403,
        )
        self.item.refresh_from_db()
        self.assertEqual(self.item.interval_distance, 5000)

    def test_nor_back_fill_a_service_onto_it(self):
        self.assertEqual(
            self.post("service_item_complete", self.theirs.pk, self.item.pk).status_code, 403
        )
        self.assertEqual(ServiceCompletion.objects.count(), 0)

    def test_a_read_only_grant_does_not_carry_a_write(self):
        from homeautoshop.accounts.models import AssetAccess

        AssetAccess.objects.filter(user=self.helper, asset=self.mine).update(level="read")
        mine = AssetServiceItem.objects.create(
            asset=self.mine,
            definition=ServiceDefinition.objects.create(name="Rotate tires"),
            interval_distance=6000,
        )
        self.assertEqual(self.post("service_item_remove", self.mine.pk, mine.pk).status_code, 403)

    def test_but_a_write_grant_on_their_own_vehicle_works(self):
        mine = AssetServiceItem.objects.create(
            asset=self.mine,
            definition=ServiceDefinition.objects.create(name="Rotate tires"),
            interval_distance=6000,
        )
        self.post("service_item_remove", self.mine.pk, mine.pk)
        self.assertFalse(AssetServiceItem.objects.filter(pk=mine.pk).exists())


class TheComponentFormSaysWhatItsBoxesAreForTests(TestCase):
    """Three bare widgets: a select reading "Other" and two empty boxes.

    `check_accessibility` cannot see this class of mistake and says so — it
    matches a literal `<input>` tag, and `{{ form.field }}` is not one. Its own
    docstring records the last time this happened, to three interval boxes on
    this same screen.

    The DOT box is the one that matters most. It is what dates a tire whose
    tread still looks fine (FR-CMP-6), and an unlabeled box is a feature
    nobody can use.
    """

    def setUp(self):
        from homeautoshop.accounts.models import User

        self.user = User.objects.create_user("andy", password="correct-horse-battery")
        self.client.force_login(self.user)
        self.asset = Asset.objects.create(nickname="Truck", meter_unit="mi")

    def test_each_control_has_a_label_bound_to_it(self):
        page = self.client.get(reverse("asset_schedule", args=[self.asset.pk])).content.decode()

        for field in ("component_type", "position", "serial_or_dot_code"):
            with self.subTest(field=field):
                self.assertIn('<label for="id_%s"' % field, page)

    def test_the_dot_box_says_what_a_dot_code_is(self):
        page = self.client.get(reverse("asset_schedule", args=[self.asset.pk])).content.decode()
        self.assertIn("week and year", page)
