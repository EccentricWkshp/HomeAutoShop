"""
The TOPDON BT600 Plus parser, against five photographs of real paper.

Every value asserted below was read off the photograph by eye before the parser
was written, which is the only order that makes a fixture worth anything. The
captures they run against are Tesseract's own output — words, boxes and
per-word confidence, produced inside the application container — not a
transcription. That distinction matters more here than anywhere else in the
corpus: a hand-made capture of a photograph would be a record of what somebody
imagined OCR does, and every hard case in this format is a case where OCR does
something surprising.

What the real captures found, that nothing hand-made would have:

* `850CCA(CCA)` comes back as `BSOCCA(CCA)` on one slip and `B850CCA(CCA)` on a
  re-read of the same picture. The first needs repair; the second must *not*
  be repaired, or the eight in front makes it 8850.
* `79%` is printed a hair above `HEALTH:` and lands on its own line, with the
  crease below the label reading as `|`. A pipe repairs to a one.
* `CRANKING TEST` tears in half — `TEST` on one line, `-CRANKING` on the next —
  so the line under the banner *is* the banner.
* `SN:` and its value have tops 24 pixels apart, which is how a real tester
  serial got written into this repository. See `RedactingABenchTesterTests`.

Three kinds of test, as everywhere in this package: values read off the
photographs, behavior the corpus does not happen to exercise, and the rules
the parser must never break.
"""

from __future__ import annotations

import unittest

from . import fixtures, topdon_bt600_plus as bt600

TOOL = "topdon bt600 plus"

SAMPLES = [s for s in fixtures.samples() if fixtures.tool(s) == TOOL]


def report(stem: str):
    return bt600.parse_pages(fixtures.pages(fixtures.find(stem + ".words.json")))


def reading(result, key: str) -> str:
    found = result.reading(key)
    return found.value if found else ""


# --------------------------------------------------------------------------
# Building rows by hand, for the cases the five photographs do not contain
# --------------------------------------------------------------------------

GLYPH = 40.0
CHAR = 24.0
LINE = 90.0


def words(lines: list[tuple[str, str, float]]) -> list[list[dict]]:
    """A page from `(label, value, confidence)` triples.

    Geometry in the same units as a real capture — pixels, a printed line forty
    of them tall — because the row grouper takes its tolerance from the words
    and a page laid out in PDF points would exercise a different branch.
    """
    page, y = [], 100.0
    for label, value, conf in lines:
        if label:
            page.append(_word(label, 300.0, y, 95.0))
        if value:
            page.append(_word(value, 1200.0 - len(value) * CHAR, y + 4, conf))
        y += LINE
    return [page]


def _word(text: str, x: float, y: float, conf: float) -> dict:
    return {
        "text": text,
        "x0": x,
        "x1": x + len(text) * CHAR,
        "top": y,
        "bottom": y + GLYPH,
        "conf": conf,
    }


#: A made-up serial of the shape this tester prints. The real one is in the
#: private JPEGs and the committed captures have it zeroed — a serial names a
#: specific piece of equipment and this repository is public.
SERIAL = "470815Y7M3118T"

BATTERY_SLIP = [
    ("BT600PLUS", "", 95.0),
    ("TEST REPORT", "", 95.0),
    ("BATTERY TEST", "", 80.0),
    ("GOOD BATTERY", "", 95.0),
    ("HEALTH:", "79%", 95.0),
    ("CHARGE:", "100%", 95.0),
    ("VOLTAGE:", "12.62V", 95.0),
    ("MEASURED:", "755CCA", 95.0),
    ("STANDARD:", "CCA", 95.0),
    ("RATED:", "850CCA(CCA)", 95.0),
    ("TYPE:", "REGULAR FLOODED", 95.0),
    ("INTERNAL R:", "03.82mQ", 95.0),
    ("SN:", SERIAL, 95.0),
    ("", "2026-05-02 18:37:59", 95.0),
]


