"""VIN handling and asset behavior (SPEC FR-VEH-*, §8.1)."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.urls import reverse

from homeautoshop.accounts.models import User
from homeautoshop.core.outbound import OutboundBlocked, OutboundFailed
from homeautoshop.work.models import WorkOrder

from . import vin as vinlib
from .models import Asset, AssetKind, ServiceInfoProvider, UsageReading
from .services import decode_vin, record_reading

# The canonical ISO 3779 worked example, whose check digit is X. Using a
# published VIN rather than an invented one means the fixture itself is
# verifiable — an invented VIN with a made-up check digit would have tested
# nothing except that the code agreed with the test author.
GOOD_VIN = "1M8GDM9AXKP042788"


class VinValidationTests(TestCase):
    """FR-VEH-2 — validation is local, and runs before any network call."""

    def test_valid_vin_passes_check_digit(self):
        check = vinlib.validate(GOOD_VIN)
        self.assertTrue(check.is_well_formed)
        self.assertTrue(check.check_digit_valid)
        self.assertEqual(check.status, "valid")
        self.assertFalse(check.errors)

    def test_forbidden_letters_are_rejected_with_a_useful_reason(self):
        check = vinlib.validate("1M8GDM9AXKI042788")  # I is never valid in a VIN
        self.assertFalse(check.is_well_formed)
        self.assertIn("I", check.errors[0])

    def test_a_shorter_vin_is_read_as_the_era_it_came_from(self):
        """It used to be an error carrying a warning that said to save it
        anyway — advice the model then refused to take. Seventeen characters
        is the rule from 1981; `tests_vin_eras.py` covers the rest."""
        check = vinlib.validate("1M8GDM9AX")
        self.assertTrue(check.is_well_formed)
        self.assertTrue(check.is_pre_1981)
        self.assertFalse(check.errors)

    def test_bad_check_digit_warns_but_does_not_block(self):
        bad = GOOD_VIN[:8] + ("0" if GOOD_VIN[8] != "0" else "1") + GOOD_VIN[9:]
        check = vinlib.validate(bad)
        self.assertTrue(check.is_well_formed)
        self.assertFalse(check.check_digit_valid)
        # A warning, not an error: imports legitimately fail this.
        self.assertFalse(check.errors)
        self.assertTrue(check.warnings)

    def test_model_year_is_ambiguous_across_the_30_year_cycle(self):
        years = vinlib.possible_model_years("J", reference_year=2026)
        self.assertIn(1988, years)
        self.assertIn(2018, years)

    def test_normalization(self):
        self.assertEqual(vinlib.normalize(" 1m8-gdm9axkp042788 "), GOOD_VIN)

    def test_masking_keeps_enough_to_identify_without_exposing(self):
        masked = vinlib.mask(GOOD_VIN)
        self.assertTrue(masked.startswith("1M8"))
        self.assertTrue(masked.endswith("042788"))
        self.assertNotIn(GOOD_VIN[4:8], masked)


class AssetTests(TestCase):
    def test_only_a_nickname_is_required(self):
        """FR-VEH-1 — a half-known project car must still be recordable."""
        asset = Asset.objects.create(nickname="Barn find")
        self.assertEqual(asset.status, "active")
        self.assertEqual(asset.vin_status, "none")

    def test_vin_status_derives_on_save(self):
        asset = Asset.objects.create(nickname="Civic", vin=GOOD_VIN.lower())
        self.assertEqual(asset.vin, GOOD_VIN)
        self.assertEqual(asset.vin_status, "valid")

    def test_equipment_defaults_to_an_hour_meter(self):
        """FR-EQP-2 — nothing may assume an odometer."""
        mower = Asset.objects.create(nickname="Mower", asset_kind=AssetKind.EQUIPMENT)
        self.assertEqual(mower.meter, "engine_hours")
        self.assertEqual(mower.meter_unit, "hours")
        self.assertEqual(mower.vehicle_class, "")

    def test_fleet_excludes_prospects_and_disposals(self):
        Asset.objects.create(nickname="Daily")
        Asset.objects.create(nickname="Looking at", status="prospect")
        Asset.objects.create(nickname="Gone", status="sold")
        self.assertEqual(Asset.objects.fleet().count(), 1)


class UsageReadingTests(TestCase):
    def setUp(self):
        self.asset = Asset.objects.create(nickname="Truck", meter_unit="mi")

    def test_canonical_value_is_stored_for_comparison(self):
        reading = record_reading(self.asset, 100, unit="mi")
        self.assertAlmostEqual(float(reading.value_canonical), 160.9344, places=3)
        # The entered value is untouched.
        self.assertEqual(float(reading.value), 100.0)

    def test_decrease_is_allowed_but_flagged(self):
        """FR-VEH-9 — cluster swaps and rollbacks are real."""
        record_reading(self.asset, 100_000)
        lower = record_reading(self.asset, 12_000, note="Replaced cluster")
        self.assertTrue(lower.is_rollback)
        self.assertTrue(UsageReading.objects.filter(pk=lower.pk).exists())

    def test_latest_reading_drives_current_usage(self):
        record_reading(self.asset, 1000)
        record_reading(self.asset, 2000)
        self.assertEqual(float(self.asset.current_usage), 2000.0)


class DecodeTests(TestCase):
    """SPEC §8.1 — explicit, timeout-bounded, override-preserving."""

    def setUp(self):
        self.asset = Asset.objects.create(nickname="Civic", vin=GOOD_VIN)

    @patch("homeautoshop.assets.services.fetch_json")
    def test_decode_fills_blanks_and_retains_the_raw_response(self, fetch):
        fetch.return_value.data = {
            "Results": [
                {"ModelYear": "1988", "Make": "HONDA", "Model": "Accord",
                 "BodyClass": "Sedan/Saloon", "DisplacementL": "2.0",
                 "EngineCylinders": "4", "EngineConfiguration": "Inline"}
            ]
        }
        result = decode_vin(self.asset)
        self.asset.refresh_from_db()
        self.assertTrue(result.ok)
        self.assertEqual(self.asset.year, 1988)
        self.assertEqual(self.asset.make, "HONDA")
        self.assertEqual(self.asset.engine, "2.0L I4")
        self.assertEqual(self.asset.vehicle_class, "car")
        # Raw payload retained so a better mapping can re-derive later.
        self.assertEqual(self.asset.decoded_raw["Make"], "HONDA")

    @patch("homeautoshop.assets.services.fetch_json")
    def test_decode_never_clobbers_a_human_correction(self, fetch):
        """FR-VEH-4 — the operator knows it is an SE-R; vPIC says SE."""
        self.asset.trim = "SE-R"
        self.asset.field_overrides = {"trim": {"value": "SE-R"}}
        self.asset.save()
        fetch.return_value.data = {"Results": [{"Trim": "SE", "Make": "NISSAN"}]}

        result = decode_vin(self.asset)
        self.asset.refresh_from_db()
        self.assertEqual(self.asset.trim, "SE-R")
        self.assertIn("trim", result.skipped_overridden)

    @patch("homeautoshop.assets.services.fetch_json", side_effect=OutboundFailed("timeout"))
    def test_failure_degrades_instead_of_blocking(self, _fetch):
        """P-7 — a failed lookup never prevents recording the vehicle."""
        result = decode_vin(self.asset)
        self.assertFalse(result.ok)
        self.assertIn("Enter what you know", result.message)
        self.assertTrue(Asset.objects.filter(pk=self.asset.pk).exists())

    @override_settings(OFFLINE_MODE=True)
    def test_offline_mode_reports_intent_not_an_error(self):
        result = decode_vin(self.asset)
        self.assertFalse(result.ok)
        self.assertIn("Offline Mode", result.message)

    def test_equipment_is_never_routed_through_vin_decode(self):
        """FR-EQP-3 — hidden rather than shown-and-failing."""
        mower = Asset.objects.create(nickname="Mower", asset_kind=AssetKind.EQUIPMENT)
        self.assertFalse(decode_vin(mower).ok)


class OutboundGuardTests(TestCase):
    def test_host_not_on_the_allowlist_is_refused_before_any_socket(self):
        from homeautoshop.core.outbound import fetch_json

        with self.assertRaises(OutboundBlocked):
            fetch_json("https://example.com/data.json")

    @override_settings(OFFLINE_MODE=True)
    def test_offline_mode_blocks_even_an_allowlisted_host(self):
        from homeautoshop.core.outbound import fetch_json

        with self.assertRaises(OutboundBlocked):
            fetch_json("https://vpic.nhtsa.dot.gov/api/vehicles/x")


class ServiceInfoTests(TestCase):
    """SPEC §8.5 — deep-link only as far as the pattern is deterministic."""

    def setUp(self):
        self.provider = ServiceInfoProvider.objects.create(
            name="LEMON", slug="lemon", base_urls=["https://lemon-manuals.la"],
            url_template="{make}/{year}/", deep_link_depth="make_year",
        )

    def test_browse_url_stops_at_make_and_year(self):
        asset = Asset.objects.create(nickname="Accord", make="Honda", year=2000)
        self.assertEqual(self.provider.browse_url(asset), "https://lemon-manuals.la/Honda/2000/")

    def test_missing_make_falls_back_to_the_site_root(self):
        asset = Asset.objects.create(nickname="Mystery")
        self.assertEqual(self.provider.browse_url(asset), "https://lemon-manuals.la")


class AssetViewTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("andy", password="correct-horse-battery")
        self.client.force_login(self.user)

    def test_create_and_view(self):
        response = self.client.post(
            reverse("asset_create"), {"nickname": "Red truck", "asset_kind": "vehicle", "status": "active"}
        )
        self.assertEqual(response.status_code, 302)
        asset = Asset.objects.get(nickname="Red truck")
        self.assertEqual(self.client.get(reverse("asset_detail", args=[asset.pk])).status_code, 200)

    def test_vin_feedback_endpoint_needs_no_network(self):
        response = self.client.get(reverse("vin_validate"), {"vin": GOOD_VIN})
        self.assertContains(response, "Check digit valid")

    def test_anonymous_is_redirected_to_login(self):
        self.client.logout()
        self.assertEqual(self.client.get(reverse("asset_list")).status_code, 302)


class TimelineTests(TestCase):
    """FR-VEH-10 — the story, at two sizes.

    Reported as the vehicle page being a scroll of photographs with the meter
    and the identity panel somewhere below it: a photograph is one row and a
    work order is one row, and there are far more photographs.
    """

    def setUp(self):
        from homeautoshop.mediafiles.testing import local_storage

        import shutil
        import tempfile
        from pathlib import Path

        self.tmp = Path(tempfile.mkdtemp())
        self.storage = local_storage(self.tmp)
        self.storage.enable()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.addCleanup(self.storage.disable)

        self.user = User.objects.create_user("andy", password="correct-horse-battery")
        self.client.force_login(self.user)
        self.asset = Asset.objects.create(nickname="Red truck")

    def add_photos(self, n: int) -> None:
        from django.core.files.uploadedfile import SimpleUploadedFile

        from homeautoshop.mediafiles.services import ingest

        for i in range(n):
            ingest(
                SimpleUploadedFile(f"shot{i}.jpg", f"jpeg-{i}".encode(), content_type="image/jpeg"),
                entity=self.asset,
            )

    def story(self) -> list:
        from .views import _group_media, _timeline

        return _group_media(_timeline(self.asset))

    def test_a_days_photographs_become_one_entry(self):
        self.add_photos(4)
        story = self.story()
        self.assertEqual(len(story), 1, "four photographs took four rows")
        self.assertEqual(story[0]["kind"], "media_group")
        self.assertIn("4", story[0]["title"])
        self.assertEqual(len(story[0]["children"]), 4)

    def test_a_single_photograph_is_not_called_a_group_of_one(self):
        self.add_photos(1)
        story = self.story()
        self.assertEqual(story[0]["kind"], "media")

    def test_work_orders_keep_their_own_row(self):
        """Grouping is about photographs, not about shortening the history."""
        self.add_photos(3)
        WorkOrder.objects.create(asset=self.asset, title="Front brakes")

        kinds = [event["kind"] for event in self.story()]

        self.assertIn("work_order", kinds)
        self.assertIn("media_group", kinds)
        self.assertEqual(len(kinds), 2)

    def test_the_vehicle_page_shows_a_summary_and_offers_the_rest(self):
        from .views import RECENT_EVENTS

        for i in range(RECENT_EVENTS + 3):
            WorkOrder.objects.create(asset=self.asset, title=f"Job {i}")

        page = self.client.get(reverse("asset_detail", args=[self.asset.pk]))

        self.assertEqual(len(page.context["timeline"]), RECENT_EVENTS)
        self.assertTrue(page.context["history_is_longer"])
        self.assertContains(page, reverse("asset_timeline", args=[self.asset.pk]))

    def test_a_short_history_does_not_offer_a_link_to_more_of_it(self):
        WorkOrder.objects.create(asset=self.asset, title="Only job")
        page = self.client.get(reverse("asset_detail", args=[self.asset.pk]))
        self.assertFalse(page.context["history_is_longer"])
        self.assertNotContains(page, reverse("asset_timeline", args=[self.asset.pk]))

    def test_the_history_page_groups_nothing(self):
        """On a page about the history, "four photos" answers nothing."""
        self.add_photos(4)

        page = self.client.get(reverse("asset_timeline", args=[self.asset.pk]))

        self.assertEqual(page.status_code, 200)
        self.assertEqual(len(page.context["timeline"]), 4)
        self.assertNotIn("media_group", [e["kind"] for e in page.context["timeline"]])


class RecallServiceQuirkTests(TestCase):
    """NHTSA answers 400 when a vehicle has no campaigns (SPEC §8.4).

    It sends a good body with it — `{"Count": 0, "Message": "Results returned
    successfully", "results": []}` — so reading only the status turns "this
    vehicle is clear" into "the recall service is down". Verified against the
    live API: a 2019 Ford Explorer returns 200 with six campaigns; a 2025
    Subaru Crosstrek returns 400 with none.
    """

    def setUp(self):
        from homeautoshop.assets.models import Asset

        self.asset = Asset.objects.create(
            nickname="Crosstrek", year=2025, make="SUBARU", model="Crosstrek"
        )

    @staticmethod
    def _no_campaigns():
        from homeautoshop.core.outbound import OutboundFailed

        return OutboundFailed(
            "HTTP 400",
            status=400,
            body={"Count": 0, "Message": "Results returned successfully", "results": []},
        )

    def test_a_vehicle_with_no_campaigns_is_not_reported_as_an_outage(self):
        from unittest.mock import patch

        from homeautoshop.assets import recalls

        with patch("homeautoshop.assets.recalls.fetch_json", side_effect=self._no_campaigns()):
            result = recalls.check(self.asset)

        self.assertNotIn("Could not reach", result.message)
        self.assertIn("no campaigns", result.message)

    def test_an_empty_400_is_reported_as_inconclusive_not_as_no_recalls(self):
        """The measurement that forced this: NHTSA answers a rate-limited
        request with the *same* status and the *same* body as a vehicle with no
        campaigns. A 2020 Outback returns 400/0 inside a burst and 200 with six
        campaigns after a pause. Reading that as a clean bill of health would
        be silent, confident, and wrong in the dangerous direction."""
        from unittest.mock import patch

        from homeautoshop.assets import recalls

        with patch("homeautoshop.assets.recalls.fetch_json", side_effect=self._no_campaigns()):
            result = recalls.check(self.asset)

        self.assertTrue(result.inconclusive)
        self.assertFalse(result.ok)
        self.assertIn("rate-limiting", result.message)
        self.assertIn("not a clean bill of health", result.message)

    def test_it_retries_once_before_calling_an_empty_answer_ambiguous(self):
        """The retry is what turns some rate limits back into real answers."""
        from unittest.mock import patch

        from homeautoshop.assets import recalls

        good = type("R", (), {"data": {"results": [{"NHTSACampaignNumber": "24V001"}]}})()
        with patch(
            "homeautoshop.assets.recalls.fetch_json",
            side_effect=[self._no_campaigns(), good],
        ) as fetch:
            result = recalls.check(self.asset)

        self.assertEqual(fetch.call_count, 2)
        self.assertTrue(result.ok)
        self.assertFalse(result.inconclusive)
        self.assertEqual(result.created, 1)

    def test_a_200_with_an_empty_list_is_a_real_no_campaigns_answer(self):
        from unittest.mock import patch

        from homeautoshop.assets import recalls

        with patch("homeautoshop.assets.recalls.fetch_json") as fetch:
            fetch.return_value.data = {"Count": 0, "results": []}
            result = recalls.check(self.asset)

        self.assertTrue(result.ok)
        self.assertFalse(result.inconclusive)
        self.assertIn("not the same as the vehicle being clear", result.message)

    def test_a_real_outage_still_reads_as_an_outage(self):
        from unittest.mock import patch

        from homeautoshop.assets import recalls
        from homeautoshop.core.outbound import OutboundFailed

        with patch(
            "homeautoshop.assets.recalls.fetch_json",
            side_effect=OutboundFailed("timeout", status=0, body=None),
        ):
            result = recalls.check(self.asset)

        self.assertFalse(result.ok)
        self.assertIn("Could not reach", result.message)

    def test_a_model_with_a_space_is_encoded(self):
        from unittest.mock import patch

        from homeautoshop.assets import recalls
        from homeautoshop.assets.models import Asset

        jeep = Asset.objects.create(
            nickname="Jeep", year=2018, make="JEEP", model="Grand Cherokee"
        )
        with patch("homeautoshop.assets.recalls.fetch_json") as fetch:
            fetch.return_value.data = {"results": []}
            recalls.check(jeep)

        called = fetch.call_args[0][0]
        self.assertIn("Grand%20Cherokee", called)
        self.assertNotIn("Grand Cherokee", called)


class ServiceManualVisibilityTests(TestCase):
    """A provider that will never have a link is a box that never gets filled.

    OQ-11 already says providers are show/hide-able per vehicle, for ALLDATA's
    per-vehicle subscriptions. The same mechanism answers the plainer case:
    CHARM has no entry for a 2025 Crosstrek and never will.
    """

    def setUp(self):
        from homeautoshop.accounts.models import User
        from homeautoshop.assets.models import Asset, ServiceInfoProvider

        self.user = User.objects.create_user(username="andy", password="x" * 16)
        self.client.force_login(self.user)
        self.asset = Asset.objects.create(nickname="Crosstrek", year=2025, make="SUBARU")
        self.provider = ServiceInfoProvider.objects.create(
            name="Operation CHARM", slug="charm", sort_order=20
        )

    def _url(self):
        from django.urls import reverse

        return reverse("service_info_visibility", args=[self.asset.pk, self.provider.pk])

    def test_hiding_takes_it_off_the_vehicle(self):
        from django.urls import reverse

        self.client.post(self._url(), {"hide": "1"})
        page = self.client.get(reverse("asset_detail", args=[self.asset.pk]))
        self.assertEqual([p for p, _link, _browse in page.context["providers"]], [])
        self.assertEqual(page.context["hidden_providers"], [self.provider])

    def test_it_is_hidden_on_this_vehicle_only(self):
        """CHARM is useless for the Crosstrek and the best source for the truck."""
        from django.urls import reverse

        from homeautoshop.assets.models import Asset

        other = Asset.objects.create(nickname="Truck", year=2007, make="FORD")
        self.client.post(self._url(), {"hide": "1"})
        page = self.client.get(reverse("asset_detail", args=[other.pk]))
        self.assertIn(self.provider, [p for p, _link, _browse in page.context["providers"]])

    def test_hiding_can_be_undone(self):
        from django.urls import reverse

        self.client.post(self._url(), {"hide": "1"})
        self.client.post(self._url(), {})
        page = self.client.get(reverse("asset_detail", args=[self.asset.pk]))
        self.assertIn(self.provider, [p for p, _link, _browse in page.context["providers"]])

    def test_restoring_leaves_no_empty_row_behind(self):
        from homeautoshop.assets.models import AssetServiceInfoLink

        self.client.post(self._url(), {"hide": "1"})
        self.client.post(self._url(), {})
        self.assertEqual(AssetServiceInfoLink.all_objects.count(), 0)

    def test_hiding_a_pinned_provider_keeps_the_pin(self):
        """Hidden is about the shelf, not about the address you found once."""
        from homeautoshop.assets.models import AssetServiceInfoLink

        AssetServiceInfoLink.objects.create(
            asset=self.asset, provider=self.provider, url="http://x.test/vehicles/a/"
        )
        self.client.post(self._url(), {"hide": "1"})
        self.client.post(self._url(), {})
        self.assertEqual(
            AssetServiceInfoLink.objects.get().url, "http://x.test/vehicles/a/"
        )

    def test_the_page_offers_a_way_back(self):
        from django.urls import reverse

        self.client.post(self._url(), {"hide": "1"})
        page = self.client.get(reverse("asset_detail", args=[self.asset.pk]))
        self.assertContains(page, "Hidden here")
        self.assertContains(page, "Operation CHARM")


class DtcManualLinkTests(TestCase):
    """Deriving the DTC index from a pinned address (SPEC §8.5)."""

    PINNED = (
        "http://manuals.home.arpa/vehicles/"
        "2007%20Ford%20Truck%20F%20150%204WD%20V8-5.4L%20VIN%20V%20Flex%20Fuel/"
        "Repair%2520and%2520Diagnosis/index.html"
    )
    DTC_PATH = (
        "Repair%2520and%2520Diagnosis/"
        "A%2520L%2520L%2520%2520Diagnostic%2520Trouble%2520Codes%2520%2528%2520DTC%2520%2529/"
        "index.html"
    )

    def setUp(self):
        from homeautoshop.assets.models import Asset, AssetServiceInfoLink, ServiceInfoProvider

        self.asset = Asset.objects.create(nickname="Truck", year=2007, make="FORD")
        self.provider = ServiceInfoProvider.objects.create(
            name="LEMON Manuals", slug="lemon", dtc_path=self.DTC_PATH
        )
        self.link = AssetServiceInfoLink.objects.create(
            asset=self.asset, provider=self.provider, url=self.PINNED
        )

    def test_the_catalog_string_is_kept_and_the_section_replaced(self):
        from homeautoshop.assets.service_info import dtc_url

        built = dtc_url(self.link)
        self.assertIn("2007%20Ford%20Truck%20F%20150", built)
        self.assertTrue(built.endswith(self.DTC_PATH))

    def test_the_double_encoding_is_left_alone(self):
        """`%2520` is a literal `%20` that was encoded again. Normalizing 404s."""
        from homeautoshop.assets.service_info import dtc_url

        self.assertIn("%2520", dtc_url(self.link))

    def test_it_works_from_a_pin_anywhere_under_the_vehicle(self):
        from homeautoshop.assets.service_info import dtc_url

        self.link.url = self.PINNED.replace("Repair%2520and%2520Diagnosis/index.html", "")
        self.assertIn("Diagnostic%2520Trouble", dtc_url(self.link))

    def test_an_address_of_another_shape_derives_nothing(self):
        """ALLDATA, or somebody's own file server — no derivable sections."""
        from homeautoshop.assets.service_info import dtc_url

        self.link.url = "https://www.alldatadiy.com/some/page"
        self.assertEqual(dtc_url(self.link), "")

    def test_a_provider_with_no_dtc_section_derives_nothing(self):
        from homeautoshop.assets.service_info import dtc_url

        self.provider.dtc_path = ""
        self.assertEqual(dtc_url(self.link), "")

    def test_a_hidden_provider_offers_no_link(self):
        from homeautoshop.assets.service_info import dtc_links

        self.link.is_hidden = True
        self.link.save()
        self.assertEqual(dtc_links(self.asset), [])

    def test_the_diagnostics_page_offers_it_without_claiming_it_was_checked(self):
        from django.urls import reverse

        from homeautoshop.accounts.models import User
        from homeautoshop.diagnostics import services

        self.client.force_login(User.objects.create_user(username="andy", password="x" * 16))
        session = services.session_from_codes(self.asset, [{"code": "P0420"}])
        services.confirm(session)

        page = self.client.get(reverse("asset_diagnostics", args=[self.asset.pk]))
        self.assertContains(page, "LEMON Manuals")
        self.assertContains(page, "Nothing checks it first")


