"""Reaching the adapter, and believing what it says (SPEC §8.3c).

These are the four ways this screen failed against a real OBDLink MX+ on a
real car, and none of them looked like a bug from the garage:

* **The chooser was empty on a phone.** Web Serial offers an unmapped
  Bluetooth port only when it carries the standard SerialPort service class,
  or when the page names the vendor's own UUID. Desktops hide this because the
  OS maps a paired adapter to a COM port and mapped ports are always offered;
  Android maps nothing, so the same adapter that works on a laptop yields
  "No compatible devices found" on the phone actually standing next to the car.
* **`ATS0` turned the separators off** while the decoder split on whitespace
  to find them, so a reply parsed as one enormous number and every code
  vanished.
* **The DTC count byte was decoded as code data.** CAN answers mode 03 with
  `43 <count> <hi lo> …`, and pairing from the count shifts every code by a
  byte: `43 02 01 33 04 20` read as P0201 rather than P0133 and P0420. Not a
  missing answer — a wrong one, which is a morning spent on the wrong circuit.
* **`UNABLE TO CONNECT` was reported as "no codes found."** The adapter draws
  power from the socket whether or not the car is awake, so an ignition-off
  read looked exactly like a clean bill of health.

The script is checked as source because there is no JavaScript runtime in the
image, which is the same bargain `tests_forms.py` makes for `forms.js`.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from django.conf import settings as django_settings
from django.test import TestCase
from django.urls import reverse

from homeautoshop.accounts.models import User
from homeautoshop.assets.models import Asset

SCRIPT = Path(django_settings.BASE_DIR) / "static" / "elm327.js"


class AdapterScriptTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.source = SCRIPT.read_text(encoding="utf-8")

    def test_the_serial_chooser_is_told_which_bluetooth_services_to_offer(self):
        """Without this the chooser is empty on a phone, for every adapter
        whose serial service sits behind the maker's own identifier."""
        self.assertIn("allowedBluetoothServiceClassIds", self.source)

    def test_it_asks_the_adapter_to_keep_the_separators(self):
        """ATS0 turns spaces off; the decoder needs them to find byte
        boundaries. The two together silently read every car as healthy."""
        self.assertIn('send("ATS1")', self.source)
        self.assertNotIn('send("ATS0")', self.source)

    def test_the_count_byte_is_dropped_rather_than_decoded(self):
        """Parity is what distinguishes a counted payload from an uncounted
        one: CAN sends 1 + 2n bytes, the older protocols send 2n."""
        self.assertIn("payload.length % 2 === 1", self.source)

    def test_a_dead_link_is_not_reported_as_a_clean_car(self):
        self.assertIn("UNABLE TO CONNECT", self.source)
        self.assertIn("noEcu", self.source)

    def test_no_data_is_not_treated_as_a_dead_link(self):
        """It is the ordinary answer for pending and permanent codes on plenty
        of cars, and calling it a failure would cry wolf on every clean read."""
        link_error = re.search(r"var LINK_ERROR = /(.+?)/i;", self.source)
        self.assertIsNotNone(link_error)
        self.assertNotIn("NO DATA", link_error.group(1))

    def test_it_prints_what_it_connected_to(self):
        """The vendor UUID needed to make a phone list an adapter is not in
        anybody's documentation. Connecting once on a desktop is how you get
        it, so the port's service class ID has to reach the log."""
        self.assertIn("bluetoothServiceClassId", self.source)

    def test_writes_are_split_for_the_ble_mtu(self):
        """Android negotiates 23 bytes by default and rejects a longer write
        outright rather than fragmenting it."""
        self.assertIn("bleChunkBytes", self.source)


class AdapterPageTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="andy", password="x" * 16)
        self.client.force_login(self.user)
        self.asset = Asset.objects.create(nickname="Work truck", make="Ford")

    def _config(self) -> dict:
        page = self.client.get(reverse("elm327", args=[self.asset.pk])).content.decode()
        block = re.search(
            r'<script id="elm-config" type="application/json">(.*?)</script>', page, re.S
        )
        self.assertIsNotNone(block)
        return json.loads(block.group(1))

    def test_the_standard_serial_port_profile_is_offered(self):
        """Which is what an OBDLink MX+ turns out to use — the adapter was
        never the exotic case, so the ordinary one has to work."""
        self.assertIn(
            "00001101-0000-1000-8000-00805f9b34fb", self._config()["bluetoothServiceUuids"]
        )

    def test_ble_adapters_have_somewhere_to_land(self):
        """Bluetooth Classic adapters are unreachable over BLE and BLE ones are
        unreachable over RFCOMM, so a single transport always strands someone."""
        profiles = self._config()["bleProfiles"]
        self.assertTrue(profiles)
        for profile in profiles:
            self.assertEqual({"service", "notify", "write"}, set(profile))

    def test_the_page_offers_both_transports(self):
        page = self.client.get(reverse("elm327", args=[self.asset.pk])).content.decode()
        self.assertIn('id="elm-transport"', page)
        self.assertIn('value="bluetooth"', page)

    def test_the_transport_control_is_labelled(self):
        page = self.client.get(reverse("elm327", args=[self.asset.pk])).content.decode()
        self.assertIn('for="elm-transport"', page)

    def test_it_no_longer_claims_to_be_usb_only(self):
        """It never was on a phone, where Web Serial reaches Bluetooth and not
        USB — the opposite of what the page used to say."""
        page = self.client.get(reverse("elm327", args=[self.asset.pk])).content.decode()
        self.assertNotIn("ELM327 over USB", page)

    def test_an_empty_chooser_is_explained_rather_than_blamed_on_the_user(self):
        """The browser reports the same NotFoundError for a cancelled chooser
        and one that had nothing in it, and its wording — "No port selected by
        the user" — accuses somebody of a decision they never made."""
        page = self.client.get(reverse("elm327", args=[self.asset.pk])).content.decode()
        self.assertIn("or none was offered", page)

    def test_the_android_permission_is_a_prerequisite_not_a_footnote(self):
        """Android 12 put every Bluetooth device behind "Nearby devices", and a
        Chrome without it opens an empty chooser and reports nothing at all —
        which is what an OBDLink MX+ looked like on a real phone. It belongs in
        the steps you work through, not in the paragraph you reach afterwards."""
        page = self.client.get(reverse("elm327", args=[self.asset.pk])).content.decode()
        self.assertIn("Nearby devices", page)
        self.assertIn("Before the first read", page)