def slip(**changes) -> list[tuple[str, str, float]]:
    """The battery slip above, with named rows replaced."""
    out = []
    for label, value, conf in BATTERY_SLIP:
        if label in changes:
            replacement = changes[label]
            value, conf = (
                replacement if isinstance(replacement, tuple) else (replacement, conf)
            )
        out.append((label, value, conf))
    return out


def parse(lines) -> object:
    return bt600.parse_pages(words(lines))


# --------------------------------------------------------------------------
# What the paper says
# --------------------------------------------------------------------------

#: Read off the five photographs by eye. `20260830_105614` is deliberately
#: absent from the timestamp column — see `TheDamagedDigitTests`.
EXPECTED = {
    "20260830_105614": {
        "kind": "battery",
        "verdict": "good_battery",
        "health": "79",
        "charge": "100",
        "voltage": "12.62",
        "measured": "755",
        "rated": "850",
        "internal_r": "3.82",
        "type": "regular_flooded",
        "standard": "CCA",
    },
    "20260830_105624": {
        "kind": "battery",
        "verdict": "good_recharge",
        "health": "65",
        "charge": "86",
        "voltage": "12.52",
        "measured": "687",
        "rated": "850",
        "internal_r": "4.02",
        "type": "regular_flooded",
        "standard": "CCA",
        "when": "2026-01-25T13:45:33",
    },
    "20260830_105632": {
        "kind": "battery",
        "verdict": "good_recharge",
        "health": "57",
        "charge": "35",
        "voltage": "12.21",
        "measured": "640",
        "rated": "850",
        "internal_r": "4.18",
        "type": "regular_flooded",
        "standard": "CCA",
        "when": "2026-01-19T15:53:03",
    },
    "20260830_105640": {
        "kind": "battery",
        "verdict": "good_recharge",
        "health": "49",
        "charge": "0",
        "voltage": "11.90",
        "measured": "598",
        "rated": "850",
        "internal_r": "4.61",
        "type": "regular_flooded",
        "standard": "CCA",
        "when": "2026-01-17T15:44:27",
    },
}


@unittest.skipIf(not SAMPLES, "no BT600 Plus captures in the corpus")
class TheBatterySlipsTests(unittest.TestCase):
    """Every legible value on the four battery slips, from the real OCR."""

    def test_each_slip_reproduces_what_is_printed_on_it(self):
        for stem, wanted in EXPECTED.items():
            with self.subTest(sample=stem):
                found = report(stem)
                self.assertEqual(len(found.results), 1, "one receipt per photograph")
                result = found.results[0]
                self.assertEqual(result.kind, wanted["kind"])
                self.assertEqual(result.verdict.value, wanted["verdict"])
                for key in ("health", "charge", "voltage", "measured", "rated", "internal_r"):
                    self.assertEqual(reading(result, key), wanted[key], key)
                self.assertEqual(result.attribute("type").value, wanted["type"])
                self.assertEqual(result.attribute("standard").value, wanted["standard"])
                if "when" in wanted:
                    self.assertEqual(result.performed_on.value, wanted["when"])

    def test_the_units_are_the_labels_own_not_the_glyphs(self):
        """The omega comes back as `n`, `N` or `Q` on all four, never as itself.

        Which does not matter, because `INTERNAL R` is in milliohms whatever
        the camera made of the symbol. Reading the unit off the glyph would
        have produced four different units for one measurement.
        """
        for stem in EXPECTED:
            with self.subTest(sample=stem):
                found = report(stem).results[0].reading("internal_r")
                self.assertEqual(found.unit, "mΩ")
                self.assertNotIn("Ω", found.raw)

    def test_what_the_operator_keyed_in_is_marked_as_such(self):
        """`MEASURED: 755CCA` against `RATED: 850CCA` is the whole result, and
        only the first is a measurement. The second is what somebody read off
        the battery's label and typed into the tester before the test ran."""
        result = report("20260830_105624").results[0]

        self.assertTrue(result.reading("rated").entered)
        self.assertTrue(result.attribute("standard").entered)
        self.assertTrue(result.attribute("type").entered)

    def test_and_what_it_measured_is_not(self):
        result = report("20260830_105624").results[0]

        for key in ("health", "charge", "voltage", "measured", "internal_r"):
            with self.subTest(reading=key):
                self.assertFalse(result.reading(key).entered)

    def test_a_rated_capacity_states_its_standard_once(self):
        """`RATED: 850CCA(CCA)` is one number and one standard, not two."""
        result = report("20260830_105624").results[0]
        self.assertEqual(reading(result, "rated"), "850")
        self.assertEqual(result.reading("rated").unit, "CCA")

    def test_a_leading_zero_is_dropped_and_a_lone_zero_is_not(self):
        """`04.61mΩ` is 4.61. `0%` is nought, and nought is a reading.

        A battery at zero percent charge and a battery whose charge nobody
        recorded are different facts, and the second one is what an empty
        string means everywhere else in this application.
        """
        result = report("20260830_105640").results[0]
        self.assertEqual(reading(result, "internal_r"), "4.61")
        self.assertEqual(reading(result, "charge"), "0")
        self.assertEqual(result.reading("charge").warnings, [])

    def test_a_value_printed_off_its_own_line_is_still_found_and_is_flagged(self):
        """On `105614` the `79%` lands above `HEALTH:` rather than beside it."""
        health = report("20260830_105614").results[0].reading("health")
        self.assertEqual(health.value, "79")
        self.assertIn(bt600.NOT_BESIDE_LABEL, health.warnings)

    def test_every_value_carries_a_box_to_show_the_operator(self):
        for stem in EXPECTED:
            for value in report(stem).results[0].values():
                with self.subTest(sample=stem, key=value.key):
                    self.assertEqual(len(value.box), 4, "no crop to show")
                    self.assertLess(value.box[0], value.box[2])


