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


class SayingWhenItWasLastDoneTests(TestCase):
    """FR-MAINT-5 — an interval runs from the last service, not from today.

    The schedule already stored `last_done_on` and `last_done_usage`, and
    `recalculate` already preferred them over the meter's current reading. The
    gap was that nothing could ever put them there: the add form offered the
    interval and nothing else, so every item created by hand started its clock
    the day it was typed. A truck bought at 96,000 miles with an oil change
    5,000 miles overdue was filed as due in 5,000 more.
    """

    def setUp(self):
        from homeautoshop.accounts.models import User

        self.user = User.objects.create_user("andy", password="correct-horse-battery")
        self.client.force_login(self.user)
        self.asset = Asset.objects.create(nickname="Truck", meter_unit="mi")
        record_reading(self.asset, 100_000)
        self.definition = oil_change()

    def add(self, **extra):
        return self.client.post(
            reverse("service_item_add", args=[self.asset.pk]),
            {
                "definition": str(self.definition.pk),
                "interval_distance": "5000",
                "interval_unit": "mi",
                "interval_months": "6",
                **extra,
            },
        )

    def test_the_form_asks_for_it(self):
        page = self.client.get(reverse("asset_schedule", args=[self.asset.pk])).content.decode()
        self.assertIn('name="last_done_on"', page)
        self.assertIn('name="last_done_usage"', page)

    def test_the_next_service_is_measured_from_it(self):
        self.add(last_done_on="2026-01-01", last_done_usage="96000")

        item = AssetServiceItem.objects.get(asset=self.asset)
        self.assertEqual(item.last_done_usage, Decimal(96_000))
        self.assertEqual(item.next_due_usage, Decimal(101_000))

    def test_without_it_the_clock_still_starts_today(self):
        """The old behavior, kept: it is the least alarming honest assumption
        for a vehicle whose history nobody has."""
        self.add()

        item = AssetServiceItem.objects.get(asset=self.asset)
        self.assertIsNone(item.last_done_usage)
        self.assertEqual(item.next_due_usage, Decimal(105_000))

    def test_a_date_that_has_not_happened_is_refused(self):
        """The one field here where a mistyped year is silent — it pushes the
        next service out by however far the year was wrong and reports the
        item as fine the whole time."""
        ahead = timezone.localdate() + timedelta(days=30)
        self.add(last_done_on=ahead.isoformat())

        self.assertFalse(AssetServiceItem.objects.filter(asset=self.asset).exists())


class BackfillingAServiceDoesNotInventAMeterReadingTests(TestCase):
    """FR-MAINT-6 — recording a past service must not claim today's odometer.

    The row's Done button sent a date and nothing else, and `complete` filled
    the reading in from the vehicle's meter *now*. An oil change back-filled at
    eight months old was therefore recorded at this morning's mileage, and the
    next one came due five thousand miles from the wrong place — the same
    "assume it happened now" the add form had.
    """

    def setUp(self):
        from homeautoshop.accounts.models import User

        self.user = User.objects.create_user("andy", password="correct-horse-battery")
        self.client.force_login(self.user)
        self.asset = Asset.objects.create(nickname="Truck", meter_unit="mi")
        record_reading(self.asset, 100_000)
        self.item = AssetServiceItem.objects.create(
            asset=self.asset, definition=oil_change(),
            interval_distance=5000, interval_unit="mi",
        )

    def done(self, **data):
        return self.client.post(
            reverse("service_item_complete", args=[self.asset.pk, self.item.pk]), data
        )

    def test_the_row_offers_the_meter_beside_the_date(self):
        page = self.client.get(reverse("asset_schedule", args=[self.asset.pk])).content.decode()
        self.assertIn('name="usage"', page)

    def test_a_past_service_recorded_with_its_meter_anchors_the_next_one(self):
        self.done(completed_on="2026-01-01", usage="94000")

        self.item.refresh_from_db()
        self.assertEqual(self.item.last_done_usage, Decimal(94_000))
        self.assertEqual(self.item.next_due_usage, Decimal(99_000))

    def test_a_past_service_with_no_meter_leaves_it_unknown(self):
        self.done(completed_on="2026-01-01")

        self.item.refresh_from_db()
        self.assertIsNone(self.item.last_done_usage)
        self.assertEqual(ServiceCompletion.objects.get().usage, None)

    def test_work_done_today_still_takes_the_meter_as_it_reads(self):
        """The reading describes this moment, so for this moment it is right —
        and asking somebody to retype what the vehicle page already knows
        would be the kind of friction that stops a record being kept."""
        self.done()

        self.item.refresh_from_db()
        self.assertEqual(self.item.last_done_usage, Decimal(100_000))

    def test_a_meter_reading_that_is_not_a_number_is_said_rather_than_crashed(self):
        response = self.done(completed_on="2026-01-01", usage="about 94k")

        self.assertEqual(response.status_code, 302)
        self.item.refresh_from_db()
        self.assertIsNone(self.item.last_done_usage)


