"""
The TOPDON BT600 Plus, read off a photograph of its printout (SPEC §8.3a, §7.9).

A battery tester prints paper and nothing else. There is no PDF to export, no
CSV, no app — what the operator has ten seconds later is a phone photo of a
thermal receipt, usually curled, usually sideways, and that is the whole of the
evidence. So this is the first parser here whose input is pixels rather than a
document, and it is a built-in for the same reason `xtool_d8` is: no regex over
flattened text can do what it has to do.

**Why a declarative profile could not read this.**

* The photographs are sideways. Every sample in the corpus carries EXIF
  orientation 6, and OCR of an unrotated receipt returns nothing at all — not
  bad text, *no* text. That is fixed upstream, in the image pipeline, but it is
  the reason the format looked unreadable before anyone wrote a rule.
* One photograph can hold **more than one report**. `20260830_105647` is a
  cranking test and a charging test on one strip of paper, each with its own
  timestamp and its own `VOLTAGE`. A profile fills a flat `{field: value}`
  dictionary, which has room for exactly one of each.
* The receipt draws its own values. Two bar graphs and a voltage trace sit
  between the labels, and the trace's axis ticks read as `24U 12U 0U` — three
  numbers with a voltage unit beside them that are not measurements of
  anything. A pattern hunting for a voltage finds them.
* Everything is a guess. A value read off paper needs its own confidence and
  its own bounding box so review can show the crop it came from; the
  declarative engine has one confidence per field, declared in advance by the
  profile author rather than measured.

**What this file will not do.** It never reads a number off the bar graphs or
the cranking trace, and it never repairs a character in anything but a numeric
field. A tester that prints `HEALTH: 79%` beside a graph of the same fact is
not offering two sources; it is offering one source and a picture of it. And a
verdict is a word from a vocabulary — turning `GOOD` into `6OOD` because `G`
looks like a `6` somewhere else on the page would be inventing a reading.

Where a value cannot be read it stays visible as the characters it was read
from, with a warning, and `value` is left empty. Nothing here guesses, and
nothing here drops.

No Django imports, deliberately, so the parser runs over the sample corpus
without a database — the same rule `xtool_d8` and `report` follow.
"""

from __future__ import annotations

import re
import statistics
from dataclasses import dataclass, field
from datetime import datetime

from .report import TesterReport, TestResult, Tool, Value

VENDOR = "TOPDON"
MODEL = "BT600 Plus"

#: Where a value's own confidence is low enough to be worth pointing at. Not a
#: rejection — a reading Tesseract was unsure of is still the only reading
#: there is, and the operator is looking at the paper anyway.
LOW_CONFIDENCE = 0.65

#: What a value is worth when the capture carries no per-word confidence at
#: all: word geometry from a PDF, or a re-parse from stored text. High, but not
#: certain — it came off a photograph either way.
ASSUMED_CONFIDENCE = 0.9

#: A value that needed characters repaired before it was a number is worth much
#: less than one that did not, whatever Tesseract thought of the glyphs.
REPAIR_PENALTY = 0.5

#: A value found on the line above or below its label rather than beside it.
NEARBY_PENALTY = 0.85

#: A category matched by folding digit-shaped letters rather than exactly.
FOLDED_PENALTY = 0.8

# --------------------------------------------------------------------------
# Warning codes
# --------------------------------------------------------------------------
#
# Codes rather than sentences, because the sentence a reviewer sees has to be
# translated and this file has no gettext to translate it with. The review
# screen owns the wording; this owns the finding.

UNREADABLE = "unreadable"
OUT_OF_RANGE = "out_of_range"
REPAIRED = "repaired"
LOW_CONF = "low_confidence"
NOT_BESIDE_LABEL = "not_beside_its_label"
MISSING = "missing"
NO_TIMESTAMP = "no_timestamp"
SERIAL_DISAGREES = "serial_disagrees"
UNCLASSIFIED = "unclassified"


# --------------------------------------------------------------------------
# Fingerprinting
# --------------------------------------------------------------------------

#: `BT600PLUS`, allowing for what a thermal printer and a phone camera do to
#: it. The zeroes and the `6` are the characters that actually move — `G` for
#: `6` and `O`/`Q`/`D` for `0` are the substitutions these five photographs
#: produce — and the spacing is optional throughout because OCR splits the word
#: wherever the paper creased.
HEADER = re.compile(r"[B8]\s*T\s*[6G]\s*[0OQD]\s*[0OQD]\s*P\s*[LI1|]\s*[UV]\s*[S5$]", re.I)

TEST_REPORT = re.compile(r"TE[S5]T\s*REP[O0Q]RT", re.I)

#: The white-on-black section banner. Inverted text is the first thing OCR
#: loses on a photograph, so nothing here depends on finding one — the field
#: signature below says the same thing from the labels, which are black on
#: white and survive.
SECTION = re.compile(r"\b(BATTERY|CRANK[I1]NG|CHARG[I1]NG)\s*TE[S5]T\b", re.I)

BATTERY = "battery"
CRANKING = "cranking"
CHARGING = "charging"