class ScheduledWorkReachesTheStoryTests(TestCase):
    """FR-VEH-10 / FR-MAINT-6 — the history nobody opened a job for.

    Pressing Done on a schedule row is how most of a home garage's maintenance
    gets recorded: the oil change you did on a Saturday, filed against the item
    and never against a work order. None of it reached the vehicle's story, so
    the page showed the work that had paperwork and silently omitted the work
    that did not — the same defect the orphan part usage already had.
    """

    def setUp(self):
        self.user = User.objects.create_user("andy", password="correct-horse-battery")
        self.client.force_login(self.user)
        self.asset = Asset.objects.create(nickname="Red truck", meter_unit="mi")

    def item(self):
        from homeautoshop.maintenance.models import AssetServiceItem, ServiceDefinition

        return AssetServiceItem.objects.create(
            asset=self.asset,
            definition=ServiceDefinition.objects.create(name="Engine oil and filter"),
            interval_distance=5000,
        )

    def story(self) -> list:
        from .views import _group_media, _timeline

        return _group_media(_timeline(self.asset))

    def test_a_service_recorded_on_the_schedule_appears(self):
        from homeautoshop.maintenance.services import complete

        complete(self.item(), usage=96_000, backfill=True)

        entry = next(e for e in self.story() if e["kind"] == "service")
        self.assertEqual(entry["title"], "Engine oil and filter")
        self.assertIn("96,000 mi", entry["detail"])

    def test_it_leads_to_the_schedule_it_was_recorded_on(self):
        from homeautoshop.maintenance.services import complete

        complete(self.item(), usage=96_000, backfill=True)

        entry = next(e for e in self.story() if e["kind"] == "service")
        self.assertEqual(entry["url"], reverse("asset_schedule", args=[self.asset.pk]))

    def test_a_service_done_on_a_job_is_not_listed_twice(self):
        """The work order is already a row. Printing its completion beside it
        would make one oil change look like two."""
        from homeautoshop.maintenance.services import complete

        wo = WorkOrder.objects.create(asset=self.asset, title="Saturday service")
        complete(self.item(), usage=96_000, work_order=wo)

        kinds = [event["kind"] for event in self.story()]
        self.assertIn("work_order", kinds)
        self.assertNotIn("service", kinds)

    def test_the_vehicle_page_shows_it(self):
        from homeautoshop.maintenance.services import complete

        complete(self.item(), usage=96_000, backfill=True)

        page = self.client.get(reverse("asset_detail", args=[self.asset.pk]))
        self.assertContains(page, "Engine oil and filter")


