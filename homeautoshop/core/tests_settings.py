"""
Instance settings and backup from the UI (SPEC §17, R-9, §17.1, §17.2, R-10).

The risks here are not "does the form save". They are:

* **Lockout.** A typo in a settings field must not be able to brick an
  instance, which is why §17.1 demands a typed registry rather than free text.
* **Leak.** Credentials move into the database, and the database is dumped
  physically — so "we did not ask for it" excludes nothing. §17.1 reduces the
  whole question to one assertion, and it is made below: *no backup and no
  export contains credential material.*
* **Lying.** A setting that displays one value while the process runs another
  is worse than one that needs a restart and says so.
"""

from __future__ import annotations

import io
import json
import shutil
import sqlite3
import subprocess
import tempfile
import zipfile
from pathlib import Path
from unittest import mock

from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.db import connection
from django.test import TestCase, TransactionTestCase, override_settings
from django.urls import reverse

from homeautoshop.accounts.models import Role, User

from . import runtime
from .backup import (
    DUMP_MAGIC,
    SCHEMA_VERSION,
    UploadRejected,
    assemble_uploaded,
    build_export,
    run_backup,
)
from .models import AuditLog, Credential, Job, Setting
from .settings_registry import BY_KEY, REGISTRY, RESTART_KEYS, children_of, coerce

SECRET = "wlk_live_do_not_leak_this"


class RuntimeMixin:
    """Settings live in module state as well as in the database."""

    def setUp(self):
        super().setUp()
        runtime.invalidate()
        runtime._booted_generation = None
        runtime._overlay_applied = False
        self.addCleanup(runtime.invalidate)
        self.addCleanup(setattr, runtime, "_booted_generation", None)
        self.addCleanup(setattr, runtime, "_overlay_applied", False)


class RuntimeBase(RuntimeMixin, TestCase):
    pass


class BackupBase(RuntimeMixin, TransactionTestCase):
    """Anything that actually takes a backup.

    `run_backup` checkpoints the WAL and copies the database file, and SQLite
    will not do that from inside the transaction a `TestCase` wraps every test
    in — it reports the table as locked. The backup path is worth exercising
    for real rather than mocking, so these tests pay for a real transaction.
    """


class PrecedenceTests(RuntimeBase):
    """database → environment → default (R-9)."""

    @override_settings(SHOP_NAME="From the environment")
    def test_an_untouched_instance_behaves_exactly_as_before(self):
        """The whole reason registry keys are Django settings names.

        With no stored row there is no second copy of the defaults to drift.
        """
        self.assertEqual(runtime.conf.SHOP_NAME, "From the environment")

    @override_settings(SHOP_NAME="From the environment")
    def test_a_stored_value_wins(self):
        runtime.save({"SHOP_NAME": "The garage"})
        self.assertEqual(runtime.conf.SHOP_NAME, "The garage")

    @override_settings(SHOP_NAME="From the environment")
    def test_and_the_environment_returns_when_the_row_goes(self):
        runtime.save({"SHOP_NAME": "The garage"})
        Setting.objects.filter(key="SHOP_NAME").delete()
        runtime.invalidate()
        self.assertEqual(runtime.conf.SHOP_NAME, "From the environment")

    def test_a_setting_nobody_moved_still_reads_through(self):
        """`conf` is a lens over Django's settings, not a replacement for them."""
        self.assertEqual(runtime.conf.EMAIL_TIMEOUT, 15)

    def test_an_unknown_name_still_raises_rather_than_returning_none(self):
        with self.assertRaises(AttributeError):
            runtime.conf.NO_SUCH_SETTING


class LiveChangeTests(RuntimeBase):
    """§17.2 — an immediate setting is immediate, with nothing restarted."""

    def test_offline_mode_takes_effect_without_a_restart(self):
        """NFR-S-2 is an emergency control. It was behind a text editor.

        This is the end-to-end version: throw the switch, and the very next
        outbound call is refused by the same code path the app uses.
        """
        from .outbound import OutboundBlocked, fetch_json

        runtime.save({"OFFLINE_MODE": True})
        with self.assertRaises(OutboundBlocked):
            fetch_json("https://vpic.nhtsa.dot.gov/api/vehicles/anything")

    def test_a_new_integration_address_is_allowed_through_at_once(self):
        """The allowlist is derived from the addresses, which are now editable.

        Computed at import, a LubeLogger saved on the settings screen would be
        configured, enabled, and refused — the worst of the three states.
        """
        runtime.save({"LUBELOGGER_URL": "https://lubelogger.home.arpa"})
        self.assertIn("lubelogger.home.arpa", runtime.allowlist())

    def test_the_schedule_follows_the_setting_on_the_next_pass(self):
        from .schedule import recurring

        runtime.save({"BACKUP_INTERVAL_HOURS": 6})
        plan = dict(recurring())
        self.assertEqual(plan["backup.run"].total_seconds(), 6 * 3600)


