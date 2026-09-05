"""Reading a supplier order, whoever printed it (SPEC FR-PUR-1, §8.3a).

RockAuto was the only reader, so the shape it returned lived inside it and the
import service named RockAuto in six places. Two more vendors make that the
wrong arrangement: what differs between suppliers is the *document* and nothing
else, so the shape moved to `orders.py`, the vendor identifies itself on the
order it returns, and the file is recognized rather than declared.

The three read very differently, and each difference is load-bearing:

* **RockAuto** prints a price each, a core charge and the vehicle every line
  was looked up against. It is a parts document and every line belongs in the
  catalog.
* **NAPA** prints the *extended* figure — `$182.39` for a five-gallon drum —
  beside a list price and a discount. Taking it directly is the whole reason
  `extended_minor` exists: dividing it by five gives $36.478, and a per-unit
  price rounded to the cent multiplies back to $182.40.
* **Amazon** is not a parts vendor at all. One of the samples carries eight
  items of which exactly one is a part; the rest are tools. Nothing in the
  document says which, so nothing here guesses, and the review screen asks.

The fixtures are captures with the address blocks removed — see
`Artifacts/samples/parts-orders/README.md`. These tests run against the parsers
directly, so they need no PDF.
"""

from __future__ import annotations

from decimal import Decimal

from django.test import TestCase

from homeautoshop.purchasing.importers import amazon, napa, orders


def rows(*lines, spacing=15.0, start=100.0):
    """`(top, text)` at an even spacing, which is what a printed page is."""
    return [(start + n * spacing, text) for n, text in enumerate(lines)]


#: The header sits further from the first item than the item's own rows sit
#: from each other, which is true of the printed page and is what the walk
#: upward uses. Built that way here so the fixture exercises the real geometry.
NAPA_PAGE = rows(
    "Order #33065705",
    "Pickup In Store",
    "Ordered On: Aug 28,2026",
    "Picked up Aug 31",
) + rows(
    "CRC Brakleen Brake Parts Cleaner Non-",
    "Flammable Chlorinated 5 gal (US)",
    "Part # CRC 05091",
    "Qty: 1",
    "$182.39",
    "$227.99 /Drum(s)",
    "- $45.60 Save Up to 20%",
    "Qty: 1",
    "The Original PB Blaster Penetrant - 1 US Gal",
    "Part # NCB 128PB",
    "Qty: 1",
    "$45.66",
    "$52.99 /Gal(s)",
    "- $7.33 Save Up to 20%",
    "Qty: 1",
    "Order Summary",
    "Subtotal(2 items): $228.05",
    "Tax: $18.74",
    "Napa Rewards Discount: -$5.00",
    "Total: $241.79",
    "VISA ending in 5690",
    start=200.0,
)


class NapaTests(TestCase):
    def order(self):
        return napa.parse_document("Your Order History Details | NAPA Auto Parts", NAPA_PAGE)

    def test_it_reads_the_order_and_the_dates(self):
        order = self.order()

        self.assertEqual(order.vendor_name, "NAPA Auto Parts")
        self.assertEqual(order.order_number, "33065705")
        self.assertEqual(str(order.ordered_on), "2026-08-28")

    def test_picked_up_is_when_it_was_received(self):
        """The page prints `Picked up Aug 31` and no year, because it is
        showing you this year's orders. It is also the date a return window
        runs from (FR-PUR-5), so it is worth resolving rather than dropping."""
        self.assertEqual(str(self.order().received_on), "2026-08-31")

    def test_a_line_keeps_the_figure_the_document_states(self):
        """$182.39 for the drum, not five times a rounded per-gallon price."""
        line = self.order().lines[0]

        self.assertEqual(line.charged_minor, 18239)
        self.assertEqual(line.list_price_minor, 22799)
        self.assertEqual(line.line_discount_minor, 4560)
        self.assertEqual(line.sold_as, "Drum(s)")

    def test_a_wrapped_description_is_rejoined_across_the_break(self):
        """`Cleaner Non-` / `Flammable` is one word split over two rows, and a
        space in the middle of it goes into the catalog under that name."""
        self.assertEqual(
            self.order().lines[0].description,
            "CRC Brakleen Brake Parts Cleaner Non-Flammable Chlorinated 5 gal (US)",
        )

    def test_the_page_header_is_not_part_of_the_first_item(self):
        """Walking up from the part number is bounded by the page's own line
        spacing. Without that the first item swallowed the promotional banner
        printed across the top and went into the catalog named after a
        discount code."""
        page = [(20.0, "Up To 20% Off! Code LABORDAY2026")] + [
            (top + 200, text) for top, text in NAPA_PAGE
        ]

        order = napa.parse_document("NAPA Auto Parts", page)

        self.assertNotIn("LABORDAY2026", order.lines[0].description)

    def test_the_brand_and_number_come_off_the_part_line(self):
        line = self.order().lines[1]

        self.assertEqual(line.brand, "NCB")
        self.assertEqual(line.part_number, "128PB")

    def test_the_totals_are_the_vendors_own(self):
        order = self.order()

        self.assertEqual(order.stated_subtotal_minor, 22805)
        self.assertEqual(order.subtotal_minor, 22805)
        self.assertEqual(order.tax_minor, 1874)
        self.assertEqual(order.discount_minor, 500)
        self.assertEqual(order.total_minor, 24179)

    def test_the_discount_named_the_way_the_page_named_it(self):
        self.assertEqual(self.order().adjustments, [("Napa Rewards Discount", 500)])

    def test_it_reconciles_and_that_is_the_evidence_for_the_tax_rule(self):
        """8.4% of $228.05 is $19.16; it is 8.4% of $223.05 that gives $18.74.

        The vendor takes its discount off *before* working out the tax, which
        is what `Purchase.taxable_minor` now does. This document is where that
        was confirmed rather than assumed, so the reconciliation is worth
        asserting as a fact about the arithmetic and not only about the parser.
        """
        order = self.order()

        self.assertTrue(order.reconciles)
        self.assertEqual(order.computed_total_minor, 24179)
        self.assertEqual(order.warnings, [])

    def test_a_document_of_another_shape_is_refused(self):
        with self.assertRaises(ValueError):
            napa.parse_document("", rows("RockAuto Order Confirmation", "Order 137001514"))


