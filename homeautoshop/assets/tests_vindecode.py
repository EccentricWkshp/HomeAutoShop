"""Reading a VIN from before there was a standard (SPEC FR-VEH-12, §8.1a).

vPIC reads the 17-character VIN and nothing else, so every vehicle built before
the 1981 model year had an identifier nothing here could interpret. The tables
that do interpret them exist — LMC Truck publishes them, and they are in
`Artifacts/VIN Decoding/` — so `F26SVAE1234` can be read as an F-250 4WD with a
400 V8 built at Kentucky Truck in 1978 without asking anyone anything.

The load-bearing test in this file is `test_every_scheme_reads_its_own_example`.
The schemes are hand-transcribed off scanned sheets, and a slip in a table does
not fail loudly — it produces a confident wrong answer about somebody's truck.
Checking every documented example on every run is what makes that slip an
ordinary test failure instead.
"""

from __future__ import annotations

from django.test import TestCase
from django.urls import reverse

from homeautoshop.accounts.models import Role, User
from homeautoshop.assets.models import Asset
from homeautoshop.assets.services import _readable_fields
from homeautoshop.assets.vin_schemes import SCHEMES
from homeautoshop.assets.vindecode import decode, describe

#: The worked example: F-250 4WD, 400 CID V8, Kentucky Truck, 1978.
TRUCK = "F26SVAE1234"


class SchemeDataTests(TestCase):
    """The transcription itself, checked against its own documentation."""

    def test_every_scheme_reads_its_own_example(self):
        """The guard on the data. A mistyped table does not raise — it answers
        wrongly and confidently, which is worse."""
        for scheme in SCHEMES:
            with self.subTest(scheme=scheme["id"]):
                read = [c.scheme for c in decode(scheme["example"])]
                self.assertIn(scheme["id"], read, scheme["example"])

    def test_every_scheme_names_the_document_it_came_from(self):
        """A disputed entry should be checkable against the page it was read
        off rather than argued about."""
        for scheme in SCHEMES:
            with self.subTest(scheme=scheme["id"]):
                self.assertTrue(scheme["source"].endswith(".pdf"))

    def test_field_widths_match_the_example(self):
        """Exactly, or with room left for the running number where a scheme
        ends in one — GM's production numbers have no fixed length."""
        for scheme in SCHEMES:
            with self.subTest(scheme=scheme["id"]):
                fixed = sum(f["width"] for f in scheme["fields"])
                if any(f["width"] == 0 for f in scheme["fields"]):
                    self.assertGreater(len(scheme["example"]), fixed)
                else:
                    self.assertEqual(fixed, len(scheme["example"]))

    def test_only_the_last_field_may_be_open_ended(self):
        """A variable-width field anywhere else would make every position
        after it unreadable."""
        for scheme in SCHEMES:
            widths = [f["width"] for f in scheme["fields"]]
            with self.subTest(scheme=scheme["id"]):
                self.assertNotIn(0, widths[:-1])

    def test_every_scheme_has_exactly_one_sequence(self):
        """The sequence is what the serial blocks are matched against, so a
        scheme with two of them, or none, would read years off the wrong part."""
        for scheme in SCHEMES:
            roles = [f["role"] for f in scheme["fields"]]
            with self.subTest(scheme=scheme["id"]):
                self.assertEqual(roles.count("sequence"), 1)

    def test_a_scheme_with_serial_blocks_has_no_year_field(self):
        """Two sources for one fact is two chances to disagree about it."""
        for scheme in SCHEMES:
            if not scheme.get("serial_blocks"):
                continue
            with self.subTest(scheme=scheme["id"]):
                self.assertNotIn("year", [f["role"] for f in scheme["fields"]])

    def test_scheme_ids_are_unique(self):
        ids = [scheme["id"] for scheme in SCHEMES]
        self.assertEqual(len(ids), len(set(ids)))


