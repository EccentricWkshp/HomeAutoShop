"""A VIN older than the standard (SPEC FR-VEH-2, §5.5).

Reported as: the field requires 17 characters, but that is only the rule for
1981 and newer. A 1973–79 Ford truck carries **eleven** — `F10GLU12345`, which
is make, series, engine, model year, assembly plant, and a five-digit unit
number (fordification.net/tech/vin.htm). GM of the same era carries thirteen.

The old validator called every one of those an error *and* attached a warning
saying to save it as-is — advice it made impossible to follow, since the model
refused the save. The requirement was never "seventeen characters"; it was
"seventeen characters from 1981", and the year is the half that was missing.

So the rule now needs both facts. Where the year is known and 1981 or later, a
short VIN is still refused, because there it really is a typo — which is the
only thing the length check was ever good for.
"""

from __future__ import annotations

from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse

from homeautoshop.accounts.models import Role, User
from homeautoshop.assets import vin as vinlib
from homeautoshop.assets.models import Asset, VinStatus

#: A real 1973–79 Ford truck VIN, decoded against LMC Truck's own tables
#: (`Artifacts/VIN Decoding/`): `F26` F-250 4WD · `S` 400 CID V8 · `V`
#: Kentucky Truck · `AE1234`, which falls in the AE0,001–CK9,999 block and so
#: is a 1978. Eleven characters, and complete — note there is no model-year
#: position at all: the year is carried by which serial block the unit number
#: lands in, which is exactly the kind of scheme no 17-character rule fits.
FORD_TRUCK = "F26SVAE1234"

#: The ISO 3779 worked example, for the era that has a check digit.
MODERN = "1M8GDM9AXKP042788"


class ShortVinTests(TestCase):
    def test_an_eleven_character_ford_truck_vin_is_accepted(self):
        check = vinlib.validate(FORD_TRUCK, year=1978)

        self.assertTrue(check.is_well_formed)
        self.assertTrue(check.is_pre_1981)
        self.assertFalse(check.errors)
        self.assertEqual(check.status, VinStatus.PRE_1981)

    def test_it_is_accepted_with_no_year_known_at_all(self):
        """A barn find is exactly the case where the year is the thing nobody
        has yet, and refusing the VIN until it is known has the dependency
        backwards."""
        check = vinlib.validate(FORD_TRUCK)

        self.assertTrue(check.is_well_formed)
        self.assertFalse(check.errors)

    def test_nothing_is_claimed_about_it(self):
        """There is no check digit, no year position, and no decoder. Reporting
        a verdict on any of those would be inventing one."""
        check = vinlib.validate(FORD_TRUCK, year=1978)

        self.assertIsNone(check.check_digit_valid)
        self.assertEqual(check.possible_years, [])
        self.assertTrue(any("no standard" in w for w in check.warnings))

    def test_a_short_vin_on_a_modern_vehicle_is_still_a_typo(self):
        """The half of the length rule worth keeping. 1981 onward the VIN
        really is seventeen characters, so eleven is a mistake."""
        check = vinlib.validate(FORD_TRUCK, year=2016)

        self.assertFalse(check.is_well_formed)
        self.assertTrue(check.errors)
        self.assertIn("2016", check.errors[0])

    def test_the_year_is_named_as_what_tells_them_apart(self):
        """With no year, both readings are open — and saying so is more use
        than either guess."""
        warnings = " ".join(vinlib.validate(FORD_TRUCK).warnings)

        self.assertIn("17 characters", warnings)
        self.assertIn("model year", warnings)

    def test_that_hint_is_dropped_once_the_year_settles_it(self):
        warnings = " ".join(vinlib.validate(FORD_TRUCK, year=1978).warnings)
        self.assertNotIn("17 characters", warnings)

    def test_the_letters_the_1981_standard_bans_are_fine_before_it(self):
        """Not a guess: Ford's own 1973–79 truck tables use both. `I` is the
        assembly-plant code for Highland Park, and the 1973 serial block runs
        from `Q00,001`. The exclusion is part of the 1981 standard and has no
        authority over the schemes that came before it."""
        self.assertTrue(vinlib.validate("F10AIQ00001", year=1973).is_well_formed)

    def test_but_they_are_still_refused_in_a_modern_one(self):
        check = vinlib.validate("1M8GDM9AXKI042788")
        self.assertFalse(check.is_well_formed)
        self.assertIn("I", check.errors[0])

    def test_three_characters_is_a_half_typed_field_not_a_vin(self):
        check = vinlib.validate("F26")

        self.assertFalse(check.is_well_formed)
        self.assertTrue(check.errors)

    def test_punctuation_is_refused_at_any_length(self):
        self.assertTrue(vinlib.validate("F26$LU12345", year=1978).errors)

    def test_longer_than_seventeen_is_refused_and_says_so(self):
        check = vinlib.validate(MODERN + "99")

        self.assertFalse(check.is_well_formed)
        self.assertIn("17", check.errors[0])

    def test_the_modern_path_is_untouched(self):
        check = vinlib.validate(MODERN)

        self.assertTrue(check.is_well_formed)
        self.assertFalse(check.is_pre_1981)
        self.assertTrue(check.check_digit_valid)
        self.assertEqual(check.status, VinStatus.VALID)