AMAZON_INVOICE = [
    (100.0, 42.0, "Final Details for Order #114-2708267-4265045", False),
    (115.0, 42.0, "Order Placed: August 22, 2026", False),
    (130.0, 42.0, "Order Total: USD 208.44", False),
    (145.0, 42.0, "Shipped on August 22, 2026", False),
    (160.0, 42.0, "Items Ordered Price", False),
    (175.0, 42.0, "1 of: CPS VG200 Digital Vacrometer Vacuum Gauge, Two Button Operation, $178.76", False),
    (175.0, 541.0, "$178.76", True),
    (190.0, 42.0, "Automatic", False),
    (205.0, 42.0, "Sold by and invoiced on behalf of: Pacific Star Corporation (seller profile)", False),
    (220.0, 42.0, "Condition: New", False),
    (235.0, 42.0, "1 of: 2Pcs 156700-2480 12VDC 30A 4Pins Automotive Relay $14.24", False),
    (235.0, 541.0, "$14.24", True),
    (250.0, 42.0, "Sold by and invoiced on behalf of: TC-Masterles (seller profile)", False),
    (265.0, 42.0, "Condition: New", False),
    (280.0, 45.0, "Shipping Address: Item(s) Subtotal: $193.00", False),
    (280.0, 451.0, "Item(s) Subtotal: $193.00", True),
    (295.0, 42.0, "Payment information", False),
    (310.0, 42.0, "Amazon.com Visa | Last digits: 5690", False),
    (325.0, 451.0, "Shipping & Handling: USD 0.00", True),
    (340.0, 451.0, "Total before tax: USD 193.00", True),
    (355.0, 451.0, "Estimated tax to be collected: USD 15.44", True),
    (370.0, 451.0, "Grand Total: USD 208.44", True),
]


class AmazonTests(TestCase):
    def order(self):
        return amazon.parse_document(AMAZON_INVOICE)

    def test_it_reads_the_invoice(self):
        order = self.order()

        self.assertEqual(order.vendor_name, "Amazon")
        self.assertEqual(order.order_number, "114-2708267-4265045")
        self.assertEqual(str(order.ordered_on), "2026-08-22")
        self.assertEqual(str(order.received_on), "2026-08-22")

    def test_the_printed_price_is_a_price_each(self):
        """`3 of: Rain-X ... $5.97` against `Item(s) Subtotal: $17.91` settles
        it, and nothing on a single-quantity invoice would have. Read as an
        extended figure, six gallons of washer fluid reported as $11.94 of
        $35.82 — and the reconciliation is what said so."""
        page = list(AMAZON_INVOICE)
        page[5] = (175.0, 42.0, "3 of: Rain-X Washer Fluid - 1 Gallon $5.97", False)

        line = amazon.parse_document(page).lines[0]

        self.assertEqual(line.quantity, Decimal(3))
        self.assertEqual(line.unit_price_minor, 597)
        self.assertEqual(line.charged_minor, 1791)

    def test_the_seller_is_not_filed_as_a_brand(self):
        """`TC-Masterles` is somebody's marketplace account, and in the brand
        column it would read as authoritative."""
        line = self.order().lines[1]

        self.assertEqual(line.brand, "")
        self.assertEqual(line.sold_by, "TC-Masterles")

    def test_the_right_hand_column_is_not_read_into_a_description(self):
        """It is re-read on its own so the totals parse; against an item it is
        the price echoed back, and appending it puts `$14.24` on the end of a
        product name and then into the catalog under that name."""
        for line in self.order().lines:
            with self.subTest(description=line.description):
                self.assertNotIn("$", line.description)

    def test_a_title_wrapping_below_its_price_is_still_one_title(self):
        self.assertIn("Automatic", self.order().lines[0].description)

    def test_the_totals_come_from_the_end_of_the_document(self):
        """Every figure is printed twice — once per parcel and once for the
        order — and taking the first would report a two-parcel order as costing
        whatever the first parcel cost."""
        order = self.order()

        self.assertEqual(order.tax_minor, 1544)
        self.assertEqual(order.total_minor, 20844)
        self.assertTrue(order.reconciles)

    def test_a_document_of_another_shape_is_refused(self):
        with self.assertRaises(ValueError):
            amazon.parse_document([(1.0, 1.0, "Order #33065705", False)])