class ValidationTests(RuntimeBase):
    """§17.1 — a free-text row with no schema is a way to brick an instance."""

    def test_a_number_outside_its_range_is_refused(self):
        with self.assertRaises(ValidationError):
            runtime.save({"REMINDER_COOLDOWN_DAYS": 100_000})

    def test_and_nothing_else_in_the_batch_is_written(self):
        """A form posts a whole group. Half-applying it leaves a state
        nobody chose."""
        with self.assertRaises(ValidationError):
            runtime.save({"SHOP_NAME": "Fine", "REMINDER_COOLDOWN_DAYS": -5})
        self.assertFalse(Setting.objects.filter(key="SHOP_NAME").exists())

    def test_a_choice_outside_its_options_is_refused(self):
        with self.assertRaises(ValidationError):
            runtime.save({"LUBELOGGER_MODE": "whatever"})

    def test_a_timezone_that_does_not_exist_is_refused(self):
        """The setting a typo would most quietly corrupt every timestamp with."""
        with self.assertRaises(ValidationError):
            runtime.save({"TIME_ZONE": "America/Chicargo"})

    def test_a_real_one_is_accepted(self):
        runtime.save({"TIME_ZONE": "America/Chicago"})
        self.assertEqual(runtime.conf.TIME_ZONE, "America/Chicago")

    def test_an_address_without_a_scheme_is_refused(self):
        with self.assertRaises(ValidationError):
            runtime.save({"LUBELOGGER_URL": "lubelogger.home.arpa"})

    def test_a_currency_that_is_not_a_code_is_refused(self):
        with self.assertRaises(ValidationError):
            runtime.save({"CURRENCY_REPORTING": "dollars"})

    def test_a_language_code_of_the_wrong_shape_is_refused(self):
        with self.assertRaises(ValidationError):
            runtime.save({"OCR_LANGUAGES": "english"})

    def test_the_error_says_what_to_do_rather_than_naming_a_type(self):
        with self.assertRaises(ValidationError) as caught:
            coerce(BY_KEY["REMINDER_COOLDOWN_DAYS"], "seven")
        self.assertIn("whole number", " ".join(caught.exception.messages))

    def test_a_key_that_is_not_in_the_registry_is_ignored_not_stored(self):
        """The form is generated from the registry; a post is not trusted to be."""
        runtime.save({"SECRET_KEY": "hijacked"})
        self.assertFalse(Setting.objects.filter(key="SECRET_KEY").exists())


class AuditTests(RuntimeBase):
    """§17.1 — every change writes to `audit_log`, credentials as *changed*."""

    def setUp(self):
        super().setUp()
        self.user = User.objects.create_user(username="andy", password="x" * 16, role=Role.ADMIN)

    def test_it_records_who_and_from_what_to_what(self):
        with override_settings(SHOP_NAME="Before"):
            runtime.save({"SHOP_NAME": "After"}, user=self.user)
        entry = AuditLog.objects.get(entity_type="Setting")
        self.assertEqual(entry.user, self.user)
        self.assertEqual(entry.summary, "SHOP_NAME")
        self.assertEqual(entry.diff, {"from": "Before", "to": "After"})

    def test_offline_mode_is_the_case_that_most_needs_an_answer_to_who(self):
        runtime.save({"OFFLINE_MODE": True}, user=self.user)
        entry = AuditLog.objects.get(entity_type="Setting", summary="OFFLINE_MODE")
        self.assertEqual(entry.user, self.user)
        self.assertIs(entry.diff["to"], True)

    def test_a_credential_is_recorded_as_changed_and_never_quoted(self):
        runtime.save({"WRENCHLEDGER_API_KEY": SECRET}, user=self.user)
        entry = AuditLog.objects.get(entity_type="Setting", summary="WRENCHLEDGER_API_KEY")
        self.assertNotIn(SECRET, str(entry.diff))
        self.assertTrue(entry.diff["secret"])
        self.assertTrue(entry.diff["now_set"])

    def test_setting_the_same_value_again_writes_nothing(self):
        runtime.save({"SHOP_NAME": "Same"}, user=self.user)
        AuditLog.objects.all().delete()
        self.assertEqual(runtime.save({"SHOP_NAME": "Same"}, user=self.user), [])
        self.assertFalse(AuditLog.objects.exists())


