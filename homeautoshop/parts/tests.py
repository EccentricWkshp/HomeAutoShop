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

from .models import (
    Location, Part, PartCrossRef, PartFitment, PartKitItem, PartUsage, StockLot,
    StockTransaction,
)
from .services import (
    InsufficientStock,
    close_kit,
    consume,
    cycle_count,
    expiring_lots,
    find,
    fits,
    open_kit,
    outstanding_cores,
    restock_list,
    split_kit_cost,
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


class KitTests(TestCase):
    """A kit holds the stock until it is opened (FR-INV-9).

    Reported as: the kit shows on hand and the parts inside it show zero, which
    is misleading and leads to ordering a duplicate. Two answers, and both are
    needed — the components stay *findable* while boxed, and the box can be
    turned into them for real.
    """

    def setUp(self):
        self.kit = Part.objects.create(name="A/C Compressor & Component Kit", part_number="964")
        self.compressor = Part.objects.create(name="A/C Compressor")
        self.drier = Part.objects.create(name="Receiver Drier")
        self.orings = Part.objects.create(name="O-Rings")
        self.asset = Asset.objects.create(nickname="Aero")

    def stock_the_kit(self, qty=1, unit_cost_minor=43654) -> StockLot:
        return lot(self.kit, qty, unit_cost_minor)

    def fill(self, **prices) -> None:
        """Put the three parts in the box, each priced in minor units."""
        for part, price in (
            (self.compressor, prices.get("compressor", 1)),
            (self.drier, prices.get("drier", 1)),
            (self.orings, prices.get("orings", 1)),
        ):
            PartKitItem.objects.create(kit=self.kit, part=part, value_minor=price)

    # -- findable while still boxed ---------------------------------------

    def test_a_boxed_component_is_findable_from_its_own_page(self):
        """The complaint, stated as a test."""
        self.fill()
        self.stock_the_kit()

        found = self.drier.available_in_kits()

        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["kit"], self.kit)
        self.assertEqual(found[0]["quantity"], Decimal(1))

    def test_a_kit_you_do_not_have_is_not_offered_as_one_you_do(self):
        self.fill()
        self.assertEqual(self.drier.available_in_kits(), [])

    def test_being_in_a_box_is_not_being_in_stock(self):
        """It is not on a shelf. Counting it as stock would be the same lie."""
        self.fill()
        self.stock_the_kit()
        self.assertEqual(self.drier.on_hand, Decimal(0))

    # -- opening ----------------------------------------------------------

    def test_opening_takes_the_box_off_the_shelf_and_puts_the_parts_on_it(self):
        self.fill()
        kit_lot = self.stock_the_kit()

        open_kit(kit_lot, 1)

        kit_lot.refresh_from_db()
        self.assertEqual(kit_lot.qty_on_hand, Decimal(0))
        self.assertEqual(self.compressor.on_hand, Decimal(1))
        self.assertEqual(self.drier.on_hand, Decimal(1))
        self.assertEqual(self.orings.on_hand, Decimal(1))

    def test_both_halves_are_one_event_in_the_ledger(self):
        self.fill()
        open_kit(self.stock_the_kit(), 1)

        moves = StockTransaction.objects.filter(
            reason=StockTransaction.Reason.KIT_OPENED
        )
        self.assertEqual(moves.filter(delta__lt=0).count(), 1, "the kit did not leave")
        self.assertEqual(moves.filter(delta__gt=0).count(), 3, "the contents did not arrive")

    def test_the_kits_cost_is_split_across_its_contents_to_the_cent(self):
        """Money is neither invented nor destroyed by opening a box."""
        self.fill()
        open_kit(self.stock_the_kit(unit_cost_minor=43654), 1)

        released = StockLot.objects.filter(from_kit_lot__isnull=False)
        self.assertEqual(released.count(), 3)
        self.assertEqual(sum(l.unit_cost_minor for l in released), 43654)

    def test_prices_decide_where_the_money_lands(self):
        self.fill(compressor=98, drier=1, orings=1)
        open_kit(self.stock_the_kit(unit_cost_minor=10000), 1)

        costs = {
            row.part_id: row.unit_cost_minor
            for row in StockLot.objects.filter(from_kit_lot__isnull=False)
        }
        self.assertEqual(costs[self.compressor.pk], 9800)
        self.assertEqual(costs[self.drier.pk], 100)

    def test_a_price_left_blank_comes_from_the_part(self):
        """The whole point of the change: enter the cost once, on the part, and
        putting it in a kit needs no number at all."""
        self.compressor.typical_cost_minor = 17526
        self.compressor.save(update_fields=["typical_cost_minor"])
        self.drier.typical_cost_minor = 846
        self.drier.save(update_fields=["typical_cost_minor"])
        self.orings.typical_cost_minor = 1628
        self.orings.save(update_fields=["typical_cost_minor"])
        for part in (self.compressor, self.drier, self.orings):
            PartKitItem.objects.create(kit=self.kit, part=part)

        # $250 landed against $200 of list prices, which is the real shape of
        # it: the kit's cost carries tax and shipping the parts' prices do not,
        # so what the split uses is the ratio.
        open_kit(self.stock_the_kit(unit_cost_minor=25000), 1)

        costs = {
            row.part_id: row.unit_cost_minor
            for row in StockLot.objects.filter(from_kit_lot__isnull=False)
        }
        self.assertEqual(costs[self.compressor.pk], 21908)
        self.assertEqual(costs[self.drier.pk], 1057)
        self.assertEqual(costs[self.orings.pk], 2035)
        self.assertEqual(sum(costs.values()), 25000)

    def test_a_price_on_the_kit_row_beats_the_part_price(self):
        """The vendor's price for this box is about this box."""
        self.compressor.typical_cost_minor = 50000
        self.compressor.save(update_fields=["typical_cost_minor"])
        PartKitItem.objects.create(kit=self.kit, part=self.compressor, value_minor=100)
        PartKitItem.objects.create(kit=self.kit, part=self.drier, value_minor=100)

        open_kit(self.stock_the_kit(unit_cost_minor=10000), 1)

        costs = {
            row.part_id: row.unit_cost_minor
            for row in StockLot.objects.filter(from_kit_lot__isnull=False)
        }
        self.assertEqual(costs[self.compressor.pk], 5000)

    def test_a_part_with_no_stated_price_falls_back_to_what_one_cost(self):
        """A price nobody typed but the shelf already knows."""
        lot(self.compressor, 1, 4200)
        self.assertEqual(self.compressor.known_cost_minor, 4200)

    def test_six_of_something_cheap_weigh_six_times_as_much(self):
        """The weight this replaced ignored quantity, so six dollar O-rings
        counted for one dollar against the compressor."""
        PartKitItem.objects.create(
            kit=self.kit, part=self.orings, quantity=6, value_minor=100
        )
        PartKitItem.objects.create(
            kit=self.kit, part=self.compressor, quantity=1, value_minor=400
        )

        open_kit(self.stock_the_kit(unit_cost_minor=1000), 1)

        costs = {
            row.part_id: row.unit_cost_minor
            for row in StockLot.objects.filter(from_kit_lot__isnull=False)
        }
        # $6 of O-rings against $4 of compressor, and the O-ring lot holds six.
        self.assertEqual(costs[self.compressor.pk], 400)
        self.assertEqual(costs[self.orings.pk], 100)

    def test_one_missing_price_makes_the_whole_split_even(self):
        """Not prices-with-a-zero: that hands the unpriced part a landed cost of
        nothing and never mentions it."""
        PartKitItem.objects.create(kit=self.kit, part=self.compressor, value_minor=9900)
        PartKitItem.objects.create(kit=self.kit, part=self.drier)

        open_kit(self.stock_the_kit(unit_cost_minor=10000), 1)

        costs = [
            row.unit_cost_minor
            for row in StockLot.objects.filter(from_kit_lot__isnull=False)
        ]
        self.assertEqual(sorted(costs), [5000, 5000])

    def test_a_multiple_quantity_kit_item_yields_that_many(self):
        self.fill()
        PartKitItem.objects.filter(kit=self.kit, part=self.orings).update(quantity=6)

        open_kit(self.stock_the_kit(), 1)

        self.assertEqual(self.orings.on_hand, Decimal(6))

    def test_opening_two_boxes_yields_two_sets(self):
        self.fill()
        open_kit(self.stock_the_kit(qty=2), 2)
        self.assertEqual(self.drier.on_hand, Decimal(2))

    def test_opening_more_than_you_have_is_refused(self):
        self.fill()
        with self.assertRaises(ValidationError):
            open_kit(self.stock_the_kit(qty=1), 2)

    def test_a_part_with_no_recorded_contents_cannot_be_opened(self):
        with self.assertRaises(ValidationError):
            open_kit(self.stock_the_kit(), 1)

    # -- putting it back --------------------------------------------------

    def test_an_opening_can_be_undone(self):
        self.fill()
        kit_lot = self.stock_the_kit()
        open_kit(kit_lot, 1)

        close_kit(kit_lot)

        kit_lot.refresh_from_db()
        self.assertEqual(kit_lot.qty_on_hand, Decimal(1))
        self.assertEqual(self.drier.on_hand, Decimal(0))

    def test_a_kit_missing_a_part_cannot_go_back_in_its_box(self):
        """A drier fitted to a car is not going back in the box."""
        self.fill()
        kit_lot = self.stock_the_kit()
        open_kit(kit_lot, 1)
        wo = WorkOrder.objects.create(asset=self.asset, title="A/C")
        consume(self.drier, 1, work_order=wo)

        with self.assertRaises(ValidationError):
            close_kit(kit_lot)

        kit_lot.refresh_from_db()
        self.assertEqual(kit_lot.qty_on_hand, Decimal(0), "the kit came back anyway")
        self.assertEqual(self.compressor.on_hand, Decimal(1), "the rest was taken back")

    # -- the containment graph --------------------------------------------

    def test_a_kit_cannot_contain_itself(self):
        item = PartKitItem(kit=self.kit, part=self.kit)
        with self.assertRaises(ValidationError):
            item.full_clean()

    def test_a_kit_cannot_contain_a_kit_that_contains_it(self):
        """Opening one would otherwise recurse for ever."""
        inner = Part.objects.create(name="Seal kit")
        PartKitItem.objects.create(kit=self.kit, part=inner)

        loop = PartKitItem(kit=inner, part=self.kit)
        with self.assertRaises(ValidationError):
            loop.full_clean()

    def test_zero_of_something_is_not_a_kit_item(self):
        item = PartKitItem(kit=self.kit, part=self.drier, quantity=0)
        with self.assertRaises(ValidationError):
            item.full_clean()


