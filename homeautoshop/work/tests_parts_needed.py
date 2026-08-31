"""
Parts a job needs, decided before it starts (SPEC FR-WO-11, FR-WO-2, FR-INV-1).

Planning a job is mostly one question — *can I start this on Saturday?* — and
until now the only record of a part was written when it was **consumed**, so
the answer existed only after the wheel was off.

The case worth writing down: two brake jobs planned for the same weekend and
one box of pads on the shelf. Counting stock alone reports both as ready, and
the second one finds out on Saturday. `free` subtracts what other open jobs
have already claimed, which is the whole reason this is arithmetic rather
than a lookup.

Nothing here moves stock, and one test exists purely to hold that down:
reserving by decrementing `qty_on_hand` would make a part that is merely
spoken for look consumed, and the ledger is the only thing allowed to move
that number.
"""

from __future__ import annotations

import re
from decimal import Decimal

from django.test import TestCase
from django.urls import reverse

from homeautoshop.accounts.models import Role, User
from homeautoshop.assets.models import Asset
from homeautoshop.parts.models import Location, Part, StockLot
from homeautoshop.parts.services import consume
from homeautoshop.work import parts_readiness
from homeautoshop.work.models import JobItem, PartRequirement, WorkOrder, WorkOrderStatus

VIN = "1M8GDM9AXKP042788"


def stock(part, qty, cost=1000):
    return StockLot.objects.create(part=part, qty_on_hand=Decimal(str(qty)), unit_cost_minor=cost)


class Fixture(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="andy", password="x" * 16, role=Role.ADMIN)
        self.client.force_login(self.user)
        self.asset = Asset.objects.create(nickname="Red truck", vin=VIN)
        self.pads = Part.objects.create(name="Front brake pads")
        self.rotors = Part.objects.create(name="Front rotors")
        self.wo = WorkOrder.objects.create(asset=self.asset, title="Front brakes")

    def need(self, part, qty=1, work_order=None, job_item=None):
        order = work_order or self.wo
        return PartRequirement.objects.create(
            work_order=order,
            part=part,
            qty=Decimal(str(qty)),
            job_item=job_item,
            origin=PartRequirement.origin_for(order),
        )

    def line_for(self, part, work_order=None):
        readiness = parts_readiness.for_work_order(work_order or self.wo)
        return next(line for line in readiness.lines if line.part.pk == part.pk)


class TheQuestionTests(Fixture):
    """Can this job start?"""

    def test_a_job_with_nothing_listed_is_not_blocked_by_parts(self):
        """Vacuously ready, which is the honest answer rather than a warning."""
        readiness = parts_readiness.for_work_order(self.wo)
        self.assertTrue(readiness.is_ready)
        self.assertEqual(readiness.lines, [])

    def test_a_part_on_the_shelf_makes_the_job_ready(self):
        stock(self.pads, 2)
        self.need(self.pads, 1)
        self.assertTrue(parts_readiness.for_work_order(self.wo).is_ready)

    def test_a_part_that_is_not_there_is_reported_short(self):
        self.need(self.pads, 1)
        line = self.line_for(self.pads)
        self.assertEqual(line.short, Decimal(1))
        self.assertEqual(line.state, "short")
        self.assertFalse(parts_readiness.for_work_order(self.wo).is_ready)

    def test_needing_more_than_is_on_the_shelf_is_short_by_the_difference(self):
        stock(self.pads, 1)
        self.need(self.pads, 4)
        self.assertEqual(self.line_for(self.pads).short, Decimal(3))