class MaskingTests(TestCase):
    def test_a_short_vin_is_masked_without_growing(self):
        """The head and tail were fixed at three and six, which on eleven
        characters overlapped: the masked form came out seventeen characters
        long and printed nine of them twice."""
        masked = vinlib.mask(FORD_TRUCK)

        self.assertEqual(len(masked), len(FORD_TRUCK))
        self.assertIn("•", masked)

    def test_it_still_hides_the_middle(self):
        masked = vinlib.mask(FORD_TRUCK)
        self.assertNotIn(FORD_TRUCK[4:7], masked)

    def test_the_seventeen_character_masking_is_unchanged(self):
        masked = vinlib.mask(MODERN)

        self.assertEqual(masked, "1M8••••••••042788")
        self.assertEqual(len(masked), len(MODERN))


class SavingTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="andy", password="x" * 16, role=Role.ADMIN
        )
        self.client.force_login(self.user)

    def test_the_truck_saves_and_says_which_era_it_is_from(self):
        asset = Asset(nickname="Old Ford", vin=FORD_TRUCK.lower(), year=1978)
        asset.full_clean()
        asset.save()

        self.assertEqual(asset.vin, FORD_TRUCK)
        self.assertEqual(asset.vin_status, VinStatus.PRE_1981)

    def test_a_short_vin_on_a_modern_vehicle_is_refused_on_save(self):
        asset = Asset(nickname="Civic", vin=FORD_TRUCK, year=2016)

        with self.assertRaises(ValidationError) as caught:
            asset.full_clean()

        self.assertIn("vin", caught.exception.message_dict)

    def test_the_form_accepts_it(self):
        response = self.client.post(
            reverse("asset_create"),
            {"nickname": "Old Ford", "asset_kind": "vehicle", "status": "active",
             "vin": FORD_TRUCK, "year": "1978", "meter": "odometer"},
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(Asset.objects.get().vin, FORD_TRUCK)

    def test_clearing_the_vin_clears_what_was_claimed_about_it(self):
        """`pre_1981` used to survive a cleared VIN. That was invisible while
        nothing set it; a record with no VIN claiming a VIN format is not."""
        asset = Asset.objects.create(nickname="Old Ford", vin=FORD_TRUCK, year=1978)
        self.assertEqual(asset.vin_status, VinStatus.PRE_1981)

        asset.vin = ""
        asset.save()

        self.assertEqual(asset.vin_status, VinStatus.NONE)

    def test_the_form_refuses_it_on_a_modern_year(self):
        self.client.post(
            reverse("asset_create"),
            {"nickname": "Civic", "asset_kind": "vehicle", "status": "active",
             "vin": FORD_TRUCK, "year": "2016", "meter": "odometer"},
        )
        self.assertEqual(Asset.objects.count(), 0)


class WhatIsOfferedTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="andy", password="x" * 16, role=Role.ADMIN
        )
        self.client.force_login(self.user)
        self.truck = Asset.objects.create(nickname="Old Ford", vin=FORD_TRUCK, year=1978)

    def test_no_lookup_button_for_a_vin_no_service_can_read(self):
        """vPIC only knows the 17-character format. A button that can only
        come back empty is worse than no button."""
        page = self.client.get(
            reverse("asset_detail", args=[self.truck.pk])
        ).content.decode()

        self.assertNotIn(reverse("vin_decode", args=[self.truck.pk]), page)

    def test_the_decoder_refuses_it_by_name_if_reached_anyway(self):
        from homeautoshop.assets.services import decode_vin

        result = decode_vin(self.truck, save=False)

        self.assertFalse(result.ok)
        self.assertIn("pre-1981", result.message)

    def test_no_check_digit_verdict_is_shown_against_it(self):
        """It has no check digit, so the ⚠ that means "mismatch" would be
        reporting a fault that does not exist."""
        page = self.client.get(
            reverse("asset_detail", args=[self.truck.pk])
        ).content.decode()

        self.assertIn("pre-1981", page)

    def test_the_modern_one_still_offers_the_lookup(self):
        civic = Asset.objects.create(nickname="Civic", vin=MODERN, year=2019)
        page = self.client.get(reverse("asset_detail", args=[civic.pk])).content.decode()

        self.assertIn(reverse("vin_decode", args=[civic.pk]), page)

    def test_recalls_link_to_the_plain_page_rather_than_a_dead_search(self):
        """NHTSA's VIN search takes seventeen characters and nothing else."""
        from homeautoshop.assets.recalls import vin_lookup_url

        self.assertEqual(vin_lookup_url(self.truck), "https://www.nhtsa.gov/recalls")

    def test_a_modern_vin_still_gets_the_direct_search(self):
        from homeautoshop.assets.recalls import vin_lookup_url

        civic = Asset.objects.create(nickname="Civic", vin=MODERN, year=2019)
        self.assertIn(MODERN, vin_lookup_url(civic))


class LiveFeedbackTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="andy", password="x" * 16, role=Role.ADMIN
        )
        self.client.force_login(self.user)

    def ask(self, **params):
        return self.client.get(reverse("vin_validate"), params).content.decode()

    def test_it_reads_a_short_vin_as_pre_1981(self):
        self.assertIn("Pre-1981", self.ask(vin=FORD_TRUCK))

    def test_the_year_makes_it_agree_with_what_saving_will_do(self):
        """Otherwise the panel says "read as a pre-1981 VIN" about a half-typed
        one on a 2016 car, and then the save refuses it."""
        self.assertIn("errorlist", self.ask(vin=FORD_TRUCK, year="2016"))
        self.assertNotIn("errorlist", self.ask(vin=FORD_TRUCK, year="1978"))

    def test_a_nonsense_year_is_ignored_rather_than_fatal(self):
        self.assertNotIn("errorlist", self.ask(vin=FORD_TRUCK, year="nineteen"))

    def test_a_modern_vin_still_reports_its_check_digit(self):
        self.assertIn("Check digit valid", self.ask(vin=MODERN))