class FordTruckTests(TestCase):
    """The reported case, decoded against `FC_VIN-Chassis_ID.pdf`."""

    def read(self, vin=TRUCK, **kwargs):
        return describe(vin, **kwargs)

    def test_the_truck_is_read_completely(self):
        found = self.read()

        self.assertEqual(found.make, "Ford")
        self.assertEqual(found.scheme, "ford-truck-1973-1979")
        self.assertIn("F-250 4WD", found.summary)
        self.assertIn("400 CID", found.summary)
        self.assertIn("Kentucky Truck", found.summary)

    def test_the_year_comes_from_the_serial_block(self):
        """There is no model-year position in this scheme at all — which is
        exactly why a rule about character positions could not read one."""
        self.assertEqual(self.read().years, (1978,))
        self.assertNotIn("year", [r.role for r in self.read().readings])

    def test_the_engine_settles_a_year_the_blocks_leave_open(self):
        """Ford's own blocks overlap: `AE1234` is inside both the 1976 block
        and the 1978 one. The 400 was not offered until 1977, so the two facts
        together give one year where either alone gives two."""
        blocks = [
            b["year"]
            for scheme in SCHEMES
            if scheme["id"] == "ford-truck-1973-1979"
            for b in scheme["serial_blocks"]
            if b["from"] <= "AE1234" <= b["to"]
        ]

        self.assertEqual(sorted(blocks), [1976, 1978])
        self.assertEqual(self.read().years, (1978,))

    def test_the_same_serial_reads_as_1976_with_a_1976_engine(self):
        """The other side of the overlap, and why both blocks are kept:
        `AE1234` really is a 1976 as well. Swap the 400 for the 360 offered
        that year and the same number reads as one.

        Taken through `decode` rather than `describe`, because a 360 in a
        Kentucky-built F-250 is also a perfectly good 1967–72 truck and there
        is no honest way to choose between the two from the number alone."""
        found = next(
            c for c in decode("F26YVAE1234") if c.scheme == "ford-truck-1973-1979"
        )

        self.assertEqual(found.years, (1976,))

    def test_a_contradiction_means_this_is_not_the_scheme(self):
        """A 400 was not offered before 1977 and `Q00,500` is squarely in the
        1973 block. Nothing reconciles those, so this is the wrong table
        rather than a truck with a surprising engine."""
        self.assertNotIn(
            "ford-truck-1973-1979", [c.scheme for c in decode("F26SVQ00500")]
        )

    def test_an_unknown_code_is_reported_rather_than_invented(self):
        found = decode("F26QVAE1234")[0]

        engine = next(r for r in found.readings if r.role == "engine")
        self.assertEqual(engine.text, "")
        self.assertIn(engine, found.unknown)

    def test_the_unit_number_is_not_a_gap(self):
        """It carries a value rather than a code, so having no table entry is
        what it is supposed to look like."""
        self.assertTrue(self.read().is_complete)

    def test_i_and_q_decode_because_ford_stamped_them(self):
        """Highland Park is plant `I`, and the 1973 block starts at Q00,001 —
        both letters the 1981 standard bans."""
        found = describe("F10AIQ00001")

        self.assertIn("Highland Park", found.summary)
        self.assertEqual(found.years, (1973,))


class AmbiguityTests(TestCase):
    def test_two_schemes_of_the_same_shape_are_both_returned(self):
        """A Ford truck of 1961 and one of 1970 are both three letters, an
        engine, a plant and six digits. Choosing between them silently would
        be inventing a fact."""
        read = [c.scheme for c in decode("F25BR350001")]

        self.assertIn("ford-truck-1961-1966", read)
        self.assertIn("ford-truck-1967-1972", read)

    def test_and_describe_refuses_to_pick_one(self):
        self.assertIsNone(describe("F25BR350001"))

    def test_a_known_year_is_what_separates_them(self):
        self.assertEqual(describe("F25BR350001", year=1965).scheme, "ford-truck-1961-1966")
        self.assertEqual(describe("F25BR350001", year=1970).scheme, "ford-truck-1967-1972")

    def test_a_year_the_scheme_cannot_narrow_reports_all_of_them(self):
        """GMC's `F` covers 1947 through 1950 and the sheet narrows it only
        with production-number charts that are not transcribed. Four years is
        the true answer and one would be a guess.

        This used to be asserted against Ford's `9`, which meant 1949, 1950 or
        1951 — until the engine sheet took a year off it. A test of "cannot be
        narrowed" needs a case that genuinely cannot."""
        self.assertEqual(describe("FC15225889").years, (1947, 1948, 1949, 1950))

    def test_nothing_that_resolves_nothing_is_offered(self):
        self.assertEqual(decode("ZZZZZZZZZZZ"), [])

    def test_an_empty_vin_reads_as_nothing(self):
        self.assertEqual(decode(""), [])
        self.assertEqual(decode(None), [])


