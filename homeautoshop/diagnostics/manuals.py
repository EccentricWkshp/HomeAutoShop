"""
Reading a code table out of an online manual page, whatever shape it is in.

`build_dtc_list` reads a document somebody hands it and takes `--column` on
trust. That does not survive a crawl: every publisher lays its table out
differently, and one positional rule applied to seventy makes produces
definitions that say nothing while looking exactly like definitions. Three real
pages, three conventions:

* **VAG** — four columns, `SAE Code · VAG Code · Code Description · Corrective
  action`. Taking the first cell after the code yields `-`, which is the VAG
  column, for all 295 rows.
* **GM** — two columns, but a cell holds a *group*: `DTC P0601, P0603, P0604 or
  P062F`, and the description repeats the code back at you.
* **Ford** — no descriptions at all. The page is an index of 929 codes, each
  linking to its own page.

So the column is found from the **header row** rather than counted to, which is
a property of the table rather than of the publisher, and the same rule reads
all three. Where there is no header, the column carrying the most codes is the
code column — data, not position.

**Nothing unreadable is kept.** A description that is a dash, or punctuation,
or the code repeated back, is dropped and counted rather than stored: §8.3c
refuses invented wording because an operator acts on it, and a harvested `-` is
the same lie arrived at by machine.

**And nothing that merely looks like one.** Reading a whole library turned up
three further shapes that pass every test above and still say nothing about
what a code means:

* `ISO/SAE Reserved`, against every unassigned number in a manufacturer chart.
  True about the code, empty about its meaning, and 7% of one harvest.
  `Manufacturer Controlled DTC` is the same statement from the other side.
* `is for the right sunload sensor.` — prose *about* B0188, in a bullet opening
  with the code, so whatever follows it becomes the meaning.
* `Modeled exhaust temp, 300-900 °C ...` — the enable-conditions column of a
  seven-column VAG table, picked because its header says `Conditions`.
* `, P0462, and P0463 are Type B DTCs` — the tail of a sentence about four
  codes, cut at the first.

What is *not* refused is a definition that names another code. `C1293` means
`C1291 or C1292 set in current or previous ignition cycle`, and a rule tidy
enough to drop the fragments above takes that with it.
"""

from __future__ import annotations

import html as htmllib
import re
from dataclasses import dataclass, field

from . import transcription

CODE = re.compile(r"\b([PBCU][0-9][0-9A-F]{3})\b")
ROW = re.compile(r"<tr\b.*?</tr>", re.S | re.I)
CELL = re.compile(r"<t([dh])\b[^>]*>(.*?)</t\1>", re.S | re.I)
ITEM = re.compile(r"<li\b[^>]*>(.*?)(?=<li\b|</[uo]l>)", re.S | re.I)
LINK = re.compile(r"""<a[^>]*href=['"]([^'"]+)['"]""", re.I)

#: Column headers. `fault` and `meaning` are here because two libraries title
#: the column that way; `corrective action` is explicitly *not* a description —
#: it is advice about the repair, and putting it in a field that says what a
#: code means would be a different claim entirely.
CODE_HEADER = re.compile(r"\bcodes?\b|\bDTCs?\b", re.I)
TEXT_HEADER = re.compile(
    r"descript|definition|fault|meaning|condition|error|message|malfunction", re.I
)
#: Columns that say *when* a code sets rather than *what* it means. VAG
#: publishes seven of them — `DTC | Error Message | Diagnostic Procedure |
#: Malfunction Criteria and Threshold Value | Secondary Parameters with Enable
#: Conditions | Monitoring Time Length | Frequency of checks` — and the bare
#: word `condition` matched the fifth, so 195 Audi codes were defined as
#: `Modeled exhaust temp, 300-900 °C Delta engine load, <9% ...` while the real
#: definition sat unread in column two.
NOT_TEXT_HEADER = re.compile(
    r"correct|action|repair|cause|remedy|note"
    r"|enabl|criteri|threshold|paramet|monitor|frequen",
    re.I,
)

#: What a description has to be to be worth storing. Below this it is furniture.
SHORTEST = 4

#: A description that opens with punctuation is the back half of one. `, P0462,
#: and P0463 are Type B DTCs` is what remains when a sentence listing four
#: codes is cut at the first, and 67 of the 70 in one harvest were exactly
#: that. It is left as it was found rather than tidied, because trimming the
#: comma would leave a sentence that reads whole and still describes the wrong
#: code — the fragment is the evidence that it was cut.
FRAGMENT = re.compile(r"^[,;.:)\]]")

