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

from . import capture, fixtures
from .report import ScanReport
from .xtool_d8 import looks_like_xtool_d8, parse_pages

SAMPLES = fixtures.samples()


@lru_cache(maxsize=None)
def report_for(name: str) -> ScanReport:
    """Parsing is not cheap and most tests here read the whole corpus."""
    return parse_pages(fixtures.pages(fixtures.CORPUS / name))


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


@unittest.skipIf(not SAMPLES, "no captured samples in the corpus")
class InvariantTests(unittest.TestCase):
    """Claims checkable without trusting the parser."""

    def test_every_vin_passes_its_own_check_digit(self):
        """A mis-read VIN almost never satisfies the ISO 3779 check digit."""
        for sample in SAMPLES:
            with self.subTest(report=sample.name):
                found = report_for(sample.name).vehicle.vin
                check = vinlib.validate(found)
                self.assertTrue(check.is_well_formed, found)
                self.assertTrue(check.check_digit_valid, found)

    def test_the_timestamp_agrees_with_the_filename(self):
        """The tool names its own files `<vehicle><YYYYMMDDHHMM>`."""
        for sample in SAMPLES:
            with self.subTest(report=sample.name):
                stamp = re.search(r"(\d{12})$", fixtures.stem(sample))
                self.assertIsNotNone(stamp, "unexpected filename shape")
                generated = report_for(sample.name).generated_at
                self.assertIsNotNone(generated)
                self.assertEqual(generated.strftime("%Y%m%d%H%M"), stamp.group(1))

    def test_nothing_is_left_unrecognized(self):
        for sample in SAMPLES:
            with self.subTest(report=sample.name):
                self.assertEqual(report_for(sample.name).warnings, [])

    def test_the_tools_own_hyphens_never_survive(self):
        """U+2011 renders as a hyphen and matches none of the usual patterns."""
        for sample in SAMPLES:
            with self.subTest(report=sample.name):
                self.assertNotIn("‑", repr(report_for(sample.name).to_dict()))

    def test_a_report_identifies_itself(self):
        for sample in SAMPLES:
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

VIN_SHAPED = re.compile(r"\b[A-HJ-NPR-Z0-9]{17}\b")


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
        for path in REPO.rglob("*"):
            if not path.is_file() or path.suffix.lower() not in SCANNED_SUFFIXES:
                continue
            if any(part in SKIP_DIRECTORIES for part in path.parts):
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            for candidate in set(VIN_SHAPED.findall(text)):
                if candidate in ALLOWED_VINS:
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
                    if vinlib.validate(found).check_digit_valid:
                        self.assertIn(found, ALLOWED_VINS, f"{found} is not a stand-in")


class ShapeTests(unittest.TestCase):
    def test_a_report_serializes_to_plain_json_types(self):
        import json

        json.dumps(ScanReport().to_dict())