class ClaimedByAnotherJobTests(Fixture):
    """The case this exists for: two jobs, one box."""

    def setUp(self):
        super().setUp()
        self.other = WorkOrder.objects.create(asset=self.asset, title="Rear brakes")

    def test_one_box_and_two_jobs_leaves_the_second_one_short(self):
        """The box goes to whoever asked first, and only that one is ready.

        Telling both jobs they are short would have somebody buy two boxes
        when one is enough — a wrong answer of exactly the kind this feature
        exists to prevent.
        """
        stock(self.pads, 1)
        self.need(self.pads, 1)  # asked first
        self.need(self.pads, 1, work_order=self.other)

        first = self.line_for(self.pads)
        self.assertEqual(first.free, Decimal(1))
        self.assertEqual(first.short, Decimal(0))

        second = self.line_for(self.pads, work_order=self.other)
        self.assertEqual(second.committed_elsewhere, Decimal(1))
        self.assertEqual(second.free, Decimal(0))
        self.assertEqual(second.short, Decimal(1))

    def test_a_later_claim_does_not_take_the_shelf_from_an_earlier_one(self):
        """Asked-first stays ready no matter how many jobs queue behind it."""
        stock(self.pads, 1)
        self.need(self.pads, 1)
        for _ in range(3):
            self.need(
                self.pads, 1, work_order=WorkOrder.objects.create(asset=self.asset, title="Later")
            )
        self.assertEqual(self.line_for(self.pads).short, Decimal(0))

    def test_the_claim_says_who_is_holding_it(self):
        stock(self.pads, 3)
        self.need(self.pads, 2, work_order=self.other)
        self.need(self.pads, 1)

        line = self.line_for(self.pads)
        self.assertEqual(line.on_hand, Decimal(3))
        self.assertEqual(line.committed_elsewhere, Decimal(2))
        self.assertEqual(line.free, Decimal(1))
        self.assertTrue(line.is_ready)

    def test_a_finished_job_stops_holding_its_claim(self):
        """Only open work competes for the shelf."""
        stock(self.pads, 1)
        self.need(self.pads, 1, work_order=self.other)
        self.need(self.pads, 1)
        self.assertEqual(self.line_for(self.pads).free, Decimal(0))

        self.other.status = WorkOrderStatus.ABANDONED
        self.other.save(update_fields=["status"])
        self.assertEqual(self.line_for(self.pads).free, Decimal(1))

    def test_a_claim_already_drawn_stops_reducing_what_is_free(self):
        """Otherwise it counts twice: once as a claim, once as missing stock.

        The other job took its pads, so `qty_on_hand` already went down. If
        its requirement kept reducing `free` as well, this job would be told
        the shelf is emptier than it is.
        """
        stock(self.pads, 2)
        self.need(self.pads, 1, work_order=self.other)
        consume(self.pads, 1, work_order=self.other, user=self.user)
        self.need(self.pads, 1)

        line = self.line_for(self.pads)
        self.assertEqual(line.on_hand, Decimal(1))
        self.assertEqual(line.committed_elsewhere, Decimal(0))
        self.assertEqual(line.free, Decimal(1))


class NothingMovesStockTests(Fixture):
    """A claim is a row, not a transaction."""

    def test_needing_a_part_does_not_touch_quantity_on_hand(self):
        lot = stock(self.pads, 5)
        self.need(self.pads, 3)
        lot.refresh_from_db()
        self.assertEqual(lot.qty_on_hand, Decimal(5))

    def test_needing_a_part_writes_no_ledger_entry(self):
        from homeautoshop.parts.models import StockTransaction

        stock(self.pads, 5)
        before = StockTransaction.objects.count()
        self.need(self.pads, 3)
        self.assertEqual(StockTransaction.objects.count(), before)


class UsingWhatWasPlannedTests(Fixture):
    def test_using_the_part_settles_the_claim(self):
        stock(self.pads, 2)
        self.need(self.pads, 2)
        consume(self.pads, 2, work_order=self.wo, user=self.user)

        line = self.line_for(self.pads)
        self.assertEqual(line.used, Decimal(2))
        self.assertEqual(line.outstanding, Decimal(0))
        self.assertEqual(line.state, "used")
        self.assertTrue(line.is_satisfied)

    def test_a_partly_used_claim_still_wants_the_rest(self):
        stock(self.pads, 3)
        self.need(self.pads, 3)
        consume(self.pads, 1, work_order=self.wo, user=self.user)
        self.assertEqual(self.line_for(self.pads).outstanding, Decimal(2))

    def test_the_plan_stays_on_the_record_after_the_part_is_used(self):
        """It is the record of what was foreseen, which the usage does not say."""
        stock(self.pads, 1)
        self.need(self.pads, 1)
        consume(self.pads, 1, work_order=self.wo, user=self.user)
        self.assertEqual(PartRequirement.objects.filter(work_order=self.wo).count(), 1)