class CredentialTests(RuntimeBase):
    """§17.1 — encrypted at rest, and stripped from every artifact."""

    def test_it_is_not_stored_in_the_clear(self):
        runtime.save({"WRENCHLEDGER_API_KEY": SECRET})
        row = Credential.objects.get(key="WRENCHLEDGER_API_KEY")
        self.assertNotIn(SECRET, row.ciphertext)
        self.assertEqual(runtime.conf.WRENCHLEDGER_API_KEY, SECRET)

    def test_it_is_not_a_setting_row(self):
        """Not fussiness — it is what makes the whole-table exclusions below
        possible instead of per-field surgery on every row."""
        runtime.save({"WRENCHLEDGER_API_KEY": SECRET})
        self.assertFalse(Setting.objects.filter(key="WRENCHLEDGER_API_KEY").exists())

    def test_rotating_the_key_invalidates_every_stored_credential(self):
        """The intended emergency behaviour, and it must read as
        *not configured* rather than as a crash."""
        runtime.save({"WRENCHLEDGER_API_KEY": SECRET})
        with override_settings(CREDENTIAL_KEY="a-completely-different-key"):
            self.assertEqual(runtime.credential_get("WRENCHLEDGER_API_KEY"), None)

    def test_clearing_it_removes_the_row_rather_than_storing_an_empty_one(self):
        runtime.save({"WRENCHLEDGER_API_KEY": SECRET})
        runtime.save({"WRENCHLEDGER_API_KEY": ""})
        self.assertFalse(Credential.objects.filter(key="WRENCHLEDGER_API_KEY").exists())

    def test_a_restored_instance_says_which_integrations_need_re_authenticating(self):
        """Because they are stripped on purpose, this is the *normal* state
        after a restore — not an error to be discovered in the log."""
        runtime.save({"WRENCHLEDGER_API_KEY": SECRET})
        # What a restore leaves behind: the configuration, without the key.
        Credential.objects.all().delete()
        runtime.invalidate()
        self.assertEqual(
            [item["key"] for item in runtime.unauthenticated_integrations()],
            ["WRENCHLEDGER_API_KEY"],
        )

    def test_it_can_tell_that_apart_from_never_having_been_configured(self):
        """Otherwise a fresh instance nags about integrations nobody wanted.

        The key *is* the on-switch for most of these, so with the table empty
        there is nothing left to infer from — which is why setting one also
        writes a plain marker that does survive the backup.
        """
        self.assertEqual(runtime.unauthenticated_integrations(), [])

    def test_the_marker_that_makes_that_possible_carries_nothing_secret(self):
        runtime.save({"WRENCHLEDGER_API_KEY": SECRET})
        marker = Setting.objects.get(key=f"{runtime.CONFIGURED_PREFIX}WRENCHLEDGER_API_KEY")
        self.assertEqual(marker.value, {"v": True})

    def test_and_it_goes_when_the_credential_is_deliberately_removed(self):
        """Removing a key is a decision, not a thing to be nagged about."""
        runtime.save({"WRENCHLEDGER_API_KEY": SECRET})
        runtime.save({"WRENCHLEDGER_API_KEY": ""})
        self.assertEqual(runtime.unauthenticated_integrations(), [])


class ArtifactsCarryNoCredentialsTests(BackupBase):
    """§17.1's single assertion, made against the produced artifact."""

    def setUp(self):
        super().setUp()
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.storage = override_settings(
            BACKUP_DIR=self.tmp,
            MEDIA_ROOT=self.tmp / "media",
            STORAGES={"default": {"BACKEND": "django.core.files.storage.FileSystemStorage"}},
        )
        self.storage.enable()
        self.addCleanup(self.storage.disable)
        runtime.save({"WRENCHLEDGER_API_KEY": SECRET})

    def test_the_export_does_not_contain_it(self):
        archive = build_export()
        with zipfile.ZipFile(archive) as zf:
            blob = b"".join(zf.read(name) for name in zf.namelist())
        self.assertNotIn(SECRET.encode(), blob)

    def test_the_export_skips_the_table_outright_rather_than_redacting_rows(self):
        archive = build_export()
        with zipfile.ZipFile(archive) as zf:
            self.assertNotIn("data/core.Credential.ndjson", zf.namelist())

    def test_the_backup_does_not_contain_it(self):
        """Run against whichever backend this instance actually uses.

        The two halves are excluded by completely different mechanisms — a
        `pg_dump --exclude-table-data` flag, and deleting from the *copied*
        SQLite file — so testing only the one the laptop happens to use would
        leave the deployed path unasserted. This scans every byte of whatever
        landed on disk, which is the assertion §17.1 actually asks for.
        """
        target = run_backup()
        written = [path for path in target.rglob("*") if path.is_file()]
        self.assertTrue(written, "the backup produced no files at all")
        for path in written:
            self.assertNotIn(SECRET.encode(), path.read_bytes(), f"{path.name} carries it")

    def test_and_the_live_database_still_has_it(self):
        """The copy is stripped, never the source. Getting this backwards
        would delete every credential in the shop to make a clean backup."""
        run_backup()
        self.assertEqual(runtime.conf.WRENCHLEDGER_API_KEY, SECRET)

    def test_the_backup_keeps_the_table_so_a_restore_has_somewhere_to_put_them(self):
        """Schema kept, rows not. A restore needs somewhere to put the keys
        that are about to be typed in again."""
        target = run_backup()
        if connection.vendor == "sqlite":
            with sqlite3.connect(target / "database.sqlite3") as handle:
                names = {row[0] for row in handle.execute("SELECT name FROM sqlite_master")}
            self.assertIn("core_credential", names)
            return

        listing = subprocess.run(
            ["pg_restore", "--list", str(target / "database.dump")],
            capture_output=True, text=True, check=True,
        ).stdout
        entries = [line for line in listing.splitlines() if "core_credential" in line]
        self.assertTrue(
            any("TABLE public core_credential" in line for line in entries),
            f"the table itself is missing from the dump: {entries}",
        )
        self.assertFalse(
            [line for line in entries if "TABLE DATA" in line],
            "the dump carries credential rows",
        )


