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

* **VINs**, by two rules. Anything VIN-shaped whose ISO 3779 check digit
  validates, wherever it appears; and anything VIN-shaped that *follows a VIN
  label*, whatever its check digit says. The second rule is not belt and
  braces — position 9 is a check digit only where a regulator requires one, so
  a VIN issued in Europe carries a filler and fails the first rule outright.
  The manufacturer, model descriptor, model year and plant survive, because
  that is what makes a sample representative and it is shared with millions of
  vehicles; the serial, which is the part that identifies one car, is replaced
  by a digest of the original so re-capturing is stable.
* **Values a label identifies.** A licence plate, a customer, a technician, a
  shop name and address, a telephone number, an e-mail address — removed. A
  workshop code, a tester serial, a module coding id — zeroed, keeping the
  shape a parser matches on. Nothing about `raffi` says it is a person; the
  `User:` printed before it does.

Two rules about *where*, both learned by publishing something. In word
geometry a personal value runs to the end of its printed **line**, not one
word: blanking one word left four fifths of a shop's name in place. And a
label only labels what is beside it: the D8 emits `SN:` and then, three lines
further down the page, `Diagnosis`, so reading order alone zeroed a section
heading in all nine reports.

The make and model remain visible — they are in the file names and the report
bodies, and a corpus that hid them would not exercise make-specific parsing.
What no longer exists anywhere is a pointer to a specific vehicle.

