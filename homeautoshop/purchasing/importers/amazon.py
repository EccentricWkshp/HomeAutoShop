"""
Reading an Amazon order invoice (SPEC FR-PUR-1, §8.3a).

The *Final Details for Order #…* page, which is what Amazon gives you when you
ask an order for its invoice.

**This one is not a parts order, and that is the whole difficulty.** RockAuto
and NAPA sell nothing but parts, so every line on those documents belongs in
the catalog. An Amazon order is a basket: one of the samples here carries eight
items, of which exactly one — a pack of automotive relays — is a part. The
other seven are tools, and a ninth order could just as easily hold dog food.

Nothing in the document says which is which, and **no amount of cleverness
here can tell**. A reader that guessed would be wrong in the expensive
direction: $455 of tools filed as vehicle parts is a vehicle cost history that
reads plausibly and is wrong for ever (G-4). So this reader reads every line
and takes no view, and the screen that follows asks. `service.run` accepts the
answer as `keep`.

**Shipments.** One order can ship in several parcels, each with its own
subtotal and its own sales tax, and the totals that matter are the ones in the
`Payment information` block at the end. The per-shipment blocks are read only
far enough to know where an item list stops.

**Two columns, one of them personal.** The shipping address and the totals are
printed side by side, so in reading order a street address arrives interleaved
with the money. The totals are taken from the right-hand column by position,
which both parses correctly and keeps this reader from ever having looked at
the address.
"""

from __future__ import annotations

import io
import logging
import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from .orders import OrderLine, ParsedOrder

log = logging.getLogger(__name__)

VENDOR_NAME = "Amazon"
VENDOR_URL = "https://www.amazon.com"
SOURCE = "amazon"

#: The invoice announces itself, and the order number is Amazon's own shape.
FINGERPRINT = re.compile(r"Final\s+Details\s+for\s+Order", re.I)
ORDER_NUMBER = re.compile(r"Order\s*#\s*(\d{3}-\d{7}-\d{7})")
PLACED = re.compile(r"Order\s+Placed:\s*([A-Z][a-z]+\s+\d{1,2},\s*\d{4})")
SHIPPED = re.compile(r"Shipped\s+on\s+([A-Z][a-z]+\s+\d{1,2},\s*\d{4})")

#: `1 of: Some very long product title $14.24`
ITEM = re.compile(r"^(?P<qty>\d+)\s+of:\s+(?P<rest>.*?)\s*\$(?P<price>[\d,]+\.\d{2})\s*$")
#: A row that ends an item's wrapped description.
ITEM_ENDS = re.compile(
    r"^(?:Sold\s+by|Condition:|Business\s+Price|Seller\s+Credentials:|Supplied\s+by:"
    r"|Shipping\s+Address:|Items\s+Ordered|Shipped\s+on|Payment\s+information)", re.I
)
SELLER = re.compile(r"^Sold\s+by(?:\s+and\s+invoiced\s+on\s+behalf\s+of)?:\s*(?P<who>.+?)\s*$", re.I)

GRAND_TOTAL = re.compile(r"Grand\s+Total:\s*(?:USD\s*)?\$?\s*(?P<amount>[\d,]+\.\d{2})", re.I)
BEFORE_TAX = re.compile(r"Total\s+before\s+tax:\s*(?:USD\s*)?\$?\s*(?P<amount>[\d,]+\.\d{2})", re.I)
EST_TAX = re.compile(
    r"Estimated\s+tax\s+to\s+be\s+collected:\s*(?:USD\s*)?\$?\s*(?P<amount>[\d,]+\.\d{2})", re.I
)
HANDLING = re.compile(
    r"Shipping\s*&\s*Handling:\s*(?:USD\s*)?\$?\s*(?P<amount>[\d,]+\.\d{2})", re.I
)
PROMOTION = re.compile(
    r"(?P<label>(?:Free\s+Shipping|Promotion|Gift\s+Card|Subscribe\s*&\s*Save|Rewards)[^:]*)"
    r":\s*-?\s*(?:USD\s*)?\$?\s*(?P<amount>[\d,]+\.\d{2})", re.I
)
PAYMENT = re.compile(r"^(?P<method>.+?)\s*\|\s*Last\s+digits:\s*\d{4}\s*$", re.I)
#: A row that is nothing but an amount.
MONEY_ONLY = re.compile(r"^\$?\s*[\d,]+\.\d{2}\s*$")

#: Where the second column starts. The address is printed to the left of this
#: and the money to the right, so reading the right-hand side is both what
#: parses and what keeps a street address out of this file's reach.
TOTALS_X = 300.0


