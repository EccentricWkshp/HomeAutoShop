"""
Repairing what a transcription got wrong about *characters* — and nothing else.

A bundled code list is somebody's published wording, read out of a PDF or a web
page by machine. The wording is the fact being recorded and is never touched.
The bytes carrying it are a different matter: extractors drop encodings on the
floor, and what lands in the table is punctuation the publisher never wrote.

Three kinds of damage turn up, and only the first two are worth a rule:

* **Mis-decoded bytes.** TroubleCodes.net's Daihatsu list reads
  ``VVT control(Advance<A1><A2>retard angle fail)``. Those two bytes are
  EUC-JP for ``U+3001``, the ideographic comma — a Japanese-authored list run
  through a Latin-1 reader. That is decidable, so it is decided.
* **Typographic characters where ASCII was meant.** An en dash reads
  identically to a hyphen and is a different character to search for. A
  technician who types ``LH - voltage low`` should not miss the row because the
  publisher's typesetter used ``U+2013``.
* **Litter.** A stray ``U+00B7`` between two words is not punctuation, it is a
  glyph the extractor could not place. Volvo settles it: ``P1637`` and
  ``P1638`` are adjacent, identically worded, and one carries a bullet where
  the other carries a dash.

**What is not repaired matters as much.** ``Pass Key(R) II`` is a product name
and the symbol belongs to it; ``4x4`` and degrees and micro are real notation.
Those survive. And where damage is visible but *not* decidable — a lone
``<A3>`` in ``Throttle valve stuck<A3>-dirty block`` — the byte is dropped
rather than guessed at, because §8.3c refuses invented wording and inventing a
comma is still inventing.

Applied at transcription time by both importers, so a list is clean when it is
written rather than cleaned up afterwards, and asserted over the bundled tables
by :mod:`homeautoshop.diagnostics.tests_transcription`.
"""

from __future__ import annotations

import re
import unicodedata

#: Byte pairs that arrive as text because a reader guessed Latin-1 at an
#: encoding that was not. Decoded, not guessed: ``bytes([0xA1, 0xA2])`` is
#: ``U+3001`` in EUC-JP, and a Japanese comma between two alternatives is a
#: comma.
MIS_DECODED = {
    "¡¢": ",",  # EUC-JP A1 A2 -> U+3001 ideographic comma
    "¡£": ".",  # EUC-JP A1 A3 -> U+3002 ideographic full stop
    "¡¦": "/",  # EUC-JP A1 A6 -> U+30FB katakana middle dot
}

#: Typography that means exactly what its ASCII spelling means. The arrows are
#: here because Hyundai's ``P1613`` writes one direction as ``U+2190`` and the
#: other as ``->`` inside a single definition; spelling both the same way is
#: the smaller change.
TYPOGRAPHY = {
    "‐": "-", "‑": "-", "‒": "-", "–": "-",
    "—": "-", "―": "-", "−": "-",
    "‘": "'", "’": "'", "‚": "'", "‛": "'", "′": "'",
    "“": '"', "”": '"', "„": '"', "″": '"',
    "…": "...",
    "×": "x",
    "←": "<-", "→": "->",
    " ": " ", " ": " ", " ": " ", " ": " ",
    "​": "", "‌": "", "‍": "", "﻿": "",
}

#: Glyphs the extractor could not place, standing where a separator or a space
#: belongs. A bullet is a dash because Volvo writes the same sentence both
#: ways; a middle dot is a space because it only ever appears welded to a word
#: that already has one on the other side.
LITTER = {
    "•": "-", "‣": "-", "▪": "-", "●": "-", "◦": "-",
    "·": " ", "‧": " ",
}

#: Visible damage that cannot be read back to a character. Dropped rather than
#: guessed. Deliberately *not* in here: the degree sign, micro, plus-minus,
#: the ordinals and fractions, and the trademark marks -- those are notation a
#: publisher writes on purpose.
UNDECIDABLE = "¡¢£¤¥¦¨¬¯¸¿"

_UNDECIDABLE = re.compile(f"[{re.escape(UNDECIDABLE)}]")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")


def tidy(text: str) -> str:
    """One definition, with its characters repaired and its wording untouched.

    Idempotent, so running it over a table that has already had it applied
    changes nothing -- which is what lets the bundled lists be asserted clean
    rather than merely cleaned once.
    """
    if not text:
        return ""
    text = unicodedata.normalize("NFC", text)
    for wrong, right in MIS_DECODED.items():
        text = text.replace(wrong, right)
    for wrong, right in {**TYPOGRAPHY, **LITTER}.items():
        text = text.replace(wrong, right)
    text = _UNDECIDABLE.sub("", text)
    text = _CONTROL.sub(" ", text)
    # Collapse what the substitutions left behind: a dropped byte can strand a
    # doubled space, and litter standing next to real punctuation can strand a
    # space before a comma.
    text = " ".join(text.split())
    text = re.sub(r"\s+([,;:.)])", r"\1", text)
    text = re.sub(r"\(\s+", "(", text)
    return text.strip(" -:;,")


def untidy(text: str) -> list[str]:
    """The characters in `text` that :func:`tidy` would change.

    For reporting. A list that needs no repair reports nothing, which is a
    more useful thing to assert than "the output equals the input".
    """
    return sorted({ch for ch in text if tidy(ch) != ch and ch.strip()})
