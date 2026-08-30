"""
Looking a VIN up while adding the vehicle (FR-VEH-2, SPEC §8.1).

The decode existed only on the detail page, so the shortest route to a
populated record was: save a vehicle you know almost nothing about, find it
again, press the button. The lookup belongs where the person is already typing.

Two properties matter more than the convenience:

* **A lookup never overwrites what someone typed.** The person holding the
  logbook outranks vPIC, which is often wrong about trim and sometimes wrong
  about the engine.
* **A failed lookup never blocks the form** (P-7). No network, no service, a
  pre-1981 VIN — all of them still let you add the vehicle by hand.
"""

from __future__ import annotations

from unittest.mock import patch

from django.test import TestCase
from django.urls import reverse

from homeautoshop.accounts.models import User
from homeautoshop.assets.models import Asset
from homeautoshop.core.outbound import OutboundFailed

VIN = "1FTFW1ET5DFC10312"

DECODE = {
    "Results": [
        {
            "Make": "FORD",
            "Model": "F-150",
            "ModelYear": "2013",
            "Trim": "XLT",
            "BodyClass": "Pickup",
            "FuelTypePrimary": "Gasoline",
            "DriveType": "4WD",
            "DisplacementL": "3.5",
            "EngineCylinders": "6",
        }
    ]
}


class CreateWithLookupTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="andy", password="x" * 16)
        self.client.force_login(self.user)
        self.url = reverse("asset_create")

    @patch("homeautoshop.assets.services.fetch_json")
    def test_the_form_offers_a_lookup_beside_the_vin(self, _fetch):
        """Next to the field it acts on, not at the bottom beside Save.

        Down there it reads as a second way to submit the form.
        """
        page = self.client.get(self.url).content.decode()
        self.assertIn('value="lookup"', page)

        vin_input = page.index('name="vin"')
        button = page.index('value="lookup"')
        save = page.index('{}'.format("Save"))
        self.assertLess(abs(button - vin_input), 400, "lookup is not beside the VIN field")
        self.assertLess(button, save, "lookup should come before the Save button")

    @patch("homeautoshop.assets.services.fetch_json")
    def test_a_lookup_fills_the_form_without_creating_anything(self, fetch):
        fetch.return_value.data = DECODE
        response = self.client.post(self.url, {"vin": VIN, "action": "lookup"})

        self.assertEqual(response.status_code, 200)
        form = response.context["form"]
        self.assertEqual(form["make"].value(), "FORD")
        self.assertEqual(form["model"].value(), "F-150")
        self.assertEqual(form["year"].value(), "2013")
        # Nothing is saved until Save is pressed.
        self.assertEqual(Asset.objects.count(), 0)

    @patch("homeautoshop.assets.services.fetch_json")
    def test_values_come_back_as_values_not_as_lists(self, fetch):
        """The bug this defends against rendered every box as "['FORD']".

        A QueryDict subclasses dict and stores each value as a list. ModelForm
        merges initial with dict.update(), which takes the C fast path straight
        over that storage and never calls the overridden __getitem__. The page
        still looked plausible, and a number input just showed nothing at all —
        which is why substring assertions did not notice.
        """
        fetch.return_value.data = DECODE
        response = self.client.post(self.url, {"vin": VIN, "action": "lookup"})

        form = response.context["form"]
        for name in form.fields:
            value = form[name].value()
            with self.subTest(field=name):
                self.assertNotIsInstance(value, (list, tuple))
        self.assertNotIn("[&#x27;", response.content.decode())

    @patch("homeautoshop.assets.services.fetch_json")
    def test_what_you_typed_survives_the_lookup(self, fetch):
        fetch.return_value.data = DECODE
        response = self.client.post(
            self.url,
            {"vin": VIN, "action": "lookup", "nickname": "Work truck", "trim": "Lariat"},
        )
        form = response.context["form"]

        self.assertEqual(form["nickname"].value(), "Work truck")
        # vPIC said XLT. The person said Lariat. The person wins.
        self.assertEqual(form["trim"].value(), "Lariat")

    @patch("homeautoshop.assets.services.fetch_json")
    def test_a_blank_nickname_is_seeded_from_the_decode(self, fetch):
        fetch.return_value.data = DECODE
        response = self.client.post(self.url, {"vin": VIN, "action": "lookup"})
        self.assertEqual(response.context["form"]["nickname"].value(), "2013 FORD F-150 XLT")

    @patch("homeautoshop.assets.services.fetch_json", side_effect=OutboundFailed("no network"))
    def test_a_failed_lookup_still_leaves_a_usable_form(self, _fetch):
        response = self.client.post(
            self.url, {"vin": VIN, "action": "lookup", "nickname": "Work truck"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["form"]["nickname"].value(), "Work truck")
        self.assertIn("Save", response.content.decode())

    @patch("homeautoshop.assets.services.fetch_json")
    def test_an_incomplete_draft_is_not_scolded_for_being_incomplete(self, fetch):
        """Nickname is required to save, but not to ask what the VIN decodes to."""
        fetch.return_value.data = DECODE
        page = self.client.post(self.url, {"vin": VIN, "action": "lookup"}).content.decode()
        self.assertNotIn("This field is required", page)

    def test_saving_still_works_and_still_requires_a_nickname(self):
        response = self.client.post(self.url, {"vin": VIN, "meter_unit": "mi"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Asset.objects.count(), 0)

        response = self.client.post(
            self.url, {"nickname": "Work truck", "vin": VIN, "meter_unit": "mi"}
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Asset.objects.get().nickname, "Work truck")


class DecodeWithoutSavingTests(TestCase):
    @patch("homeautoshop.assets.services.fetch_json")
    def test_decoding_an_unsaved_asset_leaves_it_unsaved(self, fetch):
        from homeautoshop.assets.services import decode_vin

        fetch.return_value.data = DECODE
        probe = Asset(vin=VIN, asset_kind="vehicle")
        result = decode_vin(probe, save=False)

        self.assertTrue(result.ok)
        self.assertEqual(probe.make, "FORD")
        # A pk exists the moment the instance does — the id default is uuid7(),
        # which is the point of a client-mintable key. Persistence is the thing
        # to assert on.
        self.assertTrue(probe._state.adding)
        self.assertFalse(Asset.objects.filter(pk=probe.pk).exists())

    @patch("homeautoshop.assets.services.fetch_json")
    def test_the_saving_path_is_untouched(self, fetch):
        from homeautoshop.assets.services import decode_vin

        fetch.return_value.data = DECODE
        asset = Asset.objects.create(nickname="Truck", vin=VIN)
        decode_vin(asset)
        self.assertEqual(Asset.objects.get(pk=asset.pk).make, "FORD")


class DecodeSurvivesTheSaveTests(TestCase):
    """A vehicle created from a lookup must keep what the lookup returned.

    The probe the form decodes into is thrown away, so without carrying the
    payload forward the saved record has no `decoded_raw` at all: the detail
    page shows nothing under "What the VIN says", and the lookup has to be run
    again on a record that was created from one.
    """

    def setUp(self):
        self.user = User.objects.create_user(username="andy", password="x" * 16)
        self.client.force_login(self.user)
        self.url = reverse("asset_create")

    @patch("homeautoshop.assets.services.fetch_json")
    def test_the_decode_is_stored_on_the_new_vehicle(self, fetch):
        fetch.return_value.data = DECODE
        self.client.post(self.url, {"vin": VIN, "action": "lookup"})
        self.client.post(self.url, {"nickname": "Work truck", "vin": VIN, "meter_unit": "mi"})

        asset = Asset.objects.get()
        self.assertEqual(asset.decoded_raw.get("Make"), "FORD")
        self.assertEqual(asset.decode_source, "vpic")
        self.assertIsNotNone(asset.decoded_at)

    @patch("homeautoshop.assets.services.fetch_json")
    def test_a_decode_for_a_different_vin_is_not_attached(self, fetch):
        """Editing the VIN after looking it up must not carry the old payload."""
        fetch.return_value.data = DECODE
        self.client.post(self.url, {"vin": VIN, "action": "lookup"})
        self.client.post(
            self.url,
            {"nickname": "Other truck", "vin": "1M8GDM9AXKP042788", "meter_unit": "mi"},
        )

        asset = Asset.objects.get()
        self.assertEqual(asset.decoded_raw, {})

    def test_saving_without_a_lookup_stores_no_decode(self):
        self.client.post(self.url, {"nickname": "Hand entered", "vin": VIN, "meter_unit": "mi"})
        self.assertEqual(Asset.objects.get().decoded_raw, {})