#: What each kind of report prints, used to classify a section whose banner
#: did not survive and to fingerprint the format as a whole. A battery slip
#: from another maker has a verdict and a voltage too; it does not have this
#: combination of labels.
SIGNATURES = {
    BATTERY: ("health", "charge", "measured", "rated", "internal_r"),
    CRANKING: ("voltage", "time"),
    CHARGING: ("unloaded", "loaded", "ripple"),
}


def looks_like(text: str) -> float:
    """How much this reads like a BT600 Plus printout, from 0 to 1.

    Several groups have to agree, not one. `BATTERY TEST` and a voltage appear
    on every battery slip ever printed — the mocked Midtronics GR8 in
    `tests_photo.py` has both — so a fingerprint resting on either would claim
    another maker's paper and hand it to a parser that knows nothing about it.
    """
    hits = 0.0
    if HEADER.search(text):
        hits += 0.4
    if TEST_REPORT.search(text):
        hits += 0.2
    if SECTION.search(text):
        hits += 0.2
    if _best_signature(_labels_in(text))[1] >= 2:
        hits += 0.2
    return round(min(1.0, hits), 2)


def _labels_in(text: str) -> set[str]:
    return {key for line in text.splitlines() if (key := _label_of(line)[0])}


def _best_signature(labels: set[str]) -> tuple[str, int]:
    best, best_hits = "", 0
    for kind, wanted in SIGNATURES.items():
        hits = len(labels & set(wanted))
        if hits > best_hits:
            best, best_hits = kind, hits
    return best, best_hits


# --------------------------------------------------------------------------
# Labels
# --------------------------------------------------------------------------

#: Each label as the printer lays it down, allowing the substitutions a
#: photograph of thermal paper actually produces. Ordered, and `unloaded`
#: precedes `loaded` for the reader's sake rather than the matcher's: both are
#: anchored at the start of the line, so `LOADED:` can never claim a row that
#: begins `UNLOADED:`.
LABELS: tuple[tuple[str, str], ...] = (
    ("health", r"HEA[L1I|]TH"),
    ("charge", r"CHARGE"),
    ("voltage", r"V[O0Q]LTAGE"),
    ("measured", r"MEA[S5]URED"),
    ("standard", r"[S5]TANDARD"),
    ("rated", r"RATED"),
    ("type", r"TYPE"),
    ("internal_r", r"[I1|]NTERNA[L1|]\s*R"),
    ("time", r"T[I1|]ME"),
    ("unloaded", r"UN\s*[L1|][O0Q]ADED"),
    ("loaded", r"[L1|][O0Q]ADED"),
    ("ripple", r"R[I1|]PP[L1|]E"),
    ("serial", r"[S5]\s*[NM]"),
)

#: A colon is what separates a label from its value, and it is also the first
#: mark to vanish into a crease — so it is optional, and the anchor plus the
#: whitespace carry the rest. `;` and `.` are what OCR offers in its place.
SEPARATOR = r"\s*[:;.,！!]?\s*"

_LABEL_RES = tuple(
    (key, re.compile(rf"^\s*(?:{pattern}){SEPARATOR}", re.I)) for key, pattern in LABELS
)


def _label_of(text: str) -> tuple[str, int]:
    """Which label this line begins with, and where its value starts."""
    for key, pattern in _LABEL_RES:
        found = pattern.match(text)
        if found:
            return key, found.end()
    return "", 0


#: What each numeric label measures: the unit it is printed in and the range a
#: reading has to fall inside to be a reading rather than an OCR accident.
#: Ranges are wide on purpose. They exist to catch `7550 CCA` read off `755`,
#: not to second-guess a tester about a battery it has in front of it.
KINDS: dict[str, tuple[str, str]] = {
    "health": ("%", "percent"),
    "charge": ("%", "percent"),
    "voltage": ("V", "volts"),
    "measured": ("CCA", "cca"),
    "rated": ("CCA", "cca"),
    "internal_r": ("mΩ", "resistance"),
    "time": ("ms", "duration"),
    "unloaded": ("V", "volts"),
    "loaded": ("V", "volts"),
    "ripple": ("mV", "ripple"),
}

RANGES: dict[str, tuple[float, float]] = {
    "percent": (0, 100),
    "volts": (0, 30),
    "cca": (50, 3000),
    "resistance": (0, 100),
    "duration": (0, 10_000),
    "ripple": (0, 2000),
}

#: The printed name each key answers to, for a review screen that has no
#: translation for a label a later firmware invents.
PRINTED = {
    "health": "HEALTH",
    "charge": "CHARGE",
    "voltage": "VOLTAGE",
    "measured": "MEASURED",
    "rated": "RATED",
    "standard": "STANDARD",
    "type": "TYPE",
    "internal_r": "INTERNAL R",
    "time": "TIME",
    "unloaded": "UNLOADED",
    "loaded": "LOADED",
    "ripple": "RIPPLE",
    "verdict": "",
    "performed_on": "",
}

