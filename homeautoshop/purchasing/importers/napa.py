"""
Reading a NAPA order history page (SPEC FR-PUR-1, §8.3a).

Not an emailed confirmation like RockAuto's — this is the *Order History
Details* screen printed to PDF from a browser, which is what a NAPA customer
actually has. It reads very differently and the difference matters:

**The document states what each line was charged.** RockAuto prints a price
each and leaves the multiplication to the reader; NAPA prints

    $182.39                <- what the line cost
    $227.99 /Drum(s)       <- list price, per whatever they sell it by
    - $45.60 Save Up to 20%  <- what came off it

and $227.99 − $45.60 = $182.39. The charged figure is taken directly, which is
the whole reason `OrderLine.extended_minor` exists: a five-gallon drum sold for
$182.39 has a per-gallon price of $36.478, and rebuilding the total from a
per-unit price rounded to the cent invents a penny that nobody paid.

**And it is the evidence for the tax rule.** Its own summary reads

    Subtotal(2 items): $228.05
    Tax:                $18.74
    Napa Rewards Discount: -$5.00
    Total:              $241.79

8.4% of $228.05 is $19.16. It is 8.4% of $223.05 — the subtotal *after* the
discount — that gives $18.74. The vendor takes the discount off before working
out the tax, which is what `Purchase.taxable_minor` now does, and this document
is where that was confirmed rather than assumed.

**Layout.** One column, not a table. Each item is a run of printed rows anchored
by `Part # <BRAND> <NUMBER>`, with the description wrapping above it and the
quantity, price, list price and discount below. Anchoring on `Part #` and
walking outward is what makes a wrapped description land on its own item — the
same reason the RockAuto reader works by geometry rather than reading order,
arrived at from the opposite direction.

Nothing here writes anything. It returns what it read and a person confirms it.
"""

from __future__ import annotations

import io
import logging
import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from .orders import OrderLine, ParsedOrder

log = logging.getLogger(__name__)

VENDOR_NAME = "NAPA Auto Parts"
VENDOR_URL = "https://www.napaonline.com"
SOURCE = "napa"

#: The browser writes the page title into the PDF, and it is the one unambiguous
#: statement of what this document is. The structural markers below stand in
#: where a title has been stripped — together they are specific enough, and
#: neither is a phrase that turns up in a RockAuto confirmation.
TITLE = re.compile(r"NAPA\s+Auto\s+Parts", re.I)
STRUCTURE = ("Ordered On:", "Order Summary", "Part #")

ORDER_NUMBER = re.compile(r"Order\s*#\s*(\d{4,})")
ORDERED_ON = re.compile(r"Ordered\s+On:\s*([A-Z][a-z]{2})\s+(\d{1,2})\s*,\s*(\d{4})")
#: `Picked up Aug 31` — no year, because the page is showing you this year's
#: orders. Resolved against the order date below.
PICKED_UP = re.compile(r"Picked\s+up\s+([A-Z][a-z]{2})\s+(\d{1,2})")

PART_LINE = re.compile(r"^Part\s*#\s*(?P<brand>\S+)\s+(?P<number>.+?)\s*$")
QTY_LINE = re.compile(r"^Qty:\s*(?P<qty>[\d.]+)\s*$")
MONEY_ONLY = re.compile(r"^\$\s*(?P<amount>[\d,]+\.\d{2})\s*$")
LIST_PRICE = re.compile(r"^\$\s*(?P<amount>[\d,]+\.\d{2})\s*/(?P<unit>.+?)\s*$")
LINE_DISCOUNT = re.compile(r"^-\s*\$\s*(?P<amount>[\d,]+\.\d{2})\b")

