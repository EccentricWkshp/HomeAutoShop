"""`manage.py purge_order` — clearing up after an import that went wrong.

The state these tests reconstruct is the one that prompted the command: an order
deleted from the Django admin, which soft-deleted the purchase, cascaded to
nothing, and left the lines alive and marked received under a purchase every
screen hid — along with the stock, the tooling expense and the provenance row
that made the importer refuse to read the document again.
"""

from __future__ import annotations

from io import StringIO

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from homeautoshop.core.models import AuditLog, ExternalRef
from homeautoshop.parts.models import Part, StockLot, StockTransaction
from homeautoshop.purchasing.models import Expense, Purchase, PurchaseLine, Vendor

ORDER = "205-1234567-0000001"


class PurgeOrderTests(TestCase):
    def setUp(self):
        self.vendor = Vendor.objects.create(name="RockAuto")
        self.part = Part.objects.create(name="Water pump", part_number="AW9445")
        self.purchase = Purchase.objects.create(
            vendor=self.vendor, order_number=ORDER, tax_minor=100, tax_currency="USD"
        )
        self.line = PurchaseLine.objects.create(
            purchase=self.purchase,
            part=self.part,
            description_as_ordered="Water pump",
            qty_ordered=2,
            extended_minor=8000,
            extended_currency="USD",
        )
        self.ref = ExternalRef.objects.create(
            source_system="rockauto",
            source_instance_url="",
            external_type="order",
            external_id=ORDER,
            entity_type="Purchase",
            entity_id=self.purchase.pk,
        )

    def purge(self, *args):
        out = StringIO()
        call_command("purge_order", ORDER, *args, stdout=out, stderr=out)
        return out.getvalue()

    def orphan_it(self):
        """The admin delete, exactly as it behaved: purchase into the trash,
        lines left alive underneath it, provenance row untouched."""
        Purchase.all_objects.filter(pk=self.purchase.pk).update(
            deleted_at="2026-01-01T00:00:00+00:00"
        )

    def test_it_reports_the_footprint_and_changes_nothing(self):
        output = self.purge()

        self.assertIn("purchase line", output)
        self.assertIn("Nothing changed", output)
        self.assertTrue(PurchaseLine.all_objects.filter(pk=self.line.pk).exists())

    def test_it_finds_an_order_whose_purchase_is_in_the_trash(self):
        self.orphan_it()

        output = self.purge()

        self.assertIn("in the trash", output)

    def test_and_removes_all_of_it(self):
        self.orphan_it()

        self.purge("--yes")

        self.assertFalse(Purchase.all_objects.filter(pk=self.purchase.pk).exists())
        self.assertFalse(PurchaseLine.all_objects.filter(pk=self.line.pk).exists())
        self.assertFalse(ExternalRef.objects.filter(pk=self.ref.pk).exists())

    def test_the_stock_a_receipt_created_goes_with_it(self):
        lot = self.line.receive(qty=1)

        self.purge("--yes")

        self.assertFalse(StockLot.all_objects.filter(pk=lot.pk).exists())
        self.assertFalse(StockTransaction.all_objects.filter(stock_lot_id=lot.pk).exists())

    def test_but_not_stock_that_has_since_moved(self):
        """Deleting it would silently change what the shop believes it has and
        what it believes that cost."""
        lot = self.line.receive(qty=2)
        StockTransaction.record(lot, -1, StockTransaction.Reason.CONSUME)

        with self.assertRaises(CommandError):
            self.purge("--yes")

        self.assertTrue(StockLot.all_objects.filter(pk=lot.pk).exists())

    def test_unless_that_is_asked_for_outright(self):
        lot = self.line.receive(qty=2)
        StockTransaction.record(lot, -1, StockTransaction.Reason.CONSUME)

        self.purge("--yes", "--force-stock")

        self.assertFalse(StockLot.all_objects.filter(pk=lot.pk).exists())

    def test_the_part_it_created_is_kept(self):
        """A part outlives the order that first stocked it: it is a thing the
        shop knows about, not a line on a receipt."""
        self.purge("--yes")

        self.assertTrue(Part.objects.filter(pk=self.part.pk).exists())

    def test_and_so_is_the_vendor(self):
        self.purge("--yes")

        self.assertTrue(Vendor.objects.filter(pk=self.vendor.pk).exists())

    def test_a_tooling_expense_is_removed_with_its_provenance(self):
        expense = Expense.objects.create(
            vendor=self.vendor, amount_minor=1799, amount_currency="USD", category="tooling"
        )
        ExternalRef.objects.create(
            source_system="rockauto",
            source_instance_url="",
            external_type="tooling-line",
            external_id=f"{ORDER}:0",
            entity_type="Expense",
            entity_id=expense.pk,
        )

        self.purge("--yes")

        self.assertFalse(Expense.all_objects.filter(pk=expense.pk).exists())
        self.assertFalse(ExternalRef.objects.filter(external_type="tooling-line").exists())

    def test_it_is_written_down(self):
        self.purge("--yes")

        entry = AuditLog.objects.filter(entity_id=self.purchase.pk).first()
        self.assertIsNotNone(entry)
        self.assertEqual(entry.action, AuditLog.Action.DELETE)
        self.assertIn(ORDER, entry.summary)

    def test_an_order_nothing_knows_about_is_said_so_rather_than_reported_empty(self):
        with self.assertRaises(CommandError):
            call_command("purge_order", "no-such-order", stdout=StringIO())

    def test_a_stale_ref_with_no_purchase_left_is_still_cleared(self):
        """The other half of the mess: the purchase hard-deleted somehow, the
        provenance row left behind, and the importer still refusing on it."""
        PurchaseLine.all_objects.filter(pk=self.line.pk).hard_delete()
        Purchase.all_objects.filter(pk=self.purchase.pk).hard_delete()

        self.purge("--yes")

        self.assertFalse(ExternalRef.objects.filter(pk=self.ref.pk).exists())
