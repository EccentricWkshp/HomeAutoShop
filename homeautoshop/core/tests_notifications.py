"""Reminders (SPEC FR-MAINT-10) — opt-in, deduplicated, and quiet by default."""

from __future__ import annotations

from datetime import date, timedelta
from unittest.mock import patch

from django.core import mail
from django.test import TestCase, override_settings
from django.utils import timezone

from homeautoshop.assets.models import Asset
from homeautoshop.assets.services import record_reading
from homeautoshop.maintenance.models import AssetServiceItem, ServiceDefinition
from homeautoshop.maintenance.services import recalculate

from .models import NotificationChannel, NotificationSent, Setting
from .notifications import collect, digest_for, run

SMTP = {"EMAIL_HOST": "smtp.example.test", "EMAIL_BACKEND": "django.core.mail.backends.locmem.EmailBackend"}


def overdue_item(asset) -> AssetServiceItem:
    definition = ServiceDefinition.objects.create(name="Engine oil and filter")
    item = AssetServiceItem.objects.create(
        asset=asset, definition=definition, interval_months=6, last_done_on=date(2020, 1, 1)
    )
    return recalculate(item)


class CollectionTests(TestCase):
    def setUp(self):
        self.asset = Asset.objects.create(nickname="Red truck", meter_unit="mi")
        record_reading(self.asset, 100_000)

    def test_an_overdue_service_becomes_an_alert(self):
        overdue_item(self.asset)
        digest = collect()
        titles = [a.title for a in digest.alerts]
        self.assertIn("Red truck: Engine oil and filter", titles)

    def test_an_expiring_registration_is_raised(self):
        self.asset.plate_expires_on = timezone.localdate() + timedelta(days=10)
        self.asset.save()
        keys = [a.dedupe_key for a in collect().alerts]
        self.assertTrue(any(k.startswith("registration:") for k in keys))

    def test_a_registration_far_out_is_not_raised_yet(self):
        self.asset.plate_expires_on = timezone.localdate() + timedelta(days=200)
        self.asset.save()
        keys = [a.dedupe_key for a in collect().alerts]
        self.assertFalse(any(k.startswith("registration:") for k in keys))

    def test_a_missing_backup_is_raised(self):
        keys = [a.dedupe_key for a in collect().alerts]
        self.assertTrue(any(k.startswith("backup:") for k in keys))

    def test_a_recent_backup_is_not(self):
        Setting.put("last_backup_at", timezone.now().isoformat())
        keys = [a.dedupe_key for a in collect().alerts]
        self.assertFalse(any(k.startswith("backup:") for k in keys))

    def test_urgent_items_drive_the_subject_line(self):
        overdue_item(self.asset)
        digest = collect()
        self.assertIn("need attention", digest.subject())


class DeliveryTests(TestCase):
    def setUp(self):
        self.asset = Asset.objects.create(nickname="Red truck")
        overdue_item(self.asset)
        Setting.put("last_backup_at", timezone.now().isoformat())
        self.channel = NotificationChannel.objects.create(
            name="Andy", kind=NotificationChannel.Kind.EMAIL,
            target="andy@example.test", is_enabled=True,
        )

    @override_settings(REMINDERS_ENABLED=True, **SMTP)
    def test_an_enabled_email_channel_receives_a_digest(self):
        result = run()
        self.assertEqual(result["sent"], 1)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("Red truck", mail.outbox[0].body)

    @override_settings(REMINDERS_ENABLED=True, **SMTP)
    def test_one_digest_not_one_message_per_item(self):
        """A stream of messages is how a notification system gets muted."""
        second = Asset.objects.create(nickname="The mower", asset_kind="equipment")
        overdue_item(second)
        run()
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("Red truck", mail.outbox[0].body)
        self.assertIn("The mower", mail.outbox[0].body)

    @override_settings(REMINDERS_ENABLED=True, **SMTP)
    def test_the_same_alert_is_not_repeated_within_the_cooldown(self):
        run()
        mail.outbox.clear()
        run()
        self.assertEqual(len(mail.outbox), 0)

    @override_settings(REMINDERS_ENABLED=True, REMINDER_COOLDOWN_DAYS=7, **SMTP)
    def test_it_is_raised_again_once_the_cooldown_lapses(self):
        run()
        NotificationSent.objects.update(sent_at=timezone.now() - timedelta(days=10))
        mail.outbox.clear()
        run()
        self.assertEqual(len(mail.outbox), 1)

    @override_settings(REMINDERS_ENABLED=True, **SMTP)
    def test_nothing_is_sent_when_there_is_nothing_to_say(self):
        """No 'all clear' mail — it trains people to ignore the next one."""
        AssetServiceItem.objects.all().delete()
        result = run()
        self.assertEqual(result["skipped"], "nothing to report")
        self.assertEqual(len(mail.outbox), 0)

    @override_settings(REMINDERS_ENABLED=False, **SMTP)
    def test_nothing_is_sent_while_reminders_are_disabled(self):
        self.assertEqual(run()["skipped"], "reminders disabled")
        self.assertEqual(len(mail.outbox), 0)

    @override_settings(REMINDERS_ENABLED=True, OFFLINE_MODE=True, **SMTP)
    def test_offline_mode_stops_everything(self):
        self.assertEqual(run()["skipped"], "offline mode")
        self.assertEqual(len(mail.outbox), 0)

    @override_settings(REMINDERS_ENABLED=True, **SMTP)
    def test_a_disabled_channel_receives_nothing(self):
        self.channel.is_enabled = False
        self.channel.save()
        self.assertEqual(run()["skipped"], "no enabled channels")

    @override_settings(REMINDERS_ENABLED=True, **SMTP)
    def test_a_channel_can_opt_out_of_routine_items(self):
        routine = Asset.objects.create(nickname="Quiet car")
        routine.plate_expires_on = timezone.localdate() + timedelta(days=200)
        routine.save()
        self.channel.include_routine = False
        self.channel.save()
        digest = digest_for(self.channel, collect())
        self.assertTrue(all(not a.is_routine for a in digest.alerts))


