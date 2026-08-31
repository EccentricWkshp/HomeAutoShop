"""
Reading a RockAuto order confirmation (SPEC FR-PUR-1, FR-PART-2, §8.3a).

A parts order is the densest single document this application will ever see. One
page carries the vendor, the order number, the date, what was paid, and — for
every line — a **brand, a manufacturer part number, a part type, a price, a core
charge and a quantity**, all of it grouped under *the vehicle it was bought
for*. Typed in by hand that is twenty minutes and a dozen chances to fat-finger
a part number. Read from the file it is the parts catalogue filling itself in,
with confirmed fitment attached, which is the thing that makes a parts catalogue
worth having.

**Why word geometry rather than lines of text.** The same reason the XTOOL D8
parser needs it (§8.3a): the layout wraps, and it wraps *around* the row it
belongs to. Both text columns do it, and in both directions —

    90K38156B (90K-        <- part number, first half, ABOVE its own row
    GATES    Belt Tensioner  $ 99.79  -  1  $ 99.79
    38156B)                <- and the second half BELOW it

    [Kit Component] Belt   <- description, above
    GATES 38156   $ 57.37  $ 0.00  1  -
    Tensioner              <- and below

Read in reading order, `38156B)` becomes part of the next line's part number and
`Belt Tensioner` attaches to whatever follows. Read by column and vertical
distance, every fragment lands on the row it was printed against.

**Kit components.** A kit prints its own priced line and then its contents, each
with a price and a `-` in the Total column, because they are already paid for
inside the kit. They are carried through as parts — the whole point is having
`GATES 38156` in the catalogue when it needs replacing on its own — but they
produce **no purchase line and no money**, or the order would be charged twice.

Nothing here writes anything. It returns what it read, and a person confirms it
(FR-INT-4 is about scan reports and the reasoning is identical: a misread price
that auto-commits is wrong money that looks plausible for months).
"""

from __future__ import annotations

import io
import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from homeautoshop.core.measurements import Money

log = logging.getLogger(__name__)

VENDOR_NAME = "RockAuto"

#: The document announces itself. Anything else is not this format, and saying
#: so beats extracting nonsense with confidence.
FINGERPRINT = re.compile(r"RockAuto\s+Order\s+Confirmation", re.I)

ORDER_NUMBER = re.compile(r"^\s*Order\s+(\d{4,})\s*$", re.M)
ORDER_DATE = re.compile(
    r"^\s*(?:Mon|Tues|Wednes|Thurs|Fri|Satur|Sun)day,\s+(.+?)\s+\d{1,2}:\d{2}\s*(?:AM|PM)", re.M
)
SHIPPING = re.compile(r"^Shipping\s+(?P<how>.*?)\s*\$\s*(?P<amount>[\d,]+\.\d{2})\s*$", re.M)
TAX = re.compile(r"^Tax\s+\$\s*(?P<amount>[\d,]+\.\d{2})\s*$", re.M)
ORDER_TOTAL = re.compile(r"^Order\s+Total\s+\$\s*(?P<amount>[\d,]+\.\d{2})\s*$", re.M)
PAYMENT = re.compile(r"^(?P<method>[A-Za-z][A-Za-z ]{1,20})\s+-\$\s*[\d,]+\.\d{2}\s*$", re.M)

#: `2004 SUZUKI AERIO 2.3L L4` — the group header every line below belongs to.
VEHICLE_HEADING = re.compile(
    r"^(?P<year>(?:19|20)\d{2})\s+(?P<rest>[A-Z0-9][^$]*?)\s*$"
)

KIT_COMPONENT = re.compile(r"^\[Kit Component\]\s*", re.I)

#: Rows below this are the totals block, not parts.
END_OF_ITEMS = ("Shipping", "Tax", "Order Total", "Balance", "Subtotal")


class NotARockAutoOrder(ValueError):
    """The file is a PDF, and it is not one of these."""


@dataclass(slots=True)
class OrderLine:
    brand: str = ""
    part_number: str = ""
    description: str = ""
    unit_price_minor: int = 0
    #: `None` where the column printed `-`, which means "no core on this line".
    core_minor: int | None = 0
    quantity: Decimal = Decimal(1)
    #: `None` for a kit component: already paid for inside its kit.
    total_minor: int | None = 0
    is_kit_component: bool = False
    vehicle: str = ""

    @property
    def label(self) -> str:
        return " ".join(x for x in (self.brand, self.part_number) if x)

    # Minor units are how money is *stored* (§5.5) and never how it is shown.
    # The review screen printed `5379` in a column headed "Each", which is not
    # a price anybody recognises — and on a screen whose entire job is letting
    # somebody check the numbers before they are written, an unreadable number
    # is worse than a missing one.

    @property
    def unit_price(self) -> Money:
        return Money(self.unit_price_minor, "USD")

    @property
    def core(self) -> Money | None:
        """`None` where the column printed `-`, which is not the same as zero."""
        return None if self.core_minor is None else Money(self.core_minor, "USD")

    @property
    def total(self) -> Money | None:
        """`None` for a kit component: paid for inside its kit."""
        return None if self.total_minor is None else Money(self.total_minor, "USD")


