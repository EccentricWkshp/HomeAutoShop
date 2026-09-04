"""
Phase 4 — the pieces that reach outside this machine, and the offline queue.

Every test here is offline. Nothing contacts LubeLogger, WrenchLedger, or a
plate provider: the point is the *guardrails* — that a scheduled sync respects
Offline Mode, that a WrenchLedger plan gate reads differently from a bad key,
that a plate lookup cannot spend money without asking. Those are the parts that
fail quietly in production if they are wrong, and the parts a live API test
would never exercise anyway.
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

from django.conf import settings
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from homeautoshop.accounts.models import Role, User
from homeautoshop.assets.models import Asset, UsageReading
from homeautoshop.core import csvimport, schedule
from homeautoshop.core.integrations import sync as lubelogger_sync
from homeautoshop.core.integrations import wrenchledger as wl
from homeautoshop.core.models import ExternalRef, Job, Setting
from homeautoshop.core.outbound import OutboundFailed
from homeautoshop.work.models import JobItem, JobItemTool, ShopTool, WorkOrder

VIN = "1M8GDM9AXKP042788"


# --------------------------------------------------------------------------
# Recurring work
# --------------------------------------------------------------------------


class ScheduleTests(TestCase):
    """The worker enqueues its own recurring jobs (P-3 — no second scheduler)."""

    def test_a_due_job_is_queued(self):
        self.assertGreater(schedule.tick(), 0)
        self.assertTrue(Job.objects.filter(type="backup.run").exists())

    def test_it_does_not_queue_the_same_job_twice(self):
        schedule.tick()
        before = Job.objects.count()
        schedule.tick()
        self.assertEqual(Job.objects.count(), before)

    def test_a_job_still_running_does_not_get_a_second_copy(self):
        """A worker restarted in a loop must not pile up a thousand backups."""
        schedule.tick()
        Job.objects.filter(type="backup.run").update(state=Job.State.RUNNING)
        Setting.objects.filter(key__startswith=schedule.LAST_RUN_PREFIX).delete()
        schedule.tick()
        self.assertEqual(Job.objects.filter(type="backup.run").count(), 1)

    def test_it_queues_again_once_the_interval_has_passed(self):
        schedule.tick()
        Job.objects.all().update(state=Job.State.DONE)
        Setting.put(
            f"{schedule.LAST_RUN_PREFIX}backup.run",
            (timezone.now() - timedelta(days=2)).isoformat(),
        )
        self.assertGreater(schedule.tick(), 0)

    @override_settings(LUBELOGGER_URL="https://lube.example", LUBELOGGER_MODE="pull")
    def test_a_sync_mode_puts_the_pull_on_the_timer(self):
        self.assertIn("lubelogger.sync", [name for name, _every in schedule.recurring()])

    @override_settings(LUBELOGGER_URL="https://lube.example", LUBELOGGER_MODE="import_once")
    def test_import_once_never_polls(self):
        self.assertNotIn("lubelogger.sync", [name for name, _every in schedule.recurring()])


# --------------------------------------------------------------------------
# LubeLogger scheduled pull
# --------------------------------------------------------------------------


@override_settings(LUBELOGGER_URL="https://lube.example", LUBELOGGER_MODE="pull")
class LubeLoggerSyncTests(TestCase):
    def test_it_is_due_when_nothing_has_ever_run(self):
        self.assertTrue(lubelogger_sync.due())

    def test_it_is_not_due_again_straight_away(self):
        Setting.put(lubelogger_sync.LAST_SYNC_KEY, timezone.now().isoformat())
        self.assertFalse(lubelogger_sync.due())

    def test_it_is_due_once_the_interval_has_passed(self):
        Setting.put(
            lubelogger_sync.LAST_SYNC_KEY, (timezone.now() - timedelta(days=2)).isoformat()
        )
        self.assertTrue(lubelogger_sync.due())

    @override_settings(OFFLINE_MODE=True)
    def test_offline_mode_stops_it(self):
        self.assertFalse(lubelogger_sync.due())
        with self.assertRaises(lubelogger_sync.SyncSkipped):
            lubelogger_sync.run(force=True)

    @override_settings(LUBELOGGER_MODE="import_once")
    def test_a_non_sync_mode_stops_it_even_when_forced(self):
        with self.assertRaises(lubelogger_sync.SyncSkipped):
            lubelogger_sync.run(force=True)

    def test_the_first_window_is_bounded_rather_than_all_of_history(self):
        start = lubelogger_sync.window_start()
        self.assertEqual(
            start, (timezone.now() - timedelta(days=lubelogger_sync.FIRST_RUN_DAYS)).date()
        )

    def test_later_windows_overlap_the_last_run(self):
        """A record edited at the source after import keeps its original date,
        so a window starting exactly at the last run would never see the edit."""
        last = timezone.now() - timedelta(days=1)
        Setting.put(lubelogger_sync.LAST_SYNC_KEY, last.isoformat())
        self.assertEqual(
            lubelogger_sync.window_start(),
            (last - timedelta(days=lubelogger_sync.OVERLAP_DAYS)).date(),
        )

    def test_a_run_that_errored_does_not_advance_the_marker(self):
        """Advancing past records it never reached would lose them for good."""

        class Failing:
            base_url = "https://lube.example"

            def vehicles(self):
                raise RuntimeError("nope")

        with patch("homeautoshop.core.integrations.importer.Importer.run") as run:
            run.return_value = _report(errors=["boom"])
            lubelogger_sync.run(client=Failing(), force=True)
        self.assertIsNone(Setting.get(lubelogger_sync.LAST_SYNC_KEY))

    def test_a_clean_run_advances_it(self):
        class Empty:
            base_url = "https://lube.example"

        with patch("homeautoshop.core.integrations.importer.Importer.run") as run:
            run.return_value = _report()
            lubelogger_sync.run(client=Empty(), force=True)
        self.assertIsNotNone(Setting.get(lubelogger_sync.LAST_SYNC_KEY))

    def test_a_scheduled_run_never_creates_a_vehicle_on_its_own(self):
        """Unattended vehicle creation is how an instance grows a duplicate of a
        car it already has, under a different name, with nobody watching."""
        class Empty:
            base_url = "https://lube.example"

        with patch("homeautoshop.core.integrations.importer.run_import") as run_import:
            run_import.return_value = _report()
            lubelogger_sync.run(client=Empty(), force=True)
        self.assertIs(run_import.call_args.kwargs["create_missing"], False)


def _report(**kwargs):
    from homeautoshop.core.integrations.importer import Report

    report = Report(dry_run=False)
    for key, value in kwargs.items():
        setattr(report, key, value)
    return report


class ImporterWindowTests(TestCase):
    """The date window is applied per row, and undated rows are never dropped."""

    def _importer(self, since):
        from homeautoshop.core.integrations.importer import Importer

        class Client:
            base_url = "https://lube.example"

        return Importer(Client(), dry_run=True, since=since)

    def test_a_row_older_than_the_window_is_skipped(self):
        importer = self._importer(date(2026, 1, 1))
        match = type("M", (), {"asset": None, "external_id": "1"})()
        importer._import_row("odometer", {"id": "a", "date": "2025-06-01"}, match)
        self.assertEqual(importer.report.created, {})

    def test_a_row_inside_the_window_is_imported(self):
        importer = self._importer(date(2026, 1, 1))
        match = type("M", (), {"asset": None, "external_id": "1"})()
        importer._import_row("odometer", {"id": "a", "date": "2026-06-01", "odometer": "100"}, match)
        self.assertEqual(importer.report.created.get("odometer"), 1)

    def test_an_undated_row_is_never_skipped_as_old(self):
        """Deciding an undated row is old would drop it from every future run."""
        importer = self._importer(date(2026, 1, 1))
        match = type("M", (), {"asset": None, "external_id": "1"})()
        importer._import_row("odometer", {"id": "a", "odometer": "100"}, match)
        self.assertEqual(importer.report.created.get("odometer"), 1)


# --------------------------------------------------------------------------
# WrenchLedger
# --------------------------------------------------------------------------


@override_settings(WRENCHLEDGER_API_KEY="wlk_live_test")
class WrenchLedgerConnectionTests(TestCase):
    """FR-WL-1 — a bad key and a plan without API access need different words."""

    def _check(self, side_effect=None, data=None):
        client = wl.WrenchLedgerClient()
        with patch.object(wl.WrenchLedgerClient, "get") as get:
            if side_effect is not None:
                get.side_effect = side_effect
            else:
                get.return_value = data
            return client.check()

    def test_a_working_key_reports_the_workspace(self):
        result = self._check(
            data={"tier": "shop", "state": "active", "scopes": list(wl.REQUIRED_SCOPES),
                  "features": {"api_webhooks": True}}
        )
        self.assertTrue(result.usable)
        self.assertEqual(result.tier, "shop")

    def test_a_plan_gate_is_not_reported_as_a_bad_key(self):
        result = self._check(
            side_effect=OutboundFailed("HTTP 403", status=403, body={"code": "FEATURE_NOT_IN_PLAN"})
        )
        self.assertTrue(result.authenticated)
        self.assertFalse(result.usable)
        self.assertIn("plan", str(result.message))
        self.assertNotIn("key was copied", str(result.message))

    def test_a_bad_key_says_so(self):
        result = self._check(side_effect=OutboundFailed("HTTP 401", status=401, body={}))
        self.assertFalse(result.authenticated)
        self.assertIn("key", str(result.message))

    def test_entitlement_is_read_from_the_response_not_inferred_from_the_tier(self):
        """WL-Q5: the gate is a per-workspace override as often as a plan rule,
        and it may move. Nothing may assume solo means no API."""
        result = self._check(
            data={"tier": "solo", "state": "complimentary", "scopes": list(wl.REQUIRED_SCOPES),
                  "features": {"api_webhooks": True}}
        )
        self.assertTrue(result.usable)

    def test_a_missing_scope_is_named(self):
        result = self._check(
            data={"tier": "shop", "scopes": ["workspace:read"], "features": {"api_webhooks": True}}
        )
        self.assertIn("loans:read", str(result.message))

    def test_an_over_privileged_key_is_flagged(self):
        result = self._check(
            data={
                "tier": "shop",
                "scopes": [*wl.REQUIRED_SCOPES, "loans:sensitive", "files:read"],
                "features": {"api_webhooks": True},
            }
        )
        self.assertEqual(result.excess_scopes, ["files:read", "loans:sensitive"])

    def test_the_scope_that_returns_borrower_contact_details_is_never_required(self):
        """Requesting it would create a mirroring obligation NG-8 forbids."""
        self.assertNotIn("loans:sensitive", wl.REQUIRED_SCOPES)


@override_settings(WRENCHLEDGER_API_KEY="wlk_live_test")
class WrenchLedgerSyncTests(TestCase):
    class FakeClient:
        base_url = wl.DEFAULT_BASE

        def __init__(self, loans=None, tools=None, schedules=None):
            self._loans = loans or []
            self._tools = tools or []
            self._schedules = schedules or []

        def open_loans(self):
            return self._loans

        def tools_changed_since(self, watermark):
            stamps = [t.get("updated_at", "") for t in self._tools if t.get("updated_at")]
            return self._tools, max(stamps) if stamps else watermark

        def schedules_for(self, tool_id):
            return self._schedules

    def test_an_open_loan_makes_a_tool_unavailable(self):
        wl.sync(
            client=self.FakeClient(
                loans=[{"tool_id": "t1", "tool_name": "Breaker bar",
                        "borrower_name": "Dave", "due_date": "2026-09-02"}]
            )
        )
        tool = ShopTool.objects.get(tool_id="t1")
        self.assertEqual(tool.on_loan_to, "Dave")
        self.assertEqual(tool.loan_due_on, date(2026, 9, 2))
        self.assertTrue(tool.issues)

    def test_a_returned_tool_becomes_available_again(self):
        """A returned loan simply is not in the response, so the state has to be
        cleared before it is rewritten — the bug that would otherwise leave a
        tool marked out forever."""
        client = self.FakeClient(loans=[{"tool_id": "t1", "borrower_name": "Dave"}])
        wl.sync(client=client)
        client._loans = []
        wl.sync(client=client)
        self.assertEqual(ShopTool.objects.get(tool_id="t1").on_loan_to, "")

    def test_the_cache_keeps_only_the_allowed_fields(self):
        """A deny-list would start storing valuation data the first time
        WrenchLedger adds a field, and NG-8 would erode by accident."""
        kept = wl.keep_tool_fields(
            {
                "id": "t1",
                "name": "Torque wrench",
                "purchase_price": "199.00",
                "serial_number": "SN-4471",
                "storage_location_id": "loc-9",
                "notes": "in the red box",
            }
        )
        self.assertEqual(set(kept), {"id", "name"})

    def test_the_watermark_is_the_maximum_seen_not_the_request_time(self):
        """`updated_after` is strictly greater-than, so a request-time watermark
        silently skips anything written between the query and the response."""
        wl.sync(
            client=self.FakeClient(
                tools=[
                    {"id": "t1", "name": "A", "updated_at": "2026-05-01T00:00:00Z"},
                    {"id": "t2", "name": "B", "updated_at": "2026-06-01T00:00:00Z"},
                ]
            )
        )
        self.assertEqual(Setting.get(wl.WATERMARK_KEY), "2026-06-01T00:00:00Z")

    def _referenced(self, tool_id: str) -> None:
        """A tool some job actually names. Schedules are fetched for these only —
        there is no workspace-wide schedule list, so tier 3 is per tool."""
        asset = Asset.objects.create(nickname="Silverado", vin=VIN)
        work_order = WorkOrder.objects.create(asset=asset, title="Front brakes")
        item = JobItem.objects.create(work_order=work_order, title="Torque the wheels")
        tool, _created = ShopTool.objects.get_or_create(tool_id=tool_id)
        JobItemTool.objects.create(job_item=item, tool=tool)

    #: A real WrenchLedger id, because the schedule poll now checks the shape.
    WL_ID = "0231e52e-34ca-45d3-a31d-aaae0d93b694"

    def test_a_calibration_due_today_is_an_issue(self):
        self._referenced(self.WL_ID)
        wl.sync(
            client=self.FakeClient(
                tools=[{"id": self.WL_ID, "name": "Torque wrench",
                        "updated_at": "2026-05-01T00:00:00Z"}],
                schedules=[{"kind": "calibration", "next_due_on": "2020-01-01"}],
            )
        )
        self.assertTrue(ShopTool.objects.get(tool_id=self.WL_ID).issues)

    def test_a_tool_wrenchledger_never_heard_of_is_not_asked_about(self):
        """A tool named on a job that the picker could not match is stored under
        the typed text — `Vacuum pump` became a tool id, and every sync then
        asked for `/tools/Vacuum pump/schedules`, three times in one instance's
        audit log and never once successfully."""
        self._referenced("Vacuum pump")
        client = self.FakeClient(schedules=[{"kind": "calibration", "next_due_on": "2020-01-01"}])
        client.asked = []
        original = client.schedules_for

        def watched(tool_id):
            client.asked.append(tool_id)
            return original(tool_id)

        client.schedules_for = watched

        wl.sync(client=client)

        self.assertEqual(client.asked, [])
        self.assertIsNone(ShopTool.objects.get(tool_id="Vacuum pump").calibration_due_on)

    def test_schedules_are_not_fetched_for_a_tool_no_job_references(self):
        """Per tool, and only the referenced ones — WrenchLedger has no
        workspace-wide schedule list, so this is the narrowest possible call."""
        wl.sync(
            client=self.FakeClient(
                tools=[{"id": "t9", "name": "Spanner", "updated_at": "2026-05-01T00:00:00Z"}],
                schedules=[{"kind": "calibration", "next_due_on": "2020-01-01"}],
            )
        )
        self.assertIsNone(ShopTool.objects.get(tool_id="t9").calibration_due_on)

    def test_a_lifecycle_that_is_not_stored_is_an_issue(self):
        wl.sync(
            client=self.FakeClient(
                tools=[{"id": "t1", "name": "Grinder", "lifecycle": "under_repair",
                        "updated_at": "2026-05-01T00:00:00Z"}]
            )
        )
        self.assertTrue(ShopTool.objects.get(tool_id="t1").issues)

    def test_a_tool_that_is_simply_here_has_no_issues(self):
        wl.sync(
            client=self.FakeClient(
                tools=[{"id": "t1", "name": "Hammer", "lifecycle": "stored",
                        "updated_at": "2026-05-01T00:00:00Z"}]
            )
        )
        self.assertEqual(ShopTool.objects.get(tool_id="t1").issues, [])

    @override_settings(OFFLINE_MODE=True)
    def test_offline_mode_stops_it(self):
        from homeautoshop.core.outbound import OutboundBlocked

        with self.assertRaises(OutboundBlocked):
            wl.sync(client=self.FakeClient())


class ReadinessGateTests(TestCase):
    """FR-WL-3/4 — surfaced on the work order, and never a block."""

    def setUp(self):
        self.user = User.objects.create_user(username="andy", password="x" * 16)
        self.client.force_login(self.user)
        self.asset = Asset.objects.create(nickname="Silverado", vin=VIN)
        self.wo = WorkOrder.objects.create(asset=self.asset, title="Front brakes")
        self.item = JobItem.objects.create(work_order=self.wo, title="Replace pads")

    def _reference(self, **tool):
        shop_tool = ShopTool.objects.create(checked_at=timezone.now(), **tool)
        return JobItemTool.objects.create(job_item=self.item, tool=shop_tool)

    @override_settings(WRENCHLEDGER_API_KEY="wlk_live_test")
    def test_a_loaned_tool_warns_on_the_work_order(self):
        self._reference(tool_id="t1", name="Breaker bar", on_loan_to="Dave",
                        loan_due_on=date(2026, 9, 2))
        page = self.client.get(reverse("work_order_detail", args=[self.wo.pk]))
        self.assertContains(page, "Breaker bar")
        self.assertContains(page, "Dave")

    @override_settings(WRENCHLEDGER_API_KEY="wlk_live_test")
    def test_it_never_blocks_the_work(self):
        """A hard block on data from an optional external system would be
        indefensible — and this application has no standing to be sure."""
        self._reference(tool_id="t1", name="Breaker bar", on_loan_to="Dave")
        response = self.client.post(
            reverse("work_order_transition", args=[self.wo.pk]), {"status": "in_progress"}
        )
        self.wo.refresh_from_db()
        self.assertEqual(self.wo.status, "in_progress")

    @override_settings(WRENCHLEDGER_API_KEY="")
    def test_with_no_connection_the_page_is_exactly_as_it_was(self):
        self._reference(tool_id="t1", name="Breaker bar", on_loan_to="Dave")
        page = self.client.get(reverse("work_order_detail", args=[self.wo.pk]))
        self.assertEqual(page.context["tool_warnings"], [])

    @override_settings(WRENCHLEDGER_API_KEY="wlk_live_test", OFFLINE_MODE=True)
    def test_offline_mode_suppresses_it_too(self):
        self._reference(tool_id="t1", name="Breaker bar", on_loan_to="Dave")
        page = self.client.get(reverse("work_order_detail", args=[self.wo.pk]))
        self.assertEqual(page.context["tool_warnings"], [])

    @override_settings(WRENCHLEDGER_API_KEY="wlk_live_test")
    def test_a_stale_check_is_shown_as_an_age_not_asserted_as_fact(self):
        tool = ShopTool.objects.create(
            tool_id="t1", name="Breaker bar", on_loan_to="Dave",
            checked_at=timezone.now() - timedelta(days=3),
        )
        JobItemTool.objects.create(job_item=self.item, tool=tool)
        page = self.client.get(reverse("work_order_detail", args=[self.wo.pk]))
        self.assertTrue(page.context["tool_warnings"][0].stale)

    def test_a_tool_reference_is_an_id_and_not_a_copy_of_the_record(self):
        reference = self._reference(tool_id="t1", name="Breaker bar")
        self.assertEqual(
            [f.name for f in JobItemTool._meta.get_fields() if f.name in ("job_item", "tool")],
            ["job_item", "tool"],
        )
        self.assertEqual(reference.tool.url, "https://www.wrench-ledger.app/tools/t1")


# --------------------------------------------------------------------------
# Plate lookup
# --------------------------------------------------------------------------


class PlateLookupTests(TestCase):
    """§8.2 — the only thing here that spends money."""

    def setUp(self):
        from homeautoshop.assets import plate

        self.plate = plate
        self.user = User.objects.create_user(username="andy", password="x" * 16)
        self.client.force_login(self.user)

    class FakeProvider:
        def __init__(self, result=None):
            self.result = result
            self.calls = 0

        def lookup(self, plate, region, *, user=None):
            from homeautoshop.assets.plate import PlateResult

            self.calls += 1
            return self.result or PlateResult(vin=VIN, year=1989, make="MCI")

    def test_it_is_off_by_default(self):
        with self.assertRaises(self.plate.LookupUnavailable):
            self.plate.lookup("ABC123", "TX", provider=self.FakeProvider())

    @override_settings(PLATE_LOOKUP_ENABLED=True, PLATE_LOOKUP_URL="https://plates.example")
    def test_a_lookup_returns_a_vin(self):
        result = self.plate.lookup("ABC123", "TX", provider=self.FakeProvider())
        self.assertEqual(result.vin, VIN)

    @override_settings(PLATE_LOOKUP_ENABLED=True, PLATE_LOOKUP_URL="https://plates.example")
    def test_the_region_is_required_because_terms_differ_by_jurisdiction(self):
        with self.assertRaises(self.plate.LookupUnavailable):
            self.plate.lookup("ABC123", "", provider=self.FakeProvider())

    @override_settings(PLATE_LOOKUP_ENABLED=True, PLATE_LOOKUP_URL="https://plates.example")
    def test_every_call_is_counted(self):
        provider = self.FakeProvider()
        self.plate.lookup("ABC123", "TX", provider=provider)
        self.plate.lookup("DEF456", "TX", provider=provider)
        self.assertEqual(self.plate.usage(), 2)

    @override_settings(
        PLATE_LOOKUP_ENABLED=True, PLATE_LOOKUP_URL="https://plates.example",
        PLATE_LOOKUP_MONTHLY_CAP=2,
    )
    def test_the_cap_is_a_hard_stop(self):
        provider = self.FakeProvider()
        self.plate.lookup("A", "TX", provider=provider)
        self.plate.lookup("B", "TX", provider=provider)
        with self.assertRaises(self.plate.CapReached):
            self.plate.lookup("C", "TX", provider=provider)
        self.assertEqual(provider.calls, 2)

    @override_settings(PLATE_LOOKUP_ENABLED=True, PLATE_LOOKUP_URL="https://plates.example")
    def test_a_failed_call_still_counts(self):
        """A provider that answered has almost certainly billed for it, so a
        counter that tracks only successes understates the bill."""

        class Failing:
            def lookup(self, plate, region, *, user=None):
                raise OutboundFailed("HTTP 404", status=404)

        with self.assertRaises(self.plate.LookupUnavailable):
            self.plate.lookup("ABC123", "TX", provider=Failing())
        self.assertEqual(self.plate.usage(), 1)

    @override_settings(PLATE_LOOKUP_ENABLED=True, OFFLINE_MODE=True)
    def test_offline_mode_stops_it(self):
        with self.assertRaises(self.plate.LookupUnavailable):
            self.plate.lookup("ABC123", "TX", provider=self.FakeProvider())

    @override_settings(PLATE_LOOKUP_ENABLED=True, PLATE_LOOKUP_URL="https://plates.example")
    def test_the_ui_asks_before_it_spends(self):
        response = self.client.post(
            reverse("plate_lookup"), {"plate": "ABC123", "region": "TX"}
        )
        self.assertTrue(response.context["confirming"])
        self.assertIsNone(response.context["result"])
        self.assertEqual(self.plate.usage(), 0)

    @override_settings(PLATE_LOOKUP_ENABLED=True, PLATE_LOOKUP_URL="https://plates.example")
    def test_the_confirmation_names_the_plate_and_the_running_count(self):
        response = self.client.post(
            reverse("plate_lookup"), {"plate": "ABC123", "region": "TX"}
        )
        self.assertContains(response, "ABC123")
        self.assertContains(response, "leaves your network")

    def test_the_page_says_why_it_is_off(self):
        response = self.client.get(reverse("plate_lookup"))
        self.assertContains(response, "no free, legal plate-to-VIN service")

    @override_settings(PLATE_LOOKUP_ENABLED=True, PLATE_LOOKUP_URL="https://plates.example")
    def test_a_result_is_fed_to_the_decode_path_rather_than_trusted(self):
        provider = self.FakeProvider()
        with patch("homeautoshop.assets.plate.PlateLookupProvider", return_value=provider):
            response = self.client.post(
                reverse("plate_lookup"), {"plate": "ABC123", "region": "TX", "confirm": "1"}
            )
        self.assertContains(response, "Add a vehicle with this VIN")
        self.assertEqual(Asset.objects.count(), 0)


# --------------------------------------------------------------------------
# CSV import
# --------------------------------------------------------------------------


class CsvImportTests(TestCase):
    """FR-ADM-6 — so the spreadsheet this replaces can actually come along."""

    def setUp(self):
        self.user = User.objects.create_user(username="boss", password="x" * 16, role="admin")
        self.client.force_login(self.user)

    VEHICLES = "Name,VIN,Year,Make,Model\nRed truck,%s,1989,MCI,102DL3\n" % VIN

    def test_it_reads_a_comma_file(self):
        header, rows = csvimport.read(self.VEHICLES)
        self.assertEqual(header, ["Name", "VIN", "Year", "Make", "Model"])
        self.assertEqual(rows[0]["Make"], "MCI")

    def test_it_reads_a_semicolon_file(self):
        """A European export would otherwise arrive as one column per row and
        look like a corrupt file rather than a delimiter mismatch."""
        header, rows = csvimport.read("Name;VIN;Year\nRed truck;%s;1989\n" % VIN)
        self.assertEqual(header, ["Name", "VIN", "Year"])

    def test_it_guesses_the_obvious_columns(self):
        mapping = csvimport.guess("vehicles", ["VIN", "Year", "Make", "Model"])
        self.assertEqual(mapping["vin"], "VIN")
        self.assertEqual(mapping["year"], "Year")

    def test_a_dry_run_writes_nothing(self):
        _header, rows = csvimport.read(self.VEHICLES)
        outcome = csvimport.run(
            "vehicles", rows, {"nickname": "Name", "vin": "VIN", "make": "Make"}, dry_run=True
        )
        self.assertEqual(outcome.created, 1)
        self.assertEqual(Asset.objects.count(), 0)
        self.assertEqual(ExternalRef.objects.count(), 0)

    def test_a_real_run_writes(self):
        _header, rows = csvimport.read(self.VEHICLES)
        csvimport.run(
            "vehicles", rows, {"nickname": "Name", "vin": "VIN", "make": "Make"}, dry_run=False
        )
        self.assertEqual(Asset.objects.get().vin, VIN)

    def test_running_the_same_file_twice_writes_nothing_the_second_time(self):
        """People re-run imports. They add ten rows and import the file again."""
        _header, rows = csvimport.read(self.VEHICLES)
        mapping = {"nickname": "Name", "vin": "VIN", "make": "Make"}
        csvimport.run("vehicles", rows, mapping, dry_run=False)
        second = csvimport.run("vehicles", rows, mapping, dry_run=False)
        self.assertEqual(Asset.objects.count(), 1)
        self.assertEqual(second.created, 0)

    def test_a_vehicle_already_here_by_vin_is_matched_not_duplicated(self):
        Asset.objects.create(nickname="Existing", vin=VIN)
        _header, rows = csvimport.read(self.VEHICLES)
        outcome = csvimport.run(
            "vehicles", rows, {"nickname": "Name", "vin": "VIN"}, dry_run=False
        )
        self.assertEqual(Asset.objects.count(), 1)
        self.assertEqual(outcome.already, 1)

    def test_a_missing_required_column_refuses_before_writing(self):
        _header, rows = csvimport.read(self.VEHICLES)
        outcome = csvimport.run("vehicles", rows, {"vin": "VIN"}, dry_run=False)
        self.assertEqual(outcome.created, 0)
        self.assertTrue(outcome.problems)

    def test_service_history_needs_a_vehicle_it_can_actually_find(self):
        """Attaching a service record to the wrong vehicle is invisible
        afterwards and corrupts every cost figure derived from it."""
        rows = [{"Date": "2026-01-05", "Job": "Oil change", "VIN": "NOTAVIN"}]
        outcome = csvimport.run(
            "service", rows, {"date": "Date", "title": "Job", "vin": "VIN"}, dry_run=True
        )
        self.assertEqual(outcome.created, 0)
        self.assertIn("no vehicle matched", str(outcome.problems[0]))

    def test_service_history_lands_on_the_matched_vehicle(self):
        asset = Asset.objects.create(nickname="Red truck", vin=VIN)
        rows = [{"Date": "2026-01-05", "Job": "Oil change", "VIN": VIN}]
        csvimport.run(
            "service", rows, {"date": "Date", "title": "Job", "vin": "VIN"}, dry_run=False
        )
        self.assertEqual(WorkOrder.objects.get().asset, asset)

    def test_an_ambiguous_name_is_a_question_not_a_coin_toss(self):
        Asset.objects.create(nickname="The truck")
        Asset.objects.create(nickname="the truck")
        rows = [{"Date": "2026-01-05", "Job": "Oil change", "Vehicle": "The truck"}]
        outcome = csvimport.run(
            "service", rows, {"date": "Date", "title": "Job", "vehicle": "Vehicle"}, dry_run=True
        )
        self.assertEqual(outcome.created, 0)

    def test_a_bad_row_does_not_stop_the_good_ones(self):
        rows = [
            {"Name": "Good one", "VIN": VIN},
            {"Name": "", "VIN": ""},
            {"Name": "Also good", "VIN": ""},
        ]
        outcome = csvimport.run(
            "vehicles", rows, {"nickname": "Name", "vin": "VIN"}, dry_run=False
        )
        self.assertEqual(outcome.created, 2)

    def test_the_screen_previews_before_it_writes(self):
        from django.core.files.uploadedfile import SimpleUploadedFile

        self.client.post(
            reverse("data_import"),
            {"kind": "vehicles",
             "file": SimpleUploadedFile("v.csv", self.VEHICLES.encode(), "text/csv")},
        )
        response = self.client.post(
            reverse("data_import"), {"map_nickname": "Name", "map_vin": "VIN"}
        )
        self.assertTrue(response.context["outcome"].dry_run)
        self.assertEqual(Asset.objects.count(), 0)
        self.assertContains(response, "Nothing has been written")


# --------------------------------------------------------------------------
# The offline write queue, server side
# --------------------------------------------------------------------------


class SyncBatchTests(TestCase):
    """SPEC §5.4, §10 — a reconnecting phone makes one round trip."""

    def setUp(self):
        self.user = User.objects.create_user(username="andy", password="x" * 16)
        self.client.force_login(self.user)
        self.asset = Asset.objects.create(nickname="Red truck", vin=VIN, meter="odometer")
        self.wo = WorkOrder.objects.create(asset=self.asset, title="Brakes")

    def _post(self, items):
        return self.client.post(
            "/api/v1/sync/batch",
            json.dumps({"items": items}),
            content_type="application/json",
        )

    def test_a_queued_reading_lands(self):
        response = self._post(
            [{"client_id": "01920000-0000-7000-8000-000000000001", "op": "reading.create",
              "payload": {"asset_id": str(self.asset.pk), "value": 84120}}]
        )
        self.assertEqual(response.json()["results"][0]["status"], 201)
        self.assertEqual(UsageReading.objects.count(), 1)

    def test_the_client_minted_id_is_what_gets_stored(self):
        """That is what makes a replay idempotent rather than a duplicate."""
        client_id = "01920000-0000-7000-8000-000000000002"
        self._post([{"client_id": client_id, "op": "reading.create",
                     "payload": {"asset_id": str(self.asset.pk), "value": 100}}])
        self.assertEqual(str(UsageReading.objects.get().pk), client_id)

    def test_replaying_the_same_queue_writes_nothing_twice(self):
        items = [{"client_id": "01920000-0000-7000-8000-000000000003", "op": "reading.create",
                  "payload": {"asset_id": str(self.asset.pk), "value": 100}}]
        self._post(items)
        self._post(items)
        self.assertEqual(UsageReading.objects.count(), 1)

    def test_one_bad_item_does_not_lose_the_rest(self):
        """A phone with fifty captures and one problem should land forty-nine."""
        response = self._post([
            {"client_id": "01920000-0000-7000-8000-000000000004", "op": "reading.create",
             "payload": {"asset_id": str(self.asset.pk), "value": 100}},
            {"client_id": "01920000-0000-7000-8000-000000000005", "op": "reading.create",
             "payload": {"asset_id": "01920000-0000-7000-8000-00000000ffff", "value": 100}},
            {"client_id": "01920000-0000-7000-8000-000000000006", "op": "note.create",
             "payload": {"work_order_id": str(self.wo.pk), "body": "chased the rattle"}},
        ])
        statuses = [r["status"] for r in response.json()["results"]]
        self.assertEqual(statuses[0], 201)
        self.assertEqual(statuses[2], 201)
        self.assertEqual(UsageReading.objects.count(), 1)

    def test_a_stale_revision_comes_back_409_with_the_current_state(self):
        stale = self.wo.revision
        self.wo.title = "Brakes and a rattle"
        self.wo.save()

        response = self._post([
            {"client_id": "01920000-0000-7000-8000-000000000007", "op": "work_order.status",
             "payload": {"work_order_id": str(self.wo.pk), "status": "in_progress",
                         "revision": stale}}
        ])
        result = response.json()["results"][0]
        self.assertEqual(result["status"], 409)
        self.assertEqual(result["current_revision"], self.wo.revision + 0)

    def test_a_conflict_does_not_apply_the_write(self):
        stale = self.wo.revision
        self.wo.title = "Changed"
        self.wo.save()
        self._post([
            {"client_id": "01920000-0000-7000-8000-000000000008", "op": "work_order.status",
             "payload": {"work_order_id": str(self.wo.pk), "status": "in_progress",
                         "revision": stale}}
        ])
        self.wo.refresh_from_db()
        self.assertEqual(self.wo.status, "planned")

    def test_an_unknown_operation_is_refused_rather_than_dispatched(self):
        """The queue names operations rather than carrying URLs, so a file in a
        browser's storage cannot be a way to reach every endpoint."""
        response = self._post([
            {"client_id": "01920000-0000-7000-8000-000000000009", "op": "user.promote",
             "payload": {}}
        ])
        self.assertEqual(response.json()["results"][0]["status"], 400)

    def test_it_needs_a_login(self):
        self.client.logout()
        response = self._post([])
        self.assertIn(response.status_code, (401, 403))


