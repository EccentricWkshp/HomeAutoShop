"""
XTOOL D8 PDF report parser (SPEC §8.3a).

Written against nine real reports covering five vehicles and three model years
of the tool's own software, in `Artifacts/samples/scan-reports`.

**Why this is positional rather than line-based.** The reports read cleanly by
eye, and `extract_text()` returns something quite different: the footer arrives
before the header, and every field's label lands after its value. Working from
that ordering produces a parser that appears to work on one file and silently
mis-associates on the next. Word coordinates say what the page actually shows.

The layout, once seen, is simple. Sections are blue banners down the left, and
a table belongs to **the banner immediately above it**. A report that lists nine
modules and prints two code tables is telling you seven modules were clean —
not that the codes belong to all nine.

Three things bite anyone writing a profile for this format:

* **Non-breaking hyphens (U+2011)** in dates, model names and DTC suffixes, so
  `B1352-20` typed with an ASCII hyphen never matches. Text is normalized once,
  on the way in.
* **`0mile` means "not read"**, not zero. Treated as a real odometer reading it
  would drag every mileage-based interval on the vehicle back to zero.
* **DTC codes carry a failure-type byte** — `B1352-20` — which the usual
  `[PBCU][0-9A-F]{4}` pattern rejects outright.

The constants below are deliberately declarative and grouped at the top: they
are the profile, and lifting them into the YAML of SCHEMA-PARSER-PROFILES.md is
mechanical once a second tool's format exists to generalize against. Inventing
that abstraction from a single format would only encode this one's accidents.
"""

from __future__ import annotations

import re
from datetime import datetime

from .report import Dtc, LiveDatum, ScanReport, Tool, Vehicle

# --------------------------------------------------------------------------
# The profile
# --------------------------------------------------------------------------

VENDOR = "XTOOL"
MODEL = "D8"

# Characters the tool emits that look like ASCII but are not.
TRANSLATIONS = str.maketrans({
    "‑": "-",   # non-breaking hyphen: dates, model names, DTC suffixes
    "–": "-",   # en dash
    "—": "-",   # em dash
    " ": " ",   # non-breaking space
    "→": ">",   # arrow, in diagnosis routes
})

# Banner text that names a section rather than a module.
SECTION_HEADINGS = {
    "Vehicle Information",
    "Trouble Code",
    "Live Data",
    "ECU",
    "Freeze Frame",
    "Version Information",
    "Action Test",
}

FOOTER_MARKERS = ("Remark:", "Company:", "Address:", "Telephone:")

# Banners are white text on a blue gradient; section headings are dark blue;
# field labels are gray. Color separates them exactly, where position does not:
# "Mileage :" and the "4X4" banner start at the same x, and reading the left
# margin as a module list turned every wrapped label into a phantom module.
#
# The gradient itself is an image rather than a rectangle, so there is nothing
# to hit-test against — but the ink is unambiguous.
def _is_white(color) -> bool:
    return bool(color) and all(channel > 0.9 for channel in color)


def _is_heading_blue(color) -> bool:
    if not color or len(color) != 3:
        return False
    red, green, blue = color
    return blue > 0.35 and blue > red + 0.15 and green < 0.45

# Measured against the corpus, not guessed at. Two of the three signals written
# before a sample existed were wrong: the disclaimer is not always on page one,
# and "Diagnosis Route" never survives text extraction at all — its two words
# are drawn far enough apart that nothing joins them. Score the whole document.
FINGERPRINT = (
    re.compile(r"This report is only responsible", re.IGNORECASE),
    re.compile(r"\bD8-\d{6}\b"),
    re.compile(r"Vehicle\s+Information", re.IGNORECASE),
    re.compile(r"Mileage\s*[:：]", re.IGNORECASE),
)

# Ford/Mazda continuous-memory and on-demand codes, plus the plain forms.
STATUS_MAP = {
    "cmdtcs(storage trouble code)": "stored",
    "oddtcs(request trouble code)": "current",
    "history": "history",
    "current": "current",
    "stored": "stored",
    "pending": "pending",
    "permanent": "permanent",
}

# P219A is a real code: the four characters after the letter are hex-ish, not
# decimal. Requiring digits silently dropped every such code while the report
# still looked parsed - the corpus caught it, one report in nine.
DTC_CODE = re.compile(r"^[PBCU][0-9A-F]{4}(?:-[0-9A-F]{2})?$", re.IGNORECASE)