SUBTOTAL = re.compile(r"^Subtotal\s*\([^)]*\)\s*:\s*\$\s*(?P<amount>[\d,]+\.\d{2})")
TAX = re.compile(r"^Tax:\s*\$\s*(?P<amount>[\d,]+\.\d{2})")
DISCOUNT = re.compile(r"^(?P<label>.*?Discount)\s*:\s*-\s*\$\s*(?P<amount>[\d,]+\.\d{2})")
SHIPPING = re.compile(r"^(?:Shipping|Delivery)\s*:\s*\$\s*(?P<amount>[\d,]+\.\d{2})")
TOTAL = re.compile(r"^Total:\s*\$\s*(?P<amount>[\d,]+\.\d{2})")
PAYMENT = re.compile(r"^(?P<method>[A-Za-z][A-Za-z ]{1,20}?)\s+ending\s+in\s+(\d{4})\s*$")

#: Page furniture printed in the right margin, well outside the item column.
#: `Feedback` lands in the middle of an item block in reading order and is not
#: part of it.
MARGIN_X = 520.0

#: How many rows a product name is allowed to wrap over.
#:
#: A second bound on the walk upward, independent of the spacing one, because
#: relying on a single signal here is how the first item ate the page banner.
#: The gap rule is the better of the two and it only works on a page that
#: *has* a gap; this one holds on a page laid out evenly. Four is generous —
#: the longest name in the sample wraps over two.
MOST_WRAPS = 4


class NotANapaOrder(ValueError):
    """The file is a PDF, and it is not one of these."""


def _minor(text: str | None) -> int:
    if not text:
        return 0
    try:
        return int((Decimal(text.replace(",", "").replace("$", "").strip()) * 100).to_integral_value())
    except (InvalidOperation, ValueError):
        return 0


def _rows(words: list[dict], tolerance: float = 3.0) -> list[tuple[float, str]]:
    """The words grouped back into the rows they were printed on, with the
    vertical position kept.

    `extract_text` already groups them, and two things it does not do are both
    needed here. It keeps the margin furniture in reading order, so `Feedback`
    arrives in the middle of the second item; and it throws away the geometry,
    which is what tells an item's own wrapped description from the page header
    printed above it.
    """
    kept = [w for w in words if w.get("x0", 0) < MARGIN_X]
    # Grouped by baseline first and ordered left-to-right *afterwards*. Sorting
    # by `(top, x0)` up front looks equivalent and is not: Amazon sets `1 of:`
    # and the price on one baseline and the product title 1.8pt below it, so a
    # rounded top puts the price ahead of the title it belongs after, and the
    # row reads `1 of: $8.29 TSI Supercool ...`. The price then never matches
    # at the end of the row and the whole invoice reads as having no items.
    rows: list[tuple[float, list[dict]]] = []
    for word in sorted(kept, key=lambda w: w["top"]):
        if rows and abs(rows[-1][0] - word["top"]) <= tolerance:
            rows[-1][1].append(word)
        else:
            rows.append((word["top"], [word]))
    return [
        (top, " ".join(w["text"] for w in sorted(group, key=lambda w: w["x0"])))
        for top, group in rows
    ]


def _read_pdf(raw: bytes) -> tuple[str, list[tuple[float, str]]]:
    import pdfplumber

    rows: list[tuple[float, str]] = []
    title = ""
    with pdfplumber.open(io.BytesIO(raw)) as pdf:
        title = (pdf.metadata or {}).get("Title", "") or ""
        for page in pdf.pages:
            rows.extend(_rows(page.extract_words(keep_blank_chars=False)))
    return title, rows


def parse(source) -> ParsedOrder:
    raw = source.read() if hasattr(source, "read") else source
    try:
        title, rows = _read_pdf(raw)
    except NotANapaOrder:
        raise
    except Exception as exc:  # noqa: BLE001 - a broken PDF is a refusal, not a crash
        raise NotANapaOrder("that file could not be read as a PDF") from exc
    return parse_document(title, rows)