class KitScreenTests(TestCase):
    """What the kit card says for itself.

    Reported as: the boxes on the form do not say what they are for, and the
    only way to find out is to submit one and read the row it produces.
    """

    def setUp(self):
        from homeautoshop.accounts.models import Role, User

        self.user = User.objects.create_user(
            username="andy", password="x" * 16, role=Role.ADMIN
        )
        self.client.force_login(self.user)
        self.kit = Part.objects.create(name="A/C Kit", part_number="964")
        self.compressor = Part.objects.create(name="A/C Compressor")
        self.valve = Part.objects.create(name="Expansion Valve")

    def page(self) -> str:
        return self.client.get(
            reverse("part_detail", args=[self.kit.pk])
        ).content.decode()

    def test_every_box_on_the_form_carries_a_visible_label(self):
        """Not an `aria-label`: a sighted operator got three unmarked controls,
        two of them identical number boxes."""
        page = self.page()
        for field in ("id_kit_part", "id_kit_quantity", "id_kit_value"):
            self.assertIn(f'for="{field}"', page)
            self.assertIn(f'id="{field}"', page)

    def test_the_form_asks_for_a_price_and_not_a_proportion(self):
        page = self.page()
        self.assertIn("Price each", page)
        self.assertIn("Leave blank to use whatever the part itself costs", page)

    def test_a_row_shows_its_price_and_the_split_that_falls_out_of_it(self):
        """`cost share 1` was neither a price nor a percentage."""
        PartKitItem.objects.create(
            kit=self.kit, part=self.compressor, value_minor=17526
        )
        PartKitItem.objects.create(kit=self.kit, part=self.valve, value_minor=846)

        page = self.page()

        self.assertIn("$175.26", page)
        self.assertIn("$8.46", page)
        self.assertIn("95% of the kit's cost", page)
        self.assertIn("5% of the kit's cost", page)

    def test_an_unpriced_row_says_the_split_is_even_and_why(self):
        PartKitItem.objects.create(kit=self.kit, part=self.compressor)
        PartKitItem.objects.create(kit=self.kit, part=self.valve)

        page = self.page()

        self.assertIn("Split evenly, because at least one of these has no price", page)
        self.assertIn("no price yet", page)

    def test_a_priced_kit_says_the_cost_follows_the_prices(self):
        PartKitItem.objects.create(kit=self.kit, part=self.compressor, value_minor=100)
        PartKitItem.objects.create(kit=self.kit, part=self.valve, value_minor=100)

        self.assertIn("divided in proportion to these prices", self.page())

    def test_the_price_box_may_be_left_alone(self):
        """Blank means "whatever the part costs", not zero."""
        self.compressor.typical_cost_minor = 4200
        self.compressor.save(update_fields=["typical_cost_minor"])

        self.client.post(
            reverse("kit_item_add", args=[self.kit.pk]),
            {"part": str(self.compressor.pk), "quantity": "1", "value": ""},
        )

        item = PartKitItem.objects.get()
        self.assertIsNone(item.value_minor)
        self.assertEqual(item.unit_value_minor, 4200)

    def test_a_price_is_typed_the_way_it_is_written(self):
        self.client.post(
            reverse("kit_item_add", args=[self.kit.pk]),
            {"part": str(self.compressor.pk), "quantity": "1", "value": "$175.26"},
        )
        self.assertEqual(PartKitItem.objects.get().value_minor, 17526)

    def test_nonsense_in_the_price_box_is_a_message_not_a_crash(self):
        response = self.client.post(
            reverse("kit_item_add", args=[self.kit.pk]),
            {"part": str(self.compressor.pk), "quantity": "1", "value": "later"},
            follow=True,
        )
        self.assertEqual(PartKitItem.objects.count(), 0)
        self.assertContains(response, "Enter an amount")

    def test_the_percentages_shown_add_up_to_a_hundred(self):
        """Same allocator as the money, so the screen cannot disagree with the
        ledger about where a third of a kit went."""
        for part in (self.compressor, self.valve, Part.objects.create(name="Drier")):
            PartKitItem.objects.create(kit=self.kit, part=part)

        response = self.client.get(reverse("part_detail", args=[self.kit.pk]))

        shown = [item.share_percent for item in response.context["kit_items"]]
        self.assertEqual(sum(shown), 100)
        self.assertEqual(sorted(shown), [33, 33, 34])

    def test_a_kit_with_nothing_in_it_still_renders(self):
        self.assertIn("Make this a kit", self.page())