@unittest.skipIf(not SAMPLES, "no BT600 Plus captures in the corpus")
class TwoReportsOnOneStripTests(unittest.TestCase):
    """`20260830_105647` is a cranking test and a charging test, photographed
    together. This is the case the flat extraction could not hold."""

    def setUp(self):
        self.found = report("20260830_105647")

    def test_both_reports_come_out_whole(self):
        self.assertEqual([r.kind for r in self.found.results], ["cranking", "charging"])

    def test_neither_timestamp_is_lost_to_the_other(self):
        self.assertEqual(
            [r.performed_on.value for r in self.found.results],
            ["2026-01-31T14:55:18", "2026-01-31T14:54:36"],
        )

    def test_and_neither_voltage_is(self):
        cranking, charging = self.found.results
        self.assertEqual(reading(cranking, "voltage"), "8.85")
        self.assertEqual(reading(charging, "unloaded"), "14.69")
        self.assertEqual(reading(charging, "loaded"), "14.58")

    def test_the_encounter_is_dated_by_the_last_test_in_it_not_the_first(self):
        """Print order is not time order. The cranking test is printed *above*
        a charging test taken forty-two seconds earlier, so taking the first
        receipt's clock would date the visit by where the paper was torn."""
        self.assertEqual(self.found.performed_on.isoformat(), "2026-01-31T14:55:18")

    def test_the_verdicts_survive_a_banner_torn_in_half(self):
        """OCR reads the cranking banner as `TEST` and `-CRANKING` on separate
        lines, so the line under the banner is a piece of the banner."""
        self.assertEqual(
            [r.verdict.value for r in self.found.results],
            ["cranking_low", "charging_normal"],
        )

    def test_the_graph_axis_never_becomes_a_measurement(self):
        """`24V`, `12V` and `0V` are printed under the cranking trace and read
        back as `2auu`, `12U` and `ou`. They are a picture of a scale."""
        cranking = self.found.results[0]
        self.assertEqual([v.value for v in cranking.readings if v.key == "voltage"], ["8.85"])
        self.assertEqual(len(cranking.readings), 2)

    def test_the_ripple_is_in_millivolts_and_the_voltages_are_not(self):
        charging = self.found.results[1]
        self.assertEqual(reading(charging, "ripple"), "12")
        self.assertEqual(charging.reading("ripple").unit, "mV")
        self.assertEqual(charging.reading("unloaded").unit, "V")