@dataclass(slots=True)
class ParsedOrder:
    order_number: str = ""
    ordered_on: date | None = None
    shipping_minor: int = 0
    tax_minor: int = 0
    total_minor: int = 0
    discount_minor: int = 0
    payment_method: str = ""
    lines: list[OrderLine] = field(default_factory=list)
    #: `(label, minor)` for each rebate or credit printed against the order,
    #: kept separately from the sum so the review screen can name them.
    adjustments: list[tuple[str, int]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def shipping(self) -> Money:
        return Money(self.shipping_minor, "USD")

    @property
    def tax(self) -> Money:
        return Money(self.tax_minor, "USD")

    @property
    def discount(self) -> Money:
        return Money(self.discount_minor, "USD")

    @property
    def total(self) -> Money:
        return Money(self.total_minor, "USD")

    @property
    def subtotal(self) -> Money:
        return Money(self.subtotal_minor, "USD")

    @property
    def charged_lines(self) -> list[OrderLine]:
        return [line for line in self.lines if not line.is_kit_component]

    @property
    def subtotal_minor(self) -> int:
        return sum(
            int(Decimal(line.unit_price_minor) * line.quantity) + (line.core_minor or 0)
            for line in self.charged_lines
        )

    @property
    def vehicles(self) -> list[str]:
        seen: list[str] = []
        for line in self.lines:
            if line.vehicle and line.vehicle not in seen:
                seen.append(line.vehicle)
        return seen


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


def _minor(text: str) -> int:
    """`$ 1,234.56` to 123456. Integer minor units, never a float (§5.5)."""
    cleaned = re.sub(r"[^\d.]", "", text or "")
    if not cleaned:
        return 0
    try:
        return int((Decimal(cleaned) * 100).quantize(Decimal(1)))
    except InvalidOperation:
        return 0


def _rows(words: list[dict], tolerance: float = 2.5) -> list[list[dict]]:
    """Group words into the visual rows they were printed on."""
    ordered = sorted(words, key=lambda w: (w["top"], w["x0"]))
    rows: list[list[dict]] = []
    for word in ordered:
        if rows and abs(word["top"] - rows[-1][0]["top"]) <= tolerance:
            rows[-1].append(word)
        else:
            rows.append([word])
    return [sorted(row, key=lambda w: w["x0"]) for row in rows]


@dataclass(slots=True)
class _Columns:
    """Where each column starts, learned from this document rather than fixed."""

    description: float
    price: float
    core: float
    quantity: float
    total: float


def _columns(rows: list[list[dict]]) -> _Columns | None:
    """Find the column boundaries from the header row and the data under it.

    The numeric boundaries come from the header labels, which sit close enough
    above their own columns to be usable. The **description** boundary cannot:
    the label `Part Type` is centred at roughly x=290 while its values begin
    around x=200, so anything derived from the label alone puts every
    description into the part-number column.

    So that one is measured instead. In each priced row the tokens left of the
    price column fall into two clumps with a clear space between them — brand
    and part number on the left, description on the right. The widest gap in the
    row is that space; the median of its right-hand edge across every row is the
    column start, and a median rather than a minimum because one row with an
    unusually long part number should not move the boundary for the rest.
    """
    header = None
    for row in rows:
        text = " ".join(word["text"] for word in row)
        if "Part Number" in text and "Price" in text and "Quantity" in text:
            header = row
            break
    if header is None:
        return None

    at = {}
    for word in header:
        at.setdefault(word["text"], word["x0"])
    try:
        price, core, quantity, total = at["Price"], at["Core"], at["Quantity"], at["Total"]
    except KeyError:
        return None

    # A little to the left of each label, because the values under it start
    # slightly before the (centred) heading does.
    price_left = price - 12
    boundaries = _Columns(
        description=0.0,
        price=price_left,
        core=core - 14,
        quantity=quantity - 20,
        total=total - 26,
    )

    gaps: list[float] = []
    for row in rows:
        if row[0]["top"] <= header[0]["top"]:
            continue
        left = [word for word in row if word["x0"] < price_left]
        priced = [word for word in row if price_left <= word["x0"] < boundaries.core]
        if len(left) < 2 or not priced:
            continue
        widest, edge = 0.0, None
        for previous, following in zip(left, left[1:]):
            gap = following["x0"] - previous["x1"]
            if gap > widest:
                widest, edge = gap, following["x0"]
        if edge is not None and widest > 8:
            gaps.append(edge)

    if not gaps:
        return None
    gaps.sort()
    boundaries.description = gaps[len(gaps) // 2] - 6
    return boundaries


def _cell(row: list[dict], low: float, high: float) -> str:
    return " ".join(w["text"] for w in row if low <= w["x0"] < high)


def read_pdf(source) -> tuple[list[str], list[list[dict]]]:
    """The two views of the document this parser needs: text, and geometry."""
    import pdfplumber

    raw = source.read() if hasattr(source, "read") else bytes(source)
    if hasattr(source, "seek"):
        source.seek(0)

    text_pages: list[str] = []
    word_pages: list[list[dict]] = []
    with pdfplumber.open(io.BytesIO(raw)) as pdf:
        for page in pdf.pages:
            text_pages.append(page.extract_text() or "")
            word_pages.append(
                [
                    {key: word[key] for key in ("text", "x0", "x1", "top")}
                    for word in page.extract_words(keep_blank_chars=False)
                ]
            )
    return text_pages, word_pages


def parse(source) -> ParsedOrder:
    """Read an order confirmation. Raises `NotARockAutoOrder` for anything else."""
    return parse_document(*read_pdf(source))


def parse_document(text_pages: list[str], word_pages: list[list[dict]]) -> ParsedOrder:
    """Parse from text and geometry that have already been extracted.

    Split out from `parse` so the test corpus can be **redacted word geometry**
    rather than the original PDFs. Those carry a real name, street address,
    phone number and email in the Ship To block, and none of that belongs in a
    repository — the same rule the scan-report corpus follows (§8.3a, NFR-S-5).
    """
    text = "\n".join(text_pages)
    if not FINGERPRINT.search(text):
        raise NotARockAutoOrder("This is not a RockAuto order confirmation.")

    order = ParsedOrder()
    if found := ORDER_NUMBER.search(text):
        order.order_number = found.group(1)
    if found := ORDER_DATE.search(text):
        order.ordered_on = _a_date(found.group(1))
    if found := SHIPPING.search(text):
        order.shipping_minor = _minor(found.group("amount"))
    if found := TAX.search(text):
        order.tax_minor = _minor(found.group("amount"))
    if found := ORDER_TOTAL.search(text):
        order.total_minor = _minor(found.group("amount"))
    if found := PAYMENT.search(text):
        order.payment_method = found.group("method").strip()

    for words in word_pages:
        _read_items(_rows(words), order)

    if not order.lines:
        order.warnings.append("No parts were found in this order.")
    _check_totals(order)
    return order


def _a_date(text: str) -> date | None:
    for pattern in ("%B %d, %Y", "%b %d, %Y"):
        try:
            return datetime.strptime(text.strip(), pattern).date()
        except ValueError:
            continue
    return None


def _read_items(rows: list[list[dict]], order: ParsedOrder) -> None:
    columns = _columns(rows)
    if columns is None:
        return

    header_top = next(
        row[0]["top"]
        for row in rows
        if "Part Number" in " ".join(w["text"] for w in row)
    )

    body = [row for row in rows if row[0]["top"] > header_top]

    # Where the parts stop and the money starts.
    for index, row in enumerate(body):
        first = " ".join(w["text"] for w in row[:2])
        if any(first.startswith(word) for word in END_OF_ITEMS):
            body = body[:index]
            break

    priced = [
        row for row in body if _cell(row, columns.price, columns.core).strip().startswith("$")
    ]

    # A rebate prints as its own row: a label on the left and a negative amount
    # in the Total column, with nothing in Price. Left alone it was treated as
    # wrapped text and glued onto the nearest part number — `17D1083MHF1
    # Instant $7 Manufacturer Rebate!` — while its seven dollars went missing
    # from the arithmetic, which is what the totals warning was really saying.
    discounts = []
    for row in body:
        if any(row is other for other in priced):
            continue
        amount = _cell(row, columns.total, 10_000).strip()
        if not amount.startswith("-"):
            continue
        label = re.sub(r"\s+", " ", _cell(row, 0, columns.total)).strip()
        value = _minor(amount)
        if value:
            discounts.append(row)
            order.adjustments.append((label, value))
            order.discount_minor += value
    priced_or_money = priced + discounts
    # In document order, so a group heading applies to everything under it.
    vehicle = ""
    for row in body:
        if any(row is other for other in discounts):
            continue
        text = " ".join(w["text"] for w in row).strip()
        is_priced = any(row is other for other in priced)

        if not is_priced and VEHICLE_HEADING.match(text) and "$" not in text:
            match = VEHICLE_HEADING.match(text)
            vehicle = f"{match.group('year')} {match.group('rest')}".strip()
            continue

        if not is_priced:
            continue

        line = OrderLine(vehicle=vehicle)
        fragments = _fragments_for(row, body, priced_or_money, columns)

        identity = " ".join([_cell(row, 0, columns.description), *fragments["identity"]])
        # `90K38156B (90K-` + `38156B)` is one part number that ran out of line,
        # not two words. The space only exists because of where it wrapped.
        identity = re.sub(r"-\s+", "-", identity)
        line.brand, line.part_number = _split_identity(identity.split())

        description = " ".join(
            [_cell(row, columns.description, columns.price), *fragments["description"]]
        ).strip()
        if KIT_COMPONENT.match(description):
            line.is_kit_component = True
            description = KIT_COMPONENT.sub("", description)
        line.description = re.sub(r"\s+", " ", description).strip()

        line.unit_price_minor = _minor(_cell(row, columns.price, columns.core))

        core = _cell(row, columns.core, columns.quantity).strip()
        line.core_minor = None if core.startswith("-") else _minor(core)

        quantity = _cell(row, columns.quantity, columns.total).strip()
        try:
            line.quantity = Decimal(quantity or "1")
        except InvalidOperation:
            line.quantity = Decimal(1)
            order.warnings.append(f"Unreadable quantity on {line.label or description!r}.")

        total = _cell(row, columns.total, 10_000).strip()
        line.total_minor = None if total.startswith("-") or not total else _minor(total)
        if line.total_minor is None:
            line.is_kit_component = True

        order.lines.append(line)


def _fragments_for(row, body, priced, columns) -> dict[str, list[str]]:
    """Wrapped text from the rows above and below that belongs to this one.

    A fragment row carries no money at all. It attaches to whichever priced row
    is vertically nearest, which is what the printer meant by putting it there,
    and fragments are kept in printed order so a description reads the way it
    was laid out.
    """
    collected = {"identity": [], "description": []}
    for other in body:
        if any(other is p for p in priced):
            continue
        text = " ".join(w["text"] for w in other).strip()
        if not text or VEHICLE_HEADING.match(text):
            continue
        nearest = min(
            priced, key=lambda candidate: abs(candidate[0]["top"] - other[0]["top"])
        )
        if nearest is not row:
            continue
        above = other[0]["top"] < row[0]["top"]
        identity = _cell(other, 0, columns.description).strip()
        description = _cell(other, columns.description, columns.price).strip()
        if identity:
            collected["identity"].insert(0, identity) if above else collected[
                "identity"
            ].append(identity)
        if description:
            collected["description"].insert(0, description) if above else collected[
                "description"
            ].append(description)
    return collected


def _split_identity(tokens: list[str]) -> tuple[str, str]:
    """Brand and part number out of `['DELPHI', 'TD4056W']`.

    The brand is the leading run of alphabetic words; the part number is what
    is left. Multi-word brands (`FOUR SEASONS`) and part numbers carrying their
    own spaces (`90K38156B (90K-38156B)`) both survive that rule, and neither
    is rare enough to hand-wave.
    """
    if not tokens:
        return "", ""
    brand: list[str] = []
    for index, token in enumerate(tokens):
        if token.isalpha() and index == len(brand):
            brand.append(token)
        else:
            break
    number = " ".join(tokens[len(brand) :]).strip()
    if not number:  # every token was alphabetic; the last one is the number
        return " ".join(brand[:-1]), brand[-1]
    return " ".join(brand), number


def _check_totals(order: ParsedOrder) -> None:
    """Say so when the arithmetic does not agree.

    Not a rejection — a warning on the review screen. The document is the
    record; if this cannot reconcile it, the reader needs to know which line to
    look at, not to be told the file is unreadable.
    """
    if not order.total_minor:
        return
    computed = (
        order.subtotal_minor + order.tax_minor + order.shipping_minor - order.discount_minor
    )
    difference = computed - order.total_minor
    if abs(difference) > 2:  # a cent or two of rounding is not worth a warning
        order.warnings.append(
            f"The lines add up to {computed / 100:.2f} but the order total says "
            f"{order.total_minor / 100:.2f}. Check the quantities and core charges."
        )
