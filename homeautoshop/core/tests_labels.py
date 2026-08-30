"""
QR labels and scanning (SPEC FR-VEH-5, FR-INV-2, FR-INV-3, C-4).

The camera half of this lives in the browser and cannot be tested here — what
can be tested is everything a scan depends on: that a label encodes a URL this
application will resolve, that resolving it lands on the right screen, and that
scanning a barcode nobody has seen before teaches the shop something instead of
being a dead end.

One label format for every kind of thing, because primary keys are UUIDv7 and
unique across the database. The tests below hold that property down, since it
is the reason a bin label and a windshield tag can share a scanner.
"""

from __future__ import annotations

from django.test import TestCase, override_settings
from django.urls import reverse

from homeautoshop.accounts.models import User
from homeautoshop.assets.models import Asset
from homeautoshop.core import labels
from homeautoshop.parts.models import Location, Part, PartCrossRef

VIN = "1M8GDM9AXKP042788"


class QrTests(TestCase):
    def test_the_svg_can_be_dropped_into_a_page(self):
        """An XML declaration is legal in a file and renders as text in HTML."""
        svg = labels.qr_svg("https://shop.test/s/abc/")
        self.assertTrue(svg.startswith("<svg"))
        self.assertNotIn("<?xml", svg)

    def test_every_label_prints_the_same_size(self):
        """A short URL and a long one differ in module count, not in millimetres —
        otherwise a sheet of labels comes out in assorted sizes."""
        small = labels.qr_svg("https://a.test/s/1/", size_mm=24)
        large = labels.qr_svg("https://a-much-longer-hostname.test/s/" + "0" * 40 + "/", size_mm=24)
        self.assertIn('width="24mm"', small)
        self.assertIn('width="24mm"', large)
        # …and the viewBox does differ, which is what keeps them scannable.
        self.assertNotEqual(
            small[small.index("viewBox") : small.index("viewBox") + 24],
            large[large.index("viewBox") : large.index("viewBox") + 24],
        )

    @override_settings(BASE_URL="https://shop.test")
    def test_a_label_encodes_an_absolute_url_on_this_instance(self):
        """It is read by a phone camera, which has no page to be relative to."""
        location = Location.objects.create(name="Shelf B3")
        self.assertEqual(
            labels.scan_url(location), f"https://shop.test/s/{location.pk}/"
        )


class ScanTargetTests(TestCase):
    """One route resolves a label to whatever it names."""

    def setUp(self):
        self.user = User.objects.create_user(username="andy", password="x" * 16)
        self.client.force_login(self.user)

    def test_a_bin_label_opens_what_is_in_it(self):
        location = Location.objects.create(name="Shelf B3")
        response = self.client.get(reverse("scan_target", args=[location.pk]))
        self.assertRedirects(
            response, f"{reverse('inventory')}?location={location.pk}", fetch_redirect_response=False
        )

    def test_a_vehicle_tag_opens_the_vehicle(self):
        asset = Asset.objects.create(nickname="Red truck", vin=VIN)
        response = self.client.get(reverse("scan_target", args=[asset.pk]))
        self.assertRedirects(response, reverse("asset_detail", args=[asset.pk]))

    def test_a_part_label_opens_the_part(self):
        part = Part.objects.create(name="Brake pads")
        response = self.client.get(reverse("scan_target", args=[part.pk]))
        self.assertRedirects(response, reverse("part_detail", args=[part.pk]))

    def test_a_label_for_something_deleted_says_so_rather_than_404ing(self):
        """A sticker outlives the row. Somebody is holding it, wondering."""
        location = Location.objects.create(name="Gone")
        pk = location.pk
        location.delete()
        response = self.client.get(reverse("scan_target", args=[pk]), follow=True)
        self.assertContains(response, "no longer here")

    def test_it_needs_a_login(self):
        asset = Asset.objects.create(nickname="Red truck")
        self.client.logout()
        response = self.client.get(reverse("scan_target", args=[asset.pk]))
        self.assertNotIn(reverse("asset_detail", args=[asset.pk]), response.get("Location", ""))


class ScannedBinTests(TestCase):
    """FR-INV-2 says a label opens *that location's* contents.

    The route landed on the inventory page and the page ignored the parameter,
    so a scan showed the whole shelf — right for a desk, useless with a label
    in your hand.
    """

    def setUp(self):
        self.user = User.objects.create_user(username="andy", password="x" * 16)
        self.client.force_login(self.user)
        self.cabinet = Location.objects.create(name="Red cabinet")
        self.drawer = Location.objects.create(name="Drawer 2", parent=self.cabinet)
        self.elsewhere = Location.objects.create(name="Shelf B3")

    def _shown(self, **params):
        page = self.client.get(reverse("inventory"), params)
        return [location.name for location in page.context["locations"]]

    def test_without_a_label_it_is_the_whole_shelf(self):
        self.assertEqual(sorted(self._shown()), ["Drawer 2", "Red cabinet", "Shelf B3"])

    def test_a_scanned_bin_shows_only_that_bin(self):
        self.assertEqual(self._shown(location=str(self.elsewhere.pk)), ["Shelf B3"])

    def test_scanning_a_cabinet_includes_its_drawers(self):
        """An empty cabinet is not the answer when the parts are in its drawers."""
        self.assertEqual(
            sorted(self._shown(location=str(self.cabinet.pk))), ["Drawer 2", "Red cabinet"]
        )

    def test_the_shop_wide_panels_step_aside(self):
        page = self.client.get(reverse("inventory"), {"location": str(self.elsewhere.pk)})
        self.assertEqual(list(page.context["low"]), [])
        self.assertIsNone(page.context["value"])
        self.assertContains(page, "The whole shelf")

    def test_a_label_for_a_deleted_bin_says_so(self):
        pk = self.elsewhere.pk
        self.elsewhere.delete()
        response = self.client.get(reverse("inventory"), {"location": str(pk)})
        self.assertContains(response, "no longer here")

    def test_the_end_to_end_scan_lands_on_the_bin(self):
        followed = self.client.get(reverse("scan_target", args=[self.elsewhere.pk]), follow=True)
        self.assertEqual(followed.context["focus"], self.elsewhere)


class LabelSheetTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="andy", password="x" * 16)
        self.client.force_login(self.user)

    def test_the_sheet_carries_a_code_per_location(self):
        Location.objects.create(name="Shelf B3")
        Location.objects.create(name="Red cabinet")
        page = self.client.get(reverse("labels")).content.decode()
        self.assertEqual(page.count("<svg"), 2)
        self.assertIn("Shelf B3", page)

    def test_vehicles_can_be_labeled_too(self):
        Asset.objects.create(nickname="Red truck", vin=VIN)
        page = self.client.get(reverse("labels"), {"kind": "vehicles"})
        self.assertContains(page, "Red truck")
        self.assertContains(page, "<svg")

    def test_the_codes_are_embedded_rather_than_fetched(self):
        """Thirty labels should be one request, and should print offline."""
        Location.objects.create(name="Shelf B3")
        page = self.client.get(reverse("labels")).content.decode()
        self.assertIn("<svg", page)
        self.assertNotIn("<img", page)

    def test_an_empty_shop_says_what_to_do(self):
        self.assertContains(self.client.get(reverse("labels")), "No storage locations yet")


class ScanAPartTests(TestCase):
    """FR-INV-3 — scan a part's UPC to find *or create* it."""

    def setUp(self):
        self.user = User.objects.create_user(username="andy", password="x" * 16)
        self.client.force_login(self.user)
        self.part = Part.objects.create(name="Brake pads", part_number="ACT1164")
        self.url = reverse("part_by_code")

    def test_a_known_barcode_opens_the_part(self):
        PartCrossRef.objects.create(
            part=self.part, system=PartCrossRef.System.UPC, value="012345678905"
        )
        response = self.client.get(self.url, {"code": "012345678905"})
        self.assertRedirects(response, reverse("part_detail", args=[self.part.pk]))

    def test_a_part_number_on_the_box_also_works(self):
        response = self.client.get(self.url, {"code": "act1164"})
        self.assertRedirects(response, reverse("part_detail", args=[self.part.pk]))

    def test_an_unknown_barcode_offers_to_create_it(self):
        response = self.client.get(self.url, {"code": "099999999999"})
        self.assertRedirects(
            response,
            f"{reverse('part_create')}?upc=099999999999",
            fetch_redirect_response=False,
        )

    def test_creating_from_a_scan_records_the_barcode(self):
        """Otherwise the next scan of the same box is another dead end."""
        self.client.post(
            f"{reverse('part_create')}?upc=099999999999",
            {"name": "Oil filter", "unit": "each", "part_type": "aftermarket"},
        )
        created = Part.objects.get(name="Oil filter")
        self.assertTrue(
            created.cross_refs.filter(
                system=PartCrossRef.System.UPC, value="099999999999"
            ).exists()
        )

    def test_the_creation_form_says_the_barcode_will_be_kept(self):
        page = self.client.get(reverse("part_create"), {"upc": "099999999999"})
        self.assertContains(page, "099999999999")

    def test_two_parts_with_one_barcode_is_a_question_not_a_guess(self):
        other = Part.objects.create(name="Other pads")
        for part in (self.part, other):
            PartCrossRef.objects.create(
                part=part, system=PartCrossRef.System.UPC, value="012345678905"
            )
        response = self.client.get(self.url, {"code": "012345678905"}, follow=True)
        self.assertContains(response, "More than one part")

    def test_an_empty_scan_is_a_message_not_a_crash(self):
        self.assertContains(self.client.get(self.url, follow=True), "Nothing was scanned")


class ScannerWiringTests(TestCase):
    """The buttons exist, carry the right formats, and the script is loaded."""

    def setUp(self):
        self.user = User.objects.create_user(username="andy", password="x" * 16)
        self.client.force_login(self.user)

    def test_the_vehicle_form_offers_a_vin_scan(self):
        page = self.client.get(reverse("asset_create")).content.decode()
        self.assertIn('data-scan-target="vin"', page)
        # Code 39 is what a door-jamb label carries.
        self.assertIn("code_39", page)

    def test_the_parts_list_offers_a_barcode_scan(self):
        page = self.client.get(reverse("part_list")).content.decode()
        self.assertIn("upc_a", page)
        self.assertIn(reverse("part_by_code"), page)

    def test_the_inventory_screen_offers_a_label_scan(self):
        page = self.client.get(reverse("inventory")).content.decode()
        self.assertIn('data-scan="qr_code"', page)
        self.assertIn(reverse("labels"), page)

    def test_the_scanner_and_its_wording_are_on_every_page(self):
        from django.templatetags.static import static

        page = self.client.get(reverse("dashboard")).content.decode()
        self.assertIn(static("scanner.js"), page)
        self.assertIn('id="scanner-strings"', page)

    def test_the_requirement_is_stated_rather_than_left_to_be_discovered(self):
        """Safari and Firefox cannot do this, and a dead button explains nothing."""
        page = self.client.get(reverse("dashboard")).content.decode()
        self.assertIn("Chrome or Edge", page)