def parse_document(title: str, rows: list[tuple[float, str]]) -> ParsedOrder:
    """The half that takes no PDF, so the fixtures can exercise it.

    `rows` is `(vertical position, text)`, because where a row sits is part of
    what it means here — see `_read_lines`.
    """
    rows = [(float(top), text) for top, text in rows]
    body = "\n".join(text for _top, text in rows)
    if not TITLE.search(title or "") and not all(m in body for m in STRUCTURE):
        raise NotANapaOrder("this is not a NAPA order history page")

    order = ParsedOrder(
        vendor_name=VENDOR_NAME, vendor_url=VENDOR_URL, source=SOURCE
    )

    if found := ORDER_NUMBER.search(body):
        order.order_number = found.group(1)
    if found := ORDERED_ON.search(body):
        order.ordered_on = _date(*found.groups())
    if order.ordered_on and (found := PICKED_UP.search(body)):
        order.received_on = _same_year_as(found.group(1), found.group(2), order.ordered_on)

    _read_totals(rows, order)
    order.lines = _read_lines(rows)

    if not order.lines:
        raise NotANapaOrder("no order lines were found on this page")

    _check(order)
    return order


def _date(month: str, day: str, year: str) -> date | None:
    try:
        return datetime.strptime(f"{month} {day} {year}", "%b %d %Y").date()
    except ValueError:
        return None


def _same_year_as(month: str, day: str, ordered: date) -> date | None:
    """`Picked up Aug 31` against an order date, since the page omits the year.

    Rolls forward when the result lands before the order: an order placed on
    December 30th and collected on January 2nd is not collected the previous
    January.
    """
    when = _date(month, day, str(ordered.year))
    if when is None:
        return None
    if when < ordered:
        when = _date(month, day, str(ordered.year + 1))
    return when


def _read_totals(rows: list[tuple[float, str]], order: ParsedOrder) -> None:
    for _top, row in rows:
        if found := SUBTOTAL.match(row):
            order.stated_subtotal_minor = _minor(found.group("amount"))
        elif found := TAX.match(row):
            order.tax_minor = _minor(found.group("amount"))
        elif found := SHIPPING.match(row):
            order.shipping_minor = _minor(found.group("amount"))
        elif found := TOTAL.match(row):
            order.total_minor = _minor(found.group("amount"))
        elif found := DISCOUNT.match(row):
            # Named, because "Napa Rewards Discount" and a promotional code are
            # different things to the person checking the screen even though
            # they are the same number to the arithmetic.
            amount = _minor(found.group("amount"))
            order.discount_minor += amount
            order.adjustments.append((found.group("label").strip(), amount))
        elif found := PAYMENT.match(row):
            order.payment_method = found.group("method").strip()