class TakingBackAMistypedReadingTests(TestCase):
    """FR-VEH-9 / §5.4 — the one correction append-only allows.

    A reading cannot be edited, and should not be: a capture from the garage
    must never lose to an edit war with a sync. But `15000` typed for `105000`
    became the vehicle's current mileage, every interval went wrong at once,
    and the only remedy was to record the right figure over it — which the
    meter flagged as a rollback and which left the wrong row in the history
    for ever. Removing sends the row to the trash; nothing is rewritten.
    """

    def setUp(self):
        from homeautoshop.accounts.models import Role

        from .services import record_reading

        # An admin, because the trash is theirs to manage; removing a reading
        # itself needs only `asset.edit`, which the helper test below checks.
        self.user = User.objects.create_user(
            "andy", password="correct-horse-battery", role=Role.ADMIN
        )
        self.client.force_login(self.user)
        self.asset = Asset.objects.create(nickname="Red truck", meter_unit="mi")
        self.good = record_reading(self.asset, 105_000, read_on=date(2026, 1, 1))
        self.typo = record_reading(self.asset, 15_000, read_on=date(2026, 2, 1), note="typo")

    def remove(self, reading, **kwargs):
        return self.client.post(
            reverse("reading_delete", args=[self.asset.pk, reading.pk]), **kwargs
        )

    def test_the_meter_falls_back_to_the_reading_before_it(self):
        self.assertEqual(self.asset.current_usage, Decimal(15_000))

        self.remove(self.typo)

        self.assertEqual(self.asset.current_usage, Decimal(105_000))

    def test_it_is_soft_and_the_trash_lists_it(self):
        from homeautoshop.assets.models import UsageReading

        self.remove(self.typo)

        self.assertTrue(UsageReading.all_objects.get(pk=self.typo.pk).is_deleted)
        page = self.client.get(reverse("trash")).content.decode()
        self.assertIn("Usage Reading", page)
        self.assertIn("15000", page)

    def test_and_restores_it(self):
        self.remove(self.typo)

        self.client.post(reverse("trash_restore", args=["usage_reading", self.typo.pk]))

        self.assertEqual(self.asset.current_usage, Decimal(15_000))

    def test_who_changed_the_odometer_is_written_down(self):
        from homeautoshop.core.models import AuditLog

        self.remove(self.typo)

        entry = AuditLog.objects.get(entity_id=self.typo.pk, action=AuditLog.Action.DELETE)
        self.assertEqual(entry.user, self.user)
        self.assertIn("15000", entry.summary)

    def test_the_vehicle_page_offers_it_beside_each_reading(self):
        page = self.client.get(reverse("asset_detail", args=[self.asset.pk])).content.decode()

        self.assertIn(reverse("reading_delete", args=[self.asset.pk, self.typo.pk]), page)
        self.assertIn("Entered by hand", page)

    def test_a_readings_route_is_a_post(self):
        self.assertEqual(
            self.client.get(reverse("reading_delete", args=[self.asset.pk, self.typo.pk])).status_code,
            405,
        )

    def test_a_reading_on_another_vehicle_is_not_reachable_through_this_one(self):
        other = Asset.objects.create(nickname="Van", meter_unit="mi")

        response = self.client.post(reverse("reading_delete", args=[other.pk, self.typo.pk]))

        self.assertEqual(response.status_code, 404)
        self.assertEqual(self.asset.current_usage, Decimal(15_000))

    def test_a_helper_without_the_vehicle_is_refused(self):
        from homeautoshop.accounts.models import Role

        helper = User.objects.create_user("sam", password="x" * 16, role=Role.HELPER)
        self.client.force_login(helper)

        self.assertEqual(self.remove(self.typo).status_code, 403)
        self.assertEqual(self.asset.current_usage, Decimal(15_000))