#: What the tester was **told** rather than what it measured. Before a battery
#: test the operator keys in the capacity, rating standard and chemistry printed
#: on the battery's own label; the slip prints them back beside the measurements.
#: `MEASURED: 755CCA` against `RATED: 850CCA` is the entire result, and a screen
#: that showed both as readings would invite the reader to treat a number
#: somebody typed as something the instrument found.
ENTERED = {"rated", "standard", "type"}

#: Which readings each kind of report shows, in the order the paper prints
#: them. A reading the receipt did not carry is not invented; this only fixes
#: the order, so review reads down the screen the way it reads down the paper.
ORDER = {
    BATTERY: ("health", "charge", "voltage", "measured", "rated", "internal_r"),
    CRANKING: ("voltage", "time"),
    CHARGING: ("unloaded", "loaded", "ripple"),
}


# --------------------------------------------------------------------------
# Vocabularies
# --------------------------------------------------------------------------

VERDICTS = {
    "GOOD BATTERY": "good_battery",
    "GOOD RECHARGE": "good_recharge",
    "RECHARGE": "recharge",
    "REPLACE": "replace",
    "BAD CELL": "bad_cell",
    "BAD CELL REPLACE": "bad_cell",
    "CAUTION": "caution",
    "CRANKING NORMAL": "cranking_normal",
    "CRANKING LOW": "cranking_low",
    "CRANKING HIGH": "cranking_high",
    "NO START": "no_start",
    "CHARGING NORMAL": "charging_normal",
    "CHARGING LOW": "charging_low",
    "CHARGING HIGH": "charging_high",
    "NO OUTPUT": "no_output",
    "RIPPLE NORMAL": "ripple_normal",
    "RIPPLE HIGH": "ripple_high",
}

BATTERY_TYPES = {
    "REGULAR FLOODED": "regular_flooded",
    "FLOODED": "flooded",
    "AGM FLAT PLATE": "agm_flat_plate",
    "AGM SPIRAL": "agm_spiral",
    "AGM": "agm",
    "EFB": "efb",
    "GEL": "gel",
    "VRLA": "vrla",
    "START STOP": "start_stop",
}

STANDARDS = ("CCA", "SAE", "EN2", "EN", "DIN", "IEC", "JIS", "GB", "MCA", "CA", "BCI")


# --------------------------------------------------------------------------
# Rows
# --------------------------------------------------------------------------


@dataclass(slots=True)
class Row:
    """One printed line, and the words it was assembled from.

    The words are kept rather than only their joined text because a value's
    box and a value's confidence are both facts about *which words* it came
    from — and a review screen that offers a crop of the paper needs the box,
    not the line the box was on.
    """

    words: list[dict] = field(default_factory=list)
    text: str = ""
    #: `(start, end)` of each word within `text`, so a span of the line can be
    #: traced back to the words that printed it.
    offsets: list[tuple[int, int]] = field(default_factory=list)

    def at(self, start: int, end: int) -> list[dict]:
        return [
            word
            for word, (a, b) in zip(self.words, self.offsets)
            if a < end and b > start
        ]

    def box(self, start: int = 0, end: int | None = None) -> list[float]:
        return _box(self.at(start, len(self.text) if end is None else end))

    def confidence(self, start: int = 0, end: int | None = None) -> float:
        return _confidence(self.at(start, len(self.text) if end is None else end))

    @property
    def top(self) -> float:
        return min((_f(w, "top") for w in self.words), default=0.0)


def _f(word: dict, name: str, default: float = 0.0) -> float:
    try:
        return float(word.get(name, default))
    except (TypeError, ValueError):
        return default


def _box(words: list[dict]) -> list[float]:
    if not words:
        return []
    tops = [_f(w, "top") for w in words]
    bottoms = [_f(w, "bottom", _f(w, "top")) for w in words]
    box = [
        min(_f(w, "x0") for w in words),
        min(tops),
        max(_f(w, "x1", _f(w, "x0")) for w in words),
        max(bottoms),
    ]
    # Nothing, rather than a box of no area. A re-parse from stored text has
    # line numbers standing in for geometry, and a review screen offered a crop
    # of a rectangle zero pixels wide — which is not a smaller promise than a
    # real crop, it is a broken one.
    if box[2] <= box[0] or box[3] <= box[1]:
        return []
    return box


def _confidence(words: list[dict]) -> float:
    """The weakest word in the span, on a 0-1 scale.

    Tesseract reports 0-100 per word and `-1` for a box it found no text in.
    A capture with no confidence column at all — word geometry lifted from a
    PDF, or a hand-checked fixture — is assumed good rather than assumed
    unreadable, because the alternative is a review screen that flags every
    value it has ever been shown.
    """
    scores = [_f(w, "conf", -1.0) for w in words]
    usable = [s for s in scores if s >= 0]
    if not usable:
        return ASSUMED_CONFIDENCE
    return round(min(usable) / 100.0, 4)