class OtherMakesTests(TestCase):
    def test_a_dodge_reads_from_its_own_sheet(self):
        found = describe("D14AE5S000105")

        self.assertEqual(found.make, "Dodge")
        self.assertEqual(found.years, (1975,))
        self.assertIn("Sweptline", found.summary)

    def test_a_chevrolet_reads_from_its_own_sheet(self):
        found = describe("CCL148Z100327")

        self.assertEqual(found.make, "Chevrolet")
        self.assertEqual(found.years, (1978,))
        self.assertIn("Fremont", found.summary)

    def test_the_division_letter_tells_a_gmc_from_a_chevrolet(self):
        self.assertIn("GMC", describe("TCS142S500121").summary)

    def test_a_seventeen_character_vin_is_left_to_vpic(self):
        """These sheets stop at 1980 and vPIC is better at what comes after."""
        self.assertEqual(decode("1M8GDM9AXKP042788"), [])


class EveryMakeTests(TestCase):
    """One VIN per make and per document, so a scheme cannot quietly go missing.

    The examples above prove each scheme reads *itself*; these prove the makes
    the sheets cover are actually reachable from a VIN somebody would type.
    """

    def test_every_document_in_the_folder_is_represented(self):
        """A sheet that was read and never transcribed should be a decision,
        not something nobody noticed. The gaps are named in the module."""
        sources = {scheme["source"] for scheme in SCHEMES}
        sources |= {s["also"] for s in SCHEMES if s.get("also")}

        for name in (
            "FA_VIN-Chassis_ID.pdf", "FB_VIN-Chassis_ID.pdf",
            "FC_VIN-Chassis_ID.pdf", "FD_VIN-Chassis_ID.pdf",
            "FBR_VIN-Chassis_ID.pdf", "ford-van-vin.pdf",
            "CA_VIN-Chassis_ID.pdf", "CB_VIN-Chassis_ID.pdf",
            "CBE_VIN-Chassis_ID.pdf", "CC_VIN-Chassis_ID.pdf",
            "chevy-van-vin.pdf", "DC_VIN-Chassis_ID.pdf", "dodge-van-vin.pdf",
            # The engine sheets, which are cross-checks rather than schemes of
            # their own. Every one of the five was read; the GM one was the
            # last, and was for a while the only sheet in the folder that had
            # been opened and never applied to anything.
            "FA_Engine_ID.pdf", "FB_Engine_ID.pdf", "FC_Engine_ID.pdf",
            "FD_Engine_ID.pdf", "CA_Engine_ID.pdf",
        ):
            with self.subTest(source=name):
                self.assertIn(name, sources)

    def test_all_four_makes_decode(self):
        for vin, make in (
            ("F26SVAE1234", "Ford"),
            ("CCL148Z100327", "Chevrolet"),
            ("FC15225889", "GMC"),
            ("D14AE5S000105", "Dodge"),
        ):
            with self.subTest(vin=vin):
                self.assertEqual(describe(vin).make, make)

    def test_a_ford_van_reads_from_the_scanned_sheet(self):
        found = describe("E04JKAE0021")

        self.assertEqual(found.scheme, "ford-van-1975-1980")
        self.assertIn("E-100 Cargo Van", found.summary)

    def test_a_1980_ford_is_the_last_year_before_the_standard(self):
        """1980 has no model-year position and no serial block of its own. The
        scheme covers one year, so the year is not in doubt."""
        found = describe("F10EU100001")

        self.assertEqual(found.years, (1980,))
        self.assertNotIn("year", [r.role for r in found.readings])

    def test_the_1980_engine_codes_are_what_separate_it_from_1973_79(self):
        """Same eleven characters, same field layout, disjoint engine letters.

        A 1978 truck is still *offered* as a 1980 with an engine code the 1980
        table does not have, because two of its three positions do resolve —
        and a partial reading is worth showing when it is all there is. What
        settles it is that the 1973–79 reading is complete and this one is not."""
        self.assertEqual(describe("F10EU100001").scheme, "ford-truck-1980")

        readings = {c.scheme: c for c in decode("F26SVAE1234")}

        self.assertTrue(readings["ford-truck-1973-1979"].is_complete)
        self.assertFalse(readings["ford-truck-1980"].is_complete)

    def test_a_seventeen_character_tail_is_not_a_production_number(self):
        """Without a cap on the open-ended field, a modern VIN came back as a
        1949 Chevrolet: two of four positions landed in a table and the other
        thirteen characters were read as a running number."""
        self.assertEqual(decode("1M8GDM9AXKP042788"), [])

    def test_the_two_ford_sheets_agree_on_the_serial_blocks(self):
        """The only independent check available on any of this: the truck sheet
        and the van sheet print the same 1975–79 blocks, so the trucks and the
        vans share one list here rather than two transcriptions of it."""
        blocks = {
            scheme["id"]: scheme["serial_blocks"]
            for scheme in SCHEMES
            if scheme["id"] in ("ford-truck-1973-1979", "ford-van-1975-1980")
        }

        self.assertEqual(len(blocks), 2)
        self.assertEqual(*blocks.values())

    def test_a_suburban_and_a_pickup_differ_only_in_the_body_digit(self):
        """`CSB_VIN-Chassis_ID.pdf` is the same scheme as the pickup sheet with
        two more body codes, so it is two codes here rather than a second
        scheme that would double every pickup's candidates."""
        self.assertIn("Suburban", describe("CCL168Z100327").summary)
        self.assertIn("Pickup", describe("CCL148Z100327").summary)

    def test_a_gmc_of_1967_reports_no_year_because_the_sheet_gives_none(self):
        """Its serial rule starts 1968 and 1969 at the same number. Saying
        "1967–71" and stopping is the whole of what is known."""
        found = describe("CE134S113045")

        self.assertEqual(found.make, "GMC")
        self.assertEqual(found.years, ())
        self.assertIn("Fleetside", found.summary)

    def test_a_running_production_number_has_no_fixed_length(self):
        """GM stamped a number as long as the plant had got to that year, so a
        scheme that demanded a width would reject most of them."""
        for tail in ("292", "0292", "00292", "000292"):
            with self.subTest(tail=tail):
                self.assertEqual(describe("5GRB" + tail).years, (1949,))

    def test_a_v8_is_a_different_length_not_a_different_value(self):
        """The position is blank for a six, so the two are separate schemes."""
        self.assertEqual(describe("3E57S7552").scheme, "chevrolet-truck-1955-1959")
        self.assertEqual(
            describe("V3E57S7552").scheme, "chevrolet-truck-1955-1959-v8"
        )

    def test_a_dodge_van_reads_despite_its_year_table_not_surviving_the_scan(self):
        """Those codes are read across from the truck sheet of the same years,
        which prints them legibly — and the module says so where it does it."""
        found = describe("B12AB2U100001")

        self.assertEqual(found.years, (1972,))
        self.assertIn("Sportsman Wagon", found.summary)

    def test_the_last_two_gaps_were_closed_after_the_reason_was_rechecked(self):
        """Both had been written off. GMC 1960–66 was said to have a vehicle
        number of no fixed length — it is four digits and a GVW letter — and
        Chevrolet 1953–55 was said to be too short to identify, when its
        two-digit model year is the most identifying field on any sheet here."""
        self.assertEqual(describe("K1502PN2611A").make, "GMC")
        self.assertEqual(describe("H53S7552").years, (1953,))

    def test_a_blank_drive_position_is_read_as_its_own_scheme(self):
        """A two-wheel-drive GMC has nothing in front of the series, so it is a
        length rather than a value — the same shape as the V8 prefixes."""
        self.assertEqual(describe("1502PN2611A").scheme, "gmc-truck-1960-1966")
        self.assertEqual(
            describe("K1502PN2611A").scheme, "gmc-truck-1960-1966-4wd"
        )

    def test_a_gmc_model_code_reports_the_span_it_names(self):
        """`N` is 1960 or 1961. The sheet narrows it with charts keyed on the
        plant, the drive and the tonnage at once, which the data format cannot
        express — so the span is reported rather than a guess at one end."""
        self.assertEqual(describe("1502PN2611A").years, (1960, 1961))

    def test_a_gm_van_reads_despite_its_engine_table_not_surviving(self):
        found = describe("CGL2594100001")

        self.assertEqual(found.years, (1979,))
        self.assertIn("350 CID", found.summary)


