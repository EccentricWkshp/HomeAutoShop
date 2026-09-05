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

    python -m homeautoshop.scantools.capture      # reports -> redacted captures
    python -m homeautoshop.scantools.fixtures --write   # captures -> fixtures

A photograph is captured the same way and needs Tesseract, so run that first
command in the container:

    docker compose run --rm app python -m homeautoshop.scantools.capture <file>.jpg

Never by hand. A capture records how it was read and the suite refuses an image
capture that does not say `ocr` — a transcription is a record of what somebody
imagined OCR does, and every hard case in a photographed format is a case where
OCR does something surprising.

Regenerate deliberately and read the diff. A fixture updated without looking is
a test that has been switched off.
"""

from __future__ import annotations

import json
import pathlib

CORPUS = pathlib.Path(__file__).resolve().parents[2] / "Artifacts" / "samples" / "scan-reports"

#: A capture is word geometry or it is text, and the name says which. Both are
#: `.json`, so a reader can open either; the difference is what the parser for
#: that tool needs. A PDF whose layout carries meaning in color and position
#: keeps its words; an auto-scan log keeps its text, because text is the whole
#: of what a declarative profile reads.
CAPTURE_SUFFIX = ".words.json"
TEXT_SUFFIX = ".text.json"
CAPTURE_SUFFIXES = (CAPTURE_SUFFIX, TEXT_SUFFIX)
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
    found = [path for suffix in CAPTURE_SUFFIXES for path in CORPUS.rglob("*" + suffix)]
    return sorted(found)


def kind(capture: pathlib.Path) -> str:
    """`words` or `text` — what this capture holds."""
    return "text" if capture.name.endswith(TEXT_SUFFIX) else "words"


def stem(capture: pathlib.Path) -> str:
    for suffix in CAPTURE_SUFFIXES:
        if capture.name.endswith(suffix):
            return capture.name[: -len(suffix)]
    return capture.stem


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
    """The word geometry in a capture. Empty for a text capture, which has none."""
    return json.loads(capture.read_text(encoding="utf-8")).get("pages") or []


def text(capture: pathlib.Path) -> str:
    """A capture as text, whichever kind it is.

    Word geometry is turned into text the way `engine._read_pdf` does it — by
    the *same function*, so a profile scored against a capture and the same
    profile scored against the PDF it came from see the same string. They did
    not, once: `build_catalog` built its own document and reached a different
    answer from the import screen for the same report, which is the kind of
    disagreement that makes a badge meaningless.
    """
    from homeautoshop.diagnostics.engine import lines_from_words, normalize

    data = json.loads(capture.read_text(encoding="utf-8"))
    if "text" in data:
        return normalize(data["text"])
    return "\n".join(lines_from_words(data.get("pages") or []))


def lines(capture: pathlib.Path) -> list[str]:
    """A capture as the lines it was printed as.

    The same thing :func:`text` returns, split — which it was not, once. This
    reconstructed lines while `text` flattened a page into one string, so the
    redaction audit and the parsers disagreed about the shape of the page and
    the audit reported the *absence* of a name as a name. Now the parsers read
    the printed lines too, and there is one answer to what a page says.
    """
    return text(capture).splitlines()


def media_type(capture: pathlib.Path) -> str:
    """What the original was, so a profile is matched against its own format."""
    data = json.loads(capture.read_text(encoding="utf-8"))
    return data.get("media_type") or "pdf"


def image_size(capture: pathlib.Path) -> list:
    """The frame a photograph's boxes are measured in, if it was one."""
    return json.loads(capture.read_text(encoding="utf-8")).get("image_size") or []


def document(capture: pathlib.Path):
    """A capture as the :class:`engine.Document` the parsers actually take."""
    from homeautoshop.diagnostics import engine

    return engine.Document(
        text=text(capture),
        pages=pages(capture),
        media_type=media_type(capture),
        metadata={"image_size": image_size(capture)} if image_size(capture) else {},
    )


#: Tool folders whose reports a **built-in** parser reads, and the fixture
#: shape that parser produces. Only one format has ever needed this and the
#: reason is in `engine.py`: the D8 draws its section boundaries in color and
#: prints a cell's first line above its own row, neither of which survives text
#: extraction. Every other tool in the corpus is read by a declarative profile.
#:
#: Keyed on the folder rather than fingerprinted, because the folder is already
#: the corpus's answer to *what produced this* — and because a fixture that
#: silently changed shape when a profile's score moved would be a regression
#: test that rewrites its own expectations.
BUILT_IN_PARSERS = {
    "xtool d8": "xtool_d8",
    "topdon bt600 plus": "topdon_bt600_plus",
}


def _xtool_d8(capture: pathlib.Path) -> dict:
    from .xtool_d8 import parse_pages

    return parse_pages(pages(capture)).to_dict()


def _topdon_bt600_plus(capture: pathlib.Path) -> dict:
    from .topdon_bt600_plus import parse_pages

    return parse_pages(pages(capture), image_size(capture)).to_dict()


#: The parser behind each name in :data:`BUILT_IN_PARSERS`. A table rather than
#: a chain of `if`s because the previous version simply named the XTOOL parser
#: inline — which meant the second built-in format would have been parsed by
#: the first one's rules and its fixture would have recorded the result as
#: correct.
BUILDERS = {"xtool_d8": _xtool_d8, "topdon_bt600_plus": _topdon_bt600_plus}


def build(capture: pathlib.Path) -> dict:
    """The expected output for a capture, from whatever reads its tool.

    Several shapes, because there are several kinds of parser and each is
    recorded as what it actually produces. The XTOOL parser yields a whole
    :class:`report.ScanReport` — vehicle, modules, codes, live data. The TOPDON
    battery tester yields a :class:`report.TesterReport`, which is a list of
    results rather than a list of codes. A declarative profile yields an
    extraction: named fields with confidence, and a code table.

    Flattening them into one shape would mean throwing away most of each to
    match the smallest.
    """
    builder = BUILDERS.get(BUILT_IN_PARSERS.get(tool(capture), ""))
    if builder is not None:
        return builder(capture)
    return extraction(capture)


def extraction(capture: pathlib.Path) -> dict:
    """What the best-scoring available profile makes of a capture."""
    from homeautoshop.diagnostics import engine
    from homeautoshop.diagnostics import profiles as profilelib

    doc = document(capture)
    profile, score = engine.detect(profilelib.available(), doc)
    if profile is None:
        # Recorded rather than raised. A capture nobody can read yet is a
        # legitimate thing to have — it is how a corpus gets contributed before
        # the profile that reads it does — and the fixture saying so out loud
        # is what turns "somebody should write this" into a visible gap.
        return {"profile": "", "score": 0.0, "unread": True}

    found = engine.apply(profile, doc)
    return {
        "profile": profile.name,
        "score": round(score, 2),
        "fields": found.as_dict(),
        "codes": found.codes,
        "live_data": found.live_data,
        "warnings": [str(w) for w in found.warnings],
    }


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
    import os
    import sys

    # Set up Django before touching anything. The header advertises this as a
    # command to run, and it has not been one: every capture that a declarative
    # profile reads goes through `engine`, which imports the DTC table, which
    # is lazily translated — so the first fixture built raised
    # `ImproperlyConfigured` from four frames deep in gettext.
    import django

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    django.setup()

    if "--write" in sys.argv:
        print(f"wrote {write_all()} fixtures to {CORPUS}")
    else:
        print(f"{len(samples())} captured samples in {CORPUS}; pass --write to regenerate")