class RecordingAReadingChecksWhoseVehicleItIsTests(TestCase):
    """§12.2a — the one write route the sweep missed.

    The helper gate is an allow-list of URL names, and `reading_create` is on
    it, as it should be: a helper does record the meter on the vehicle they
    were given. The view never asked which vehicle, so a helper granted read on
    one could post a reading onto any vehicle in the shop — and readings drive
    every distance-based due status.
    """

    def setUp(self):
        from homeautoshop.accounts.models import AssetAccess, Role

        self.helper = User.objects.create_user("sam", password="x" * 16, role=Role.HELPER)
        self.client.force_login(self.helper)
        self.mine = Asset.objects.create(nickname="Mine", meter_unit="mi")
        self.theirs = Asset.objects.create(nickname="Theirs", meter_unit="mi")
        AssetAccess.objects.create(user=self.helper, asset=self.mine, level="write")

    def test_a_helper_cannot_record_on_a_vehicle_they_were_not_given(self):
        response = self.client.post(
            reverse("reading_create", args=[self.theirs.pk]), {"value": "12345"}
        )

        self.assertEqual(response.status_code, 403)
        self.assertFalse(UsageReading.objects.filter(asset=self.theirs).exists())

    def test_but_can_on_their_own(self):
        self.client.post(reverse("reading_create", args=[self.mine.pk]), {"value": "12345"})

        self.assertEqual(self.mine.current_usage, Decimal(12345))

    def test_read_only_access_is_not_enough(self):
        from homeautoshop.accounts.models import AssetAccess

        AssetAccess.objects.filter(asset=self.mine).update(level="read")

        response = self.client.post(
            reverse("reading_create", args=[self.mine.pk]), {"value": "12345"}
        )

        self.assertEqual(response.status_code, 403)


