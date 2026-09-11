"""
Reading an eBay *Order details* page (SPEC FR-PUR-1, FR-PUR-10, §8.3a).

The page you get from *Purchase history → View order details*, printed to PDF
from the browser. It is the fourth reader and the second marketplace, and it
differs from the other three in three ways that each change what may be
believed about the document.

**One payment, several orders.** RockAuto, NAPA and Amazon each print one order
number. eBay prints one per *seller*: a single checkout that took parts from
three sellers is one payment, one tax figure and one order total, printed above
three separate tables with three order numbers. Five of the eight samples here
carry one and three carry between two and three — so a reader that assumed one
would read a third of the corpus and silently drop the rest.

They stay one `ParsedOrder`, because everything the shop records about a
purchase is the part that is shared: it was one payment, on one day, with one
tax charge and one total, and the per-seller totals needed to split it are not
printed anywhere. Each line carries its own seller in `sold_by`, the purchase is
filed under the first order number, and a page covering several says so in a
warning rather than quietly picking one.

**The price column is the extended figure, not a price each.** Nothing on a
single-quantity line distinguishes the two, and getting it wrong is the mistake
the Amazon reader already made once in the other direction. The sample that
settles it is the nine-item order: `2 × Lock Nut $12.98`, `2 × Washer $13.88`
and five single lines, against a stated `9 items $154.99`. Read as prices each
that comes to $181.85; read as extended figures it comes to $154.99 exactly. So
it is taken directly, the way NAPA's is and for the same reason (FR-PUR-9).

**A refund is not a discount**, and this is the one place the arithmetic could
have been made to work while being wrong. One sample was paid at $167.08 and
ends on `Order total $131.57`, with `Total refunded -$35.51` between them for an
item that never arrived. Putting that $35.51 into `discount_minor` reconciles
against the printed total — and then `service._overheads_per_line` spreads it
pro-rata across all seven lines, so every part in the order records as 23%
cheaper than it was, including the six that turned up. The page does not say
which item came back, and **no reader can work it out**, so this one states the
amount paid, names the refund, and says on the review screen that a line should
come out. Dropping it there removes the line and its own share of the tax, which
lands on $131.57 — the figure the page itself ends on.

**Layout.** Three columns at the top — order information, the shipping address,
and the totals — and below them one table per seller. The address is dropped by
position and never inspected, the same arrangement the Amazon reader uses and
for the same reason: it parses correctly *and* it means this file has never
looked at a street address. Each table declares its own column positions in its
header, which is what the item rows are read against, because an item's name and
its shipping service are printed on the same baselines and only the columns tell
them apart. Nothing else does: `USPS Priority Mail` sits eleven points to the
right of the name it follows, and a reader working in reading order files a
carrier as part of a product name and then puts it in the catalog under it.

Nothing here writes anything. It returns what it read and a person confirms it.
"""

from __future__ import annotations

import io
import logging
import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from .orders import OrderLine, ParsedOrder

log = logging.getLogger(__name__)

VENDOR_NAME = "eBay"
VENDOR_URL = "https://www.ebay.com"
SOURCE = "ebay"

#: The browser writes the page title into the PDF and it is the one unambiguous
#: statement of what this is. The structural markers stand in where a title has
#: been stripped; together they are specific enough, and `Items bought from` is
#: not a phrase any of the other three documents contains.
TITLE = re.compile(r"\beBay\b", re.I)
STRUCTURE = ("Order information", "Items bought from", "Order number:")

SELLER_HEADING = re.compile(r"^Items\s+bought\s+from\s+(?P<who>.+?)\s*$", re.I)
ORDER_NUMBER = re.compile(r"^Order\s+number:\s*(?P<number>\S+)\s*$", re.I)

#: `9 items $154.99` — the label half. What the lines are supposed to come to,
#: and how many things are supposed to be on them.
ITEM_COUNT = re.compile(r"^(?P<count>\d+)\s+items?$", re.I)
#: The totals column, by the label eBay prints down the left of it.
SHIPPING = re.compile(r"^Shipping$", re.I)
TAX = re.compile(r"^Tax\*?$", re.I)
#: `Item discount`, and whatever else eBay ends a discount label with. Searched
#: rather than matched from the start, which the reconciliation caught: written
#: as `\bdiscount$` against `.match()` it anchors at the beginning of the label
#: and stops matching the one row in the corpus it exists for.
ITEM_DISCOUNT = re.compile(r"\bdiscount$", re.I)
AMOUNT_PAID = re.compile(r"^Amount\s+paid$", re.I)
REFUNDED = re.compile(r"^Total\s+refunded$", re.I)
ORDER_TOTAL = re.compile(r"^Order\s+total$", re.I)

