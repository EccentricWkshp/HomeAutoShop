"""Parts, FIFO stock, purchasing, and cost rollups (SPEC §7.4–§7.6)."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from homeautoshop.assets.models import Asset
from homeautoshop.core import costs
from homeautoshop.purchasing.models import (
    Expense, Purchase, PurchaseLine, PurchaseStatus, Vendor,
)
from homeautoshop.work.models import JobItem, TimeEntry, WorkOrder

from .models import Location, Part, PartCrossRef, PartFitment, PartUsage, StockLot, StockTransaction
from .services import (
    InsufficientStock,
    consume,
    cycle_count,
    expiring_lots,
    find,
    fits,
    outstanding_cores,
    restock_list,
)


def lot(part, qty, unit_cost_minor, *, acquired=None, **kwargs) -> StockLot:
    row = StockLot.objects.create(
        part=part,
        qty_on_hand=0,
        unit_cost_minor=unit_cost_minor,
        acquired_on=acquired or timezone.localdate(),
        **kwargs,
    )
    StockTransaction.record(row, qty, StockTransaction.Reason.RECEIVE)
    return row


class StockLedgerTests(TestCase):
    """FR-INV-1 — quantity on hand is a projection, never a typed number."""

    def setUp(self):
        self.part = Part.objects.create(name="Oil filter", part_number="PH3593A")

    def test_quantity_follows_the_ledger(self):
        row = lot(self.part, 10, 899)
        self.assertEqual(row.qty_on_hand, 10)
        StockTransaction.record(row, -3, StockTransaction.Reason.CONSUME)
        row.refresh_from_db()
        self.assertEqual(row.qty_on_hand, 7)
        self.assertEqual(row.transactions.count(), 2)

    def test_adjustment_without_a_reason_is_refused(self):
        """FR-INV-7 — silent corrections hide problems."""
        row = lot(self.part, 5, 899)
        with self.assertRaises(ValidationError):
            StockTransaction.record(row, -1, StockTransaction.Reason.ADJUST)

    def test_cycle_count_writes_an_adjustment_rather_than_overwriting(self):
        row = lot(self.part, 5, 899)
        entry = cycle_count(row, 4, note="One missing from the bin")
        row.refresh_from_db()
        self.assertEqual(row.qty_on_hand, 4)
        self.assertEqual(entry.reason, StockTransaction.Reason.ADJUST)
        self.assertEqual(entry.delta, Decimal("-1"))

    def test_a_matching_count_writes_nothing(self):
        row = lot(self.part, 5, 899)
        self.assertIsNone(cycle_count(row, 5, note="counted"))


class FifoTests(TestCase):
    """FR-INV-5 — consumption uses the oldest lot's actual cost."""

    def setUp(self):
        self.part = Part.objects.create(name="Brake pads")
        self.asset = Asset.objects.create(nickname="Truck", make="Ford", model="F-150", year=2004)
        self.wo = WorkOrder.objects.create(asset=self.asset, title="Front brakes")
        self.old = lot(self.part, 2, 4000, acquired=date(2025, 1, 1))
        self.new = lot(self.part, 2, 6000, acquired=date(2026, 1, 1))

    def test_draw_takes_the_oldest_lot_first(self):
        result = consume(self.part, 1, work_order=self.wo)
        self.old.refresh_from_db()
        self.new.refresh_from_db()
        self.assertEqual(self.old.qty_on_hand, 1)
        self.assertEqual(self.new.qty_on_hand, 2)
        self.assertEqual(result.total_minor, 4000)

    def test_a_draw_spanning_two_prices_costs_honestly(self):
        # Three units: two at $40, one at $60 — not three at a blended $50.
        result = consume(self.part, 3, work_order=self.wo)
        self.assertEqual(result.total_minor, 4000 * 2 + 6000)
        self.assertEqual(len(result.usages), 2)

    def test_insufficient_stock_is_refused_by_default(self):
        with self.assertRaises(InsufficientStock):
            consume(self.part, 99, work_order=self.wo)
        self.old.refresh_from_db()
        self.assertEqual(self.old.qty_on_hand, 2)  # nothing was taken

    def test_shortfall_may_be_recorded_as_bought_for_the_job(self):
        result = consume(self.part, 6, work_order=self.wo, allow_short=True)
        self.assertEqual(result.shortfall, Decimal(2))
        self.assertTrue(any(u.source == PartUsage.Source.PURCHASED for u in result.usages))

    def test_installing_a_part_records_confirmed_fitment(self):
        """FR-PART-3 — the shop's own history is its fitment database."""
        consume(self.part, 1, work_order=self.wo)
        fitment = PartFitment.objects.get(part=self.part, asset=self.asset)
        self.assertEqual(fitment.confidence, PartFitment.Confidence.CONFIRMED)

    def test_confirmed_fitment_outranks_a_vendor_claim(self):
        PartFitment.objects.create(
            part=self.part, make="Ford", model="F-150", year_from=2000, year_to=2010,
            confidence=PartFitment.Confidence.VENDOR,
        )
        other = Part.objects.create(name="Rotor")
        PartFitment.objects.create(
            part=other, make="Ford", model="F-150", year_from=2000, year_to=2010,
            confidence=PartFitment.Confidence.VENDOR,
        )
        consume(self.part, 1, work_order=self.wo)
        self.assertEqual(fits(self.asset)[0], self.part)

    def test_a_part_you_tried_and_could_not_fit_is_not_offered_again(self):
        """The report: a vendor said it fits, the bench said otherwise.

        Not merely ranked below the others — gone. Being offered a part you
        have already held up against the car and rejected is how it gets
        ordered a second time, which is the cost this state exists to avoid.
        """
        PartFitment.objects.create(
            part=self.part, make="Ford", model="F-150", year_from=2000, year_to=2010,
            confidence=PartFitment.Confidence.VENDOR,
        )
        self.assertIn(self.part, fits(self.asset))

        PartFitment.objects.create(
            part=self.part, asset=self.asset,
            confidence=PartFitment.Confidence.DOES_NOT_FIT,
        )
        self.assertNotIn(self.part, fits(self.asset))

    def test_one_disproved_fitment_does_not_hide_the_part_from_other_vehicles(self):
        """It did not fit *that* car. Everything else is unaffected."""
        other = Asset.objects.create(nickname="Van", make="Ford", model="F-150", year=2005)
        PartFitment.objects.create(
            part=self.part, make="Ford", model="F-150", year_from=2000, year_to=2010,
            confidence=PartFitment.Confidence.VENDOR,
        )
        PartFitment.objects.create(
            part=self.part, asset=self.asset,
            confidence=PartFitment.Confidence.DOES_NOT_FIT,
        )
        self.assertNotIn(self.part, fits(self.asset))
        self.assertIn(self.part, fits(other))