class ChoosingAReaderTests(TestCase):
    def test_the_formats_are_named(self):
        self.assertEqual(orders.formats(), ["RockAuto", "NAPA Auto Parts", "Amazon"])

    def test_a_file_nothing_recognizes_says_what_it_tried(self):
        """"This is not a RockAuto order" was a complete answer while RockAuto
        was the only reader. The useful thing to tell somebody now is what this
        screen *does* read."""
        with self.assertRaises(orders.UnreadableOrder) as refused:
            orders.read(b"%PDF-1.4 not really")

        self.assertIn("RockAuto", str(refused.exception))
        self.assertIn("NAPA Auto Parts", str(refused.exception))
        self.assertIn("Amazon", str(refused.exception))

    def test_one_reader_falling_over_does_not_hide_the_others(self):
        """A corrupt file makes a PDF library raise something that is not a
        refusal. A file the next reader understands perfectly should not be
        turned away because the previous one fell over."""
        from unittest import mock

        from homeautoshop.purchasing.importers import rockauto

        with mock.patch.object(rockauto, "parse", side_effect=RuntimeError("boom")):
            with self.assertRaises(orders.UnreadableOrder):
                orders.read(b"%PDF-1.4 not really")


class BringingInOnlySomeOfItTests(TestCase):
    """The half an Amazon order needs and a parts order never did.

    RockAuto and NAPA sell nothing but parts, so every line belongs in the
    catalog and the question never arose. An Amazon basket carries eight items
    of which one is a part and seven are tools — and committing all eight would
    put $455 of tooling into the shop's parts spend, where it reads perfectly
    plausibly and is wrong for ever (G-4).

    So the money follows the choice. Tax and shipping are shared across what is
    kept, because the alternative is a purchase claiming the shop paid $32.72
    of tax on $14.24 of relays.
    """

    def setUp(self):
        from homeautoshop.accounts.models import Role, User

        self.user = User.objects.create_user(
            username="andy", password="x" * 16, role=Role.ADMIN
        )

    def order(self):
        """Two items, $178.76 of tool and $14.24 of part, $15.44 of tax."""
        return amazon.parse_document(AMAZON_INVOICE)

    def run_import(self, keep=None):
        from homeautoshop.purchasing.importers import service

        return service.run(self.order(), dry_run=False, user=self.user, keep=keep)

    def test_everything_by_default(self):
        report = self.run_import()

        self.assertEqual(report.purchase.lines.count(), 2)
        self.assertEqual(report.purchase.subtotal_minor, 19300)

    def test_keeping_one_line_leaves_the_other_out(self):
        report = self.run_import(keep={1})

        line = report.purchase.lines.get()
        self.assertEqual(line.extended_minor, 1424)
        self.assertIn("Relay", line.description_as_ordered)

    def test_and_leaves_out_its_share_of_the_tax(self):
        """$15.44 of tax on $193.00 of items; $14.24 of that is 7.4%, so
        $1.14. Recording the whole $15.44 against a $14.24 purchase would make
        the shop's cost history wrong in a way nothing on the screen shows."""
        report = self.run_import(keep={1})

        self.assertEqual(report.purchase.tax_minor, 114)

    def test_the_lines_left_out_are_reported_rather_than_dropped(self):
        """A screen that silently returns fewer lines than the document had is
        a screen nobody can check."""
        report = self.run_import(keep={1})

        skipped = [o for o in report.outcomes if o.skipped]
        self.assertEqual(len(skipped), 1)
        self.assertIn("Vacrometer", skipped[0].line.description)

    def test_and_it_says_the_money_was_shared_out(self):
        report = self.run_import(keep={1})

        self.assertTrue(
            any("shared out" in str(w) for w in report.warnings), report.warnings
        )

    def test_keeping_all_of_them_shares_nothing(self):
        report = self.run_import(keep={0, 1})

        self.assertEqual(report.purchase.tax_minor, 1544)
        self.assertNotIn(
            True, [bool("shared out" in str(w)) for w in report.warnings]
        )

    def test_keeping_none_is_a_real_answer(self):
        """An order that turned out to hold nothing for the shop."""
        report = self.run_import(keep=set())

        self.assertEqual(report.purchase.lines.count(), 0)
        self.assertEqual(report.purchase.tax_minor, 0)

    def test_a_part_is_created_for_what_was_kept_and_not_for_what_was_not(self):
        from homeautoshop.parts.models import Part

        self.run_import(keep={1})

        self.assertEqual(Part.objects.count(), 1)
        self.assertIn("Relay", Part.objects.get().name)

    def test_the_vendor_is_the_one_that_printed_the_document(self):
        from homeautoshop.purchasing.models import Vendor

        report = self.run_import()

        self.assertEqual(report.purchase.vendor.name, "Amazon")
        self.assertTrue(Vendor.objects.filter(name="Amazon").exists())

    def test_a_napa_order_records_when_it_was_picked_up(self):
        from homeautoshop.purchasing.importers import service

        order = napa.parse_document("NAPA Auto Parts", NAPA_PAGE)

        report = service.run(order, dry_run=False, user=self.user)

        self.assertEqual(str(report.purchase.received_on), "2026-08-31")
        self.assertEqual(report.purchase.vendor.name, "NAPA Auto Parts")

    def test_a_napa_line_keeps_the_price_the_document_printed(self):
        """The end-to-end version of the penny. $182.39 arrives as $182.39."""
        from homeautoshop.purchasing.importers import service

        order = napa.parse_document("NAPA Auto Parts", NAPA_PAGE)

        report = service.run(order, dry_run=False, user=self.user)

        drum = report.purchase.lines.first()
        self.assertEqual(drum.extended_minor, 18239)
        self.assertEqual(report.purchase.subtotal_minor, 22805)
        self.assertEqual(report.purchase.total_minor, 24179)

    def test_two_vendors_do_not_collide_on_provenance(self):
        """Both orders are filed under their own source, so re-reading one
        never finds the other's purchase."""
        from homeautoshop.purchasing.importers import service
        from homeautoshop.purchasing.models import Purchase

        service.run(self.order(), dry_run=False, user=self.user)
        service.run(
            napa.parse_document("NAPA Auto Parts", NAPA_PAGE),
            dry_run=False, user=self.user,
        )

        self.assertEqual(Purchase.objects.count(), 2)