#: A description that says there is no description. `ISO/SAE Reserved` is a
#: true statement about the code and an empty one about its meaning, and a
#: manufacturer chart marks every unassigned number that way: 10,429 of the
#: 151,128 definitions in one library harvest said exactly this. Storing them
#: answers a lookup with the news that the standard has not got there yet, and
#: at the MAKE layer they would sit on top of whatever STANDARD does know.
NOTHING_SAID = re.compile(
    r"^(?:iso/sae\s+)?reserved\b"
    r"|^not\s+(?:detected|used|applicable|available|defined)\b"
    r"|^no\s+(?:description|definition|data)\b"
    r"|^n/?a$|^future\s+use\b|^unused\b"
    # Which half of the numbering a code falls in, against codes that have no
    # definition on the page. `dtc.parse` works that out from the number
    # itself and the STRUCTURE layer already says it in more words, so storing
    # it as a make's definition puts a worse answer in front of a better one.
    r"|^(?:manufacturer|iso/sae)\s+controlled\b",
    re.I,
)

#: Words a description cannot open with, because they only continue a sentence.
#: Manuals write prose *about* a code — `B0188 is for the right sunload sensor.`
#: as a bullet, `DTCs P0102 and P0103 are Type B DTCs.` as a paragraph — and a
#: reader that takes whatever follows a code makes that its meaning.
#:
#: Case-sensitive, and a closed list rather than a rule about grammar, because
#: both looser tests fail on real data: `In Vehicle Temperature Sensor` is a
#: definition, and so is `invalid Data Received From Image Processing Module
#: "A"`, so an opening lowercase letter proves nothing by itself. Every one of
#: the 277 entries this drops from that harvest was read before it was written.
CONTINUES = re.compile(
    r"^(?:is|are|was|were|and|or|but|will|would|shall|should|can|may|must"
    r"|with|in|on|at|to|of|for|from|that|which|when|if|then|also|refer)\b"
)


@dataclass
class Harvest:
    """What one page yielded."""

    codes: dict[str, str] = field(default_factory=dict)
    #: Codes the page names but does not define — an index of links.
    undefined: dict[str, str] = field(default_factory=dict)
    shape: str = "none"
    dropped: int = 0

    def __bool__(self) -> bool:
        return bool(self.codes or self.undefined)


def read(document: str) -> Harvest:
    """Every code and description on a manual page."""
    found = _from_tables(document)
    if found.codes:
        return found
    listed = _from_list(document)
    if listed.undefined or listed.codes:
        listed.dropped += found.dropped
        return listed
    # Nothing was read. Reporting "table" would claim a shape was recognised,
    # and the difference between a page with no codes on it and a page whose
    # shape this cannot read is the difference between a gap and a bug.
    return Harvest(
        shape=found.shape if found.dropped else "none", dropped=found.dropped
    )


# --------------------------------------------------------------------------
# Tables
# --------------------------------------------------------------------------


def _from_tables(document: str) -> Harvest:
    out = Harvest(shape="table")
    for rows in _tables(document):
        # One row is a table too. A headerless two-cell table is the simplest
        # shape any of these libraries uses, and requiring two rows skipped it.
        code_at, text_at = _columns(rows)
        if code_at is None or text_at is None:
            continue
        for cells in rows:
            if len(cells) <= max(code_at, text_at):
                continue
            codes = CODE.findall(cells[code_at])
            if not codes:
                continue
            for code, text in _split(cells[text_at], codes).items():
                if _usable(text, code):
                    out.codes[code.upper()] = text
                else:
                    out.dropped += 1
    return out


def _tables(document: str) -> list[list[list[str]]]:
    """Each table as a list of rows, each row a list of cell texts."""
    tables = re.split(r"<table\b", document, flags=re.I)[1:]
    out = []
    for block in tables:
        rows = []
        for row in ROW.findall(block.split("</table>", 1)[0]):
            cells = [_text(body) for _tag, body in CELL.findall(row)]
            if cells:
                rows.append(cells)
        if rows:
            out.append(rows)
    return out


