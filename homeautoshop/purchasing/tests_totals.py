"""What an order comes to, and in which order the arithmetic runs.

Reported against a real NAPA order: five gallons of brake cleaner and one of
penetrant, five dollars off, and the screen produced a total the receipt did
not agree with. The discount was being subtracted **after** tax rather than
before, so the tax was charged on five dollars the shop was never charged for.

That fault survived because of a coincidence. With tax stated as an amount,
`subtotal + tax - discount` and `subtotal - discount + tax` are the same
number — addition is commutative — so the totals column looked right for as
long as nobody could state a *rate*. Nobody could, which is the second half of
the same bug: the only way to record tax was to work the money out by hand and
type it, and a typed figure is right about one arrangement of lines and stops
being right, silently, the moment a line changes.
"""

from __future__ import annotations

from decimal import Decimal

from django.test import TestCase
from django.urls import reverse

from homeautoshop.accounts.models import Role, User
from homeautoshop.parts.models import Part
from homeautoshop.purchasing.models import Purchase, PurchaseLine, Vendor


class Base(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="andy", password="x" * 16, role=Role.ADMIN
        )
        self.client.force_login(self.user)
        self.vendor = Vendor.objects.create(name="NAPA")

    def order(self, **kwargs):
        return Purchase.objects.create(vendor=self.vendor, **kwargs)

    def line(self, purchase, name, qty, unit_minor):
        return PurchaseLine.objects.create(
            purchase=purchase,
            part=Part.objects.create(name=name),
            description_as_ordered=name,
            qty_ordered=Decimal(qty),
            extended_minor=int(Decimal(unit_minor) * Decimal(qty)),
        )


class TheReportedOrderTests(Base):
    """The order in the report, worked through end to end.

    Five gallons of Brakleen at $36.48 and one of PB Blaster at $45.66 is
    $228.06 of lines. Five dollars off leaves $223.06 to be taxed, and 8.4% of
    that is $18.74 — for $241.80. The screen said $238.38, because it taxed the
    $228.06 and took the discount off afterwards.
    """

    def setUp(self):
        super().setUp()
        self.purchase = self.order(
            order_number="33065705", discount_minor=500, tax_rate=Decimal("8.4")
        )
        self.line(self.purchase, "CRC Brakleen 05091", 5, 3648)
        self.line(self.purchase, "Blaster Penetrant 128-PB", 1, 4566)

    def test_the_lines_come_to_what_they_come_to(self):
        self.assertEqual(self.purchase.subtotal_minor, 22806)

    def test_the_discount_comes_off_before_the_tax_is_worked_out(self):
        self.assertEqual(self.purchase.taxable_minor, 22306)

    def test_the_tax_is_charged_on_the_discounted_figure(self):
        self.assertEqual(self.purchase.tax_charged_minor, 1874)

    def test_and_the_total_is_what_the_receipt_says(self):
        self.assertEqual(self.purchase.total_minor, 24180)

    def test_the_old_answer_is_gone(self):
        """$238.38 — the lines taxed whole, then five dollars off the end."""
        self.assertNotEqual(self.purchase.total_minor, 23838)


class TheOrderOfOperationsTests(Base):
    def test_a_discount_reduces_the_tax(self):
        """The property the commutative coincidence was hiding."""
        without = self.order(tax_rate=Decimal("10"))
        self.line(without, "Widget", 1, 10000)
        with_it = self.order(tax_rate=Decimal("10"), discount_minor=2000)
        self.line(with_it, "Widget", 1, 10000)

        self.assertEqual(without.tax_charged_minor, 1000)
        self.assertEqual(with_it.tax_charged_minor, 800)

    def test_shipping_is_not_taxed(self):
        """Whether a carrier's charge is taxable is a question about a
        jurisdiction, not about this order. Taxing it would be the application
        inventing an answer to it."""
        purchase = self.order(tax_rate=Decimal("10"), shipping_minor=5000)
        self.line(purchase, "Widget", 1, 10000)

        self.assertEqual(purchase.tax_charged_minor, 1000)
        self.assertEqual(purchase.total_minor, 16000)

    def test_a_discount_larger_than_the_order_does_not_tax_backwards(self):
        purchase = self.order(tax_rate=Decimal("10"), discount_minor=50000)
        self.line(purchase, "Widget", 1, 10000)

        self.assertEqual(purchase.taxable_minor, 0)
        self.assertEqual(purchase.tax_charged_minor, 0)

    def test_the_rate_rounds_to_the_cent_half_up(self):
        purchase = self.order(tax_rate=Decimal("8.4"))
        self.line(purchase, "Widget", 1, 22306)

        # 22306 x 0.084 = 1873.704
        self.assertEqual(purchase.tax_charged_minor, 1874)