MONEY = re.compile(r"^-?\$\s*(?P<amount>[\d,]+\.\d{2})$")
#: `Free`, which is what eBay prints in the shipping row rather than `$0.00`.
FREE = re.compile(r"^Free$", re.I)
QUANTITY = re.compile(r"^\d{1,4}$")

#: What each table calls its columns, and the key each becomes. The header is
#: read rather than assumed because the positions move: one sample prints the
#: item name at x=64.8 in its first table and x=71.5 in its second.
COLUMNS = {
    "quantity": "quantity",
    "item name": "name",
    "shipping service": "shipping",
    "item price": "price",
}

#: The gutters between the three columns at the top of the page: order
#: information at the margin, then the shipping address, then the totals. The
#: middle strip is the one this file refuses to look inside, bounded below by
#: the first seller heading — the item tables run the full width of the page
#: and a product name lands squarely in that band.
#:
#: These sit **between** the columns and never on one. pdfplumber returns
#: 189.99999999999997 for a word set at x=190, so a boundary named after a
#: column's own coordinate is decided by the last bit of a float: written as
#: 190.0 this let the address through into the order information, which
#: turned `Placed on Aug 29, 2024` into an unparseable date and — far worse —
#: meant this file had read somebody's street address after all.
INFO_VALUE_X = 90.0
ADDRESS_X = 187.0
TOTALS_X = 345.0

#: A run of words is one printed cell; a gap wider than this is the next
#: column. Words inside a cell sit about 2 points apart and the narrowest gap
#: between two cells in the corpus is 9, so this sits between two clear
#: populations rather than on a guess. It is also what keeps the sales-tax
#: footnote out of the totals: that is continuous prose printed under the
#: totals label column, and it spills past the value column's left edge without
#: ever leaving a gap wide enough to become a value.
CELL_GAP = 5.0

#: How far a word may sit from a baseline and still be on that row. Small,
#: because an item's own rows are only six points apart — but not zero, because
#: the summary block prints its left column 1.5 points above its right one.
ROW_TOLERANCE = 3.0

#: How far a printed row may sit from an item's quantity and still belong to it.
#: A name or a carrier wraps one row above and one below the row carrying the
#: quantity and the price, and consecutive items are 32 points apart.
ITEM_BAND = 14.0

#: Slack when deciding which column a cell is in. The columns are declared by
#: the header and the item rows print to the same coordinates, so this absorbs
#: rounding and nothing else.
COLUMN_SLACK = 4.0

#: How far a wrapped half of a column heading sits from the row carrying
#: `Quantity`. `Shipping service` and `Item price` are set over two and
#: sometimes three baselines six points apart, and the nearest item is
#: twenty-one points below the header at its closest in the corpus.
HEADER_BAND = 8.0

#: How far two stacked halves of one heading may disagree about their left
#: edge. eBay sets `Item` at 508.1 over `price` at 510.8 and means one column;
#: the nearest neighbouring heading is forty points away.
HEADER_STACK = 6.0


class NotAnEbayOrder(ValueError):
    """The file is a PDF, and it is not one of these."""


def _minor(text: str | None) -> int:
    if not text:
        return 0
    try:
        cleaned = text.replace(",", "").replace("$", "").replace("-", "").strip()
        return int((Decimal(cleaned) * 100).to_integral_value())
    except (InvalidOperation, ValueError):
        return 0


def _cells(words: list[dict]) -> list[tuple[float, float, str]]:
    """`(top, x0, text)` for every printed cell, in reading order.

    Words are grouped onto a baseline and then split back apart wherever the
    gap between two of them is wider than the space inside a phrase. What comes
    out is the page as it was laid out — `Tax` and `$12.09` as two things
    rather than one row of text — which is the only form in which the columns
    below mean anything.
    """
    rows: list[tuple[float, list[dict]]] = []
    for word in sorted(words, key=lambda w: w["top"]):
        if rows and abs(rows[-1][0] - word["top"]) <= ROW_TOLERANCE:
            rows[-1][1].append(word)
        else:
            rows.append((word["top"], [word]))

    out: list[tuple[float, float, str]] = []
    for top, group in rows:
        run: list[dict] = []
        for word in sorted(group, key=lambda w: w["x0"]):
            if run and word["x0"] - run[-1]["x1"] > CELL_GAP:
                out.append((top, run[0]["x0"], " ".join(w["text"] for w in run)))
                run = []
            run.append(word)
        if run:
            out.append((top, run[0]["x0"], " ".join(w["text"] for w in run)))
    return out