class RestartTests(RuntimeBase):
    """§17.2 — the UI never leaves someone guessing which kind they have."""

    def test_an_immediate_change_does_not_raise_a_banner(self):
        runtime.record_generation()
        runtime.save({"SHOP_NAME": "The garage"})
        self.assertFalse(runtime.is_stale())

    def test_a_restart_class_change_does(self):
        runtime.record_generation()
        runtime.save({"TIME_ZONE": "America/Chicago"})
        self.assertTrue(runtime.is_stale())

    def test_and_the_banner_names_what_is_waiting(self):
        runtime.record_generation()
        runtime.save({"TIME_ZONE": "America/Chicago"})
        self.assertEqual(runtime.pending_restart_keys(), ["TIME_ZONE"])

    def test_a_process_that_never_recorded_one_is_not_stale(self):
        """A management command is not long-running; nagging it means nothing."""
        runtime.save({"TIME_ZONE": "America/Chicago"})
        self.assertFalse(runtime.is_stale())

    def test_restarting_is_what_clears_it(self):
        runtime.record_generation()
        runtime.save({"MAX_UPLOAD_MB": 100})
        self.assertTrue(runtime.is_stale())
        runtime.record_generation()  # what a fresh process does at boot
        self.assertFalse(runtime.is_stale())

    def test_the_overlay_is_what_makes_a_restart_mean_something(self):
        """`MAX_UPLOAD_MB` is read by Django itself, so it cannot go through
        the accessor — it is written into the settings at startup instead."""
        runtime.save({"MAX_UPLOAD_MB": 123})
        runtime.apply_restart_overlay()
        from django.conf import settings as django_settings

        self.assertEqual(django_settings.MAX_UPLOAD_MB, 123)
        self.assertEqual(django_settings.DATA_UPLOAD_MAX_MEMORY_SIZE, 123 * 1024 * 1024)

    def test_the_worker_notices_it_is_running_stale_configuration(self):
        """Its whole restart mechanism: exit, and let compose bring it back."""
        runtime.record_generation()
        runtime.save({"LANGUAGE_CODE": "fr-ca"})
        self.assertTrue(runtime.is_stale())

    def test_every_restart_key_is_one_django_really_does_resolve_at_startup(self):
        """A setting marked `restart` that did not need to be is a lie in the
        other direction — it makes people restart for nothing."""
        self.assertEqual(
            RESTART_KEYS, {"LANGUAGE_CODE", "TIME_ZONE", "MAX_UPLOAD_MB"}
        )

    def test_it_says_so_when_it_cannot_restart_itself(self):
        """Rather than a button that silently does nothing."""
        with override_settings(GUNICORN_PIDFILE=""):
            self.assertFalse(runtime.restart_web())


