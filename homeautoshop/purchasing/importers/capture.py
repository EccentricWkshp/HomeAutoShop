"""
Capture a parts order as redacted text and geometry (SPEC §8.3a, NFR-S-5).

    python -m homeautoshop.purchasing.importers.capture

A RockAuto order confirmation is a shipping document, so it carries a **name, a
street address, a phone number and an email address** twice over, in the Ship To
and Bill To block. The parser needs none of it and the repository must never see
any of it — which is exactly the situation the scan-report corpus is already in,
and this follows the rule that one arrived at the hard way: *redact by rule, not
by list*, because a list of real values is itself the leak.

The rule here is positional and total. Everything printed between the `Ship To:`
line and the `Part Number` table header is the address block, and all of it is
dropped — rather than trying to recognise which words are a surname. What
survives is the order number, the date, the parts table and the totals, which is
the whole of what the parser reads.

The fixtures this writes are what `tests_rockauto.py` runs against, so the tests
exercise real documents with real wrapping and real kit rows, on any machine,
without the originals.
"""

from __future__ import annotations

import json
import pathlib
import re

from . import rockauto

CORPUS = pathlib.Path(__file__).resolve().parents[3] / "Artifacts" / "samples" / "parts-orders"
FIXTURES = pathlib.Path(__file__).resolve().parent / "fixtures"

START = re.compile(r"Ship\s*To:", re.I)
END = re.compile(r"Part\s+Number", re.I)

#: Belt and braces. If the positional rule ever fails to cover something, these
#: shapes are removed wherever they appear, so a capture cannot leak by silence.
EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
PHONE = re.compile(r"\b\d{10}\b|\b\d{3}[-. ]\d{3}[-. ]\d{4}\b")


class CouldNotRedact(RuntimeError):
    """The address block was not found, so nothing may be written.

    Refusing is the only safe answer. The first version of this searched each
    *word* for `Ship To:` — which is two words, so it never matched, the band
    was never found, and every capture was written out with a real name and
    street address in it. Caught by reading the output, which is not a control.
    Now the absence of the band is itself the failure.
    """


def _address_band(words: list[dict]) -> tuple[float, float]:
    """The vertical strip the Ship To / Bill To block occupies.

    Found by row, because both markers are more than one word.
    """
    rows = rockauto._rows(words)
    top = bottom = None
    for row in rows:
        text = " ".join(word["text"] for word in row)
        if top is None and START.search(text):
            top = row[0]["top"] - 1
        elif top is not None and END.search(text):
            bottom = row[0]["top"] - 1
            break
    if top is None or bottom is None:
        raise CouldNotRedact(
            "Could not find the Ship To block, so this document was not captured."
        )
    return top, bottom


def redact_words(words: list[dict], *, band: tuple[float, float] | None) -> list[dict]:
    kept = []
    for word in words:
        if band and band[0] <= word["top"] < band[1]:
            continue
        text = EMAIL.sub("someone@example.com", word["text"])
        text = PHONE.sub("5555550100", text)
        # Rounded, for two reasons. A fixture is read by people during a
        # regression, and `170.00027892985133` is noise; and a full-precision
        # float has enough digits in it to trip a search for a postcode, which
        # is a false alarm somebody then has to spend time disproving.
        kept.append(
            {
                "text": text,
                "x0": round(float(word["x0"]), 2),
                "x1": round(float(word["x1"]), 2),
                "top": round(float(word["top"]), 2),
            }
        )
    return kept


def redact_text(page: str) -> str:
    lines = page.splitlines()
    out, skipping = [], False
    for line in lines:
        if START.search(line):
            skipping = True
            continue
        if skipping and END.search(line):
            skipping = False
        if skipping:
            continue
        line = EMAIL.sub("someone@example.com", line)
        out.append(PHONE.sub("5555550100", line))
    return "\n".join(out)


def capture(path: pathlib.Path) -> dict:
    """Redacted text and geometry, or `CouldNotRedact`. Never a partial job."""
    with path.open("rb") as handle:
        text_pages, word_pages = rockauto.read_pdf(handle)

    # The block is on the first page. Every other page is the parts table and
    # the footer, and has no band to find — which must not read as a failure.
    bands = [_address_band(word_pages[0]), *([None] * (len(word_pages) - 1))]
    return {
        "source": path.name,
        "text": [redact_text(page) for page in text_pages],
        "words": [
            redact_words(words, band=band) for words, band in zip(word_pages, bands)
        ],
    }


def expected(order: rockauto.ParsedOrder) -> dict:
    """What the parser read, frozen — so a regression is visible as a diff."""
    return {
        "order_number": order.order_number,
        "ordered_on": order.ordered_on.isoformat() if order.ordered_on else None,
        "shipping_minor": order.shipping_minor,
        "tax_minor": order.tax_minor,
        "discount_minor": order.discount_minor,
        "total_minor": order.total_minor,
        "payment_method": order.payment_method,
        "subtotal_minor": order.subtotal_minor,
        "vehicles": order.vehicles,
        "warnings": order.warnings,
        "lines": [
            {
                "brand": line.brand,
                "part_number": line.part_number,
                "description": line.description,
                "unit_price_minor": line.unit_price_minor,
                "core_minor": line.core_minor,
                "quantity": str(line.quantity),
                "total_minor": line.total_minor,
                "is_kit_component": line.is_kit_component,
                "vehicle": line.vehicle,
            }
            for line in order.lines
        ],
    }


#: What the redaction puts in place of the real thing. Never flagged.
PLACEHOLDERS = ("someone@example.com", "5555550100")


def leaked(captured: dict) -> bool:
    """True when anything identifying survived. Text only, never coordinates."""
    words = [
        word["text"]
        for page in captured["words"]
        for word in page
        if word["text"] not in PLACEHOLDERS
    ]
    lines = [
        line
        for page in captured["text"]
        for line in page.splitlines()
    ]
    for value in [*words, *lines]:
        cleaned = value
        for placeholder in PLACEHOLDERS:
            cleaned = cleaned.replace(placeholder, "")
        if EMAIL.search(cleaned) or PHONE.search(cleaned):
            return True
    return False


def main() -> None:
    FIXTURES.mkdir(parents=True, exist_ok=True)
    found = sorted((CORPUS / "rockauto").glob("*.pdf"))
    if not found:
        print(f"No order PDFs in {CORPUS / 'rockauto'}")
        return

    for path in found:
        try:
            captured = capture(path)
        except CouldNotRedact as refusal:
            print(f"REFUSED {path.name}: {refusal}")
            continue
        # Checked against the *words*, not the serialized JSON: a coordinate
        # like 123.4567890123 contains ten consecutive digits, and the first
        # version of this guard duly refused every file in the corpus on the
        # strength of a decimal place.
        if leaked(captured):
            print(f"REFUSED {path.name}: something identifying survived redaction")
            continue
        blob = json.dumps(captured, indent=1, sort_keys=True)
        try:
            order = rockauto.parse_document(captured["text"], captured["words"])
        except rockauto.NotARockAutoOrder:
            print(f"skipped {path.name}: not a RockAuto order")
            continue

        stem = re.sub(r"\D", "", path.stem) or path.stem
        (FIXTURES / f"{stem}.capture.json").write_text(blob, encoding="utf-8")
        (FIXTURES / f"{stem}.expected.json").write_text(
            json.dumps(expected(order), indent=1, sort_keys=True), encoding="utf-8"
        )
        print(f"captured {path.name} -> {stem} ({len(order.lines)} lines)")


if __name__ == "__main__":
    main()
