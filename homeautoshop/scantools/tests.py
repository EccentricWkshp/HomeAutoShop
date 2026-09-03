"""
Scan-report parsing, against the real corpus (SPEC §8.3a).

Nine reports, five vehicles, three years of the tool's own firmware. Every one
is a file someone actually generated in a garage, which is the only kind of
sample that finds what a hand-made fixture never would — `P219A`, a status line
that renders above the row it belongs to, `0mile`.

The corpus is captured word geometry rather than the PDFs themselves, so the
layout is real and the vehicles are not; `capture.py` explains the trade.

Three kinds of test live here, and telling them apart matters:

* **Fixture tests** lock in behavior. They prove the parser has not changed,
  not that it is right.
* **Invariant tests** check claims verifiable without trusting the parser: VIN
  check digits, timestamps against the filename the tool chose, module counts
  read off the rendered page by eye.
* **Redaction tests** are the ones that keep the repository publishable. They
  are not about parsing at all, and they run whether or not a corpus is present.
"""

from __future__ import annotations

import pathlib
import re
import unittest
from functools import lru_cache

from homeautoshop.assets import vin as vinlib

from . import capture, fixtures, manifest
from .report import ScanReport
from .xtool_d8 import looks_like_xtool_d8, parse_pages

SAMPLES = fixtures.samples()

#: The reports the **D8 parser** reads, which is no longer the whole corpus.
#:
#: The corpus began as nine reports from one scanner and every test here could
#: iterate all of it. It now holds a hundred and eighty captures from a dozen
#: tools, and three of the invariants below are facts about the D8 rather than
#: about scan reports: that it names its files `<vehicle><YYYYMMDDHHMM>`, that
#: it identifies itself to `looks_like_xtool_d8`, and that the VINs in it
#: satisfy their check digit — which a VIN issued in Europe does not, by
#: design, as `capture.synthesise_vin` is careful to preserve.
#:
#: Applied to the whole corpus they failed 171 times each and every failure was
#: the test being wrong about its own subject. Which parser reads a capture is
#: the corpus's own answer, so it is asked rather than assumed.
D8_SAMPLES = [
    s for s in SAMPLES if fixtures.BUILT_IN_PARSERS.get(fixtures.tool(s)) == "xtool_d8"
]


@lru_cache(maxsize=None)
def report_for(name: str) -> ScanReport:
    """Parsing is not cheap and most tests here read the whole corpus."""
    return parse_pages(fixtures.pages(fixtures.find(name)))


def _named(stem: str) -> str:
    return next(p.name for p in SAMPLES if p.name.startswith(stem))


@unittest.skipIf(not SAMPLES, "no captured samples in the corpus")
class FixtureTests(unittest.TestCase):
    """A profile change that breaks an older report must fail the build."""

    def test_every_sample_still_parses_to_its_fixture(self):
        for sample in SAMPLES:
            with self.subTest(report=sample.name):
                self.assertEqual(fixtures.build(sample), fixtures.load(sample))

    def test_every_sample_has_a_fixture(self):
        missing = [p.name for p in SAMPLES if not fixtures.fixture_path(p).exists()]
        self.assertEqual(missing, [])


@unittest.skipIf(not D8_SAMPLES, "no captured D8 samples in the corpus")
class InvariantTests(unittest.TestCase):
    """Claims checkable without trusting the parser.

    About the D8 and its reports specifically, not about scan reports in
    general — see `D8_SAMPLES`.
    """

    def test_every_vin_passes_its_own_check_digit(self):
        """A mis-read VIN almost never satisfies the ISO 3779 check digit.

        True of every vehicle this scanner has seen, all of them North
        American. It is not true of a VIN issued in Europe, which carries a
        filler where the check digit goes — so this is a claim about the D8
        corpus and not one to make of the corpus as a whole.
        """
        for sample in D8_SAMPLES:
            with self.subTest(report=sample.name):
                found = report_for(sample.name).vehicle.vin
                check = vinlib.validate(found)
                self.assertTrue(check.is_well_formed, found)
                self.assertTrue(check.check_digit_valid, found)

    def test_the_timestamp_agrees_with_the_filename(self):
        """The tool names its own files `<vehicle><YYYYMMDDHHMM>`."""
        for sample in D8_SAMPLES:
            with self.subTest(report=sample.name):
                stamp = re.search(r"(\d{12})$", fixtures.stem(sample))
                self.assertIsNotNone(stamp, "unexpected filename shape")
                generated = report_for(sample.name).generated_at
                self.assertIsNotNone(generated)
                self.assertEqual(generated.strftime("%Y%m%d%H%M"), stamp.group(1))

    def test_nothing_is_left_unrecognized(self):
        for sample in D8_SAMPLES:
            with self.subTest(report=sample.name):
                self.assertEqual(report_for(sample.name).warnings, [])

    def test_the_tools_own_hyphens_never_survive(self):
        """U+2011 renders as a hyphen and matches none of the usual patterns."""
        for sample in D8_SAMPLES:
            with self.subTest(report=sample.name):
                self.assertNotIn("‑", repr(report_for(sample.name).to_dict()))

    def test_a_report_identifies_itself(self):
        for sample in D8_SAMPLES:
            with self.subTest(report=sample.name):
                text = "\n".join(
                    " ".join(word["text"] for word in page)
                    for page in fixtures.pages(sample)
                )
                self.assertEqual(looks_like_xtool_d8(text), 1.0)

    def test_something_else_entirely_is_not_mistaken_for_one(self):
        self.assertEqual(looks_like_xtool_d8("An invoice for two tires and a balance."), 0.0)