class PwaTests(TestCase):
    """§9.4 — installable, and the worker actually controls the pages."""

    def setUp(self):
        self.user = User.objects.create_user(username="andy", password="x" * 16)
        self.client.force_login(self.user)

    def test_the_worker_is_served_from_the_root(self):
        """At /static/sw.js its scope would be /static/ — it would register
        cleanly, report as active, and intercept nothing."""
        response = self.client.get("/sw.js")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Service-Worker-Allowed"], "/")
        self.assertIn("javascript", response["Content-Type"])

    def test_the_worker_is_not_cached(self):
        """A stale worker keeps serving a stale app, and it looks like a failed
        deploy rather than a caching problem."""
        self.assertEqual(self.client.get("/sw.js")["Cache-Control"], "no-cache")

    def test_the_page_registers_the_worker_from_the_root(self):
        """The gap the two tests above left, and it cost the whole feature.

        They proved `/sw.js` was served correctly and said nothing about
        whether anything asked for it — and `offline.js` asked for
        `/static/sw.js`. So the worker registered with a scope of `/static/`:
        no page was ever cached, the offline landing was unreachable, and
        Background Sync woke a worker watching a directory of stylesheets.

        Worse than the missing half was the half that did run. `/static/` is
        cache-first against a literal `VERSION`, so `shell-v1` was written on a
        browser's first ever visit and served for the life of the install —
        every later release of the stylesheet and of every script was fetched
        into a cache nothing read, and looked to the operator like a change
        that had not deployed.
        """
        source = (Path(settings.BASE_DIR) / "static" / "offline.js").read_text(
            encoding="utf-8"
        )
        self.assertIn('register("/sw.js")', source)
        self.assertNotIn('register("/static/sw.js")', source)

    def test_a_worker_left_at_the_wrong_scope_is_retired(self):
        """An upgrade has to undo the bad registration, not just stop making it.

        Where two registrations match a request the more specific scope wins,
        so a browser still holding the `/static/` worker would go on being
        served the frozen shell by it while the correct one sat at `/` with
        nothing to do.
        """
        source = (Path(settings.BASE_DIR) / "static" / "offline.js").read_text(
            encoding="utf-8"
        )
        self.assertIn("getRegistrations", source)
        self.assertIn("unregister()", source)

    def test_the_manifest_has_the_icons_installability_needs(self):
        import json as _json
        from pathlib import Path

        from django.conf import settings as _settings

        manifest = _json.loads(
            (Path(_settings.BASE_DIR) / "static" / "manifest.webmanifest").read_text()
        )
        sizes = {icon["sizes"] for icon in manifest["icons"]}
        self.assertIn("192x192", sizes)
        self.assertIn("512x512", sizes)
        self.assertTrue(any(i.get("purpose") == "maskable" for i in manifest["icons"]))

    def test_every_manifest_icon_actually_exists(self):
        import json as _json
        from pathlib import Path

        from django.conf import settings as _settings

        root = Path(_settings.BASE_DIR)
        manifest = _json.loads((root / "static" / "manifest.webmanifest").read_text())
        for icon in manifest["icons"]:
            self.assertTrue(
                (root / icon["src"].lstrip("/")).exists(), f"{icon['src']} is not there"
            )

    def test_the_offline_page_exists_because_the_worker_falls_back_to_it(self):
        from pathlib import Path

        from django.conf import settings as _settings

        self.assertTrue((Path(_settings.BASE_DIR) / "static" / "offline.html").exists())

    def test_the_sync_indicator_is_on_every_page(self):
        # The script URL is resolved through `static()` rather than written
        # out: with STATIC_HASHED on — which is how the container runs — the
        # file is served as `offline.<hash>.js`, so a literal filename is an
        # assertion that only holds in development.
        from django.templatetags.static import static

        page = self.client.get(reverse("dashboard")).content.decode()
        self.assertIn('id="sync-indicator"', page)
        self.assertIn('id="offline-strings"', page)
        self.assertIn(static("offline.js"), page)