class UnitNumberTests(TestCase):
    """Reported against a 1979 F-100: `DH6036 — not in the table`.

    Two things wrong with one line. The consecutive unit number holds a number,
    not a code, so there is no table for it to be missing from — and on a Ford
    of these years it is the field that *determines* the model year. Calling
    the one position that answered the question a gap inverted its meaning.
    """

    def sequence_row(self, vin, **kwargs):
        found = next(
            c for c in decode(vin, **kwargs) if c.scheme == "ford-truck-1973-1979"
        )
        return next(r for r in found.readings if r.role == "sequence")

    def test_the_unit_number_says_which_block_it_falls_in(self):
        row = self.sequence_row("F10BLDH6036", year=1979)

        self.assertIn("1979", row.text)
        self.assertIn("DC0001", row.text)
        self.assertIn("FK9000", row.text)

    def test_it_is_never_reported_as_a_missing_code(self):
        self.assertTrue(self.sequence_row("F10BLDH6036", year=1979).free)

    def test_the_page_shows_the_block_instead_of_calling_it_missing(self):
        user = User.objects.create_user(
            username="andy", password="x" * 16, role=Role.ADMIN
        )
        self.client.force_login(user)
        asset = Asset.objects.create(nickname="Old Ford", vin=TRUCK, year=1978)

        page = self.client.get(reverse("asset_detail", args=[asset.pk]))

        self.assertContains(page, "in the 1978 block")
        # Nothing on this vehicle's readings is unrecognised, so the phrase
        # should not be on the page at all.
        self.assertNotContains(page, "not in the table")

    def test_where_the_blocks_overlap_it_names_both(self):
        """Showing only the year that survived the other checks would hide why
        anything needed checking."""
        text = self.sequence_row("F26SVAE1234").text

        self.assertIn("1976", text)
        self.assertIn("1978", text)

    def test_a_scheme_with_no_blocks_says_nothing_about_its_sequence(self):
        found = describe("D14AE5S000105")
        row = next(r for r in found.readings if r.role == "sequence")

        self.assertEqual(row.text, "")
        self.assertTrue(row.free)

    def test_a_weaker_reading_is_marked_as_weaker(self):
        """This VIN also reads as a van, because the van tables recognise its
        engine and its plant but not its series. It is offered, ranked below
        the complete reading, and labelled — rather than hidden, since the
        tables have documented gaps and the second answer is sometimes right."""
        readings = decode("F10BLDH6036", year=1979)

        self.assertEqual(readings[0].scheme, "ford-truck-1973-1979")
        self.assertTrue(readings[0].is_complete)
        self.assertFalse(readings[1].is_complete)
        self.assertEqual(readings[1].unknown[0].role, "series")