class ToolingIsASpendAndNotAnInventoryTests(TestCase):
    """A line that is not a part can still be money the shop spent (OQ-4).

    The distinction the spec draws and this had to be built inside. **NG-8 and
    NG-9 put tool and toolbox tracking permanently out of scope** — what you
    own, what it is worth, which drawer it lives in, who borrowed it — and
    WrenchLedger is where that lives; `ShopTool` exists only as a deliberately
    thin cached shadow of one, with an allow-list that drops price and location
    so it cannot drift into being an inventory.

    **OQ-4 keeps `expense.category = tooling`** as first-class, always
    exported, and excluded from per-vehicle cost, because a torque wrench is
    not a cost of the Civic. So $455 of tools on an Amazon order is not a
    catalog entry and is not nothing either: it is one expense row.

    Most of what follows asserts what must *not* happen, because the line
    between a spend and an inventory is easy to write past a month from now.
    """

    def setUp(self):
        from homeautoshop.accounts.models import Role, User

        self.user = User.objects.create_user(
            username="andy", password="x" * 16, role=Role.ADMIN
        )

    def run_import(self, keep=None, as_tooling=None):
        from homeautoshop.purchasing.importers import service

        return service.run(
            amazon.parse_document(AMAZON_INVOICE),
            dry_run=False, user=self.user, keep=keep, as_tooling=as_tooling,
        )

    def expense(self):
        from homeautoshop.purchasing.models import Expense

        return Expense.objects.get()

    # -- what it does -------------------------------------------------------

    def test_a_tooling_line_becomes_an_expense(self):
        from homeautoshop.purchasing.models import ExpenseCategory

        self.run_import(keep={1}, as_tooling={0})

        spend = self.expense()
        self.assertEqual(spend.category, ExpenseCategory.TOOLING)
        self.assertIn("Vacrometer", spend.description)

    def test_it_carries_its_own_share_of_the_tax(self):
        """$15.44 of tax on $193.00 of items; the $178.76 tool's share is
        $14.30, so the expense is $193.06 and not the bare $178.76. Leaving
        the tax on the purchase would put the tax on a tool into the parts
        spend, which is the thing the choice exists to avoid."""
        self.run_import(keep={1}, as_tooling={0})

        self.assertEqual(self.expense().amount_minor, 17876 + 1430)

    def test_and_the_purchase_keeps_only_its_own(self):
        report = self.run_import(keep={1}, as_tooling={0})

        self.assertEqual(report.purchase.tax_minor, 114)
        self.assertEqual(report.purchase.subtotal_minor, 1424)

    def test_nothing_is_lost_between_the_two(self):
        """Every cent of the order lands somewhere, once. The allocator is the
        same largest-remainder one the kit split uses, so the shares add to the
        whole rather than to the whole less a couple of cents."""
        report = self.run_import(keep={1}, as_tooling={0})

        recorded = report.purchase.total_minor + self.expense().amount_minor
        self.assertEqual(recorded, 20844)

    def test_the_vendor_is_recorded_on_the_spend(self):
        self.run_import(keep={1}, as_tooling={0})

        self.assertEqual(self.expense().vendor.name, "Amazon")

    def test_it_is_dated_from_the_order(self):
        self.run_import(keep={1}, as_tooling={0})

        self.assertEqual(str(self.expense().incurred_on), "2026-08-22")

    def test_the_report_says_how_much_became_tooling(self):
        report = self.run_import(keep={1}, as_tooling={0})

        self.assertEqual(report.tooling_recorded, 17876 + 1430)
        self.assertTrue(any(o.tooling for o in report.outcomes))

    # -- and what it must never do -----------------------------------------

    def test_no_part_is_created_for_a_tool(self):
        """The whole point. A tool in the parts catalog is the catalog
        answering "what fits this car" with a tubing cutter."""
        from homeautoshop.parts.models import Part

        self.run_import(keep={1}, as_tooling={0})

        self.assertEqual(Part.objects.count(), 1)
        self.assertIn("Relay", Part.objects.get().name)

    def test_no_purchase_line_is_created_for_a_tool(self):
        report = self.run_import(keep={1}, as_tooling={0})

        self.assertEqual(report.purchase.lines.count(), 1)

    def test_no_tool_record_is_created_anywhere(self):
        """NG-8, as an assertion. `ShopTool` is a cached shadow of a
        WrenchLedger tool and is written by that sync alone; an importer
        reaching into it is this application starting to be a tool inventory,
        which is the thing it is documented as never becoming."""
        from homeautoshop.work.models import ShopTool

        self.run_import(keep={1}, as_tooling={0})

        self.assertEqual(ShopTool.objects.count(), 0)

    def test_the_spend_is_not_attached_to_a_vehicle(self):
        """A torque wrench is not a cost of the Civic (OQ-4). Null rather than
        merely excluded by a setting: an expense attached to no vehicle cannot
        be pulled into one by a later change of mind about
        `COST_INCLUDE_TOOLING`."""
        self.run_import(keep={1}, as_tooling={0})

        self.assertIsNone(self.expense().asset)
        self.assertIsNone(self.expense().work_order)

    def test_it_holds_no_specification_of_the_thing(self):
        """A description of a transaction, which is what the field is for —
        not a record of an object. There is nowhere here for a serial number, a
        model, a location or a valuation, and that is the design."""
        self.run_import(keep={1}, as_tooling={0})
        spend = self.expense()

        stored = {f.name for f in spend._meta.get_fields()}
        for absent in ("serial_number", "model", "location", "brand", "quantity"):
            with self.subTest(field=absent):
                self.assertNotIn(absent, stored)

    def test_reading_the_order_again_does_not_bank_it_twice(self):
        """The lines are replaced rather than added to on a re-import, and
        these have to go the same way or changing your mind about one item
        leaves the first answer behind as a second expense."""
        from homeautoshop.purchasing.models import Expense

        self.run_import(keep={1}, as_tooling={0})
        self.run_import(keep={1}, as_tooling={0})

        self.assertEqual(Expense.objects.count(), 1)

    def test_changing_your_mind_removes_the_expense(self):
        from homeautoshop.purchasing.models import Expense

        self.run_import(keep={1}, as_tooling={0})
        self.run_import(keep={0, 1})

        self.assertEqual(Expense.objects.count(), 0)

    def test_a_parts_order_records_no_tooling_at_all(self):
        """Nothing changes for a document where every line is a part."""
        from homeautoshop.purchasing.importers import service
        from homeautoshop.purchasing.models import Expense

        report = service.run(
            napa.parse_document("NAPA Auto Parts", NAPA_PAGE),
            dry_run=False, user=self.user,
        )

        self.assertEqual(Expense.objects.count(), 0)
        self.assertEqual(report.tooling_recorded, 0)
        self.assertEqual(report.purchase.total_minor, 24179)