@unittest.skipIf(not SAMPLES, "no captured samples in the corpus")
class ReadByEyeTests(unittest.TestCase):
    """Values confirmed against the rendered page, not against the parser."""

    def test_codes_belong_to_the_module_banner_above_them(self):
        """F-150: nine modules listed, two with codes. Seven are simply clean."""
        report = report_for(_named("F150202404"))
        self.assertEqual(len(report.modules), 9)
        by_module: dict[str, list[str]] = {}
        for dtc in report.dtcs:
            by_module.setdefault(dtc.module, []).append(dtc.code)

        self.assertEqual(by_module["IC"], ["B1352-20", "B1318-20"])
        self.assertEqual(by_module["VSM"], ["B1322", "B1695", "U1950"])
        self.assertEqual(report.modules_with_codes, ["IC", "VSM"])

    def test_a_failure_type_suffix_survives(self):
        report = report_for(_named("F150202404"))
        self.assertIn("B1352-20", [d.code for d in report.dtcs])

    def test_a_hex_code_is_not_dropped_for_looking_wrong(self):
        """P219A is real; a digits-only pattern silently discarded all three."""
        report = report_for(_named("Corolla"))
        self.assertEqual([d.code for d in report.dtcs], ["P219A", "P219A", "P219A"])
        self.assertEqual([d.status for d in report.dtcs], ["current", "history", "current"])

    def test_gm_reports_three_outcomes_and_all_three_are_kept(self):
        report = report_for(_named("Silverado202504"))
        dtc = report.dtcs[0]
        self.assertEqual(dtc.code, "B2725-08")
        self.assertEqual(dtc.last_test, "Passed")
        self.assertEqual(dtc.this_ignition, "Passed")
        self.assertEqual(dtc.since_clear, "Passed & failed")
        # Not present on the last test, so history rather than current.
        self.assertEqual(dtc.status, "history")

    def test_zero_miles_means_not_read_not_zero(self):
        """Recording it as 0 would reset every mileage interval on the vehicle."""
        self.assertIsNone(report_for(_named("F150202404")).vehicle.odometer)
        self.assertEqual(report_for(_named("AerioRH")).vehicle.odometer, 152320)

    def test_live_data_keeps_its_range_and_unit(self):
        report = report_for(_named("Silverado202504"))
        by_name = {d.name: d for d in report.live_data}
        voltage = by_name["Control module voltage signal"]
        self.assertEqual(
            (voltage.value, voltage.maximum, voltage.minimum), ("14.40", "15.84", "12.96")
        )
        self.assertEqual(voltage.unit, "V")
        self.assertEqual(voltage.module, "Transfer case control module")

    def test_ecu_part_numbers_are_captured(self):
        report = report_for(_named("Silverado202504"))
        self.assertEqual(report.ecu["Software part number"], "24261370")
        self.assertEqual(report.ecu["Calibration part number"], "24261373")

    def test_a_report_with_no_codes_is_not_an_error(self):
        report = report_for(_named("Silverado202606181500"))
        self.assertEqual(report.dtcs, [])
        self.assertTrue(report.vehicle.vin)


# --------------------------------------------------------------------------
# Keeping the repository publishable
# --------------------------------------------------------------------------