def _read_pdf(raw: bytes) -> tuple[str, list[tuple[float, float, str]]]:
    import pdfplumber

    cells: list[tuple[float, float, str]] = []
    with pdfplumber.open(io.BytesIO(raw)) as pdf:
        title = (pdf.metadata or {}).get("Title", "") or ""
        # Pages are concatenated with the vertical position carried forward, so
        # a table that runs over a page break stays one table. eBay reprints
        # the column header at the top of the continuation, which is handled
        # below by simply reading it again.
        offset = 0.0
        for page in pdf.pages:
            page_cells = _cells(page.extract_words(keep_blank_chars=False))
            cells.extend((top + offset, x0, text) for top, x0, text in page_cells)
            offset += float(page.height)
    return title, cells


def parse(source) -> ParsedOrder:
    raw = source.read() if hasattr(source, "read") else source
    try:
        title, cells = _read_pdf(raw)
    except NotAnEbayOrder:
        raise
    except Exception as exc:  # noqa: BLE001 - a broken PDF is a refusal, not a crash
        raise NotAnEbayOrder("that file could not be read as a PDF") from exc
    return parse_document(title, cells)


def parse_document(title: str, cells: list[tuple[float, float, str]]) -> ParsedOrder:
    """The half that takes no PDF, so the fixtures can exercise it.

    `cells` is `(vertical position, left edge, text)` per printed cell — see
    `_cells`, and see the module docstring for why a left edge is not optional
    here the way it is for a document printed as rows of prose.
    """
    cells = [(float(top), float(x0), text) for top, x0, text in cells]
    body = "\n".join(text for _top, _x0, text in cells)
    if not TITLE.search(title or "") and not all(m in body for m in STRUCTURE):
        raise NotAnEbayOrder("this is not an eBay order details page")

    order = ParsedOrder(vendor_name=VENDOR_NAME, vendor_url=VENDOR_URL, source=SOURCE)

    starts = [top for top, x0, text in cells if x0 < ADDRESS_X and SELLER_HEADING.match(text)]
    if not starts:
        raise NotAnEbayOrder("this page lists no seller's items")

    summary = [cell for cell in cells if cell[0] < min(starts)]
    stated = _read_summary(summary, order)
    order.lines, numbers = _read_lines([cell for cell in cells if cell[0] >= min(starts)])

    if not order.lines:
        raise NotAnEbayOrder("no items were found on this page")

    # Filed under the first, which is the one printed highest on the page and
    # is therefore stable across re-reads of the same document.
    order.order_number = numbers[0] if numbers else ""
    if len(numbers) > 1:
        order.warnings.append(
            "This page covers %(n)s orders paid for together (%(numbers)s). "
            "They are being brought in as one purchase, filed under %(first)s."
            % {"n": len(numbers), "numbers": ", ".join(numbers), "first": numbers[0]}
        )

    _check(order, stated)
    return order


def _date(text: str) -> date | None:
    for shape in ("%b %d, %Y", "%B %d, %Y"):
        try:
            return datetime.strptime(text.strip(), shape).date()
        except ValueError:
            continue
    return None


@dataclass(slots=True)
class _Stated:
    """What the totals column says that does not simply land on the order.

    Three figures the reader has to hold on to rather than assign: a count it
    checks itself against, and the two halves of a refund — what was paid and
    what the page ends on — which are the same number on every order where
    nothing came back.
    """

    count: int | None = None
    paid_minor: int = 0
    final_minor: int = 0
    refunded_minor: int = 0