class TheFullHistoryIsFullTests(TestCase):
    """FR-VEH-10 — the page that is the history used to inherit the summary's
    caps and quietly drop everything older than the sixtieth event, while its
    own copy said nothing here was grouped or cut."""

    def setUp(self):
        from datetime import timedelta

        from django.utils import timezone

        from .services import record_reading

        self.user = User.objects.create_user("andy", password="correct-horse-battery")
        self.client.force_login(self.user)
        self.asset = Asset.objects.create(nickname="Old truck", meter_unit="mi")
        today = timezone.localdate()
        for n in range(70):
            record_reading(self.asset, 100_000 + n * 100, read_on=today - timedelta(days=70 - n))

    def test_the_history_page_shows_every_event(self):
        page = self.client.get(reverse("asset_timeline", args=[self.asset.pk]))
        self.assertEqual(len(page.context["timeline"]), 70)

    def test_the_vehicle_page_still_shows_a_summary_and_offers_the_rest(self):
        page = self.client.get(reverse("asset_detail", args=[self.asset.pk]))
        self.assertEqual(len(page.context["timeline"]), 8)
        self.assertTrue(page.context["history_is_longer"])


class NobodyIsAskedUnlessYouAskTests(TestCase):
    """The recall sweep exists and is off. A shop that has chosen to stay
    offline must not find its fleet being announced to NHTSA because a default
    said so, and a safety feature is the last place to make that exception."""

    def setUp(self):
        from homeautoshop.assets.models import Asset

        self.asset = Asset.objects.create(
            nickname="Explorer", year=2019, make="FORD", model="Explorer"
        )

    def test_by_default_nothing_is_due_for_a_check(self):
        from homeautoshop.assets.recalls import due_for_check

        self.assertIsNone(due_for_check())

    def test_and_nothing_is_on_the_timer(self):
        from homeautoshop.core import schedule

        self.assertNotIn("recalls.sweep", dict(schedule.recurring()))

    @override_settings(RECALL_CHECK_DAYS=30)
    def test_setting_an_interval_is_what_turns_it_on(self):
        from homeautoshop.assets.recalls import due_for_check
        from homeautoshop.core import schedule

        self.assertIn("recalls.sweep", dict(schedule.recurring()))
        self.assertEqual(due_for_check(), self.asset)

    @override_settings(RECALL_CHECK_DAYS=30, RECALLS_ENABLED=False)
    def test_the_recall_switch_still_governs_it(self):
        from homeautoshop.assets.recalls import due_for_check
        from homeautoshop.core import schedule

        self.assertNotIn("recalls.sweep", dict(schedule.recurring()))
        self.assertIsNone(due_for_check())


