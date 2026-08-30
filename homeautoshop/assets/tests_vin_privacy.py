"""
NFR-S-5 — "VINs and plates are masked in the UI where not needed; full values
require an explicit reveal."

The requirement shipped without a test, and both halves of it were broken at
once: there was no reveal anywhere in the UI, and the full VIN was in the page
regardless, sitting in a `title` attribute. Masking that the source contradicts
is not masking, it is decoration — and it is worse than showing the value,
because it tells the reader they are protected when they are not.

These tests assert on the rendered bytes, because that is the thing that
actually leaks.
"""

from __future__ import annotations

from unittest.mock import patch

from django.test import TestCase
from django.urls import reverse

from homeautoshop.accounts.models import User
from homeautoshop.assets.models import Asset

# The ISO 3779 worked example — a real check digit, so nothing here depends on
# an invented VIN agreeing with the author.
VIN = "1M8GDM9AXKP042788"


class VinRevealTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="andy", password="x" * 16)
        self.client.force_login(self.user)
        self.asset = Asset.objects.create(nickname="Red truck", vin=VIN)
        self.url = reverse("asset_detail", args=[self.asset.pk])

    def test_the_full_vin_is_not_in_the_page_by_default(self):
        page = self.client.get(self.url).content.decode()
        self.assertNotIn(VIN, page)

    def test_the_masked_form_is_what_gets_shown(self):
        page = self.client.get(self.url).content.decode()
        self.assertIn(self.asset.masked_vin, page)
        self.assertIn("1M8", page)

    def test_no_tooltip_smuggles_the_value_out(self):
        """The reported bug: masked on screen, complete in the markup."""
        page = self.client.get(self.url).content.decode()
        self.assertNotIn(f'title="{VIN}"', page)
        # Nor any other attribute, however spelled.
        self.assertNotIn(VIN, page)

    def test_there_is_a_way_to_see_it(self):
        page = self.client.get(self.url).content.decode()
        self.assertIn("?vin=show", page)

    def test_revealing_shows_the_whole_thing(self):
        page = self.client.get(self.url, {"vin": "show"}).content.decode()
        self.assertIn(VIN, page)

    def test_revealing_offers_a_way_back(self):
        page = self.client.get(self.url, {"vin": "show"}).content.decode()
        self.assertNotIn("?vin=show", page)

    def test_the_reveal_still_requires_a_login(self):
        self.client.logout()
        response = self.client.get(self.url, {"vin": "show"})
        self.assertNotEqual(response.status_code, 200)
        if response.status_code == 200:  # pragma: no cover - guarded above
            self.assertNotIn(VIN, response.content.decode())


class DecodeRouteTests(TestCase):
    def setUp(self):
        self.asset = Asset.objects.create(nickname="Red truck", vin=VIN)

    @patch("homeautoshop.assets.services.fetch_json")
    def test_the_extended_route_is_the_one_called(self, fetch):
        from homeautoshop.assets.services import decode_vin

        fetch.return_value.data = {"Results": [{"Make": "MCI"}]}
        decode_vin(self.asset)

        called = fetch.call_args[0][0]
        self.assertIn("DecodeVinValuesExtended", called)
        self.assertIn(VIN, called)


class DecodedDetailTests(TestCase):
    """The lookup returns 30-40 populated fields; eight became columns."""

    def setUp(self):
        self.asset = Asset.objects.create(nickname="Red truck", vin=VIN)

    def test_nothing_is_shown_before_a_decode(self):
        self.assertEqual(self.asset.decoded_details(), [])

    def test_populated_fields_are_grouped_and_labeled(self):
        self.asset.decoded_raw = {
            "EngineHP": "365",
            "BrakeSystemType": "Air",
            "PlantCity": "WINNIPEG",
        }
        groups = dict(
            (str(label), dict((str(field_label), value) for _key, field_label, value in rows))
            for label, rows in self.asset.decoded_details()
        )
        self.assertEqual(groups["Engine"]["Horsepower"], "365")
        self.assertEqual(groups["Safety equipment"]["Brake system"], "Air")
        self.assertEqual(groups["Where it was built"]["Plant city"], "WINNIPEG")

    def test_vpic_placeholders_are_not_presented_as_facts(self):
        """"Not Applicable" and a bare zero are absence, not data."""
        self.asset.decoded_raw = {
            "EngineHP": "0",
            "TractionControl": "Not Applicable",
            "ABS": "",
            "Doors": "2",
        }
        rows = [row for _label, group in self.asset.decoded_details() for row in group]
        values = {str(label): value for _key, label, value in rows}
        self.assertEqual(values, {"Doors": "2"})

    def test_a_group_with_nothing_in_it_is_omitted(self):
        self.asset.decoded_raw = {"EngineHP": "365"}
        labels = [str(label) for label, _rows in self.asset.decoded_details()]
        self.assertEqual(labels, ["Engine"])

    def test_it_does_not_repeat_what_the_header_already_says(self):
        """A second copy of make and model is noise, not detail."""
        self.asset.decoded_raw = {
            "Make": "MCI",
            "Model": "102DL3",
            "ModelYear": "1989",
            "BodyClass": "Bus",
            "FuelTypePrimary": "Diesel",
            "EngineHP": "365",
        }
        rows = [row for _label, group in self.asset.decoded_details() for row in group]
        values = {str(label) for _key, label, _value in rows}
        self.assertEqual(values, {"Horsepower"})