VIN_RE = re.compile(r"\bVIN\s*[:：]?\s*([A-HJ-NPR-Z0-9]{17})\b")
YEAR_RE = re.compile(r"\bYear\s*[:：]\s*(\d{4})\b")
NAME_RE = re.compile(r"Name\s*[:：]?\s*(.+?)(?=\s+Year\s*[:：]|\s+VIN\s*[:：]|$)")
MILEAGE_RE = re.compile(r"\bMileage\s*[:：]\s*([\d,]+)\s*(mile|miles|km)\b", re.IGNORECASE)
SERIAL_RE = re.compile(r"\bSN\s*[:：]\s*(\S+)")
ROUTE_RE = re.compile(r"Route\s*[:：]?\s*(.+?)\s*$")
TIMESTAMP_RE = re.compile(r"\b(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2}(?::\d{2})?)\b")

# GM prints three outcomes instead of one status.
GM_STATUS_RE = re.compile(
    r"(Last test|This ignition|Since Clear)\s*[:：]\s*([^:]+?)(?=\s*(?:Last test|This ignition|Since Clear)\s*[:：]|$)"
)

# Wrapped labels put "Vehicle" and "Name" on baselines 8pt apart, with the
# value beside them, so a tight tolerance splits a field from its own label.
# Table rows are 22-28pt apart, which leaves room to be generous here.
LINE_TOLERANCE = 9.0


# --------------------------------------------------------------------------
# Line assembly
# --------------------------------------------------------------------------


class Line:
    """One visual row of words, with the geometry needed to place it."""

    __slots__ = ("page", "top", "words", "text", "x0", "color")

    def __init__(self, page: int, words: list[dict]):
        self.page = page
        self.words = sorted(words, key=lambda w: w["x0"])
        self.top = min(w["top"] for w in words)
        self.x0 = self.words[0]["x0"]
        self.text = " ".join(w["text"] for w in self.words).strip()
        self.color = self.words[0].get("color")

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Line p{self.page} top={self.top:.0f} {self.text[:48]!r}>"

    @property
    def max_x(self) -> float:
        return max(w["x1"] for w in self.words)


def _normalize(text: str) -> str:
    return text.translate(TRANSLATIONS)


def _lines_from_pages(pages: list[list[dict]]) -> list[Line]:
    """Group captured words into visual rows, in reading order.

    Takes plain dictionaries — text, x0, x1, top, color — rather than a PDF,
    because that is genuinely all this parser needs. The test corpus is stored
    in exactly this form: it keeps every layout quirk that matters while
    carrying nothing that identifies a vehicle (see `capture.py`).
    """
    out: list[Line] = []
    for number, words in enumerate(pages, start=1):
        for word in words:
            word["text"] = _normalize(word["text"])
        rows: list[list[dict]] = []
        for word in sorted(words, key=lambda w: (w["top"], w["x0"])):
            if rows and abs(rows[-1][0]["top"] - word["top"]) <= LINE_TOLERANCE:
                rows[-1].append(word)
            else:
                rows.append([word])
        out.extend(Line(number, row) for row in rows)
    return out


def words_from_pdf(source) -> list[list[dict]]:
    """Read a PDF into the word structure the parser consumes."""
    import pdfplumber

    pages = []
    with pdfplumber.open(source) as pdf:
        for page in pdf.pages:
            pages.append(
                [
                    {
                        "text": word["text"],
                        "x0": word["x0"],
                        "x1": word["x1"],
                        "top": word["top"],
                        "color": word.get("non_stroking_color"),
                    }
                    for word in page.extract_words(
                        keep_blank_chars=False,
                        use_text_flow=False,
                        extra_attrs=["non_stroking_color"],
                    )
                ]
            )
    return pages


def _is_banner(line: Line) -> bool:
    """A module banner, or the Vehicle Information banner: white on blue."""
    return _is_white(line.color)


def _is_heading(line: Line) -> bool:
    """A section heading such as Trouble Code or Live Data: dark blue, bold."""
    return _is_heading_blue(line.color)


# --------------------------------------------------------------------------
# Tables
# --------------------------------------------------------------------------


def _column_bounds(header: Line, labels: list[str]) -> list[tuple[str, float, float]]:
    """Turn a header row into (role, x_start, x_end) using where its words sit.

    Column edges come from the header rather than from whitespace in the joined
    text: a description long enough to reach the next column would otherwise
    swallow it, and GM statuses that wrap onto three rows have no single-line
    form to split at all.
    """
    starts: list[tuple[str, float]] = []
    for label in labels:
        first = label.split()[0]
        for word in header.words:
            if word["text"].lower().startswith(first.lower()):
                if not starts or starts[-1][0] != label:
                    starts.append((label, word["x0"]))
                break
    bounds = []
    for index, (label, x0) in enumerate(starts):
        x1 = starts[index + 1][1] if index + 1 < len(starts) else 10_000.0
        # Half the gap back, so a value nudged left of its header still lands
        # in the right column.
        bounds.append((label, x0 - 6 if index else 0.0, x1 - 6))
    return bounds