class WebhookTests(TestCase):
    def setUp(self):
        self.asset = Asset.objects.create(nickname="Red truck")
        overdue_item(self.asset)
        Setting.put("last_backup_at", timezone.now().isoformat())
        self.channel = NotificationChannel.objects.create(
            name="Home Assistant", kind=NotificationChannel.Kind.WEBHOOK,
            target="https://hass.example.test/hook", is_enabled=True,
        )

    @override_settings(REMINDERS_ENABLED=True)
    @patch("homeautoshop.core.outbound.post_json")
    def test_a_webhook_receives_structured_json(self, post):
        run()
        self.assertTrue(post.called)
        url, payload = post.call_args[0][0], post.call_args[0][1]
        self.assertEqual(url, "https://hass.example.test/hook")
        self.assertGreaterEqual(payload["count"], 1)
        self.assertIn("alerts", payload)
        self.assertIn("severity", payload["alerts"][0])

    @override_settings(REMINDERS_ENABLED=True)
    @patch("homeautoshop.core.outbound.post_json", side_effect=RuntimeError("connection refused"))
    def test_a_failing_channel_records_the_error_and_does_not_crash(self, _post):
        result = run()
        self.assertEqual(result["sent"], 0)
        self.channel.refresh_from_db()
        self.assertIn("connection refused", self.channel.last_error)
        # A failed send must not mark the alerts as said.
        self.assertEqual(NotificationSent.objects.count(), 0)


class MaskingTests(TestCase):
    def test_targets_are_masked_in_listings(self):
        """A webhook URL can carry a token in its path or query."""
        email = NotificationChannel(kind="email", target="andrew@example.test", name="A")
        hook = NotificationChannel(
            kind="webhook", target="https://hass.example.test/api/webhook/SECRETTOKEN", name="B"
        )
        self.assertNotIn("andrew", email.masked_target)
        # The whole path is withheld, not merely truncated: a short path would
        # otherwise leak the entire token.
        self.assertNotIn("SECRETTOKEN", hook.masked_target)
        self.assertNotIn("webhook", hook.masked_target)
        self.assertIn("hass.example.test", hook.masked_target)