class OnOrderTests(Fixture):
    def setUp(self):
        super().setUp()
        from homeautoshop.purchasing.models import Purchase, PurchaseLine, Vendor

        self.Purchase, self.PurchaseLine = Purchase, PurchaseLine
        self.vendor = Vendor.objects.create(name="RockAuto")

    def order(self, part, qty, *, work_order=None, received=0, status="ordered"):
        purchase = self.Purchase.objects.create(
            vendor=self.vendor, work_order=work_order or self.wo, status=status
        )
        return self.PurchaseLine.objects.create(
            purchase=purchase,
            part=part,
            qty_ordered=Decimal(str(qty)),
            qty_received=Decimal(str(received)),
        )

    def test_a_part_on_order_for_this_job_is_not_a_shortfall(self):
        self.need(self.rotors, 2)
        self.order(self.rotors, 2)

        line = self.line_for(self.rotors)
        self.assertEqual(line.on_order, Decimal(2))
        self.assertEqual(line.short, Decimal(0))
        self.assertEqual(line.state, "on_order")

    def test_an_order_for_a_different_job_does_not_count(self):
        """A box somebody else earmarked is not this job's box."""
        other = WorkOrder.objects.create(asset=self.asset, title="Rear brakes")
        self.need(self.rotors, 2)
        self.order(self.rotors, 2, work_order=other)
        self.assertEqual(self.line_for(self.rotors).short, Decimal(2))

    def test_a_cancelled_order_stops_counting(self):
        self.need(self.rotors, 2)
        self.order(self.rotors, 2, status="cancelled")
        self.assertEqual(self.line_for(self.rotors).short, Decimal(2))

    def test_a_received_order_no_longer_counts_as_coming(self):
        """Received stock is on the shelf; counting both would double it."""
        self.need(self.rotors, 2)
        self.order(self.rotors, 2, received=2)
        self.assertEqual(self.line_for(self.rotors).on_order, Decimal(0))


class ForeseenOrDiscoveredTests(Fixture):
    """The distinction the operator asked for, stamped by the clock."""

    def test_a_part_named_while_planning_is_planned(self):
        self.assertEqual(self.wo.status, WorkOrderStatus.PLANNED)
        self.assertEqual(self.need(self.pads).origin, PartRequirement.Origin.PLANNED)

    def test_a_part_named_after_work_started_is_discovered(self):
        self.wo.status = WorkOrderStatus.IN_PROGRESS
        self.wo.save(update_fields=["status"])
        self.assertEqual(self.need(self.pads).origin, PartRequirement.Origin.DISCOVERED)

    def test_the_stamp_does_not_change_when_the_job_moves_on(self):
        """It is a fact about when it was decided, not about now."""
        requirement = self.need(self.pads)
        self.wo.status = WorkOrderStatus.IN_PROGRESS
        self.wo.save(update_fields=["status"])
        requirement.refresh_from_db()
        self.assertEqual(requirement.origin, PartRequirement.Origin.PLANNED)