def _cells(line: Line, bounds: list[tuple[str, float, float]]) -> dict[str, str]:
    cells: dict[str, list[str]] = {label: [] for label, _s, _e in bounds}
    for word in line.words:
        center = (word["x0"] + word["x1"]) / 2
        for label, start, end in bounds:
            if start <= center < end:
                cells[label].append(word["text"])
                break
    return {label: " ".join(parts).strip() for label, parts in cells.items()}


def _looks_like_row_start(cells: dict[str, str]) -> bool:
    return cells.get("NO.", "").isdigit()


TROUBLE_COLUMNS = ["NO.", "DTC Code", "Trouble Code Descriptions", "Status"]
LIVE_LABELS = ["NO.", "Name", "Value", "Maximum", "Minimum", "Unit"]


def _live_bounds(header_words: list[dict]) -> list[tuple[str, float, float]]:
    """Live Data wraps its header over three rows, so match tokens, not a line."""
    picked: list[tuple[str, float]] = []
    used: set[int] = set()
    for label in LIVE_LABELS:
        for index, word in enumerate(header_words):
            if index in used:
                continue
            if word["text"].rstrip(".").lower() == label.rstrip(".").lower():
                picked.append((label, word["x0"]))
                used.add(index)
                break
    picked.sort(key=lambda pair: pair[1])
    bounds = []
    for index, (label, x0) in enumerate(picked):
        x1 = picked[index + 1][1] if index + 1 < len(picked) else 10_000.0
        bounds.append((label, 0.0 if index == 0 else x0 - 6, x1 - 6))
    return bounds


def looks_like_xtool_d8(text: str) -> float:
    """Confidence that this text came from a D8 report (SPEC 8.3a fingerprint).

    Text-only: the tool writes no PDF metadata at all, so the Producer-based
    signal the schema proposed can never fire.
    """
    normalized = _normalize(text)
    hits = sum(1 for pattern in FINGERPRINT if pattern.search(normalized))
    return hits / len(FINGERPRINT)


def parse(source) -> ScanReport:
    """Parse a D8 PDF into a ScanReport. `source` is a path or file object."""
    return parse_pages(words_from_pdf(source))