def rows_from_words(pages: list[list[dict]]) -> list[Row]:
    """Word geometry read back as the lines the tester printed.

    Grouped by the vertical **centre** of each word rather than its top, and
    with a tolerance taken from the words' own height rather than a constant.
    Both matter here and neither did for a PDF. A constant of two points is
    right for typeset text seven hundred points tall and meaningless for a
    photograph three thousand pixels tall, where one printed line is sixty
    pixels of glyph; and a receipt pairs `HEALTH:` with `79%`, whose tops
    differ by more than the `%` is tall.
    """
    rows: list[Row] = []
    for page in pages:
        words = [w for w in page if str(w.get("text", "")).strip()]
        if not words:
            continue
        tolerance = _tolerance(words)
        current: list[dict] = []
        centre = None
        for word in sorted(words, key=lambda w: (_centre(w), _f(w, "x0"))):
            here = _centre(word)
            if centre is None or abs(here - centre) <= tolerance:
                current.append(word)
                centre = statistics.fmean([_centre(w) for w in current])
            else:
                rows.append(_row(current))
                current, centre = [word], here
        if current:
            rows.append(_row(current))
    return rows


def _centre(word: dict) -> float:
    top = _f(word, "top")
    return (top + _f(word, "bottom", top)) / 2.0


def _tolerance(words: list[dict]) -> float:
    """Half a line of text, worked out from the text.

    Falls back to a small constant where the capture carries no `bottom` — a
    PDF's word geometry, where the old constant was right to begin with.
    """
    heights = [h for w in words if (h := _f(w, "bottom") - _f(w, "top")) > 0]
    return 0.6 * statistics.median(heights) if heights else 2.0


def _row(words: list[dict]) -> Row:
    ordered = sorted(words, key=lambda w: _f(w, "x0"))
    parts, offsets, cursor = [], [], 0
    for word in ordered:
        text = str(word.get("text", "")).strip()
        offsets.append((cursor, cursor + len(text)))
        parts.append(text)
        cursor += len(text) + 1
    return Row(words=ordered, text=" ".join(parts), offsets=offsets)


def rows_from_text(text: str) -> list[Row]:
    """Lines with no geometry behind them.

    This is what a re-parse of an older session gets: photographs were read for
    their text long before their words were kept, and those sessions still have
    to be re-readable — that is the whole promise of retaining the extraction.
    Boxes come out empty, so review offers no crop and says so.
    """
    rows = []
    for index, line in enumerate(text.splitlines()):
        stripped = line.strip()
        if stripped:
            rows.append(_row([{"text": stripped, "x0": 0.0, "top": float(index)}]))
    return rows


# --------------------------------------------------------------------------
# Noise
# --------------------------------------------------------------------------

#: The dotted rule the printer lays down between receipts, and the marks a
#: photograph adds: a crease read as a pipe, an edge read as an equals sign, the
#: shadow under a curl read as a dash. None of them is a value and all of them
#: sit between the labels and their values, where the fallback goes looking.
RULE = re.compile(r"^[\s*+.,;:'\"`´“”‘’·•~^_|/\\=()\[\]<>!?—–-]+$")

#: A tick on the cranking graph's axis. `24V` printed small enough that the `V`
#: comes back as a `U`, which is exactly what all five samples do.
AXIS_TICK = re.compile(r"^\d{1,2}\s*[UV]$", re.I)


def is_noise(text: str) -> bool:
    """Anything printed that is not a statement about the battery.

    The axis ticks are the ones that matter. `24U 12U 0U` is three numbers with
    a voltage unit beside them, sitting a line or two from `VOLTAGE:` — so the
    fallback that looks for a value near its label would find `0U` and report a
    cranking voltage of zero volts, off a picture of a graph.
    """
    stripped = text.strip()
    if not stripped:
        return True
    if RULE.match(stripped):
        return True
    if TEST_REPORT.search(stripped) or HEADER.search(stripped):
        return True
    tokens = stripped.split()
    return bool(tokens) and all(AXIS_TICK.match(token) for token in tokens)


# --------------------------------------------------------------------------
# Segmentation
# --------------------------------------------------------------------------

#: How far above its section banner a receipt's own `BT600PLUS` header can sit.
#: Two lines in the samples — the header, then `TEST REPORT`, then the banner.
HEADER_REACH = 3


def receipts(rows: list[Row]) -> list[list[Row]]:
    """Cut a strip of paper into the reports printed on it.

    The `BT600PLUS` header is the real boundary and it is repeated for every
    report, which is what makes a multi-report photograph readable at all. But
    it is also the largest, boldest thing on the page and therefore the most
    likely to be blown out by a flash — so a section banner with no header
    above it starts a receipt too. Either signal alone is enough; needing both
    would lose a report to one unlucky highlight.
    """
    heads = [i for i, row in enumerate(rows) if HEADER.search(row.text)]
    starts = set(heads)
    for index, row in enumerate(rows):
        if not SECTION.search(row.text):
            continue
        near = [h for h in heads if 0 <= index - h <= HEADER_REACH]
        if not near:
            starts.add(index)
    if not starts:
        return [rows] if rows else []

    ordered = sorted(starts)
    if ordered[0] > 0:
        # Whatever precedes the first header is the tail of the previous
        # receipt, torn off. Kept with the first report rather than dropped:
        # it is where a timestamp ends up when somebody photographs the strip
        # from the bottom.
        ordered.insert(0, 0)
    bounds = ordered + [len(rows)]
    return [rows[a:b] for a, b in zip(bounds, bounds[1:]) if rows[a:b]]