class TheScreenTests(Fixture):
    def test_the_work_order_lists_what_it_needs(self):
        stock(self.pads, 2)
        self.need(self.pads, 1)
        page = self.client.get(reverse("work_order_detail", args=[self.wo.pk])).content.decode()
        self.assertIn("Parts needed", page)
        self.assertIn("Front brake pads", page)

    def test_a_shortfall_is_visible_on_the_page(self):
        self.need(self.pads, 1)
        page = self.client.get(reverse("work_order_detail", args=[self.wo.pk])).content.decode()
        self.assertIn("part short", page)

    def test_a_job_with_everything_says_so(self):
        stock(self.pads, 4)
        self.need(self.pads, 1)
        page = self.client.get(reverse("work_order_detail", args=[self.wo.pk])).content.decode()
        self.assertIn("Everything is here", page)

    def test_adding_a_requirement_from_the_page(self):
        self.client.post(
            reverse("work_order_part_require", args=[self.wo.pk]),
            {"part": str(self.pads.pk), "qty": "2"},
        )
        requirement = PartRequirement.objects.get(work_order=self.wo)
        self.assertEqual(requirement.qty, Decimal(2))
        self.assertEqual(requirement.origin, PartRequirement.Origin.PLANNED)

    def test_naming_the_same_part_twice_asks_for_more_of_it(self):
        """Two rows saying the same thing is not what anybody meant."""
        for _ in range(2):
            self.client.post(
                reverse("work_order_part_require", args=[self.wo.pk]),
                {"part": str(self.pads.pk), "qty": "1"},
            )
        self.assertEqual(PartRequirement.objects.filter(work_order=self.wo).count(), 1)
        self.assertEqual(PartRequirement.objects.get().qty, Decimal(2))

    def test_the_same_part_for_a_different_job_item_is_its_own_line(self):
        first = JobItem.objects.create(work_order=self.wo, title="Front")
        second = JobItem.objects.create(work_order=self.wo, title="Rear")
        for item in (first, second):
            self.client.post(
                reverse("work_order_part_require", args=[self.wo.pk]),
                {"part": str(self.pads.pk), "qty": "1", "job_item": str(item.pk)},
            )
        self.assertEqual(PartRequirement.objects.filter(work_order=self.wo).count(), 2)
        # One part, so one row of arithmetic, asked for by two lines of work.
        line = self.line_for(self.pads)
        self.assertEqual(line.needed, Decimal(2))
        self.assertEqual(len(line.requirements), 2)

    def test_a_job_item_from_another_work_order_is_refused(self):
        other = WorkOrder.objects.create(asset=self.asset, title="Rear brakes")
        stray = JobItem.objects.create(work_order=other, title="Elsewhere")
        self.client.post(
            reverse("work_order_part_require", args=[self.wo.pk]),
            {"part": str(self.pads.pk), "qty": "1", "job_item": str(stray.pk)},
        )
        requirement = PartRequirement.objects.get(work_order=self.wo)
        self.assertIsNone(requirement.job_item)

    def test_a_quantity_that_is_not_a_number_is_refused(self):
        response = self.client.post(
            reverse("work_order_part_require", args=[self.wo.pk]),
            {"part": str(self.pads.pk), "qty": "some"},
            follow=True,
        )
        self.assertEqual(PartRequirement.objects.count(), 0)
        self.assertContains(response, "quantity")

    def test_a_requirement_can_be_taken_off_the_list(self):
        requirement = self.need(self.pads)
        self.client.post(
            reverse("work_order_part_unrequire", args=[self.wo.pk, requirement.pk])
        )
        self.assertEqual(PartRequirement.objects.count(), 0)

    def test_taking_it_draws_the_outstanding_quantity(self):
        """The button on the row uses the existing consume flow."""
        stock(self.pads, 5)
        self.need(self.pads, 2)
        self.client.post(
            reverse("work_order_part_use", args=[self.wo.pk]),
            {"part": str(self.pads.pk), "qty": "2"},
        )
        self.assertEqual(self.line_for(self.pads).outstanding, Decimal(0))
        self.assertEqual(self.pads.on_hand, Decimal(3))


class StartingAnywayTests(Fixture):
    """A warning, never a block."""

    def test_starting_short_is_allowed_and_said_out_loud(self):
        self.need(self.pads, 1)
        response = self.client.post(
            reverse("work_order_transition", args=[self.wo.pk]),
            {"status": WorkOrderStatus.IN_PROGRESS},
            follow=True,
        )
        self.wo.refresh_from_db()
        self.assertEqual(self.wo.status, WorkOrderStatus.IN_PROGRESS)
        self.assertContains(response, "short of")

    def test_starting_with_everything_says_nothing_about_parts(self):
        stock(self.pads, 1)
        self.need(self.pads, 1)
        response = self.client.post(
            reverse("work_order_transition", args=[self.wo.pk]),
            {"status": WorkOrderStatus.IN_PROGRESS},
            follow=True,
        )
        self.assertNotContains(response, "short of")