class TheReviewScreenSaysWhatItKnowsTests(TestCase):
    """A column headed Part must not hold something that is not one.

    Reported from the screen: an Amazon order showed *sold by Pacific Star
    Corporation* under **Part**, because the seller had been put there and
    Amazon states no brand and no part number — so the column held nothing but
    the seller. A marketplace account name in a column headed Part reads as
    something a person could look up, and it is not.
    """

    def setUp(self):
        from homeautoshop.accounts.models import Role, User

        self.user = User.objects.create_user(
            username="andy", password="x" * 16, role=Role.ADMIN
        )
        self.client.force_login(self.user)

    def report(self, order):
        from homeautoshop.purchasing.importers import service

        return service.run(order, dry_run=True, user=self.user)

    def render(self, order):
        from django.template.loader import render_to_string

        from homeautoshop.purchasing.importers import orders as shapes

        return render_to_string(
            "purchasing/order_import.html",
            {"report": self.report(order), "held": "t", "held_name": "x.pdf",
             "formats": shapes.formats()},
        )

    def amazon_order(self):
        return amazon.parse_document(AMAZON_INVOICE)

    def napa_order(self):
        return napa.parse_document("NAPA Auto Parts", NAPA_PAGE)

    def test_a_document_that_states_no_identifiers_says_so(self):
        self.assertFalse(self.report(self.amazon_order()).shows_identifiers)

    def test_and_one_that_does_says_that(self):
        self.assertTrue(self.report(self.napa_order()).shows_identifiers)

    def test_the_part_column_is_absent_where_there_is_nothing_to_put_in_it(self):
        """Not blank cells. A column of empty cells is width taken from the
        description, which is the only identity such a line has."""
        page = self.render(self.amazon_order())
        table = page[page.index('<table class="orderlines"'):]

        self.assertNotIn(">Part<", table[:table.index("</thead>")])

    def test_and_present_where_there_is(self):
        page = self.render(self.napa_order())
        table = page[page.index('<table class="orderlines"'):]

        self.assertIn(">Part<", table[:table.index("</thead>")])
        self.assertIn("CRC", table)

    def test_the_seller_is_shown_with_the_description(self):
        """It is a fact about where the thing came from, which sits with what
        the thing is — not with what it is called in a catalog."""
        page = self.render(self.amazon_order())

        self.assertIn("sold by", page)
        self.assertIn("TC-Masterles", page)

    def test_a_core_nobody_stated_is_a_dash_and_not_a_zero(self):
        """The distinction the column already made, extended to documents that
        have no core column at all. RockAuto prints one, so a zero there is the
        claim that this line has no core charge; NAPA and Amazon say nothing
        about cores, and `$0.00` would be a claim they did not make."""
        for order in (self.amazon_order(), self.napa_order()):
            with self.subTest(vendor=order.vendor_name):
                for line in order.lines:
                    self.assertIsNone(line.core_minor)
                    self.assertIsNone(line.core)

    def test_which_does_not_disturb_the_money(self):
        """`core_minor` is `None` rather than `0` and every sum treats it as
        nothing, so the totals are unchanged by saying so honestly."""
        order = self.napa_order()

        self.assertEqual(order.subtotal_minor, 22805)
        self.assertEqual(order.total_minor, 24179)
        self.assertTrue(order.reconciles)