def classify(rows: list[Row]) -> str:
    """Which test this receipt records.

    The banner answers it when the banner survived. When it did not, the labels
    do — and they are black on white, so they nearly always have.
    """
    for row in rows:
        found = SECTION.search(row.text)
        if found:
            word = found.group(1).upper()
            if word.startswith("BATT"):
                return BATTERY
            if word.startswith("CRANK"):
                return CRANKING
            return CHARGING
    kind, hits = _best_signature({_label_of(row.text)[0] for row in rows} - {""})
    return kind if hits >= 2 else ""


# --------------------------------------------------------------------------
# Values
# --------------------------------------------------------------------------

#: Characters a thermal print and a phone camera swap for digits. Applied only
#: where the label says the field is a number — never to a verdict, a battery
#: type or a serial, where the same substitution would be an invention.
REPAIRS = str.maketrans(
    {
        "O": "0", "o": "0", "D": "0", "Q": "0",
        "I": "1", "i": "1", "l": "1", "|": "1",
        "S": "5", "s": "5",
        "B": "8",
        "Z": "2", "z": "2",
        "G": "6",
    }
)

#: `U` is deliberately not in there. It is not a digit-shaped letter — it is
#: what a small `V` comes back as, which is exactly the axis ticks on the
#: cranking graph. With `U` mapped to zero, `12U` repairs to `120` and a
#: picture of a graph becomes a hundred and twenty of something.

#: The characters a number could have been printed as, before repair. Anything
#: outside this ends the number, which is what keeps `755` out of `755CCA` and
#: `12` out of `12mV`.
NUMBER = re.compile(r"[-+]?[0-9OoDQIil|SsBZzG][0-9OoDQIil|SsBZzG.,]*")

CLEAN_NUMBER = re.compile(r"^[-+]?\d+(?:\.\d+)?$")


#: A run that is already digits. Tried before anything is repaired.
CLEAN_RUN = re.compile(r"[-+]?\d[\d.,]*")


def _number(raw: str) -> tuple[str, bool, str, tuple[int, int]]:
    """The number in `raw`, whether it needed repair, and what follows it.

    **A reading that needs no repair beats one that does**, even when the
    repairable one comes first. OCR reads `850CCA(CCA)` off one of these
    receipts as `B850CCA(CCA)` — it found a mark before the eight — and taking
    the first repairable run gives `8850`, a rated capacity nine times what the
    battery has. Taking the clean digits gives `850`, which is what is printed.
    Preferring the unrepaired reading is strictly the more conservative of the
    two; it is not choosing whichever answer fits.

    Returns the digits exactly as printed apart from a stripped leading zero —
    `03.82` is `3.82`, and `0` is `0`. That distinction is the whole of the
    difference between a battery at zero percent charge and a battery whose
    charge nobody recorded, and it is not a difference a `float` keeps.
    """
    clean = CLEAN_RUN.search(raw)
    if clean:
        text = clean.group(0).rstrip(".,").replace(",", "")
        if CLEAN_NUMBER.match(text):
            return _plain(text), False, raw[clean.end() :].strip(), clean.span()

    found = NUMBER.search(raw)
    if not found:
        return "", False, raw.strip(), (0, len(raw))
    text = found.group(0).rstrip(".,").replace(",", "")
    repaired = text.translate(REPAIRS).rstrip(".,")
    # One character is not enough to repair. `BSO` -> `850` is three glyphs
    # agreeing that this is a number; `|` -> `1` is a crease in the paper, and
    # a receipt photographed off a bench is full of single marks that are
    # digit-shaped if you are willing to read them that way. Every repair the
    # corpus actually needs is at least two characters long.
    if len(text) >= 2 and CLEAN_NUMBER.match(repaired):
        return _plain(repaired), True, raw[found.end() :].strip(), found.span()
    return "", False, raw.strip(), (0, len(raw))


def _plain(text: str) -> str:
    """`03.82` -> `3.82`, `0` -> `0`, `11.90` -> `11.90`.

    Trailing zeroes after the point are kept because the tester printed them
    and a reading of `11.9` is a different claim about precision from `11.90`.
    """
    sign = "-" if text.startswith("-") else ""
    body = text.lstrip("+-")
    whole, _, fraction = body.partition(".")
    whole = whole.lstrip("0") or "0"
    return sign + (f"{whole}.{fraction}" if fraction else whole)


def _fold(text: str) -> str:
    """A category's letters, with digit-shaped ones folded back.

    Matching, not repair: `FL00DED` is compared against `FLOODED` without any
    claim that the paper said `O`. `raw` keeps what was read and `value` gets
    the vocabulary's own identifier, so nothing is invented in either
    direction — the confidence is docked instead.
    """
    folded = text.upper().translate(
        str.maketrans({"0": "O", "1": "I", "5": "S", "8": "B", "6": "G", "2": "Z", "|": "I"})
    )
    return re.sub(r"[^A-Z0-9]+", " ", folded).strip()


