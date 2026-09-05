"""`manage.py purge_trash` — the thing that makes the retention window real.

The trash promised thirty days from the day it was written and enforced nothing:
`TRASH_RETENTION_DAYS` was read by one queryset method used by one screen, and
no command or job ever removed a row. So a soft delete was permanent in the
direction nobody wanted — the row stayed for ever, and there was no supported
way to make it leave.
"""

from __future__ import annotations

from datetime import timedelta
from io import StringIO

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase
from django.utils import timezone

from homeautoshop.assets.models import Asset
from homeautoshop.core.models import AuditLog
from homeautoshop.purchasing.models import Purchase, PurchaseLine, Vendor


def trashed(model, pk, *, days_ago: int) -> None:
    model.all_objects.filter(pk=pk).update(
        deleted_at=timezone.now() - timedelta(days=days_ago)
    )


class PurgeTrashTests(TestCase):
    def purge(self, *args, **kwargs) -> str:
        out = StringIO()
        call_command("purge_trash", *args, stdout=out, stderr=out, **kwargs)
        return out.getvalue()

    def test_it_reports_and_changes_nothing_without_yes(self):
        asset = Asset.objects.create(nickname="Parts car")
        asset.delete()
        trashed(Asset, asset.pk, days_ago=90)

        output = self.purge()

        self.assertIn("would purge", output)
        self.assertIn("assets.Asset", output)
        self.assertTrue(Asset.all_objects.filter(pk=asset.pk).exists())

    def test_and_removes_it_with_yes(self):
        asset = Asset.objects.create(nickname="Parts car")
        asset.delete()
        trashed(Asset, asset.pk, days_ago=90)

        self.purge("--yes")

        self.assertFalse(Asset.all_objects.filter(pk=asset.pk).exists())

    def test_the_retention_window_is_honored_rather_than_described(self):
        recent = Asset.objects.create(nickname="Recent")
        recent.delete()
        trashed(Asset, recent.pk, days_ago=5)

        self.purge("--yes")

        self.assertTrue(Asset.all_objects.filter(pk=recent.pk).exists())

    def test_a_live_record_is_never_touched(self):
        asset = Asset.objects.create(nickname="Daily driver")

        self.purge("--yes", "--days", "0")

        self.assertTrue(Asset.objects.filter(pk=asset.pk).exists())

    def test_children_go_before_the_things_they_hang_off(self):
        """A hard delete is the first moment the database's own `on_delete`
        rules apply to these rows, so the order matters. A purchase and its
        lines have to come out together and in the right sequence."""
        vendor = Vendor.objects.create(name="RockAuto")
        purchase = Purchase.objects.create(vendor=vendor, order_number="123")
        PurchaseLine.objects.create(
            purchase=purchase, description_as_ordered="Gasket", extended_minor=400
        )
        purchase.delete()
        trashed(Purchase, purchase.pk, days_ago=90)
        trashed(PurchaseLine, purchase.lines(manager="all_objects").first().pk, days_ago=90)

        self.purge("--yes")

        self.assertFalse(Purchase.all_objects.filter(pk=purchase.pk).exists())
        self.assertFalse(PurchaseLine.all_objects.filter(purchase_id=purchase.pk).exists())

    def test_it_says_what_it_removed_in_the_audit_log(self):
        asset = Asset.objects.create(nickname="Parts car")
        asset.delete()
        trashed(Asset, asset.pk, days_ago=90)

        self.purge("--yes")

        entry = AuditLog.objects.filter(entity_id=asset.pk).first()
        self.assertIsNotNone(entry)
        self.assertEqual(entry.action, AuditLog.Action.DELETE)
        self.assertIn("purged from trash", entry.summary)

    def test_one_model_at_a_time_is_allowed(self):
        asset = Asset.objects.create(nickname="Parts car")
        asset.delete()
        trashed(Asset, asset.pk, days_ago=90)
        vendor = Vendor.objects.create(name="RockAuto")
        vendor.delete()
        trashed(Vendor, vendor.pk, days_ago=90)

        self.purge("--yes", "--model", "purchasing.Vendor")

        self.assertFalse(Vendor.all_objects.filter(pk=vendor.pk).exists())
        self.assertTrue(Asset.all_objects.filter(pk=asset.pk).exists())

    def test_a_model_that_does_not_soft_delete_is_refused_by_name(self):
        with self.assertRaises(CommandError):
            self.purge("--model", "core.AuditLog")

    def test_a_negative_window_is_refused(self):
        with self.assertRaises(CommandError):
            self.purge("--days", "-1")

    def test_an_empty_trash_says_so(self):
        self.assertIn("Nothing in the trash", self.purge())