class SettingsScreenTests(RuntimeBase):
    def setUp(self):
        super().setUp()
        self.admin = User.objects.create_user(
            username="boss", password="x" * 16, role=Role.ADMIN
        )
        self.client.force_login(self.admin)

    def test_the_page_renders_with_its_groups(self):
        page = self.client.get(reverse("settings"))
        self.assertContains(page, "Shop name")
        self.assertContains(page, "Outbound requests")

    def test_the_kill_switch_is_reachable_from_it(self):
        """NFR-S-2 is an emergency control. Two clicks from any page."""
        page = self.client.get(reverse("settings", args=["outbound"]))
        self.assertContains(page, "Offline Mode")

    def test_saving_from_the_form_works_end_to_end(self):
        self.client.post(reverse("settings", args=["shop"]), {"SHOP_NAME": "The garage",
                                                              "LANGUAGE_CODE": "en-us",
                                                              "TIME_ZONE": "UTC",
                                                              "UNITS": "imperial",
                                                              "CURRENCY_REPORTING": "USD"})
        self.assertEqual(runtime.conf.SHOP_NAME, "The garage")

    def test_an_unticked_checkbox_means_off(self):
        """A checkbox posts nothing when unticked, so reading `.get()` would
        make every switch in the group impossible to turn off."""
        runtime.save({"STRIP_GPS_EXIF": True})
        self.client.post(reverse("settings", args=["media"]), {"OCR_LANGUAGES": "eng",
                                                               "MAX_UPLOAD_MB": "50",
                                                               "OCR_PDF_MAX_PAGES": "20"})
        self.assertFalse(runtime.conf.STRIP_GPS_EXIF)

    def test_a_stored_key_is_never_sent_to_the_browser(self):
        runtime.save({"WRENCHLEDGER_API_KEY": SECRET})
        page = self.client.get(reverse("settings", args=["integrations"]))
        self.assertNotContains(page, SECRET)
        self.assertContains(page, "stored")

    def test_leaving_a_password_field_blank_keeps_the_stored_one(self):
        """Otherwise saving any other field on the page unauthenticates
        every integration in the group."""
        runtime.save({"WRENCHLEDGER_API_KEY": SECRET})
        self.client.post(
            reverse("settings", args=["integrations"]),
            {"LUBELOGGER_MODE": "import_once", "LUBELOGGER_SYNC_HOURS": "12",
             "WRENCHLEDGER_SYNC_HOURS": "6", "CONSUMABLES_OWNER": "homeautoshop",
             "WRENCHLEDGER_API_KEY": ""},
        )
        self.assertEqual(runtime.conf.WRENCHLEDGER_API_KEY, SECRET)

    def test_removing_one_takes_an_explicit_tick(self):
        runtime.save({"WRENCHLEDGER_API_KEY": SECRET})
        self.client.post(
            reverse("settings", args=["integrations"]),
            {"LUBELOGGER_MODE": "import_once", "LUBELOGGER_SYNC_HOURS": "12",
             "WRENCHLEDGER_SYNC_HOURS": "6", "CONSUMABLES_OWNER": "homeautoshop",
             "clear": "WRENCHLEDGER_API_KEY"},
        )
        self.assertFalse(Credential.objects.filter(key="WRENCHLEDGER_API_KEY").exists())

    def test_a_bad_value_is_explained_and_nothing_is_saved(self):
        page = self.client.post(
            reverse("settings", args=["shop"]),
            {"SHOP_NAME": "The garage", "LANGUAGE_CODE": "en-us",
             "TIME_ZONE": "Mars/Olympus", "UNITS": "imperial", "CURRENCY_REPORTING": "USD"},
            follow=True,
        )
        self.assertContains(page, "not one of the choices")
        self.assertFalse(Setting.objects.filter(key="SHOP_NAME").exists())

    def test_a_restart_setting_is_marked_before_it_is_edited(self):
        page = self.client.get(reverse("settings", args=["shop"]))
        self.assertContains(page, "takes effect when the instance restarts")

    def test_an_unknown_group_is_not_a_page(self):
        self.assertEqual(self.client.get("/settings/nonsense/").status_code, 404)

    def test_a_member_cannot_reach_it(self):
        self.client.force_login(
            User.objects.create_user(username="helper", password="x" * 16, role=Role.MEMBER)
        )
        self.assertNotEqual(self.client.get(reverse("settings")).status_code, 200)


class BackupScreenTests(BackupBase):
    """R-10 — the health page said the backup was overdue and offered nothing."""

    def setUp(self):
        super().setUp()
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.storage = override_settings(BACKUP_DIR=self.tmp, MEDIA_ROOT=self.tmp / "media")
        self.storage.enable()
        self.addCleanup(self.storage.disable)
        self.admin = User.objects.create_user(
            username="boss", password="x" * 16, role=Role.ADMIN
        )
        self.client.force_login(self.admin)

    def test_an_instance_that_has_never_backed_up_says_so_loudly(self):
        page = self.client.get(reverse("backups"))
        self.assertContains(page, "Nothing has ever been backed up")

    def test_backing_up_is_queued_rather_than_run_in_the_request(self):
        """Minutes of pg_dump inside a request is a proxy timeout and a dead
        page with no idea whether it finished."""
        self.client.post(reverse("backup_now"))
        self.assertTrue(Job.objects.filter(type="backup.run", state="pending").exists())

    def test_pressing_it_twice_does_not_queue_two(self):
        self.client.post(reverse("backup_now"))
        self.client.post(reverse("backup_now"))
        self.assertEqual(Job.objects.filter(type="backup.run").count(), 1)

    def test_the_export_is_a_separate_button_and_a_separate_job(self):
        self.client.post(reverse("backup_now"), {"what": "export"})
        self.assertTrue(Job.objects.filter(type="export.build").exists())

    def test_what_is_held_is_read_from_the_disk(self):
        """Not from a table of what the instance believes it wrote — the
        failure this screen exists to catch is a backup that is not there."""
        run_backup()
        page = self.client.get(reverse("backups"))
        self.assertContains(page, "database")
        self.assertNotContains(page, "Nothing here yet")

    def test_a_backup_with_no_database_in_it_is_flagged(self):
        """Restoring from one produces an empty shop full of photos."""
        (self.tmp / "20240101-000000").mkdir()
        page = self.client.get(reverse("backups"))
        self.assertContains(page, "no database in it")

    def test_the_database_half_can_be_downloaded(self):
        target = run_backup()
        response = self.client.get(reverse("backup_download", args=[target.name]))
        self.assertEqual(response.status_code, 200)
        self.assertIn("attachment", response["Content-Disposition"])

    def test_walking_out_of_the_backup_directory_is_refused(self):
        response = self.client.get("/backups/..%2F..%2Fsettings.py/download/")
        self.assertIn(response.status_code, (404, 301, 302))

    def test_a_backup_that_has_since_been_pruned_is_a_message_not_a_crash(self):
        """Pruning runs on a schedule, so an open page offers stale rows."""
        response = self.client.get(
            reverse("backup_download", args=["20200101-000000"]), follow=True
        )
        self.assertContains(response, "not here any more")

    def test_deleting_one_removes_it(self):
        target = run_backup()
        self.client.post(reverse("backup_delete", args=[target.name]))
        self.assertFalse(target.exists())

    def test_the_restore_command_carries_this_instance_real_paths(self):
        """Rather than leaving someone to reassemble it from the docs during
        the one hour they can least afford to."""
        run_backup()
        page = self.client.get(reverse("backups"))
        self.assertContains(page, "manage.py restore /data/backups/")

    def test_restore_is_not_offered_as_a_button(self):
        """Swapping the database underneath a running process is not something
        a web request should attempt."""
        page = self.client.get(reverse("backups")).content.decode()
        self.assertNotIn('action="/backups/restore', page)

    def test_a_member_cannot_reach_it(self):
        self.client.force_login(
            User.objects.create_user(username="helper", password="x" * 16, role=Role.MEMBER)
        )
        self.assertNotEqual(self.client.get(reverse("backups")).status_code, 200)