class NamingAServiceTheListDoesNotHaveTests(TestCase):
    """FR-MAINT-2 — the shop decides what it tracks, not the shipped list.

    "Add an item" offered a picker over `ServiceDefinition` and nothing else,
    so a job nobody had seeded — greasing a fifth wheel, the boat lift's
    cables — could only be added through the Django admin or by authoring a
    whole schedule template for one line. The picker quietly defined what this
    shop was allowed to track.
    """

    def setUp(self):
        from homeautoshop.accounts.models import User

        self.user = User.objects.create_user("andy", password="correct-horse-battery")
        self.client.force_login(self.user)
        self.asset = Asset.objects.create(nickname="Truck", meter_unit="mi")
        self.definition = oil_change()

    def add(self, **data):
        return self.client.post(
            reverse("service_item_add", args=[self.asset.pk]),
            {"interval_months": "6", **data},
        )

    def test_a_new_name_becomes_a_scheduled_item(self):
        self.add(new_definition="Grease the fifth wheel")

        item = AssetServiceItem.objects.get(asset=self.asset)
        self.assertEqual(item.definition.name, "Grease the fifth wheel")
        self.assertEqual(item.interval_months, 6)

    def test_it_joins_the_shared_list_rather_than_this_vehicle_alone(self):
        """The next vehicle that needs it picks it from the list above."""
        self.add(new_definition="Grease the fifth wheel")

        page = self.client.get(
            reverse("asset_schedule", args=[Asset.objects.create(nickname="Van").pk])
        ).content.decode()
        self.assertIn("Grease the fifth wheel", page)

    def test_the_same_name_twice_is_one_definition(self):
        """Two rows differing only in capitalization would be two entries in
        every picker and one job's history split in half."""
        self.add(new_definition="Cabin filter")
        self.add(new_definition="cabin filter")

        self.assertEqual(ServiceDefinition.objects.filter(name__iexact="cabin filter").count(), 1)

    def test_naming_one_and_picking_one_is_refused(self):
        self.add(definition=str(self.definition.pk), new_definition="Cabin filter")

        self.assertFalse(AssetServiceItem.objects.exists())
        self.assertFalse(ServiceDefinition.objects.filter(name="Cabin filter").exists())

    def test_a_new_name_with_no_interval_creates_nothing_at_all(self):
        """Not even the definition. The item is rejected for having no
        interval, and a rejected form must not leave the shared list one entry
        longer than it found it."""
        response = self.add(new_definition="Cabin filter", interval_months="")

        self.assertFalse(AssetServiceItem.objects.exists())
        self.assertFalse(ServiceDefinition.objects.filter(name="Cabin filter").exists())
        self.assertContains(
            self.client.get(response["Location"]), "at least one interval"
        )

    def test_the_box_is_on_the_page_with_a_label(self):
        page = self.client.get(reverse("asset_schedule", args=[self.asset.pk])).content.decode()
        self.assertIn('name="new_definition"', page)
        self.assertIn('<label for="id_new_definition"', page)