class OneLineCanBeSeveralThingsTests(TestCase):
    """A two-pack of relays is one line, one charge and **two relays**.

    Reported from the shelf: `1 of: 2Pcs 156700-2480 ... Automotive Relay` for
    $14.24. The importer copied Amazon's line-item count into `qty_ordered`,
    which has meant *how many of the part* everywhere else since the day a line
    started holding its extended price — the add-line form has always asked for
    it in those terms, which is why typing that same purchase by hand worked
    and reading it in did not. One relay went on the shelf at twice what it
    cost, and the second one did not exist.

    So the count is asked for and **the money is not**. A charge is the one
    thing an invoice is never ambiguous about; the count is the one thing it
    can be wrong about.
    """

    def setUp(self):
        from homeautoshop.accounts.models import Role, User

        self.user = User.objects.create_user(
            username="andy", password="x" * 16, role=Role.ADMIN
        )

    def order(self):
        """Line 0 is a $178.76 tool, line 1 is $14.24 of two-pack relay."""
        return amazon.parse_document(AMAZON_INVOICE)

    def run_import(self, **kwargs):
        from homeautoshop.purchasing.importers import service

        kwargs.setdefault("keep", {1})
        return service.run(self.order(), dry_run=False, user=self.user, **kwargs)

    # ---------------------------------------------------------- the quoting

    def test_the_seller_s_own_words_are_quoted(self):
        self.assertEqual(self.order().lines[1].pack_note, "2Pcs")

    def test_and_a_line_that_says_nothing_of_the_kind_quotes_nothing(self):
        self.assertEqual(self.order().lines[0].pack_note, "")

    def test_the_phrase_is_never_turned_into_a_count(self):
        """The whole design decision, kept as a test.

        Across three sample orders the obvious regex finds `2Pcs` on a pack of
        relays, calls `1-Pack` a multipack, misses `1 Gallon (Case of 4)`, and
        reads `6-Pack` off a product title that is one kit. That is three
        different kinds of wrong in one small sample — and a count nobody
        stated silently doubles what the shelf claims to hold and halves what
        it claims to have cost, which is a cost history that reads plausibly
        and is wrong for ever (G-4).

        So the phrase is shown next to the box and the operator types the
        number. Reading the document alone changes nothing.
        """
        report = self.run_import()

        line = report.purchase.lines.get()
        self.assertIn("2Pcs", line.description_as_ordered)
        self.assertEqual(line.qty_ordered, Decimal(1))

    # ----------------------------------------------------------- the count

    def test_saying_it_was_two_records_two(self):
        report = self.run_import(counts={1: Decimal(2)})

        self.assertEqual(report.purchase.lines.get().qty_ordered, Decimal(2))

    def test_and_moves_no_money(self):
        """`extended_minor` is the document's own figure and stays it."""
        report = self.run_import(counts={1: Decimal(2)})

        self.assertEqual(report.purchase.lines.get().extended_minor, 1424)
        self.assertEqual(report.purchase.subtotal_minor, 1424)

    def test_so_the_price_each_is_worked_out_from_what_was_charged(self):
        report = self.run_import(counts={1: Decimal(2)})

        self.assertEqual(report.purchase.lines.get().unit_price_shown, "$7.12")

    def test_including_when_it_does_not_divide_into_cents(self):
        """$14.24 across three is $4.7466⅔ — not a number of cents, and not a
        reason to invent one. The same rule as five gallons at $182.39
        (FR-PUR-11): the total is the money fact, the per-unit figure is a rate
        derived from it, and it is shown to the precision that makes it true.
        """
        report = self.run_import(counts={1: Decimal(3)})

        line = report.purchase.lines.get()
        self.assertEqual(line.extended_minor, 1424)
        self.assertEqual(line.unit_price_shown, "4.7467")

    def test_a_count_of_nothing_is_not_a_count(self):
        """Zero would make a line with no parts on it and a division by zero
        behind every price it shows. The document's own number stands."""
        report = self.run_import(counts={1: Decimal(0)})

        self.assertEqual(report.purchase.lines.get().qty_ordered, Decimal(1))

    def test_the_order_still_adds_up_to_what_it_cost(self):
        """The check that catches a count leaking into the money. $178.76 of
        tool plus $14.24 of relays plus $15.44 of tax is $208.44 however many
        relays were in the packet."""
        from homeautoshop.purchasing.importers import service

        order = self.order()
        report = service.run(
            order, dry_run=False, user=self.user,
            keep={1}, as_tooling={0}, counts={1: Decimal(2)},
        )

        self.assertTrue(order.reconciles)
        self.assertEqual(
            report.purchase.total_minor + report.tooling_recorded, order.total_minor
        )

    # ----------------------------------------------------------- the shelf

    def test_receiving_it_puts_both_relays_on_the_shelf(self):
        """What the whole thing is for. Two parts, each costing half of
        $14.24 plus its share of the tax — not one part costing $14.24 and a
        relay that is in the drawer and in no record anywhere."""
        report = self.run_import(counts={1: Decimal(2)})
        line = report.purchase.lines.get()

        lot = line.receive(user=self.user)
        lot.refresh_from_db()

        self.assertEqual(lot.qty_on_hand, Decimal(2))
        # $7.12 each, plus half of the $1.14 tax apportioned to this line.
        self.assertEqual(lot.unit_cost_minor, 769)

    # --------------------------------------------------------- the screen

    def test_a_kit_component_keeps_the_vendor_s_count(self):
        """It carries no money and no count of its own — how many are in the
        box is the kit's question, and `_record_kit_item` already divides the
        kit's own quantity out of it."""
        from homeautoshop.purchasing.importers import service
        from homeautoshop.purchasing.importers.orders import OrderLine, ParsedOrder

        order = ParsedOrder(
            vendor_name="RockAuto", source="rockauto", order_number="7",
            total_minor=0,
            lines=[
                OrderLine(brand="KYB", part_number="KIT333432", description="Strut Kit",
                          unit_price_minor=35879, quantity=Decimal(1)),
                OrderLine(brand="KYB", part_number="SB101", description="Bellow",
                          unit_price_minor=846, quantity=Decimal(2),
                          is_kit_component=True, total_minor=None),
            ],
        )
        report = service.run(
            order, dry_run=False, user=self.user, counts={1: Decimal(99)}
        )

        self.assertEqual(report.outcomes[1].units, Decimal(2))

    def render(self, report):
        from django.template.loader import render_to_string

        return render_to_string(
            "purchasing/order_import.html",
            {"report": report, "held": "t", "held_name": "x.pdf",
             "formats": orders.formats()},
        )

    def preview(self, **kwargs):
        from homeautoshop.purchasing.importers import service

        return service.run(self.order(), dry_run=True, user=self.user, **kwargs)

    def test_the_screen_asks_per_line(self):
        page = self.render(self.preview())

        self.assertIn('name="count_0"', page)
        self.assertIn('name="count_1"', page)

    def test_prefilled_with_what_the_document_counted(self):
        """Not with anything read out of a product title. The default is a
        number somebody actually printed."""
        import re

        page = re.sub(r"\s+", " ", self.render(self.preview()))

        self.assertIn('name="count_1" value="1"', page)

    def test_and_the_phrase_sits_beside_the_box(self):
        page = self.render(self.preview())

        self.assertIn("2Pcs", page)
        self.assertIn("the description says", page)

    def test_the_box_stays_on_a_line_marked_as_tooling(self):
        """The count belongs to the line, not to the choice. Gating it on *is
        a part* meant marking something tooling took its number away — and the
        two controls are submitted together, so somebody is expected to move
        between them freely."""
        page = self.render(self.preview(keep=set(), as_tooling={0, 1}))

        self.assertIn('name="count_1"', page)

    def test_what_it_would_do_shows_the_price_each_it_would_use(self):
        page = self.render(self.preview(counts={1: Decimal(2)}))

        self.assertIn("$7.12", page)