REPO = pathlib.Path(__file__).resolve().parents[2]

SCANNED_SUFFIXES = {".py", ".json", ".md", ".yaml", ".yml", ".html", ".txt", ".cfg", ".toml"}
SKIP_DIRECTORIES = {".git", "venv", ".venv", "node_modules", "staticfiles", "data", "__pycache__"}

# VIN-shaped tokens permitted in the tree. Everything here is deliberate:
# synthetic replacements, or published examples that belong to no one.
ALLOWED_VINS = capture.synthetic_vins() | {
    # The ISO 3779 worked example, used so VIN tests do not depend on an
    # invented check digit agreeing with the author who invented it.
    "1M8GDM9AXKP042788",
    # NHTSA's own published decode examples.
    "1FTFW1ET5DFC10312",
    "1HGCR2F3XFA027534",
}

#: The same rule `capture.VIN_SHAPED` uses, and for the same reason: `\b`
#: counts an underscore as a word character, so a VIN published as
#: `<VIN>_Aug_17` was invisible to the guard as well as to the redactor.
VIN_SHAPED = re.compile(r"(?<![A-Z0-9])[A-HJ-NPR-Z0-9]{17}(?![A-Z0-9])")


def _is_mask(token: str) -> bool:
    """Seventeen of the same character is a mask, not somebody's vehicle.

    `00000000000000000` satisfies the ISO 3779 check digit — the weighted sum
    of nothing is nothing, and nothing modulo eleven is zero — so the
    equipment rules, which zero a serial to keep its shape, produce something
    this guard reported as a real VIN. That is the redaction working, and a
    guard that fails on its own successes is one somebody learns to ignore.
    """
    return len(set(token)) == 1


