"""
The parser-profile engine (SPEC §8.3a, FR-INT-4..7).

Given a document and the profiles on file, this answers two questions: *which
tool wrote this*, and *what does it say*. Both answers carry a score, because
both are guesses and a review screen that cannot show its own uncertainty is
just an auto-commit with extra clicks.

**Two kinds of profile, and the reason for it.** A declarative profile is data:
a scored fingerprint, label-anchored or regex field extractors, and a table
extractor, all evaluated here. That is the design the spec asks for and it
covers the formats where labels and values survive text extraction in the order
a human sees them.

The XTOOL D8 does not. Its section boundaries are *colors*, its labels come
out after their values, and a cell's first line can render above its own row.
No regex over extracted text can recover any of that, so the D8 profile names a
built-in parser instead. The alternative — a profile language expressive enough
to describe color-banded positional layout — is a programming language with
worse tooling. The profile row still exists, still versions, and still records
itself on every session it reads, so re-parse and regression triage work the
same either way.

Nothing here writes to the database. The engine turns bytes into an
:class:`Extraction`; deciding what to keep is the review screen's job.
"""

from __future__ import annotations

import csv
import io
import json
import re
from dataclasses import dataclass, field
from datetime import datetime

from django.utils.translation import gettext_lazy as _

from homeautoshop.assets import vin as vinlib

from . import dtc

# Characters that look like ASCII and are not. Normalized once, on the way in,
# because a pattern written with an ASCII hyphen silently misses U+2011 and the
# failure looks like "the report has no codes".
TRANSLATIONS = str.maketrans({"‑": "-", "–": "-", "—": "-", " ": " "})


def normalize(text: str) -> str:
    return (text or "").translate(TRANSLATIONS)


@dataclass(slots=True)
class Field:
    """One extracted value, and enough provenance to point at it on screen."""

    value: str = ""
    confidence: float = 0.0
    page: int = 0
    label: str = ""

    def as_dict(self) -> dict:
        return {
            "value": self.value,
            "confidence": round(self.confidence, 2),
            "page": self.page,
            "label": self.label,
        }


@dataclass(slots=True)
class Extraction:
    """What a profile made of a document. Nothing here is committed."""

    fields: dict[str, Field] = field(default_factory=dict)
    codes: list[dict] = field(default_factory=list)
    live_data: list[dict] = field(default_factory=list)
    readiness: dict = field(default_factory=dict)
    #: Whole results from a bench tester — see `scantools/report.py`. A scan
    #: tool answers *what is wrong* and fills `codes`; a battery tester answers
    #: *what did this measure* and prints its answer once per test, with its own
    #: verdict and its own clock each time. One photograph of a BT600 Plus
    #: printout holds a cranking test and a charging test taken forty seconds
    #: apart, so a flat `{field: value}` dictionary would have to throw one of
    #: the two timestamps and one of the two voltages away.
    test_results: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def value(self, name: str, default: str = "") -> str:
        found = self.fields.get(name)
        return found.value if found else default

    def as_dict(self) -> dict:
        return {name: f.as_dict() for name, f in self.fields.items()}


# --------------------------------------------------------------------------
# Document handling
# --------------------------------------------------------------------------


@dataclass(slots=True)
class Document:
    """A report reduced to what every extraction strategy needs.

    `pages` is word geometry where the source had any (PDF), empty otherwise.
    `text` is always present, so a declarative profile works on a CSV export
    and a PDF alike.
    """

    text: str = ""
    pages: list[list[dict]] = field(default_factory=list)
    media_type: str = "text"
    metadata: dict = field(default_factory=dict)

    @property
    def page_count(self) -> int:
        return len(self.pages) or 1


def media_type(raw: bytes, *, filename: str = "") -> str:
    """What a file is, decided from its *content* rather than its extension.

    A scan tool that names its CSV export `report.txt` is common, and refusing
    it on the strength of three characters would be silly. The extension only
    breaks a tie: a single-column CSV has no commas to count, so `.csv` on the
    name is the only evidence there is.

    Separate from :func:`read` because the corpus fetcher needs the answer
    without paying for the document — reading a PDF means parsing it and
    reading an image means OCR, and neither is worth doing to fill in a field
    in a provenance record.
    """
    if raw[:5] == b"%PDF-":
        return "pdf"
    if _is_image(raw):
        return "image"
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return "binary"

    if text.lstrip()[:1] in "{[":
        try:
            json.loads(text)
        except ValueError:
            pass
        else:
            return "json"

    if _looks_delimited(text) or (filename or "").lower().endswith((".csv", ".tsv")):
        return "csv"
    return "text"