def _plain_letters(text: str) -> str:
    """The printed text with punctuation flattened and nothing else changed."""
    return re.sub(r"[^A-Z0-9]+", " ", text.upper()).strip()


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


def _category(raw: str, vocabulary: dict[str, str]) -> tuple[str, bool]:
    """A printed category as a stable identifier, and whether folding was used.

    A word the vocabulary does not hold becomes a slug of itself rather than
    nothing. A firmware that starts printing a battery chemistry this file has
    never seen should still record *which* chemistry, and a slug of the printed
    words is a faithful, reversible name for it — not a guess about what it
    means. Anything switching on these identifiers has to treat an unknown one
    as unknown, which it would have to do anyway.
    """
    key = re.sub(r"[^A-Z0-9]+", " ", raw.upper()).strip()
    if key in vocabulary:
        return vocabulary[key], False
    folded = _fold(raw)
    if folded in vocabulary:
        return vocabulary[folded], True
    return _slug(folded or raw), bool(folded and folded != key)


#: The date is matched strictly and the clock is not. The date is the part
#: that says *this row is a timestamp* — four digits, two, two, in that order,
#: is not something else on this receipt — and the clock is the part with a
#: tear-off printed through it on one of the five samples.
TIMESTAMP = re.compile(
    r"(\d{4})\s*-\s*(\d{1,2})\s*-\s*(\d{1,2})\s+(\S{1,2})\s*[:;.]\s*(\S{1,2})\s*[:;.]\s*(\S{1,2})"
)


def _component(text: str) -> tuple[str, bool]:
    """One field of a clock: all of it is digits, or none of it is a reading.

    Whole-token, and that is the point. Pulling the leading digits out of
    `1@` would report an hour of one o'clock off a character nobody can read —
    the failure this whole file is written to avoid, in the one field where it
    would be least visible afterwards.
    """
    if text.isdigit():
        return text, False
    repaired = text.translate(REPAIRS)
    if repaired.isdigit():
        return repaired, True
    return "", False


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------


def parse_pages(pages: list[list[dict]], size: tuple[int, int] | list = ()) -> TesterReport:
    """Read word geometry. `size` is the picture the boxes are measured in."""
    return parse_rows(rows_from_words(pages), size=size)


def parse_text(text: str) -> TesterReport:
    return parse_rows(rows_from_text(text))


def parse_rows(rows: list[Row], size: tuple[int, int] | list = ()) -> TesterReport:
    report = TesterReport(tool=Tool(vendor=VENDOR, model=MODEL), page=list(size))
    serials: list[str] = []
    for chunk in receipts(rows):
        # Numbered by the results kept, not by the chunks cut. A photograph
        # taken from the bottom of the strip starts with the torn tail of the
        # receipt before it, and counting that would leave the first report
        # anybody can see labelled as the second.
        result, serial = _result(chunk, len(report.results))
        if result is None:
            if _looked_like_a_report(chunk):
                # A section that carries a banner or several of the labels and
                # still could not be classified is a report this parser failed
                # to read, which is a different thing from the torn tail of the
                # previous receipt at the top of the photograph. Only the first
                # is worth telling anybody about.
                report.warnings.append(UNCLASSIFIED)
            continue
        if serial:
            serials.append(serial)
        report.results.append(result)

    if serials:
        report.tool.serial = serials[0]
        if len(set(serials)) > 1:
            # One tester printed this strip. Two serials means a receipt was
            # misread or two strips were photographed together, and either is
            # worth saying out loud rather than picking the first and moving on.
            report.warnings.append(SERIAL_DISAGREES)
    return report


def _looked_like_a_report(rows: list[Row]) -> bool:
    if any(SECTION.search(row.text) for row in rows):
        return True
    return len({_label_of(row.text)[0] for row in rows} - {""}) >= 2


def _result(rows: list[Row], index: int) -> tuple[TestResult | None, str]:
    kind = classify(rows)
    if not kind:
        return None, ""

    result = TestResult(kind=kind, index=index, box=_box([w for r in rows for w in r.words]))
    serial = ""
    found: dict[str, Value] = {}
    standard = ""

    for position, row in enumerate(rows):
        key, start = _label_of(row.text)
        if not key:
            continue
        raw, where, span, nearby = _value_for(rows, position, start)
        if key == "serial":
            serial = raw.strip()
            continue
        value = _read(key, raw, where, span, nearby)
        if key == "standard":
            standard = value.value
        found[key] = value

    result.verdict = _verdict(rows)
    result.performed_on = _timestamp(rows)

    for key in ("standard", "type"):
        if key in found:
            result.attributes.append(found[key])
    for key in ORDER.get(kind, ()):
        if key in found:
            result.readings.append(_with_standard(found[key], standard))
    for key, value in found.items():
        # A label a later firmware added, kept rather than dropped. It has no
        # place in the printed order because nobody here knows what that is.
        if key not in ORDER.get(kind, ()) and key not in ("standard", "type"):
            result.readings.append(value)

    if result.performed_on is None:
        # Not fatal. A receipt with no clock still records a battery, and the
        # session keeps the time of whichever receipt on the strip did have
        # one — but the reviewer should know this reading is undated.
        result.warnings.append(NO_TIMESTAMP)
    if [k for k in ORDER.get(kind, ()) if k not in found]:
        result.warnings.append(MISSING)
    return result, serial


