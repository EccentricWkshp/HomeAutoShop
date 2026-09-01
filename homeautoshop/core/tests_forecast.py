"""Projecting the next twelve months of spend (SPEC R-7).

`spend_by_month` looks backwards and this is the same shape looking forwards,
which is the whole reason it is worth building: the schedule already knows when
each service comes round and the completion history already knows what that
service has cost in this shop, so the forecast is a join over data nobody has
to enter twice.

Two things it must not do, and both are load-bearing:

* **Price an unknown at zero.** A service this shop has never performed has no
  price, and counting it as free would understate exactly the thing the report
  exists to warn about. It is counted, named, and left out of the total, and
  the figure is labeled a floor.
* **Count a recurring service once.** An oil change on a daily driver lands
  three times in a year. A forecast that counted each schedule item once would
  be roughly half of what actually happens, and wrong in the same direction for
  everybody.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from django.test import TestCase
from django.urls import reverse

from homeautoshop.accounts.models import Role, User
from homeautoshop.assets.models import Asset, UsageReading
from homeautoshop.core.costs import forecast, typical_costs
from homeautoshop.maintenance.models import (
    AssetServiceItem,
    ServiceCompletion,
    ServiceDefinition,
)
from homeautoshop.parts.models import Part, PartUsage
from homeautoshop.work.models import WorkOrder

TODAY = date(2026, 3, 1)


class Base(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="andy", password="x" * 16, role=Role.ADMIN
        )
        self.client.force_login(self.user)
        self.asset = Asset.objects.create(nickname="Aero", meter_unit="mi")
        self.oil = ServiceDefinition.objects.create(name="Engine oil and filter")

    def drives(self, per_day=40, days=180):
        """Give the asset an observed usage rate, which is what turns a
        distance interval into a date."""
        start = TODAY - timedelta(days=days)
        for offset in (0, days):
            UsageReading.objects.create(
                asset=self.asset,
                meter="odometer",
                read_on=start + timedelta(days=offset),
                value=Decimal(10000 + per_day * offset),
            )
        self.asset.refresh_from_db()

    def scheduled(self, definition=None, *, distance=5000, months=None, done_at=10000):
        return AssetServiceItem.objects.create(
            asset=self.asset,
            definition=definition or self.oil,
            interval_distance=distance,
            interval_unit="mi",
            interval_months=months,
            last_done_on=TODAY - timedelta(days=180),
            last_done_usage=Decimal(done_at),
            next_due_usage=Decimal(done_at + (distance or 0)),
        )

    def performed(self, item, *, cost_minor, on=None, also=()):
        """A past completion, with a work order carrying a real cost."""
        order = WorkOrder.objects.create(asset=self.asset, title="Service")
        part = Part.objects.create(name="Filter %s" % cost_minor)
        PartUsage.objects.create(
            part=part, qty=1, work_order=order, unit_cost_minor=cost_minor
        )
        for target in (item, *also):
            ServiceCompletion.objects.create(
                service_item=target,
                work_order=order,
                completed_on=on or TODAY - timedelta(days=200),
            )
        return order


class PricingTests(Base):
    def test_a_service_is_priced_from_what_it_cost_here_before(self):
        item = self.scheduled()
        self.performed(item, cost_minor=6000)

        self.assertEqual(typical_costs()[self.oil.pk], 6000)

    def test_shared_work_is_split_rather_than_counted_twice(self):
        """One Saturday that closed three services is not three bills. Charging
        the whole order to each would treble a year's forecast."""
        oil = self.scheduled()
        tires = self.scheduled(
            ServiceDefinition.objects.create(name="Tire rotation"), distance=6000
        )
        air = self.scheduled(
            ServiceDefinition.objects.create(name="Air filter"), distance=15000
        )
        self.performed(oil, cost_minor=9000, also=[tires, air])

        prices = typical_costs()

        self.assertEqual(prices[self.oil.pk], 3000)
        self.assertEqual(prices[tires.definition_id], 3000)
        self.assertEqual(prices[air.definition_id], 3000)

    def test_the_median_is_taken_so_one_bad_day_does_not_set_the_price(self):
        """A brake job where the caliper also failed is not what the next one
        will cost."""
        item = self.scheduled()
        for cost in (4000, 4500, 40000):
            self.performed(item, cost_minor=cost)

        self.assertEqual(typical_costs()[self.oil.pk], 4500)

    def test_a_work_order_with_no_recorded_cost_prices_nothing(self):
        """Far likelier to be a job whose parts were never entered than a
        service that is genuinely free, and reading it as free would drag every
        future estimate down with it."""
        item = self.scheduled()
        order = WorkOrder.objects.create(asset=self.asset, title="Free")
        ServiceCompletion.objects.create(
            service_item=item, work_order=order, completed_on=TODAY
        )

        self.assertNotIn(self.oil.pk, typical_costs())