class TheCountComesOffTheReviewScreenTests(TestCase):
    """The half of it that is a form: what the view will and will not accept."""

    def setUp(self):
        from homeautoshop.accounts.models import Role, User

        self.user = User.objects.create_user(
            username="andy", password="x" * 16, role=Role.ADMIN
        )

    def counts(self, **posted):
        from django.test import RequestFactory

        from homeautoshop.purchasing import views_import

        request = RequestFactory().post("/purchases/import/", posted)
        return views_import._counts(request)

    def test_a_number_is_taken(self):
        self.assertEqual(self.counts(count_3="2"), ({3: Decimal(2)}, False))

    def test_fractions_are_allowed_because_some_parts_are_measured_out(self):
        self.assertEqual(self.counts(count_0="2.5"), ({0: Decimal("2.5")}, False))

    def test_something_that_is_not_a_number_is_dropped_and_said_so(self):
        """Rather than guessed at. Falling back to the document's own count is
        the conservative direction — it is a figure somebody printed, and it is
        what this screen did before it asked at all."""
        self.assertEqual(self.counts(count_1="two"), ({}, True))

    def test_and_so_is_a_count_nobody_could_have_meant(self):
        for value in ("0", "-4", "999999"):
            with self.subTest(value=value):
                self.assertEqual(self.counts(count_1=value), ({}, True))

    def test_keys_that_are_not_line_numbers_are_ignored(self):
        self.assertEqual(self.counts(count_x="3", counted="3"), ({}, False))