class CostSplitTests(TestCase):
    """The arithmetic on its own, where the awkward cases are cheap to state."""

    def test_an_even_split_that_does_not_divide_still_adds_up(self):
        shares = split_kit_cost(43654, [Decimal(1)] * 4)
        self.assertEqual(sum(shares), 43654)
        self.assertEqual(sorted(shares), [10913, 10913, 10914, 10914])

    def test_the_remainder_goes_to_the_larger_shares(self):
        shares = split_kit_cost(100, [Decimal(2), Decimal(1)])
        self.assertEqual(shares, [67, 33])

    def test_weights_of_zero_fall_back_to_an_even_split(self):
        """Rather than dividing by zero and taking the screen with it."""
        self.assertEqual(split_kit_cost(10, [Decimal(0), Decimal(0)]), [5, 5])

    def test_nothing_to_split_is_not_an_error(self):
        self.assertEqual(split_kit_cost(500, []), [])


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

    def test_a_time_entry_can_be_corrected(self):
        """It used to refuse, and the refusal was wrong for this shop: picking
        the wrong category is the commonest mistake anybody makes here, and
        delete-and-retype is the same record with a gap where the old row was.
        Nobody is billed from these numbers (NG-1)."""
        entry = TimeEntry.objects.create(work_order=self.wo, minutes=45)

        entry.minutes = 30
        entry.category = TimeEntry.Category.DIAGNOSIS
        entry.save()

        entry.refresh_from_db()
        self.assertEqual(entry.minutes, 30)
        self.assertEqual(entry.category, TimeEntry.Category.DIAGNOSIS)


