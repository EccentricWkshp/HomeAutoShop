"""
Characters a publisher did not write, and the ones they did.

The rule being tested is narrow on purpose: repair the bytes, never the
wording. Every case here is a real definition out of a bundled list, because
the failures worth catching are the ones that actually reached the tables --
a Japanese comma read as Latin-1, an extractor's stray glyph, an en dash a
technician cannot type.
"""

from __future__ import annotations

import json
from pathlib import Path

from django.test import SimpleTestCase

from . import transcription

CODELISTS = Path(__file__).resolve().parent / "codelists"
PUBLISHED = Path(__file__).resolve().parents[2] / "catalog" / "codes"


class MisDecodedBytesTests(SimpleTestCase):
    """`bytes([0xA1, 0xA2])` is EUC-JP for the ideographic comma. That is
    decidable, so it gets decided rather than stripped."""

    def test_a_japanese_comma_read_as_latin_1_becomes_a_comma(self):
        self.assertEqual(
            transcription.tidy("VVT control(Advance¡¢retard angle fail)"),
            "VVT control(Advance,retard angle fail)",
        )

    def test_damage_that_cannot_be_read_back_is_dropped_not_guessed(self):
        """A lone `0xA3` has no lead byte to decode with. Turning it into the
        comma its neighbours decode to would read better and would be
        invention, which §8.3c refuses whether a person or a table does it."""
        self.assertEqual(
            transcription.tidy("Throttle valve stuck£-dirty block"),
            "Throttle valve stuck-dirty block",
        )


class TypographyTests(SimpleTestCase):
    def test_an_en_dash_becomes_a_hyphen(self):
        """It reads identically and is a different character to search for. A
        technician typing `LH - voltage low` should not miss the row."""
        self.assertEqual(
            transcription.tidy("sensor, LH – voltage low"), "sensor, LH - voltage low"
        )

    def test_curly_quotes_become_straight_ones(self):
        self.assertEqual(
            transcription.tidy("Alternator ‘L’ terminal"), "Alternator 'L' terminal"
        )

    def test_an_arrow_is_spelled_the_way_the_same_definition_already_spells_one(self):
        """Hyundai's `P1613` writes one direction as `U+2190` and the other as
        `->` inside a single string. Spelling both the same way is the smaller
        change."""
        self.assertEqual(
            transcription.tidy("(Communication EMS←ETS) ECM-ETS Line (EMS->ETS)"),
            "(Communication EMS<-ETS) ECM-ETS Line (EMS->ETS)",
        )

    def test_a_multiplication_sign_becomes_an_x(self):
        self.assertEqual(
            transcription.tidy("4×4 low range switch"), "4x4 low range switch"
        )


class LitterTests(SimpleTestCase):
    def test_a_bullet_standing_where_a_dash_belongs_becomes_a_dash(self):
        """Volvo settles this one: `P1637` and `P1638` are adjacent,
        identically worded, and differ only in which glyph survived."""
        self.assertEqual(
            transcription.tidy("internal temperature high • stage 1"),
            "internal temperature high - stage 1",
        )

    def test_a_stray_middle_dot_becomes_the_space_it_displaced(self):
        self.assertEqual(
            transcription.tidy("Engine coolant temperature·(ECT) sensor"),
            "Engine coolant temperature (ECT) sensor",
        )

    def test_it_does_not_stack_up_spaces_where_it_removed_something(self):
        self.assertEqual(
            transcription.tidy("position (CMP) ·actuator, bank 2"),
            "position (CMP) actuator, bank 2",
        )


class NotationSurvivesTests(SimpleTestCase):
    """Repairing everything unfamiliar would be its own kind of damage."""

    def test_a_trademark_belongs_to_the_product_it_names(self):
        self.assertEqual(
            transcription.tidy("Pass Key® II Fuel Enable Circuit"),
            "Pass Key® II Fuel Enable Circuit",
        )

    def test_real_notation_is_left_alone(self):
        for text in ("Coolant above 100°C", "Signal ± 0.5 V", "Below 50 µA"):
            with self.subTest(text=text):
                self.assertEqual(transcription.tidy(text), text)

    def test_wording_is_never_touched(self):
        """The manufacturer's phrasing is the fact being recorded. Only the
        characters carrying it are in scope."""
        plain = "Camshaft Position Sensor Circuit Range/Performance (Bank 1)"
        self.assertEqual(transcription.tidy(plain), plain)


class IdempotenceTests(SimpleTestCase):
    def test_running_it_again_changes_nothing(self):
        """This is what lets the bundled tables be *asserted* clean rather
        than cleaned once and hoped about."""
        for text in (
            "sensor, LH – voltage low",
            "VVT control(Advance¡¢retard angle fail)",
            "internal temperature high • stage 1",
        ):
            with self.subTest(text=text):
                once = transcription.tidy(text)
                self.assertEqual(transcription.tidy(once), once)

    def test_nothing_is_not_something(self):
        self.assertEqual(transcription.tidy(""), "")


def every_definition():
    """Every definition this repository ships or publishes, and where it is.

    Both, because they went different ways: the ISO/SAE sets stayed in the
    image and the manufacturer lists are published for a shop to install. A
    check that followed only one of them would have quietly stopped covering
    fourteen thousand definitions on the day they moved.
    """
    for path in sorted(CODELISTS.glob("*.json")):
        if path.name.startswith("_"):
            continue
        for code, text in json.loads(path.read_text(encoding="utf-8")).get("codes", {}).items():
            yield path.name, code, text
    for path in sorted(PUBLISHED.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        for document in data.get("documents", []):
            for code, text in (document.get("codes") or {}).items():
                yield path.name, code, text


class TheShippedListsAreCleanTests(SimpleTestCase):
    """The guard that keeps this fixed. A new transcription that reintroduces
    a publisher's typesetting fails here rather than shipping."""

    def test_every_definition_needs_no_repair(self):
        damaged = [
            f"{where} {code}: {text!r}"
            for where, code, text in every_definition()
            if transcription.tidy(text) != text
        ]
        self.assertEqual(damaged, [], f"{len(damaged)} definitions need repair")

    def test_there_are_enough_of_them_that_the_check_above_means_something(self):
        self.assertGreater(len(list(every_definition())), 15000)

    def test_both_places_definitions_live_are_covered(self):
        """Named explicitly, because the failure this guards against is not a
        definition slipping through — it is a whole folder doing."""
        seen = {where for where, _code, _text in every_definition()}
        self.assertTrue(any(n.startswith("obd-ii-") for n in seen), "the image")
        self.assertIn("ford.json", seen, "the published catalog")