class AllowlistTests(RuntimeBase):
    """The list is half derived and half hand-written, so it needs tidying."""

    @override_settings(OUTBOUND_ALLOWLIST=["api.nhtsa.gov", "api.nhtsa.gov"])
    def test_a_host_appearing_twice_is_listed_once(self):
        """`settings.py` already appends the configured integration hosts, so
        every one of them arrives here a second time."""
        self.assertEqual(runtime.allowlist().count("api.nhtsa.gov"), 1)

    @override_settings(OUTBOUND_ALLOWLIST=["nhtsa.gov/recalls"])
    def test_an_entry_written_with_a_path_still_matches_its_host(self):
        """It reads as allowed and can never match — the worst kind of typo."""
        self.assertIn("nhtsa.gov", runtime.allowlist())

    @override_settings(OUTBOUND_ALLOWLIST=["https://Example.COM:8443"])
    def test_a_scheme_a_port_and_a_capital_are_all_forgiven(self):
        self.assertIn("example.com", runtime.allowlist())


class DependentFieldTests(RuntimeBase):
    """A form full of controls that currently do nothing is unreadable."""

    def setUp(self):
        super().setUp()
        self.admin = User.objects.create_user(
            username="boss", password="x" * 16, role=Role.ADMIN
        )
        self.client.force_login(self.admin)

    def test_a_child_names_the_switch_it_hangs_off(self):
        page = self.client.get(reverse("settings", args=["outbound"]))
        self.assertContains(page, 'data-child-of="id_PLATE_LOOKUP_ENABLED"')

    def test_the_switch_itself_is_not_a_child_of_anything(self):
        """A parent that hides itself is a control nobody can turn back on."""
        self.assertEqual(BY_KEY["PLATE_LOOKUP_ENABLED"].depends_on, "")

    def test_every_declared_parent_is_a_real_setting(self):
        """A typo would silently mean 'never hide this', which looks fine."""
        for entry in REGISTRY:
            if entry.depends_on:
                with self.subTest(key=entry.key):
                    self.assertIn(entry.depends_on, BY_KEY)

    def test_a_parent_can_actually_be_switched_off(self):
        """Hiding a child behind something with no off state hides it for ever."""
        for entry in REGISTRY:
            if not entry.depends_on:
                continue
            parent = BY_KEY[entry.depends_on]
            with self.subTest(key=entry.key):
                self.assertIn(parent.kind, ("bool", "str", "secret"))

    def test_the_plate_lookup_fields_hang_off_the_plate_lookup_switch(self):
        self.assertEqual(
            sorted(entry.key for entry in children_of("PLATE_LOOKUP_ENABLED")),
            [
                "PLATE_LOOKUP_COST_MINOR",
                "PLATE_LOOKUP_KEY",
                "PLATE_LOOKUP_MONTHLY_CAP",
                "PLATE_LOOKUP_URL",
            ],
        )

    def test_nothing_is_hidden_without_script(self):
        """Progressive enhancement: the page is correct before forms.js runs."""
        page = self.client.get(reverse("settings", args=["outbound"])).content.decode()
        self.assertIn("PLATE_LOOKUP_URL", page)
        self.assertNotIn("hidden", page.split("PLATE_LOOKUP_URL")[0][-200:])