class ForecastTests(Base):
    def test_a_due_service_lands_in_the_month_it_is_expected(self):
        self.drives()
        item = self.scheduled(done_at=10000)
        self.performed(item, cost_minor=6000)

        plan = forecast(today=TODAY)

        self.assertTrue(plan.months)
        self.assertGreaterEqual(plan.total_minor, 6000)

    def test_a_recurring_service_appears_as_often_as_it_recurs(self):
        """40 mi a day against a 5,000 mi interval is roughly every four
        months, so a year holds about three of them — not one."""
        self.drives(per_day=40)
        item = self.scheduled(distance=5000, done_at=10000)
        self.performed(item, cost_minor=6000)

        plan = forecast(today=TODAY)

        occurrences = sum(len(month.entries) for month in plan.months)
        self.assertGreaterEqual(occurrences, 2)
        self.assertEqual(plan.total_minor, occurrences * 6000)

    def test_a_vehicle_that_barely_moves_is_forecast_lower(self):
        """The same schedule on a truck that leaves the yard twice a month is
        a smaller bill, and the observed rate is what says so."""
        self.drives(per_day=2)
        item = self.scheduled(distance=5000, done_at=10000)
        self.performed(item, cost_minor=6000)

        plan = forecast(today=TODAY)

        self.assertLessEqual(sum(len(m.entries) for m in plan.months), 1)

    def test_a_service_never_done_here_is_counted_but_not_priced(self):
        self.drives()
        self.scheduled()

        plan = forecast(today=TODAY)

        self.assertEqual(plan.total_minor, 0)
        self.assertTrue(plan.unpriced)
        self.assertFalse(plan.is_complete)

    def test_and_the_page_says_the_total_is_a_floor(self):
        self.drives()
        self.scheduled()

        self.assertIn("floor", forecast(today=TODAY).basis)

    def test_a_priced_forecast_says_where_the_prices_came_from(self):
        self.drives()
        item = self.scheduled()
        self.performed(item, cost_minor=6000)

        self.assertIn("paid", forecast(today=TODAY).basis)

    def test_something_overdue_is_spend_that_has_not_happened_yet(self):
        """It belongs in the month ahead. Dropping it because its date has
        passed would forecast nothing for the vehicle most likely to need
        money spent on it."""
        self.drives()
        item = self.scheduled(done_at=0)
        item.next_due_usage = Decimal(1)
        item.save()
        self.performed(item, cost_minor=6000)

        plan = forecast(today=TODAY)

        self.assertTrue(plan.months)
        self.assertGreaterEqual(plan.months[0].entries[0].due_on, TODAY)

    def test_a_snoozed_item_is_not_forecast(self):
        """Snoozing says *not now*, and a forecast that ignores it is a
        forecast that argues with the operator."""
        self.drives()
        item = self.scheduled()
        self.performed(item, cost_minor=6000)
        item.snooze_until = TODAY + timedelta(days=400)
        item.save()

        self.assertEqual(forecast(today=TODAY).total_minor, 0)

    def test_a_retired_vehicle_is_not_forecast(self):
        self.drives()
        item = self.scheduled()
        self.performed(item, cost_minor=6000)
        self.asset.status = "sold"
        self.asset.save()

        self.assertEqual(forecast(today=TODAY).total_minor, 0)

    def test_an_empty_shop_says_so_rather_than_showing_a_zero(self):
        plan = forecast(today=TODAY)

        self.assertEqual(plan.months, [])
        self.assertIn("Nothing is due", plan.basis)

    def test_nothing_is_forecast_beyond_the_horizon(self):
        self.drives()
        item = self.scheduled()
        self.performed(item, cost_minor=6000)

        plan = forecast(months=12, today=TODAY)

        for month in plan.months:
            self.assertLess(month.month, "2027-03")


class OnThePageTests(Base):
    def test_the_report_shows_it(self):
        self.drives()
        item = self.scheduled()
        self.performed(item, cost_minor=6000)

        page = self.client.get(reverse("reports"))

        self.assertContains(page, "Next 12 months")
        self.assertContains(page, reverse("export_csv", args=["forecast"]))

    def test_the_csv_carries_a_row_per_occurrence(self):
        self.drives()
        item = self.scheduled()
        self.performed(item, cost_minor=6000)

        body = self.client.get(
            reverse("export_csv", args=["forecast"])
        ).content.decode()

        self.assertIn("Engine oil and filter", body)
        self.assertIn("Aero", body)
        self.assertIn("6000", body)

    def test_an_unpriced_row_exports_blank_and_never_zero(self):
        """A spreadsheet sums a zero and cannot sum a blank, which is the
        whole difference between a floor and a wrong total."""
        self.drives()
        self.scheduled()

        rows = self.client.get(
            reverse("export_csv", args=["forecast"])
        ).content.decode().strip().splitlines()

        self.assertGreater(len(rows), 1)
        self.assertTrue(rows[1].endswith(",,USD"), rows[1])

    def test_it_needs_a_login(self):
        self.client.logout()
        response = self.client.get(reverse("export_csv", args=["forecast"]))
        self.assertEqual(response.status_code, 302)


