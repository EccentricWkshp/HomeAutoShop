"""Authorization, ownership, trash, restore, and the i18n guard."""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

from django.core.management import call_command
from django.db import connection
from django.test import TestCase, TransactionTestCase, override_settings
from django.urls import reverse

from homeautoshop.assets.models import Asset, AssetOwnership
from homeautoshop.people.models import Person

from .models import Role, User, can


class PolicyTests(TestCase):
    """SPEC §12.2 — every decision goes through can()."""

    def setUp(self):
        self.admin = User.objects.create_user("root", password="correct-horse-battery", role=Role.ADMIN)
        self.member = User.objects.create_user("andy", password="correct-horse-battery")

    def test_members_do_the_work_but_not_the_administration(self):
        self.assertTrue(can(self.member, "asset.create"))
        self.assertTrue(can(self.member, "work_order.edit"))
        self.assertFalse(can(self.member, "user.manage"))
        self.assertFalse(can(self.member, "backup.manage"))

    def test_admins_may_do_everything(self):
        self.assertTrue(can(self.admin, "user.manage"))
        self.assertTrue(can(self.admin, "trash.manage"))

    def test_anonymous_and_deactivated_users_may_do_nothing(self):
        self.assertFalse(can(None, "asset.create"))
        self.member.is_active = False
        self.assertFalse(can(self.member, "asset.create"))

    def test_deactivating_a_user_does_not_destroy_their_records(self):
        """FR-ADM-2 — authored work outlives the account."""
        asset = Asset.objects.create(nickname="Truck", created_by=self.member)
        self.member.is_active = False
        self.member.save()
        asset.refresh_from_db()
        self.assertEqual(asset.created_by_id, self.member.pk)


class OwnershipTests(TestCase):
    """FR-OWN-1/2 — ownership is dated history, not a foreign key."""

    def setUp(self):
        self.user = User.objects.create_user("andy", password="correct-horse-battery")
        self.client.force_login(self.user)
        self.asset = Asset.objects.create(nickname="Red truck")
        self.alice = Person.objects.create(display_name="Alice")
        self.bob = Person.objects.create(display_name="Bob")

    def test_adding_a_second_owner_closes_the_first_run(self):
        self.client.post(reverse("ownership_add", args=[self.asset.pk]), {"person": self.alice.pk, "role": "owner"})
        self.client.post(reverse("ownership_add", args=[self.asset.pk]), {"person": self.bob.pk, "role": "owner"})

        self.assertEqual(self.asset.ownerships.count(), 2)
        current = self.asset.ownerships.filter(to_date__isnull=True)
        self.assertEqual(current.count(), 1)
        self.assertEqual(current.first().person, self.bob)
        # The history is preserved, not overwritten.
        self.assertEqual(self.asset.current_owner(), self.bob)
        self.assertIn(self.asset, self.alice.former_assets())

    def test_a_driver_does_not_displace_the_owner(self):
        self.client.post(reverse("ownership_add", args=[self.asset.pk]), {"person": self.alice.pk, "role": "owner"})
        self.client.post(reverse("ownership_add", args=[self.asset.pk]), {"person": self.bob.pk, "role": "primary_driver"})
        self.assertEqual(self.asset.current_owner(), self.alice)

    def test_ending_ownership_keeps_the_record(self):
        self.client.post(reverse("ownership_add", args=[self.asset.pk]), {"person": self.alice.pk, "role": "owner"})
        row = self.asset.ownerships.first()
        self.client.post(reverse("ownership_end", args=[self.asset.pk, row.pk]))
        row.refresh_from_db()
        self.assertIsNotNone(row.to_date)
        self.assertTrue(AssetOwnership.objects.filter(pk=row.pk).exists())