class SecretSourceTests(RuntimeBase):
    """It said "not set" for a key the application was authenticating with."""

    def setUp(self):
        super().setUp()
        self.admin = User.objects.create_user(
            username="boss", password="x" * 16, role=Role.ADMIN
        )
        self.client.force_login(self.admin)

    @override_settings(WRENCHLEDGER_API_KEY="from-the-env-file")
    def test_a_key_set_in_the_environment_is_reported_as_set(self):
        page = self.client.get(reverse("settings", args=["integrations"]))
        self.assertContains(page, "set in the .env file")
        self.assertNotContains(page, "from-the-env-file")

    @override_settings(WRENCHLEDGER_API_KEY="")
    def test_a_key_stored_here_says_so_and_offers_removal(self):
        runtime.save({"WRENCHLEDGER_API_KEY": SECRET})
        page = self.client.get(reverse("settings", args=["integrations"]))
        self.assertContains(page, "stored here")
        self.assertContains(page, "Remove the stored key")

    @override_settings(WRENCHLEDGER_API_KEY="")
    def test_only_a_genuinely_absent_key_says_not_set(self):
        page = self.client.get(reverse("settings", args=["integrations"]))
        self.assertContains(page, "not set")

    @override_settings(WRENCHLEDGER_API_KEY="from-the-env-file")
    def test_an_environment_key_is_not_offered_a_remove_box_that_cannot_work(self):
        """A checkbox here cannot delete something a file sets."""
        page = self.client.get(reverse("settings", args=["integrations"])).content.decode()
        self.assertNotIn(
            'name="clear" value="WRENCHLEDGER_API_KEY"', page
        )


class TimezonePickerTests(RuntimeBase):
    """Typed by hand, this is the setting a typo corrupts most quietly."""

    def setUp(self):
        super().setUp()
        self.admin = User.objects.create_user(
            username="boss", password="x" * 16, role=Role.ADMIN
        )
        self.client.force_login(self.admin)

    def test_it_is_a_list_to_choose_from(self):
        self.assertEqual(BY_KEY["TIME_ZONE"].kind, "choice")
        self.assertGreater(len(BY_KEY["TIME_ZONE"].choices), 300)

    def test_the_default_is_selectable(self):
        """UTC has no region prefix, so filtering to `Region/City` drops it."""
        self.assertIn("UTC", [value for value, _label in BY_KEY["TIME_ZONE"].choices])

    def test_the_legacy_aliases_are_left_out(self):
        """`EST5EDT` and `Zulu` resolve, and are not what anyone is looking
        for in a list of five hundred."""
        offered = {value for value, _label in BY_KEY["TIME_ZONE"].choices}
        self.assertNotIn("EST5EDT", offered)
        self.assertNotIn("Zulu", offered)

    def test_a_real_zone_can_be_chosen_from_the_page(self):
        self.client.post(
            reverse("settings", args=["shop"]),
            {
                "SHOP_NAME": "The garage", "LANGUAGE_CODE": "en-us",
                "TIME_ZONE": "America/Chicago", "UNITS": "imperial",
                "CURRENCY_REPORTING": "USD",
            },
        )
        self.assertEqual(runtime.conf.TIME_ZONE, "America/Chicago")

    def test_the_error_for_an_invented_one_does_not_print_five_hundred_options(self):
        with self.assertRaises(ValidationError) as caught:
            coerce(BY_KEY["TIME_ZONE"], "Mars/Olympus")
        self.assertLess(len(" ".join(caught.exception.messages)), 120)


class DeletingABackupTests(BackupBase):
    """`ignore_errors=True` hid a real failure: deleted, said so, still there."""

    def setUp(self):
        super().setUp()
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.storage = override_settings(BACKUP_DIR=self.tmp, MEDIA_ROOT=self.tmp / "media")
        self.storage.enable()
        self.addCleanup(self.storage.disable)
        self.admin = User.objects.create_user(
            username="boss", password="x" * 16, role=Role.ADMIN
        )
        self.client.force_login(self.admin)

    def test_taking_a_backup_leaves_no_file_handle_open(self):
        """Stripping the credential table opened the copy with sqlite3 and
        `with sqlite3.connect(...)` commits without closing. On Windows the
        open handle then stopped the backup ever being deleted or pruned."""
        target = run_backup()
        shutil.rmtree(target)  # no ignore_errors: this must simply work
        self.assertFalse(target.exists())

    def test_the_screen_says_so_when_a_delete_does_not_happen(self):
        target = run_backup()
        with mock.patch("homeautoshop.core.views_settings.shutil.rmtree") as blocked:
            blocked.side_effect = lambda path, onexc=None: onexc(None, path, OSError("in use"))
            response = self.client.post(
                reverse("backup_delete", args=[target.name]), follow=True
            )
        self.assertContains(response, "could not be deleted")
        self.assertTrue(target.exists())