class TheAnswerAppearsAtTheRowThatAskedTests(TestCase):
    """The schedule card updates in place, so a banner at the top is unseen.

    `liveform.js` swaps the region without moving the page. Setting an interval
    on the eleventh item wrote a success message above a fold nobody was
    looking at, and pressing Done additionally cleared the date box — so the
    only two visible effects of a successful write were nothing, and a field
    emptying itself.
    """

    def setUp(self):
        from homeautoshop.accounts.models import User

        self.user = User.objects.create_user("andy", password="correct-horse-battery")
        self.client.force_login(self.user)
        self.asset = Asset.objects.create(nickname="Truck", meter_unit="mi")
        record_reading(self.asset, 100_000)
        self.item = AssetServiceItem.objects.create(
            asset=self.asset, definition=oil_change(), interval_distance=5000,
        )
        self.other = AssetServiceItem.objects.create(
            asset=self.asset,
            definition=ServiceDefinition.objects.create(name="Brake fluid"),
            interval_months=24,
        )

    def test_setting_an_interval_confirms_at_that_item(self):
        response = self.client.post(
            reverse("service_item_update", args=[self.asset.pk, self.item.pk]),
            {"interval_distance": "7500"},
        )

        page = self.client.get(response["Location"]).content.decode()
        self.assertIn("Interval saved.", page)

    def test_recording_a_service_confirms_at_that_item(self):
        response = self.client.post(
            reverse("service_item_complete", args=[self.asset.pk, self.item.pk]), {}
        )

        page = self.client.get(response["Location"]).content.decode()
        self.assertIn("Recorded.", page)

    def test_it_lands_on_one_row_and_not_the_others(self):
        response = self.client.post(
            reverse("service_item_update", args=[self.asset.pk, self.item.pk]),
            {"interval_distance": "7500"},
        )

        page = self.client.get(response["Location"]).content.decode()
        self.assertEqual(page.count('<span class="saved">'), 1)

    def test_the_words_come_from_here_and_never_off_the_url(self):
        """A message read out of a querystring is one anybody with a link can
        write onto somebody else's screen."""
        page = self.client.get(
            reverse("asset_schedule", args=[self.asset.pk]),
            {"saved": str(self.item.pk), "said": "Your account has been suspended"},
        ).content.decode()

        self.assertNotIn("suspended", page)
        self.assertNotIn('<span class="saved">', page)

    def test_the_plain_schedule_confirms_nothing(self):
        page = self.client.get(reverse("asset_schedule", args=[self.asset.pk])).content.decode()
        self.assertNotIn('<span class="saved">', page)


class ArrangingTheScheduleTests(TestCase):
    """The vehicle's schedule in the operator's order, not only the sort's.

    Soonest-first is the right default and stays the default — but it is a
    sort, and a sort cannot hold "these three are the ones I actually do
    myself, keep them together at the top". Two buttons, the same two the job
    items list carries, and the first press writes every row's position so
    the arrangement somebody was looking at is the one their swap lands in.
    """

    def setUp(self):
        from homeautoshop.accounts.models import User

        self.user = User.objects.create_user("andy", password="correct-horse-battery")
        self.client.force_login(self.user)
        self.asset = Asset.objects.create(nickname="Truck", meter_unit="mi")
        record_reading(self.asset, 100_000)
        self.a = self._item("Engine oil and filter", 1)
        self.b = self._item("Brake fluid", 2)
        self.c = self._item("Coolant", 3)
        self.ignored = self._item("Cabin filter", 4, status=ServiceStatus.DISABLED)

    def _item(self, name, months, **kwargs):
        return AssetServiceItem.objects.create(
            asset=self.asset,
            definition=ServiceDefinition.objects.create(name=name),
            interval_months=months,
            **kwargs,
        )

    def move(self, item, direction):
        return self.client.post(
            reverse("service_item_move", args=[self.asset.pk, item.pk]),
            {"direction": direction},
        )

    def names(self):
        page = self.client.get(reverse("asset_schedule", args=[self.asset.pk]))
        return [item.definition.name for item, _projection in page.context["rows"]]

    def test_untouched_it_still_reads_soonest_first(self):
        self.assertEqual(self.names(), ["Engine oil and filter", "Brake fluid", "Coolant"])

    def test_moving_one_down_swaps_it_with_its_neighbor(self):
        # Looking at the list first, as a person does: the neighbor is worked
        # out against the order the page last showed, and the page is what
        # computes the due dates that order rests on.
        self.names()
        self.move(self.a, "down")

        self.assertEqual(self.names(), ["Brake fluid", "Engine oil and filter", "Coolant"])

    def test_the_arrangement_outlives_a_recalculation(self):
        """Every request recalculates every item; the order is a column, not a
        sort, so it has to survive that."""
        self.names()
        self.move(self.c, "up")
        self.move(self.c, "up")

        self.assertEqual(self.names(), ["Coolant", "Engine oil and filter", "Brake fluid"])
        self.assertEqual(self.names(), ["Coolant", "Engine oil and filter", "Brake fluid"])

    def test_the_top_item_cannot_go_higher(self):
        self.move(self.a, "up")

        self.assertEqual(self.names(), ["Engine oil and filter", "Brake fluid", "Coolant"])

    def test_ignored_items_are_in_nobodys_count(self):
        """The neighbor is worked out against the list as shown. An ignored
        item folded away below must not be the row something swaps with."""
        self.move(self.c, "down")

        self.assertEqual(self.names(), ["Engine oil and filter", "Brake fluid", "Coolant"])
        self.ignored.refresh_from_db()
        self.assertEqual(self.ignored.status, ServiceStatus.DISABLED)

    def test_the_page_offers_both_buttons_and_disables_the_ends(self):
        page = self.client.get(reverse("asset_schedule", args=[self.asset.pk])).content.decode()

        self.assertIn(reverse("service_item_move", args=[self.asset.pk, self.a.pk]), page)
        self.assertIn("Move Engine oil and filter up", page)
        self.assertIn("Move Coolant down", page)
        self.assertEqual(page.count('type="submit" disabled'), 2)

    def test_a_helper_cannot_arrange_a_vehicle_they_were_not_given(self):
        from homeautoshop.accounts.models import Role, User

        helper = User.objects.create_user("sam", password="x" * 16, role=Role.HELPER)
        self.client.force_login(helper)

        self.assertEqual(self.move(self.a, "down").status_code, 403)


