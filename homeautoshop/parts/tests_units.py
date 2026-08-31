"""Units, editable time, a correctable order, and a part that counts (§7.4).

Four reports, and the last is a defect in something built two rounds ago:

* Part units were four hard-coded choices — each, litres, kilograms, feet —
  which is a guess about somebody else's catalogue. R-134a is sold in cylinders
  by the pound and dispensed by the ounce or the half-kilogram, and none of
  those were sayable.
* A time entry was append-only, so the commonest mistake anybody makes — the
  wrong category — became delete-and-retype.
* An order's own fields could not be touched, so a missing order number or a
  tax figure left at zero was permanent.
* **Using a part on a vehicle put it nowhere the vehicle is read.** It left the
  shelf, cost money, and appeared in no total and on no timeline.
"""

from __future__ import annotations

from decimal import Decimal

from django.test import TestCase
from django.urls import reverse

from homeautoshop.accounts.models import Role, User
from homeautoshop.assets.models import Asset
from homeautoshop.core import costs
from homeautoshop.core.measurements import compatible_units, convert, part_dimension
from homeautoshop.parts.models import Part, PartUsage, StockLot, StockTransaction
from homeautoshop.purchasing.models import Purchase, Vendor
from homeautoshop.work.models import TimeEntry, WorkOrder


def stock(part, qty, unit_cost_minor=100):
    lot = StockLot.objects.create(part=part, qty_on_hand=0, unit_cost_minor=unit_cost_minor)
    StockTransaction.record(lot, qty, StockTransaction.Reason.RECEIVE)
    return lot


class Base(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="andy", password="x" * 16, role=Role.ADMIN
        )
        self.client.force_login(self.user)


class UnitTests(Base):
    """The R-134a case, which is the one that exposed this."""

    def setUp(self):
        super().setUp()
        self.refrigerant = Part.objects.create(name="R-134a", unit="lb")
        self.gasket = Part.objects.create(name="Head gasket", unit="each")

    def test_a_part_may_be_measured_in_pounds(self):
        self.assertEqual(self.refrigerant.unit, "lb")
        self.assertEqual(self.refrigerant.unit_label, "pounds")

    def test_the_units_offered_are_the_ones_it_converts_to(self):
        self.assertEqual(self.refrigerant.compatible_units, ("lb", "oz", "kg", "g"))

    def test_a_counted_part_converts_to_nothing(self):
        """A thing is a thing. There is no factor between a gasket and a litre,
        and offering one would be a control asking a question with no answers."""
        self.assertEqual(self.gasket.compatible_units, ("each",))
        self.assertEqual(part_dimension("each"), "count")

    def test_half_a_kilogram_out_of_a_pound_cylinder(self):
        """The Suzuki takes 0.50 kg, which is 1.10 lb — and nobody should be
        doing that on paper mid-job."""
        self.assertEqual(
            round(self.refrigerant.quantity_in_stock_units("0.50", "kg"), 3),
            Decimal("1.102"),
        )

    def test_the_part_s_own_unit_is_left_exactly_alone(self):
        self.assertEqual(
            self.refrigerant.quantity_in_stock_units("2", "lb"), Decimal("2")
        )
        self.assertEqual(self.refrigerant.quantity_in_stock_units("2", None), Decimal("2"))

    def test_dispensing_in_kilograms_draws_the_right_amount(self):
        stock(self.refrigerant, 30)

        self.client.post(
            reverse("part_use", args=[self.refrigerant.pk]),
            {"qty": "0.50", "qty_unit": "kg"},
        )

        self.assertEqual(round(self.refrigerant.on_hand, 3), Decimal("28.898"))

    def test_stock_added_in_ounces_lands_in_pounds(self):
        self.client.post(
            reverse("lot_add", args=[self.refrigerant.pk]),
            {"quantity": "16", "quantity_unit": "oz", "acquired_on": "2026-01-01"},
        )
        self.assertEqual(round(self.refrigerant.on_hand, 3), Decimal("1.000"))

    def test_a_count_taken_in_grams_is_still_a_count_in_pounds(self):
        lot = stock(self.refrigerant, 2)

        self.client.post(
            reverse("lot_count", args=[self.refrigerant.pk, lot.pk]),
            {"counted": "453.59237", "counted_unit": "g", "note": "weighed it"},
        )

        lot.refresh_from_db()
        self.assertEqual(round(lot.qty_on_hand, 3), Decimal("1.000"))

    def test_the_picker_is_offered_only_where_there_is_a_choice(self):
        stock(self.refrigerant, 30)
        page = self.client.get(
            reverse("part_detail", args=[self.refrigerant.pk])
        ).content.decode()
        self.assertIn('name="qty_unit"', page)

        stock(self.gasket, 1)
        page = self.client.get(reverse("part_detail", args=[self.gasket.pk])).content.decode()
        self.assertNotIn('name="qty_unit"', page)

    def test_length_is_a_dimension_parts_can_use(self):
        """Hose, wire and weatherstrip are sold by the foot."""
        hose = Part.objects.create(name="Heater hose", unit="ft")
        self.assertIn("in", hose.compatible_units)
        self.assertEqual(convert(1, "ft", "in"), Decimal(12))

    def test_an_unknown_unit_does_not_crash_the_conversion(self):
        odd = Part.objects.create(name="Something", unit="widgets")
        self.assertEqual(odd.quantity_in_stock_units("3", "kg"), Decimal("3"))


