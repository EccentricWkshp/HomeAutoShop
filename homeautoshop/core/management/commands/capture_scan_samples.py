"""Turn fetched scan-tool samples into redacted corpus captures (§8.3a, FR-INT-7).

`fetch_scan_samples` pulls other people's reports off the public web into
`<tool>/originals/`, which git never sees. This is the half that produces what
*does* ship: a redacted capture per report, and the expected output beside it.

    python manage.py capture_scan_samples
    python manage.py capture_scan_samples --tool "ross-tech vcds"
    python manage.py capture_scan_samples --audit

The redaction is `scantools/capture.py` and it is a rule rather than a list —
VINs by check digit *and* by label, tool serials, workshop codes, licence
plates, e-mail addresses. It is not, and cannot be, complete: it does not know
that a technician signed page four. So this ends by **auditing what it wrote**
and printing everything that still looks like somebody's details, and
`--audit` re-runs that pass alone over the committed corpus.

Read the audit before committing. A capture is going somewhere public and
permanent, and a corpus is not worth one stranger's phone number.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

#: What a capture should never contain, checked after redaction rather than
#: trusted to it. Every one of these has been seen in a scan report: the
#: THINKCAR pre-scans carry a workshop's contact block, and the forum-posted
#: reports carry whoever posted them.
#: `[ \t]*` after every label, never `\s*`. A label whose value is empty is
#: followed by a newline and then the next label, and a pattern that crosses
#: the line break reports `License Plate: Mileage` as a plate — ten findings,
#: all of them the absence of the thing being looked for. An audit that cries
#: wolf is an audit somebody stops reading, which costs more than not having
#: one.
SUSPICIOUS = (
    ("an e-mail address", re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")),
    (
        # Punctuation-separated only. Three numbers spaced apart are the tick
        # labels on a BimmerLink graph far more often than they are a phone
        # number, and `500 750 1000` is exactly 3-3-4.
        "a phone number",
        re.compile(r"(?<![\d.-])(?:\+\d{1,3}[ .-]?)?(?:\(\d{3}\)[ .-]?|\d{3}[.-])\d{3}[.-]\d{4}(?!\d)"),
    ),
    (
        "a street address",
        re.compile(
            r"(?i)\b\d{1,5}\s+[A-Z][a-z]+\s+"
            r"(?:St|Street|Rd|Road|Ave|Avenue|Blvd|Dr|Drive|Lane|Ln|Way)\b"
        ),
    ),
    ("a labelled plate", re.compile(r"(?i)licen[cs]e[ \t]*plate[ \t]*[:#][ \t]*[A-Z0-9]{2,}")),
    (
        # The trailing lookahead is what stops an *empty* field being reported.
        # Autel prints `Customer name:` and `Technician:` next to each other
        # with nothing between them, and a name-shaped word followed by its own
        # colon is the next label, not somebody called Technician.
        "a labelled person",
        re.compile(
            r"(?i)(?:customer|owner|client|technician|operator|user)[ \t]*(?:name)?"
            r"[ \t]*[:：#][ \t]*[A-Za-z]{2,}(?![A-Za-z]*[:：])"
        ),
    ),
)


class Command(BaseCommand):
    help = "Capture the fetched public samples into the corpus, redacted."

    def add_arguments(self, parser):
        parser.add_argument(
            "--tool", default="", help="Only this tool folder (substring match)."
        )
        parser.add_argument(
            "--refresh",
            action="store_true",
            help="Re-capture reports that already have a capture.",
        )
        parser.add_argument(
            "--audit",
            action="store_true",
            help="Only re-check the committed captures for missed identifiers.",
        )
        parser.add_argument(
            "--fixtures",
            action="store_true",
            default=True,
            help="Write the expected output beside each capture (default).",
        )
        parser.add_argument(
            "--no-fixtures",
            action="store_false",
            dest="fixtures",
            help="Capture only. Useful before any profile reads the format.",
        )

    def handle(self, *args, **options):
        from homeautoshop.scantools import capture as capturelib
        from homeautoshop.scantools import fixtures, manifest

        if options["audit"]:
            return self._audit(fixtures.samples(), fixtures)

        sources = sorted(manifest.CORPUS.glob(f"*/{manifest.ORIGINALS}/*"))
        if needle := options["tool"].lower().strip():
            sources = [p for p in sources if needle in p.parent.parent.name.lower()]
        if not sources:
            raise CommandError(
                "No fetched samples found. Run `manage.py fetch_scan_samples` first."
            )

        skip = _skipped(manifest.CORPUS)
        kept = [p for p in sources if _key(p) not in skip]
        if len(kept) < len(sources):
            self.stdout.write(
                f"Skipping {len(sources) - len(kept)} file(s) listed in "
                f"{SKIP_FILE} — see the reasons there."
            )
        sources = kept

        captured, skipped, refused = [], 0, []
        produced: set[str] = set()
        for source in sources:
            tool = source.parent.parent.name
            target = capturelib.capture_path(source, tool)
            if target.exists() and not options["refresh"]:
                skipped += 1
                captured.append(target)
                continue
            try:
                written, made = capturelib.write(source, tool)
            except Exception as exc:  # noqa: BLE001 - a bad sample is not fatal
                refused.append(f"{tool}/{source.name}: {exc}")
                continue
            produced |= made
            captured.append(written)
            self.stdout.write(f"  + {tool}/{written.name}")

        if produced:
            total = capturelib.remember(produced)
            self.stdout.write(
                f"Replaced {len(produced)} VIN(s) with synthetic stand-ins "
                f"({total} recorded in {capturelib.MANIFEST.name})."
            )

        for problem in refused:
            self.stderr.write(self.style.WARNING(f"  ! {problem}"))

        self.stdout.write(
            self.style.SUCCESS(
                f"{len(captured)} capture(s) in the corpus — "
                f"{len(captured) - skipped} written, {skipped} already there, "
                f"{len(refused)} unreadable."
            )
        )

        if options["fixtures"]:
            self._fixtures(captured, fixtures)
        self._audit(captured, fixtures)

    # -- fixtures --------------------------------------------------------

    def _fixtures(self, captures, fixtures) -> None:
        written = unread = 0
        for capture in captures:
            try:
                expected = fixtures.build(capture)
            except Exception as exc:  # noqa: BLE001
                self.stderr.write(self.style.WARNING(f"  ! {capture.name}: {exc}"))
                continue
            if expected.get("unread"):
                unread += 1
            fixtures.fixture_path(capture).write_text(
                json.dumps(expected, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            written += 1
        self.stdout.write(
            f"Wrote {written} expected-output file(s); {unread} report(s) "
            f"no profile reads yet."
        )

    # -- audit -----------------------------------------------------------

    def _audit(self, captures, fixtures) -> None:
        """Look at what was written, for the things the rules cannot know.

        Redaction handles shapes it can name. A person's name in a header is
        not a shape. So this is the second pass the corpus README asks a human
        for, done first by a program so the human is reading a short list
        rather than eleven thousand words of geometry.
        """
        findings: list[str] = []
        for capture in captures:
            try:
                printed = "\n".join(fixtures.lines(capture))
            except (OSError, ValueError):
                continue
            for what, pattern in SUSPICIOUS:
                for found in sorted(set(pattern.findall(printed)))[:3]:
                    findings.append(f"{_short(capture)}: {what} — {found!r}")

        if not findings:
            self.stdout.write(
                self.style.SUCCESS(
                    f"Audited {len(captures)} capture(s): nothing that looks "
                    f"like somebody's details. Still read the diff."
                )
            )
            return

        for finding in findings:
            self.stderr.write(self.style.WARNING(f"  ? {finding}"))
        self.stderr.write(
            self.style.ERROR(
                f"{len(findings)} thing(s) in {len(captures)} capture(s) look "
                f"identifying. Redact them or delete the capture — the "
                f"originals stay on this machine either way, so nothing is "
                f"lost by not publishing one."
            )
        )


def _short(path: Path) -> str:
    return f"{path.parent.name}/{path.name}"


#: Files fetched into the corpus that are deliberately not captured, with the
#: reason beside each. A list rather than a rule, because "this is a picture of
#: a graph" and "this is the vendor's training slides" are judgments a person
#: makes about a particular file, not properties a program can test for.
SKIP_FILE = "not-captured.json"


def _key(path: Path) -> str:
    return f"{path.parent.parent.name}/{path.parent.name}/{path.name}"


def _skipped(corpus: Path) -> set[str]:
    import json as _json

    target = corpus / SKIP_FILE
    if not target.exists():
        return set()
    try:
        return set(_json.loads(target.read_text(encoding="utf-8")).get("skip") or {})
    except (OSError, ValueError):
        return set()