def _columns(rows: list[list[str]]) -> tuple[int | None, int | None]:
    """Which column holds the code, and which the description.

    From the header where there is one. `SAE Code` and `VAG Code` both look
    like code columns by name, so the tie is settled on the data: the column
    that actually carries codes wins over the one that carries dashes.
    """
    width = max(len(r) for r in rows)
    header = rows[0] if not CODE.search(" ".join(rows[0])) else []
    body = rows[1:] if header else rows

    def named(pattern, avoid=None):
        return [
            i for i, cell in enumerate(header)
            if pattern.search(cell) and not (avoid and avoid.search(cell))
        ]

    def density(index):
        return sum(1 for r in body if len(r) > index and CODE.search(r[index]))

    candidates = named(CODE_HEADER) or list(range(width))
    code_at = max(candidates, key=density, default=None)
    if code_at is None or not density(code_at):
        return None, None

    wanted = named(TEXT_HEADER, avoid=NOT_TEXT_HEADER)
    text_at = wanted[0] if wanted else None
    if text_at is None:
        # No header to go on: the first column after the code that carries
        # prose rather than codes.
        for index in range(code_at + 1, width):
            if density(index) < len(body) / 2 and any(
                len(r) > index and len(r[index]) > SHORTEST for r in body
            ):
                text_at = index
                break
    return code_at, text_at


# --------------------------------------------------------------------------
# An index of codes with no descriptions
# --------------------------------------------------------------------------


def _from_list(document: str) -> Harvest:
    """A page that names codes and links each to its own page.

    Ford's index is 929 of these. They are recorded as *undefined* rather than
    stored with empty text, because "this make has a code P0010" and "this is
    what P0010 means on this make" are different facts and only the second is
    what a lookup is for.
    """
    out = Harvest(shape="index")
    for item in ITEM.findall(document):
        text = _text(item)
        match = CODE.match(text)
        if not match:
            continue
        code = match.group(1).upper()
        rest = _clean(text[match.end():], [code])
        if _usable(rest, code):
            out.codes[code] = rest
        else:
            href = LINK.search(item)
            out.undefined[code] = href.group(1) if href else ""
    return out


# --------------------------------------------------------------------------
# Cleaning up
# --------------------------------------------------------------------------


def _text(fragment: str) -> str:
    return transcription.tidy(htmllib.unescape(re.sub(r"<[^>]+>", " ", fragment)))


def _split(value: str, codes: list[str]) -> dict[str, str]:
    """One description per code, where a cell holds several.

    GM groups a row — `DTC P0601, P0603, P0604 or P062F` — and its description
    cell then runs all four definitions together, each opening with its own
    code. Handing the whole blob to every code in the group gives four rows
    that each say what the other three mean as well.

    Where the description names no codes at all, it is one description for
    whatever the row listed, which is the ordinary case.
    """
    text = " ".join(value.split())
    marks = [m for m in CODE.finditer(text) if m.group(1).upper() in {c.upper() for c in codes}]
    if len(marks) < 2:
        whole = _clean(text, codes)
        return {code: whole for code in codes}

    out: dict[str, str] = {}
    for index, mark in enumerate(marks):
        end = marks[index + 1].start() if index + 1 < len(marks) else len(text)
        # Back up over the label that introduces the next code, so it does not
        # land on the tail of this one.
        segment = re.sub(r"\bDTCs?\s*$", "", text[mark.start():end], flags=re.I)
        out[mark.group(1).upper()] = _clean(segment, [mark.group(1)])
    return out


def _clean(value: str, codes: list[str]) -> str:
    """Drop the label the description opens by repeating.

    GM writes `DTC P0601 Control Module Read Only Memory Performance` against
    the code `P0601`. Leaving the prefix in makes every row start by telling
    you what you already looked up.
    """
    text = value.strip()
    for _ in range(len(codes) + 1):
        stripped = re.sub(r"^(?:DTCs?\b|codes?\b)[\s:,-]*", "", text, flags=re.I)
        for code in codes:
            stripped = re.sub(rf"^{code}\b[\s:,.-]*", "", stripped, flags=re.I)
        if stripped == text:
            break
        text = stripped
    # Trailing punctuation is furniture; leading punctuation is evidence, so
    # only one end is trimmed and `FRAGMENT` gets to see the other.
    return " ".join(text.split()).lstrip(" -–—:·").rstrip(" -–—:·,;.")


def _usable(text: str, code: str) -> bool:
    """Whether this says anything.

    A dash is not a definition; neither is a note that the number is
    unassigned, nor the back half of a sentence that happened to open with a
    code. All three look like descriptions and none of them describe anything,
    which is §8.3c's problem reached by machine rather than by guessing.
    """
    if len(text) < SHORTEST or text.upper() == code.upper():
        return False
    if NOTHING_SAID.match(text) or CONTINUES.match(text) or FRAGMENT.match(text):
        return False
    return bool(re.search(r"[A-Za-z]{3}", text))
