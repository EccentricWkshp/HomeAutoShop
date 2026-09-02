"""
Transcribe a manufacturer's published trouble-code list into a bundled table.

Manufacturer-controlled codes are not published anywhere free *and*
comprehensive, so §8.3c's answer has always been "the operator types it once".
That is right for a code nobody has a list for, and needlessly bleak for one
where a list exists: a shop with a Ford in it should not have to key in
`B1352 Ignition Key-In Circuit Failure` to be told what its own scan tool just
read.

So a published list can be transcribed into `diagnostics/codelists/<make>.json`
and shipped. This command does the transcribing, and exists rather than a
one-off script for the same reason `capture_scan_samples` does: **the source
document is not in the repository** — it is somebody else's PDF or web page,
held under `Artifacts/samples/dtc-lists/`, which is git-ignored — so the only
way to check a committed table against what it came from is to be able to run
the transcription again.

Nothing is reworded. A manufacturer's phrasing is the fact being recorded, and
"improving" it would turn a lookup into a guess.

**It checks a new list against the ones already held**, and this is the part
that earns its keep. Multi-make compilations circulate widely and are mostly
one make's list with the attribution filed off: a "Duramax" troubleshooting
manual turned out to be 670 Ford codes, 611 of them word for word, and the
most-starred OBD code dataset on GitHub is the same Ford document served as
make-agnostic. Shipping either would put Ford's definitions against a Chevrolet
— the exact failure that scoping definitions by make exists to prevent. So the
overlap is measured and reported, loudly, rather than left for somebody to
notice by eye.

    python manage.py build_dtc_list "Artifacts/samples/dtc-lists/Ford.pdf" \\
        --make Ford --alias Lincoln --alias Mercury \\
        --source "Ford Motor Company Group — Master List of DTC Codes"
"""

from __future__ import annotations

import html as htmllib
import json
import re
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from homeautoshop.diagnostics import dtc, transcription

#: A code, whitespace, then the rest of the printed line. Anchored at the start
#: because a description may legitimately contain something code-shaped — "see
#: P0300" — and that is a mention, not a row.
LINE = re.compile(r"^([PBCU][0-9A-F]{4})\s+(\S.*?)\s*$", re.I)

#: Exactly a code and nothing else, for a cell in a table.
CELL = re.compile(r"^([PBCU][0-9A-F]{4})$", re.I)

#: Lines that only look like rows. A structure primer explaining that `P0xxx`
#: means a government code is not a definition of the code `P0xxx`.
NOISE = re.compile(r"^(?:[PBCU]xxxx|P[0-9]xxx|Px[0-9]xx)\b", re.I)

#: Overlap alone is not plagiarism, and the numbers say where the line is.
#: Brands under one corporate parent genuinely share modules and therefore
#: codes, but each writes its own text: Chevrolet and Buick share 427 codes and
#: word only 227 of them identically, Audi and VW 74%, Lincoln and Mercury 76%.
#: What identity means is *one document under two names* — Kenworth and
#: Peterbilt are 100%, Citroen and Peugeot 100%, and a "Duramax" manual was 91%
#: Ford. So a high share is worth saying out loud, and near-identity is worth
#: refusing.
COPY_THRESHOLD = 0.9
SHARED_THRESHOLD = 0.6