class NotAnAmazonOrder(ValueError):
    """The file is a PDF, and it is not one of these."""


def _minor(text: str | None) -> int:
    if not text:
        return 0
    try:
        cleaned = text.replace(",", "").replace("$", "").replace("USD", "").strip()
        return int((Decimal(cleaned) * 100).to_integral_value())
    except (InvalidOperation, ValueError):
        return 0


def _rows(words: list[dict], tolerance: float = 3.0) -> list[tuple[float, float, str, bool]]:
    """`(top, leftmost x, text, is_echo)` per printed row.

    Every row is emitted once as printed, and a row that spans both columns is
    emitted **again** carrying only its right-hand half. Two columns because
    the page puts the shipping address beside the money, so in reading order
    `Item(s) Subtotal: $193.00` arrives wedged between two lines of somebody's
    street; the second pass is what makes it a row that can be matched.

    The re-read is flagged rather than left to be recognized later. It is the
    same text twice, and against an item row the right-hand half is the price
    echoed back — `Tool $79.59`, where `Tool` happens to fall past the column
    boundary. Sniffing for that (is it money? is it short?) gets the easy cases
    and puts `$79.59` on the end of a product name in the hard one, and then
    into the catalog under that name. A flag cannot be wrong about it.
    """
    # Grouped by baseline first and ordered left-to-right *afterwards*. Sorting
    # by `(top, x0)` up front looks equivalent and is not: Amazon sets `1 of:`
    # and the price on one baseline and the product title 1.8pt below it, so a
    # rounded top puts the price ahead of the title it belongs after, and the
    # row reads `1 of: $8.29 TSI Supercool ...`. The price then never matches
    # at the end of the row and the whole invoice reads as having no items.
    rows: list[tuple[float, list[dict]]] = []
    for word in sorted(words, key=lambda w: w["top"]):
        if rows and abs(rows[-1][0] - word["top"]) <= tolerance:
            rows[-1][1].append(word)
        else:
            rows.append((word["top"], [word]))

    out: list[tuple[float, float, str, bool]] = []
    for top, group in rows:
        ws = sorted(group, key=lambda w: w["x0"])
        out.append((top, ws[0]["x0"], " ".join(w["text"] for w in ws), False))
        right = [w for w in ws if w["x0"] >= TOTALS_X]
        if right and len(right) != len(ws):
            out.append(
                (top, right[0]["x0"], " ".join(w["text"] for w in right), True)
            )
    return out


def _read_pdf(raw: bytes) -> list[tuple[float, float, str, bool]]:
    import pdfplumber

    rows: list[tuple[float, float, str, bool]] = []
    with pdfplumber.open(io.BytesIO(raw)) as pdf:
        for page in pdf.pages:
            rows.extend(_rows(page.extract_words(keep_blank_chars=False)))
    return rows


def parse(source) -> ParsedOrder:
    raw = source.read() if hasattr(source, "read") else source
    try:
        rows = _read_pdf(raw)
    except NotAnAmazonOrder:
        raise
    except Exception as exc:  # noqa: BLE001 - a broken PDF is a refusal, not a crash
        raise NotAnAmazonOrder("that file could not be read as a PDF") from exc
    return parse_document(rows)


def parse_document(rows: list[tuple[float, float, str, bool]]) -> ParsedOrder:
    """The half that takes no PDF, so the fixtures can exercise it."""
    rows = [(float(top), float(x), text, bool(echo)) for top, x, text, echo in rows]
    body = "\n".join(text for _t, _x, text, _e in rows)
    if not FINGERPRINT.search(body):
        raise NotAnAmazonOrder("this is not an Amazon order invoice")

    order = ParsedOrder(vendor_name=VENDOR_NAME, vendor_url=VENDOR_URL, source=SOURCE)

    if found := ORDER_NUMBER.search(body):
        order.order_number = found.group(1)
    if found := PLACED.search(body):
        order.ordered_on = _date(found.group(1))
    # Several parcels, several dates. The last one is when the order finished
    # arriving, which is the date a return window should run from (FR-PUR-5).
    shipped = [_date(m) for m in SHIPPED.findall(body)]
    if arrivals := [d for d in shipped if d]:
        order.received_on = max(arrivals)

    _read_totals(body, order)
    order.lines = _read_lines(rows)

    if not order.lines:
        raise NotAnAmazonOrder("no items were found on this invoice")

    _check(order)
    return order


def _date(text: str) -> date | None:
    for shape in ("%B %d, %Y", "%b %d, %Y"):
        try:
            return datetime.strptime(text.strip(), shape).date()
        except ValueError:
            continue
    return None


