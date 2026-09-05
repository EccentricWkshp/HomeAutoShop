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