class Command(BaseCommand):
    help = "Transcribe a published DTC list (PDF, HTML or text) into a bundled table."

    def add_arguments(self, parser):
        parser.add_argument("source_file", help="The PDF, HTML or text file to read.")
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
            "--precedence",
            type=int,
            default=0,
            help=(
                "Higher wins where two documents cover one make and define one "
                "code. One vehicle's own service manual outranks a third party's "
                "summary of the whole badge; nothing in the files says so."
            ),
        )
        parser.add_argument(
            "--column",
            type=int,
            default=1,
            help=(
                "Which cell after the code holds the definition, counting from 1. "
                "Documents disagree: one puts a category in column 1 and the "
                "definition in 2, another puts the definition in 1 and probable "
                "causes in 2. Look at the document."
            ),
        )
        parser.add_argument(
            "--scope",
            choices=["make", "iso-sae"],
            default="make",
            help=(
                "`iso-sae` for a document that is the standard's own list rather "
                "than one manufacturer's — the P/B/C/U sets. J2012 calls these "
                "codes ISO/SAE controlled. They answer for every vehicle and are "
                "matched to no make at all."
            ),
        )
        parser.add_argument(
            "--author", default="", help="Who is publishing this list, for the catalog."
        )
        parser.add_argument(
            "--out",
            default="",
            help=(
                "Where to write. A manufacturer's list defaults to "
                "catalog/codes/<make>.json, where it is published for shops to "
                "install; the standard's own sets default to "
                "diagnostics/codelists/, where they ship in the image."
            ),
        )
        parser.add_argument(
            "--dry-run", action="store_true", help="Report what it found and write nothing."
        )
        parser.add_argument(
            "--force",
            action="store_true",
            help="Write even if this looks like a copy of a list already held.",
        )

    def handle(self, *args, **options):
        path = Path(options["source_file"])
        if not path.exists():
            raise CommandError(f"No such file: {path}")

        rejected = _rejected().get(path.name)
        if rejected and not options["force"]:
            raise CommandError(
                "\n".join(
                    [
                        f"{path.name} was examined and kept out.",
                        f"  Claimed: {rejected['claimed']}",
                        f"  {rejected['evidence']}",
                        "  --force if you disagree, and say why in codelists/_rejected.json.",
                    ]
                )
            )

        codes, skipped = _read(path, column=options["column"])
        if not codes:
            raise CommandError("Nothing code-shaped in that file — is it the right document?")

        if options["scope"] == "iso-sae":
            # A document that is the standard's list has no business asserting
            # manufacturer codes. The B and U pages carry a few hundred `B1xxx`
            # and `U1xxx` with no make against them, and an unattributed
            # manufacturer definition is the one thing this design refuses.
            dropped = [c for c in codes if not dtc.parse(c)["is_iso_sae"]]
            for code in dropped:
                del codes[code]
            if dropped:
                self.stdout.write(
                    f"Dropped {len(dropped)} manufacturer-controlled codes: an "
                    "ISO/SAE list cannot say whose they are."
                )

        make = options["make"].strip()
        iso_sae_list = options["scope"] == "iso-sae"
        specific = {c: d for c, d in codes.items() if not dtc.parse(c)["is_iso_sae"]}
        self.stdout.write(
            f"{len(codes)} codes for {make} — {len(specific)} manufacturer-controlled, "
            f"{len(codes) - len(specific)} ISO/SAE"
        )
        if skipped:
            self.stdout.write(self.style.WARNING(f"{len(skipped)} lines skipped:"))
            for line in skipped[:20]:
                self.stdout.write(f"  {line}")

        # A document that *is* the standard's list shares everything with
        # everybody by definition, so the copy check has nothing to say about it.
        overlaps = [] if iso_sae_list else _overlaps(specific, own_make=make)
        copies = [o for o in overlaps if o[1] >= COPY_THRESHOLD]
        for other, share, sample in overlaps:
            if share >= COPY_THRESHOLD:
                self.stdout.write(
                    self.style.ERROR(
                        f"\n{share:.0%} of the manufacturer-controlled codes here are word "
                        f"for word the list already held for {other}. That is one document "
                        "under two names, not two brands that share parts."
                    )
                )
                self.stdout.write(
                    f"  Shipping it would put {other}'s definitions on a {make}. If they "
                    f"genuinely are one document, add {make} as an --alias of {other} "
                    "rather than writing it out twice. For example:"
                )
            else:
                self.stdout.write(
                    self.style.WARNING(
                        f"\n{share:.0%} of the manufacturer-controlled codes here match "
                        f"{other} word for word — ordinary between brands sharing modules, "
                        "worth a look if these two are unrelated. For example:"
                    )
                )
            for code, text in sample:
                self.stdout.write(f"    {code}  {text}")

        if options["dry_run"]:
            self.stdout.write("Dry run — nothing written.")
            return
        if copies and not options["force"]:
            raise CommandError("Refused. See above, and --force if you disagree.")

        out = Path(options["out"]) if options["out"] else _default_out(make, iso_sae_list)
        out.parent.mkdir(parents=True, exist_ok=True)
        aliases = [a.strip() for a in options["alias"] if a.strip()]

        if iso_sae_list:
            # The standard's sets ship in the image as one file each. They are
            # not published for installing, so they keep the flat shape — but
            # they are versioned all the same. J2012 gets revised and codes get
            # added, and "which revision is this answering from" is a question
            # worth being able to answer about a definition presented as fact.
            held = json.loads(out.read_text(encoding="utf-8")) if out.exists() else {}
            payload = {
                "make": make,
                "scope": "iso_sae",
                "version": int(held.get("version") or 0),
                "precedence": options["precedence"],
                "aliases": aliases,
                "source": options["source"].strip(),
                "codes": dict(sorted(codes.items())),
            }
            if held and {**held, "version": payload["version"]} == payload:
                note = " — unchanged, version left at %d" % payload["version"]
            else:
                payload["version"] += 1
                note = f" — version {payload['version']}"
        else:
            payload, note = _merge(
                out,
                make=make,
                aliases=aliases,
                author=options["author"].strip(),
                document={
                    "source": options["source"].strip(),
                    "precedence": options["precedence"],
                    "codes": dict(sorted(codes.items())),
                },
            )

        out.write_text(
            json.dumps(payload, indent=1, ensure_ascii=False, sort_keys=False) + "\n",
            encoding="utf-8",
        )
        self.stdout.write(
            self.style.SUCCESS(f"Wrote {out} ({out.stat().st_size // 1024} KB){note}")
        )