def read(upload, *, filename: str = "") -> Document:
    """Turn an uploaded file into a :class:`Document`."""
    name = (filename or getattr(upload, "name", "") or "").lower()
    raw = upload.read() if hasattr(upload, "read") else bytes(upload)
    if hasattr(upload, "seek"):
        upload.seek(0)

    kind = media_type(raw, filename=name)
    if kind == "pdf":
        return _read_pdf(raw)
    if kind == "image":
        return _read_image(raw)

    text = normalize(raw.decode("utf-8", errors="replace"))
    # Bytes that are not valid UTF-8 are still shown to the operator rather
    # than refused — a mis-encoded export is readable enough to review, and
    # `text` is the honest name for what is left after replacement.
    return Document(text=text, media_type="text" if kind == "binary" else kind)


#: Magic bytes, because the extension is not to be trusted here either — a
#: phone hands over `image.jpg` for a HEIC often enough to matter.
IMAGE_SIGNATURES = (
    b"\xff\xd8\xff",       # JPEG
    b"\x89PNG\r\n\x1a\n",  # PNG
    b"GIF8",                 # GIF
    b"BM",                   # BMP
)


def _is_image(raw: bytes) -> bool:
    if raw.startswith(IMAGE_SIGNATURES):
        return True
    # RIFF containers name their type at byte 8; HEIC and HEIF, which is what
    # an iPhone actually hands over, name theirs at 4.
    return (raw[:4] == b"RIFF" and raw[8:12] == b"WEBP") or raw[4:8] == b"ftyp"


def _read_image(raw: bytes) -> Document:
    """Read a photographed report (§7.9, FR-DOC-5).

    Not every tool prints to a file. A battery tester prints a paper slip, and
    a compression tester prints nothing at all — what the operator has is a
    photo taken with the phone already in their hand, which is precisely the
    case OCR was specified for and the one the import would not accept: the
    JPEG was decoded as UTF-8 and handed to the parsers as line noise.

    OCR runs inline here rather than on the queue, unlike an uploaded document.
    The difference is that this one has somebody standing in front of it
    waiting to review what was read, and a review screen that appears empty and
    fills in later is worse than a request that takes two seconds.

    **The words are kept, not only the text.** A photograph of a printout has
    exactly the property that made the D8 need a positional parser: labels in
    one column and values in another, meaningful only together. That geometry
    was recognized, used to produce a string, and thrown away — so an image was
    the one format where a built-in parser had nothing to stand on.
    """
    from homeautoshop.mediafiles.services import image_size, read_image_words

    pages = read_image_words(raw)
    text = normalize("\n".join(lines_from_words(pages)))
    return Document(
        text=text,
        pages=pages,
        media_type="image",
        metadata={"image_size": list(image_size(raw))},
    )


def _looks_delimited(text: str) -> bool:
    """A header row plus at least one data row, with a consistent field count.

    Two lines that both happen to contain a comma are a sentence, not a table,
    so the column count has to agree before this claims CSV.
    """
    lines = [line for line in text.splitlines() if line.strip()][:10]
    if len(lines) < 2:
        return False
    counts = {line.count(",") for line in lines}
    return len(counts) == 1 and counts.pop() >= 2


#: Tops within this far apart were printed on the same line — in a PDF, where
#: the units are points and a line of body text is ten of them. A photograph's
#: geometry is pixels and a printed line is sixty of them tall, so the constant
#: is scaled by :func:`_line_tolerance` wherever the words say how tall they
#: are. Left as the floor rather than replaced: a PDF's word geometry carries
#: no height, and two points is the right answer there.
LINE_TOLERANCE = 2.0