class UseWithoutAJobTests(TestCase):
    """Taking a part off the shelf when there is no work order (FR-INV-10).

    Reported as: I know I installed the fuel pump bought in June 2015 and I have
    no other information. Every route out of stock wanted a work order, so the
    choice was inventing one — a fiction in a vehicle's history — or leaving the
    pump on the shelf where it inflates what the shop thinks it owns.
    """

    def setUp(self):
        from homeautoshop.accounts.models import Role, User

        self.user = User.objects.create_user(
            username="andy", password="x" * 16, role=Role.ADMIN
        )
        self.client.force_login(self.user)
        self.part = Part.objects.create(name="Fuel pump", part_number="E8229")
        self.asset = Asset.objects.create(nickname="Aero", make="Suzuki", model="Aerio")
        self.lot = lot(self.part, 2, 8995)

    def use(self, **extra):
        return self.client.post(
            reverse("part_use", args=[self.part.pk]), {"qty": "1", **extra}, follow=True
        )

    def test_a_part_can_be_used_with_nothing_else_known(self):
        self.use()

        usage = PartUsage.objects.get()
        self.assertIsNone(usage.work_order)
        self.assertIsNone(usage.asset)
        self.assertEqual(usage.qty, Decimal(1))
        self.assertEqual(self.part.on_hand, Decimal(1))

    def test_it_costs_what_that_one_cost(self):
        """Off the shelf is off the shelf: the FIFO draw is the same one a job
        makes, so the part leaves at its lot's price rather than at nothing."""
        self.use()
        self.assertEqual(PartUsage.objects.get().unit_cost_minor, 8995)

    def test_the_ledger_carries_it_like_any_other_use(self):
        self.use()
        entry = self.lot.transactions.order_by("-created_at").first()
        self.assertEqual(entry.reason, StockTransaction.Reason.CONSUME)
        self.assertEqual(entry.delta, Decimal(-1))
        self.assertIsNone(entry.work_order)

    def test_a_vehicle_may_be_named_without_a_job(self):
        self.use(asset=str(self.asset.pk))

        usage = PartUsage.objects.get()
        self.assertEqual(usage.asset, self.asset)
        self.assertEqual(usage.vehicle, self.asset)

    def test_naming_the_vehicle_still_records_the_fitment(self):
        """FR-PART-3 does not depend on there having been a work order: the
        part went on the car, which is the fact fitment is made of."""
        self.use(asset=str(self.asset.pk))

        fitment = PartFitment.objects.get(part=self.part, asset=self.asset)
        self.assertEqual(fitment.confidence, PartFitment.Confidence.CONFIRMED)

    def test_a_date_from_years_ago_is_kept(self):
        self.use(installed_at="2015-06-14")
        self.assertEqual(PartUsage.objects.get().installed_at, date(2015, 6, 14))

    def test_no_date_means_today_rather_than_nothing(self):
        self.use()
        self.assertEqual(PartUsage.objects.get().installed_at, timezone.localdate())

    def test_using_more_than_is_there_is_refused(self):
        response = self.use(qty="9")

        self.assertEqual(PartUsage.objects.count(), 0)
        self.assertEqual(self.part.on_hand, Decimal(2))
        self.assertContains(response, "on the shelf")

    def test_the_part_page_offers_it_while_there_is_stock(self):
        page = self.client.get(reverse("part_detail", args=[self.part.pk])).content.decode()
        self.assertIn("Record it as used", page)

    def test_and_does_not_while_there_is_none(self):
        """A button that can only fail is worse than no button."""
        StockTransaction.record(self.lot, -2, StockTransaction.Reason.CONSUME)
        page = self.client.get(reverse("part_detail", args=[self.part.pk])).content.decode()
        self.assertNotIn("Record it as used", page)

    def test_a_usage_with_no_job_still_renders_on_the_part(self):
        """`{% url 'work_order_detail' usage.work_order.pk %}` on a null work
        order is a NoReverseMatch, not a blank."""
        self.use()
        response = self.client.get(reverse("part_detail", args=[self.part.pk]))
        self.assertContains(response, "Used — nothing else recorded")