class WebPushTests(TestCase):
    """§9.4 — the one feature that cannot avoid a large cloud service."""

    def setUp(self):
        from homeautoshop.core import webpush

        self.webpush = webpush
        self.user = User.objects.create_user(username="boss", password="x" * 16, role="admin")
        self.client.force_login(self.user)

    FCM = "https://fcm.googleapis.com/fcm/send/abc123"

    def test_a_key_pair_is_generated_once_and_kept(self):
        first = self.webpush.public_key()
        self.assertTrue(first)
        self.assertEqual(first, self.webpush.public_key())

    def test_the_private_key_is_never_sent_to_the_browser(self):
        response = self.client.get(reverse("push_subscribe")).json()
        self.assertNotIn("PRIVATE", response["key"])
        self.assertEqual(response["key"], self.webpush.public_key())

    def test_a_known_push_service_is_accepted(self):
        self.assertTrue(self.webpush.endpoint_allowed(self.FCM))
        self.assertTrue(
            self.webpush.endpoint_allowed("https://updates.push.services.mozilla.com/wpush/v2/x")
        )

    def test_an_endpoint_anywhere_else_is_refused(self):
        """A subscription is written by script; the allowlist is what stops this
        becoming a general-purpose outbound POST."""
        self.assertFalse(self.webpush.endpoint_allowed("https://evil.example/collect"))
        self.assertFalse(self.webpush.endpoint_allowed("https://fcm.googleapis.com.evil.example/x"))
        self.assertFalse(self.webpush.endpoint_allowed(""))

    def test_subscribing_creates_an_enabled_channel(self):
        from homeautoshop.core.models import NotificationChannel

        response = self.client.post(
            reverse("push_subscribe"),
            json.dumps({"subscription": {"endpoint": self.FCM, "keys": {"p256dh": "k", "auth": "a"}}}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        channel = NotificationChannel.objects.get(kind="webpush")
        self.assertTrue(channel.is_enabled)
        self.assertEqual(channel.subscription["endpoint"], self.FCM)

    def test_subscribing_twice_from_one_browser_makes_one_channel(self):
        from homeautoshop.core.models import NotificationChannel

        body = json.dumps({"subscription": {"endpoint": self.FCM, "keys": {}}})
        self.client.post(reverse("push_subscribe"), body, content_type="application/json")
        self.client.post(reverse("push_subscribe"), body, content_type="application/json")
        self.assertEqual(NotificationChannel.objects.filter(kind="webpush").count(), 1)

    def test_a_foreign_endpoint_is_refused_at_the_door(self):
        from homeautoshop.core.models import NotificationChannel

        response = self.client.post(
            reverse("push_subscribe"),
            json.dumps({"subscription": {"endpoint": "https://evil.example/x"}}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(NotificationChannel.objects.count(), 0)

    def test_the_endpoint_is_not_echoed_back_to_a_screen(self):
        """It is a bearer capability for that browser. Showing it gives it away."""
        from homeautoshop.core.models import NotificationChannel

        channel = NotificationChannel.objects.create(
            kind="webpush", name="Phone", target=self.FCM,
            subscription={"endpoint": self.FCM},
        )
        self.assertNotIn("abc123", channel.masked_target)
        self.assertIn("fcm.googleapis.com", channel.masked_target)

    @override_settings(OFFLINE_MODE=True)
    def test_offline_mode_stops_it(self):
        from homeautoshop.core.models import NotificationChannel

        channel = NotificationChannel.objects.create(
            kind="webpush", name="Phone", target=self.FCM, subscription={"endpoint": self.FCM}
        )
        self.assertFalse(self.webpush.available())
        with self.assertRaises(self.webpush.PushUnavailable):
            self.webpush.send(channel, title="x", body="y")

    def test_a_gone_subscription_disables_the_channel_rather_than_retrying(self):
        from pywebpush import WebPushException

        from homeautoshop.core.models import NotificationChannel

        channel = NotificationChannel.objects.create(
            kind="webpush", name="Phone", target=self.FCM,
            subscription={"endpoint": self.FCM, "keys": {"p256dh": "k", "auth": "a"}},
            is_enabled=True,
        )
        gone = WebPushException("gone")
        gone.response = type("R", (), {"status_code": 410})()

        with patch("pywebpush.webpush", side_effect=gone):
            with self.assertRaises(self.webpush.PushUnavailable):
                self.webpush.send(channel, title="x", body="y")

        channel.refresh_from_db()
        self.assertFalse(channel.is_enabled)

    def test_the_notification_never_names_a_vehicle(self):
        """It renders on a lock screen and passes through a third party."""
        from homeautoshop.core.models import NotificationChannel
        from homeautoshop.core.notifications import Alert, Digest, _send_push

        channel = NotificationChannel.objects.create(
            kind="webpush", name="Phone", target=self.FCM, subscription={"endpoint": self.FCM}
        )
        digest = Digest(
            alerts=[
                Alert(
                    dedupe_key="x",
                    severity="overdue",
                    title="Brake fluid on the Silverado",
                    detail="overdue by 400 mi",
                )
            ]
        )
        with patch("homeautoshop.core.webpush.send") as send:
            _send_push(channel, "subject", digest)

        body = send.call_args.kwargs
        self.assertNotIn("Silverado", body["title"] + body["body"])
        self.assertIn("1", body["body"])

    def test_push_is_not_offered_as_a_kind_to_type_a_target_into(self):
        page = self.client.get(reverse("reminders"))
        self.assertNotIn("webpush", [value for value, _label in page.context["kinds"]])
        self.assertContains(page, "Notify this device")


class ToolDrainTests(TestCase):
    """Reading the whole tool list, whatever shape the envelope is.

    Reported as: most tools cannot be found by name, and the few that can look
    arbitrary. Three assumptions in this client had never met the real API —
    every test above mocks the client itself — and each one produces exactly
    that symptom.
    """

    def client_returning(self, pages):
        """A client whose `get` replays `pages` in order."""
        client = wl.WrenchLedgerClient(api_key="wlk_test")
        calls = []

        def get(path, **params):
            calls.append((path, params))
            return pages[min(len(calls) - 1, len(pages) - 1)]

        client.get = get
        client.calls = calls
        return client

    def test_a_cursor_under_meta_is_still_a_cursor(self):
        """The rows were looked for in four places and the cursor in one. An
        envelope that pages under `meta` therefore looked like a finished drain
        after page one."""
        client = self.client_returning(
            [
                {"data": [{"id": "t1", "updated_at": "2026-01-02"}],
                 "meta": {"next_cursor": "c2"}},
                {"data": [{"id": "t2", "updated_at": "2026-01-01"}], "meta": {}},
            ]
        )

        rows, watermark = client.tools_changed_since(None)

        self.assertEqual([row["id"] for row in rows], ["t1", "t2"])
        self.assertEqual(watermark, "2026-01-02")

    def test_every_cursor_spelling_this_api_might_use(self):
        for envelope in (
            {"next_cursor": "c"}, {"nextCursor": "c"}, {"cursor": "c"},
            {"next": "c"}, {"meta": {"cursor": "c"}}, {"pagination": {"next": "c"}},
            {"links": {"next": "c"}},
        ):
            with self.subTest(envelope=envelope):
                self.assertEqual(wl._cursor({"data": [], **envelope}), "c")

    def test_a_full_page_with_no_cursor_leaves_the_watermark_alone(self):
        """The silent one. A page we could not continue used to advance the
        watermark to its newest row, and `updated_after` is strictly greater —
        so every older tool was skipped for good, by one run, invisibly."""
        page = [
            {"id": f"t{n}", "updated_at": "2026-01-%02d" % (n % 28 + 1)}
            for n in range(wl.PAGE_SIZE)
        ]
        client = self.client_returning([{"data": page}])

        rows, watermark = client.tools_changed_since("2025-01-01")

        self.assertEqual(len(rows), wl.PAGE_SIZE)
        self.assertEqual(watermark, "2025-01-01", "the watermark moved past unread tools")

    def test_a_short_page_with_no_cursor_is_a_finished_drain(self):
        client = self.client_returning([{"data": [{"id": "t1", "updated_at": "2026-02-02"}]}])
        rows, watermark = client.tools_changed_since(None)
        self.assertEqual(len(rows), 1)
        self.assertEqual(watermark, "2026-02-02")

    def test_the_drain_asks_for_a_page_size_of_its_own(self):
        """Otherwise 'a full page' is whatever the server felt like sending, and
        the check above cannot be made."""
        client = self.client_returning([{"data": []}])
        client.tools_changed_since(None)
        self.assertEqual(client.calls[0][1]["limit"], wl.PAGE_SIZE)

    def test_the_drain_says_where_the_cursor_was(self):
        """A truncated drain looks exactly like a short one, so where the token
        lives is worth a line the day the envelope changes again."""
        client = self.client_returning(
            [
                {"data": [{"id": "t1"}], "meta": {"next_cursor": "c2"}},
                {"data": [{"id": "t2"}]},
            ]
        )

        with self.assertLogs("homeautoshop.core.integrations.wrenchledger", "INFO") as logged:
            client.tools_changed_since(None)

        self.assertTrue(any("meta.next_cursor" in line for line in logged.output))

    def test_it_says_so_only_once_however_many_pages(self):
        client = self.client_returning(
            [
                {"data": [{"id": "t1"}], "next_cursor": "c2"},
                {"data": [{"id": "t2"}], "next_cursor": "c3"},
                {"data": [{"id": "t3"}]},
            ]
        )

        with self.assertLogs("homeautoshop.core.integrations.wrenchledger", "INFO") as logged:
            client.tools_changed_since(None)

        paging = [line for line in logged.output if "pages /tools by" in line]
        self.assertEqual(len(paging), 1)

    def test_a_match_may_be_on_the_brand_or_the_model(self):
        row = {"id": "t1", "name": "Pump", "brand": "Robinair", "model": "15600"}
        self.assertTrue(wl.matches(row, "robinair"))
        self.assertTrue(wl.matches(row, "15600"))
        self.assertFalse(wl.matches(row, "snap-on"))

    def test_nothing_matches_an_empty_query(self):
        self.assertFalse(wl.matches({"name": "Anything"}, "  "))


@override_settings(WRENCHLEDGER_API_KEY="wlk_live_test")
class RebuildTests(TestCase):
    """Repairing a cache an earlier truncated run left with holes."""

    class FakeClient:
        base_url = wl.DEFAULT_BASE

        def __init__(self):
            self.asked_for = []

        def open_loans(self):
            return []

        def tools_changed_since(self, watermark):
            self.asked_for.append(watermark)
            if watermark is None:
                return [
                    {"id": "t1", "name": "Vacuum pump", "updated_at": "2026-01-01"},
                    {"id": "t2", "name": "Torque wrench", "updated_at": "2026-02-01"},
                ], "2026-02-01"
            return [], watermark

        def schedules_for(self, tool_id):
            return []

    def test_an_ordinary_run_asks_only_for_what_changed(self):
        from homeautoshop.core.models import Setting

        Setting.put(wl.WATERMARK_KEY, "2026-02-01")
        client = self.FakeClient()

        wl.sync(client=client)

        self.assertEqual(client.asked_for, ["2026-02-01"])
        self.assertEqual(ShopTool.objects.count(), 0)

    def test_a_rebuild_forgets_the_watermark_and_reads_everything(self):
        """A delta poll is only as complete as every run before it, so fixing
        the code that truncated does not fix the rows it skipped."""
        from homeautoshop.core.models import Setting

        Setting.put(wl.WATERMARK_KEY, "2026-02-01")
        client = self.FakeClient()

        wl.sync(client=client, rebuild=True)

        self.assertEqual(client.asked_for, [None])
        self.assertEqual(
            sorted(ShopTool.objects.values_list("name", flat=True)),
            ["Torque wrench", "Vacuum pump"],
        )

    def test_the_screen_offers_it(self):
        user = User.objects.create_user(
            username="andy", password="x" * 16, role=Role.ADMIN
        )
        self.client.force_login(user)
        response = self.client.get(reverse("integrations"))
        self.assertContains(response, "Read every tool again")
        self.assertContains(response, "Tools known here")
