"""What every supplier order reader produces, and how one gets chosen.

`rockauto.py` was the only reader, so the shape it returned lived inside it and
the import service named RockAuto in half a dozen places. A second vendor makes
that arrangement wrong in a way worth fixing rather than working around: the
*document* differs between suppliers and nothing else does. A parsed order is a
number, a date, some money and some lines, whoever printed it.

So the shape lives here, the vendor identifies itself **on the order it
returns** rather than being assumed by whatever applies it, and `read` picks a
reader by asking each one whether it recognizes the file.

**Recognition is the reader's own job and it is allowed to say no.** A reader
that tries hardest wins nothing: extracting confident nonsense from a document
of the wrong shape is worse than refusing it, because the numbers look
plausible and land in the shop's cost history. Every reader raises rather than
guesses, and `read` reports which formats it tried.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from homeautoshop.core.measurements import Money


class UnreadableOrder(ValueError):
    """The file is a PDF and no reader recognized it."""


#: How a seller writes that one line is several of something — `2Pcs`,
#: `6-Pack`, `Case of 4`.
#:
#: **This is quoted, never counted**, and the pattern is deliberately generous
#: because of it. Run across the sample orders it finds `2Pcs` on a pack of
#: relays and `6-Pack` on a kit, misses `1 Gallon (Case of 4)` written as
#: `Case of 4`... which it now catches, and would have called `1-Pack` a
#: multipack. Those are three different kinds of wrong in one small sample, and
#: they are why no number extracted from here is allowed to reach a quantity: a
#: seller's product title is marketing copy, and a count taken from it silently
#: doubles what the shelf claims to hold and halves what it claims to have cost.
#:
#: So the phrase is shown next to the count on the review screen, in the
#: seller's own words, and the operator types the number. The same division of
#: labor the rest of that screen already makes — the reader states what the
#: document says, and the person decides what it means.
PACK_PHRASE = re.compile(
    r"(?:\b(?:case|pack|packs|set|box|lot|bundle)\s+of\s+\d{1,4}\b"
    r"|(?<![\w./-])\d{1,4}\s*-?\s*(?:pcs|pc|pieces|piece|packs|pack|pk|ct|count)\b)",
    re.I,
)


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
    #: **What the line was actually charged**, where the document states it.
    #:
    #: RockAuto prints a price each and leaves the multiplication to the
    #: reader; NAPA prints the extended figure and a list price beside it. The
    #: extended one is the better fact when it is given, for the reason
    #: `PurchaseLine.extended_minor` exists at all: five gallons sold for
    #: $182.39 has a per-unit price of $36.478, and reconstructing the total
    #: from a per-unit price rounded to the cent invents a penny.
    #:
    #: `None` means the document did not state it, and `charged_minor` falls
    #: back to multiplying out.
    extended_minor: int | None = None
    #: What the document says one costs before any line discount — `$227.99
    #: /Drum(s)`. Kept for the review screen, never for arithmetic.
    list_price_minor: int | None = None
    #: What came off this line, where the document itemizes it.
    line_discount_minor: int = 0
    #: `Drum(s)`, `Gal(s)` — what the vendor sells it by, which is not
    #: necessarily what the shop measures it in.
    sold_as: str = ""
    #: Who filled the order, where that is a different party from whoever made
    #: the thing. A marketplace prints `TC-Masterles` against a pack of relays,
    #: which is a seller and emphatically **not a brand** — filing it as one
    #: would put a stranger's account name in the catalog where the
    #: manufacturer belongs, and it would look right.
    sold_by: str = ""

    @property
    def label(self) -> str:
        return " ".join(x for x in (self.brand, self.part_number) if x)

    @property
    def pack_note(self) -> str:
        """The seller's own words for this line being several of something.

        `2Pcs`, `Case of 4` — returned verbatim and never parsed into a number.
        See `PACK_PHRASE` for why the count is asked for rather than taken.
        """
        found = PACK_PHRASE.search(self.description or "")
        return found.group(0).strip() if found else ""

    @property
    def charged_minor(self) -> int:
        """What this line comes to, preferring the figure the document states."""
        if self.extended_minor is not None:
            return self.extended_minor
        return int(Decimal(self.unit_price_minor) * self.quantity)

    # Minor units are how money is *stored* (§5.5) and never how it is shown.
    # The review screen printed `5379` in a column headed "Each", which is not
    # a price anybody recognizes — and on a screen whose entire job is letting
    # somebody check the numbers before they are written, an unreadable number
    # is worse than a missing one.

    @property
    def unit_price(self) -> Money:
        return Money(self.unit_price_minor, "USD")

    @property
    def extended(self) -> Money:
        return Money(self.charged_minor, "USD")

    @property
    def list_price(self) -> Money | None:
        return None if self.list_price_minor is None else Money(self.list_price_minor, "USD")

    @property
    def line_discount(self) -> Money | None:
        return Money(self.line_discount_minor, "USD") if self.line_discount_minor else None

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
    #: Who printed it, and the key its provenance is filed under. Carried on
    #: the order rather than assumed by whatever applies it — the import
    #: service used to name RockAuto in six places, which is five more than a
    #: second vendor can survive.
    vendor_name: str = ""
    vendor_url: str = ""
    source: str = ""

    order_number: str = ""
    ordered_on: date | None = None
    #: When it was actually collected or delivered, where the document says.
    #: A NAPA pickup order prints *Picked up Aug 31*, which is the date the
    #: return window runs from (FR-PUR-5) and is worth more than the order
    #: date for that purpose.
    received_on: date | None = None
    shipping_minor: int = 0
    tax_minor: int = 0
    total_minor: int = 0
    discount_minor: int = 0
    payment_method: str = ""
    #: What the document itself says its lines come to, where it prints a
    #: subtotal. Kept beside the computed one so `reconciles` can compare them.
    stated_subtotal_minor: int | None = None
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
            line.charged_minor + (line.core_minor or 0) for line in self.charged_lines
        )

    @property
    def computed_total_minor(self) -> int:
        """What the lines say the order comes to.

        **Discount before tax**, which is the order the vendors themselves work
        in: NAPA's own summary reads $228.05 of lines, $18.74 of tax and a
        $5.00 rewards discount against a $241.79 total, and 8.4% of $228.05 is
        $19.16 — it is 8.4% of $223.05 that gives $18.74. The document is the
        evidence for the rule this application now follows.
        """
        return (
            max(self.subtotal_minor - self.discount_minor, 0)
            + self.tax_minor
            + self.shipping_minor
        )

    @property
    def reconciles(self) -> bool:
        """Whether what was read adds up to what the page says it came to.

        The check that needs no fixture written by hand, and the only one that
        catches a misread price, a dropped line, or a quantity read as one when
        it was two.
        """
        return not self.total_minor or self.computed_total_minor == self.total_minor

    @property
    def vehicles(self) -> list[str]:
        seen: list[str] = []
        for line in self.lines:
            if line.vehicle and line.vehicle not in seen:
                seen.append(line.vehicle)
        return seen


def readers() -> list:
    """Every reader, in the order they are offered the file.

    Imported here rather than at module scope because each one pulls in a PDF
    library, and this module is imported by the model layer through
    `service.py`.
    """
    from . import amazon, napa, rockauto

    return [rockauto, napa, amazon]


def formats() -> list[str]:
    """What the import screen can say it understands."""
    return [reader.VENDOR_NAME for reader in readers()]


def read(source) -> ParsedOrder:
    """The first reader that recognizes the document, or a refusal naming all.

    Each reader raises its own `ValueError` when the fingerprint does not
    match, so the loop is "ask, and move on" rather than "try to parse and see
    whether it looks sane". A reader is allowed to be certain it is the wrong
    one; none of them is allowed to guess it is the right one.
    """
    import logging

    raw = source.read() if hasattr(source, "read") else source
    tried = []
    for reader in readers():
        tried.append(reader.VENDOR_NAME)
        try:
            return reader.parse(raw)
        except ValueError:
            # "Not mine" — the ordinary answer from every reader but one.
            continue
        except Exception:  # noqa: BLE001
            # Something worse: a corrupt file, or a bug in that reader. Neither
            # is a reason to stop asking the others, because a file the next
            # one understands perfectly should not be refused on account of the
            # previous one falling over. Logged rather than swallowed, so a bug
            # here is still a bug somebody can find.
            logging.getLogger(__name__).exception("%s could not read the file", reader.VENDOR_NAME)
            continue
    raise UnreadableOrder(
        "This does not look like an order from %s." % ", ".join(tried)
    )