class PartListShapeTests(TestCase):
    """A kit's contents belong under it, not beside it.

    Reported as: the parts list shows everything flat, so there is no way to see
    which parts are components of a kit.
    """

    def setUp(self):
        from homeautoshop.accounts.models import Role, User

        self.user = User.objects.create_user(
            username="andy", password="x" * 16, role=Role.ADMIN
        )
        self.client.force_login(self.user)
        self.kit = Part.objects.create(name="A/C Kit", part_number="9642644B")
        self.condenser = Part.objects.create(name="A/C Condenser", part_number="4696C")
        self.loose = Part.objects.create(name="Oil filter", part_number="PH3593A")
        PartKitItem.objects.create(kit=self.kit, part=self.condenser)

    def rows(self, **params):
        return self.client.get(reverse("part_list"), params).context["parts"]

    def test_a_component_is_not_listed_beside_its_own_kit(self):
        listed = self.rows()
        self.assertIn(self.kit, listed)
        self.assertIn(self.loose, listed)
        self.assertNotIn(self.condenser, listed)

    def test_the_kit_carries_its_contents(self):
        kit = next(part for part in self.rows() if part.pk == self.kit.pk)
        self.assertEqual([item.part for item in kit.kit_contents], [self.condenser])

    def test_the_component_is_still_on_the_page(self):
        page = self.client.get(reverse("part_list")).content.decode()
        self.assertIn("A/C Condenser", page)
        self.assertIn("kit of 1 part", page)

    def test_a_search_finds_a_component_at_the_top_level(self):
        """Nesting it under a kit whose name does not match would answer a
        question nobody asked."""
        self.assertIn(self.condenser, self.rows(q="Condenser"))

    def test_a_searched_component_says_which_kit_it_is_in(self):
        response = self.client.get(reverse("part_list"), {"q": "Condenser"})
        self.assertContains(response, "in A/C Kit")

    def test_a_component_whose_kit_is_absent_stays_at_the_top_level(self):
        """Otherwise it would be nested under nothing and disappear."""
        self.kit.delete()
        self.assertIn(self.condenser, self.rows())