class StatingItAsAnAmountStillWorksTests(Base):
    """An imported order confirmation carries a figure and never a rate.

    `rockauto.py` reads the tax off the PDF, so the amount column has to go on
    meaning what it says.
    """

    def test_with_no_rate_the_typed_amount_is_the_tax(self):
        purchase = self.order(tax_minor=1532)
        self.line(purchase, "Widget", 1, 22806)

        self.assertEqual(purchase.tax_charged_minor, 1532)

    def test_the_discount_still_comes_off_first(self):
        purchase = self.order(tax_minor=1532, discount_minor=500)
        self.line(purchase, "Widget", 1, 22806)

        self.assertEqual(purchase.taxable_minor, 22306)
        self.assertEqual(purchase.total_minor, 22306 + 1532)

    def test_a_rate_wins_over_an_amount_left_behind(self):
        """The more durable of the two statements. An amount is right about one
        arrangement of lines; a rate is still right after one changes."""
        purchase = self.order(tax_minor=9999, tax_rate=Decimal("10"))
        self.line(purchase, "Widget", 1, 10000)

        self.assertEqual(purchase.tax_charged_minor, 1000)


class ItStaysRightWhenALineChangesTests(Base):
    def test_a_rate_follows_the_lines(self):
        """The reason the tax is derived and never written back on save. What
        makes it stale is a *line* changing, and lines are edited from a
        different screen — so a figure recomputed by whoever remembers to call
        a helper is a figure that will eventually be wrong."""
        purchase = self.order(tax_rate=Decimal("10"))
        self.line(purchase, "Widget", 1, 10000)
        self.assertEqual(purchase.tax_charged_minor, 1000)

        self.line(purchase, "Second widget", 1, 5000)

        self.assertEqual(purchase.tax_charged_minor, 1500)
        self.assertEqual(purchase.total_minor, 16500)


class LandedCostUsesTheTaxActuallyChargedTests(Base):
    def test_a_lot_is_priced_from_the_rate_not_the_stale_amount(self):
        """`_overhead_per_unit` spreads tax and shipping over the lines. Reading
        the amount column with a rate set would price received stock from a
        figure that appears nowhere on the screen — and a lot's cost is what
        every job drawing from it will be charged."""
        purchase = self.order(tax_minor=9999, tax_rate=Decimal("10"))
        line = self.line(purchase, "Widget", 1, 10000)

        # $100 of widget carrying all $10 of tax: $10 on the one unit.
        self.assertEqual(line._overhead_per_unit(), Decimal(1000))

    def test_the_received_lot_carries_that_cost(self):
        purchase = self.order(tax_minor=9999, tax_rate=Decimal("10"))
        line = self.line(purchase, "Widget", 1, 10000)

        line.receive(user=self.user)

        self.assertEqual(line.lots.get().unit_cost_minor, 11000)


class TheScreenShowsTheWorkingTests(Base):
    def test_the_totals_panel_names_what_the_tax_was_charged_on(self):
        purchase = self.order(discount_minor=500, tax_rate=Decimal("8.4"))
        self.line(purchase, "Brakleen", 5, 3648)
        self.line(purchase, "Penetrant", 1, 4566)

        page = self.client.get(
            reverse("purchase_detail", args=[purchase.pk])
        ).content.decode()

        self.assertIn("Taxable", page)
        self.assertIn("$223.06", page)
        self.assertIn("Tax at 8.4%", page)
        self.assertIn("$18.74", page)
        self.assertIn("$241.80", page)

    def test_an_order_with_no_discount_does_not_print_an_empty_one(self):
        purchase = self.order(tax_rate=Decimal("8.4"))
        self.line(purchase, "Widget", 1, 10000)

        page = self.client.get(
            reverse("purchase_detail", args=[purchase.pk])
        ).content.decode()

        self.assertNotIn("Taxable", page)

    def test_a_rate_can_be_entered_on_the_form(self):
        self.client.post(
            reverse("purchase_create"),
            {"vendor": str(self.vendor.pk), "ordered_on": "2026-08-28",
             "status": "ordered", "tax_rate": "8.4", "discount_minor": "$5.00",
             "tax_minor": "$0.00", "shipping_minor": "$0.00"},
        )

        purchase = Purchase.objects.get()
        self.assertEqual(purchase.tax_rate, Decimal("8.400"))
        self.assertEqual(purchase.discount_minor, 500)