class ActingFromTheDueListTests(TestCase):
    """FR-MAINT-7 — the list you open to decide what to do, able to do it.

    Every row on Due led to the vehicle's schedule, and acting there left you
    on that vehicle with the rest of the list somewhere else. Done, Snooze and
    Ignore now post from the row and come back here; the row leaving is the
    confirmation, because each of the three means "no longer needs attention".
    """

    def setUp(self):
        from homeautoshop.accounts.models import User

        self.user = User.objects.create_user("andy", password="correct-horse-battery")
        self.client.force_login(self.user)
        self.asset = Asset.objects.create(nickname="Truck", meter_unit="mi")
        record_reading(self.asset, 100_000)
        # Six months, not one: with a 30-day lead an item due monthly is
        # *always* due soon, so completing it could never take it off the list.
        self.item = AssetServiceItem.objects.create(
            asset=self.asset, definition=oil_change(),
            interval_months=6, last_done_on=date(2025, 1, 1),
        )
        recalculate(self.item)

    def due(self):
        return self.client.get(reverse("due_list"))

    def test_the_row_carries_the_three_answers(self):
        page = self.due().content.decode()

        self.assertIn(reverse("service_item_complete", args=[self.asset.pk, self.item.pk]), page)
        self.assertIn(reverse("service_item_snooze", args=[self.asset.pk, self.item.pk]), page)
        self.assertIn('value="disable"', page)
        self.assertIn('name="usage"', page)

    def test_done_comes_back_here_and_the_row_is_gone(self):
        response = self.client.post(
            reverse("service_item_complete", args=[self.asset.pk, self.item.pk]),
            {"from": "due", "usage": "100000"},
        )

        self.assertEqual(response["Location"], reverse("due_list"))
        self.assertEqual(list(self.due().context["items"]), [])
        self.item.refresh_from_db()
        self.assertEqual(self.item.status, ServiceStatus.OK)

    @override_settings(SNOOZE_DAYS=7)
    def test_snooze_lasts_as_long_as_the_shop_says(self):
        page = self.due().content.decode()
        self.assertIn("Snooze 7 days", page)

        response = self.client.post(
            reverse("service_item_snooze", args=[self.asset.pk, self.item.pk]),
            {"from": "due", "action": "snooze"},
        )

        self.assertEqual(response["Location"], reverse("due_list"))
        self.item.refresh_from_db()
        self.assertEqual(self.item.snooze_until, timezone.localdate() + timedelta(days=7))
        self.assertEqual(self.item.status, ServiceStatus.SNOOZED)
        self.assertEqual(list(self.due().context["items"]), [])

    def test_ignore_comes_back_here_too(self):
        response = self.client.post(
            reverse("service_item_snooze", args=[self.asset.pk, self.item.pk]),
            {"from": "due", "action": "disable"},
        )

        self.assertEqual(response["Location"], reverse("due_list"))
        self.item.refresh_from_db()
        self.assertEqual(self.item.status, ServiceStatus.DISABLED)

    def test_from_the_schedule_the_same_actions_still_return_to_the_schedule(self):
        response = self.client.post(
            reverse("service_item_snooze", args=[self.asset.pk, self.item.pk]),
            {"action": "snooze"},
        )

        self.assertEqual(response["Location"], reverse("asset_schedule", args=[self.asset.pk]))

    def test_the_setting_is_offered_where_the_other_thresholds_are(self):
        from homeautoshop.core.settings_registry import BY_KEY

        self.assertEqual(BY_KEY["SNOOZE_DAYS"].group, "maintenance")