class EngineSheetTests(TestCase):
    """The second sheet per era, and what reading it against the first found.

    `*_Engine_ID.pdf` lists every engine code against the years and models it
    was fitted to. It is the only independent check these tables have, and it
    disagrees with the VIN sheets in several places — once in a way that was
    refusing a real vehicle.
    """

    def test_a_1954_with_the_239_v8_decodes(self):
        """The bug it found. The VIN sheet has code V as a 1955 only; the
        engine sheet has it from 1954. Against a 1954 year digit the narrow
        table made a real truck a contradiction, and it was refused."""
        found = describe("F10V4U100001")

        self.assertIsNotNone(found)
        self.assertEqual(found.years, (1954,))
        self.assertIn("239 CID", found.summary)

    def test_the_union_is_taken_where_the_sheets_disagree_on_years(self):
        """Both directions, because that is the point: being a year too broad
        costs a check, being a year too narrow refuses a vehicle. The VIN sheet
        has the 292 four-barrel in 1959–60 and the engine sheet only in 1959."""
        self.assertEqual(describe("F25D9U100001").years, (1959,))
        self.assertEqual(describe("F25D0U100001").years, (1960,))

    def test_a_flatly_contradicted_detail_is_printed_by_neither_sheet(self):
        """One says 2-bbl and the other 4-bbl. Nothing decodes off it, so the
        carburettor is dropped rather than picked."""
        summary = describe("F25D9U100001").summary

        self.assertIn("160 hp", summary)
        self.assertNotIn("bbl", summary)

    def test_the_engine_sheet_narrows_a_year_the_vin_sheet_left_open(self):
        """A 9 in the year position means 1949, 1950 or 1951 on its own. The
        226 was gone after 1950, so the two together mean one less year."""
        self.assertEqual(describe("97HC139260").years, (1949, 1950))

    def test_a_scheme_names_both_sheets_it_was_read_against(self):
        checked = [s for s in SCHEMES if s.get("also")]

        self.assertGreaterEqual(len(checked), 9)
        for scheme in checked:
            with self.subTest(scheme=scheme["id"]):
                self.assertTrue(scheme["also"].endswith("_Engine_ID.pdf"))
                self.assertNotEqual(scheme["also"], scheme["source"])
                # And it is that make's own sheet. LMC's first letter is the
                # make — F Ford, C Chevrolet and GMC, D Dodge — so checking a
                # Ford's engine table against `CA_Engine_ID.pdf` would be
                # reading one manufacturer's tables into another's numbers.
                # Nothing stopped that; the data was right by hand alone.
                self.assertEqual(scheme["also"][0], scheme["source"][0])

    def test_the_page_says_it_was_checked_against_a_second_sheet(self):
        user = User.objects.create_user(
            username="andy", password="x" * 16, role=Role.ADMIN
        )
        self.client.force_login(user)
        asset = Asset.objects.create(nickname="Old Ford", vin=TRUCK, year=1978)

        page = self.client.get(reverse("asset_detail", args=[asset.pk]))

        self.assertContains(page, "FC_VIN-Chassis_ID.pdf")
        self.assertContains(page, "FC_Engine_ID.pdf")

    def test_the_1973_79_engine_table_was_confirmed_unchanged(self):
        """Not every check finds something. The engine sheet for these years
        agrees with the VIN sheet on every code, which is worth asserting: it
        is the era the decoder is used for most."""
        self.assertEqual(describe(TRUCK).years, (1978,))
        self.assertIn("400 CID", describe(TRUCK).summary)


class ReadingItOnScreenTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="andy", password="x" * 16, role=Role.ADMIN
        )
        self.client.force_login(self.user)

    def test_the_form_reads_it_while_it_is_being_typed(self):
        page = self.client.get(reverse("vin_validate"), {"vin": TRUCK})

        self.assertContains(page, "F-250 4WD")
        self.assertContains(page, "1978")

    def test_the_vehicle_page_says_what_the_vin_means(self):
        asset = Asset.objects.create(nickname="Old Ford", vin=TRUCK, year=1978)

        page = self.client.get(reverse("asset_detail", args=[asset.pk]))

        self.assertContains(page, "Kentucky Truck")
        self.assertContains(page, "400 CID")

    def test_it_names_the_sheet_it_read_the_vin_against(self):
        """Provenance, for the same reason a decoded field carries it: somebody
        checking the claim needs to know what it was read off."""
        asset = Asset.objects.create(nickname="Old Ford", vin=TRUCK, year=1978)

        page = self.client.get(reverse("asset_detail", args=[asset.pk]))

        self.assertContains(page, "FC_VIN-Chassis_ID.pdf")

    def test_the_model_year_hint_is_dropped_once_the_year_is_known(self):
        """It was printed unconditionally, so a vehicle whose year was filled
        in was advised to fill in the year."""
        asset = Asset.objects.create(nickname="Old Ford", vin="F10BLDH6036", year=1979)

        page = self.client.get(reverse("asset_detail", args=[asset.pk]))

        self.assertNotContains(page, "Filling in the model year")

    def test_but_offered_while_it_is_still_open(self):
        page = self.client.get(reverse("vin_validate"), {"vin": "F10BLDH6036"})
        self.assertContains(page, "Filling in the model year")

    def test_a_vin_it_cannot_read_says_nothing_rather_than_guessing(self):
        asset = Asset.objects.create(nickname="Mystery", vin="ZZZZZZZZZZZ")

        page = self.client.get(reverse("asset_detail", args=[asset.pk]))

        self.assertNotContains(page, "Kentucky Truck")

    def test_the_masked_vin_is_still_what_is_shown(self):
        """Reading the VIN out loud must not print the VIN (NFR-S-5)."""
        asset = Asset.objects.create(nickname="Old Ford", vin=TRUCK, year=1978)

        page = self.client.get(reverse("asset_detail", args=[asset.pk]))

        self.assertNotContains(page, TRUCK)


