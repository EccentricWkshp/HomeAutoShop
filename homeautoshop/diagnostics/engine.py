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


def read(upload, *, filename: str = "") -> Document:
    """Turn an uploaded file into a :class:`Document`.

    The media type is decided from the *content*, not the extension: a scan
    tool that names its CSV export `report.txt` is common, and refusing it on
    the strength of three characters would be silly.
    """
    name = (filename or getattr(upload, "name", "") or "").lower()
    raw = upload.read() if hasattr(upload, "read") else bytes(upload)
    if hasattr(upload, "seek"):
        upload.seek(0)

    if raw[:5] == b"%PDF-":
        return _read_pdf(raw)

    text = normalize(raw.decode("utf-8", errors="replace"))
    stripped = text.lstrip()
    if stripped[:1] in "{[":
        try:
            json.loads(text)
        except ValueError:
            pass
        else:
            return Document(text=text, media_type="json")

    if _looks_delimited(text) or name.endswith(".csv"):
        return Document(text=text, media_type="csv")
    return Document(text=text, media_type="text")


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


def _read_pdf(raw: bytes) -> Document:
    from homeautoshop.scantools.xtool_d8 import words_from_pdf

    pages = words_from_pdf(io.BytesIO(raw))
    text = "\n".join(
        " ".join(normalize(str(w.get("text", ""))) for w in page) for page in pages
    )
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


BUILTINS = {"xtool_d8": _xtool_d8}


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
            haystack = " ".join(document.metadata.values())
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
    best, best_score = None, 0.0
    for profile in profiles:
        if not profile.is_active:
            continue
        if profile.media_type != document.media_type:
            continue
        value = score(profile, document)
        threshold = float((profile.fingerprint or {}).get("threshold", 0.7))
        if value >= threshold and value > best_score:
            best, best_score = profile, value
    return best, best_score


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
    return out


def _extract_field(rule: dict, text: str) -> Field | None:
    strategy = rule.get("strategy", "regex")
    pattern = rule.get("pattern") or ""
    confidence = float(rule.get("confidence", 0.6))

    if strategy == "label_anchored":
        for label in rule.get("labels") or []:
            # The value is whatever follows the label on the same line, or the
            # profile's pattern applied to it. Anchoring on the line keeps a
            # label from reaching across the page and claiming the next field.
            anchor = re.search(
                rf"{re.escape(label)}\s*[:\-]?\s*(.+)", text, re.I
            )
            if not anchor:
                continue
            tail = anchor.group(1).strip()
            value = tail.split("\n")[0].strip()
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


def _extract_table(rule: dict, text: str) -> list[dict]:
    """Pull DTC rows out of flat text, between a heading and a stop marker."""
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
    row_pattern = rule.get("row_pattern") or r"^\s*([PBCU][0-9A-F]{4}(?:-[0-9A-F]{2})?)\s+(.*)$"
    columns = rule.get("columns") or [
        {"role": "code", "group": 1},
        {"role": "description", "group": 2},
    ]

    rows: list[dict] = []
    for line in body.splitlines():
        if any(pattern.search(line) for pattern in drop):
            continue
        match = re.search(row_pattern, line, re.I)
        if not match:
            continue
        row: dict = {}
        for column in columns:
            try:
                raw = (match.group(int(column.get("group", 1))) or "").strip()
            except (IndexError, ValueError):
                continue
            mapping = column.get("map") or {}
            role = column.get("role", "description")
            row[role] = mapping.get(raw, raw)
        if row.get("code"):
            row.setdefault("state", "stored")
            rows.append(row)
    return rows


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