class TrashTests(TestCase):
    """FR-ADM-7 — a 30-day trash with restore."""

    def setUp(self):
        self.admin = User.objects.create_user("root", password="correct-horse-battery", role=Role.ADMIN)
        self.member = User.objects.create_user("andy", password="correct-horse-battery")

    def test_admin_can_restore_a_deleted_asset(self):
        asset = Asset.objects.create(nickname="Parts car")
        asset.delete()
        self.client.force_login(self.admin)

        response = self.client.get(reverse("trash"))
        self.assertContains(response, "Parts car")

        self.client.post(reverse("trash_restore", args=["asset", asset.pk]))
        self.assertTrue(Asset.objects.filter(pk=asset.pk).exists())

    def test_members_cannot_reach_the_trash(self):
        self.client.force_login(self.member)
        # 403, not 404: the page exists, this user may not have it.
        self.assertEqual(self.client.get(reverse("trash")).status_code, 403)

    def test_restore_requires_post(self):
        """405, not 404.

        This view used to hand-roll its method check and answer 404, which says
        "there is nothing here" about a route that exists. It now uses the same
        `@require_POST` as the other thirty write views, so a GET gets the
        accurate answer and the codebase has one way of saying it.
        """
        asset = Asset.objects.create(nickname="Parts car")
        asset.delete()
        self.client.force_login(self.admin)
        self.assertEqual(
            self.client.get(reverse("trash_restore", args=["asset", asset.pk])).status_code, 405
        )
        self.assertFalse(Asset.objects.filter(pk=asset.pk).exists())


