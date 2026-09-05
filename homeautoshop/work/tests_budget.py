"""
Project budget burn-down (SPEC §7.6b, FR-COST-8, FR-WO-8 — roadmap R-6).

The case this exists for is an engine swap: a project work order with a
teardown, a machine-shop job and a reassembly hanging off it, a number the
household agreed to, and eight months in which to forget what that number
was. Every test below is a way that could quietly report the wrong answer.

The first one is the one that made this worth building. FR-WO-8 said costs
roll up to the parent since the first draft, and nothing ever did it — so a
project reported its own line items and none of its children's, which for an
engine swap is close to reporting nothing. A burn-down on top of that would
not have been merely incomplete; it would have said *on budget* about a
project that was not.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone, translation

from homeautoshop.accounts.models import Role, User
from homeautoshop.assets.models import Asset
from homeautoshop.core.budget import budget_burndown, project_cost
from homeautoshop.core.costs import work_order_cost
from homeautoshop.parts.models import Part, StockLot
from homeautoshop.parts.services import consume
from homeautoshop.purchasing.models import (
    Expense,
    ExpenseCategory,
    Purchase,
    PurchaseLine,
    PurchaseStatus,
    Vendor,
)
from homeautoshop.work.models import WorkOrder, WorkOrderType

VIN = "1M8GDM9AXKP042788"


class Fixture(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="andy", password="x" * 16, role=Role.ADMIN
        )
        self.client.force_login(self.user)
        self.asset = Asset.objects.create(nickname="Red truck", vin=VIN)
        self.project = WorkOrder.objects.create(
            asset=self.asset,
            title="LS swap",
            type=WorkOrderType.PROJECT,
            budget_minor=500000,  # $5,000
        )

    def child(self, title: str, parent=None) -> WorkOrder:
        return WorkOrder.objects.create(
            asset=self.asset, title=title, parent=parent or self.project
        )

    def spend(self, work_order, minor: int, *, on: date | None = None):
        """An expense is the shortest way to put money on a job."""
        return Expense.objects.create(
            work_order=work_order,
            category=ExpenseCategory.MACHINE_WORK,
            amount_minor=minor,
            incurred_on=on or timezone.localdate(),
        )

    def fit(self, work_order, *, cost: int, qty=1):
        part = Part.objects.create(name=f"Part {work_order.pk}-{cost}")
        StockLot.objects.create(
            part=part, qty_on_hand=Decimal(str(qty)), unit_cost_minor=cost
        )
        return consume(part, Decimal(str(qty)), work_order=work_order)


class TheTreeIsTheProjectTests(Fixture):
    """FR-WO-8, which the document has always claimed and nothing did."""

    def test_a_childs_spend_counts_against_the_parents_budget(self):
        teardown = self.child("Teardown")
        self.spend(teardown, 120000)
        self.assertEqual(budget_burndown(self.project).spent_minor, 120000)

    def test_a_grandchilds_spend_counts_too(self):
        """The tree is walked, not the one row of children."""
        machine = self.child("Machine work", parent=self.child("Teardown"))
        self.spend(machine, 80000)
        self.assertEqual(budget_burndown(self.project).spent_minor, 80000)

    def test_the_one_job_rollup_still_answers_for_one_job(self):
        """The costs card on a leaf work order must not change meaning."""
        teardown = self.child("Teardown")
        self.spend(teardown, 120000)
        self.spend(self.project, 5000)
        self.assertEqual(work_order_cost(self.project).total_minor, 5000)
        self.assertEqual(project_cost(self.project).total_minor, 125000)

    def test_another_projects_spend_stays_out_of_it(self):
        other = WorkOrder.objects.create(
            asset=self.asset, title="Bodywork", type=WorkOrderType.PROJECT
        )
        self.spend(self.child("Teardown"), 10000)
        self.spend(WorkOrder.objects.create(asset=self.asset, title="Panels", parent=other), 90000)
        self.assertEqual(budget_burndown(self.project).spent_minor, 10000)


class ThreeKindsOfMoneyTests(Fixture):
    """Fitted, on the shelf, on order — and never the same dollar twice."""

    def _order(self, *, qty, unit, received=0, shipping=0, status=PurchaseStatus.ORDERED):
        vendor = Vendor.objects.create(name="Summit")
        purchase = Purchase.objects.create(
            vendor=vendor, work_order=self.project, status=status, shipping_minor=shipping
        )
        part = Part.objects.create(name=f"Ordered {qty}@{unit}")
        line = PurchaseLine.objects.create(
            purchase=purchase,
            part=part,
            qty_ordered=Decimal(str(qty)),
            qty_received=Decimal(str(received)),
            extended_minor=int(unit * Decimal(str(qty))),
        )
        return purchase, line

    def test_an_open_order_is_committed_but_not_spent(self):
        self._order(qty=2, unit=50000)
        budget = budget_burndown(self.project)
        self.assertEqual(budget.spent_minor, 0)
        self.assertEqual(budget.on_order_minor, 100000)
        self.assertEqual(budget.committed_minor, 100000)

    def test_shipping_rides_along_with_what_is_still_outstanding(self):
        """Overheads land in the lot cost at receiving, so committing the bare
        unit price would understate the order by the shipping already invoiced."""
        self._order(qty=1, unit=100000, shipping=2500)
        self.assertEqual(budget_burndown(self.project).on_order_minor, 102500)

    def test_a_cart_is_not_a_commitment(self):
        """Nobody has promised anybody anything yet."""
        self._order(qty=1, unit=50000, status=PurchaseStatus.CART)
        self.assertEqual(budget_burndown(self.project).on_order_minor, 0)

    def test_a_cancelled_order_is_not_a_commitment_either(self):
        self._order(qty=1, unit=50000, status=PurchaseStatus.CANCELLED)
        self.assertEqual(budget_burndown(self.project).on_order_minor, 0)

    def test_a_partly_received_order_commits_only_what_is_still_coming(self):
        self._order(qty=4, unit=25000, received=3, status=PurchaseStatus.PARTIAL)
        self.assertEqual(budget_burndown(self.project).on_order_minor, 25000)

    def test_a_received_purchase_is_no_longer_on_order(self):
        self._order(qty=1, unit=50000, received=1, status=PurchaseStatus.RECEIVED)
        self.assertEqual(budget_burndown(self.project).on_order_minor, 0)

    def test_stock_bought_for_the_job_and_not_yet_fitted_shows_as_on_the_shelf(self):
        _purchase, line = self._order(qty=1, unit=40000, received=1, status=PurchaseStatus.RECEIVED)
        StockLot.objects.create(
            part=line.part, qty_on_hand=Decimal("1"), unit_cost_minor=40000, purchase_line=line
        )
        budget = budget_burndown(self.project)
        self.assertEqual(budget.on_shelf_minor, 40000)
        self.assertEqual(budget.spent_minor, 0)

    def test_fitting_it_moves_the_money_rather_than_counting_it_twice(self):
        _purchase, line = self._order(qty=1, unit=40000, received=1, status=PurchaseStatus.RECEIVED)
        StockLot.objects.create(
            part=line.part, qty_on_hand=Decimal("1"), unit_cost_minor=40000, purchase_line=line
        )
        consume(line.part, Decimal("1"), work_order=self.project)

        budget = budget_burndown(self.project)
        self.assertEqual(budget.on_shelf_minor, 0)
        self.assertEqual(budget.spent_minor, 40000)
        self.assertEqual(budget.committed_minor, 40000)

    def test_stock_bought_for_nothing_in_particular_is_not_this_projects_money(self):
        part = Part.objects.create(name="Shop rags")
        StockLot.objects.create(part=part, qty_on_hand=Decimal("10"), unit_cost_minor=500)
        self.assertEqual(budget_burndown(self.project).on_shelf_minor, 0)


class WhereThatLeavesItTests(Fixture):
    def test_remaining_counts_everything_committed(self):
        self.fit(self.project, cost=100000)
        budget = budget_burndown(self.project)
        self.assertEqual(budget.remaining_minor, 400000)
        self.assertEqual(budget.used_percent, 20)
        self.assertFalse(budget.is_over)

    def test_an_overrun_is_reported_rather_than_clamped(self):
        """140% is the fact. The bar is what needs a limit, not the number."""
        self.spend(self.project, 700000)
        budget = budget_burndown(self.project)
        self.assertTrue(budget.is_over)
        self.assertEqual(budget.over_minor, 200000)
        self.assertEqual(budget.used_percent, 140)

    def test_the_bar_rescales_so_the_overrun_stays_visible(self):
        """Scaled to the larger of the two, so the budget marker moves left
        and the bands run past it instead of pinning at the end."""
        self.spend(self.project, 1000000)
        budget = budget_burndown(self.project)
        self.assertEqual(budget.spent_percent, 100)
        self.assertEqual(budget.budget_marker_percent, 50)

    def test_no_budget_means_no_card(self):
        plain = WorkOrder.objects.create(asset=self.asset, title="Oil change")
        self.assertIsNone(budget_burndown(plain))

    def test_a_budget_of_zero_is_a_budget(self):
        """`None` and 0 are different statements. Somebody who says this job
        should cost nothing wants to be told the moment it does not."""
        free = WorkOrder.objects.create(asset=self.asset, title="Warranty job", budget_minor=0)
        self.spend(free, 4500)
        budget = budget_burndown(free)
        self.assertIsNotNone(budget)
        self.assertTrue(budget.is_over)
        self.assertEqual(budget.over_minor, 4500)


class YourOwnTimeIsNotBudgetedTests(Fixture):
    """FR-TIME-3 values a Saturday for reporting. A budget is cash."""

    def _log(self, minutes: int):
        from homeautoshop.work.models import TimeEntry

        TimeEntry.objects.create(
            work_order=self.child("Teardown"), minutes=minutes, user=self.user
        )

    def test_hours_are_counted_across_the_tree(self):
        self._log(180)
        self.assertEqual(budget_burndown(self.project).labour_hours, 3.0)

    def test_but_they_never_move_the_bar(self):
        from django.test import override_settings

        with override_settings(LABOR_RATE_MINOR=10000):
            self._log(600)
            budget = budget_burndown(self.project)
            self.assertEqual(budget.spent_minor, 0)
            self.assertEqual(budget.committed_minor, 0)
            self.assertFalse(budget.is_over)


class TheLineTests(Fixture):
    """Monthly buckets — a burn-down is about pace."""

    def test_a_quiet_month_still_gets_a_row(self):
        """Otherwise the slope between two rows is a lie about how fast it went."""
        today = date(2026, 6, 15)
        self.spend(self.project, 10000, on=date(2026, 3, 4))
        self.spend(self.project, 10000, on=date(2026, 6, 2))
        months = budget_burndown(self.project, today=today).months
        self.assertEqual(
            [m.month for m in months], ["2026-03", "2026-04", "2026-05", "2026-06"]
        )
        self.assertEqual([m.spent_minor for m in months], [10000, 0, 0, 10000])

    def test_the_running_total_accumulates(self):
        today = date(2026, 6, 15)
        self.spend(self.project, 10000, on=date(2026, 5, 4))
        self.spend(self.project, 25000, on=date(2026, 6, 2))
        months = budget_burndown(self.project, today=today).months
        self.assertEqual([m.cumulative_minor for m in months], [10000, 35000])

    def test_spend_from_before_the_record_existed_is_not_dropped(self):
        """Somebody who buys the engine and opens the work order afterwards is
        doing the ordinary thing; a window starting at `opened_at` loses it."""
        self.spend(self.project, 90000, on=timezone.localdate() - timedelta(days=90))
        months = budget_burndown(self.project).months
        self.assertEqual(months[0].spent_minor, 90000)
        self.assertEqual(months[-1].cumulative_minor, 90000)

    def test_the_line_runs_up_to_today_even_after_the_spending_stops(self):
        today = date(2026, 6, 15)
        self.spend(self.project, 10000, on=date(2026, 4, 4))
        self.assertEqual(budget_burndown(self.project, today=today).months[-1].month, "2026-06")

    def test_nothing_spent_means_no_rows_rather_than_a_flat_invented_line(self):
        self.assertEqual(budget_burndown(self.project).months, [])


class OnTheScreenTests(Fixture):
    def test_the_card_appears_on_a_work_order_with_a_budget(self):
        self.fit(self.project, cost=100000)
        page = self.client.get(
            reverse("work_order_detail", args=[self.project.pk])
        ).content.decode()
        self.assertIn('id="budget"', page)
        self.assertIn("Budget", page)

    def test_it_stays_off_a_work_order_without_one(self):
        plain = WorkOrder.objects.create(asset=self.asset, title="Oil change")
        page = self.client.get(reverse("work_order_detail", args=[plain.pk])).content.decode()
        self.assertNotIn('id="budget"', page)

    def test_a_project_lists_the_jobs_under_it(self):
        """There was no way to see a project's children from the project."""
        child = self.child("Teardown")
        page = self.client.get(
            reverse("work_order_detail", args=[self.project.pk])
        ).content.decode()
        self.assertIn(child.number, page)

    def test_a_zero_budget_overrun_does_not_claim_zero_percent(self):
        """There is no percentage of nothing, and `0% committed` over a job
        that has spent $45 is the one reading the card must never give."""
        free = WorkOrder.objects.create(asset=self.asset, title="Warranty job", budget_minor=0)
        self.spend(free, 4500)
        page = self.client.get(
            reverse("work_order_detail", args=[free.pk])
        ).content.decode()
        self.assertIn("over budget", page)
        self.assertNotIn("0% committed", page)

    def test_the_bar_is_a_width_in_every_locale(self):
        """A CSS length is not prose. `33,33%` is not a length, and fr-CA
        renders 33.33 that way — the band would collapse to nothing in exactly
        the locales §5.6 exists to support."""
        import re

        self.spend(self.project, 166667)  # a third of the budget, awkwardly
        # `LocaleMiddleware` activates a language per request and never
        # deactivates, so without this the next case in the suite inherits
        # French — which is how a trouble-code search two apps away used to
        # fail only in a full run.
        self.addCleanup(translation.deactivate)
        page = self.client.get(
            reverse("work_order_detail", args=[self.project.pk]),
            headers={"accept-language": "fr-ca"},
        ).content.decode()
        widths = re.findall(r"inline-size:([^%]*)%", page)
        self.assertTrue(widths)
        for width in widths:
            with self.subTest(width=width):
                self.assertNotIn(",", width)
                float(width)  # raises if it is not a number

    def test_the_budget_is_typed_in_the_currency_not_in_cents(self):
        """The trap `MoneyFormMixin` exists to close: 5000 must not be $50."""
        response = self.client.post(
            reverse("work_order_edit", args=[self.project.pk]),
            {
                "asset": str(self.asset.pk),
                "title": "LS swap",
                "type": WorkOrderType.PROJECT,
                "priority": "3",
                "budget_minor": "6,500.00",
            },
        )
        self.assertIn(response.status_code, (302, 200))
        self.project.refresh_from_db()
        self.assertEqual(self.project.budget_minor, 650000)
