"""Correcting and removing the small records (FR-ADM-8).

Everything here was create-only: a vendor, a location, a cross-reference, an
expense, a time entry, an order line. Each was reachable from a screen once and
never again, so a name typed in a hurry, an amount off by a decimal place, or a
timer left running overnight was permanent — and the alternatives were the
Django admin or living with it.

The refusals are the interesting half. Every one of these can be entangled with
something that has already happened, and the rule throughout is the same: a
record that explains money already spent or stock already moved does not quietly
disappear from underneath it.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from django.test import TestCase
from django.urls import reverse

from homeautoshop.accounts.models import Role, User
from homeautoshop.assets.models import Asset, AssetOwnership, UsageReading
from homeautoshop.parts.models import (
    Location, Part, PartCrossRef, PartKitItem, StockLot, StockTransaction,
)
from homeautoshop.people.models import Person
from homeautoshop.purchasing.models import Expense, Purchase, PurchaseLine, Vendor
from homeautoshop.work.models import TimeEntry, WorkOrder


class Base(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="andy", password="x" * 16, role=Role.ADMIN
        )
        self.client.force_login(self.user)


class VendorTests(Base):
    def setUp(self):
        super().setUp()
        self.vendor = Vendor.objects.create(name="RockAuto")

    def test_a_name_can_be_corrected(self):
        self.client.post(
            reverse("vendor_edit", args=[self.vendor.pk]),
            {"name": "RockAuto.com", "type": self.vendor.type},
        )
        self.vendor.refresh_from_db()
        self.assertEqual(self.vendor.name, "RockAuto.com")

    def test_an_unused_vendor_can_go(self):
        self.client.post(reverse("vendor_delete", args=[self.vendor.pk]))
        self.assertEqual(Vendor.objects.count(), 0)

    def test_one_a_purchase_names_stays(self):
        """`Purchase.vendor` is PROTECT for a reason, and a soft delete would
        slip past that check while leaving the order pointing at a supplier no
        list shows."""
        Purchase.objects.create(vendor=self.vendor)

        response = self.client.post(
            reverse("vendor_delete", args=[self.vendor.pk]), follow=True
        )

        self.assertEqual(Vendor.objects.count(), 1)
        self.assertContains(response, "so it stays")

    def test_the_list_links_to_the_editor(self):
        page = self.client.get(reverse("vendor_list")).content.decode()
        self.assertIn(reverse("vendor_edit", args=[self.vendor.pk]), page)


class LocationTests(Base):
    def setUp(self):
        super().setUp()
        self.shelf = Location.objects.create(name="Shelf A")
        self.bin = Location.objects.create(name="Bin 3", parent=self.shelf)
        self.part = Part.objects.create(name="Oil filter")

    def test_a_shelf_can_be_renamed(self):
        self.client.post(
            reverse("location_edit", args=[self.shelf.pk]), {"name": "Back wall"}
        )
        self.shelf.refresh_from_db()
        self.assertEqual(self.shelf.name, "Back wall")

    def test_a_bin_can_be_moved(self):
        other = Location.objects.create(name="Cabinet")
        self.client.post(
            reverse("location_edit", args=[self.bin.pk]),
            {"name": "Bin 3", "parent": str(other.pk)},
        )
        self.bin.refresh_from_db()
        self.assertEqual(self.bin.parent, other)

    def test_it_cannot_be_moved_inside_itself(self):
        response = self.client.post(
            reverse("location_edit", args=[self.shelf.pk]),
            {"name": "Shelf A", "parent": str(self.bin.pk)},
        )
        self.shelf.refresh_from_db()
        self.assertIsNone(self.shelf.parent)
        self.assertEqual(response.status_code, 200)

    def test_the_dropdown_does_not_offer_the_impossible(self):
        page = self.client.get(reverse("location_edit", args=[self.shelf.pk])).content.decode()
        self.assertNotIn(str(self.bin.pk), page)

    def test_an_empty_one_can_go(self):
        self.client.post(reverse("location_delete", args=[self.bin.pk]))
        self.assertEqual(Location.objects.filter(pk=self.bin.pk).count(), 0)

    def test_one_holding_stock_stays(self):
        """A lot pointing at a removed location reads as unfiled on one screen
        and as a real shelf on another."""
        StockLot.objects.create(part=self.part, location=self.bin, qty_on_hand=1)

        response = self.client.post(
            reverse("location_delete", args=[self.bin.pk]), follow=True
        )

        self.assertTrue(Location.objects.filter(pk=self.bin.pk).exists())
        self.assertContains(response, "still holds")

    def test_one_containing_another_stays(self):
        response = self.client.post(
            reverse("location_delete", args=[self.shelf.pk]), follow=True
        )
        self.assertTrue(Location.objects.filter(pk=self.shelf.pk).exists())
        self.assertContains(response, "contains")

    def test_the_shelf_links_to_the_editor(self):
        page = self.client.get(reverse("inventory")).content.decode()
        self.assertIn(reverse("location_edit", args=[self.shelf.pk]), page)


class CrossRefTests(Base):
    def setUp(self):
        super().setUp()
        self.part = Part.objects.create(name="Water pump")
        self.ref = PartCrossRef.objects.create(part=self.part, value="AW9271")

    def test_a_wrong_number_can_be_taken_off(self):
        """Worse than a missing one: it makes a scan land on the wrong shelf,
        confidently."""
        self.client.post(reverse("crossref_remove", args=[self.part.pk, self.ref.pk]))
        self.assertEqual(self.part.cross_refs.count(), 0)

    def test_the_part_page_offers_it(self):
        page = self.client.get(reverse("part_detail", args=[self.part.pk])).content.decode()
        self.assertIn(reverse("crossref_remove", args=[self.part.pk, self.ref.pk]), page)

    def test_a_ref_on_another_part_is_not_reachable_here(self):
        other = Part.objects.create(name="Thermostat")
        response = self.client.post(
            reverse("crossref_remove", args=[other.pk, self.ref.pk])
        )
        self.assertEqual(response.status_code, 404)


class ExpenseTests(Base):
    def setUp(self):
        super().setUp()
        self.asset = Asset.objects.create(nickname="Aero")
        self.wo = WorkOrder.objects.create(asset=self.asset, title="Brakes")
        self.expense = Expense.objects.create(
            work_order=self.wo, amount_minor=4500, description="Machine work"
        )

    def test_an_amount_can_be_corrected(self):
        """It lands in cost per mile and in every rollup that reads it."""
        self.client.post(
            reverse("expense_edit", args=[self.expense.pk]),
            {"amount_minor": "$450.00", "category": "machine_work",
             "incurred_on": "2026-08-01", "description": "Machine work"},
        )
        self.expense.refresh_from_db()
        self.assertEqual(self.expense.amount_minor, 45000)

    def test_it_can_be_removed(self):
        self.client.post(reverse("expense_delete", args=[self.expense.pk]))
        self.assertEqual(Expense.objects.count(), 0)

    def test_removing_returns_to_the_job_it_was_on(self):
        response = self.client.post(
            reverse("expense_delete", args=[self.expense.pk])
        )
        self.assertEqual(response["Location"], reverse("work_order_detail", args=[self.wo.pk]))

    def test_one_against_a_vehicle_returns_to_its_costs(self):
        expense = Expense.objects.create(asset=self.asset, amount_minor=100)
        response = self.client.post(reverse("expense_delete", args=[expense.pk]))
        self.assertEqual(response["Location"], reverse("asset_costs", args=[self.asset.pk]))


class TimeEntryTests(Base):
    def setUp(self):
        super().setUp()
        self.asset = Asset.objects.create(nickname="Aero")
        self.wo = WorkOrder.objects.create(asset=self.asset, title="Brakes")
        self.entry = TimeEntry.objects.create(work_order=self.wo, minutes=660)

    def test_a_timer_left_running_overnight_can_be_voided(self):
        """Append-only never meant unremovable — eleven hours nobody worked is
        a number that costs the whole figure its credibility."""
        self.client.post(reverse("time_entry_delete", args=[self.wo.pk, self.entry.pk]))
        self.assertEqual(self.wo.time_entries.count(), 0)

    def test_it_is_still_a_soft_delete(self):
        self.client.post(reverse("time_entry_delete", args=[self.wo.pk, self.entry.pk]))
        self.assertEqual(TimeEntry.all_objects.filter(pk=self.entry.pk).count(), 1)

    def test_the_job_offers_it(self):
        page = self.client.get(reverse("work_order_detail", args=[self.wo.pk])).content.decode()
        self.assertIn(reverse("time_entry_delete", args=[self.wo.pk, self.entry.pk]), page)


class PurchaseLineTests(Base):
    def setUp(self):
        super().setUp()
        self.vendor = Vendor.objects.create(name="RockAuto")
        self.purchase = Purchase.objects.create(vendor=self.vendor)
        self.part = Part.objects.create(name="Brake pads")
        self.line = PurchaseLine.objects.create(
            purchase=self.purchase, part=self.part, qty_ordered=2, unit_price_minor=1000
        )

    def test_a_price_typed_wrong_can_be_corrected(self):
        self.client.post(
            reverse("purchase_line_edit", args=[self.purchase.pk, self.line.pk]),
            {"part": str(self.part.pk), "qty_ordered": "2",
             "unit_price_minor": "$100.00", "core_charge_minor": "$0.00",
             "description_as_ordered": "Brake pads"},
        )
        self.line.refresh_from_db()
        self.assertEqual(self.line.unit_price_minor, 10000)

    def test_a_line_can_be_removed(self):
        self.client.post(
            reverse("purchase_line_delete", args=[self.purchase.pk, self.line.pk])
        )
        self.assertEqual(self.purchase.lines.count(), 0)

    def test_a_received_line_refuses_both(self):
        """The receipt made stock at this price. Changing it underneath would
        leave lots costed at a number the order no longer states."""
        self.line.receive(2, user=self.user)

        edited = self.client.get(
            reverse("purchase_line_edit", args=[self.purchase.pk, self.line.pk]),
            follow=True,
        )
        deleted = self.client.post(
            reverse("purchase_line_delete", args=[self.purchase.pk, self.line.pk]),
            follow=True,
        )

        self.assertContains(edited, "Un-receive it first")
        self.assertContains(deleted, "Un-receive it first")
        self.assertEqual(self.purchase.lines.count(), 1)

    def test_the_form_does_not_offer_a_received_box(self):
        page = self.client.get(
            reverse("purchase_line_edit", args=[self.purchase.pk, self.line.pk])
        ).content.decode()
        self.assertNotIn('name="qty_received"', page)


class PartDeleteTests(Base):
    def setUp(self):
        super().setUp()
        self.part = Part.objects.create(name="Oil filter")

    def test_a_part_nothing_holds_can_go(self):
        self.client.post(reverse("part_delete", args=[self.part.pk]))
        self.assertEqual(Part.objects.count(), 0)
        self.assertEqual(Part.all_objects.count(), 1, "it should be in the trash")

    def test_one_with_stock_on_the_shelf_stays(self):
        """Removing the record does not empty the drawer."""
        lot = StockLot.objects.create(part=self.part, qty_on_hand=0, unit_cost_minor=100)
        StockTransaction.record(lot, 3, StockTransaction.Reason.RECEIVE)

        response = self.client.post(reverse("part_delete", args=[self.part.pk]), follow=True)

        self.assertEqual(Part.objects.count(), 1)
        self.assertContains(response, "does not empty the drawer")

    def test_one_a_kit_lists_stays(self):
        kit = Part.objects.create(name="A/C Kit")
        PartKitItem.objects.create(kit=kit, part=self.part)

        response = self.client.post(reverse("part_delete", args=[self.part.pk]), follow=True)

        self.assertEqual(Part.objects.filter(pk=self.part.pk).count(), 1)
        self.assertContains(response, "list this as one of their contents")

    def test_it_is_restorable_from_the_trash(self):
        self.client.post(reverse("part_delete", args=[self.part.pk]))
        self.client.post(reverse("trash_restore", args=["part", self.part.pk]))
        self.assertEqual(Part.objects.count(), 1)


class AssetDeleteTests(Base):
    def setUp(self):
        super().setUp()
        self.asset = Asset.objects.create(nickname="Aero", make="Suzuki", model="Aerio")

    def test_a_vehicle_added_twice_can_go(self):
        self.client.post(reverse("asset_delete", args=[self.asset.pk]))
        self.assertEqual(Asset.objects.count(), 0)

    def test_one_with_history_says_to_mark_it_sold_instead(self):
        """A sold car keeps every job and receipt, which is the reason to have
        kept it. Deleting it throws that away to tidy a filtered list."""
        WorkOrder.objects.create(asset=self.asset, title="Brakes")

        response = self.client.post(
            reverse("asset_delete", args=[self.asset.pk]), follow=True
        )

        self.assertEqual(Asset.objects.count(), 1)
        self.assertContains(response, "marked")

    def test_a_reading_counts_as_history_too(self):
        UsageReading.objects.create(asset=self.asset, value=1000)
        self.client.post(reverse("asset_delete", args=[self.asset.pk]))
        self.assertEqual(Asset.objects.count(), 1)

    def test_the_button_is_absent_once_there_is_history(self):
        """A button that can only fail is worse than no button."""
        page = self.client.get(reverse("asset_detail", args=[self.asset.pk])).content.decode()
        self.assertIn(reverse("asset_delete", args=[self.asset.pk]), page)

        WorkOrder.objects.create(asset=self.asset, title="Brakes")

        page = self.client.get(reverse("asset_detail", args=[self.asset.pk])).content.decode()
        self.assertNotIn(reverse("asset_delete", args=[self.asset.pk]), page)


class PersonDeleteTests(Base):
    def setUp(self):
        super().setUp()
        self.person = Person.objects.create(display_name="Dave")
        self.asset = Asset.objects.create(nickname="Aero")

    def test_somebody_no_vehicle_names_can_go(self):
        self.client.post(reverse("person_delete", args=[self.person.pk]))
        self.assertEqual(Person.objects.count(), 0)

    def test_a_current_owner_stays(self):
        """Ending an ownership records that a car changed hands; deleting the
        owner loses who used to have it."""
        AssetOwnership.objects.create(
            asset=self.asset, person=self.person, from_date=date(2020, 1, 1)
        )

        response = self.client.post(
            reverse("person_delete", args=[self.person.pk]), follow=True
        )

        self.assertEqual(Person.objects.count(), 1)
        self.assertContains(response, "End that on the vehicle first")

    def test_a_former_owner_may_go(self):
        AssetOwnership.objects.create(
            asset=self.asset, person=self.person,
            from_date=date(2020, 1, 1), to_date=date(2024, 1, 1),
        )
        self.client.post(reverse("person_delete", args=[self.person.pk]))
        self.assertEqual(Person.objects.count(), 0)