class FitmentScreenTests(TestCase):
    """Reading, correcting and removing a fitment (FR-PART-3/4).

    Reported together, because they are one complaint: the card said something
    that read as nonsense and there was no way to argue with it.
    """

    def setUp(self):
        from homeautoshop.accounts.models import Role, User

        self.user = User.objects.create_user(
            username="andy", password="x" * 16, role=Role.ADMIN
        )
        self.client.force_login(self.user)
        self.asset = Asset.objects.create(
            nickname="Aero", make="Suzuki", model="Aerio", year=2004
        )
        self.part = Part.objects.create(
            name="A/C Compressor & Component Kit", manufacturer="GPD",
            part_number="9642644B",
        )
        self.fitment = PartFitment.objects.create(
            part=self.part, make="Suzuki", model="Aerio", year_from=2004, year_to=2004,
            confidence=PartFitment.Confidence.VENDOR,
        )

    def page(self) -> str:
        return self.client.get(
            reverse("part_detail", args=[self.part.pk])
        ).content.decode()

    def test_the_card_names_the_vehicle_and_not_the_part_again(self):
        """The bug as reported: "this part fits this part plus a vehicle"."""
        page = self.page()
        self.assertIn("Suzuki Aerio 2004", page)
        self.assertNotIn("fits Suzuki", page)
        # The part's name belongs to the heading, not to every row beneath it.
        self.assertEqual(page.count("A/C Compressor &amp; Component Kit 9642644B"), 0)

    def test_a_year_range_of_one_is_written_as_one_year(self):
        self.assertEqual(self.fitment.vehicle, "Suzuki Aerio 2004")

    def test_a_fitment_against_one_of_your_vehicles_names_that_vehicle(self):
        mine = PartFitment.objects.create(
            part=self.part, asset=self.asset,
            confidence=PartFitment.Confidence.CONFIRMED,
        )
        self.assertEqual(mine.vehicle, str(self.asset))

    def test_a_fitment_can_be_corrected(self):
        response = self.client.post(
            reverse("fitment_edit", args=[self.part.pk, self.fitment.pk]),
            {
                "make": "Suzuki", "model": "Aerio",
                "year_from": 2004, "year_to": 2007,
                "confidence": PartFitment.Confidence.VENDOR,
                "engine_code": "", "position": "", "notes": "",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.fitment.refresh_from_db()
        self.assertEqual(self.fitment.year_to, 2007)

    def test_a_fitment_can_be_marked_as_one_that_did_not_fit(self):
        """The case that prompted this: discovered on the bench, not on paper."""
        self.client.post(
            reverse("fitment_edit", args=[self.part.pk, self.fitment.pk]),
            {
                "make": "Suzuki", "model": "Aerio",
                "year_from": 2004, "year_to": 2004,
                "confidence": PartFitment.Confidence.DOES_NOT_FIT,
                "engine_code": "", "position": "",
                "notes": "Pulley offset is wrong.",
            },
        )
        self.fitment.refresh_from_db()
        self.assertEqual(self.fitment.confidence, PartFitment.Confidence.DOES_NOT_FIT)
        self.assertIn("Pulley offset is wrong.", self.page())

    def test_a_fitment_can_be_removed(self):
        self.client.post(reverse("fitment_delete", args=[self.part.pk, self.fitment.pk]))
        self.assertFalse(PartFitment.objects.filter(pk=self.fitment.pk).exists())
        # Soft, like everything else here.
        self.assertTrue(PartFitment.all_objects.filter(pk=self.fitment.pk).exists())

    def test_a_fitment_may_be_added_by_hand(self):
        self.client.post(
            reverse("fitment_add", args=[self.part.pk]),
            {
                "asset": str(self.asset.pk),
                "make": "", "model": "", "year_from": "", "year_to": "",
                "confidence": PartFitment.Confidence.CONFIRMED,
                "engine_code": "", "position": "", "notes": "",
            },
        )
        self.assertTrue(self.part.fitments.filter(asset=self.asset).exists())

    def test_a_fitment_that_names_no_vehicle_is_refused(self):
        """One with no vehicle in it would read as fitting everything."""
        response = self.client.post(
            reverse("fitment_add", args=[self.part.pk]),
            {
                "asset": "", "make": "", "model": "",
                "year_from": "", "year_to": "",
                "confidence": PartFitment.Confidence.UNVERIFIED,
                "engine_code": "", "position": "", "notes": "",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.part.fitments.count(), 1)

    def test_years_the_wrong_way_round_are_refused(self):
        response = self.client.post(
            reverse("fitment_add", args=[self.part.pk]),
            {
                "asset": "", "make": "Suzuki", "model": "Aerio",
                "year_from": 2010, "year_to": 2004,
                "confidence": PartFitment.Confidence.UNVERIFIED,
                "engine_code": "", "position": "", "notes": "",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.part.fitments.count(), 1)


class PartLookupTests(TestCase):
    """FR-PART-1/2 — one search box, every identifier."""

    def setUp(self):
        self.part = Part.objects.create(name="Water pump", manufacturer="Aisin", part_number="WPT-001")
        PartCrossRef.objects.create(part=self.part, system=PartCrossRef.System.OEM, value="16100-09071")
        PartCrossRef.objects.create(part=self.part, system=PartCrossRef.System.UPC, value="012345678905")

    def test_found_by_any_number(self):
        for query in ("Water pump", "Aisin", "WPT-001", "16100-09071", "012345678905"):
            self.assertIn(self.part, find(query), f"not found by {query!r}")

    def test_short_queries_return_nothing(self):
        self.assertEqual(find("a"), [])


class LocationTests(TestCase):
    def test_path_reads_like_the_physical_shop(self):
        garage = Location.objects.create(name="Garage")
        cabinet = Location.objects.create(name="Red cabinet", parent=garage)
        drawer = Location.objects.create(name="Drawer 2", parent=cabinet)
        self.assertEqual(drawer.path, "Garage / Red cabinet / Drawer 2")

    def test_a_location_cannot_contain_itself(self):
        node = Location.objects.create(name="Shelf")
        node.parent = node
        with self.assertRaises(ValidationError):
            node.full_clean()


class ReceivingTests(TestCase):
    """FR-PUR-2/3 — receiving creates stock at landed cost."""

    def setUp(self):
        self.vendor = Vendor.objects.create(name="RockAuto", return_window_days=30)
        self.part = Part.objects.create(name="Alternator", has_core=True, core_value_minor=4500)
        self.purchase = Purchase.objects.create(
            vendor=self.vendor, shipping_minor=1000, tax_minor=0
        )
        self.line = PurchaseLine.objects.create(
            purchase=self.purchase, part=self.part, qty_ordered=2, unit_price_minor=10000
        )

    def test_receiving_creates_stock(self):
        self.line.receive()
        self.line.refresh_from_db()
        self.assertEqual(self.line.qty_received, 2)
        self.assertEqual(self.part.on_hand, 2)

    def test_shipping_is_spread_across_the_line(self):
        """A $4 gasket that shipped in a $30 box did not cost $4."""
        stock = self.line.receive()
        # $100 each + $10 shipping over 2 units = $105 each.
        self.assertEqual(stock.unit_cost_minor, 10500)

    def test_partial_receiving_tracks_the_remainder(self):
        self.line.receive(1)
        self.line.refresh_from_db()
        self.purchase.refresh_from_db()
        self.assertEqual(self.line.outstanding, Decimal(1))
        self.assertEqual(self.purchase.status, "partial")

        self.line.receive(1)
        self.purchase.refresh_from_db()
        self.assertEqual(self.purchase.status, "received")

    def test_over_receiving_is_refused(self):
        with self.assertRaises(ValidationError):
            self.line.receive(5)

    def test_a_line_with_no_part_cannot_be_received(self):
        orphan = PurchaseLine.objects.create(
            purchase=self.purchase, description_as_ordered="Misc hardware", qty_ordered=1
        )
        with self.assertRaises(ValidationError):
            orphan.receive()

    def test_return_window_runs_from_receipt(self):
        """FR-PUR-5 — not from the order date."""
        self.line.receive()
        self.purchase.refresh_from_db()
        self.assertEqual(self.purchase.return_by, timezone.localdate() + timedelta(days=30))

    def test_totals_include_tax_shipping_and_discount(self):
        self.purchase.tax_minor = 800
        self.purchase.discount_minor = 500
        self.purchase.save()
        self.assertEqual(self.purchase.subtotal_minor, 20000)
        self.assertEqual(self.purchase.total_minor, 20000 + 800 + 1000 - 500)


class UnreceivingTests(TestCase):
    """Taking back a receipt recorded by mistake (FR-PUR-2, FR-INV-1).

    Receiving is one tap on a screen where every line has one, and the quantity
    is filled in for you — so it is easy to do to the wrong line, and until now
    it was a one-way door.
    """

    def setUp(self):
        self.part = Part.objects.create(name="Filter", part_number="PH1")
        self.asset = Asset.objects.create(nickname="Truck")
        self.vendor = Vendor.objects.create(name="RockAuto")
        self.purchase = Purchase.objects.create(vendor=self.vendor)
        self.line = PurchaseLine.objects.create(
            purchase=self.purchase, part=self.part, qty_ordered=4, unit_price_minor=1000
        )

    def test_the_stock_goes_back_out(self):
        self.line.receive(4)
        self.assertEqual(self.part.on_hand, Decimal(4))

        self.line.unreceive()

        self.assertEqual(self.part.on_hand, Decimal(0))
        self.line.refresh_from_db()
        self.assertEqual(self.line.qty_received, Decimal(0))

    def test_the_receipt_is_not_erased_but_answered(self):
        """FR-INV-1 — `qty_on_hand` is a projection of an append-only ledger.

        A shelf that disagrees with the book has to be explainable, and an
        erased row explains nothing.
        """
        self.line.receive(4)
        self.line.unreceive()

        reasons = list(
            StockTransaction.objects.filter(stock_lot__part=self.part)
            .order_by("created_at")
            .values_list("reason", flat=True)
        )
        self.assertEqual(
            reasons,
            [StockTransaction.Reason.RECEIVE, StockTransaction.Reason.UNRECEIVE],
        )

    def test_a_correction_is_not_filed_as_a_cycle_count(self):
        """Two different facts. Only one of them is about the shelf."""
        self.line.receive(1)
        self.line.unreceive()
        self.assertFalse(
            StockTransaction.objects.filter(reason=StockTransaction.Reason.ADJUST).exists()
        )

    def test_part_of_a_receipt_may_be_taken_back(self):
        self.line.receive(4)
        self.line.unreceive(1)
        self.line.refresh_from_db()
        self.assertEqual(self.line.qty_received, Decimal(3))
        self.assertEqual(self.part.on_hand, Decimal(3))

    def test_stock_already_used_on_a_job_is_refused(self):
        """The parts are in a car. Only the paperwork is still in question."""
        self.line.receive(4)
        wo = WorkOrder.objects.create(asset=self.asset, title="Service")
        consume(self.part, 3, work_order=wo)

        with self.assertRaises(ValidationError):
            self.line.unreceive()

        self.line.refresh_from_db()
        self.assertEqual(self.line.qty_received, Decimal(4), "the line was changed anyway")
        self.assertEqual(self.part.on_hand, Decimal(1), "stock went negative")

    def test_what_is_still_on_the_shelf_can_still_be_taken_back(self):
        self.line.receive(4)
        wo = WorkOrder.objects.create(asset=self.asset, title="Service")
        consume(self.part, 3, work_order=wo)

        self.line.unreceive(1)

        self.assertEqual(self.part.on_hand, Decimal(0))

    def test_more_than_was_received_is_refused(self):
        self.line.receive(1)
        with self.assertRaises(ValidationError):
            self.line.unreceive(2)

    def test_the_purchase_stops_claiming_to_be_received(self):
        """The status only ever ratcheted upward, so this had nowhere to go."""
        self.line.receive(4)
        self.purchase.refresh_from_db()
        self.assertEqual(self.purchase.status, PurchaseStatus.RECEIVED)
        self.assertIsNotNone(self.purchase.received_on)

        self.line.unreceive()

        self.purchase.refresh_from_db()
        self.assertEqual(self.purchase.status, PurchaseStatus.ORDERED)
        self.assertIsNone(
            self.purchase.received_on,
            "a return window was counting down from a date that no longer means anything",
        )

    def test_taking_back_part_of_it_leaves_the_purchase_partial(self):
        self.line.receive(4)
        self.line.unreceive(1)
        self.purchase.refresh_from_db()
        self.assertEqual(self.purchase.status, PurchaseStatus.PARTIAL)


class CoreTrackingTests(TestCase):
    """FR-PUR-4 — uncollected core charges are the money most often lost."""

    def test_outstanding_cores_are_listed_until_returned(self):
        part = Part.objects.create(name="Alternator", has_core=True, core_value_minor=4500)
        asset = Asset.objects.create(nickname="Truck")
        wo = WorkOrder.objects.create(asset=asset, title="Charging fault")
        lot(part, 1, 12000)
        consume(part, 1, work_order=wo)

        usage = PartUsage.objects.get()
        self.assertTrue(usage.owes_core)
        self.assertIn(usage, outstanding_cores())

        usage.core_returned = True
        usage.core_returned_on = timezone.localdate()
        usage.save()
        self.assertFalse(usage.owes_core)
        self.assertNotIn(usage, outstanding_cores())


class ShelfAlertTests(TestCase):
    def test_low_stock_appears_on_the_restock_list(self):
        part = Part.objects.create(name="Oil filter", min_quantity=4)
        lot(part, 2, 800)
        self.assertIn(part, restock_list())

    def test_stocked_parts_do_not(self):
        part = Part.objects.create(name="Oil filter", min_quantity=1)
        lot(part, 6, 800)
        self.assertNotIn(part, restock_list())

    def test_expiring_consumables_are_surfaced(self):
        """FR-INV-6 — brake fluid and sealants do expire."""
        fluid = Part.objects.create(name="DOT 4 brake fluid", is_consumable=True)
        lot(fluid, 2, 1200, expires_on=timezone.localdate() + timedelta(days=15))
        self.assertEqual(len(expiring_lots()), 1)


class WarrantyTests(TestCase):
    def test_a_live_warranty_is_surfaced(self):
        part = Part.objects.create(name="Alternator")
        asset = Asset.objects.create(nickname="Truck")
        wo = WorkOrder.objects.create(asset=asset, title="Charging")
        usage = PartUsage.objects.create(
            work_order=wo, part=part, qty=1, unit_cost_minor=12000, warranty_months=24
        )
        self.assertTrue(usage.under_warranty)
        self.assertIn(usage, costs.active_warranties())

    def test_an_expired_warranty_is_not(self):
        part = Part.objects.create(name="Alternator")
        asset = Asset.objects.create(nickname="Truck")
        wo = WorkOrder.objects.create(asset=asset, title="Charging")
        usage = PartUsage.objects.create(
            work_order=wo, part=part, qty=1, warranty_months=12,
            installed_at=timezone.localdate() - timedelta(days=800),
        )
        self.assertFalse(usage.under_warranty)


class CostRollupTests(TestCase):
    """FR-COST-1..3, and OQ-4 on tooling."""

    def setUp(self):
        self.asset = Asset.objects.create(nickname="Truck", meter_unit="mi")
        self.wo = WorkOrder.objects.create(asset=self.asset, title="Front brakes")
        self.part = Part.objects.create(name="Brake pads")
        lot(self.part, 4, 4500)
        consume(self.part, 2, work_order=self.wo)

    def test_work_order_cost_itemises_parts_and_expenses(self):
        Expense.objects.create(
            work_order=self.wo, category="machine_work", amount_minor=6000, description="Turn rotors"
        )
        rollup = costs.work_order_cost(self.wo)
        self.assertEqual(rollup.total_minor, 9000 + 6000)
        self.assertEqual(len(rollup.lines), 2)

    def test_expense_on_a_work_order_inherits_the_asset(self):
        expense = Expense.objects.create(work_order=self.wo, category="towing", amount_minor=8000)
        self.assertEqual(expense.asset_id, self.asset.pk)

    def test_tooling_is_excluded_from_asset_cost_by_default(self):
        Expense.objects.create(asset=self.asset, category="tooling", amount_minor=25000)
        self.assertEqual(costs.asset_cost(self.asset).total_minor, 9000)

    @override_settings(COST_INCLUDE_TOOLING=True)
    def test_tooling_is_included_when_the_operator_opts_in(self):
        Expense.objects.create(asset=self.asset, category="tooling", amount_minor=25000)
        self.assertEqual(costs.asset_cost(self.asset).total_minor, 9000 + 25000)

    def test_tooling_is_still_recorded_even_when_excluded(self):
        """OQ-4 — tracked and exportable, just not charged to the vehicle."""
        Expense.objects.create(asset=self.asset, category="tooling", amount_minor=25000)
        self.assertEqual(Expense.objects.filter(category="tooling").count(), 1)

    @override_settings(LABOR_RATE_MINOR=0)
    def test_time_is_not_valued_without_a_rate(self):
        TimeEntry.objects.create(work_order=self.wo, minutes=120)
        rollup = costs.work_order_cost(self.wo)
        self.assertEqual(rollup.labour_minutes, 120)
        self.assertEqual(rollup.total_minor, 9000)

    @override_settings(LABOR_RATE_MINOR=5000)
    def test_time_is_valued_as_an_estimate_when_a_rate_is_set(self):
        TimeEntry.objects.create(work_order=self.wo, minutes=120)
        rollup = costs.work_order_cost(self.wo)
        self.assertEqual(rollup.total_minor, 9000 + 10000)
        self.assertTrue(rollup.labour_is_estimate)

    def test_cost_per_distance_states_its_basis(self):
        from homeautoshop.assets.services import record_reading

        record_reading(self.asset, 100_000, read_on=date(2025, 1, 1))
        record_reading(self.asset, 110_000, read_on=date(2026, 1, 1))
        per = costs.cost_per_distance(self.asset)
        self.assertTrue(per.is_computable)
        self.assertEqual(per.distance, Decimal(10_000))
        self.assertIn("10,000", per.basis)

    def test_cost_per_distance_says_so_when_it_cannot_be_computed(self):
        per = costs.cost_per_distance(self.asset)
        self.assertFalse(per.is_computable)
        self.assertIn("Not enough", per.basis)

    def test_inventory_value_uses_actual_lot_cost(self):
        self.assertEqual(costs.inventory_value().amount, 2 * 4500)


class TimeEntryTests(TestCase):
    def setUp(self):
        self.asset = Asset.objects.create(nickname="Truck")
        self.wo = WorkOrder.objects.create(asset=self.asset, title="Brakes")

    def test_a_timer_derives_minutes_from_start_and_end(self):
        start = timezone.now()
        entry = TimeEntry.objects.create(
            work_order=self.wo, started_at=start, ended_at=start + timedelta(minutes=95)
        )
        self.assertEqual(entry.minutes, 95)
        self.assertEqual(entry.hours, 1.58)

    def test_manual_entry_needs_no_timestamps(self):
        entry = TimeEntry.objects.create(work_order=self.wo, minutes=45)
        self.assertEqual(entry.hours, 0.75)

    def test_time_entries_are_append_only(self):
        entry = TimeEntry.objects.create(work_order=self.wo, minutes=45)
        entry.minutes = 500
        with self.assertRaises(ValidationError):
            entry.save()