def _line_tolerance(page: list[dict]) -> tuple[float, bool]:
    """How far apart two words can be and still share a line, and whether the
    page carries real glyph heights — which is also whether its word order
    means anything.
    """
    heights = sorted(
        height
        for word in page
        if (height := float(word.get("bottom", 0) or 0) - float(word.get("top", 0) or 0)) > 0
    )
    if not heights:
        return LINE_TOLERANCE, False
    return max(LINE_TOLERANCE, 0.6 * heights[len(heights) // 2]), True


def _row_of(word: dict) -> float:
    """A word's vertical middle, which is what shares a line with its neighbour.

    Not its top. `HEALTH:` and `79%` are printed on one line by a tester whose
    two glyph heights differ, and on a photograph of that line the tops differ
    by more than the `%` is tall — so grouping by top splits a label from its
    own value, which is the only thing on the receipt that matters.
    """
    top = float(word.get("top", 0) or 0)
    return (top + float(word.get("bottom", top) or top)) / 2.0


def lines_from_words(pages: list[list[dict]]) -> list[str]:
    """Word geometry read back as the lines it was **printed** as.

    Extraction order is not layout. A PDF hands its words over in whatever
    order they were written into the content stream, and joining them that way
    produced one meaningless line per page — which is what every profile here
    was reading, and what made three formats unparseable:

        Curren  EOBD/OBD II P0A80 Replace Hybrid/EV Battery Pack  t

    That is a THINKCAR report whose status column wraps. Reconstructed by
    position it is `EOBD/OBD II P0A80 Replace Hybrid/EV Battery Pack`, with the
    module, the code and the description in the order a person reads them.

    It also stops a label reaching across the page for a value. An Autel report
    that prints `VIN: --` was giving up a VIN — the first seventeen characters
    of the repair-order number, found much further down a page that had been
    flattened into a single line. A wrong VIN is worse than no VIN: it is the
    one misreading that poisons the vehicle record silently.

    Sorted by position rather than trusted: the words arrive in extraction
    order, so a row assembled by appending would be scrambled left to right as
    well as top to bottom.
    """
    out: list[str] = []
    for page in pages:
        tolerance, measured = _line_tolerance(page)
        rows: list[list[dict]] = []
        row_at: float | None = None
        for word in sorted(page, key=lambda w: (_row_of(w), float(w.get("x0", 0)))):
            here = _row_of(word)
            if row_at is None or abs(here - row_at) > tolerance:
                rows.append([])
                row_at = here
            rows[-1].append(word)
        for row in rows:
            if measured:
                # Left to right, but only for geometry that was *measured* —
                # which in practice means OCR. Grouping walks the page top to
                # bottom, so words arrive in the order their lines start, and
                # that is reading order only when every word on a line shares a
                # top. On a photograph none of them do: `BATTERY TEST` came out
                # as `TEST "BATTERY`, costing the profile the section heading
                # it fingerprints on.
                #
                # A PDF is deliberately left alone, and this is a compatibility
                # decision rather than a claim that it is right. It is not
                # right — a Toyota report prints a module's fault count in a
                # narrow column to the *right* of its name and one point
                # higher, so extraction order gives `2 EOBD/OBD II` where the
                # page says `EOBD/OBD II 2`. But the catalog's section patterns
                # were written against what this function returns, and two of
                # them stop finding their headings when it changes: nine
                # modules' worth of code attribution on two real reports,
                # silently, plus four summary rows that then look like data
                # stream readings. Fixing it means new profile versions, which
                # is its own change. Recorded in SPEC §19.
                row.sort(key=lambda w: float(w.get("x0", 0)))
            out.append(" ".join(normalize(str(word.get("text", ""))) for word in row))
    return out


def _read_pdf(raw: bytes) -> Document:
    from homeautoshop.scantools.xtool_d8 import words_from_pdf

    pages = words_from_pdf(io.BytesIO(raw))
    text = "\n".join(lines_from_words(pages))
    if not text.strip():
        # An image-only PDF — a scanner's output, or a tool that renders its
        # report as a picture. §7.9 always promised the OCR fallback here; the
        # word geometry is gone either way, so a profile matches on text alone.
        from homeautoshop.mediafiles.services import read_pdf_text_by_ocr

        text = normalize(read_pdf_text_by_ocr(raw))
    metadata: dict = {}
    try:  # pragma: no cover - metadata is absent on every sample we have
        import pdfplumber

        with pdfplumber.open(io.BytesIO(raw)) as pdf:
            metadata = {str(k): str(v) for k, v in (pdf.metadata or {}).items()}
    except Exception:
        metadata = {}
    return Document(text=text, pages=pages, media_type="pdf", metadata=metadata)


# --------------------------------------------------------------------------
# Built-in parsers, for formats a regex cannot read
# --------------------------------------------------------------------------


def _xtool_d8(document: Document) -> Extraction:
    from homeautoshop.scantools import xtool_d8

    report = xtool_d8.parse_pages(document.pages)
    out = Extraction(warnings=list(report.warnings))

    def put(name: str, value, confidence: float, label: str = "") -> None:
        if value in (None, ""):
            return
        out.fields[name] = Field(
            value=str(value), confidence=confidence, page=1, label=label or name
        )

    # A VIN that passes its own check digit is worth more than one that does
    # not, and the review screen shows the difference rather than averaging it
    # away — a transposed VIN is the single most expensive misread here.
    vin = report.vehicle.vin
    put("vin", vin, 0.95 if vin and _vin_ok(vin) else 0.5, "VIN")
    put("odometer", report.vehicle.odometer, 0.9, "Mileage")
    put("odometer_unit", report.vehicle.odometer_unit, 0.9, "Unit")
    put("vehicle_description", report.vehicle.name, 0.8, "Vehicle")
    put("model_year", report.vehicle.year, 0.8, "Year")
    put("tool_vendor", report.tool.vendor, 1.0, "Tool")
    put("tool_model", report.tool.model, 1.0, "Model")
    put("tool_serial", report.tool.serial, 0.9, "Serial")
    if report.generated_at:
        put("performed_on", report.generated_at.isoformat(), 0.9, "Date")

    for code in report.dtcs:
        out.codes.append(
            {
                "code": code.code,
                "description": code.description,
                "module": code.module,
                "state_raw": code.status_raw,
                "state": code.status or "stored",
            }
        )
    out.live_data = [
        {
            "name": d.name,
            "value": d.value,
            "unit": d.unit,
            "minimum": d.minimum,
            "maximum": d.maximum,
            "module": d.module,
        }
        for d in report.live_data
    ]
    out.fields["ecu"] = Field(
        value=json.dumps(report.ecu), confidence=0.9, page=1, label=str(_("ECU identifiers"))
    )
    return out


def _topdon_bt600_plus(document: Document) -> Extraction:
    """A battery tester's paper, read off a photograph of it.

    Only four scalars come out of here, and that is the point. The readings
    live in `test_results`, whole, because they belong to a test rather than to
    the session — a photograph holding a cranking test and a charging test has
    two voltages and neither of them is *the* voltage.

    What is scalar is what is true of the encounter: which tool, whose serial,
    and when the last test on the strip finished. Those are the fields the
    confirm form edits, and they are the ones a session has room for.
    """
    from homeautoshop.scantools import topdon_bt600_plus as bt600

    report = (
        bt600.parse_pages(document.pages, document.metadata.get("image_size") or ())
        if document.pages
        else bt600.parse_text(document.text)
    )
    out = Extraction(warnings=list(report.warnings))
    # With the frame the boxes are measured in. Without it every box is a
    # rectangle in unknown units and the review screen can show no crop —
    # which is exactly what happened, silently, because a box with no page
    # beside it still looks like a perfectly good box.
    out.test_results = [result.to_dict(report.page) for result in report.results]

    def put(name: str, value, confidence: float, label: str) -> None:
        if value in (None, ""):
            return
        out.fields[name] = Field(
            value=str(value), confidence=confidence, page=1, label=label
        )

    put("tool_vendor", report.tool.vendor, 1.0, str(_("Tool")))
    put("tool_model", report.tool.model, 1.0, str(_("Model")))
    put("tool_serial", report.tool.serial, 0.8, str(_("Serial")))

    when = report.performed_on
    if when is not None:
        # Carried at the confidence of the receipt it came off, not at the
        # parser's. The one sample with a damaged timestamp is damaged on the
        # paper, and a review screen that showed it as certain would be lying
        # about the only value on the page nobody can check twice.
        latest = max(
            (r for r in report.results if r.when == when),
            key=lambda r: r.performed_on.confidence,
        )
        put(
            "performed_on",
            when.isoformat(),
            latest.performed_on.confidence,
            str(_("Date")),
        )
    return out


BUILTINS = {"xtool_d8": _xtool_d8, "topdon_bt600_plus": _topdon_bt600_plus}


# --------------------------------------------------------------------------
# Fingerprinting
# --------------------------------------------------------------------------


def score(profile, document: Document) -> float:
    """How well a profile claims this document, from 0 to 1.

    Scored over the whole document rather than page one: the D8's disclaimer —
    its strongest single signal — is not on the first page of four reports in
    nine.
    """
    fingerprint = profile.fingerprint or {}
    signals = fingerprint.get("signals") or []
    if not signals:
        return 0.0

    total = sum(float(s.get("weight", 0)) for s in signals) or 1.0
    hit = 0.0
    for signal in signals:
        kind = signal.get("kind", "doc_text")
        pattern = signal.get("pattern", "")
        if not pattern:
            continue
        haystack = document.text
        if kind == "pdf_metadata":
            # Stringified, because metadata is no longer only a PDF's own
            # `/Producer` strings: a photograph carries the size of the frame
            # its boxes are measured in, which is a list.
            haystack = " ".join(str(v) for v in document.metadata.values())
        elif kind == "page_text":
            haystack = document.text.split("\n", 1)[0]
        try:
            if re.search(pattern, haystack, re.I | re.S):
                hit += float(signal.get("weight", 0))
        except re.error:
            continue
    return min(1.0, hit / total)


def detect(profiles, document: Document) -> tuple[object | None, float]:
    """Pick the best-scoring active profile above its own threshold.

    Each profile carries its own threshold because a format with one strong
    signal and a format with four weak ones need different bars, and a single
    global number would either admit junk or reject the D8.
    """
    best, best_rank = None, ()
    best_score = 0.0
    for profile in profiles:
        if not profile.is_active:
            continue
        if profile.media_type != document.media_type:
            continue
        value = score(profile, document)
        threshold = float((profile.fingerprint or {}).get("threshold", 0.7))
        if value < threshold:
            continue
        rank = (_names_a_tool(profile), value, bool(profile.tool_model))
        if rank > best_rank:
            best, best_rank, best_score = profile, rank, value
    return best, best_score


def _names_a_tool(profile) -> bool:
    """Whether this profile is *for* a tool, or is a fallback for anything.

    Ranked above the score, and that ordering decides real imports rather than
    being tidiness. `Generic code list` claims any text with a trouble code in
    it — deliberately; that is what generic means — and it scores **1.0** on a
    VCDS Auto-Scan, where the VCDS profile scores 0.85 because the older Beta
    builds omit one of its four signals. Score alone therefore handed three
    real Auto-Scans to the fallback, which read *nothing* out of reports
    holding 61, 14 and 0 faults.

    A fallback is not in competition with a profile written for the hardware.
    It is what happens when nothing else recognizes the file, so it only wins
    when nothing else clears its own threshold — and clearing its own threshold
    is exactly the claim a profile's author makes. Between two profiles that
    both name a tool, the score decides as before.
    """
    return bool(profile.tool_vendor or profile.tool_model)


# --------------------------------------------------------------------------
# Declarative extraction
# --------------------------------------------------------------------------

def _vin_ok(value: str) -> bool:
    """Well-formed *and* the check digit agrees.

    Deliberately stricter than the vehicle form, which accepts a failed check
    digit with a warning because gray-market imports legitimately fail it. Here
    the answer only feeds a confidence score, and a VIN a tool misread is far
    more likely than a gray-market import in a scan report.
    """
    checked = vinlib.validate(value)
    return checked.is_well_formed and checked.check_digit_valid


VALIDATORS = {
    "vin_check_digit": _vin_ok,
    "dtc_format": lambda value: dtc.parse(value) is not None,
}


def apply(profile, document: Document) -> Extraction:
    """Run a profile over a document."""
    if profile is not None and profile.engine:
        builtin = BUILTINS.get(profile.engine)
        if builtin is None:
            return Extraction(
                warnings=[
                    str(
                        _("%(name)s names a built-in parser that is not in this build.")
                        % {"name": profile.engine}
                    )
                ]
            )
        return builtin(document)
    return _declarative(profile, document)


def _declarative(profile, document: Document) -> Extraction:
    out = Extraction()
    text = normalize(document.text)

    for name, rule in (profile.field_extractors or {}).items():
        found = _extract_field(rule, text)
        if found is not None:
            out.fields[name] = found

    table = profile.table_extractor or {}
    if table:
        out.codes = _extract_table(table, text)

    live = profile.live_data_extractor or {}
    if live:
        out.live_data = _extract_live_data(live, text)
    return out


def _extract_field(rule: dict, text: str) -> Field | None:
    strategy = rule.get("strategy", "regex")
    pattern = rule.get("pattern") or ""
    confidence = float(rule.get("confidence", 0.6))

    if strategy == "label_anchored":
        for label in rule.get("labels") or []:
            # **Every** occurrence of the label, not the first. A BlueDriver
            # report captions its header `VIN retrieved from Vehicle` and
            # prints `VIN: <vin>` on the next line, so stopping at the first
            # match found a label with no value after it and reported the
            # report as having no VIN. Cheap to try them all, and a label that
            # appears twice with a value only under the second is ordinary.
            for anchor in re.finditer(
                rf"{re.escape(label)}\s*[:：\-]?\s*(.+)", text, re.I
            ):
                # The value is whatever follows the label on the same line, or
                # the profile's pattern applied to it. Anchoring on the line
                # keeps a label from reaching across the page and claiming the
                # next field.
                # `(.+)` cannot cross a newline, so the tail *is* the rest of
                # the label's line — which is only a real limit now that a PDF
                # is read as the lines it was printed as rather than one line
                # per page.
                tail = anchor.group(1).strip()
                value = tail
                if pattern:
                    inner = re.search(pattern, tail, re.I)
                    if not inner:
                        continue
                    value = (inner.group(1) if inner.groups() else inner.group(0)).strip()
                if not value:
                    continue
                return _coerced(rule, value, confidence, label)
        return None

    if not pattern:
        return None
    match = re.search(pattern, text, re.I | re.M)
    if not match:
        return None
    value = (match.group(1) if match.groups() else match.group(0)).strip()
    return _coerced(rule, value, confidence, rule.get("label", ""))


def _coerced(rule: dict, value: str, confidence: float, label: str) -> Field | None:
    """Apply the profile's validator and coercion, adjusting confidence.

    A failed validator does not drop the value — it lowers the confidence and
    lets the review screen show it. Discarding a VIN because its check digit
    failed would hide the one thing the operator most needs to see: that the
    tool printed something wrong.
    """
    validator = VALIDATORS.get(rule.get("validate", ""))
    if validator is not None:
        confidence = min(confidence + 0.3, 1.0) if validator(value) else confidence * 0.4

    coerce = rule.get("coerce") or {}
    kind = coerce.get("type")
    if kind == "number":
        cleaned = re.sub(r"[^\d.]", "", value)
        value = cleaned or value
    elif kind == "datetime":
        for fmt in coerce.get("formats") or ["%Y-%m-%d", "%m/%d/%Y", "%Y-%m-%d %H:%M"]:
            try:
                value = datetime.strptime(value, fmt).isoformat()
                break
            except ValueError:
                continue
    if not value:
        return None
    return Field(value=value, confidence=confidence, label=label)


#: What a live-data row must carry to be a row at all. A reading with no name
#: is a number on a page.
LIVE_DEFAULT_PATTERN = r"(?m)^(.+?)\s+(-?[\d.,]+)\s*([^\s\d]{0,12})\s*$"


def _extract_live_data(rule: dict, text: str) -> list[dict]:
    """Pull a data-stream table out of a report (`live_data_extractor`).

    The same machinery as the code table, keyed on `name` instead of `code`,
    because a data stream *is* a table — the difference is only which column
    makes a row worth keeping.

    Worth having because the shape is already modelled and already shown. The
    D8's built-in parser has always filled `DiagnosticSession.live_data`, and
    the session screen has always had a Reading / Value / Min / Max table for
    it; a declarative profile simply had no way to. So a THINKCAR data-stream
    report holding 159 readings and no fault codes at all imported as an empty
    session — a report whose entire content the parser could see and had
    nowhere to put.

    Not every tool prints a minimum and a maximum. The D8 does; THINKCAR does
    not. Those columns stay empty rather than being computed from a single
    sample, because a minimum equal to the reading is a claim about a range
    nobody measured.
    """
    rows = _extract_rows(rule, text, required="name", default_pattern=LIVE_DEFAULT_PATTERN)
    for row in rows:
        row.pop("state", None)
    return rows


def _extract_table(rule: dict, text: str) -> list[dict]:
    """Pull DTC rows out of flat text, between a heading and a stop marker."""
    return _extract_rows(rule, text, required="code")


def _extract_rows(
    rule: dict, text: str, *, required: str, default_pattern: str = ""
) -> list[dict]:
    locate = rule.get("locate") or {}
    body = text
    for heading in locate.get("headings") or []:
        found = re.search(re.escape(heading), body, re.I)
        if found:
            body = body[found.end():]
            break
    for stop in locate.get("stop_at") or []:
        found = re.search(re.escape(stop), body, re.I)
        if found:
            body = body[: found.start()]
            break

    drop = [re.compile(p, re.I) for p in (rule.get("row_filters") or {}).get("drop_if_matches", [])]
    row_pattern = (
        rule.get("row_pattern")
        or default_pattern
        or r"^\s*([PBCU][0-9A-F]{4}(?:-[0-9A-F]{2})?)\s+(.*)$"
    )
    columns = rule.get("columns") or _default_columns(required)

    sections = _sections(locate.get("section_pattern"), body)

    if rule.get("multiline"):
        return _rows_from_blocks(body, row_pattern, columns, drop, sections, required)

    rows: list[dict] = []
    offset = 0
    for line in body.splitlines(keepends=True):
        stripped = line.rstrip("\r\n")
        if not any(pattern.search(stripped) for pattern in drop):
            match = re.search(row_pattern, stripped, re.I)
            if match and (row := _row_from(match, columns, required)):
                # Only where a heading was found, so a profile that declares no
                #  keeps rows exactly as they were.
                if not row.get("module") and (found := _section_at(sections, offset)):
                    row["module"] = found
                rows.append(row)
        offset += len(line)
    return rows


def _sections(pattern: str | None, body: str) -> list[tuple[int, str]]:
    """Where each section heading starts, and what it names.

    **Which module a code came from is a fact about the page, not about the
    row.** Every PDF report in the corpus prints the module as a heading above
    a group of rows — `ABS ( 1 DTC )`, `Engine`, `01 - Engine Control Module` —
    and a row-at-a-time extractor has no idea which heading it is under. So a
    nine-module all-system scan imported as one undifferentiated list, and the
    schema documented that as something a declarative profile could not do.

    It can: find the headings once, remember where each starts, and a row
    belongs to the last heading before it. `module` is still a column first —
    THINKCAR prints it *in* the row — and this only fills the gap.
    """
    if not pattern:
        return []
    try:
        found = list(re.finditer(pattern, body, re.I | re.M))
    except re.error:
        return []
    # The first group that matched, not group 1. A tool that changed its report
    # layout between firmware versions needs an alternation to name the module
    # in both — TOPDON writes `1. EGS(…) (6Fault Code)` on one tablet and
    # `01 - Engine Control Module 1` on the other — and in an alternation every
    # group but one is `None`.
    return [(m.start(), name) for m in found if (name := _named_group(m))]


def _named_group(match: re.Match) -> str:
    for value in match.groups() or ():
        if value and value.strip():
            return value.strip()
    return match.group(0).strip() if not match.groups() else ""


def _section_at(sections: list[tuple[int, str]], offset: int) -> str:
    name = ""
    for start, heading in sections:
        if start > offset:
            break
        name = heading
    return name


def _default_columns(required: str) -> list[dict]:
    if required == "name":
        return [
            {"role": "name", "group": 1},
            {"role": "value", "group": 2},
            {"role": "unit", "group": 3},
        ]
    return [{"role": "code", "group": 1}, {"role": "description", "group": 2}]


def _rows_from_blocks(body, row_pattern, columns, drop, sections, required="code") -> list[dict]:
    """One row per *fault*, where a fault is printed across several lines.

    A VCDS Auto-Scan states a fault as a block: Ross-Tech's own five- to
    eight-digit fault number and a description on one line, and — for the
    controllers that have one — the J2012 code underneath it:

        000772 - Cylinder 4
                       P0304 - 000 - Misfire Detected - Intermittent

    A line-at-a-time reader has to pick one of those two lines and is wrong
    either way: take the first and the J2012 code is lost, take the second and
    five faults in six vanish, because across nine real Auto-Scans only 30 of
    191 faults carry a J2012 code at all. Neither is a parser being clever
    about a quirk — it is a format that states one fact on two lines, which is
    common enough to be worth a mode rather than a special case.
    """
    rows: list[dict] = []
    for match in re.finditer(row_pattern, body, re.I | re.M):
        if any(pattern.search(match.group(0)) for pattern in drop):
            continue
        if row := _row_from(match, columns, required):
            if not row.get("module") and (found := _section_at(sections, match.start())):
                row["module"] = found
            rows.append(row)
    return rows


def _row_from(match: re.Match, columns: list[dict], required: str = "code") -> dict | None:
    """One row, or nothing if the column that makes it a row did not match.

     is which column that is:  for a fault table,  for a
    data stream. A reading with no name is a number on a page, exactly as a
    description with no code is a sentence.
    """
    row: dict = {}
    for column in columns:
        raw = _first_group(match, column.get("group", 1), column.get("join"))
        if raw is None:
            continue
        role = column.get("role", "description")
        value = _mapped(column, raw)
        if value is not None:
            row[role] = value
    if not row.get(required):
        return None
    row.setdefault("state", "stored")
    return row


def _mapped(column: dict, raw: str) -> str | None:
    """A column's value, confined to its vocabulary if it declares one.

    **A `map` is a closed list, not a set of shortcuts.** It used to pass an
    unrecognized value straight through, which is fine until the column it
    fills is `state` — a four-value field twelve characters wide. Car Scanner
    reports a status as the DTC status bits in prose, so `Confirmed, Test
    failed since last DTC clear, Warning indicator requested` went in as a
    state, sixty characters into a field that has four legal values.

    Unrecognized now yields nothing, so the row's own default stands and the
    tool's wording survives where it belongs — in `state_raw`, which is
    deliberately not mapped. `map_default` names a different fallback for a
    profile that has a better answer than the default.
    """
    mapping = column.get("map") or {}
    if not mapping:
        return raw
    if raw in mapping:
        return mapping[raw]
    lowered = {str(k).lower(): v for k, v in mapping.items()}
    if raw.lower() in lowered:
        return lowered[raw.lower()]
    return column.get("map_default")


def _first_group(match: re.Match, group, join: str | None = None) -> str | None:
    """A column's raw text, from one capture group or several.

    Without `join`, the **first** group that matched wins, so a column can have
    a fallback. A fault block names its code in the vendor's vocabulary and,
    where one exists, in J2012's; which is *available* varies per controller. A
    column that says `group: [3, 1]` — prefer the J2012 code, fall back to the
    vendor's — reads that correctly, where a single group has to be wrong for
    one of the two shapes.

    With `join`, they are concatenated, which is how a **wrapped cell** is put
    back together. TOPDON prints every fault as exactly two lines with the
    description split across both and the status column split with it:

        CF1461 No message (diagnosis OBD engine, 0x397): Receiver EGS, Fault currently
        transmitter DME/DDE                                                    present

    The halves are two groups of one match. Taking only the first would publish
    a profile that truncates every description it reads, which is worse than
    one that admits it cannot read the format.
    """
    parts = []
    for index in group if isinstance(group, (list, tuple)) else [group]:
        try:
            value = match.group(int(index))
        except (IndexError, ValueError):
            continue
        if value and value.strip():
            if join is None:
                return value.strip()
            parts.append(value.strip())
    return join.join(parts) if parts else None


# --------------------------------------------------------------------------
# Structured file import (§8.3b) — the cheap path, offered first when it exists
# --------------------------------------------------------------------------

#: Column names real tools actually emit, lowercased. A mapping wizard still
#: exists for everything else; this just means the common case needs no wizard.
CSV_ALIASES = {
    "code": {"code", "dtc", "trouble code", "fault code", "dtc code"},
    "description": {"description", "fault", "meaning", "definition", "detail"},
    "state": {"state", "status", "type", "code status"},
    "module": {"module", "system", "ecu", "unit"},
}


def sniff_columns(header: list[str], rows: list[dict] | None = None) -> dict[str, str]:
    """Guess a column mapping. Never final — the wizard shows it to be corrected.

    **Content beats the header name.** A column whose values are trouble codes
    is the code column whatever it is called, and that is what makes the guess
    right for a tool nobody has ever aliased. Header names are the tie-break,
    not the evidence: "Fault" is the code on one tool and the description on the
    next, and a name-only guess has to be wrong for one of them.
    """
    mapping: dict[str, str] = {}
    rows = rows or []

    if rows:
        for role, looks_right in (("code", _is_code), ("state", _is_state)):
            best, best_hits = "", 0
            for column in header:
                values = [str(row.get(column, "")).strip() for row in rows[:20]]
                hits = sum(1 for value in values if value and looks_right(value))
                if hits > best_hits:
                    best, best_hits = column, hits
            # A majority, not a single match: one stray cell that happens to
            # look like a code does not make a column the code column.
            if best and best_hits >= max(1, len([r for r in rows[:20] if r]) // 2):
                mapping[role] = best

    taken = set(mapping.values())
    for column in header:
        key = (column or "").strip().lower()
        for role, names in CSV_ALIASES.items():
            if key in names and role not in mapping and column not in taken:
                mapping[role] = column
                taken.add(column)
    return mapping


def _is_code(value: str) -> bool:
    return dtc.parse(value) is not None


def _is_state(value: str) -> bool:
    return value.strip().lower() in STATE_WORDS


def rows_from_csv(text: str) -> tuple[list[str], list[dict]]:
    reader = csv.reader(io.StringIO(text))
    rows = [row for row in reader if any(cell.strip() for cell in row)]
    if not rows:
        return [], []
    header, body = rows[0], rows[1:]
    return header, [dict(zip(header, row)) for row in body]


def codes_from_mapping(rows: list[dict], mapping: dict[str, str]) -> list[dict]:
    """Turn mapped rows into code dicts, dropping anything not code-shaped.

    A header row that got read as data, or a trailing "no codes found" line,
    both fail :func:`dtc.parse` and are silently skipped — an import that
    invented a code called "Code" would be worse than one that skipped a row.
    """
    out: list[dict] = []
    for row in rows:
        code = (row.get(mapping.get("code", ""), "") or "").strip()
        if not dtc.parse(code):
            continue
        out.append(
            {
                "code": dtc.normalize(code),
                "description": (row.get(mapping.get("description", ""), "") or "").strip(),
                "module": (row.get(mapping.get("module", ""), "") or "").strip(),
                "state_raw": (row.get(mapping.get("state", ""), "") or "").strip(),
                "state": _state_from(row.get(mapping.get("state", ""), "")),
            }
        )
    return out


STATE_WORDS = {
    "stored": "stored",
    "current": "stored",
    "confirmed": "stored",
    "active": "stored",
    "pending": "pending",
    "permanent": "permanent",
    "history": "history",
    "historical": "history",
    "past": "history",
}


def _state_from(raw: str) -> str:
    words = (raw or "").strip().lower()
    for word, state in STATE_WORDS.items():
        if word in words:
            return state
    return "stored"