class TheLineHoldsWhatItCostTests(Base):
    """The penny, and where it was coming from.

    Five gallons of brake cleaner bought for $182.39. The form asked for a
    price *each*, so the figure had to be worked out — $36.478 — and the column
    it went into holds whole cents, so it became $36.48 and the line then
    claimed $182.40. A cent appeared that nobody paid, and the order stopped
    agreeing with the receipt.

    The mistake is a category one, not a rounding one. Money is an integer
    number of minor units because that is what survives arithmetic (§5.5), but
    **a unit price is not an amount anybody paid — it is a rate**, and rates do
    not divide evenly. So the line stores the extended price, exactly, and the
    per-unit figure is derived from it. It is the same distinction the tax on
    this order makes: a rate is a `Decimal`, an amount is cents.
    """

    def test_a_line_bought_as_a_total_keeps_that_total(self):
        purchase = self.order()
        line = PurchaseLine.objects.create(
            purchase=purchase,
            part=Part.objects.create(name="CRC Brakleen 05091"),
            qty_ordered=Decimal(5),
            extended_minor=18239,
        )

        self.assertEqual(line.line_total_minor, 18239)
        self.assertEqual(purchase.subtotal_minor, 18239)

    def test_and_the_price_each_is_the_one_that_does_not_divide(self):
        purchase = self.order()
        line = PurchaseLine.objects.create(
            purchase=purchase, qty_ordered=Decimal(5), extended_minor=18239
        )

        self.assertEqual(line.unit_price_exact, Decimal("3647.8"))
        self.assertEqual(line.unit_price_shown, "36.4780")

    def test_the_old_answer_is_gone(self):
        """$182.40 — five times a per-gallon price rounded to the cent."""
        purchase = self.order()
        PurchaseLine.objects.create(
            purchase=purchase, qty_ordered=Decimal(5), extended_minor=18239
        )

        self.assertNotEqual(purchase.subtotal_minor, 18240)

    def test_a_price_that_does_divide_reads_as_it_always_did(self):
        line = PurchaseLine.objects.create(
            purchase=self.order(), qty_ordered=Decimal(4), extended_minor=3596
        )

        self.assertEqual(line.unit_price_minor, 899)
        self.assertEqual(line.unit_price_shown, "$8.99")

    def test_a_core_charge_still_sits_on_top(self):
        line = PurchaseLine.objects.create(
            purchase=self.order(), qty_ordered=Decimal(2),
            extended_minor=10000, core_charge_minor=2500,
        )

        self.assertEqual(line.line_total_minor, 12500)

    def test_a_line_of_nothing_does_not_divide_by_zero(self):
        line = PurchaseLine.objects.create(
            purchase=self.order(), qty_ordered=Decimal(0), extended_minor=500
        )

        self.assertEqual(line.unit_price_exact, Decimal(500))