class PartsBreakdownTests(TestCase):
    """What the parts money was spent on (FR-COST-2).

    Reported as: *"a flat number by itself for a cost doesn't tell much."*
    It does not. `Parts — $1,240.00` is a true figure and a useless one: the
    question anybody has about it is *on what*, and until now neither the
    costs screen nor the sale document could answer.

    Grouped by the job, because the job is what the money bought, and ordered
    biggest first — the costs screen is asking where the money went and the
    answer belongs at the top. The report lists the same work by date under
    Service history, so ordering this one the same way would print the list
    twice and answer the question neither time.
    """

    def setUp(self):
        self.user = User.objects.create_user(
            username="andy", password="x" * 16, role=Role.ADMIN
        )
        self.client.force_login(self.user)
        self.asset = Asset.objects.create(nickname="Aero")

    def job(self, title, *, parts):
        order = WorkOrder.objects.create(asset=self.asset, title=title)
        for name, cost in parts:
            PartUsage.objects.create(
                part=Part.objects.create(name=name),
                qty=1,
                work_order=order,
                unit_cost_minor=cost,
            )
        return order

    def parts_line(self):
        from homeautoshop.core.costs import asset_cost

        return next(
            line for line in asset_cost(self.asset).lines if line.label == "Parts"
        )

    def test_the_parts_total_says_which_jobs_it_paid_for(self):
        self.job("Brake overhaul", parts=[("Pads", 4000), ("Rotors", 12000)])
        self.job("Oil change", parts=[("Filter", 900)])

        line = self.parts_line()

        self.assertEqual(line.amount_minor, 16900)
        self.assertEqual([row.label for row in line.breakdown],
                         ["Brake overhaul", "Oil change"])

    def test_the_biggest_job_leads(self):
        """The costs screen asks where the money went; the answer goes first."""
        self.job("Oil change", parts=[("Filter", 900)])
        self.job("Gearbox", parts=[("Gearbox", 90000)])

        self.assertEqual(self.parts_line().breakdown[0].label, "Gearbox")

    def test_each_row_names_the_parts(self):
        self.job("Brake overhaul", parts=[("Pads", 4000), ("Rotors", 12000)])

        detail = self.parts_line().breakdown[0].detail

        self.assertIn("Pads", detail)
        self.assertIn("Rotors", detail)

    def test_parts_with_no_job_behind_them_get_their_own_row(self):
        """FR-INV-10 — plenty of what a home garage fits was never a work
        order, that spend is as real as the rest, and a breakdown that omitted
        it would not add up to the total above it."""
        PartUsage.objects.create(
            part=Part.objects.create(name="Wiper"),
            qty=1,
            asset=self.asset,
            unit_cost_minor=1500,
        )

        line = self.parts_line()

        self.assertEqual(line.breakdown[0].label, "Not recorded against a job")
        self.assertEqual(line.breakdown[0].amount_minor, 1500)

    def test_the_breakdown_adds_up_to_the_line_above_it(self):
        """The property that makes it checkable rather than decorative."""
        for n in range(12):
            self.job("Job %d" % n, parts=[("Part %d" % n, 1000 * (n + 1))])

        line = self.parts_line()

        self.assertEqual(
            sum(row.amount_minor for row in line.breakdown), line.amount_minor
        )

    def test_a_long_history_is_summarized_rather_than_cut(self):
        """Stopping short without saying so leaves somebody checking the
        arithmetic with a hole instead of a footnote."""
        for n in range(12):
            self.job("Job %d" % n, parts=[("Part %d" % n, 1000 * (n + 1))])

        labels = [row.label for row in self.parts_line().breakdown]

        self.assertEqual(len(labels), 9)
        self.assertEqual(labels[-1], "4 more jobs")

    def test_the_costs_screen_shows_it(self):
        self.job("Brake overhaul", parts=[("Pads", 4000)])

        page = self.client.get(reverse("asset_costs", args=[self.asset.pk]))

        self.assertContains(page, "Brake overhaul")
        self.assertContains(page, "Pads")

    def test_and_so_does_the_report(self):
        self.job("Brake overhaul", parts=[("Pads", 4000)])

        page = self.client.get(reverse("asset_report", args=[self.asset.pk]))

        self.assertContains(page, "Brake overhaul")

    def test_but_not_when_costs_are_left_out_of_the_report(self):
        """The whole section goes when somebody prints it without costs, and
        the breakdown is part of the section rather than an exception to it."""
        self.job("Brake overhaul", parts=[("Pads", 4000)])

        page = self.client.get(
            reverse("asset_report", args=[self.asset.pk]), {"costs": "0"}
        )

        self.assertNotContains(page, "Cost of ownership")
