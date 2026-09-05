"""Pack sizes: what is in the container, versus how many containers.

The shop sprays gallons of brake cleaner and NAPA sells a drum of it. Until the
size was read, `CRC Brakleen ... 5 gal (US)` at `Qty: 1` arrived as one of
something costing $182.39 and the shelf could never say how much cleaner was
left — which is the only reason to stock a consumable at all.

The line this draws is between a **measure** and a **count**, and it is the same
line FR-PUR-12 drew: `5 gal` is read, `2Pcs` is not. A bare number in a product
title may be the pack, the pin count or the number of vehicles it fits, and
counting it doubles somebody's shelf silently. A number with a unit of measure
on it is a regulated statement about the container.
"""

from __future__ import annotations

from decimal import Decimal

from django.test import TestCase

from homeautoshop.purchasing.importers import amazon, napa, packs, service

from .tests_orders import AMAZON_INVOICE, NAPA_PAGE


class ReadingASizeTests(TestCase):
    def size(self, text):
        found = packs.read_size(text)
        return None if found is None else (found[0], found[1])

    def test_the_sizes_these_documents_actually_print(self):
        for text, want in (
            ("CRC Brakleen Brake Parts Cleaner Non-Flammable Chlorinated 5 gal (US)",
             (Decimal(5), "gal")),
            ("The Original PB Blaster Penetrant - 1 US Gal", (Decimal(1), "gal")),
            ("Lucas Oil Assembly Lube 8 Ounce", (Decimal(8), "floz")),
            ("Marvel Mystery Oil, 16 fl oz", (Decimal(16), "floz")),
            ("Mobil 1 5W-30 5 Quart Jug", (Decimal(5), "qt")),
            ("Brake Fluid DOT 4 500 ml", (Decimal(500), "ml")),
            ("Anti-Seize 1 lb", (Decimal(1), "lb")),
            ("Permatex Ultra Black 3.35 oz", (Decimal("3.35"), "floz")),
        ):
            with self.subTest(text=text):
                self.assertEqual(self.size(text), want)

    def test_a_size_needs_no_space_in_front_of_its_unit(self):
        """`10ml` is the same statement as `10 ml`, and an optional separator
        written as `rf"{sep}?"` around a pattern ending in `\\s*` puts the `?`
        on that trailing `\\s*` instead — which reads correctly and silently
        stops matching this."""
        self.assertEqual(self.size("Loctite 242 Threadlocker 10ml"), (Decimal(10), "ml"))

    def test_a_bare_count_is_not_a_size(self):
        for text in (
            "2Pcs 156700-2480 12VDC 30A 4Pins Automotive Relay",
            "Case of 4 Oil Filters",
            "Brake Pad Set 3-Pack",
            "CPS VG200 Digital Vacrometer Vacuum Gauge, Two Button Operation",
            "Tail Lamp Assembly",
        ):
            with self.subTest(text=text):
                self.assertIsNone(self.size(text))

    def test_the_trailing_size_wins(self):
        """A description runs general to specific, so the last one is the
        package and an earlier one is part of the product's name."""
        self.assertEqual(self.size("5W-30 Full Synthetic 1 Quart"), (Decimal(1), "qt"))

    def test_a_fluid_ounce_is_not_read_as_a_mass_ounce(self):
        """`fl oz` shares its tail with `oz`, so an alternation that reached
        `oz` first would read sixteen fluid ounces as sixteen pounds' worth."""
        self.assertEqual(self.size("16 fl oz"), (Decimal(16), "floz"))
        self.assertEqual(self.size("16 fl. oz."), (Decimal(16), "floz"))

    def test_nothing_and_nonsense_are_answered_rather_than_raised(self):
        for text in ("", "   ", "0 gal", "Part # CRC 05091"):
            with self.subTest(text=text):
                self.assertIsNone(self.size(text))