class WhenNhtsaLastAnsweredTests(TestCase):
    """An empty list on a safety page reads as "this vehicle is clear" unless
    the page can say when it last looked. Only a real answer stamps it."""

    def setUp(self):
        from homeautoshop.assets.models import Asset

        self.asset = Asset.objects.create(
            nickname="Explorer", year=2019, make="FORD", model="Explorer"
        )

    @staticmethod
    def _empty_400():
        from homeautoshop.core.outbound import OutboundFailed

        return OutboundFailed(
            "HTTP 400",
            status=400,
            body={"Count": 0, "Message": "Results returned successfully", "results": []},
        )

    def _check(self, side_effect):
        from unittest.mock import patch

        from homeautoshop.assets import recalls

        with patch("homeautoshop.assets.recalls.fetch_json", side_effect=side_effect):
            return recalls.check(self.asset)

    def test_campaigns_found_records_the_answer(self):
        found = type("R", (), {"data": {"results": [{"NHTSACampaignNumber": "24V001"}]}})()

        self._check([found])

        self.asset.refresh_from_db()
        self.assertIsNotNone(self.asset.recalls_checked_at)

    def test_a_real_empty_answer_also_counts_as_having_looked(self):
        empty = type("R", (), {"data": {"results": []}})()

        self._check([empty])

        self.asset.refresh_from_db()
        self.assertIsNotNone(self.asset.recalls_checked_at)

    def test_an_ambiguous_empty_400_does_not(self):
        """It might be a rate limit. "Nobody has looked recently" and "we
        looked and could not tell" must not become the same sentence."""
        self._check([self._empty_400(), self._empty_400()])

        self.asset.refresh_from_db()
        self.assertIsNone(self.asset.recalls_checked_at)

    def test_nor_does_an_unreachable_service(self):
        from homeautoshop.core.outbound import OutboundFailed

        self._check(OutboundFailed("boom"))

        self.asset.refresh_from_db()
        self.assertIsNone(self.asset.recalls_checked_at)

    def test_looking_something_up_does_not_make_an_open_edit_form_stale(self):
        """Provenance, not a change anybody made to the vehicle."""
        before = self.asset.revision
        found = type("R", (), {"data": {"results": [{"NHTSACampaignNumber": "24V001"}]}})()

        self._check([found])

        self.asset.refresh_from_db()
        self.assertEqual(self.asset.revision, before)