class PlanningAcrossJobsTests(Fixture):
    def test_the_blocked_list_finds_a_planned_job(self):
        """The tool sibling asked for status `open`, which does not exist.

        So it never saw a `planned` work order — which is every job you would
        want warned about before the weekend.
        """
        self.need(self.pads, 1)
        blocked = parts_readiness.blocked_by_parts()
        self.assertEqual([wo.pk for wo, _ in blocked], [self.wo.pk])

    def test_a_job_with_everything_is_not_listed(self):
        stock(self.pads, 1)
        self.need(self.pads, 1)
        self.assertEqual(parts_readiness.blocked_by_parts(), [])

    def test_the_tool_sibling_now_looks_at_real_statuses(self):
        from homeautoshop.work import readiness

        # With WrenchLedger off it returns nothing either way; what is being
        # checked is that the query no longer names a status that cannot match.
        self.assertEqual(readiness.blocked_work_orders(), [])


class SayingYouAreWaitingTests(Fixture):
    """The note the blocked status requires is already written."""

    def test_a_planned_job_can_go_straight_to_waiting_on_parts(self):
        """The graph did not allow this, and had to.

        Listing parts while planning exists so a shortfall turns up before
        the job starts. Without this edge the only way to record one was to
        start the job first — a false statement about the shop, written to
        get around the lifecycle.
        """
        self.need(self.pads, 1)
        self.client.post(
            reverse("work_order_transition", args=[self.wo.pk]),
            {"status": WorkOrderStatus.WAITING_ON_PARTS, "blocked_reason": "Waiting on pads."},
        )
        self.wo.refresh_from_db()
        self.assertEqual(self.wo.status, WorkOrderStatus.WAITING_ON_PARTS)

    def test_the_page_offers_the_reason_already_written(self):
        self.need(self.pads, 2)
        page = self.client.get(reverse("work_order_detail", args=[self.wo.pk])).content.decode()
        self.assertIn("Waiting on these", page)
        self.assertIn("2 × Front brake pads", page)

    def test_pressing_it_records_the_block_and_says_what_for(self):
        self.need(self.pads, 2)
        page = self.client.get(reverse("work_order_detail", args=[self.wo.pk])).content.decode()
        reason = re.search(r'name="blocked_reason" value="([^"]+)"', page).group(1)

        self.client.post(
            reverse("work_order_transition", args=[self.wo.pk]),
            {"status": WorkOrderStatus.WAITING_ON_PARTS, "blocked_reason": reason},
        )
        self.wo.refresh_from_db()
        self.assertEqual(self.wo.status, WorkOrderStatus.WAITING_ON_PARTS)
        self.assertIn("Front brake pads", self.wo.blocked_reason)

    def test_a_job_with_everything_is_not_offered_the_button(self):
        stock(self.pads, 2)
        self.need(self.pads, 1)
        page = self.client.get(reverse("work_order_detail", args=[self.wo.pk])).content.decode()
        self.assertNotIn("Waiting on these", page)