@unittest.skipIf(not SAMPLES, "no BT600 Plus captures in the corpus")
class TheDamagedDigitTests(unittest.TestCase):
    """The one value on the corpus this parser gets wrong, recorded as such.

    `20260830_105614` prints its timestamp across the tear-off perforation and
    the hour's second digit is physically overprinted. A person reading the
    paper makes it `18:37:59`; Tesseract makes it `19:37:59` and reports word
    confidence 89 — against 95 for the date printed beside it.

    There is no signal here to act on, and the parser does not invent one.
    Per-character confidence was tried, which Tesseract will give at no extra
    cost: on this page it marks 21 of 41 words including `79%`, `100%` and
    `SN:`, every one read perfectly. A review screen that flags half of what it
    shows teaches the reader to stop looking.

    What makes this survivable is the rest of the design rather than a rule:
    the session stays a draft, the field is editable, and the crop of the paper
    is shown beside it. This test exists so the failure is *recorded* rather
    than discovered again.
    """

    def test_the_reading_is_what_ocr_said_and_it_is_not_what_the_paper_says(self):
        found = report("20260830_105614").results[0].performed_on
        self.assertEqual(found.value, "2026-05-02T19:37:59")
        self.assertGreater(found.confidence, bt600.LOW_CONFIDENCE)

    def test_the_characters_it_was_read_from_are_kept_for_the_operator(self):
        found = report("20260830_105614").results[0].performed_on
        self.assertEqual(found.raw, "2026-05-02 19:37:59")
        self.assertEqual(len(found.box), 4)


class ReadingANumberTests(unittest.TestCase):
    """Repair is for numbers, and only where nothing cleaner is available."""

    def test_a_clean_reading_beats_a_repairable_one_that_comes_first(self):
        """OCR finds a mark before the eight and returns `B850CCA(CCA)`.

        Repairing the `B` gives 8850 — nine times the battery's capacity, and
        in range for nothing. Taking the digits that are already digits gives
        850, which is what is printed.
        """
        result = parse(slip(**{"RATED:": "B850CCA(CCA)"})).results[0]
        self.assertEqual(reading(result, "rated"), "850")
        self.assertNotIn(bt600.REPAIRED, result.reading("rated").warnings)

    def test_but_a_wholly_misread_number_is_repaired_and_says_so(self):
        result = parse(slip(**{"RATED:": "BSOCCA(CCA)"})).results[0]
        found = result.reading("rated")
        self.assertEqual(found.value, "850")
        self.assertIn(bt600.REPAIRED, found.warnings)
        self.assertLess(found.confidence, bt600.LOW_CONFIDENCE)

    def test_a_reading_outside_its_own_range_is_shown_and_not_used(self):
        """Kept as the characters it was read from, with nothing made of them.

        Dropping it would hide that the tool printed something impossible;
        trusting it would put 300 percent health into a trend.
        """
        result = parse(slip(**{"HEALTH:": "300%"})).results[0]
        found = result.reading("health")
        self.assertEqual(found.value, "")
        self.assertEqual(found.raw, "300%")
        self.assertIn(bt600.OUT_OF_RANGE, found.warnings)

    def test_an_unreadable_value_is_neither_dropped_nor_guessed(self):
        result = parse(slip(**{"VOLTAGE:": "~~~~"})).results[0]
        found = result.reading("voltage")
        self.assertEqual(found.value, "")
        self.assertEqual(found.raw, "~~~~")
        self.assertIn(bt600.UNREADABLE, found.warnings)

    def test_a_low_confidence_reading_is_kept_and_pointed_at(self):
        result = parse(slip(**{"VOLTAGE:": ("12.62V", 31.0)})).results[0]
        found = result.reading("voltage")
        self.assertEqual(found.value, "12.62")
        self.assertIn(bt600.LOW_CONF, found.warnings)