# --------------------------------------------------------------------------
# Reading a document
# --------------------------------------------------------------------------


def _read(path: Path, *, column: int = 1) -> tuple[dict[str, str], list[str]]:
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return _from_pdf(path, column=column)
    if suffix in (".html", ".htm"):
        text = path.read_text(encoding="utf-8", errors="replace")
        return _collect(_from_html(text, column=column))
    return _collect(
        _from_lines(path.read_text(encoding="utf-8", errors="replace").splitlines())
    )


def _from_pdf(path: Path, *, column: int = 1) -> tuple[dict[str, str], list[str]]:
    """Table structure first, printed lines second.

    A ruled two-column table is what most of these documents actually are, and
    reading it as lines truncates every description that wraps — the wrap goes
    *above* its own code as often as below, so a line reader silently drops
    half the sentence. Where a document has no table at all, its lines are
    one-per-code and read fine.
    """
    try:
        import pdfplumber
    except ImportError as exc:  # pragma: no cover - dependency is installed
        raise CommandError("pdfplumber is needed to read a PDF.") from exc

    pairs: list[tuple[str, str]] = []
    lines: list[str] = []
    with pdfplumber.open(str(path)) as pdf:
        for page in pdf.pages:
            for table in page.extract_tables() or []:
                for row in table:
                    cells = [" ".join((cell or "").split()) for cell in row]
                    pairs.extend(_pair_from_cells(cells, column=column))
            lines.extend((page.extract_text() or "").splitlines())

    if pairs:
        return _collect(pairs)
    return _collect(_from_lines(lines))


def _pair_from_cells(cells: list[str], headed: set[int] | None = None, column: int = 1):
    """One row of a table, if a cell in it is exactly a code.

    **There is no positional rule that fits every document**, which is why
    `--column` exists. One list reads *code · category · definition* and
    another reads *code · definition · probable cause*, so first-after-the-code
    is right for one and last-after-the-code for the other. Guessing put "Air &
    Fuel Metering" in the meaning column of one whole list, and would have put
    "Wiring, oil control valve, ECM" there for another.

    The default is the first cell after the code, which is the common
    convention. A cell that carried a heading still beats position: an HTML
    page puts the short name in `<strong>` with a paragraph of diagnostic
    advice under it, and the heading is the definition.
    """
    headed = headed or set()
    for index, cell in enumerate(cells):
        if not CELL.match(cell):
            continue
        after = [i for i in range(index + 1, len(cells)) if cells[i]]
        if not after:
            return
        preferred = [i for i in after if i in headed]
        if preferred:
            yield cell.upper(), cells[preferred[0]]
            return
        wanted = index + column
        yield cell.upper(), cells[wanted] if wanted in after else cells[after[0]]
        return


def _from_lines(lines: list[str]):
    for raw in lines:
        line = raw.strip()
        if not line or NOISE.match(line):
            continue
        match = LINE.match(line)
        if match:
            yield match.group(1).upper(), match.group(2).strip()
        elif re.match(r"^[PBCU][0-9]", line) and len(line) > 4:
            # Reported rather than swallowed: a truncated code in the source is
            # how a wrong description ends up against a real one.
            yield None, line


def _from_html(text: str, *, column: int = 1):
    """Rows of an HTML table.

    The description is the cell's **heading** where it has one. These pages put
    a short name in `<strong>` and a paragraph of explanation under it, and the
    paragraph is somebody's advice rather than the definition — useful reading,
    but not what belongs in a column that says what a code means.
    """
    for row in re.findall(r"<tr\b.*?</tr>", text, re.S | re.I):
        cells = re.findall(r"<t[dh]\b[^>]*>(.*?)</t[dh]>", row, re.S | re.I)
        if not cells:
            continue
        values, headed = [], set()
        for index, cell in enumerate(cells):
            strong = re.search(r"<(strong|b)\b[^>]*>(.*?)</\1>", cell, re.S | re.I)
            if strong:
                headed.add(index)
            values.append(_text(strong.group(2) if strong else cell))
        yield from _pair_from_cells(values, headed, column=column)


def _text(fragment: str) -> str:
    return " ".join(htmllib.unescape(re.sub(r"<[^>]+>", " ", fragment)).split())