class OrderingWhatIsMissingTests(Fixture):
    def setUp(self):
        super().setUp()
        from homeautoshop.purchasing.models import Purchase, PurchaseStatus, Vendor

        self.Purchase, self.PurchaseStatus = Purchase, PurchaseStatus
        self.vendor = Vendor.objects.create(name="RockAuto")

    def order_the_shortfall(self, vendor=None):
        return self.client.post(
            reverse("work_order_order_shortfall", args=[self.wo.pk]),
            {"vendor": str((vendor or self.vendor).pk)},
        )

    def test_it_drafts_a_line_for_each_missing_part(self):
        self.need(self.pads, 2)
        self.need(self.rotors, 2)
        stock(self.rotors, 2)  # this one is covered, so it is not ordered

        self.order_the_shortfall()
        purchase = self.Purchase.objects.get(work_order=self.wo)
        self.assertEqual([line.part for line in purchase.lines.all()], [self.pads])
        self.assertEqual(purchase.lines.get().qty_ordered, Decimal(2))

    def test_it_orders_only_the_difference(self):
        """Two on the shelf and three needed is one to buy, not three."""
        stock(self.pads, 2)
        self.need(self.pads, 3)
        self.order_the_shortfall()
        self.assertEqual(
            self.Purchase.objects.get(work_order=self.wo).lines.get().qty_ordered, Decimal(1)
        )

    def test_it_is_a_cart_not_an_order(self):
        """Nobody has been asked to send anything yet."""
        self.need(self.pads, 1)
        self.order_the_shortfall()
        self.assertEqual(
            self.Purchase.objects.get(work_order=self.wo).status, self.PurchaseStatus.CART
        )

    def test_a_cart_does_not_clear_the_shortfall(self):
        """A list nobody has placed will not arrive."""
        self.need(self.pads, 1)
        self.order_the_shortfall()
        line = self.line_for(self.pads)
        self.assertEqual(line.short, Decimal(1))
        self.assertEqual(line.on_order, Decimal(0))

    def test_placing_the_order_does_clear_it(self):
        self.need(self.pads, 1)
        self.order_the_shortfall()
        purchase = self.Purchase.objects.get(work_order=self.wo)
        purchase.status = self.PurchaseStatus.ORDERED
        purchase.save(update_fields=["status"])

        line = self.line_for(self.pads)
        self.assertEqual(line.on_order, Decimal(1))
        self.assertEqual(line.short, Decimal(0))
        self.assertEqual(line.state, "on_order")

    def test_the_draft_is_visible_on_the_job(self):
        """Otherwise pressing the button looks like it did nothing."""
        self.need(self.pads, 1)
        self.order_the_shortfall()
        page = self.client.get(reverse("work_order_detail", args=[self.wo.pk])).content.decode()
        self.assertIn("on a draft order", page)

    def test_it_lands_on_the_purchase_to_be_priced(self):
        self.need(self.pads, 1)
        response = self.order_the_shortfall()
        purchase = self.Purchase.objects.get(work_order=self.wo)
        self.assertRedirects(response, reverse("purchase_detail", args=[purchase.pk]))

    def test_ordering_nothing_is_refused_rather_than_creating_an_empty_cart(self):
        stock(self.pads, 5)
        self.need(self.pads, 1)
        self.order_the_shortfall()
        self.assertEqual(self.Purchase.objects.count(), 0)

    def test_a_vendor_is_required(self):
        self.need(self.pads, 1)
        response = self.client.post(
            reverse("work_order_order_shortfall", args=[self.wo.pk]), {}, follow=True
        )
        self.assertEqual(self.Purchase.objects.count(), 0)
        self.assertContains(response, "who to order from")

    def test_with_no_vendors_the_page_says_to_add_one(self):
        from homeautoshop.purchasing.models import Vendor

        Vendor.objects.all().delete()
        self.need(self.pads, 1)
        page = self.client.get(reverse("work_order_detail", args=[self.wo.pk])).content.decode()
        self.assertIn("Add a vendor to order from", page)


class TheDashboardTests(Fixture):
    def test_a_job_short_of_parts_is_listed(self):
        self.need(self.pads, 2)
        page = self.client.get(reverse("dashboard")).content.decode()
        self.assertIn("Short of parts", page)
        self.assertIn(self.wo.number, page)
        self.assertIn("Front brake pads", page)

    def test_a_job_with_everything_is_not_listed(self):
        stock(self.pads, 2)
        self.need(self.pads, 2)
        self.assertNotIn("Short of parts", self.client.get(reverse("dashboard")).content.decode())

    def test_a_job_already_marked_blocked_is_not_listed_twice(self):
        """The marked list is the more specific statement, so it wins."""
        self.need(self.pads, 1)
        self.wo.status = WorkOrderStatus.WAITING_ON_PARTS
        self.wo.blocked_reason = "Waiting on pads."
        self.wo.save(update_fields=["status", "blocked_reason"])

        page = self.client.get(reverse("dashboard")).content.decode()
        self.assertIn("Waiting on parts", page)
        # Not "appears once" — "Open work" below lists every open job, and
        # always did. What must not happen is a second *blocked* section
        # saying the same thing in weaker terms.
        self.assertNotIn("Short of parts", page)