class LookingBesideTheLabelTests(unittest.TestCase):
    def test_a_label_with_nothing_beside_it_looks_at_the_next_line(self):
        lines = slip(**{"CHARGE:": ""})
        lines.insert(lines.index(("CHARGE:", "", 95.0)) + 1, ("", "86%", 95.0))
        result = parse(lines).results[0]
        found = result.reading("charge")
        self.assertEqual(found.value, "86")
        self.assertIn(bt600.NOT_BESIDE_LABEL, found.warnings)

    def test_but_never_at_a_line_with_no_digits_on_it(self):
        """A crease reads as `|`, and a pipe repairs to a one.

        Without this, `HEALTH:` on a curled receipt reached down to the crease
        below it and reported the battery at one percent. Guarded twice, since
        the crease also turns up printed *beside* the label: the fallback
        refuses a line with no digits on it, and repair refuses a single
        character whatever line it is on.
        """
        lines = slip(**{"HEALTH:": ""})
        lines.insert(lines.index(("HEALTH:", "", 95.0)) + 1, ("", "|", 40.0))
        result = parse(lines).results[0]
        self.assertEqual(reading(result, "health"), "")

    def test_and_a_single_mark_is_never_repaired_into_a_digit(self):
        """The same crease, printed beside the label rather than under it."""
        result = parse(slip(**{"HEALTH:": ("|", 40.0)})).results[0]
        found = result.reading("health")
        self.assertEqual(found.value, "")
        self.assertIn(bt600.UNREADABLE, found.warnings)

    def test_nor_at_a_line_that_belongs_to_another_label(self):
        result = parse(slip(**{"MEASURED:": ""})).results[0]
        self.assertEqual(reading(result, "measured"), "")
        self.assertEqual(result.attribute("standard").value, "CCA")


class TheVerdictTests(unittest.TestCase):
    def test_a_known_verdict_is_given_a_stable_name_and_kept_verbatim(self):
        result = parse(BATTERY_SLIP).results[0]
        self.assertEqual(result.verdict.value, "good_battery")
        self.assertEqual(result.verdict.raw, "GOOD BATTERY")

    def test_the_punctuation_the_tester_prints_does_not_change_the_name(self):
        result = parse(slip(**{"GOOD BATTERY": ""})[:3] + [("GOOD, RECHARGE", "", 95.0)] + BATTERY_SLIP[4:]).results[0]
        self.assertEqual(result.verdict.value, "good_recharge")

    def test_a_verdict_nobody_has_seen_becomes_a_name_of_itself(self):
        lines = list(BATTERY_SLIP)
        lines[3] = ("SULFATED CELL", "", 95.0)
        result = parse(lines).results[0]
        self.assertEqual(result.verdict.value, "sulfated_cell")
        self.assertEqual(result.verdict.raw, "SULFATED CELL")

    def test_a_verdict_is_never_repaired(self):
        """`G` reads as a `6` elsewhere on the page and is left alone here."""
        lines = list(BATTERY_SLIP)
        lines[3] = ("6OOD BATTERY", "", 95.0)
        result = parse(lines).results[0]
        self.assertEqual(result.verdict.raw, "6OOD BATTERY")
        self.assertEqual(result.verdict.value, "good_battery")
        self.assertLess(result.verdict.confidence, 0.95)