def _with_standard(value: Value, standard: str) -> Value:
    """A CCA reading carries whichever rating standard the receipt named.

    The unit on `MEASURED` and `RATED` is not a unit in the way `%` is — it is
    whichever of CCA, EN, DIN or JIS the tester was set to, and 755 of one is
    not 755 of another. `RATED: 850CCA(CCA)` states it twice; `STANDARD:` is
    the row that means to.
    """
    if value.key in ("measured", "rated") and standard and value.unit == "CCA":
        value.unit = standard
    return value


def _value_for(
    rows: list[Row], position: int, start: int
) -> tuple[str, Row, tuple[int, int], bool]:
    """What follows a label, from beside it or from the line next to it.

    Beside it first, always. The fallback exists because a curled receipt
    photographed at an angle puts a right-aligned value a line off its
    left-aligned label often enough to matter, and it is deliberately narrow:
    one line either way, never onto a line that carries its own label, and
    never onto the graph's axis ticks.
    """
    row = rows[position]
    # Stripped of what a separator leaves behind. OCR puts the colon back a
    # character late often enough that `MEASURED: 687CCA` comes out with the
    # value reading `: 687CCA` — which is not what was read off the paper for
    # that value, it is the label's own punctuation in the wrong column.
    tail = row.text[start:].strip().lstrip(":;.,·• 	")
    if tail:
        offset = row.text.index(tail, start)
        return tail, row, (offset, offset + len(tail)), False

    key = _label_of(row.text)[0]
    for step in (1, -1):
        index = position + step
        if not 0 <= index < len(rows):
            continue
        near = rows[index]
        if _label_of(near.text)[0] or is_noise(near.text) or SECTION.search(near.text):
            continue
        # A numeric field will not accept a line with no digits in it. Without
        # this, `HEALTH:` on a curled receipt reached down to the crease below
        # it, read the crease as `|`, repaired the pipe into a one and reported
        # the battery at one percent health — with the real `79%` printed on
        # the line above, which is where the search goes next.
        if key in KINDS and not any(c.isdigit() for c in near.text):
            continue
        text = near.text.strip()
        if text:
            # The neighbouring row, not this one. The box is what the review
            # screen crops the photograph to, and pointing it at the label
            # instead of the value would show the reader the word they already
            # knew rather than the characters in doubt.
            return text, near, (0, len(near.text)), True
    return "", row, (start, start), False


def _read(key: str, raw: str, row: Row, span: tuple[int, int], nearby: bool) -> Value:
    """One labelled value, validated but never repaired into existence."""
    value = Value(
        key=key,
        label=PRINTED.get(key, key.upper()),
        raw=raw,
        entered=key in ENTERED,
        box=row.box(*span),
        confidence=row.confidence(*span),
    )
    if nearby:
        value.confidence *= NEARBY_PENALTY
        value.warnings.append(NOT_BESIDE_LABEL)

    if key == "standard":
        value.value, folded = _category(raw, {s: s for s in STANDARDS})
        if folded:
            value.confidence *= FOLDED_PENALTY
        return _flag(value)
    if key == "type":
        value.value, folded = _category(raw, BATTERY_TYPES)
        if folded:
            value.confidence *= FOLDED_PENALTY
        return _flag(value)

    unit, measures = KINDS.get(key, ("", ""))
    number, repaired, rest, where = _number(raw)
    if number:
        # Narrowed to the characters the number was actually read from. A
        # stray mark elsewhere on the line is not evidence about this value:
        # one receipt puts a colon of its own at confidence 8 beside a
        # perfectly clear `687CCA`, and taking the weakest word on the row
        # reported the reading at 0.08 and told the operator to check it.
        # It also points the crop at the number rather than at the line.
        value.box = row.box(span[0] + where[0], span[0] + where[1])
        value.confidence = row.confidence(span[0] + where[0], span[0] + where[1])
        if nearby:
            value.confidence *= NEARBY_PENALTY
    value.unit = _unit_for(key, unit, rest)
    if not number:
        value.warnings.append(UNREADABLE)
        value.confidence = min(value.confidence, 0.15)
        return _flag(value)
    if repaired:
        value.confidence *= REPAIR_PENALTY
        value.warnings.append(REPAIRED)

    low, high = RANGES.get(measures, (float("-inf"), float("inf")))
    if not low <= float(number) <= high:
        # Kept as `raw` and shown, with nothing made of it. A reading outside
        # its own range is an OCR accident often enough that trusting it would
        # poison a trend, and a guess at what it should have been would be
        # worse than either.
        value.warnings.append(OUT_OF_RANGE)
        value.confidence = min(value.confidence, 0.2)
        return _flag(value)

    value.value = number
    return _flag(value)