class TheShelfReachesTheDigestTests(TestCase):
    """Brake fluid with a date on it and a part below its minimum were visible
    only to somebody who opened the parts pages and looked. Both are dated
    facts, which is what everything else in this digest already is."""

    def setUp(self):
        from homeautoshop.parts.models import Part, StockLot

        self.Part = Part
        self.StockLot = StockLot
        Setting.put("last_backup_at", timezone.now().isoformat())

    def _lot(self, name: str, *, expires, qty=2):
        part = self.Part.objects.create(name=name)
        return self.StockLot.objects.create(part=part, qty_on_hand=qty, expires_on=expires)

    def test_an_expired_lot_is_urgent(self):
        self._lot("DOT 4 brake fluid", expires=timezone.localdate() - timedelta(days=3))

        alert = next(a for a in collect().alerts if a.dedupe_key.startswith("lot:"))

        self.assertEqual(alert.severity, "overdue")
        self.assertIn("DOT 4 brake fluid", alert.title)
        self.assertFalse(alert.is_routine)

    def test_one_approaching_its_date_is_routine(self):
        self._lot("Threadlocker", expires=timezone.localdate() + timedelta(days=20))

        alert = next(a for a in collect().alerts if a.dedupe_key.startswith("lot:"))

        self.assertEqual(alert.severity, "warning")
        self.assertTrue(alert.is_routine)

    def test_a_date_far_out_is_not_raised_yet(self):
        self._lot("Epoxy", expires=timezone.localdate() + timedelta(days=400))

        keys = [a.dedupe_key for a in collect().alerts]

        self.assertFalse(any(k.startswith("lot:") for k in keys))

    def test_a_lot_with_no_date_is_never_raised(self):
        self._lot("Wheel nut", expires=None)

        keys = [a.dedupe_key for a in collect().alerts]

        self.assertFalse(any(k.startswith("lot:") for k in keys))

    def test_the_quantity_reads_as_somebody_typed_it(self):
        """The lot column is a Decimal stored to three places."""
        self._lot("Wiper blade", expires=timezone.localdate(), qty=1)

        alert = next(a for a in collect().alerts if a.dedupe_key.startswith("lot:"))

        self.assertIn("1 on hand", alert.detail)
        self.assertNotIn("1.000", alert.detail)

    def test_low_stock_is_one_alert_and_not_one_per_part(self):
        for name in ("Oil filter", "Air filter", "Cabin filter"):
            self.Part.objects.create(name=name, min_quantity=2)

        restock = [a for a in collect().alerts if a.dedupe_key.startswith("restock:")]

        self.assertEqual(len(restock), 1)
        self.assertIn("3", restock[0].title)
        self.assertIn("Oil filter", restock[0].detail)

    def test_a_shopping_list_is_routine(self):
        self.Part.objects.create(name="Oil filter", min_quantity=2)

        alert = next(a for a in collect().alerts if a.dedupe_key.startswith("restock:"))

        self.assertEqual(alert.severity, "info")
        self.assertTrue(alert.is_routine)

    def test_a_stocked_part_is_not_on_it(self):
        part = self.Part.objects.create(name="Oil filter", min_quantity=2)
        self.StockLot.objects.create(part=part, qty_on_hand=4)

        keys = [a.dedupe_key for a in collect().alerts]

        self.assertFalse(any(k.startswith("restock:") for k in keys))

    def test_a_part_going_low_tomorrow_is_not_silenced_by_todays_cooldown(self):
        """Keyed on which parts are low, not on the fact that any are — a fixed
        key would let one cooldown swallow every later shortage."""
        self.Part.objects.create(name="Oil filter", min_quantity=2)
        first = next(a for a in collect().alerts if a.dedupe_key.startswith("restock:"))

        self.Part.objects.create(name="Air filter", min_quantity=2)
        second = next(a for a in collect().alerts if a.dedupe_key.startswith("restock:"))

        self.assertNotEqual(first.dedupe_key, second.dedupe_key)

    def test_the_same_shelf_keeps_the_same_key(self):
        self.Part.objects.create(name="Oil filter", min_quantity=2)

        first = next(a for a in collect().alerts if a.dedupe_key.startswith("restock:"))
        second = next(a for a in collect().alerts if a.dedupe_key.startswith("restock:"))

        self.assertEqual(first.dedupe_key, second.dedupe_key)


class AnOpenRecallIsSaidOutLoudTests(TestCase):
    """A campaign already recorded against a vehicle and not yet answered. No
    lookup, no network, no interval — until now the only way to find out was to
    open one vehicle's recall page and read it."""

    def setUp(self):
        from homeautoshop.assets.models import Recall

        self.Recall = Recall
        self.asset = Asset.objects.create(nickname="Explorer")
        Setting.put("last_backup_at", timezone.now().isoformat())

    def _recall(self, **kwargs):
        fields = {
            "asset": self.asset,
            "campaign_number": "24V001",
            "component": "Fuel pump",
        }
        fields.update(kwargs)
        return self.Recall.objects.create(**fields)

    def test_it_reaches_the_digest(self):
        self._recall()

        alert = next(a for a in collect().alerts if a.dedupe_key.startswith("recall:"))

        self.assertEqual(alert.severity, "safety")
        self.assertIn("Explorer", alert.title)
        self.assertIn("Fuel pump", alert.detail)
        self.assertFalse(alert.is_routine)

    def test_one_already_dealt_with_is_not_raised(self):
        self._recall(owner_status=self.Recall.OwnerStatus.COMPLETED)

        keys = [a.dedupe_key for a in collect().alerts]

        self.assertFalse(any(k.startswith("recall:") for k in keys))

    def test_nor_one_that_does_not_apply_to_this_vin(self):
        self._recall(owner_status=self.Recall.OwnerStatus.NOT_APPLICABLE)

        keys = [a.dedupe_key for a in collect().alerts]

        self.assertFalse(any(k.startswith("recall:") for k in keys))

    def test_a_vehicle_that_is_no_longer_yours_is_not_raised(self):
        from homeautoshop.assets.models import AssetStatus

        self.asset.status = AssetStatus.SOLD
        self.asset.save()
        self._recall()

        keys = [a.dedupe_key for a in collect().alerts]

        self.assertFalse(any(k.startswith("recall:") for k in keys))

    def test_it_drives_the_subject_line(self):
        self._recall()

        self.assertIn("need attention", collect().subject())