class TheClockTests(unittest.TestCase):
    def test_only_the_format_the_tester_prints(self):
        lines = list(BATTERY_SLIP)
        lines[-1] = ("", "05/02/2026 18:37", 95.0)
        self.assertIsNone(parse(lines).results[0].performed_on)

    def test_a_digit_that_is_not_a_digit_leaves_the_value_empty(self):
        lines = list(BATTERY_SLIP)
        lines[-1] = ("", "2026-05-02 1@:37:59", 60.0)
        found = parse(lines).results[0].performed_on
        self.assertEqual(found.value, "")
        self.assertEqual(found.raw, "2026-05-02 1@:37:59")
        self.assertIn(bt600.UNREADABLE, found.warnings)

    def test_an_impossible_clock_is_not_forced_into_one(self):
        lines = list(BATTERY_SLIP)
        lines[-1] = ("", "2026-05-02 29:37:59", 95.0)
        self.assertEqual(parse(lines).results[0].performed_on.value, "")

    def test_a_receipt_with_no_clock_says_so(self):
        result = parse(BATTERY_SLIP[:-1]).results[0]
        self.assertIsNone(result.performed_on)
        self.assertIn(bt600.NO_TIMESTAMP, result.warnings)


class TheToolTests(unittest.TestCase):
    def test_the_serial_identifies_the_tester_and_is_not_a_reading(self):
        found = parse(BATTERY_SLIP)
        self.assertEqual(found.tool.serial, SERIAL)
        self.assertEqual(found.tool.vendor, "TOPDON")
        self.assertEqual(found.tool.model, "BT600 Plus")
        self.assertNotIn("serial", [v.key for v in found.results[0].values()])

    def test_two_serials_on_one_strip_is_worth_saying_out_loud(self):
        page = words(BATTERY_SLIP)[0] + words(
            [("BT600PLUS", "", 95.0)] + BATTERY_SLIP[1:-1] + [("SN:", "999999Y7M3118T", 95.0)]
        )[0]
        for word in page[len(words(BATTERY_SLIP)[0]) :]:
            word["top"] += 2000.0
            word["bottom"] += 2000.0
        self.assertIn(bt600.SERIAL_DISAGREES, bt600.parse_pages([page]).warnings)


class ClassifyingASectionTests(unittest.TestCase):
    def test_the_banner_answers_it_when_the_banner_survived(self):
        self.assertEqual(parse(BATTERY_SLIP).results[0].kind, "battery")

    def test_and_the_labels_answer_it_when_the_banner_did_not(self):
        """White-on-black is the first thing OCR loses. The labels are black on
        white and say the same thing."""
        lines = [row for row in BATTERY_SLIP if row[0] != "BATTERY TEST"]
        self.assertEqual(parse(lines).results[0].kind, "battery")

    def test_a_page_that_is_not_one_of_these_yields_nothing(self):
        found = parse([("Grocery receipt", "", 95.0), ("Milk", "2.49", 95.0)])
        self.assertEqual(found.results, [])


class ReadingFromStoredTextTests(unittest.TestCase):
    """A photograph uploaded before the words were kept still re-parses.

    Boxes come out empty, so review offers no crop — which is honest, because
    there is nothing to crop from.
    """

    def test_a_report_read_from_lines_alone(self):
        text = "\n".join(
            f"{label} {value}".strip() for label, value, _c in BATTERY_SLIP
        )
        result = bt600.parse_text(text).results[0]
        self.assertEqual(reading(result, "health"), "79")
        self.assertEqual(result.performed_on.value, "2026-05-02T18:37:59")
        self.assertEqual(result.readings[0].box, [])


class RecognizingTheFormatTests(unittest.TestCase):
    """Several signals have to agree, or another maker's slip is claimed."""

    def test_a_bt600_printout_is_recognized(self):
        text = "\n".join(f"{a} {b}".strip() for a, b, _c in BATTERY_SLIP)
        self.assertGreaterEqual(bt600.looks_like(text), 0.8)

    def test_another_makers_battery_slip_is_not(self):
        """`BATTERY TEST` and a voltage appear on every battery slip printed."""
        other = (
            "MIDTRONICS  GR8\n"
            "BATTERY TEST  GOOD BATTERY\n"
            "MEASURED  612 CCA\n"
            "RATED     590 CCA\n"
            "VOLTAGE   12.61 V\n"
        )
        self.assertLessEqual(bt600.looks_like(other), 0.4)