class GmEngineSheetTests(TestCase):
    """`CA_Engine_ID.pdf`, and the question GM's numbering leaves open.

    The Ford sheets were read against their engine sheets and this one was not,
    which left the GM schemes saying `V8` and stopping there. GM stamped no
    engine code: the number carries a *flag* — a leading `V` on a Chevrolet, an
    `8` before the plant on a GMC — and the absence of it is the six, which is
    why those are separate schemes of a different length.

    So the plate says six-or-eight and the year, and this sheet says which six
    and which eight. Neither half is a guess and neither half is enough alone,
    which makes this the one place in the file where two documents are needed
    to read one position.
    """

    def test_the_v8_flag_and_the_year_together_name_a_displacement(self):
        self.assertIn("265 CID V8", describe("V3E57S7552").summary)
        self.assertIn("283 CID V8", describe("V3E58S7552").summary)

    def test_a_gmc_v8_was_a_different_engine_almost_every_year(self):
        """Which is what makes the flag worth reading at all: four years, four
        displacements, and the year code already says which."""
        for vin, engine in (
            ("1528PY5935", "288 CID V8"),
            ("1528PX5935", "316 CID V8"),
            ("1528PT5935", "347 CID V8"),
            ("1528PS5935", "336 CID V8"),
        ):
            with self.subTest(vin=vin):
                self.assertIn(engine, describe(vin).summary)

    def test_the_absent_flag_is_a_reading_too(self):
        """No `8` before the plant is not a gap in the number. It is the
        positive statement that this truck left Pontiac with the six."""
        found = describe("152PT5935")

        self.assertEqual(found.make, "GMC")
        self.assertIn("270 CID 6-cyl", found.summary)

    def test_it_is_marked_as_coming_from_the_year_rather_than_the_plate(self):
        """A code stamped on the door and a fact about every truck built that
        year are both true and are not the same claim."""
        found = describe("152PT5935")
        engine = next(r for r in found.readings if r.role == "engine")

        self.assertEqual(engine.code, "")
        self.assertIn("standard", engine.label)

    def test_the_engine_never_narrows_the_year_it_was_derived_from(self):
        """It is a consequence of the year, never evidence for it — letting it
        narrow would be the decoder agreeing with itself. A GMC of 1947–50 has
        four open years and the 228 does not close one of them."""
        found = describe("FC15225889")

        self.assertEqual(found.years, (1947, 1948, 1949, 1950))
        self.assertIn("228 CID 6-cyl", found.summary)

    def test_an_open_year_can_still_settle_the_engine(self):
        """`S` means 1958 or 1959 and the six was the same engine in both, so
        the reading names the engine while still refusing to name the year."""
        found = describe("152PS5935")
        engine = next(r for r in found.readings if r.role == "engine")

        self.assertEqual(found.years, (1958, 1959))
        self.assertTrue(engine.settled)
        self.assertIn("270 CID 6-cyl", engine.text)

    def test_and_offers_both_where_it_cannot(self):
        """The year unread, both engines stand, and the reading says so rather
        than picking the likelier one."""
        six = next(c for c in decode("1528PX5935") if "V8" not in c.label)
        engine = next(r for r in six.readings if r.role == "engine")

        self.assertEqual(engine.options, 2)
        self.assertIn("248 CID", engine.text)
        self.assertIn("270 CID", engine.text)

    def test_the_two_sheets_disagree_about_a_1953_v8(self):
        """The VIN sheet says a leading V marks a V8 across 1953–55 1st series.
        The engine sheet lists no Chevrolet truck V8 before the 1955 2nd
        series, which is a different scheme. Both cannot be right."""
        found = describe("VH53S7552")

        self.assertIn("V8", found.summary)
        self.assertNotIn("CID", found.summary)

    def test_and_the_era_is_left_as_the_vin_sheet_gives_it(self):
        """The asymmetry that governs every disagreement here. Narrowing this
        to 1955 would refuse a 1953 truck outright; declining to name the
        displacement only declines to answer."""
        found = describe("VH53S7552")

        self.assertEqual(found.years, (1953,))
        self.assertEqual(found.make, "Chevrolet")

    def test_the_chevrolet_six_changed_with_the_1954_model_year(self):
        self.assertIn("216.5 CID", describe("H53S7552").summary)
        self.assertIn("235 CID", describe("H54S7552").summary)

    def test_no_scheme_carries_both_a_stamped_engine_and_a_standard_one(self):
        """Two engine readings in one candidate would make which one gets
        written to a vehicle depend on field order."""
        for scheme in SCHEMES:
            if "engine_by_year" not in scheme:
                continue
            with self.subTest(scheme=scheme["id"]):
                roles = {f["role"] for f in scheme["fields"]}
                self.assertNotIn("engine", roles)

    def test_every_gm_scheme_the_sheet_covers_names_it(self):
        """1947–59 Chevrolet and GMC, both halves of each V8 split."""
        checked = {
            s["id"] for s in SCHEMES if s.get("also") == "CA_Engine_ID.pdf"
        }

        self.assertEqual(
            checked,
            {
                "chevrolet-truck-1947-1952",
                "chevrolet-truck-1953-1955",
                "chevrolet-truck-1953-1955-v8",
                "chevrolet-truck-1955-1959",
                "chevrolet-truck-1955-1959-v8",
                "gmc-truck-1947-1950",
                "gmc-truck-1955-1959",
                "gmc-truck-1955-1959-v8",
            },
        )


