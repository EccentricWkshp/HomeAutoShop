"""
Transcribe a manufacturer's published trouble-code list into a bundled table.

Manufacturer-specific codes are not published anywhere free *and*
comprehensive, so §8.3c's answer has always been "the operator types it once".
That is right for a code nobody has a list for, and needlessly bleak for one
where a list exists: a shop with a Ford in it should not have to key in
`B1352 Ignition Key-In Circuit Failure` to be told what its own scan tool just
read.

So a published list can be transcribed into `diagnostics/codelists/<make>.json`
and shipped. This command does the transcribing, and exists rather than a
one-off script for the same reason `capture_scan_samples` does: **the source
document is not in the repository** — it is somebody else's PDF, held under
`Artifacts/samples/dtc-lists/`, which is git-ignored — so the only way to check
a committed table against what it came from is to be able to run the
transcription again.

It is deliberately dumb. A line is a code and a description or it is skipped,
every code is validated against the same `CODE_RE` the parser uses, and what
was skipped is printed rather than swallowed. Nothing is reworded: a
manufacturer's phrasing is the fact being recorded, and "improving" it would
turn a lookup into a guess.

    python manage.py build_dtc_list "Artifacts/samples/dtc-lists/Ford.pdf" \\
        --make Ford --alias Lincoln --alias Mercury \\
        --source "Ford Motor Company Group — Master List of DTC Codes"
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from homeautoshop.diagnostics import dtc

#: A code, whitespace, then everything else on the printed line. Anchored at
#: the start because a description may legitimately contain something
#: code-shaped — "see P0300" — and that is a mention, not a row.
ROW = re.compile(r"^([PBCU][0-9A-F]{4})\s+(\S.*?)\s*$", re.I)

#: Lines that only look like rows. A page header repeated 58 times is not a
#: code, and neither is the structure primer on the first page.
NOISE = re.compile(r"^(?:[PBCU]xxxx|P[0-9]xxx|Px[0-9]xx)\b", re.I)


class Command(BaseCommand):
    help = "Transcribe a published DTC list (PDF or text) into a bundled table."

    def add_arguments(self, parser):
        parser.add_argument("source_file", help="The PDF or text file to read.")
        parser.add_argument("--make", required=True, help="The make these codes belong to.")
        parser.add_argument(
            "--alias",
            action="append",
            default=[],
            help=(
                "Another make this list also covers. Repeatable. Ford's list is "
                "the Ford Motor Company Group's, so Lincoln and Mercury read it too."
            ),
        )
        parser.add_argument("--source", default="", help="Where the document came from.")
        parser.add_argument(
            "--out",
            default="",
            help="Where to write. Defaults to diagnostics/codelists/<make>.json.",
        )
        parser.add_argument(
            "--dry-run", action="store_true", help="Report what it found and write nothing."
        )

    def handle(self, *args, **options):
        path = Path(options["source_file"])
        if not path.exists():
            raise CommandError(f"No such file: {path}")

        lines = _lines(path)
        codes, skipped = _rows(lines)
        if not codes:
            raise CommandError("Nothing code-shaped in that file — is it the right document?")

        make = options["make"].strip()
        payload = {
            "make": make,
            "aliases": [a.strip() for a in options["alias"] if a.strip()],
            "source": options["source"].strip(),
            "codes": dict(sorted(codes.items())),
        }

        specific = sum(1 for c in codes if not dtc.parse(c)["is_generic"])
        self.stdout.write(
            f"{len(codes)} codes for {make} — {specific} manufacturer-specific, "
            f"{len(codes) - specific} in the generic range"
        )
        if skipped:
            # Printed, not swallowed. A truncated code in the source PDF is how
            # a wrong description ends up against a real code.
            self.stdout.write(self.style.WARNING(f"{len(skipped)} lines skipped:"))
            for line in skipped[:20]:
                self.stdout.write(f"  {line}")

        if options["dry_run"]:
            self.stdout.write("Dry run — nothing written.")
            return

        out = Path(options["out"]) if options["out"] else _default_out(make)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            json.dumps(payload, indent=1, ensure_ascii=False, sort_keys=False) + "\n",
            encoding="utf-8",
        )
        self.stdout.write(self.style.SUCCESS(f"Wrote {out} ({out.stat().st_size // 1024} KB)"))


def _default_out(make: str) -> Path:
    from homeautoshop.diagnostics import codelists

    slug = re.sub(r"[^a-z0-9]+", "-", make.lower()).strip("-")
    return Path(codelists.__file__).parent / f"{slug}.json"


def _lines(path: Path) -> list[str]:
    if path.suffix.lower() != ".pdf":
        return path.read_text(encoding="utf-8", errors="replace").splitlines()

    try:
        import pdfplumber
    except ImportError as exc:  # pragma: no cover - dependency is installed
        raise CommandError("pdfplumber is needed to read a PDF.") from exc

    out: list[str] = []
    with pdfplumber.open(str(path)) as pdf:
        for page in pdf.pages:
            out.extend((page.extract_text() or "").splitlines())
    return out


def _rows(lines: list[str]) -> tuple[dict[str, str], list[str]]:
    """Every code line, and every line that looked like one and was not.

    A later repeat of a code wins over an earlier one only when they agree;
    a disagreement is reported, because two different meanings for one code in
    one manufacturer's own document means the reading is wrong somewhere.
    """
    codes: dict[str, str] = {}
    skipped: list[str] = []

    for raw in lines:
        line = raw.strip()
        if not line or NOISE.match(line):
            continue
        match = ROW.match(line)
        if not match:
            # Only worth reporting if it starts like a code — the rest is
            # page furniture nobody wants listed.
            if re.match(r"^[PBCU][0-9]", line) and len(line) > 4:
                skipped.append(line)
            continue

        code, description = match.group(1).upper(), match.group(2).strip()
        if dtc.parse(code) is None:
            skipped.append(line)
            continue
        if code in codes and codes[code] != description:
            skipped.append(f"{line}   (conflicts with: {codes[code]})")
            continue
        codes[code] = description

    return codes, skipped