class UploadedBackupTests(TestCase):
    """Putting back together what the download path takes apart (R-10).

    The point of these is the loop, not the plumbing: this screen hands out a
    database file and an export ZIP, `restore` demands a folder with a manifest
    neither of them carries, and until now nothing in the product joined the
    two. `test_the_assembled_folder_is_one_restore_accepts` is the test that
    actually says the feature works — the rest guard the ways it could be fed
    something it should refuse.
    """

    def setUp(self):
        self.directory = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.directory, ignore_errors=True)

    def _dump(self):
        """A file that starts like this engine's dump, whichever engine it is."""
        magic, name = DUMP_MAGIC[connection.vendor]
        return SimpleUploadedFile(name, magic + b"\x00 and then the rest of a dump")

    def _export(self, *, media=("media/photos/rocker.jpg",), manifest=None):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr(
                "manifest.json",
                json.dumps(
                    manifest
                    if manifest is not None
                    else {
                        "application": "HomeAutoShop",
                        "schema_version": SCHEMA_VERSION,
                        "exported_at": "2026-09-03T23:31:28+00:00",
                    }
                ),
            )
            for name in media:
                archive.writestr(name, b"not really a jpeg")
        buffer.seek(0)
        return SimpleUploadedFile("export-20260903-233128.zip", buffer.read())

    def test_the_assembled_folder_is_one_restore_accepts(self):
        """The whole feature, asserted end to end.

        `restore --dry-run` makes every check that matters — manifest present,
        schema version current, vendor matching, database file where it expects
        — and stops before touching anything. If this passes, the folder this
        builds is restorable.
        """
        with override_settings(BACKUP_DIR=self.directory):
            target, notes = assemble_uploaded(self._dump(), self._export())
            call_command("restore", str(target), "--dry-run")

        self.assertEqual(notes, [])
        manifest = json.loads((target / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["vendor"], connection.vendor)
        self.assertEqual(manifest["schema_version"], SCHEMA_VERSION)
        self.assertEqual(manifest["media"], "included")
        self.assertTrue(manifest["assembled_from_upload"])
        self.assertTrue((target / DUMP_MAGIC[connection.vendor][1]).exists())
        self.assertTrue((target / "media" / "photos" / "rocker.jpg").exists())

    def test_it_sorts_beside_the_backups_taken_here(self):
        """Named stamp-first so the newest really is the newest.

        `held_backups` sorts by name, so `uploaded-...` would have sorted above
        every dated folder for ever and the restore command on the page would
        have gone on naming an old upload after a fresh backup was taken.
        """
        with override_settings(BACKUP_DIR=self.directory):
            target, _notes = assemble_uploaded(self._dump(), self._export())
        self.assertTrue(target.name.endswith("-uploaded"))
        self.assertTrue(target.name[:8].isdigit())

    def test_a_file_that_is_not_a_dump_is_refused(self):
        """Caught here, or discovered by pg_restore after the drop."""
        with override_settings(BACKUP_DIR=self.directory):
            with self.assertRaises(UploadRejected):
                assemble_uploaded(SimpleUploadedFile("database.dump", b"PK\x03\x04 a zip, actually"))

    def test_a_zip_that_writes_outside_the_folder_is_refused(self):
        """This archive arrived over an upload form; its paths are not ours."""
        with override_settings(BACKUP_DIR=self.directory):
            with self.assertRaises(UploadRejected):
                assemble_uploaded(
                    self._dump(), self._export(media=("media/../../../etc/escaped",))
                )
        self.assertFalse((self.directory.parent / "escaped").exists())

    def test_without_an_export_it_says_what_it_could_not_check(self):
        """No media, and no evidence of the schema the dump was taken under."""
        with override_settings(BACKUP_DIR=self.directory):
            target, notes = assemble_uploaded(self._dump())
        manifest = json.loads((target / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["media"], "none")
        self.assertTrue(any("schema version" in str(note) for note in notes))

    def test_an_older_export_keeps_its_own_schema_version(self):
        """Believed rather than overwritten, or `restore` could not refuse it.

        Writing this build's version would make every uploaded backup pass the
        one check that exists to catch a dump too old to restore.
        """
        with override_settings(BACKUP_DIR=self.directory):
            target, _notes = assemble_uploaded(
                self._dump(),
                self._export(manifest={"schema_version": SCHEMA_VERSION - 1}),
            )
            manifest = json.loads((target / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["schema_version"], SCHEMA_VERSION - 1)
            with self.assertRaises(Exception):
                call_command("restore", str(target), "--dry-run")

    def test_the_upload_needs_the_settings_permission(self):
        User.objects.create_user(username="mechanic", password="pw", role=Role.MEMBER)
        self.client.login(username="mechanic", password="pw")
        response = self.client.post(reverse("backup_upload"), {})
        self.assertIn(response.status_code, (302, 403))