def _unit_for(key: str, unit: str, rest: str) -> str:
    """The unit a reading is in, from the label rather than from the glyphs.

    `HEALTH` is a percentage whatever the camera made of the `%`, and
    `INTERNAL R` is in milliohms whether the omega came back as `Ω`, `Q`, `n`
    or nothing at all. The one printed unit worth reading is the rating
    standard on a CCA figure, because that one genuinely varies.
    """
    if key in ("measured", "rated"):
        printed = _fold(rest).replace(" ", "")
        for standard in STANDARDS:
            if printed.startswith(standard):
                return standard
    return unit


def _flag(value: Value) -> Value:
    value.confidence = round(max(0.0, min(1.0, value.confidence)), 4)
    if value.confidence < LOW_CONFIDENCE and LOW_CONF not in value.warnings:
        value.warnings.append(LOW_CONF)
    return value


#: Every verdict this file knows, as one pattern over the folded text. The
#: vocabulary is consulted **before** position, and that ordering is what makes
#: the verdict survive a banner OCR tore in half: on one sample `CRANKING TEST`
#: comes back as `TEST` on one line and `-CRANKING` on the next, so the first
#: line under the banner — which is where the verdict is printed — is a piece of
#: the banner. Looking for a word from the list finds `CRANKING LOW` two rows
#: further down, which is the answer.
KNOWN_VERDICT = re.compile(
    "|".join(sorted((re.escape(k) for k in VERDICTS), key=len, reverse=True))
)

#: Words that are part of a banner rather than a verdict, however the OCR broke
#: them up. Only used to reject a *fallback* candidate; a real verdict that
#: happens to start with one of these — `CRANKING LOW` — is found by the
#: vocabulary first.
BANNER_ONLY = {
    "TEST", "REPORT", "TESTREPORT", "BATTERY", "CRANKING", "CHARGING",
    "BATTERYTEST", "CRANKINGTEST", "CHARGINGTEST",
}


def _verdict(rows: list[Row]) -> Value | None:
    """The tester's own words for how the test came out.

    Never repaired. `GOOD` has a `G` in it and `G` is one of the characters
    that comes back as a `6`; a parser willing to fix that on a verdict would
    be willing to invent one. Folding is not repair — it compares a printed
    word against a known one without claiming the paper said anything else —
    and it costs the value some confidence when it is what found the answer.
    """
    start = 0
    for index, row in enumerate(rows):
        if SECTION.search(row.text):
            start = index + 1
            break

    candidates = [
        row
        for row in rows[start:]
        if row.text.strip()
        and not is_noise(row.text)
        and not _label_of(row.text)[0]
        and not TIMESTAMP.search(row.text)
    ]

    for row in candidates:
        folded = _fold(row.text)
        found = KNOWN_VERDICT.search(folded)
        if found:
            # Only charged for folding when folding is what found it. Most of
            # these read cleanly and were being docked a fifth of their
            # confidence for going through the same door.
            return _found_verdict(
                row,
                found.group(0),
                VERDICTS[found.group(0)],
                folded=folded != _plain_letters(row.text),
            )

    for row in candidates:
        if re.sub(r"[^A-Za-z]", "", row.text).upper() in BANNER_ONLY:
            continue
        text = row.text.strip()
        slug, folded = _category(text, VERDICTS)
        return _found_verdict(row, text, slug, folded=folded)
    return None


def _found_verdict(row: Row, raw: str, slug: str, *, folded: bool) -> Value:
    return _flag(
        Value(
            key="verdict",
            raw=row.text.strip(),
            value=slug,
            box=row.box(),
            confidence=row.confidence() * (FOLDED_PENALTY if folded else 1.0),
        )
    )


def _timestamp(rows: list[Row]) -> Value | None:
    """When the tester says it ran the test.

    Only the tester's own clock, in the one format it prints. Never the file
    name and never the EXIF capture time: those say when somebody took a
    photograph of a piece of paper, which on this corpus is seven months after
    the earliest test on it.

    A digit that cannot be read leaves the value empty and the characters
    visible. One of the five samples has an hour digit physically overprinted
    by the tear-off — the paper is damaged, not the reading — and the honest
    answer is to show what is there and say it is doubtful.
    """
    for row in reversed(rows):
        found = TIMESTAMP.search(row.text)
        if not found:
            continue
        raw = found.group(0)
        span = (found.start(), found.end())
        value = Value(
            key="performed_on",
            raw=raw,
            box=row.box(*span),
            confidence=row.confidence(*span),
        )
        parts, repaired = [], False
        for group in found.groups():
            number, fixed = _component(group)  # noqa: F841 - fixed feeds `repaired`
            parts.append(number)
            repaired = repaired or fixed
        if all(parts):
            try:
                value.value = datetime(*(int(p) for p in parts)).isoformat()
            except ValueError:
                value.value = ""
        if not value.value:
            value.warnings.append(UNREADABLE)
            value.confidence = min(value.confidence, 0.15)
        elif repaired:
            value.confidence *= REPAIR_PENALTY
            value.warnings.append(REPAIRED)
        return _flag(value)
    return None
