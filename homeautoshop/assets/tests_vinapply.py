"""Filling in a vehicle from a pre-1981 VIN (SPEC FR-VEH-12, §8.1a).

The decoder could read `F26SVAE1234` as a 1978 F-250 with a 400 V8 and then do
nothing with it: the reading was on the screen and the boxes beside it stayed
empty. This is the write-back, and it holds the same line `decode_vin` holds
for vPIC — blanks only, never over a correction, provenance recorded — with two
rules of its own.

**Ambiguity is never resolved by writing.** Several schemes share a shape, so a
VIN can have two honest readings. `describe` refuses to choose between them,
and so does this: stamping one on the record would turn a question the page is
asking into a fact the record asserts.

**Only what the sheet actually says.** A year is taken when the reading settled
on one and not when it offered a range. A model is taken only from a scheme
that says which position names one — Ford stamps `F-250 4WD`, GM stamps
`1/2 ton`, and a tonnage in the model column would be worse than a blank.
"""

from __future__ import annotations

from django.test import TestCase
from django.urls import reverse

from homeautoshop.accounts.models import Role, User
from homeautoshop.assets.models import Asset
from homeautoshop.assets.services import (
    LOCAL_DECODE_SOURCE, mark_override, read_vin_locally,
)

TRUCK = "F26SVAE1234"


class Base(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="andy", password="x" * 16, role=Role.ADMIN
        )
        self.client.force_login(self.user)


class FillingInTests(Base):
    def truck(self, **kwargs):
        return Asset.objects.create(nickname="Old Ford", vin=TRUCK, **kwargs)

    def test_it_fills_in_what_the_vin_says(self):
        asset = self.truck()

        result = read_vin_locally(asset)

        self.assertTrue(result.ok)
        asset.refresh_from_db()
        self.assertEqual(asset.make, "Ford")
        self.assertEqual(asset.year, 1978)
        self.assertEqual(asset.model, "F-250 4WD")
        self.assertIn("400 CID", asset.engine)

    def test_it_records_where_the_answer_came_from(self):
        """A field filled from a table typed out of a scan and one filled from
        NHTSA are not the same claim, and the record says which."""
        asset = self.truck()

        read_vin_locally(asset)

        asset.refresh_from_db()
        self.assertEqual(asset.decode_source, LOCAL_DECODE_SOURCE)
        self.assertIsNotNone(asset.decoded_at)

    def test_it_never_writes_over_something_already_there(self):
        asset = self.truck(model="My truck", engine="Whatever is in it")

        read_vin_locally(asset)

        asset.refresh_from_db()
        self.assertEqual(asset.model, "My truck")
        self.assertEqual(asset.engine, "Whatever is in it")
        self.assertEqual(asset.make, "Ford")

    def test_it_never_writes_over_a_correction(self):
        """The same promise FR-VEH-4 makes about a re-decode."""
        asset = self.truck()
        mark_override(asset, "model", "F-250 Ranger")
        asset.save()

        result = read_vin_locally(asset)

        asset.refresh_from_db()
        self.assertEqual(asset.model, "")
        self.assertIn("model", result.skipped_overridden)

    def test_running_it_twice_changes_nothing_the_second_time(self):
        asset = self.truck()
        read_vin_locally(asset)

        result = read_vin_locally(asset)

        self.assertEqual(result.applied, {})
        self.assertIn("Nothing new", result.message)


class WhatItRefusesTests(Base):
    def test_it_will_not_choose_between_two_readings(self):
        """`F25BR350001` is as good a 1961 Ford as a 1970 one. Writing one of
        them would answer a question the page is asking on the reader's
        behalf, wrongly half the time."""
        asset = Asset.objects.create(nickname="Barn find", vin="F25BR350001")

        result = read_vin_locally(asset)

        self.assertFalse(result.ok)
        self.assertIn("More than one", result.message)
        asset.refresh_from_db()
        self.assertEqual(asset.make, "")

    def test_and_says_the_year_is_what_separates_them(self):
        asset = Asset.objects.create(nickname="Barn find", vin="F25BR350001")
        self.assertIn("model year", read_vin_locally(asset).message)

    def test_the_year_settles_it_and_then_it_writes(self):
        asset = Asset.objects.create(
            nickname="Barn find", vin="F25BR350001", year=1965
        )

        self.assertTrue(read_vin_locally(asset).ok)

        asset.refresh_from_db()
        self.assertEqual(asset.model, "F-250 2WD")

    def test_a_vin_nothing_reads_is_a_dead_end_and_says_so(self):
        asset = Asset.objects.create(nickname="Mystery", vin="ZZZZZZZZZZZ")

        result = read_vin_locally(asset)

        self.assertFalse(result.ok)
        self.assertIn("Nothing here reads", result.message)

    def test_equipment_has_no_vin_to_read(self):
        mower = Asset.objects.create(nickname="Mower", asset_kind="equipment")
        self.assertFalse(read_vin_locally(mower).ok)

    def test_a_year_it_could_not_narrow_is_not_written(self):
        """`FC15225889` is a GMC of 1947, 1948, 1949 or 1950. A range is not a
        year, and the make is still worth having."""
        asset = Asset.objects.create(nickname="Old GMC", vin="FC15225889")

        read_vin_locally(asset)

        asset.refresh_from_db()
        self.assertEqual(asset.make, "GMC")
        self.assertIsNone(asset.year)

    def test_a_tonnage_is_not_a_model(self):
        """GM's series position says "1/2 ton", which is a weight. Ford's says
        "F-250 4WD", which is a model. Only the scheme knows which it has."""
        asset = Asset.objects.create(nickname="Chev", vin="CCL148Z100327")

        read_vin_locally(asset)

        asset.refresh_from_db()
        self.assertEqual(asset.make, "Chevrolet")
        self.assertEqual(asset.year, 1978)
        self.assertEqual(asset.model, "")