class WhatTheShopEndsUpHoldingTests(TestCase):
    """The end-to-end version: a document in, gallons on the shelf."""

    def napa(self, **kwargs):
        order = napa.parse_document("Your Order History Details | NAPA Auto Parts", NAPA_PAGE)
        return service.run(order, dry_run=False, **kwargs)

    def amazon(self, **kwargs):
        return service.run(amazon.parse_document(AMAZON_INVOICE), dry_run=False, **kwargs)

    def brakleen(self, report):
        return next(
            line for line in report.purchase.lines.all()
            if "Brakleen" in line.description_as_ordered
        )

    def test_a_five_gallon_pail_is_five_gallons(self):
        line = self.brakleen(self.napa())

        self.assertEqual(line.qty_ordered, Decimal(5))
        self.assertEqual(line.part.unit, "gal")

    def test_and_it_still_cost_what_the_document_says(self):
        """The count moves what the shop has and never what the order cost."""
        report = self.napa()

        self.assertEqual(self.brakleen(report).extended_minor, 18239)
        self.assertEqual(report.purchase.subtotal_minor, 22805)

    def test_so_the_price_is_a_price_per_gallon(self):
        outcome = next(o for o in self.napa().outcomes if "Brakleen" in o.line.description)

        self.assertEqual(outcome.unit_cost_shown, "36.4780")

    def test_receiving_it_puts_five_gallons_on_the_shelf(self):
        line = self.brakleen(self.napa())

        lot = line.receive(qty=line.qty_ordered)

        self.assertEqual(lot.qty_on_hand, Decimal(5))
        self.assertEqual(lot.part.unit, "gal")
        self.assertEqual(lot.part.on_hand, Decimal(5))

    def test_a_part_measured_in_anything_but_each_is_a_consumable(self):
        self.assertTrue(self.brakleen(self.napa()).part.is_consumable)

    def test_a_one_gallon_bottle_is_one_gallon(self):
        report = self.napa()
        line = next(
            line for line in report.purchase.lines.all() if "PB Blaster" in line.description_as_ordered
        )

        self.assertEqual(line.qty_ordered, Decimal(1))
        self.assertEqual(line.part.unit, "gal")

    def test_the_two_pack_of_relays_is_still_not_counted(self):
        """The rule this had to be built alongside rather than through."""
        report = self.amazon()
        line = next(line for line in report.purchase.lines.all() if "Relay" in line.description_as_ordered)

        self.assertEqual(line.qty_ordered, Decimal(1))
        self.assertEqual(line.part.unit, "each")

    def test_the_review_screen_says_what_it_read(self):
        outcome = next(o for o in self.napa().outcomes if "Brakleen" in o.line.description)

        self.assertEqual(outcome.size_read, "5 gal")

    def test_and_says_nothing_where_it_read_nothing(self):
        outcome = next(o for o in self.amazon().outcomes if "Relay" in o.line.description)

        self.assertEqual(outcome.size_read, "")

    def test_the_operator_can_overrule_the_unit(self):
        report = self.napa(units={0: "qt"})
        line = self.brakleen(report)

        self.assertEqual(line.part.unit, "qt")

    def test_and_the_count_independently_of_it(self):
        report = self.napa(counts={0: Decimal(20)})
        line = self.brakleen(report)

        self.assertEqual(line.qty_ordered, Decimal(20))
        self.assertEqual(line.part.unit, "gal")
        self.assertEqual(line.extended_minor, 18239)

    def test_a_part_the_shop_already_files_differently_keeps_its_own_unit(self):
        """A document is not entitled to overrule somebody's catalog. The count
        falls back to the vendor's, because multiplying gallons into a part
        measured in `each` would put five of the wrong thing on the shelf."""
        from homeautoshop.parts.models import Part

        Part.objects.create(
            name="CRC Brakleen", manufacturer="CRC", part_number="05091", unit="each"
        )

        line = self.brakleen(self.napa())

        self.assertEqual(line.part.unit, "each")
        self.assertEqual(line.qty_ordered, Decimal(1))


