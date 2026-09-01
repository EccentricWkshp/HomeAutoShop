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
    """Every captured report in the corpus, whichever tool folder it is in.

    The corpus is filed one folder per tool — `xtool d8/`, and whatever comes
    next — because a flat pile of reports stops saying which scanner produced
    them the moment there is a second one. The same convention the parts-order
    corpus already uses.

    Walked rather than globbed for that reason: a flat `glob` found nothing at
    all once the folders appeared, and found it silently, because every test
    that uses the corpus skips politely when it is absent.
    """
    return sorted(CORPUS.rglob("*" + CAPTURE_SUFFIX))


def stem(capture: pathlib.Path) -> str:
    return capture.name[: -len(CAPTURE_SUFFIX)]


def fixture_path(capture: pathlib.Path) -> pathlib.Path:
    """The expected output for a capture, which lives beside it.

    Beside, not at the corpus root: the two halves of a fixture belong in the
    same tool folder, and a reviewer reading one should not have to go looking
    for the other.
    """
    return capture.parent / (stem(capture) + FIXTURE_SUFFIX)


def find(name: str) -> pathlib.Path:
    """A corpus file by its bare name, wherever it is filed.

    Callers used to join `CORPUS / name` and got a path that simply did not
    exist once the reports moved into per-tool folders. One resolver means the
    next reorganization is one function rather than a hunt.
    """
    for path in sorted(CORPUS.rglob(name)):
        return path
    raise FileNotFoundError(f"{name} is not in {CORPUS}")


def tool(capture: pathlib.Path) -> str:
    """Which scanner a capture came from, read off the folder it is filed in.

    The folder is the answer rather than anything inside the file: a report
    that a parser cannot yet read still has to say what produced it, which is
    the whole reason somebody contributed it.
    """
    return capture.parent.name if capture.parent != CORPUS else ""


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