class TheStoredStatusFollowsItsInputsTests(TestCase):
    """FR-MAINT-7 — reported as caching: several refreshes after changing the
    look-ahead before the Due list caught up.

    `status` is stored, and stored is what the board, the Due list and the
    report read — on purpose, so a card per vehicle costs no arithmetic. So it
    is rewritten where its inputs change: the look-ahead settings, the meter,
    and the calendar. Nothing recomputes at read time.
    """

    def setUp(self):
        from homeautoshop.core import runtime

        self.addCleanup(runtime.invalidate)
        self.asset = Asset.objects.create(nickname="Truck", meter_unit="mi")
        record_reading(self.asset, 100_000)
        # Due in 20 days.
        self.item = AssetServiceItem.objects.create(
            asset=self.asset, definition=oil_change(),
            interval_months=1, last_done_on=timezone.localdate() - timedelta(days=10),
        )
        recalculate(self.item)

    def test_saving_a_wider_look_ahead_rewrites_every_status(self):
        from homeautoshop.core import runtime

        runtime.save({"DUE_SOON_DAYS": 10})
        self.assertNotIn(self.item, due_dashboard())

        runtime.save({"DUE_SOON_DAYS": 30})

        self.assertIn(self.item, due_dashboard())
        self.item.refresh_from_db()
        self.assertEqual(self.item.status, ServiceStatus.DUE_SOON)

    def test_and_a_narrower_one_takes_it_off_again(self):
        from homeautoshop.core import runtime

        runtime.save({"DUE_SOON_DAYS": 30})
        self.assertIn(self.item, due_dashboard())

        runtime.save({"DUE_SOON_DAYS": 10})

        self.assertNotIn(self.item, due_dashboard())

    def test_a_new_reading_rewrites_that_vehicles_statuses(self):
        tires = AssetServiceItem.objects.create(
            asset=self.asset,
            definition=ServiceDefinition.objects.create(name="Tire rotation"),
            interval_distance=5000, interval_unit="mi", last_done_usage=96_000,
        )
        recalculate(tires)
        self.assertEqual(tires.status, ServiceStatus.OK)  # 1,000 mi to go

        record_reading(self.asset, 100_800)

        tires.refresh_from_db()
        self.assertEqual(tires.status, ServiceStatus.DUE_SOON)

    def test_removing_a_reading_rewrites_them_too(self):
        tires = AssetServiceItem.objects.create(
            asset=self.asset,
            definition=ServiceDefinition.objects.create(name="Tire rotation"),
            interval_distance=5000, interval_unit="mi", last_done_usage=96_000,
        )
        typo = record_reading(self.asset, 100_800)
        tires.refresh_from_db()
        self.assertEqual(tires.status, ServiceStatus.DUE_SOON)

        typo.delete()

        tires.refresh_from_db()
        self.assertEqual(tires.status, ServiceStatus.OK)

    def test_a_day_passing_is_covered_by_the_daily_job(self):
        from homeautoshop.core import jobs, schedule
        from homeautoshop.core.models import Job

        self.assertIn("maintenance.refresh", dict(schedule.recurring()))
        # Stale on purpose: the row says OK about an item that is due soon.
        AssetServiceItem.objects.filter(pk=self.item.pk).update(status=ServiceStatus.OK)

        self.assertTrue(jobs.run_one(Job.objects.create(type="maintenance.refresh")))

        self.item.refresh_from_db()
        self.assertEqual(self.item.status, ServiceStatus.DUE_SOON)

    def test_a_refresh_does_not_rewrite_rows_that_did_not_move(self):
        """A `RevisionedModel` write bumps the revision; forty items refreshed
        every day must not be forty revisions for nothing."""
        from .services import refresh_fleet

        self.item.refresh_from_db()
        revision = self.item.revision

        refresh_fleet()
        refresh_fleet()

        self.item.refresh_from_db()
        self.assertEqual(self.item.revision, revision)

    def test_the_row_arrives_with_its_projection(self):
        rows = due_dashboard()
        self.assertTrue(hasattr(rows[0], "projection"))
        self.assertEqual(rows[0].projection.item, rows[0])