A PDF is captured as word geometry; anything else is captured as its text,
because text is the whole of what a declarative profile reads. A tabular log
keeps its header and :data:`TABLE_ROWS` rows and says that it was cut.
"""

from __future__ import annotations

import hashlib
import json
import pathlib
import re

from homeautoshop.assets import vin as vinlib

from .fixtures import CORPUS

#: Seventeen VIN characters not run together with an eighteenth. Written with
#: lookarounds rather than `\b` because `\b` treats an underscore as a word
#: character, and two of the public samples are published as
#: `<VIN>_Aug_17_2025_LiveData` — so the boundary never matched, the VIN was
#: never replaced, and it went into a filename. Underscore separates a VIN from
#: what follows it exactly as a space does; only another VIN character means
#: this is not a VIN.
VIN_SHAPED = re.compile(r"(?<![A-Z0-9])[A-HJ-NPR-Z0-9]{17}(?![A-Z0-9])")
TOOL_SERIAL = re.compile(r"\bD8[-‑]\d{6}\b")

#: What a report calls the VIN, immediately before printing it.
#:
#: **The check digit is not enough on its own.** ISO 3779 position 9 is a check
#: digit only where a regulator requires one; a VIN issued in Europe carries a
#: filler there, so `WVWZZZ1KZAW422476` — a real Golf, in a real VCDS auto-scan
#: — fails the check and the rule that keys off it would have left it in place.
#: That was fine while every report in the corpus came from one North American
#: garage and stopped being fine the moment reports from the public web arrived.
#:
#: So there are two rules, not one: a token that satisfies its check digit is a
#: VIN wherever it appears, and a token that follows a VIN label is a VIN
#: whatever its check digit says. The first catches an unlabelled VIN in a
#: filename or a footer; the second catches every VIN issued outside North
#: America. A part number of the same shape is unaffected by either, because it
#: neither validates nor follows the word "VIN".
VIN_LABEL = r"(?:VIN(?:\s*(?:Code|No\.?|Number))?|Chassis\s*(?:No\.?|Number)|Frame\s*No\.?|Vehicle\s*Identification\s*(?:No\.?|Number))"

#: What separates a label from its value. `：` is U+FF1A, the fullwidth colon,
#: and it is not a curiosity — a TOPDON report writes `VIN：` and `Report
#: Number：` with it while writing `Mileage:` with an ASCII one, in the same
#: header. A rule that knows only the ASCII colon reads that report as having
#: no labels at all, so its VIN survived on the label rule's account and only
#: the check digit caught it. The next report's VIN might be European.
COLON = r"[:：#=]"
VIN_LABEL_BEFORE = re.compile(rf"(?i:{VIN_LABEL})\s*{COLON}?\s*$")

#: How far back a label can sit and still be labelling this value. Three words
#: covers `Vehicle Identification Number: <VIN>` in word geometry, where the
#: label and the value are separate words and nothing joins them.
LABEL_REACH = 3

# Synthetic VINs produced so far, so the redaction guard can tell a deliberate
# stand-in from a real vehicle. Only replacements are ever written here.
MANIFEST = CORPUS / "synthetic-vins.json"


def synthesise_vin(real: str) -> str:
    """A valid stand-in that keeps everything except which car it is."""
    digest = hashlib.sha256(real.encode("utf-8")).hexdigest()
    serial = f"{int(digest[:12], 16) % 1_000_000:06d}"
    body = real[:8] + "0" + real[9:11] + serial
    filler = real[8]
    if filler not in "0123456789X":
        # Not a check digit — a filler, which is what position 9 holds outside
        # North America. Recomputing one would turn a European VIN into a
        # North-American-shaped one and quietly delete from the corpus the very
        # case this application's VIN validation exists to tolerate (§5.5).
        return body[:8] + filler + body[9:]
    return body[:8] + vinlib.check_digit(body) + body[9:]


def redact(text: str, *, vin_expected: bool = False) -> tuple[str, set[str]]:
    """Rewrite anything identifying. Returns the text and the stand-ins used.

    `vin_expected` says a VIN label was seen just before this text and no
    longer appears in it — which is the situation in word geometry, where the
    label is one word and the value is the next. Callers reading whole lines do
    not need it; the label is in the string and this finds it.
    """
    produced: set[str] = set()

    def swap_vin(match: re.Match) -> str:
        candidate = match.group(0)
        labelled = vin_expected or bool(
            VIN_LABEL_BEFORE.search(text[max(0, match.start() - 48) : match.start()])
        )
        # A North American VIN satisfies its check digit; an arbitrary 17
        # characters of part number or calibration ID does not, and must not be
        # mangled. A labelled one is a VIN either way.
        if not labelled and not vinlib.validate(candidate).check_digit_valid:
            return candidate
        replacement = synthesise_vin(candidate)
        produced.add(replacement)
        return replacement

    text = VIN_SHAPED.sub(swap_vin, text)
    text = TOOL_SERIAL.sub(lambda m: m.group(0)[:3] + "000000", text)
    text = EMAIL.sub(MASK, text)
    return text, produced


# --------------------------------------------------------------------------
# Labelled values
# --------------------------------------------------------------------------

MASK = "[redacted]"

EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")

#: Labelled values that identify a person — the owner, the customer, the
#: technician, the shop. Removed outright, because unlike a VIN there is no
#: shape worth preserving and no parser here reads one.
PERSONAL_LABELS = (
    r"Licen[cs]e\s*Plate(?:\s*(?:No\.?|Number))?",
    r"Plate\s*(?:No\.?|Number)?",
    r"Registration(?:\s*(?:No\.?|Number))?",
    r"Repair\s*Order(?:\s*(?:No\.?|Number))?",
    r"Repair\s*Shop",
    r"Customer(?:\s*Name)?",
    r"Owner(?:\s*Name)?",
    r"Client(?:\s*Name)?",
    # A TOPDON report labels whoever ran the scan `User:` and prints their
    # first name. Nothing about the shape of `raffi` says it is a person; only
    # the label does, which is the argument for the label rules existing.
    r"User(?:\s*Name)?",
    r"Technician(?:\s*Name)?",
    r"Operator",
    # Not bare `Shop`, or the lookahead would be unnecessary: `Shop #` is a
    # VCDS workshop code and belongs to the equipment rules below, which keep
    # its shape instead of deleting it.
    r"Shop(?:\s*Name)?(?!\s*#)",
    r"Company",
    r"Address\d?",
    r"City",
    r"(?:Zip|Post(?:al)?)\s*Code",
    r"Tel(?:ephone)?(?:\s*(?:No\.?|Number))?",
    r"Phone\s*Number\s*of\s*Customer",
    r"Phone(?:\s*(?:No\.?|Number))?",
    r"E-?mail",
)

#: Labelled values that identify a piece of *equipment* — the tester, the
#: workshop code it is registered under, a module's coding id. Zeroed rather
#: than removed: the shape is what a parser matches on, and the existing rule
#: for the D8's own serial already works this way (`D8-123456` → `D8-000000`).
EQUIPMENT_LABELS = (
    r"Shop\s*#",
    r"WSC",
    r"VCID",
    r"Serial(?:\s*(?:No\.?|Number))?",
    r"S/N",
    r"S\.?N\.?",
    r"Importer",
    r"Equipment(?:\s*Number)?",
    r"Device\s*(?:No\.?|Number|Serial)",
    r"Tester\s*(?:No\.?|Number|Serial)?",
)


def _labelled(labels: tuple[str, ...]) -> re.Pattern:
    """`Label: value`, where the value ends at a column break or the line end.

    Two spaces end a value because these reports print two columns per line —
    `VIN: … License Plate: …` — and a value that ran to the line end would
    swallow the next label with it.

    Spacing is `[ \t]*` rather than `\\s*` throughout, which is the whole
    difference between this working and this being destructive. A VCDS
    auto-scan prints `Repair Order:` with nothing after it, and `\\s*` crossed
    the blank line below to find a value — so the label claimed the next
    non-empty line and a row of eighty dashes came out as `[redacted]`. An
    empty value is empty; it does not reach down the page for one.
    """
    joined = "|".join(labels)
    return re.compile(
        rf"((?i:{joined})[ \t]*{COLON}[ \t]*)([^\s\r\n][^\r\n]*?)(?=\s{{2,}}|[ \t]*(?:\r?\n|$))",
        re.M,
    )


PERSONAL_LINE = _labelled(PERSONAL_LABELS)
EQUIPMENT_LINE = _labelled(EQUIPMENT_LABELS)

#: The same labels, recognized at the *end* of the words already seen. Word
#: geometry splits a label from its value, so nothing joins them in one string
#: and a line-shaped rule matches nothing at all — the mistake the parts-order
#: capture made with `Ship To:`, which is two words, and shipped real names.
PERSONAL_BEFORE = re.compile(rf"(?i:{'|'.join(PERSONAL_LABELS)})[ 	]*{COLON}[ 	]*$")
EQUIPMENT_BEFORE = re.compile(rf"(?i:{'|'.join(EQUIPMENT_LABELS)})[ 	]*{COLON}[ 	]*$")


def _zeroed(value: str) -> str:
    """Every letter and digit to `0`, keeping spacing and punctuation."""
    return re.sub(r"[0-9A-Za-z]", "0", value)


def redact_document(text: str) -> tuple[str, set[str]]:
    """Redact a whole text report — labels and values together on a line.

    The line-shaped rules run first so that a VIN reached by its label is
    replaced by the VIN rule rather than blanked by the personal one, which
    would cost the corpus the VIN shape it exists to exercise.
    """
    text, produced = redact(text)
    text = PERSONAL_LINE.sub(lambda m: m.group(1) + MASK, text)
    text = EQUIPMENT_LINE.sub(lambda m: m.group(1) + _zeroed(m.group(2)), text)
    return text, produced


#: How far a personal value can run before this stops blanking regardless. A
#: backstop, not the rule — the rule is the line and the next label.
VALUE_WORDS = 12

#: Two words are on the same printed line if their tops agree to within this,
#: in whatever units the geometry uses. Two points is right for a PDF and
#: nonsense for a photograph: image geometry is pixels, a printed line is sixty
#: of them tall, and at two the answer is always *no*. Which matters here more
#: than anywhere — the redaction rules that protect a serial all work by asking
#: what is printed **beside** it, so a tolerance too small for the units does
#: not degrade the redaction, it switches it off.
LINE_TOLERANCE = 2.0


def line_tolerance(words: list[dict]) -> float:
    """Half a line of text, measured off the words themselves.

    Falls back to :data:`LINE_TOLERANCE` where the capture carries no
    `bottom` — a PDF's word geometry, which is where that constant came from
    and where it is still right.
    """
    heights = []
    for word in words:
        try:
            height = float(word.get("bottom", 0)) - float(word.get("top", 0))
        except (TypeError, ValueError):
            continue
        if height > 0:
            heights.append(height)
    if not heights:
        return LINE_TOLERANCE
    heights.sort()
    return max(LINE_TOLERANCE, 0.6 * heights[len(heights) // 2])


def redact_words(words: list[dict], *, tolerance: float | None = None) -> tuple[list[str], set[str]]:
    """Redact one page of word geometry, reading each word in its context.

    A word on its own cannot tell you it is a shop's name. What it follows can,
    so this carries a short window of the preceding words and lets a label in
    that window decide — the only way a rule about labelled values reaches a
    format where the label and the value are separate words.

    **A personal value is as long as it is.** Blanking one word after the label
    was the first attempt and it published a shop: `Shop Name: <name> vehicle
    testing center Al-ain` came out with four fifths of the name intact, which
    is worse than not redacting at all because the file then looks redacted. So
    blanking continues to the end of the printed line, and the *geometry* is
    what ends it — these headers put two labelled fields per line, and without
    the line there is nothing to stop the blanking running down the page.

    It stops early at the next label, and one word early when the word after
    this one is a label, so that `Tel: <number>  Test Time: …` keeps its
    `Test`. Over-blanking is the safe direction and it still costs something:
    every word taken is a word no fingerprint can match on.
    """
    produced: set[str] = set()
    near = line_tolerance(words) if tolerance is None else tolerance
    texts = [str(word.get("text", "")) for word in words]
    out: list[str] = []
    blanking = 0
    for index, text in enumerate(texts):
        # A label only labels what is printed *beside* it. Reading order is not
        # layout: the D8 emits `SN:` and then, three lines down the page,
        # `Diagnosis` — and a rule that trusted the word order alone zeroed the
        # heading of the section that names how the scan was run, in all nine
        # reports, on the strength of a label two lines above it.
        window = (
            " ".join(texts[max(0, index - LABEL_REACH) : index])
            if index and _same_line(words[index - 1], words[index], near)
            else ""
        )
        value, made = redact(text, vin_expected=bool(VIN_LABEL_BEFORE.search(window)))
        produced |= made

        if blanking and not _still_the_value(words, texts, index, blanking, near):
            blanking = 0

        if value == text and _is_value(text):
            if blanking:
                value, blanking = MASK, blanking - 1
            elif PERSONAL_BEFORE.search(window) and not _next_is_a_label(texts, index):
                # The lookahead matters on the *first* word too, not only when
                # continuing. A TOPDON report prints `Repair Shop:` with nothing
                # after it and `Report Number:` beside it, so blanking the first
                # word took `Report` — leaving `Number: TOPDON1709155693`, which
                # cost that tool's own profile its strongest fingerprint signal.
                value, blanking = MASK, VALUE_WORDS - 1
            elif EQUIPMENT_BEFORE.search(window):
                # One word. A serial is a token, and blanking the rest of the
                # line after one would take the label beside it.
                value = _zeroed(text)
        out.append(value)
    return out, produced


def _still_the_value(
    words: list[dict], texts: list[str], index: int, left: int, tolerance: float
) -> bool:
    """Is this word still part of the value that started a few words back?"""
    if left <= 0 or _is_label(texts[index]) or _next_is_a_label(texts, index):
        return False
    return _same_line(words[index - 1], words[index], tolerance)


def _next_is_a_label(texts: list[str], index: int) -> bool:
    """Is the word after this one a label, making this one part of *its* name?

    `Tel: <number>  Test Time: …` keeps its `Test` this way, and a label with
    an empty value keeps the label printed beside it.
    """
    return index + 1 < len(texts) and _is_label(texts[index + 1])


def _is_label(text: str) -> bool:
    return text.strip().endswith((":", "：", "#"))


def _same_line(first: dict, second: dict, tolerance: float = LINE_TOLERANCE) -> bool:
    """Were these two words printed beside each other?

    Compared by the **middle** of each word rather than its top, which is not
    fussiness: it is what decides whether a tester serial is redacted. On a
    real OCR capture of a BT600 Plus slip, `SN:` and the serial beside it have
    tops 24 pixels apart — the colon is a full cap height and the serial is
    digits — against a tolerance of 21, so they were not the same line, so no
    label preceded the serial, so it was written into the corpus in full.

    Their middles are 14 apart. Every rule here that protects a value works by
    asking what is printed beside it, so getting *beside* wrong does not
    weaken the redaction, it switches it off.
    """
    try:
        return abs(_middle(first) - _middle(second)) <= tolerance
    except (TypeError, ValueError):
        return False


def _middle(word: dict) -> float:
    top = float(word.get("top", 0) or 0)
    return (top + float(word.get("bottom", top) or top)) / 2.0


def _is_value(text: str) -> bool:
    """Something that could be a value rather than another label or a rule.

    A label whose value is empty is followed by the *next* label, and blanking
    that would delete the field name a parser matches on.
    """
    stripped = text.strip()
    return bool(stripped) and not _is_label(stripped) and stripped not in "-|"


class NothingToCapture(ValueError):
    """The file was read and holds no text a parser could ever see."""


def capture(pdf_path: pathlib.Path) -> tuple[dict, set[str]]:
    """Read a report into the plain structure the parser consumes."""
    from .xtool_d8 import words_from_pdf

    produced: set[str] = set()
    pages = []
    for page in words_from_pdf(str(pdf_path)):
        texts, made = redact_words(page)
        produced |= made
        pages.append(
            [
                {
                    "text": text,
                    "x0": round(float(word["x0"]), 2),
                    "x1": round(float(word["x1"]), 2),
                    "top": round(float(word["top"]), 2),
                    "color": _color(word.get("color")),
                }
                for word, text in zip(page, texts)
            ]
        )
    if not any(pages):
        # An image-only PDF — a scanned document, or a tool that renders its
        # report as a picture. §7.9 has always promised the OCR fallback for
        # exactly this, and the capture path was not using it: a Techstream
        # report scanned into a recall filing produced a capture with **zero
        # words**, which was written, committed and counted as a sample. A
        # fixture over nothing passes every test there is.
        raise NothingToCapture(
            f"{pdf_path.name} has no text layer — it is an image-only PDF"
        )
    return {"source": pdf_path.name, "pages": pages}, produced


#: Keys a captured word carries. `color` is a PDF's; `conf`, `bottom` and
#: `line` are a photograph's. Listed rather than copied wholesale so a future
#: OCR engine cannot smuggle an extra field into the committed corpus.
WORD_KEYS = ("text", "x0", "x1", "top", "bottom", "conf", "line")


def capture_photo(image_path: pathlib.Path) -> tuple[dict, set[str]]:
    """Read a photographed printout into redacted word geometry (§7.9).

    The same trade as a PDF, for the same reason: what is committed is words
    and positions, not the photograph. The photograph is somebody's driveway,
    somebody's bench, and — on a battery slip — a tester serial that identifies
    a specific piece of equipment. The words are the whole of what a parser
    ever sees, so the corpus loses nothing a test could have used.

    JPEGs are already excluded from the repository by `.gitignore`; this is
    what makes that exclusion survivable rather than merely tidy.
    """
    from homeautoshop.mediafiles.services import image_size, read_image_words

    raw = image_path.read_bytes()
    pages = read_image_words(raw)
    if not any(pages):
        raise NothingToCapture(
            f"{image_path.name} yielded no words — is OCR_ENABLED set, and is "
            f"tesseract installed?"
        )

    produced: set[str] = set()
    out = []
    for page in pages:
        texts, made = redact_words(page)
        produced |= made
        out.append(
            [
                {**{k: word[k] for k in WORD_KEYS if k in word}, "text": text}
                for word, text in zip(page, texts)
            ]
        )
    return (
        {
            "source": image_path.name,
            "media_type": "image",
            "read_by": "ocr",
            # As OCR saw it — rotated upright and capped, not as the camera
            # wrote it. Every box in this capture is measured in this frame,
            # and a reader with the box and not the frame has a rectangle in
            # unknown units.
            "image_size": list(image_size(raw)),
            "pages": out,
        },
        produced,
    )


def capture_by_ocr(pdf_path: pathlib.Path) -> tuple[dict, set[str]]:
    """Read an image-only PDF the only way there is to read one.

    OCR loses the geometry, so this yields a *text* capture. That is honest
    about what survived: a parser reading this is reading what a program
    guessed the picture said, and a profile written against word positions
    would have nothing to stand on.
    """
    from homeautoshop.mediafiles.services import read_pdf_text_by_ocr

    raw = pdf_path.read_bytes()
    text = read_pdf_text_by_ocr(raw)
    if not text.strip():
        raise NothingToCapture(
            f"{pdf_path.name} has no text layer, and OCR read nothing from it "
            f"(is OCR_ENABLED set, and is tesseract installed?)"
        )
    kept, truncated = _trim(text, "text")
    text, produced = redact_document(kept)
    capture: dict = {
        "source": pdf_path.name,
        "media_type": "text",
        "read_by": "ocr",
        "text": text,
    }
    if truncated:
        capture["truncated"] = True
    return capture, produced


#: A tabular log is a header and then ten thousand rows of the same columns.
#: Eighty prove the format exactly as well as the whole file does, and the whole
#: file is sometimes 38 MB — the live-data exports fetched from the public web
#: come to 336 MB between them, which is not a thing to put in a git history to
#: demonstrate that a comma separates two numbers.
TABLE_ROWS = 80

#: A report is not a table: every line of it is different and the last page
#: tests the parser as hard as the first, so it is kept whole. The ceiling is
#: only there to stop a 3.7 MB CAN trace that happens not to be comma-separated
#: from arriving by the other door.
MAX_LINES = 3_000
MAX_CHARS = 256 * 1024


def capture_document(path: pathlib.Path) -> tuple[dict, set[str]]:
    """Read a text report — an auto-scan log, a CSV export — into a capture.

    Text has no geometry, so the capture is the text: what the declarative
    engine reads is `Document.text` and nothing else. Keeping it as JSON rather
    than as the original file is not ceremony — it is what makes the capture
    obviously *derived and redacted* rather than a copy of somebody's report
    with a different name.
    """
    from homeautoshop.diagnostics import engine

    raw = path.read_bytes()
    media_type = engine.media_type(raw, filename=path.name)
    whole = raw.decode("utf-8", errors="replace")
    kept, truncated = _trim(whole, media_type)
    text, produced = redact_document(kept)

    capture: dict = {"source": path.name, "media_type": media_type, "text": text}
    if truncated:
        # Said in the file, because a reader comparing a capture against the
        # original needs to know the difference is deliberate. A capture that
        # was quietly shortened is a fixture that quietly stopped covering the
        # end of the report.
        capture["truncated"] = True
        capture["source_lines"] = whole.count("\n") + 1
        capture["source_bytes"] = len(raw)
    return capture, produced


def _trim(text: str, media_type: str) -> tuple[str, bool]:
    lines = text.splitlines(keepends=True)
    limit = TABLE_ROWS + 1 if media_type == "csv" else MAX_LINES
    kept = lines[:limit]
    truncated = len(kept) < len(lines)
    joined = "".join(kept)
    if len(joined) > MAX_CHARS:
        joined, truncated = joined[:MAX_CHARS], True
    return joined, truncated


def _color(value):
    if value is None:
        return None
    try:
        return [round(float(channel), 4) for channel in value]
    except TypeError:
        return [round(float(value), 4)]


def capture_path(source: pathlib.Path, tool: str = "", *, kind: str = "") -> pathlib.Path:
    """Where a capture belongs: under its tool's folder in the corpus.

    The corpus is filed one folder per scanner, because a flat pile of reports
    stops saying what produced them the moment there is a second tool — the
    same convention the parts-order corpus already uses. `tool` defaults to
    empty only so an older caller keeps working; a capture at the root is
    still found, since the walk is recursive, and still says nothing about
    where it came from.

    The suffix says what kind of capture it is, because the two are read
    differently and a reader should not have to open one to find out: a PDF
    keeps its word geometry, everything else keeps its text. `kind` overrides
    the guess for the one case where the extension lies about the outcome — a
    PDF with no text layer is read by OCR, which produces text and no geometry.
    """
    folder = CORPUS / tool if tool else CORPUS
    return folder / (source.stem + suffix_for(source, kind=kind))


#: Which capture kind keeps geometry. A photograph does, now that OCR is asked
#: for words rather than for a string — the whole reason the BT600 Plus can be
#: read at all is that its labels and its values are in two columns and the
#: parser can see that they are.
GEOMETRIC = ("pdf", "image")


def suffix_for(source: pathlib.Path, *, kind: str = "") -> str:
    if kind:
        return ".words.json" if kind in GEOMETRIC else ".text.json"
    return (
        ".words.json"
        if source.suffix.lower() in (".pdf", ".jpg", ".jpeg", ".png", ".webp", ".heic")
        else ".text.json"
    )


def synthetic_vins() -> set[str]:
    if not MANIFEST.exists():
        return set()
    return set(json.loads(MANIFEST.read_text(encoding="utf-8")))


def remember(produced: set[str]) -> int:
    """Record stand-ins so the redaction guard knows them for what they are.

    Every capture path has to do this, and only the developer script did — so
    a fixture written through `capture_fixture` produced a stand-in that no
    file recorded, and the guard that checks the corpus for real VINs would
    have reported the replacement as one. The guard was right to; the manifest
    is the only thing that tells a stand-in from a stranger's vehicle.
    """
    everything = synthetic_vins() | set(produced)
    MANIFEST.write_text(
        json.dumps(sorted(everything), indent=1) + "\n", encoding="utf-8"
    )
    return len(everything)


def write(source: pathlib.Path, tool: str = "") -> tuple[pathlib.Path, set[str]]:
    """Capture one report into the corpus, whatever kind of file it is.

    A PDF with no text layer falls through to OCR rather than being written as
    an empty capture, and if OCR reads nothing either this raises — so the
    caller reports the report as unreadable instead of the corpus quietly
    gaining a sample that says nothing.
    """
    # Decided from the content, like everywhere else here. A file fetched from
    # the web has whatever extension its publisher chose, and one of them
    # serves a PDF under a name that says otherwise.
    from homeautoshop.diagnostics import engine

    head = source.open("rb").read(64)
    if head[:5] == b"%PDF-":
        try:
            data, produced = capture(source)
        except NothingToCapture:
            data, produced = capture_by_ocr(source)
    elif engine.media_type(head, filename=source.name) == "image":
        data, produced = capture_photo(source)
    else:
        data, produced = capture_document(source)
    target = capture_path(source, tool, kind=data.get("media_type", ""))
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(data, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    return target, produced


def verify_synthetic_vins() -> list[str]:
    """A stand-in that fails validation would weaken the corpus silently."""
    problems = []
    for candidate in sorted(synthetic_vins()):
        check = vinlib.validate(candidate)
        if not check.is_well_formed:
            problems.append(f"{candidate} is not a well-formed VIN")
        elif candidate[8] in "0123456789X" and not check.check_digit_valid:
            # Only where position 9 is a check digit at all. A stand-in for a
            # European VIN keeps the filler it found there, so demanding a
            # valid check digit of every stand-in would fail the ones that are
            # most faithful to what they replaced.
            problems.append(f"{candidate} check digit should be {vinlib.check_digit(candidate)}")
    return problems


if __name__ == "__main__":  # pragma: no cover - developer tool
    import sys

    CAPTURABLE = (".pdf", ".jpg", ".jpeg", ".png", ".webp", ".heic")
    targets = [pathlib.Path(a) for a in sys.argv[1:] if a.lower().endswith(CAPTURABLE)]
    # Recursive, because the corpus is filed one folder per tool and the
    # photographs of a battery tester's paper are three levels down.
    targets = targets or sorted(
        path for suffix in CAPTURABLE for path in CORPUS.rglob("*" + suffix)
    )
    if not targets:
        print(f"nothing capturable found in {CORPUS}")
        raise SystemExit(1)

    everything: set[str] = set()
    for original in targets:
        folder = original.parent.name if original.parent != CORPUS else ""
        target, produced = write(original, folder)
        everything |= produced
        print(f"captured {target.name}" + (f" (redacted {len(produced)})" if produced else ""))

    print(f"{remember(everything)} synthetic VINs recorded in {MANIFEST.name}")