class BackupRestoreTests(TransactionTestCase):
    """SPEC §13.1/§13.2 — the backup captures data and the restore guards hold.

    TransactionTestCase rather than TestCase: backup checkpoints the WAL and
    copies the database file, which a transaction-wrapped test cannot honestly
    represent — the rows would not be committed for the copy to contain them.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.override = override_settings(
            BACKUP_DIR=self.tmp / "backups", MEDIA_ROOT=self.tmp / "media"
        )
        self.override.enable()
        (self.tmp / "media").mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        self.override.disable()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_backup_captures_real_rows_readable_without_django(self):
        """The dump is a database, not a marker file — verified by reading it.

        Runs on whichever backend is configured. Written SQLite-only, it passed
        for months while `pg_dump` was refusing to run against the production
        target — a backup suite that only tests the development database is
        testing the wrong thing.
        """
        import json

        from homeautoshop.core.backup import run_backup

        Asset.objects.create(nickname="Red truck")
        target = run_backup()

        manifest = json.loads((target / "manifest.json").read_text())
        self.assertEqual(manifest["schema_version"], 1)
        self.assertEqual(manifest["vendor"], connection.vendor)

        dump = target / self._dump_name()
        self.assertTrue(dump.exists(), "backup produced no database file")
        self.assertGreater(dump.stat().st_size, 0, "backup file is empty")

        if connection.vendor == "sqlite":
            # Open the copy directly: if the backup is good, the row is in it.
            import sqlite3

            conn = sqlite3.connect(dump)
            try:
                names = [r[0] for r in conn.execute("SELECT nickname FROM assets_asset")]
            finally:
                conn.close()
            self.assertIn("Red truck", names)
        else:
            # pg_restore reads the archive without a server, which is the same
            # promise: the file is a database, not a marker.
            import subprocess

            listing = subprocess.run(
                ["pg_restore", "--list", str(dump)],
                capture_output=True,
                text=True,
                check=True,
            ).stdout
            self.assertIn("assets_asset", listing)

    def test_a_failed_dump_says_why(self):
        """An exit status alone leaves an operator with nowhere to go."""
        if connection.vendor == "sqlite":
            self.skipTest("the SQLite path is a file copy, with no subprocess")

        from unittest.mock import patch as mock_patch

        from homeautoshop.core.backup import BackupFailed, run_backup

        failure = subprocess.CompletedProcess(
            args=["pg_dump"], returncode=1, stdout="", stderr="server version mismatch"
        )
        with mock_patch("homeautoshop.core.backup.subprocess.run", return_value=failure):
            with self.assertRaises(BackupFailed) as ctx:
                run_backup()
        self.assertIn("server version mismatch", str(ctx.exception))

    def test_backup_records_its_age_for_the_dashboard_warning(self):
        from homeautoshop.core.backup import last_backup_age_days, run_backup

        self.assertIsNone(last_backup_age_days())
        run_backup()
        self.assertLess(last_backup_age_days(), 1)

    @staticmethod
    def _dump_name() -> str:
        """What `run_backup` calls the database half, per backend."""
        return "database.sqlite3" if connection.vendor == "sqlite" else "database.dump"

    def _fake_backup(self) -> Path:
        """A structurally valid backup, so the guards can be tested in isolation.

        Named for the configured backend: a fixture that always wrote
        `database.sqlite3` made every guard test fail on Postgres for a reason
        that had nothing to do with the guard.
        """
        import json

        target = self.tmp / "backups" / "20260101-000000"
        target.mkdir(parents=True, exist_ok=True)
        (target / "manifest.json").write_text(
            json.dumps({"created_at": "2026-01-01T00:00:00", "vendor": connection.vendor,
                        "schema_version": 1}),
            encoding="utf-8",
        )
        (target / self._dump_name()).write_bytes(b"SQLite" if connection.vendor == "sqlite" else b"PGDMP")
        return target

    def test_restore_refuses_a_directory_that_is_not_a_backup(self):
        from django.core.management.base import CommandError

        junk = self.tmp / "junk"
        junk.mkdir()
        with self.assertRaises(CommandError) as ctx:
            call_command("restore", junk)
        self.assertIn("manifest.json", str(ctx.exception))

    def test_restore_refuses_a_mismatched_schema_version(self):
        import json

        from django.core.management.base import CommandError

        target = self._fake_backup()
        (target / "manifest.json").write_text(
            json.dumps({"vendor": connection.vendor, "schema_version": 99})
        )
        with self.assertRaises(CommandError) as ctx:
            call_command("restore", target)
        self.assertIn("schema version", str(ctx.exception))

    def test_restore_refuses_to_overwrite_a_populated_instance(self):
        """A partial or accidental restore is worse than none."""
        from django.core.management.base import CommandError

        Asset.objects.create(nickname="Red truck")
        with self.assertRaises(CommandError) as ctx:
            call_command("restore", self._fake_backup())
        self.assertIn("--force", str(ctx.exception))

    def test_dry_run_reports_without_changing_anything(self):
        Asset.objects.create(nickname="Red truck")
        call_command("restore", self._fake_backup(), "--dry-run", "--force")
        self.assertTrue(Asset.objects.filter(nickname="Red truck").exists())


class TranslationGuardTests(TestCase):
    """SPEC §5.6 — a discipline survives only if a check enforces it."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "homeautoshop").mkdir()
        (self.tmp / "templates").mkdir()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self) -> int:
        with override_settings(BASE_DIR=self.tmp):
            try:
                call_command("check_translations")
            except SystemExit as exc:
                return int(str(exc).split()[0])
            return 0

    def test_the_check_actually_catches_an_unwrapped_message(self):
        (self.tmp / "homeautoshop" / "views.py").write_text(
            "from django.contrib import messages\n"
            "def v(request):\n"
            "    messages.success(request, 'Your changes were saved.')\n",
            encoding="utf-8",
        )
        self.assertEqual(self._run(), 1)

    def test_a_wrapped_message_passes(self):
        (self.tmp / "homeautoshop" / "views.py").write_text(
            "from django.contrib import messages\n"
            "from django.utils.translation import gettext as _\n"
            "def v(request):\n"
            "    messages.success(request, _('Your changes were saved.'))\n",
            encoding="utf-8",
        )
        self.assertEqual(self._run(), 0)

    def test_short_identifiers_are_not_flagged_as_prose(self):
        (self.tmp / "homeautoshop" / "views.py").write_text(
            "from django.contrib import messages\n"
            "def v(request):\n"
            "    messages.success(request, 'ok')\n",
            encoding="utf-8",
        )
        self.assertEqual(self._run(), 0)

    def test_the_real_codebase_passes(self):
        call_command("check_translations")


class AccessibilityGuardTests(TestCase):
    """SPEC §9.5 targets WCAG 2.1 AA.

    Most of that is a judgment a script cannot make. This holds down the part
    that is unambiguous — every control has a name, every image has an alt — for
    the same reason the translation guard exists: a discipline nobody can verify
    stops being true, quietly, one hurried form at a time.
    """

    def test_the_templates_pass(self):
        call_command("check_accessibility")