def _read_summary(cells: list[tuple[float, float, str]], order: ParsedOrder) -> _Stated:
    """The block above the first seller, minus the part of it that is a person.

    Three columns: what the order is, who it was posted to, and what it came
    to. The middle one is dropped **by position and unread** — the parser needs
    none of it, and the strongest form of not putting somebody's street address
    in a fixture is never having extracted it (NFR-S-5).

    The count it comes back with is the only thing on the page that would catch
    a quantity read wrong. The money cannot: every price here is already the
    extended figure, so a line read as one of something when it was two leaves
    the subtotal exactly as it was.
    """
    rows: dict[float, list[tuple[float, str]]] = {}
    for top, x0, text in cells:
        if ADDRESS_X <= x0 < TOTALS_X:
            continue
        rows.setdefault(top, []).append((x0, text))

    stated = _Stated()
    for top in sorted(rows):
        cluster = sorted(rows[top])
        info = [text for x0, text in cluster if x0 < INFO_VALUE_X]
        value = [text for x0, text in cluster if INFO_VALUE_X <= x0 < ADDRESS_X]
        _read_order_information(info, value, order)

        labels = [text for x0, text in cluster if TOTALS_X <= x0 < _totals_value_x(cluster)]
        amounts = [text for x0, text in cluster if x0 >= _totals_value_x(cluster)]
        if not labels or not amounts:
            continue
        label, amount = labels[-1], amounts[0]
        if not MONEY.match(amount) and not FREE.match(amount):
            # The sales-tax footnote, which is printed in this column and is
            # prose. A label with no money beside it is not a total.
            continue
        _read_total(label, amount, order, stated)

    # **What the lines on this page add up to**, which is what the order was
    # paid at and not what it ended on. They differ only when something came
    # back afterwards; see the module docstring for why the difference is not
    # allowed to become a discount.
    order.total_minor = stated.paid_minor or stated.final_minor
    return stated


def _totals_value_x(cluster: list[tuple[float, str]]) -> float:
    """Where the amounts start on this row.

    eBay right-aligns the totals column against a page width this reader has no
    other reason to know, so the split is taken from the row itself: the last
    cell is the amount and everything before it is the label it belongs to.
    """
    right = [x0 for x0, _text in cluster if x0 >= TOTALS_X]
    return right[-1] if len(right) > 1 else float("inf")


def _read_order_information(info: list[str], value: list[str], order: ParsedOrder) -> None:
    """`Placed on` / `Aug 25, 2026`, and the two others worth keeping.

    Paired by column rather than by reading the row as a sentence, because
    `Payment methods` wraps onto its own second row while its value stays on
    the first — so the row reads `Payment PayPal, eBay Bucks` and the label
    never matches anything.

    `Buyer` is deliberately not read. It is the operator's own account name and
    the shop already knows whose shop it is.
    """
    if not info or not value:
        return
    label, said = info[0], " ".join(value)
    if label.startswith("Placed on"):
        order.ordered_on = _date(said) or order.ordered_on
    elif label.startswith("Payment"):
        order.payment_method = said[:40]


def _read_total(label: str, amount: str, order: ParsedOrder, stated: _Stated) -> None:
    """One row of the totals column."""
    if found := ITEM_COUNT.match(label):
        # `9 items $154.99` — what the lines are supposed to add up to, and how
        # many things are supposed to be on them.
        order.stated_subtotal_minor = _minor(amount)
        stated.count = int(found.group("count"))
    elif SHIPPING.match(label):
        order.shipping_minor = 0 if FREE.match(amount) else _minor(amount)
    elif TAX.match(label):
        order.tax_minor = _minor(amount)
    elif ITEM_DISCOUNT.search(label):
        # A checkout discount, which came off before the tax was worked out:
        # $37.22 less $1.86 is $35.36, and 9.3% of that is the $3.29 printed —
        # the order `computed_total_minor` already works in (FR-PUR-1). This
        # one does belong to every line, so it is the one that is spread.
        order.discount_minor += _minor(amount)
        order.adjustments.append((label, _minor(amount)))
    elif AMOUNT_PAID.match(label):
        stated.paid_minor = _minor(amount)
    elif REFUNDED.match(label):
        # Held, and deliberately kept out of `adjustments`: that list is what
        # the review screen prints between the tax and the total, and a row
        # there would read as money already taken off a figure it was not taken
        # off. It comes back as a warning instead, which can say what to do
        # about it.
        stated.refunded_minor = _minor(amount)
    elif ORDER_TOTAL.match(label):
        stated.final_minor = _minor(amount)


def _read_lines(
    cells: list[tuple[float, float, str]]
) -> tuple[list[OrderLine], list[str]]:
    """Every seller's table, and the order number each was printed under."""
    lines: list[OrderLine] = []
    numbers: list[str] = []
    seller = ""

    for start, end, header in _tables(cells):
        for top, x0, text in cells:
            if not (start <= top < end):
                continue
            if found := SELLER_HEADING.match(text):
                seller = found.group("who")
            elif found := ORDER_NUMBER.match(text):
                number = found.group("number")
                if number not in numbers:
                    numbers.append(number)
        if header is None:
            continue
        lines.extend(_read_table(cells, start, end, header, seller))
    return lines, numbers