class UsageWithoutAJobRendersEverywhereTests(TestCase):
    """Every screen that lists a usage, given one with no work order.

    `{% url 'work_order_detail' usage.work_order.pk %}` against `None` is a
    NoReverseMatch — a 500, not a blank — so making the field nullable is only
    half the change. These are the three screens that render usages.
    """

    def setUp(self):
        from homeautoshop.accounts.models import Role, User

        self.user = User.objects.create_user(
            username="andy", password="x" * 16, role=Role.ADMIN
        )
        self.client.force_login(self.user)
        self.asset = Asset.objects.create(nickname="Aero")
        self.part = Part.objects.create(
            name="Alternator", has_core=True, core_value_minor=3500
        )
        self.lot = lot(self.part, 1, 12000)
        consume(
            self.part, 1, asset=self.asset, user=self.user,
        )
        PartUsage.objects.update(warranty_months=24)

    def test_the_cores_owed_list(self):
        response = self.client.get(reverse("inventory"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Alternator")

    def test_the_warranty_report(self):
        response = self.client.get(reverse("reports"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Alternator")

    def test_the_part_page(self):
        response = self.client.get(reverse("part_detail", args=[self.part.pk]))
        self.assertEqual(response.status_code, 200)


class QuantityStepTests(TestCase):
    """A spinner that suits what the part is measured in.

    Reported as: it is not realistic to have 0.001 of a part. True of nearly
    everything a shop counts, and untrue of the oil and the heater hose — so
    the step follows the part's unit rather than being one number for both.
    """

    def setUp(self):
        from homeautoshop.accounts.models import Role, User

        self.user = User.objects.create_user(
            username="andy", password="x" * 16, role=Role.ADMIN
        )
        self.client.force_login(self.user)
        self.gasket = Part.objects.create(name="Head gasket", unit="each")
        self.oil = Part.objects.create(name="5W-30", unit="L")
        lot(self.gasket, 4, 3200)
        lot(self.oil, 5, 900)

    def page(self, part) -> str:
        return self.client.get(
            reverse("part_detail", args=[part.pk])
        ).content.decode()

    def test_a_counted_part_steps_by_one(self):
        self.assertEqual(self.gasket.qty_step, "1")

    def test_a_measured_one_keeps_its_thousandths(self):
        """Four and a half litres of oil is an ordinary thing to record."""
        self.assertEqual(self.oil.qty_step, "0.001")

    def test_the_use_box_follows_the_part(self):
        self.assertIn('step="1" min="1" name="qty"', self.page(self.gasket))
        self.assertIn('step="0.001" min="0.001" name="qty"', self.page(self.oil))

    def test_the_cycle_count_box_follows_the_part(self):
        self.assertIn('step="1" name="counted"', self.page(self.gasket))
        self.assertIn('step="0.001" name="counted"', self.page(self.oil))

    def test_the_add_stock_box_follows_the_part(self):
        self.assertIn('step="1"', self.page(self.gasket))
        page = self.page(self.oil)
        self.assertIn('name="quantity"', page)
        self.assertNotIn('step="1" name="quantity"', page)

    def test_a_part_picker_carries_each_parts_step_for_the_box_beside_it(self):
        """The kit form cannot know which part is coming, so the choice does:
        whole ones by default, relaxed by the part that gets picked."""
        page = self.page(self.gasket)
        self.assertIn('data-step-from="id_kit_part"', page)
        self.assertIn('data-step="0.001"', page)

    def test_storage_still_takes_fractions_whatever_the_box_says(self):
        """The step is a spinner, not a rule. Half a litre must still record."""
        self.client.post(
            reverse("part_use", args=[self.oil.pk]), {"qty": "0.5"}
        )
        self.assertEqual(PartUsage.objects.get().qty, Decimal("0.5"))


class LotEditTests(TestCase):
    """Correcting a lot after it was recorded (FR-INV-11).

    Reported as: I added stock without a unit cost or a location and there is no
    way to change either. A lot with no cost is not cosmetic — everything drawn
    from it costs nothing, so the job it goes on is cheaper than it was.
    """

    def setUp(self):
        from homeautoshop.accounts.models import Role, User

        self.user = User.objects.create_user(
            username="andy", password="x" * 16, role=Role.ADMIN
        )
        self.client.force_login(self.user)
        self.part = Part.objects.create(name="Fuel pump")
        self.bin = Location.objects.create(name="Shelf A")
        self.lot = StockLot.objects.create(part=self.part, qty_on_hand=0)
        StockTransaction.record(self.lot, 2, StockTransaction.Reason.FOUND)

    def url(self):
        return reverse("lot_edit", args=[self.part.pk, self.lot.pk])

    def test_the_page_opens_from_the_part(self):
        page = self.client.get(
            reverse("part_detail", args=[self.part.pk])
        ).content.decode()
        self.assertIn(self.url(), page)

    def test_a_missing_cost_is_named_where_the_number_should_be(self):
        response = self.client.get(reverse("part_detail", args=[self.part.pk]))
        self.assertContains(response, "no cost")

    def test_a_cost_and_a_location_can_be_filled_in_afterwards(self):
        self.client.post(
            self.url(),
            {
                "location": str(self.bin.pk),
                "unit_cost_minor": "$89.95",
                "acquired_on": "2015-06-14",
            },
        )

        self.lot.refresh_from_db()
        self.assertEqual(self.lot.unit_cost_minor, 8995)
        self.assertEqual(self.lot.location, self.bin)
        self.assertEqual(self.lot.acquired_on, date(2015, 6, 14))

    def test_the_quantity_is_not_one_of_the_boxes(self):
        """It is a projection of the ledger. A box here would be exactly the
        silent correction the ledger exists to prevent."""
        page = self.client.get(self.url()).content.decode()
        self.assertNotIn('name="qty_on_hand"', page)
        self.assertIn("is not typed", page)

    def test_editing_moves_no_stock(self):
        self.client.post(self.url(), {"unit_cost_minor": "$1.00"})
        self.lot.refresh_from_db()
        self.assertEqual(self.lot.qty_on_hand, Decimal(2))
        self.assertEqual(self.lot.transactions.count(), 1)

    def test_an_untouched_lot_can_be_removed(self):
        self.client.post(reverse("lot_delete", args=[self.part.pk, self.lot.pk]))
        self.assertEqual(StockLot.objects.filter(pk=self.lot.pk).count(), 0)
        self.assertEqual(self.part.on_hand, Decimal(0))

    def test_a_lot_something_came_out_of_is_not(self):
        """The draw is what a job cost. Deleting the lot underneath it would
        rewrite that."""
        consume(self.part, 1, user=self.user)

        response = self.client.post(
            reverse("lot_delete", args=[self.part.pk, self.lot.pk]), follow=True
        )

        self.assertTrue(StockLot.objects.filter(pk=self.lot.pk).exists())
        self.assertContains(response, "Count it to zero instead")

    def test_a_received_lot_says_to_un_receive_it(self):
        vendor = Vendor.objects.create(name="RockAuto")
        purchase = Purchase.objects.create(vendor=vendor)
        line = PurchaseLine.objects.create(
            purchase=purchase, part=self.part, qty_ordered=1
        )
        self.lot.purchase_line = line
        self.lot.save(update_fields=["purchase_line"])

        response = self.client.post(
            reverse("lot_delete", args=[self.part.pk, self.lot.pk]), follow=True
        )

        self.assertTrue(StockLot.objects.filter(pk=self.lot.pk).exists())
        self.assertContains(response, "Un-receive it on the order")

    def test_a_lot_from_a_kit_says_to_close_the_kit(self):
        kit = Part.objects.create(name="A/C Kit")
        kit_lot = lot(kit, 1, 1000)
        self.lot.from_kit_lot = kit_lot
        self.lot.save(update_fields=["from_kit_lot"])

        response = self.client.post(
            reverse("lot_delete", args=[self.part.pk, self.lot.pk]), follow=True
        )

        self.assertTrue(StockLot.objects.filter(pk=self.lot.pk).exists())
        self.assertContains(response, "Put the kit back together")

    def test_a_lot_of_another_part_is_not_reachable_from_this_one(self):
        other = Part.objects.create(name="Oil filter")
        response = self.client.get(reverse("lot_edit", args=[other.pk, self.lot.pk]))
        self.assertEqual(response.status_code, 404)


class PartListFactsTests(TestCase):
    """What a row answers without being opened.

    Reported as: I have to go into each part to see what it fits, what it costs,
    when it was bought and for how much.
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
            name="Fuel pump", manufacturer="DELPHI", part_number="E8229",
            typical_cost_minor=8995,
        )
        self.bin = Location.objects.create(name="Shelf A")
        lot(self.part, 2, 8995, location=self.bin)
        PartFitment.objects.create(
            part=self.part, asset=self.asset,
            confidence=PartFitment.Confidence.CONFIRMED,
        )
        vendor = Vendor.objects.create(name="RockAuto")
        purchase = Purchase.objects.create(vendor=vendor, ordered_on=date(2015, 6, 14))
        PurchaseLine.objects.create(
            purchase=purchase, part=self.part, qty_ordered=1, unit_price_minor=8995
        )

    def page(self) -> str:
        return self.client.get(reverse("part_list")).content.decode()

    def test_the_row_names_the_price(self):
        page = self.page()
        self.assertIn("Price", page)
        self.assertIn("$89.95", page)

    def test_the_row_names_what_it_fits(self):
        self.assertIn("Aero", self.page())

    def test_the_row_names_when_it_was_bought_and_for_how_much(self):
        page = self.page()
        self.assertIn("Bought", page)
        self.assertIn("June 14, 2015", page)
        self.assertIn("RockAuto", page)

    def test_the_row_names_where_it_is(self):
        page = self.page()
        self.assertIn("Where", page)
        self.assertIn("Shelf A", page)

    def test_a_part_it_does_not_fit_is_not_listed_as_fitting(self):
        other = Asset.objects.create(nickname="Van", make="Ford", model="F-150")
        PartFitment.objects.create(
            part=self.part, asset=other,
            confidence=PartFitment.Confidence.DOES_NOT_FIT,
        )
        self.assertNotIn("Van", self.page())

    def test_a_long_fitment_list_is_counted_rather_than_printed(self):
        for n in range(5):
            PartFitment.objects.create(
                part=self.part, make="Make%s" % n, model="Model%s" % n,
                confidence=PartFitment.Confidence.VENDOR,
            )
        self.assertIn("+3 more", self.page())

    def test_a_part_that_knows_nothing_shows_no_empty_labels(self):
        """A row of dashes is noise; the gap says the same thing."""
        Part.objects.create(name="Mystery bracket")
        page = self.client.get(reverse("part_list"), {"q": "Mystery"}).content.decode()
        self.assertIn("Mystery bracket", page)
        self.assertNotIn("Where", page)
        self.assertNotIn("Bought", page)

    def queries_for_the_page(self) -> int:
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        with CaptureQueriesContext(connection) as captured:
            self.assertEqual(self.client.get(reverse("part_list")).status_code, 200)
        return len(captured)

    def test_adding_parts_does_not_add_queries(self):
        """The row went from two facts to six, and each of `on_hand`,
        `is_low` and `known_cost` issues its own query when read off a model.
        Twenty more parts must cost nothing extra, or the page that now answers
        more questions answers them too slowly to be worth having.

        Counted as a difference rather than a fixed budget: the absolute number
        moves whenever an unrelated part of the page changes, and what has to
        stay true is the slope.
        """
        one_part = self.queries_for_the_page()

        for n in range(20):
            other = Part.objects.create(name="Filler %s" % n)
            lot(other, 1, 100)

        self.assertEqual(self.queries_for_the_page(), one_part)