def _collect(pairs) -> tuple[dict[str, str], list[str]]:
    """Every code and its description, and every line that looked like one.

    A repeat of a code wins only when it agrees with what is already held; a
    disagreement is reported, because two meanings for one code inside one
    manufacturer's own document means the reading is wrong somewhere.
    """
    codes: dict[str, str] = {}
    skipped: list[str] = []

    for code, description in pairs:
        if code is None:
            skipped.append(description)
            continue
        description = transcription.tidy(description or "")
        if not description or dtc.parse(code) is None:
            skipped.append(f"{code} {description}".strip())
            continue
        if code in codes and codes[code] != description:
            skipped.append(f"{code} {description}   (conflicts with: {codes[code]})")
            continue
        codes[code] = description

    return codes, skipped


# --------------------------------------------------------------------------
# Is this actually somebody else's list?
# --------------------------------------------------------------------------


def _normalise(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def _overlaps(specific: dict[str, str], *, own_make: str):
    """Held lists this one repeats word for word, worst first.

    Measured against the **smaller** of the two, because a 150-code page lifted
    wholesale out of a 3,000-code one is still a lifted page, and dividing by
    the larger would bury it under everything it did not copy.
    """
    findings = []
    for held in _held():
        if held.make.lower() == own_make.lower() or not specific or held.is_iso_sae:
            continue
        theirs = {c: t for c, t in held.codes.items() if not dtc.parse(c)["is_iso_sae"]}
        if not theirs:
            continue
        matching = [
            c
            for c in specific
            if c in theirs and _normalise(specific[c]) == _normalise(theirs[c])
        ]
        share = len(matching) / min(len(specific), len(theirs))
        if share >= SHARED_THRESHOLD:
            findings.append(
                (held.make, share, [(c, specific[c][:70]) for c in sorted(matching)[:3]])
            )
    return sorted(findings, key=lambda f: -f[1])


def _rejected() -> dict:
    """Documents already examined and kept out, by file name.

    The overlap check below compares against the lists *already held*, which
    makes it depend on build order — rebuilt from empty in alphabetical order,
    `Citroen` is read before `Ford` and nothing catches that it is Ford's list.
    A finding that survives only in somebody's memory of a terminal session is
    not a finding, so each one is written down where the command reads it.
    """
    from homeautoshop.diagnostics import codelists

    register = Path(codelists.__file__).parent / "_rejected.json"
    if not register.exists():
        return {}
    data = json.loads(register.read_text(encoding="utf-8"))
    return {entry["file"]: entry for entry in data.get("documents", [])}


def _held():
    """Every manufacturer document already published, read off disk.

    Off disk rather than through `dtc`, because `dtc` now answers with what
    *this instance has installed* — which in a build checkout is nothing. The
    copy check has to compare a new document against everything published, or
    it silently passes everything.

    A flat sequence rather than a mapping by make: a make can be covered by
    several documents, and collapsing them would hide all but one from the
    check that is supposed to notice repetition.
    """
    out = []
    for path in sorted(catalog_codes().glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        for document in data.get("documents", []):
            out.append(
                dtc.CodeList(
                    make=str(data.get("make") or ""),
                    source=str(document.get("source") or ""),
                    codes=document.get("codes") or {},
                    precedence=int(document.get("precedence") or 0),
                )
            )
    return out


def slug_for(make: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", make.lower()).strip("-")


def catalog_codes() -> Path:
    """Where published manufacturer lists live, for shops to install."""
    return Path(settings.BASE_DIR) / "catalog" / "codes"


def _default_out(make: str, iso_sae: bool = False) -> Path:
    if iso_sae:
        from homeautoshop.diagnostics import codelists

        return Path(codelists.__file__).parent / f"{slug_for(make)}.json"
    return catalog_codes() / f"{slug_for(make)}.json"


def _merge(out: Path, *, make: str, aliases: list[str], author: str, document: dict):
    """Fold one document into the make's published bundle.

    One file per manufacturer, holding every document that covers it, because
    a shop installs *Ford* — not three documents it is then expected to rank.
    A document already present under the same `source` is replaced rather than
    added: re-running a transcription after a fix is the ordinary case, and the
    alternative is a bundle that grows a near-duplicate every time.

    **The version is bumped only when the content actually changes**, so that it
    answers the question a browse screen asks — is what I installed behind what
    is published — rather than counting how often somebody ran the command.
    """
    held = json.loads(out.read_text(encoding="utf-8")) if out.exists() else {}
    documents = [
        d for d in held.get("documents", []) if d.get("source") != document["source"]
    ]
    documents.append(document)
    documents.sort(key=lambda d: (-int(d.get("precedence") or 0), d.get("source") or ""))

    payload = {
        "make": make,
        "aliases": aliases or held.get("aliases") or [],
        "version": int(held.get("version") or 0),
        "author": author or held.get("author") or "",
        "documents": documents,
    }
    if held and {**held, "version": payload["version"]} == payload:
        return payload, " — unchanged, version left at %d" % payload["version"]
    payload["version"] += 1
    return payload, f" — version {payload['version']}, {len(documents)} document(s)"