class ChevroletSeriesTableTests(TestCase):
    """Page 2 of `CA_VIN-Chassis_ID.pdf`, and the half of it that is readable.

    The sheet's second page expands each series into its class, wheelbase and
    bed type. Only the first two can be read from a VIN. Bed type lives in the
    fourth digit of the model number — 3104 stepside, 3124 Fleetside Cameo,
    3154 4WD — and the number stamped on the door carries only 3100, so a
    Chevrolet of these years cannot be read as a stepside and is not.
    """

    def test_the_series_names_a_wheelbase_the_number_never_carried(self):
        self.assertIn("125.25 in", describe("5GRB292").summary)
        self.assertIn("116 in", describe("H53S7552").summary)

    def test_the_same_series_moved_to_a_shorter_chassis_in_the_2nd_series(self):
        """A 3600 is a 125.25 in truck through the 1955 1st series and a
        123.25 in one after, which is why the reading takes the scheme's own
        table rather than one table for the model designation."""
        self.assertIn("123.25 in", describe("3E57S7552").summary)

    def test_it_is_marked_as_implied_rather_than_stamped(self):
        found = describe("5GRB292")
        rating = next(r for r in found.readings if r.role == "rating")

        self.assertEqual(rating.code, "")
        self.assertTrue(rating.derived)

    def test_the_model_written_to_a_vehicle_is_the_designation_alone(self):
        """It used to be the whole sentence. `model_from` points at the series
        text, so every word added there landed in a vehicle's model column —
        which is what made a `3600` into a `3600, 3/4 ton pickup, 125.25 in
        wheelbase` and is why the class and wheelbase are a reading of their
        own rather than more words in that string."""
        for vin, model in (
            ("5GRB292", "3600"),
            ("H53S7552", "3100"),
            ("3E57S7552", "3600"),
            ("V3A58S7552", "3100"),
        ):
            with self.subTest(vin=vin):
                self.assertEqual(_readable_fields(describe(vin))["model"], model)

    def test_no_bed_type_is_claimed_where_the_number_cannot_carry_one(self):
        """The one thing on that page these VINs genuinely cannot say: reading
        it would need the fourth digit of the model number, which is nowhere in
        them.

        Scoped to those schemes rather than to all of them, because a bed type
        is not unreadable in general — GMC's 1967–71 numbers carry a two-
        character body position and this file reads `0C` off it as a stepside.
        What decides it is whether the scheme has the position, which is the
        distinction a blanket ban would have flattened.
        """
        for scheme in SCHEMES:
            if "class_by_series" not in scheme:
                continue
            blob = repr(scheme["tables"]) + repr(scheme["class_by_series"])
            for absent in ("Stepside", "Fleetside", "Cameo"):
                with self.subTest(scheme=scheme["id"], term=absent):
                    self.assertNotIn(absent, blob)
