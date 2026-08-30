"""
Capture a scan report as redacted word geometry (SPEC §8.3a, NFR-S-5).

The parser never wants a PDF. It wants words: text, position, color. Capturing
exactly that and committing the capture instead of the report gives a test
corpus that is real — every wrapped label, every status line that renders above
its own row — while nothing identifying a vehicle enters the repository.

    python -m homeautoshop.scantools.capture            # whole corpus
    python -m homeautoshop.scantools.capture one.pdf    # one report

**Redaction is a rule, not a list.** The first version of this file kept a
mapping of real value to replacement, which put four real VINs into a committed
source file — caught by the guard in `tests.py`, which is the entire argument
for having one. Nothing here stores an original. Values are detected by shape
and rewritten deterministically, so a new sample is protected the moment it is
captured rather than when somebody remembers to add it.

What is rewritten:

* **VINs** — anything VIN-shaped whose ISO 3779 check digit validates. The
  manufacturer, model descriptor, model year and plant survive, because that is
  what makes a sample representative and it is shared with millions of vehicles;
  the serial, which is the part that identifies one car, is replaced by a
  digest of the original so re-capturing is stable, and the check digit is
  recomputed so the result is a usable VIN.
* **The tool's serial number**, which identifies a person's scan tool.

The make and model remain visible — they are in the file names and the report
bodies, and a corpus that hid them would not exercise make-specific parsing.
What no longer exists anywhere is a pointer to a specific vehicle.
"""

from __future__ import annotations

import hashlib
import json
import pathlib
import re

from homeautoshop.assets import vin as vinlib

from .fixtures import CORPUS

VIN_SHAPED = re.compile(r"\b[A-HJ-NPR-Z0-9]{17}\b")
TOOL_SERIAL = re.compile(r"\bD8[-‑]\d{6}\b")

# Synthetic VINs produced so far, so the redaction guard can tell a deliberate
# stand-in from a real vehicle. Only replacements are ever written here.
MANIFEST = CORPUS / "synthetic-vins.json"


def synthesise_vin(real: str) -> str:
    """A valid stand-in that keeps everything except which car it is."""
    digest = hashlib.sha256(real.encode("utf-8")).hexdigest()
    serial = f"{int(digest[:12], 16) % 1_000_000:06d}"
    body = real[:8] + "0" + real[9:11] + serial
    return body[:8] + vinlib.check_digit(body) + body[9:]


def redact(text: str) -> tuple[str, set[str]]:
    """Rewrite anything identifying. Returns the text and the stand-ins used."""
    produced: set[str] = set()

    def swap_vin(match: re.Match) -> str:
        candidate = match.group(0)
        # A real VIN satisfies its check digit; an arbitrary 17 characters of
        # part number or calibration ID does not, and must not be mangled.
        if not vinlib.validate(candidate).check_digit_valid:
            return candidate
        replacement = synthesise_vin(candidate)
        produced.add(replacement)
        return replacement

    text = VIN_SHAPED.sub(swap_vin, text)
    text = TOOL_SERIAL.sub(lambda m: m.group(0)[:3] + "000000", text)
    return text, produced


def capture(pdf_path: pathlib.Path) -> tuple[dict, set[str]]:
    """Read a report into the plain structure the parser consumes."""
    from .xtool_d8 import words_from_pdf

    produced: set[str] = set()
    pages = []
    for page in words_from_pdf(str(pdf_path)):
        row = []
        for word in page:
            text, made = redact(word["text"])
            produced |= made
            row.append(
                {
                    "text": text,
                    "x0": round(float(word["x0"]), 2),
                    "x1": round(float(word["x1"]), 2),
                    "top": round(float(word["top"]), 2),
                    "color": _color(word.get("color")),
                }
            )
        pages.append(row)
    return {"source": pdf_path.name, "pages": pages}, produced


def _color(value):
    if value is None:
        return None
    try:
        return [round(float(channel), 4) for channel in value]
    except TypeError:
        return [round(float(value), 4)]


def capture_path(pdf_path: pathlib.Path) -> pathlib.Path:
    return CORPUS / (pdf_path.stem + ".words.json")


def synthetic_vins() -> set[str]:
    if not MANIFEST.exists():
        return set()
    return set(json.loads(MANIFEST.read_text(encoding="utf-8")))


def write(pdf_path: pathlib.Path) -> tuple[pathlib.Path, set[str]]:
    data, produced = capture(pdf_path)
    target = capture_path(pdf_path)
    target.write_text(json.dumps(data, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    return target, produced


def verify_synthetic_vins() -> list[str]:
    """A stand-in that fails validation would weaken the corpus silently."""
    problems = []
    for candidate in sorted(synthetic_vins()):
        check = vinlib.validate(candidate)
        if not check.is_well_formed:
            problems.append(f"{candidate} is not a well-formed VIN")
        elif not check.check_digit_valid:
            problems.append(f"{candidate} check digit should be {vinlib.check_digit(candidate)}")
    return problems


if __name__ == "__main__":  # pragma: no cover - developer tool
    import sys

    targets = [pathlib.Path(a) for a in sys.argv[1:] if a.endswith(".pdf")]
    targets = targets or sorted(CORPUS.glob("*.pdf"))
    if not targets:
        print(f"no PDFs found in {CORPUS}")
        raise SystemExit(1)

    everything = synthetic_vins()
    for pdf in targets:
        target, produced = write(pdf)
        everything |= produced
        print(f"captured {target.name}" + (f" (redacted {len(produced)})" if produced else ""))

    MANIFEST.write_text(
        json.dumps(sorted(everything), indent=1) + "\n", encoding="utf-8"
    )
    print(f"{len(everything)} synthetic VINs recorded in {MANIFEST.name}")
