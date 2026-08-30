"""VIN handling and asset behavior (SPEC FR-VEH-*, §8.1)."""

from __future__ import annotations

from unittest.mock import patch

from django.test import TestCase, override_settings
from django.urls import reverse

from homeautoshop.accounts.models import User
from homeautoshop.core.outbound import OutboundBlocked, OutboundFailed

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

    def test_wrong_length_suggests_the_pre_1981_case(self):
        check = vinlib.validate("1M8GDM9AX")
        self.assertFalse(check.is_well_formed)
        self.assertTrue(any("1981" in w for w in check.warnings))

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
        self.assertEqual([p for p, _link in page.context["providers"]], [])
        self.assertEqual(page.context["hidden_providers"], [self.provider])

    def test_it_is_hidden_on_this_vehicle_only(self):
        """CHARM is useless for the Crosstrek and the best source for the truck."""
        from django.urls import reverse

        from homeautoshop.assets.models import Asset

        other = Asset.objects.create(nickname="Truck", year=2007, make="FORD")
        self.client.post(self._url(), {"hide": "1"})
        page = self.client.get(reverse("asset_detail", args=[other.pk]))
        self.assertIn(self.provider, [p for p, _link in page.context["providers"]])

    def test_hiding_can_be_undone(self):
        from django.urls import reverse

        self.client.post(self._url(), {"hide": "1"})
        self.client.post(self._url(), {})
        page = self.client.get(reverse("asset_detail", args=[self.asset.pk]))
        self.assertIn(self.provider, [p for p, _link in page.context["providers"]])

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