def parse_pages(pages: list[list[dict]]) -> ScanReport:
    """Parse already-extracted word geometry — the corpus form, and the PDF's."""
    lines = _lines_from_pages(pages)
    report = ScanReport(pages=len(pages))

    report.tool = Tool(vendor=VENDOR, model=MODEL)
    report.vehicle = Vehicle()

    module = ""
    section = ""
    bounds: list[tuple[str, float, float]] = []
    header_words: list[dict] = []
    awaiting_header = False
    vehicle_lines: list[str] = []
    remark_seen = False
    # A cell's first line can render slightly above the row's own baseline: GM
    # statuses put "Last test:Passed" a row-height above the number it belongs
    # to. Fragments seen before any row exists wait here for the next one
    # rather than being dropped, which cost three of twelve statuses.
    pending: dict[str, str] = {}

    for line in lines:
        text = line.text

        # Before the footer skips: the report timestamp shares a line with
        # "Telephone:", so skipping footer markers first loses it entirely.
        if report.generated_at is None:
            match = TIMESTAMP_RE.search(text)
            if match:
                stamp = match.group(1) + " " + match.group(2)
                for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
                    try:
                        report.generated_at = datetime.strptime(stamp, fmt)
                        break
                    except ValueError:
                        continue

        if text.startswith("Remark:"):
            remark_seen = True
            section = ""
            continue
        if any(text.startswith(marker) for marker in FOOTER_MARKERS):
            continue
        if text.startswith("*This report"):
            continue

        if _is_banner(line) or _is_heading(line):
            if text in SECTION_HEADINGS:
                if text == "Vehicle Information":
                    section = "vehicle"
                elif text == "Trouble Code":
                    section, awaiting_header, pending = "dtc", True, {}
                elif text == "Live Data":
                    section, awaiting_header, header_words = "live", True, []
                elif text == "ECU":
                    section = "ecu"
                else:
                    section = ""
                    report.warnings.append("unhandled section: " + text)
                continue
            if _is_banner(line) and not remark_seen and text:
                # A module banner. Everything under it belongs to it, until the
                # next banner - including nothing at all, which is how a clean
                # module is reported.
                module = text
                if module not in report.modules:
                    report.modules.append(module)
                section = ""
                continue

        if section == "vehicle":
            vehicle_lines.append(text)
            continue

        if section == "ecu":
            parts = text.rsplit(" ", 1)
            if len(parts) == 2 and parts[1]:
                report.ecu[parts[0].strip()] = parts[1].strip()
            continue

        if section == "dtc":
            if awaiting_header and "DTC" in text:
                bounds = _column_bounds(line, TROUBLE_COLUMNS)
                awaiting_header = False
                continue
            if not bounds:
                continue
            cells = _cells(line, bounds)
            code = cells.get("DTC Code", "")
            if _looks_like_row_start(cells) and DTC_CODE.match(code):
                dtc = Dtc(
                    code=code,
                    description=cells.get("Trouble Code Descriptions", ""),
                    module=module,
                    status_raw=cells.get("Status", ""),
                )
                if pending.get("status"):
                    dtc.status_raw = f"{pending['status']} {dtc.status_raw}".strip()
                if pending.get("description"):
                    dtc.description = f"{pending['description']} {dtc.description}".strip()
                pending = {}
                report.dtcs.append(dtc)
            elif not report.dtcs:
                # Before the first row: keep the fragment for it.
                for role, key in (("Status", "status"), ("Trouble Code Descriptions", "description")):
                    if cells.get(role):
                        pending[key] = f"{pending.get(key, '')} {cells[role]}".strip()
            elif report.dtcs:
                # A wrapped description, or one of GM's extra status rows.
                target = report.dtcs[-1]
                extra_description = cells.get("Trouble Code Descriptions", "")
                extra_status = cells.get("Status", "")
                if extra_description:
                    target.description = (target.description + " " + extra_description).strip()
                if extra_status:
                    target.status_raw = (target.status_raw + " " + extra_status).strip()
            continue

        if section == "live":
            if awaiting_header:
                header_words.extend(line.words)
                if line.words and line.words[0]["text"].isdigit():
                    # The first data row arrived; the header is what came before.
                    header_words = [w for w in header_words if not w["text"].isdigit()]
                    bounds = _live_bounds(header_words)
                    awaiting_header = False
                elif "Unit" in text:
                    bounds = _live_bounds(header_words)
                    awaiting_header = False
                    continue
                else:
                    continue
            if not bounds:
                continue
            cells = _cells(line, bounds)
            if cells.get("NO.", "").isdigit() and cells.get("Name"):
                report.live_data.append(
                    LiveDatum(
                        name=cells["Name"],
                        value=cells.get("Value", ""),
                        maximum=cells.get("Maximum", ""),
                        minimum=cells.get("Minimum", ""),
                        unit=cells.get("Unit", ""),
                        module=module,
                    )
                )
            elif report.live_data and cells.get("Name"):
                report.live_data[-1].name += " " + cells["Name"]
            continue

    _finish_vehicle(report, " ".join(vehicle_lines))
    for dtc in report.dtcs:
        _finish_status(dtc)
    return report


def _finish_vehicle(report: ScanReport, blob: str) -> None:
    vehicle = report.vehicle
    match = VIN_RE.search(blob)
    if match:
        vehicle.vin = match.group(1)
    match = YEAR_RE.search(blob)
    if match:
        vehicle.year = int(match.group(1))
    match = NAME_RE.search(blob)
    if match:
        vehicle.name = match.group(1).strip(" :")
    match = SERIAL_RE.search(blob)
    if match:
        report.tool.serial = match.group(1)
    match = ROUTE_RE.search(blob)
    if match:
        vehicle.diagnosis_route = match.group(1).strip()
    match = MILEAGE_RE.search(blob)
    if match:
        value = int(match.group(1).replace(",", ""))
        vehicle.odometer_unit = "mi" if match.group(2).lower().startswith("mile") else "km"
        # "0mile" is the tool saying it never read the odometer. Storing a zero
        # would look like a genuine reading and reset every mileage interval.
        vehicle.odometer = value or None


def _finish_status(dtc: Dtc) -> None:
    raw = dtc.status_raw.strip()
    if not raw:
        return
    gm = GM_STATUS_RE.findall(raw)
    if gm:
        for label, value in gm:
            setattr(dtc, label.lower().replace(" ", "_"), value.strip())
        # GM does not print a single state. "Last test: Passed" says the fault
        # is not present now, which is history; anything else is left unmapped
        # for a human rather than guessed at.
        if dtc.last_test.lower().startswith("passed"):
            dtc.status = "history"
        return
    dtc.status = STATUS_MAP.get(raw.lower(), "")