def _ignored() -> set[pathlib.Path]:
    """Files git would never publish, so files this test cannot be about.

    The subject here is **what leaves this machine**, and the working tree is
    not that. Research notes gathered for a later phase — a manifest of
    third-party report URLs, some of which carry a VIN in the path — sat
    untracked and gitignored and failed a test about the contents of the
    repository, which they were never going to be part of.

    Only *ignored* files are skipped, never merely untracked ones. A new
    untracked file is one `git add` away from being published, and catching it
    then is the whole reason this runs before a commit rather than after.

    If git cannot answer — no git, not a checkout, a machine that has never
    run it — nothing is skipped and everything is scanned. Wrong in the
    direction that produces a false alarm rather than a quiet publication.
    """
    import subprocess

    try:
        done = subprocess.run(
            ["git", "ls-files", "--others", "--ignored", "--exclude-standard", "-z"],
            cwd=REPO,
            capture_output=True,
            timeout=30,
            check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return set()
    return {
        REPO / name.decode("utf-8")
        for name in done.stdout.split(b"\0")
        if name
    }


class RedactionTests(unittest.TestCase):
    """The repository is going to be public. Nobody's vehicle goes with it.

    Cleaning up afterwards does not work: a VIN committed once lives in the
    history forever, and the fix is a rewrite that every clone has to be told
    about. This runs on every test run so the answer is known before a commit,
    not after a publish.
    """

    def test_no_real_vin_appears_anywhere_in_the_tree(self):
        """A real VIN satisfies its check digit; a random 17 characters does not."""
        offenders = []
        ignored = _ignored()
        for path in REPO.rglob("*"):
            if not path.is_file() or path.suffix.lower() not in SCANNED_SUFFIXES:
                continue
            if any(part in SKIP_DIRECTORIES for part in path.parts):
                continue
            if path in ignored:
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            for candidate in set(VIN_SHAPED.findall(text)):
                if candidate in ALLOWED_VINS or _is_mask(candidate):
                    continue
                if vinlib.validate(candidate).check_digit_valid:
                    offenders.append(f"{path.relative_to(REPO)}: {candidate}")
        self.assertEqual(
            offenders,
            [],
            "Valid VINs found in the tree. Redact them, or add them to "
            "ALLOWED_VINS with a reason if they belong to nobody.",
        )

    def test_every_stand_in_is_a_usable_vin(self):
        """A replacement that fails validation would weaken the corpus silently."""
        self.assertEqual(capture.verify_synthetic_vins(), [])

    def test_redaction_rewrites_a_real_vin_and_leaves_other_text_alone(self):
        """The rule keys off the check digit, so part numbers survive intact."""
        redacted, produced = capture.redact("VIN 1M8GDM9AXKP042788 SN: D8-123456")
        self.assertNotIn("1M8GDM9AXKP042788", redacted)
        self.assertIn("D8-000000", redacted)
        self.assertEqual(len(produced), 1)
        self.assertTrue(vinlib.validate(produced.pop()).check_digit_valid)

        # 17 characters that are not a VIN must not be touched.
        untouched = "Calibration ABCDEFGHJKLMNPRST"
        self.assertEqual(capture.redact(untouched)[0], untouched)

    def test_redaction_is_stable_across_runs(self):
        """Re-capturing must not churn the corpus with new stand-ins."""
        first = capture.synthesise_vin("1M8GDM9AXKP042788")
        self.assertEqual(first, capture.synthesise_vin("1M8GDM9AXKP042788"))

    def test_raw_reports_and_photographs_are_ignored_by_git(self):
        """The corpus ships as redacted captures; the originals stay local.

        A property of the repository rather than of the application, so it
        skips where there is no checkout — inside the built image, which
        deliberately contains neither .gitignore nor the originals it names.
        """
        ignore_file = REPO / ".gitignore"
        if not ignore_file.exists():
            self.skipTest("not a checkout — nothing to enforce here")
        ignore = ignore_file.read_text(encoding="utf-8")
        for pattern in ("Artifacts/samples/**/*.pdf", "Artifacts/samples/*.jpg"):
            self.assertIn(pattern, ignore)

    @unittest.skipIf(not SAMPLES, "no captured samples in the corpus")
    def test_the_committed_corpus_carries_no_real_identifier(self):
        for sample in SAMPLES:
            with self.subTest(report=sample.name):
                for found in VIN_SHAPED.findall(sample.read_text(encoding="utf-8")):
                    if _is_mask(found):
                        continue
                    if vinlib.validate(found).check_digit_valid:
                        self.assertIn(found, ALLOWED_VINS, f"{found} is not a stand-in")


class MachineReadTests(unittest.TestCase):
    """A capture is what a reader produced, never what a person typed.

    This matters most for photographs, and it is the one place the temptation
    is real: a JPEG cannot be committed, OCR needs Tesseract installed, and
    writing the words out by hand looks like a reasonable way to get a fixture
    when the machine in front of you has no OCR on it.

    It is not. A hand-made capture is a record of what somebody *imagined* OCR
    does, and every hard case in the BT600 Plus format is a case where OCR does
    something surprising — `850CCA(CCA)` read as `BSOCCA(CCA)`, a value landing
    a line above its own label, a section banner torn in half. A fixture built
    on a transcription would have passed on all of them and shipped a parser
    that failed on all of them.

    So the capture says how it was read, and this refuses anything that does
    not say `ocr`. `python -m homeautoshop.scantools.capture <file>.jpg`, in the
    container, which has Tesseract.
    """

    @unittest.skipIf(not SAMPLES, "no captured samples in the corpus")
    def test_every_photograph_was_read_by_a_machine(self):
        import json

        for sample in SAMPLES:
            data = json.loads(sample.read_text(encoding="utf-8"))
            if data.get("media_type") != "image":
                continue
            with self.subTest(capture=sample.name):
                self.assertEqual(
                    data.get("read_by"),
                    "ocr",
                    "an image capture must record that OCR produced it",
                )

    @unittest.skipIf(not SAMPLES, "no captured samples in the corpus")
    def test_a_photograph_keeps_the_confidence_its_reader_reported(self):
        """Without it every value would be assumed good, which is the opposite
        of what a photograph deserves."""
        import json

        for sample in SAMPLES:
            data = json.loads(sample.read_text(encoding="utf-8"))
            if data.get("media_type") != "image":
                continue
            words = [w for page in data.get("pages") or [] for w in page]
            with self.subTest(capture=sample.name):
                self.assertTrue(words, "no words at all")
                self.assertTrue(all("conf" in w for w in words))
                self.assertTrue(all("bottom" in w for w in words))


class ShapeTests(unittest.TestCase):
    def test_a_report_serializes_to_plain_json_types(self):
        import json

        json.dumps(ScanReport().to_dict())

    def test_a_tester_report_serializes_to_plain_json_types(self):
        import json

        from .report import TesterReport, TestResult, Value

        json.dumps(
            TesterReport(
                results=[TestResult(kind="battery", readings=[Value(key="health")])]
            ).to_dict()
        )


class TheCorpusIsWhereItSaysItIsTests(unittest.TestCase):
    """The corpus exists, and is filed the way the README says.

    Written after it silently was not. The reports were reorganized into the
    per-tool folders this README has always specified — `scan-reports/
    tool-model/` — and `fixtures.samples()` still used a flat `glob`, so it
    found **nothing**. Most tests here take `SAMPLES` and iterate, and a loop
    over an empty list passes; the profile-verification tests skip politely
    when the corpus is thin. The suite stayed green while every corpus-backed
    check in it had quietly stopped running.

    That is the failure worth a test of its own: not "is the parser right" but
    "is anything actually being parsed".
    """

    def test_the_corpus_is_not_empty(self):
        self.assertGreaterEqual(
            len(fixtures.samples()),
            5,
            "no captured reports were found — the parser tests are passing "
            "over an empty corpus",
        )

    def test_every_capture_is_filed_under_a_tool(self):
        """A flat pile of reports stops saying what produced them the moment
        there is a second scanner, which is why the README specifies a folder
        per tool. A capture at the root is loose, not merely untidy."""
        for capture in fixtures.samples():
            with self.subTest(capture=capture.name):
                self.assertTrue(
                    fixtures.tool(capture),
                    "sits at the corpus root rather than under a tool folder",
                )

    def test_every_capture_has_its_expected_output_beside_it(self):
        """Half a fixture is a test that cannot fail."""
        for capture in fixtures.samples():
            with self.subTest(capture=capture.name):
                self.assertTrue(fixtures.fixture_path(capture).exists())

    def test_a_capture_can_be_found_by_name(self):
        """What the rest of the suite relies on now that paths are nested."""
        first = fixtures.samples()[0]

        self.assertEqual(fixtures.find(first.name), first)

    def test_and_a_name_that_is_not_there_says_so(self):
        with self.assertRaises(FileNotFoundError):
            fixtures.find("NoSuchReport.words.json")


class RedactingAVinTests(unittest.TestCase):
    """The two rules, and why one was never enough.

    The corpus began as reports from one North American garage, where every
    VIN satisfies its ISO 3779 check digit and keying off that is exact. It now
    holds reports gathered from the public web, and position 9 is a check digit
    only where a regulator requires one — a VIN issued in Europe carries a
    filler there. The check-digit rule alone would have published every
    European VIN in the corpus.
    """

    #: A real-shaped European VIN: `Z` where North America puts a check digit.
    EUROPEAN = "WVWZZZ1KZAW111111"

    def test_a_labelled_vin_is_replaced_whatever_its_check_digit_says(self):
        self.assertFalse(vinlib.validate(self.EUROPEAN).check_digit_valid)

        redacted, produced = capture.redact(f"VIN: {self.EUROPEAN}")

        self.assertNotIn(self.EUROPEAN, redacted)
        self.assertEqual(len(produced), 1)

    def test_an_unlabelled_one_is_left_alone_unless_it_validates(self):
        """Otherwise every seventeen-character part number would be mangled."""
        text = f"Calibration {self.EUROPEAN} rev 2"
        self.assertEqual(capture.redact(text)[0], text)

    def test_a_fullwidth_colon_is_a_colon(self):
        """A TOPDON report writes `VIN：` with U+FF1A and `Mileage:` with an
        ASCII one, in the same header."""
        redacted, produced = capture.redact(f"VIN：{self.EUROPEAN}")
        self.assertNotIn(self.EUROPEAN, redacted)
        self.assertEqual(len(produced), 1)

    def test_a_stand_in_for_a_european_vin_keeps_its_filler(self):
        """Recomputing a check digit would turn a European VIN into a North
        American-shaped one and delete from the corpus the case this
        application's VIN validation exists to tolerate."""
        stand_in = capture.synthesise_vin(self.EUROPEAN)
        self.assertEqual(stand_in[8], "Z")
        self.assertTrue(vinlib.validate(stand_in).is_well_formed)

    def test_a_stand_in_for_a_north_american_vin_still_validates(self):
        stand_in = capture.synthesise_vin("1M8GDM9AXKP042788")
        self.assertTrue(vinlib.validate(stand_in).check_digit_valid)

    def test_the_manufacturer_and_model_year_survive_either_way(self):
        """What makes a sample representative is shared with millions of cars;
        only the serial identifies one."""
        for real in (self.EUROPEAN, "1M8GDM9AXKP042788"):
            with self.subTest(vin=real):
                self.assertEqual(capture.synthesise_vin(real)[:8], real[:8])
                self.assertEqual(capture.synthesise_vin(real)[9:11], real[9:11])

    def test_an_underscore_does_not_hide_a_vin(self):
        """Two public samples are published as `<VIN>_Aug_17_2025_LiveData`.
        `\\b` counts an underscore as a word character, so the boundary never
        matched and the VIN reached a filename."""
        real = "1M8GDM9AXKP042788"
        redacted, produced = capture.redact(f"{real}_Aug_17_2025_LiveData")
        self.assertNotIn(real, redacted)
        self.assertEqual(len(produced), 1)


class RedactingALabelledValueTests(unittest.TestCase):
    """Values that are only identifiable by what they follow.

    Nothing about the shape of `raffi` says it is a person. The label above it
    does, and a TOPDON report prints exactly that: `User: raffi`.
    """

    def test_a_personal_value_is_removed_and_an_equipment_one_keeps_its_shape(self):
        text = "VIN: -   License Plate: ZR86BR\n   Shop #: WSC 01357 011 00200\n"
        redacted, _ = capture.redact_document(text)

        self.assertNotIn("ZR86BR", redacted)
        self.assertIn(capture.MASK, redacted)
        self.assertNotIn("01357", redacted)
        # Zeroed rather than blanked: the shape is what a parser matches on.
        self.assertIn("Shop #: 000 00000 000 00000", redacted)

    def test_an_empty_value_does_not_reach_down_the_page_for_one(self):
        """`\\s*` crossed the blank line below `Repair Order:` to find a value,
        and a row of eighty dashes came out as the redaction mask."""
        text = "Mileage: 46280km   Repair Order: \n\n\n" + "-" * 20 + "\n"
        redacted, _ = capture.redact_document(text)
        self.assertIn("-" * 20, redacted)

    def test_an_email_address_goes_wherever_it_appears(self):
        redacted, _ = capture.redact("Email: somebody@example.com")
        self.assertNotIn("example.com", redacted)


class RedactingWordGeometryTests(unittest.TestCase):
    """The same rules where the label and the value are separate words.

    This is the half a line-shaped rule cannot reach, and the half that has
    twice published something: one word after the label was blanked and
    `Shop Name: <name> vehicle testing center Al-ain` came out with four fifths
    of the name intact.
    """

    def _page(self, *words):
        """Words as `(text, top)`, laid out left to right."""
        return [
            {"text": text, "top": top, "x0": 10.0 * index, "x1": 10.0 * index + 8}
            for index, (text, top) in enumerate(words)
        ]

    def test_a_personal_value_is_blanked_to_the_end_of_its_line(self):
        page = self._page(
            ("Shop", 26.6), ("Name:", 26.6), ("Somebody", 26.6),
            ("vehicle", 26.6), ("testing", 26.6), ("centre", 26.6),
        )
        out, _ = capture.redact_words(page)
        self.assertEqual(out[:2], ["Shop", "Name:"])
        self.assertEqual(set(out[2:]), {capture.MASK})

    def test_it_stops_one_word_early_when_the_next_word_is_a_label(self):
        """`Tel: <number>  Test Time: …` keeps its `Test`."""
        page = self._page(
            ("Tel:", 37.0), ("555-0100", 37.0), ("Test", 37.0), ("Time:", 37.0),
            ("2025-12-21", 37.0),
        )
        out, _ = capture.redact_words(page)
        self.assertEqual(out[1], capture.MASK)
        self.assertEqual(out[2:], ["Test", "Time:", "2025-12-21"])

    def test_it_does_not_run_on_to_the_next_line(self):
        page = self._page(("Address:", 57.8), ("Somewhere", 57.8), ("Vehicle", 88.3))
        out, _ = capture.redact_words(page)
        self.assertEqual(out, ["Address:", capture.MASK, "Vehicle"])

    def test_a_label_on_another_line_labels_nothing(self):
        """Reading order is not layout. The D8 emits `SN:` and then, three
        lines down the page, `Diagnosis` — and the heading of the section
        naming how the scan was run was zeroed in all nine reports."""
        page = self._page(("Mileage", 141.75), ("SN:", 141.75), ("Diagnosis", 162.57))
        out, _ = capture.redact_words(page)
        self.assertEqual(out[-1], "Diagnosis")

    def test_an_empty_field_does_not_consume_the_label_beside_it(self):
        page = self._page(("Customer:", 267.4), ("Technician:", 267.4))
        out, _ = capture.redact_words(page)
        self.assertEqual(out, ["Customer:", "Technician:"])


#: A **made-up** serial of the shape a BT600 Plus prints. The real one lives in
#: the private JPEGs and belongs nowhere else: it names a specific piece of
#: equipment, this repository is public, and a public repository is forever.
#: Writing the genuine value into a test *about redacting it* is the mistake
#: this class exists to catch, and it was made here once.
SERIAL = "470815Y7M3118T"


class RedactingABenchTesterTests(unittest.TestCase):
    """The rule that reaches a photograph, using the geometry that broke it.

    Every redaction rule here works by asking what is printed *beside* a value,
    and `_same_line` is what answers. It compared word **tops** against a
    tolerance scaled from the words' own height — which is correct arithmetic
    and the wrong statistic. On a real OCR capture of a BT600 Plus slip, `SN:`
    and the serial beside it have tops 24 pixels apart, because the colon runs
    a full cap height and the serial is digits, against a tolerance of 21.

    So they were not the same line, so no label preceded the serial, so the
    tester's real serial number was written into this repository and committed.
    Getting *beside* wrong does not weaken the redaction; it switches it off.
    """

    #: The geometry is from `20260830_105632.words.json` exactly as captured;
    #: only the serial is a stand-in.
    SERIAL_ROW = [
        {"text": "SN:", "x0": 320.0, "x1": 388.0, "top": 1579.0, "bottom": 1634.0},
        {
            "text": SERIAL,
            "x0": 719.0,
            "x1": 1094.0,
            "top": 1603.0,
            "bottom": 1639.0,
        },
    ]

    def test_a_tester_serial_is_zeroed_even_when_its_label_sits_higher(self):
        texts, _made = capture.redact_words(self.SERIAL_ROW)

        self.assertEqual(texts, ["SN:", "0" * len(SERIAL)])

    def test_the_label_beside_it_is_kept_because_a_parser_matches_on_it(self):
        texts, _made = capture.redact_words(self.SERIAL_ROW)

        self.assertEqual(texts[0], "SN:")

    def test_the_tolerance_is_taken_from_the_words_not_from_a_constant(self):
        """Two points is right for a PDF and meaningless for a photograph."""
        self.assertEqual(capture.line_tolerance([]), capture.LINE_TOLERANCE)
        self.assertGreater(capture.line_tolerance(self.SERIAL_ROW), 20)

    def test_a_value_on_the_line_below_is_still_a_different_line(self):
        """The fix must not join the whole page into one row."""
        below = [
            self.SERIAL_ROW[0],
            {**self.SERIAL_ROW[1], "top": 1750.0, "bottom": 1786.0},
        ]
        texts, _made = capture.redact_words(below)

        self.assertEqual(texts[1], SERIAL)


class CapturingATextReportTests(unittest.TestCase):
    """Captures of things that are not PDFs, and how much of one is kept."""

    def _write(self, name: str, body: str) -> pathlib.Path:
        import tempfile

        folder = pathlib.Path(tempfile.mkdtemp())
        path = folder / name
        path.write_text(body, encoding="utf-8")
        return path

    def test_a_report_is_kept_whole(self):
        body = "VCDS Version: 16.5\n" + "Address 01: Engine\n" * 200
        data, _ = capture.capture_document(self._write("scan.txt", body))
        self.assertEqual(data["media_type"], "text")
        self.assertNotIn("truncated", data)
        self.assertIn("Address 01", data["text"].splitlines()[-1])

    def test_a_tabular_log_is_cut_down_and_says_so(self):
        """A live-data export is a header and then ten thousand rows of the
        same five columns. The public ones come to 336 MB between them, which
        is not a thing to put in a git history to show that a comma separates
        two numbers."""
        body = "time,rpm,load\n" + "".join(f"{n},800,12\n" for n in range(500))
        data, _ = capture.capture_document(self._write("log.csv", body))

        self.assertEqual(data["media_type"], "csv")
        self.assertTrue(data["truncated"])
        self.assertEqual(data["source_lines"], 502)
        self.assertEqual(len(data["text"].splitlines()), capture.TABLE_ROWS + 1)

    def test_the_capture_names_the_file_it_came_from(self):
        data, _ = capture.capture_document(self._write("scan.txt", "VIN: -\n"))
        self.assertEqual(data["source"], "scan.txt")


class ManifestTests(unittest.TestCase):
    """Reading the research manifests into a fetch plan (`manifest.py`)."""

    def entry(self, **overrides):
        base = {
            "vendor": "Ross-Tech",
            "product": "VCDS",
            "access_type": "raw_file",
            "parser_scope": "diagnostic_report",
            "format": "txt",
            "direct_url": "https://example.invalid/scans/golf-auto-scan.txt",
        }
        return {**base, **overrides}

    def test_a_folder_is_the_vendor_and_the_product(self):
        self.assertEqual(manifest.folder_for(self.entry()), "ross-tech vcds")

    def test_corporate_history_is_not_part_of_a_tool_name(self):
        """`Creosys / PLX Devices` and `Car Scanner / Torque ecosystem` name
        companies, not the thing in somebody's hand."""
        self.assertEqual(
            manifest.folder_for(self.entry(vendor="Creosys / PLX Devices", product="OBD Auto Doctor")),
            "creosys obd auto doctor",
        )

    def test_a_product_nobody_recorded_says_so_rather_than_collecting_tools(self):
        folder = manifest.folder_for(self.entry(product="unknown"))
        self.assertEqual(folder, "ross-tech unspecified")

    def test_a_gist_url_does_not_name_every_sample_raw(self):
        """A gist's download URL ends in `/raw` for every gist there has ever
        been, so the basename gives them all the same name."""
        name = manifest.filename_for(
            self.entry(title="", direct_url="https://gist.example.invalid/abc123/raw")
        )
        self.assertEqual(name, "abc123.txt")

    def test_a_vin_never_reaches_a_filename(self):
        """Two entries are published as `<VIN>_Aug_17`. A corpus that redacts
        the inside of a report and prints the vehicle on the outside of it has
        protected nothing."""
        real = "1M8GDM9AXKP042788"
        name = manifest.filename_for(
            self.entry(
                title=f"{real}_Aug_17_2025",
                format="csv",
                direct_url="https://example.invalid/logs/live.csv",
            )
        )
        self.assertNotIn(real.lower(), name.lower())
        self.assertTrue(name.endswith(".csv"))
        # The stand-in is still a stand-in, so re-fetching produces the same
        # name rather than churning the corpus.
        self.assertEqual(
            name,
            manifest.filename_for(
                self.entry(
                    title=f"{real}_Aug_17_2025",
                    format="csv",
                    direct_url="https://example.invalid/logs/live.csv",
                )
            ),
        )

    def test_a_url_carrying_a_vin_is_withheld_rather_than_redacted(self):
        """A stand-in in a provenance record is a lie about where a file came
        from, so the field is dropped and the digest identifies it instead."""
        self.assertTrue(manifest.carries_a_vin("https://x.invalid/1M8GDM9AXKP042788_Aug"))
        self.assertFalse(manifest.carries_a_vin("https://x.invalid/golf-auto-scan.txt"))

    def test_the_record_withholds_a_title_as_well_as_a_url(self):
        sample = manifest.Sample(
            entry=self.entry(title="1M8GDM9AXKP042788_Aug_17_2025"),
            folder="ross-tech vcds",
            filename="scan.txt",
        )
        record = manifest.record_for(
            sample, digest="abc", size=1, media_type="text", retrieved_on="2026-09-01"
        )
        self.assertNotIn("title", record)
        self.assertEqual(record["title_withheld"], "carries a VIN")
        self.assertEqual(record["sha256"], "abc")

    def test_only_entries_naming_a_fetchable_file_are_wanted(self):
        self.assertTrue(manifest.wanted(self.entry()))
        self.assertFalse(manifest.wanted(self.entry(access_type="forum_post")))
        self.assertFalse(manifest.wanted(self.entry(direct_url="")))

    def test_a_proprietary_logger_binary_is_left_where_it_is(self):
        """A corpus is not improved by being larger. `ScanReport.live_data`
        models live data, so a CSV of it is a parser target; this application
        will never own a decoder for `.xrk`."""
        binary = self.entry(parser_scope="live_data", format="xrk")
        self.assertFalse(manifest.wanted(binary))
        self.assertTrue(manifest.wanted(binary, everything=True))
        self.assertTrue(manifest.wanted(self.entry(parser_scope="live_data", format="csv")))