class TheSameThingTwiceInOneOrderTests(TestCase):
    """One order, two shipments, one product — and one part.

    A general retailer states no brand and no part number for anything, so the
    matcher had nothing to look up and every such line created a part
    unconditionally. An order that ships in two boxes lists the same item on a
    line for each, and the shop ended up with the product twice: two catalog
    rows, the stock split between them, and neither able to say what is on the
    shelf. Three gallons of washer fluid and three gallons of the same washer
    fluid, filed apart.
    """

    TITLE = "Rain-X -30F Extreme Temperature De-Icer Windshield Washer Fluid - 1 Gallon"

    def order(self, *lines):
        """An Amazon invoice with the given `(title, price)` lines on it."""
        rows = [
            (100.0, 42.0, "Final Details for Order #112-9135129-3761048", False),
            (115.0, 42.0, "Order Placed: September 1, 2026", False),
            (130.0, 42.0, "Order Total: USD 35.82", False),
            (145.0, 42.0, "Shipped on September 1, 2026", False),
            (160.0, 42.0, "Items Ordered Price", False),
        ]
        top = 175.0
        for title, price in lines:
            rows.append((top, 42.0, f"3 of: {title} ${price}", False))
            rows.append((top, 541.0, f"${price}", True))
            rows.append((top + 15.0, 42.0, "Condition: New", False))
            top += 30.0
        total = sum(float(price) for _title, price in lines)
        rows += [
            (top + 15.0, 451.0, f"Item(s) Subtotal: ${total:.2f}", True),
            (top + 30.0, 451.0, "Shipping & Handling: USD 0.00", True),
            (top + 45.0, 451.0, f"Total before tax: USD {total:.2f}", True),
            (top + 60.0, 451.0, "Estimated tax to be collected: USD 0.00", True),
            (top + 75.0, 451.0, f"Grand Total: USD {total:.2f}", True),
        ]
        return amazon.parse_document(rows)

    def run_import(self, *lines):
        return service.run(self.order(*lines), dry_run=False)

    def test_two_shipment_lines_become_one_part(self):
        from homeautoshop.parts.models import Part

        report = self.run_import((self.TITLE, "17.91"), (self.TITLE, "17.91"))

        self.assertEqual(Part.objects.filter(name=self.TITLE).count(), 1)
        self.assertEqual(report.purchase.lines.count(), 2)

    def test_and_the_shelf_adds_them_up_instead_of_splitting_them(self):
        report = self.run_import((self.TITLE, "17.91"), (self.TITLE, "17.91"))

        for line in report.purchase.lines.all():
            line.receive(qty=line.qty_ordered)

        part = report.purchase.lines.first().part
        self.assertEqual(part.on_hand, Decimal(6))
        self.assertEqual(part.unit, "gal")

    def test_the_second_line_is_reported_as_a_match_not_a_new_part(self):
        report = self.run_import((self.TITLE, "17.91"), (self.TITLE, "17.91"))

        self.assertEqual(report.parts_created, 1)
        self.assertEqual(report.parts_matched, 1)
        self.assertEqual(report.outcomes[1].matched_on, "on the description")

    def test_a_later_order_for_the_same_thing_reuses_it_too(self):
        from homeautoshop.parts.models import Part

        self.run_import((self.TITLE, "17.91"))
        self.run_import((self.TITLE, "17.91"))

        self.assertEqual(Part.objects.filter(name=self.TITLE).count(), 1)

    def test_a_different_product_is_still_its_own_part(self):
        from homeautoshop.parts.models import Part

        self.run_import(
            (self.TITLE, "17.91"),
            ("Prestone Coolant Concentrate - 1 Gallon", "24.99"),
        )

        self.assertEqual(Part.objects.count(), 2)

    def test_matching_is_exact_rather_than_close(self):
        """Nothing fuzzy on purpose: a near-match would eventually fold two
        sizes of the same product together, and stock merged into the wrong row
        is not undoable by looking at it."""
        from homeautoshop.parts.models import Part

        self.run_import(
            (self.TITLE, "17.91"),
            (self.TITLE.replace("1 Gallon", "1 Quart"), "6.99"),
        )

        self.assertEqual(Part.objects.count(), 2)

    def test_a_line_with_a_part_number_still_matches_on_that(self):
        """The parts suppliers are untouched: this only ever runs where the
        document states no number at all."""
        report = service.run(
            napa.parse_document("Your Order History Details | NAPA Auto Parts", NAPA_PAGE),
            dry_run=False,
        )

        self.assertEqual(report.parts_created, 2)
        self.assertTrue(all(o.matched_on == "" for o in report.outcomes))