@override_settings(RECALL_CHECK_DAYS=30)
class WhichVehicleTheSweepTakesTests(TestCase):
    """One an hour, oldest answer first — NHTSA answers a rate-limited request
    exactly the way it answers "no campaigns", so a burst would turn one rate
    limit into a fleet of clean bills of health."""

    def _vehicle(self, nickname, **kwargs):
        from homeautoshop.assets.models import Asset

        fields = {"year": 2019, "make": "FORD", "model": "Explorer"}
        fields.update(kwargs)
        return Asset.objects.create(nickname=nickname, **fields)

    def test_a_vehicle_never_asked_about_comes_first(self):
        from django.utils import timezone as tz

        from homeautoshop.assets.recalls import due_for_check

        asked = self._vehicle("Asked", recalls_checked_at=tz.now() - timedelta(days=90))
        never = self._vehicle("Never")

        self.assertEqual(due_for_check(), never)
        self.assertNotEqual(due_for_check(), asked)

    def test_then_the_oldest_answer(self):
        from django.utils import timezone as tz

        from homeautoshop.assets.recalls import due_for_check

        self._vehicle("Recent", recalls_checked_at=tz.now() - timedelta(days=40))
        oldest = self._vehicle("Oldest", recalls_checked_at=tz.now() - timedelta(days=400))

        self.assertEqual(due_for_check(), oldest)

    def test_one_answered_inside_the_interval_is_not_asked_again(self):
        from django.utils import timezone as tz

        from homeautoshop.assets.recalls import due_for_check

        self._vehicle("Fresh", recalls_checked_at=tz.now() - timedelta(days=2))

        self.assertIsNone(due_for_check())

    def test_a_vehicle_nhtsa_cannot_be_asked_about_never_blocks_the_queue(self):
        """`check` refuses it and stamps nothing, so a sweep that took it would
        take the same one every hour for ever."""
        from homeautoshop.assets.recalls import due_for_check

        self._vehicle("No year", year=None)
        askable = self._vehicle("Askable")

        self.assertEqual(due_for_check(), askable)

    def test_equipment_is_not_a_vehicle_recall_candidate(self):
        from homeautoshop.assets.recalls import due_for_check

        self._vehicle("Mower", asset_kind="equipment")

        self.assertIsNone(due_for_check())

    def test_a_sold_vehicle_is_not_asked_about(self):
        from homeautoshop.assets.models import AssetStatus
        from homeautoshop.assets.recalls import due_for_check

        self._vehicle("Sold", status=AssetStatus.SOLD)

        self.assertIsNone(due_for_check())

    def test_the_sweep_looks_up_exactly_one_vehicle(self):
        from unittest.mock import patch

        from homeautoshop.core.jobs import HANDLERS

        for n in range(3):
            self._vehicle(f"Truck {n}")

        with patch("homeautoshop.assets.recalls.check") as check:
            check.return_value = type("R", (), {"message": "ok"})()
            HANDLERS["recalls.sweep"]({})

        self.assertEqual(check.call_count, 1)

    def test_and_nothing_when_the_interval_was_switched_off_meanwhile(self):
        from unittest.mock import patch

        from django.test import override_settings as override

        from homeautoshop.core.jobs import HANDLERS

        self._vehicle("Truck")

        with patch("homeautoshop.assets.recalls.check") as check, override(RECALL_CHECK_DAYS=0):
            HANDLERS["recalls.sweep"]({})

        check.assert_not_called()