class EditableTimeTests(Base):
    def setUp(self):
        super().setUp()
        self.asset = Asset.objects.create(nickname="Aero")
        self.wo = WorkOrder.objects.create(asset=self.asset, title="Brakes")
        self.entry = TimeEntry.objects.create(
            work_order=self.wo, minutes=90, category=TimeEntry.Category.WRENCHING
        )

    def test_the_wrong_category_can_be_corrected(self):
        """The reported case, and the commonest mistake anybody makes here."""
        self.client.post(
            reverse("time_entry_edit", args=[self.wo.pk, self.entry.pk]),
            {"hours": "1.5", "category": TimeEntry.Category.DIAGNOSIS, "note": ""},
        )
        self.entry.refresh_from_db()
        self.assertEqual(self.entry.category, TimeEntry.Category.DIAGNOSIS)
        self.assertEqual(self.entry.minutes, 90)

    def test_hours_are_what_the_box_asks_for(self):
        self.client.post(
            reverse("time_entry_edit", args=[self.wo.pk, self.entry.pk]),
            {"hours": "0.25", "category": TimeEntry.Category.WRENCHING, "note": ""},
        )
        self.entry.refresh_from_db()
        self.assertEqual(self.entry.minutes, 15)

    def test_editing_a_timed_entry_drops_its_timestamps(self):
        """Otherwise the duration and the clock times disagree, and the row
        makes two contradictory claims about the same afternoon."""
        from django.utils import timezone

        start = timezone.now()
        entry = TimeEntry.objects.create(
            work_order=self.wo, started_at=start, ended_at=start
        )
        entry.minutes = 60
        entry.save()

        self.client.post(
            reverse("time_entry_edit", args=[self.wo.pk, entry.pk]),
            {"hours": "2", "category": TimeEntry.Category.WRENCHING, "note": ""},
        )

        entry.refresh_from_db()
        self.assertEqual(entry.minutes, 120)
        self.assertIsNone(entry.started_at)

    def test_the_job_links_to_it(self):
        page = self.client.get(
            reverse("work_order_detail", args=[self.wo.pk])
        ).content.decode()
        self.assertIn(reverse("time_entry_edit", args=[self.wo.pk, self.entry.pk]), page)


class PurchaseEditTests(Base):
    def setUp(self):
        super().setUp()
        self.vendor = Vendor.objects.create(name="RockAuto")
        self.purchase = Purchase.objects.create(vendor=self.vendor)

    def test_the_order_number_can_be_filled_in_afterwards(self):
        self.client.post(
            reverse("purchase_edit", args=[self.purchase.pk]),
            {"vendor": str(self.vendor.pk), "order_number": "357640871",
             "status": self.purchase.status, "tax_minor": "$0.00",
             "shipping_minor": "$0.00", "discount_minor": "$0.00"},
        )
        self.purchase.refresh_from_db()
        self.assertEqual(self.purchase.order_number, "357640871")

    def test_shipping_and_tax_can_be_corrected(self):
        """They are what makes a landed cost landed."""
        self.client.post(
            reverse("purchase_edit", args=[self.purchase.pk]),
            {"vendor": str(self.vendor.pk), "status": self.purchase.status,
             "tax_minor": "$39.85", "shipping_minor": "$55.96", "discount_minor": "$0.00"},
        )
        self.purchase.refresh_from_db()
        self.assertEqual(self.purchase.tax_minor, 3985)
        self.assertEqual(self.purchase.shipping_minor, 5596)

    def test_the_page_offers_it(self):
        page = self.client.get(
            reverse("purchase_detail", args=[self.purchase.pk])
        ).content.decode()
        self.assertIn(reverse("purchase_edit", args=[self.purchase.pk]), page)


class PartUseReachesTheVehicleTests(Base):
    """The defect: a part used on a vehicle went nowhere the vehicle is read."""

    def setUp(self):
        super().setUp()
        self.asset = Asset.objects.create(nickname="Aero", make="Suzuki", model="Aerio")
        self.part = Part.objects.create(name="Fuel pump")
        stock(self.part, 1, unit_cost_minor=8995)

    def use_it(self):
        return self.client.post(
            reverse("part_use", args=[self.part.pk]),
            {"qty": "1", "asset": str(self.asset.pk), "installed_at": "2015-06-14"},
        )

    def test_it_lands_in_what_the_vehicle_has_cost(self):
        """It left the shelf and cost real money. Appearing in no total is the
        one thing the whole feature exists to prevent."""
        self.use_it()

        rollup = costs.asset_cost(self.asset)

        self.assertEqual(rollup.total.amount, 8995)

    def test_it_appears_on_the_vehicle_timeline(self):
        self.use_it()
        response = self.client.get(reverse("asset_detail", args=[self.asset.pk]))
        self.assertContains(response, "Part fitted")
        self.assertContains(response, "Fuel pump")

    def test_a_usage_with_no_vehicle_named_stays_out_of_a_vehicle_s_costs(self):
        """Not knowing which car it went on is a real answer, and guessing one
        would put somebody else's money on this vehicle."""
        self.client.post(reverse("part_use", args=[self.part.pk]), {"qty": "1"})

        self.assertEqual(costs.asset_cost(self.asset).total.amount, 0)
        self.assertEqual(PartUsage.objects.count(), 1)

    def test_a_part_used_on_a_job_is_still_counted_once(self):
        """Both routes now feed the same total, so the obvious way to fix this
        would have been to double-count everything that already worked."""
        wo = WorkOrder.objects.create(asset=self.asset, title="Fuel")
        from homeautoshop.parts.services import consume

        consume(self.part, 1, work_order=wo)

        self.assertEqual(costs.asset_cost(self.asset).total.amount, 8995)