class DeletingAnOrderReallyDeletesItTests(TestCase):
    """The state a deleted order used to leave behind, and could not leave now.

    A soft delete runs no SQL DELETE, so `PurchaseLine.purchase`'s `CASCADE`
    never fired and the lines stayed alive under an order every screen hid —
    reachable from nowhere, because a line is only ever listed through its
    order. Re-reading the document then hit the importer's `already_imported`
    check, which resolved the surviving `ExternalRef` through `all_objects` and
    found the trashed purchase, and refused on the strength of received lines
    nobody could see. Four small decisions, and together they made an order
    permanently un-re-readable.
    """

    def setUp(self):
        from homeautoshop.accounts.models import Role, User

        self.user = User.objects.create_user(
            username="andy", password="x" * 16, role=Role.ADMIN
        )

    def run_import(self):
        from homeautoshop.purchasing.importers import service

        return service.run(
            amazon.parse_document(AMAZON_INVOICE), dry_run=False, user=self.user
        )

    def test_the_lines_go_into_the_trash_with_the_order(self):
        from homeautoshop.purchasing.models import PurchaseLine

        purchase = self.run_import().purchase
        purchase.delete()

        self.assertEqual(PurchaseLine.objects.filter(purchase=purchase).count(), 0)
        self.assertEqual(PurchaseLine.all_objects.filter(purchase=purchase).count(), 2)

    def test_a_bulk_delete_takes_them_too(self):
        """`delete_selected` in the admin goes through the queryset, not the
        instance, and that was the path with no cascade at all."""
        from homeautoshop.purchasing.models import Purchase, PurchaseLine

        purchase = self.run_import().purchase
        Purchase.objects.filter(pk=purchase.pk).delete()

        self.assertEqual(PurchaseLine.objects.filter(purchase=purchase).count(), 0)

    def test_restoring_brings_its_own_lines_back(self):
        from homeautoshop.purchasing.models import PurchaseLine

        purchase = self.run_import().purchase
        purchase.delete()
        purchase.restore()

        self.assertEqual(PurchaseLine.objects.filter(purchase=purchase).count(), 2)

    def test_but_not_a_line_somebody_had_already_deleted(self):
        """The shared timestamp is what tells the two apart. A line deleted on
        its own is a decision somebody made, and restoring the order is not a
        reason to undo it."""
        from homeautoshop.purchasing.models import PurchaseLine

        purchase = self.run_import().purchase
        dropped = purchase.lines.first()
        dropped.delete()
        purchase.delete()
        purchase.restore()

        self.assertEqual(PurchaseLine.objects.filter(purchase=purchase).count(), 1)
        self.assertFalse(PurchaseLine.objects.filter(pk=dropped.pk).exists())

    def test_the_order_can_be_read_in_again_afterwards(self):
        purchase = self.run_import().purchase
        purchase.delete()

        report = self.run_import()

        self.assertFalse(report.already_imported)
        self.assertEqual(report.purchase.lines.count(), 2)
        self.assertNotEqual(report.purchase.pk, purchase.pk)

    def test_even_when_a_line_had_been_received(self):
        """The case that produced the warning nobody could act on."""
        from homeautoshop.parts.models import Part

        purchase = self.run_import().purchase
        line = purchase.lines.exclude(part=None).first()
        line.receive(qty=1, user=self.user)
        purchase.refresh_from_db()
        purchase.delete()

        report = self.run_import()

        self.assertFalse(report.already_imported)
        self.assertEqual(report.warnings, [])
        self.assertTrue(Part.objects.exists())

    def test_the_stale_provenance_row_is_dropped_rather_than_left(self):
        from homeautoshop.core.models import ExternalRef

        purchase = self.run_import().purchase
        purchase.delete()
        report = self.run_import()

        refs = ExternalRef.objects.filter(external_type="order")
        self.assertEqual(refs.count(), 1)
        self.assertEqual(refs.first().entity_id, report.purchase.pk)

    def test_an_order_that_is_still_here_is_still_recognized(self):
        """The guard the fix must not have removed: re-reading a live order is
        still a re-import, not a second copy of it."""
        first = self.run_import().purchase

        report = self.run_import()

        self.assertTrue(report.already_imported)
        self.assertEqual(report.purchase.pk, first.pk)
        self.assertEqual(report.purchase.lines.count(), 2)