class TheRecallPageSaysWhenItLookedTests(TestCase):
    def setUp(self):
        from homeautoshop.assets.models import Asset

        self.user = User.objects.create_user("andy", password="correct-horse-battery")
        self.client.force_login(self.user)
        self.asset = Asset.objects.create(
            nickname="Explorer", year=2019, make="FORD", model="Explorer"
        )

    def page(self) -> str:
        return self.client.get(
            reverse("asset_recalls", args=[self.asset.pk])
        ).content.decode()

    def test_never_asked_says_so(self):
        page = self.page()

        self.assertIn("never been asked", page)
        self.assertIn("Nothing has been asked yet", page)

    def test_asked_and_empty_is_a_different_sentence(self):
        """"Nobody has looked" and "NHTSA listed nothing" are not the same
        claim about a vehicle, and both used to print "Nothing checked yet"."""
        from django.utils import timezone as tz

        self.asset.recalls_checked_at = tz.now()
        self.asset.save()

        page = self.page()

        self.assertIn("listed no campaigns", page)
        self.assertIn("not proof this vehicle is clear", page)

    def test_with_no_interval_the_page_says_nothing_will_ask(self):
        self.assertIn("Nothing is asked unless you press Check", self.page())

    @override_settings(RECALL_CHECK_DAYS=30)
    def test_with_one_it_says_how_often(self):
        self.assertIn("Asked again after 30 days", self.page())