class EitherBoxOnTheFormTests(Base):
    """"It is a bad way of requesting the information when it likely isn't
    known" — the report, and the reason the add form takes both.

    A receipt states whichever figure the vendor felt like printing. NAPA
    printed the five-gallon total; a box of spark plugs prints the price each.
    Demanding the one that was not printed asks somebody to do a division the
    vendor never did, and then rounds their answer.
    """

    def add_line(self, **fields):
        purchase = self.order()
        payload = {"description": "Brakleen", "qty_ordered": "5",
                   "unit_price_minor": "", "extended_minor": "",
                   "core_charge_minor": ""}
        payload.update(fields)
        self.client.post(
            reverse("purchase_line_add", args=[purchase.pk]), payload
        )
        return purchase

    def test_a_total_is_taken_as_the_total(self):
        purchase = self.add_line(extended_minor="$182.39")

        self.assertEqual(purchase.lines.get().extended_minor, 18239)
        self.assertEqual(purchase.subtotal_minor, 18239)

    def test_a_price_each_is_still_accepted_and_multiplied_out(self):
        purchase = self.add_line(qty_ordered="4", unit_price_minor="$8.99")

        self.assertEqual(purchase.lines.get().extended_minor, 3596)

    def test_the_total_wins_when_both_are_given(self):
        """It is the figure that was actually charged; the other is arithmetic
        somebody did on the way to the box."""
        purchase = self.add_line(
            unit_price_minor="$36.48", extended_minor="$182.39"
        )

        self.assertEqual(purchase.lines.get().extended_minor, 18239)

    def test_the_form_offers_both(self):
        purchase = self.order()

        page = self.client.get(
            reverse("purchase_detail", args=[purchase.pk])
        ).content.decode()

        self.assertIn('name="unit_price_minor"', page)
        self.assertIn('name="extended_minor"', page)

    def test_the_row_prints_both_so_neither_reads_as_a_lie(self):
        purchase = self.add_line(extended_minor="$182.39")

        page = self.client.get(
            reverse("purchase_detail", args=[purchase.pk])
        ).content.decode()

        self.assertIn("$182.39", page)
        self.assertIn("36.4780", page)


class ReceivingKeepsTheFractionTests(Base):
    def test_a_lot_is_costed_from_the_unrounded_price(self):
        purchase = self.order()
        line = PurchaseLine.objects.create(
            purchase=purchase,
            part=Part.objects.create(name="Brakleen"),
            qty_ordered=Decimal(5),
            extended_minor=18239,
        )

        line.receive(user=self.user)

        # $36.478 a gallon, rounded once at the lot: $36.48.
        self.assertEqual(line.lots.get().unit_cost_minor, 3648)

    def test_the_overhead_share_is_rounded_and_not_truncated(self):
        """`int()` on a Decimal throws the fraction away, and the fraction is a
        share of tax and shipping — always positive — so every lot ever
        received landed a little cheaper than it was, in the same direction
        every time."""
        purchase = self.order(shipping_minor=1)
        line = PurchaseLine.objects.create(
            purchase=purchase,
            part=Part.objects.create(name="Widget"),
            qty_ordered=Decimal(1),
            extended_minor=10000,
        )

        line.receive(user=self.user)

        self.assertEqual(line.lots.get().unit_cost_minor, 10001)


class TheWholeReceiptTests(Base):
    """The reported order typed the way it was actually bought.

    Both faults were in this one screen and they compounded. The line demanded
    a price *each* for five gallons sold as a single $182.39, so the division
    was rounded up to $182.40 — and then the discount was taken off after tax
    rather than before, so the tax was charged on five dollars nobody paid.
    Entered as the receipt states it, and with the discount coming off first,
    the screen now lands on the figure printed at the bottom of the paper.
    """

    def test_it_comes_to_what_the_paper_says(self):
        purchase = self.order(
            order_number="33065705", discount_minor=500, tax_rate=Decimal("8.4")
        )
        PurchaseLine.objects.create(
            purchase=purchase,
            part=Part.objects.create(name="CRC Brakleen 05091"),
            qty_ordered=Decimal(5),
            extended_minor=18239,          # five gallons, as sold
        )
        PurchaseLine.objects.create(
            purchase=purchase,
            part=Part.objects.create(name="Blaster Penetrant 128-PB"),
            qty_ordered=Decimal(1),
            extended_minor=4566,
        )

        self.assertEqual(purchase.subtotal_minor, 22805)     # $228.05
        self.assertEqual(purchase.taxable_minor, 22305)      # $223.05
        self.assertEqual(purchase.tax_charged_minor, 1874)   # $18.74
        self.assertEqual(purchase.total_minor, 24179)        # $241.79

    def test_and_neither_old_answer_survives(self):
        purchase = self.order(discount_minor=500, tax_rate=Decimal("8.4"))
        PurchaseLine.objects.create(
            purchase=purchase, qty_ordered=Decimal(5), extended_minor=18239
        )
        PurchaseLine.objects.create(
            purchase=purchase, qty_ordered=Decimal(1), extended_minor=4566
        )

        # $228.06 was the rounded-up line; $238.38 was the discount taken off
        # after the tax. Both were wrong and only one of them was visible.
        self.assertNotEqual(purchase.subtotal_minor, 22806)
        self.assertNotEqual(purchase.total_minor, 23838)