class TheDueListReadsSoonestFirstTests(TestCase):
    """FR-MAINT-7 — reported as random order.

    Every Safety item was sorted ahead of every routine one whatever the dates,
    so a registration due in 213 days sat under three safety items due in 365.
    Overdue safety still leads; among things merely coming up, soonest is
    first and safety breaks the tie.
    """

    def setUp(self):
        self.asset = Asset.objects.create(nickname="Truck", meter_unit="mi")
        record_reading(self.asset, 100_000)

    def item(self, name, *, days, safety=False, **kwargs):
        definition = ServiceDefinition.objects.create(
            name=name, severity=Severity.SAFETY if safety else Severity.ROUTINE
        )
        row = AssetServiceItem.objects.create(
            asset=self.asset, definition=definition,
            interval_months=12, last_done_on=timezone.localdate() - timedelta(days=365 - days),
            **kwargs,
        )
        recalculate(row)
        return row

    def names(self):
        return [row.definition.name for row in due_dashboard()]

    @override_settings(DUE_SOON_DAYS=400)
    def test_soonest_first_among_things_coming_up(self):
        self.item("Wiper blades", days=365, safety=True)
        self.item("Registration renewal", days=213)
        self.item("Brake inspection", days=365, safety=True)

        self.assertEqual(
            self.names(), ["Registration renewal", "Brake inspection", "Wiper blades"]
        )

    @override_settings(DUE_SOON_DAYS=400)
    def test_overdue_safety_still_leads_everything(self):
        self.item("Registration renewal", days=10)
        self.item("Brake inspection", days=-5, safety=True)
        self.item("Engine oil and filter", days=-20)

        self.assertEqual(
            self.names(),
            ["Brake inspection", "Engine oil and filter", "Registration renewal"],
        )

    @override_settings(DUE_SOON_DAYS=400, DUE_SOON_DISTANCE=2000)
    def test_an_item_due_by_distance_sorts_on_when_that_is_expected(self):
        """It has no `next_due_on`, and used to sort last however soon it was."""
        self.item("Registration renewal", days=200)
        # 1,000 mi to go at the fallback rate of 30 mi a day: about 33 days.
        tires = AssetServiceItem.objects.create(
            asset=self.asset,
            definition=ServiceDefinition.objects.create(name="Tire rotation"),
            interval_distance=5000, interval_unit="mi", last_done_usage=96_000,
        )
        recalculate(tires)

        self.assertEqual(self.names(), ["Tire rotation", "Registration renewal"])


class RefreshingOncePerImportTests(TestCase):
    """Every saved reading refreshes its vehicle's due statuses, which is right
    for one typed in the garage and wrong by the size of the import for a
    LubeLogger pull. Inside `refresh_later()` a reading only notes its vehicle,
    and each touched vehicle is refreshed once when the block ends."""

    def setUp(self):
        self.asset = Asset.objects.create(nickname="Truck", meter_unit="mi")
        record_reading(self.asset, 100_000)
        self.tires = AssetServiceItem.objects.create(
            asset=self.asset,
            definition=ServiceDefinition.objects.create(name="Tire rotation"),
            interval_distance=5000, interval_unit="mi", last_done_usage=96_000,
        )
        recalculate(self.tires)
        self.assertEqual(self.tires.status, ServiceStatus.OK)  # 1,000 mi to go

    def test_readings_inside_the_block_wait_and_the_vehicle_is_refreshed_on_exit(self):
        from .services import refresh_later

        with refresh_later() as touched:
            record_reading(self.asset, 100_600)
            record_reading(self.asset, 100_800)
            self.tires.refresh_from_db()
            self.assertEqual(self.tires.status, ServiceStatus.OK, "refreshed too early")
            self.assertEqual(touched, {self.asset.pk})

        self.tires.refresh_from_db()
        self.assertEqual(self.tires.status, ServiceStatus.DUE_SOON)

    def test_outside_a_block_a_reading_still_refreshes_at_once(self):
        record_reading(self.asset, 100_800)

        self.tires.refresh_from_db()
        self.assertEqual(self.tires.status, ServiceStatus.DUE_SOON)

    def test_a_block_that_touched_nothing_refreshes_nothing(self):
        from .services import refresh_later

        self.tires.refresh_from_db()
        revision = self.tires.revision
        with refresh_later():
            pass
        self.tires.refresh_from_db()
        self.assertEqual(self.tires.revision, revision)
