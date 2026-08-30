"""
Golden fixtures for the scan-report corpus (SCHEMA-PARSER-PROFILES.md §2).

The corpus is **captured word geometry**, not the original PDFs. `capture.py`
explains why: the parser wants words rather than a document, so committing the
words keeps a real test corpus — the wrapped labels, the status line that
renders above its own row — while nothing that identifies a vehicle ever
reaches the repository.

Each `<name>.words.json` has a `<name>.expected.json` beside it, and the suite
fails if the parser stops reproducing one. The point is regression, not proof:
a fixture records what the parser did on a day someone checked, so a profile
change that quietly breaks a two-year-old report cannot reach a release.

    python -m homeautoshop.scantools.capture      # PDFs  -> redacted captures
    python -m homeautoshop.scantools.fixtures --write   # captures -> fixtures

Regenerate deliberately and read the diff. A fixture updated without looking is
a test that has been switched off.
"""

from __future__ import annotations

import json
import pathlib

CORPUS = pathlib.Path(__file__).resolve().parents[2] / "Artifacts" / "samples" / "scan-reports"

CAPTURE_SUFFIX = ".words.json"
FIXTURE_SUFFIX = ".expected.json"


def samples() -> list[pathlib.Path]:
    """Every captured report in the corpus."""
    return sorted(CORPUS.glob("*" + CAPTURE_SUFFIX))


def stem(capture: pathlib.Path) -> str:
    return capture.name[: -len(CAPTURE_SUFFIX)]


def fixture_path(capture: pathlib.Path) -> pathlib.Path:
    return CORPUS / (stem(capture) + FIXTURE_SUFFIX)


def pages(capture: pathlib.Path) -> list[list[dict]]:
    return json.loads(capture.read_text(encoding="utf-8"))["pages"]


def build(capture: pathlib.Path) -> dict:
    from .xtool_d8 import parse_pages

    return parse_pages(pages(capture)).to_dict()


def load(capture: pathlib.Path) -> dict:
    return json.loads(fixture_path(capture).read_text(encoding="utf-8"))


def write_all() -> int:
    written = 0
    for capture in samples():
        fixture_path(capture).write_text(
            json.dumps(build(capture), indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        written += 1
    return written


if __name__ == "__main__":  # pragma: no cover - developer tool
    import sys

    if "--write" in sys.argv:
        print(f"wrote {write_all()} fixtures to {CORPUS}")
    else:
        print(f"{len(samples())} captured samples in {CORPUS}; pass --write to regenerate")