def _read_lines(rows: list[tuple[float, str]]) -> list[OrderLine]:
    """Anchored on `Part #`, reading outward.

    The description wraps across as many rows as it needs and the price block
    follows, so neither can be found by counting rows from the top. The part
    number can: it is the one row in each item whose shape is unmistakable.

    Walking up is bounded **by the spacing of the page**, not by a row count.
    An item's own rows sit about fifteen points apart; the gap between the last
    thing above the first item and that item is nearly double. Without the
    bound the first item swallowed the promotional banner printed across the
    top of the page and went into the catalog named after a discount code —
    which is the reading-order failure this file avoids everywhere else,
    arriving from above instead of below.
    """
    anchors = [i for i, (_top, row) in enumerate(rows) if PART_LINE.match(row)]
    lines: list[OrderLine] = []
    spacing = _spacing(rows)

    for position, anchor in enumerate(anchors):
        found = PART_LINE.match(rows[anchor][1])
        line = OrderLine(
            brand=found.group("brand"),
            part_number=found.group("number").strip(),
        )

        floor = anchors[position - 1] if position else -1
        head = []
        for index in range(anchor - 1, max(floor, anchor - 1 - MOST_WRAPS), -1):
            top, row = rows[index]
            if _is_item_field(row):
                break
            if rows[index + 1][0] - top > spacing * 1.6:
                # A gap wider than this page's own line spacing: whatever is
                # above belongs to something else.
                break
            head.append(row)
        line.description = _join(reversed(head))

        end = anchors[position + 1] if position + 1 < len(anchors) else len(rows)
        seen_qty = False
        for _top, row in rows[anchor + 1:end]:
            if qty := QTY_LINE.match(row):
                if not seen_qty:
                    # Printed twice — ordered and collected. The first is the
                    # one this line was charged for.
                    line.quantity = Decimal(qty.group("qty"))
                    seen_qty = True
            elif money := MONEY_ONLY.match(row):
                if line.extended_minor is None:
                    line.extended_minor = _minor(money.group("amount"))
            elif listed := LIST_PRICE.match(row):
                line.list_price_minor = _minor(listed.group("amount"))
                line.sold_as = listed.group("unit").strip()
            elif off := LINE_DISCOUNT.match(row):
                line.line_discount_minor = _minor(off.group("amount"))

        if line.extended_minor is None and line.list_price_minor is not None:
            # A line with no charged figure of its own: the list price less
            # whatever came off it is the honest reconstruction, and it is
            # exact because both are amounts the document printed.
            line.extended_minor = max(
                line.list_price_minor - line.line_discount_minor, 0
            )
        line.unit_price_minor = _unit_price(line)
        # Nothing on this page mentions a core, so nothing is claimed about
        # one. `0` would be the claim that it is zero.
        line.core_minor = None
        line.total_minor = line.charged_minor
        lines.append(line)

    return lines


def _spacing(rows: list[tuple[float, str]]) -> float:
    """The page's own line spacing, as the commonest gap between rows.

    Measured rather than assumed, because it is a browser's print of a web
    page and the next one may be rendered at a different size.
    """
    gaps = [
        round(rows[i + 1][0] - rows[i][0], 1)
        for i in range(len(rows) - 1)
        if 0 < rows[i + 1][0] - rows[i][0] < 60
    ]
    if not gaps:
        return 15.0
    return max(set(gaps), key=gaps.count)


def _unit_price(line: OrderLine) -> int:
    """Whole cents, for the fields that want one. Never multiplied back out."""
    qty = line.quantity or Decimal(1)
    charged = Decimal(line.extended_minor or 0)
    return int((charged / qty).to_integral_value())


def _is_item_field(row: str) -> bool:
    return bool(
        QTY_LINE.match(row)
        or MONEY_ONLY.match(row)
        or LIST_PRICE.match(row)
        or LINE_DISCOUNT.match(row)
    )


def _join(parts) -> str:
    """Re-join a wrapped description, healing the hyphen it broke on.

    `Cleaner Non-` / `Flammable` is one word split across a line, and a space
    in the middle of it makes the description wrong in a way that then goes
    into the catalog under that name.
    """
    text = ""
    for part in parts:
        part = part.strip()
        if not part:
            continue
        if text.endswith("-"):
            text += part
        elif text:
            text += " " + part
        else:
            text = part
    return re.sub(r"\s+", " ", text).strip()


def _check(order: ParsedOrder) -> None:
    """Reconcile against the document's own figures, and say so when it fails.

    A warning rather than a refusal: the operator confirms this on a review
    screen, and a page that reads correctly except for one line is far more
    useful with the discrepancy named than rejected outright. What is not
    acceptable is committing a total nobody checked.
    """
    if (
        order.stated_subtotal_minor is not None
        and order.stated_subtotal_minor != order.subtotal_minor
    ):
        order.warnings.append(
            "The lines read come to %s and the page says %s."
            % (order.subtotal, ParsedOrder(total_minor=order.stated_subtotal_minor).total)
        )
    if not order.reconciles:
        order.warnings.append(
            "Lines less the discount, plus tax and shipping, come to %s "
            "and the page says %s."
            % (
                ParsedOrder(total_minor=order.computed_total_minor).total,
                order.total,
            )
        )
