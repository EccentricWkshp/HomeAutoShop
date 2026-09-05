"""How much is in the container, read off the container's own description.

A vendor sells a *package* and the shop consumes what is *in* it. NAPA charges
$182.39 for `CRC Brakleen ... 5 gal (US)` at `Qty: 1`, and the shop has five
gallons of brake cleaner to spray, not one drum to install. Without the size,
the line arrives as one of something at $182.39 and the shelf can never say how
much cleaner is left — which is the whole point of stocking it.

**Only measured sizes are read here, never bare counts.** `5 gal`, `8 oz` and
`1 US Gal` are read; `2Pcs`, `Case of 4` and `3-Pack` are deliberately not, and
that line is the same one FR-PUR-12 already drew. A bare count in a product
title is marketing copy — it may be the pack, or the pin count, or the number of
vehicles it fits — and counting it doubles somebody's shelf silently. A number
with a *unit of measure* on it is not that: nothing writes `5 gal` on a pail
that does not hold five gallons, because the volume is regulated and the count
is a headline.

So a two-pack of relays still arrives as one line to be counted by hand, and a
five-gallon pail arrives as five gallons — and neither rule had to bend for the
other.

Everything here only *proposes*: the review screen shows what was read and the
operator confirms or corrects it before anything is written.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

#: Spellings seen on the documents this reads, mapped to the catalog's own unit
#: codes (`PART_UNITS`). Longest first at match time, so `fl oz` never loses to
#: `oz` and `US Gal` never loses to `gal`.
UNIT_WORDS: dict[str, str] = {
    "gal": "gal",
    "gals": "gal",
    "gallon": "gal",
    "gallons": "gal",
    "qt": "qt",
    "qts": "qt",
    "quart": "qt",
    "quarts": "qt",
    "l": "L",
    "liter": "L",
    "liters": "L",
    "litre": "L",
    "litres": "L",
    "ml": "ml",
    "milliliter": "ml",
    "milliliters": "ml",
    "lb": "lb",
    "lbs": "lb",
    "pound": "lb",
    "pounds": "lb",
    "kg": "kg",
    "kilogram": "kg",
    "kilograms": "kg",
    "g": "g",
    "gram": "g",
    "grams": "g",
}

#: Read apart from the table above because they have to beat it. `fl oz` shares
#: its tail with `oz` and `US Gal` shares its tail with `gal`, so an alternation
#: that reached `oz` first would read `16 fl oz` as sixteen *mass* ounces.
FLUID_OUNCE = r"fl\.?\s*\.?\s*oz\.?|fluid\s+ounces?"

#: A bare ounce, which the document does not disambiguate and this reads as
#: **fluid** ounces.
#:
#: A guess, and stated as one. What gets stocked by the ounce in a home shop is
#: what comes in a bottle — assembly lube, dye, additive, oil — and a mass ounce
#: belongs to hardware nobody stocks by weight. Guessing the other way would be
#: wrong every time somebody reads in the eight-ounce bottle of oil this was
#: written for. It is a dropdown away on the review screen either way, which is
#: why a guess is allowed to be made here at all.
BARE_OUNCE = r"ounces?|ozs?\.?"

_NUMBER = r"(?P<qty>\d+(?:[.,]\d+)?)"
#: Spaces, dashes, or nothing at all — `5 gal`, `5-gal` and `10ml` are all the
#: same statement. Written as one character class rather than an optional
#: group: `rf"{sep}?"` around a pattern ending in `\s*` puts the `?` on that
#: trailing `\s*` and makes it lazy instead of making the separator optional,
#: which reads identically and silently stops matching `10ml`.
_SEP = r"[\s\-]*"

SIZE = re.compile(
    rf"(?<![\w.]){_NUMBER}{_SEP}"
    rf"(?:us\s+|u\.s\.\s+|imperial\s+)?"
    rf"(?P<unit>{FLUID_OUNCE}|{BARE_OUNCE}|{'|'.join(sorted(UNIT_WORDS, key=len, reverse=True))})"
    rf"(?![\w])",
    re.IGNORECASE,
)

#: Words that make a number a count rather than a measure. Present so that
#: `2Pcs` and `Case of 4` cannot be mistaken for a size by some future spelling
#: of the pattern above; the pattern does not match them today either.
COUNT_WORDS = re.compile(r"\b(?:pcs?|pieces?|pack|pk|count|ct|case|box|set)\b", re.IGNORECASE)


def read_size(text: str) -> tuple[Decimal, str] | None:
    """`(quantity, unit code)` the description states, or `None`.

    The **last** match wins. A description runs from the general to the
    specific — `CRC Brakleen Brake Parts Cleaner Non-Flammable Chlorinated
    5 gal (US)` — and where two sizes appear the trailing one is the package,
    the leading one part of the product's name (`5W-30 1 Quart`).
    """
    if not text:
        return None

    found = None
    for match in SIZE.finditer(text):
        unit = _code(match.group("unit"))
        if unit is None:
            continue
        try:
            quantity = Decimal(match.group("qty").replace(",", "."))
        except InvalidOperation:
            continue
        if quantity <= 0:
            continue
        found = (quantity, unit)
    return found


def _code(word: str) -> str | None:
    cleaned = word.strip().lower().rstrip(".")
    if re.fullmatch(FLUID_OUNCE, word.strip(), re.IGNORECASE):
        return "floz"
    if re.fullmatch(BARE_OUNCE, word.strip(), re.IGNORECASE):
        return "floz"
    return UNIT_WORDS.get(cleaned)