def _last(pattern, body: str) -> str | None:
    """The final match, which is the order-level one.

    Every figure on this page is printed twice: once per shipment and once
    again in the `Payment information` block at the end. The last is the one
    that describes the order, and taking the first would report a two-parcel
    order as costing whatever the first parcel cost.
    """
    found = list(pattern.finditer(body))
    return found[-1].group("amount") if found else None


def _read_totals(body: str, order: ParsedOrder) -> None:
    order.total_minor = _minor(_last(GRAND_TOTAL, body))
    order.stated_subtotal_minor = _minor(_last(BEFORE_TAX, body)) or None
    order.tax_minor = _minor(_last(EST_TAX, body))
    order.shipping_minor = _minor(_last(HANDLING, body))
    for found in PROMOTION.finditer(body):
        amount = _minor(found.group("amount"))
        if amount:
            order.discount_minor += amount
            order.adjustments.append((found.group("label").strip(), amount))
    for line in body.splitlines():
        if found := PAYMENT.match(line.strip()):
            order.payment_method = found.group("method").strip()
            break


def _read_lines(rows: list[tuple[float, float, str, bool]]) -> list[OrderLine]:
    # The right-column re-reads exist so the totals block parses; among the
    # items they are the price printed a second time and belong to no
    # description.
    texts = [text for _t, _x, text, echo in rows if not echo]
    lines: list[OrderLine] = []

    for index, text in enumerate(texts):
        found = ITEM.match(text)
        if not found:
            continue
        # **Amazon states a price *each*, where NAPA states the extended one.**
        # `3 of: Rain-X ... $5.97` against `Item(s) Subtotal: $17.91` settles
        # it, and nothing on a single-quantity invoice would have. Read as an
        # extended figure it reported six gallons of washer fluid as $11.94
        # of $35.82 — and the reconciliation against the invoice's own total
        # is what said so, which is exactly what it is there for.
        #
        # Multiplying out is lossless here because the unit price Amazon prints
        # is itself exact; the rounding this application worries about arises
        # when a *stated* total has to be divided by a quantity, which is the
        # other direction and is why NAPA is read the other way.
        line = OrderLine(
            quantity=Decimal(found.group("qty")),
            unit_price_minor=_minor(found.group("price")),
        )
        # The title wraps *below* its own price row, so the description is
        # gathered forwards until something structural stops it.
        parts = [found.group("rest")]
        for follower in texts[index + 1:]:
            if ITEM.match(follower) or ITEM_ENDS.match(follower):
                break
            parts.append(follower)
        line.description = _join(parts)[:400]

        for follower in texts[index + 1:index + 6]:
            if seller := SELLER.match(follower):
                # `sold_by`, never `brand`. A marketplace seller is not the
                # manufacturer, and `TC-Masterles` filed as a brand against a
                # pack of relays is a catalog entry that reads as authoritative
                # and is somebody's account name. Amazon states no brand and no
                # part number, so the description is the identity — which is
                # honest, and is what the review screen puts in front of a
                # person before any of it is written.
                line.sold_by = seller.group("who").strip().removesuffix(" (seller profile)")
                break

        # The invoice says nothing about cores, which is not the same as
        # saying there is none — the column renders a dash rather than $0.00.
        line.core_minor = None
        line.total_minor = line.charged_minor
        lines.append(line)

    return lines


def _join(parts) -> str:
    """Re-join a wrapped title.

    Amazon wraps mid-word and without a hyphen — `Ki` on one row and `t
    Replaces 9036` on the next — so a plain space produces `Ki t Replaces`. A
    fragment of one or two characters followed by something continuing in lower
    case is that wrap and is closed up; anything longer is a word.
    """
    text = ""
    for part in parts:
        part = (part or "").strip()
        if not part:
            continue
        if not text:
            text = part
            continue
        tail = text.rsplit(" ", 1)[-1]
        if len(tail) <= 2 and part[:1].islower() and tail[-1:].isalpha():
            text += part
        else:
            text += " " + part
    return re.sub(r"\s+", " ", text).strip()


def _check(order: ParsedOrder) -> None:
    """Reconcile against the invoice's own figures and say so when it fails."""
    if (
        order.stated_subtotal_minor is not None
        and order.stated_subtotal_minor != order.subtotal_minor
    ):
        order.warnings.append(
            "The items read come to %s and the invoice says %s."
            % (order.subtotal, ParsedOrder(total_minor=order.stated_subtotal_minor).total)
        )
    if not order.reconciles:
        order.warnings.append(
            "Items less any promotion, plus tax and shipping, come to %s "
            "and the invoice says %s."
            % (ParsedOrder(total_minor=order.computed_total_minor).total, order.total)
        )