def _tables(
    cells: list[tuple[float, float, str]]
) -> list[tuple[float, float, dict[str, float] | None]]:
    """`(from, to, columns)` for each stretch of page under one seller heading.

    A heading starts a stretch and the next one ends it; the column header
    inside it says where that table's columns are. A stretch with no header is
    a seller whose table began on the next page, and carries no items of its
    own — eBay reprints the header at the top of the continuation, so the
    continuation is simply another table.
    """
    headings = sorted(
        top for top, x0, text in cells if x0 < ADDRESS_X and SELLER_HEADING.match(text)
    )
    headers = sorted(
        top for top, x0, text in cells if x0 < ADDRESS_X and text == "Quantity"
    )

    bounds: list[float] = sorted({*headings, *headers})
    out: list[tuple[float, float, dict[str, float] | None]] = []
    for position, start in enumerate(bounds):
        end = bounds[position + 1] if position + 1 < len(bounds) else float("inf")
        out.append((start, end, _columns(cells, start) if start in headers else None))
    return out


def _columns(cells: list[tuple[float, float, str]], top: float) -> dict[str, float] | None:
    """Where this table's columns start, taken from the header it prints.

    The header wraps: `Shipping service` and `Item price` are set on two and
    sometimes three baselines with their halves stacked in the same column, so
    the fragments are gathered back together by their left edge before being
    matched against what eBay calls each column.
    """
    band = [cell for cell in cells if abs(cell[0] - top) <= HEADER_BAND]
    stacked: list[tuple[float, list[tuple[float, str]]]] = []
    for cell_top, x0, text in sorted(band, key=lambda c: (c[1], c[0])):
        if stacked and abs(stacked[-1][0] - x0) <= HEADER_STACK:
            stacked[-1][1].append((cell_top, text))
        else:
            stacked.append((x0, [(cell_top, text)]))

    columns: dict[str, float] = {}
    for x0, parts in stacked:
        label = " ".join(text for _top, text in sorted(parts)).lower()
        if key := COLUMNS.get(label):
            columns[key] = x0
    # Without both of these there is no way to tell a product name from the
    # carrier printed beside it, and a guess ends up in the catalog.
    if "name" not in columns or "price" not in columns:
        return None
    return columns


def _read_table(
    cells: list[tuple[float, float, str]],
    start: float,
    end: float,
    header: dict[str, float],
    seller: str,
) -> list[OrderLine]:
    """One seller's items, read down its own columns.

    Anchored on the quantity, because it is the one cell of an item that is
    always printed and always alone in its column. The name and the carrier
    wrap above and below that baseline, so an item is the band of rows nearest
    its own quantity rather than a row of text.
    """
    body = [cell for cell in cells if start < cell[0] < end]
    quantity_x = header.get("quantity", 0.0)
    anchors = sorted(
        top
        for top, x0, text in body
        if abs(x0 - quantity_x) <= COLUMN_SLACK and QUANTITY.match(text)
    )
    if not anchors:
        return []

    columns = sorted((x0, key) for key, x0 in header.items())
    banded: dict[float, list[tuple[float, float, str]]] = {top: [] for top in anchors}
    for cell in body:
        nearest = min(anchors, key=lambda top: abs(top - cell[0]))
        if abs(nearest - cell[0]) <= ITEM_BAND:
            banded[nearest].append(cell)

    lines = []
    for top in anchors:
        line = _read_item(banded[top], columns, seller)
        if line is not None:
            lines.append(line)
    return lines