class OnThePageTests(Base):
    def test_the_button_is_offered_for_one_clear_reading(self):
        asset = Asset.objects.create(nickname="Old Ford", vin=TRUCK, year=1978)

        page = self.client.get(reverse("asset_detail", args=[asset.pk]))

        self.assertContains(page, reverse("vin_read", args=[asset.pk]))

    def test_and_withheld_where_two_readings_fit(self):
        asset = Asset.objects.create(nickname="Barn find", vin="F25BR350001")

        page = self.client.get(reverse("asset_detail", args=[asset.pk]))

        self.assertNotContains(page, reverse("vin_read", args=[asset.pk]))

    def test_a_modern_vehicle_is_still_sent_to_vpic(self):
        """Two buttons that fill in the same boxes from different places would
        make the provenance a coincidence of which one got pressed."""
        civic = Asset.objects.create(
            nickname="Civic", vin="1M8GDM9AXKP042788", year=2019
        )

        page = self.client.get(reverse("asset_detail", args=[civic.pk]))

        self.assertContains(page, reverse("vin_decode", args=[civic.pk]))
        self.assertNotContains(page, reverse("vin_read", args=[civic.pk]))

    def test_pressing_it_fills_the_vehicle_in(self):
        asset = Asset.objects.create(nickname="Old Ford", vin=TRUCK)

        self.client.post(reverse("vin_read", args=[asset.pk]), follow=True)

        asset.refresh_from_db()
        self.assertEqual(asset.year, 1978)

    def test_it_takes_a_post(self):
        asset = Asset.objects.create(nickname="Old Ford", vin=TRUCK)
        self.assertEqual(
            self.client.get(reverse("vin_read", args=[asset.pk])).status_code, 405
        )

    def test_it_needs_a_login(self):
        """There is no read-only role in v1 — `member` may edit a vehicle — so
        the line this guards is the one between signed in and not."""
        asset = Asset.objects.create(nickname="Old Ford", vin=TRUCK)
        self.client.logout()

        response = self.client.post(reverse("vin_read", args=[asset.pk]))

        self.assertEqual(response.status_code, 302)
        asset.refresh_from_db()
        self.assertIsNone(asset.year)


class EngineFromTheYearTests(Base):
    """An engine no position on the plate ever carried (`CA_Engine_ID.pdf`).

    GM stamped six-or-eight as a flag and the year as a code, and the engine
    sheet turns that pair into a displacement. It is a weaker-sounding claim
    than a stamped engine letter and it is not a weaker one: both say what the
    factory fitted, and the write-back holds the same line for it — blanks
    only, never over a correction.
    """

    def test_an_engine_the_number_never_carried_is_still_filled_in(self):
        asset = Asset.objects.create(nickname="Old GMC", vin="152PT5935")

        self.assertTrue(read_vin_locally(asset).ok)

        asset.refresh_from_db()
        self.assertEqual(asset.make, "GMC")
        self.assertEqual(asset.year, 1957)
        self.assertIn("270 CID", asset.engine)

    def test_a_year_it_could_not_narrow_does_not_stop_it(self):
        """`S` is 1958 or 1959, and the six was the same engine in both. The
        year stays blank because a range is not a year; the engine does not,
        because there is only one of it."""
        asset = Asset.objects.create(nickname="Old GMC", vin="152PS5935")

        read_vin_locally(asset)

        asset.refresh_from_db()
        self.assertIsNone(asset.year)
        self.assertIn("270 CID", asset.engine)

    def test_but_two_engines_are_not_an_engine(self):
        """Ford's `H` is a 390 through 1976 and a 351M after it, and this
        serial falls in both blocks. The reading honestly says both — and
        `390 CID V8 / 351M CID V8` in an engine column is not an engine, it is
        the question still being asked."""
        asset = Asset.objects.create(nickname="Barn find", vin="F26HVAE1234")

        read_vin_locally(asset)

        asset.refresh_from_db()
        self.assertEqual(asset.make, "Ford")
        self.assertEqual(asset.engine, "")

    def test_a_disputed_displacement_is_not_invented_to_fill_the_box(self):
        """The two sheets disagree about a 1953 Chevrolet V8, so the reading
        says `V8` and no more. That is what lands in the column: what the
        sheets agree on, and nothing added to round it out."""
        asset = Asset.objects.create(nickname="Old Chev", vin="VH53S7552")

        read_vin_locally(asset)

        asset.refresh_from_db()
        self.assertEqual(asset.engine, "V8")
        self.assertEqual(asset.year, 1953)