def _read_item(
    band: list[tuple[float, float, str]], columns: list[tuple[float, str]], seller: str
) -> OrderLine | None:
    """One item, from the cells printed around its quantity.

    Everything is taken by the column it was printed in, including the carrier
    — `USPS Priority Mail`, `eBay SpeedPAK Expedited` — which is then used for
    nothing at all. It is how the parcel traveled rather than anything about
    the part, and what the shipping cost is already on the order. **Reading it
    is what keeps it out of the description**: it shares a baseline with the
    name it follows, eleven points to the right, so a reader that did not know
    about the column would file the carrier as part of the product name.
    """
    held: dict[str, list[tuple[float, float, str]]] = {}
    for cell in sorted(band, key=lambda c: (c[0], c[1])):
        held.setdefault(_column_of(cell[1], columns), []).append(cell)

    quantity = next(
        (text for _t, _x, text in held.get("quantity", ()) if QUANTITY.match(text)), None
    )
    price = next(
        (text for _t, _x, text in held.get("price", ()) if MONEY.match(text)), None
    )
    if quantity is None or price is None:
        return None

    line = OrderLine(
        quantity=Decimal(quantity),
        # **The extended figure**, which the nine-item sample proves and no
        # single-quantity line could have. Taken directly rather than divided
        # and multiplied back, the way NAPA's is (FR-PUR-9).
        extended_minor=_minor(price),
        # eBay states neither a brand nor a part number, and the numbers strewn
        # through a seller's title are not those. `Genuine OEM Briggs &
        # Stratton Fuel Pump Part # 597338 replaces 808656` carries two, one of
        # which is what it supersedes; `For 2002-2007 Suzuki Aerio Remote Key`
        # carries none. A number picked out of marketing copy and filed in the
        # catalog reads as authoritative, which is why the review screen asks
        # instead — the same division of labor `PACK_PHRASE` already makes.
        description=_join(held.get("name", ()))[:400],
        # A marketplace handle, and emphatically not a brand. `wurknman` in the
        # manufacturer column would be a catalog entry that looks right.
        sold_by=seller,
        # The page says nothing about a core, which is not the same as saying
        # there is none: `0` would be a claim.
        core_minor=None,
    )
    line.unit_price_minor = _unit_price(line)
    line.total_minor = line.charged_minor
    return line if line.description else None


def _unit_price(line: OrderLine) -> int:
    """Whole cents, for the fields that want one. Never multiplied back out.

    The extended figure is what the line is charged at and what everything
    downstream uses (FR-PUR-11); this is here so a screen can print what one of
    them came to, and $18.61 of a $37.22 pair is a statement about the pair.
    """
    charged = Decimal(line.extended_minor or 0)
    return int((charged / (line.quantity or Decimal(1))).to_integral_value())


def _column_of(x0: float, columns: list[tuple[float, str]]) -> str:
    """The column a cell was printed in: the rightmost one it starts at or after."""
    found = columns[0][1] if columns else "name"
    for column_x, key in columns:
        if x0 >= column_x - COLUMN_SLACK:
            found = key
    return found


def _join(cells) -> str:
    """A name that wrapped, back in one piece.

    eBay breaks at spaces, so unlike NAPA's hyphen and Amazon's mid-word split
    there is nothing to heal — the fragments join with a space and the eBay
    item number that ends the cell comes along with them, because that is what
    the column says and it is the only identity such a line has.
    """
    return re.sub(r"\s+", " ", " ".join(text for _top, _x0, text in cells)).strip()


def _check(order: ParsedOrder, stated: _Stated) -> None:
    """Reconcile against the page's own figures, and say so when it fails.

    A warning rather than a refusal, on the same principle the other readers
    follow: a page that reads correctly except for one line is more useful with
    the discrepancy named than rejected. What is not acceptable is committing a
    total nobody checked.
    """
    if (
        order.stated_subtotal_minor is not None
        and order.stated_subtotal_minor != order.subtotal_minor
    ):
        order.warnings.append(
            "The items read come to %s and the page says %s."
            % (order.subtotal, ParsedOrder(total_minor=order.stated_subtotal_minor).total)
        )
    if stated.count is not None:
        counted = sum(line.quantity for line in order.charged_lines)
        if counted != stated.count:
            # The only check that catches a quantity read wrong. The money
            # cannot: every price here is already the extended one, so a line
            # read as one of something when it was two still adds up.
            order.warnings.append(
                "The lines read come to %(counted)s items and the page says %(stated)s."
                % {"counted": counted, "stated": stated.count}
            )
    if not order.reconciles:
        order.warnings.append(
            "Items less any discount, plus tax and shipping, come to %s "
            "and the page says %s."
            % (ParsedOrder(total_minor=order.computed_total_minor).total, order.total)
        )
    if stated.refunded_minor:
        # The most useful thing this reader can say about a refund, because it
        # is the only thing it knows: how much came back, that the page does
        # not say which item it was for, and what to do about it. Leaving the
        # line out takes its share of the tax with it, which is what lands the
        # order on the figure the page itself ends on.
        order.warnings.append(
            "%(amount)s came back on this order and the page does not say which "
            "item it was for. Leave that line out below if it never arrived — "
            "its share of the tax goes with it, and the order then comes to "
            "%(final)s."
            % {
                "amount": ParsedOrder(total_minor=stated.refunded_minor).total,
                "final": ParsedOrder(total_minor=stated.final_minor).total,
            }
        )
