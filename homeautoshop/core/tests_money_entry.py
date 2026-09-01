"""
Typing money the way it is written (SPEC §5.5, §5.6).

Storage does not change here and these tests say so first: every amount is
still an integer count of minor units, because that is what survives
arithmetic. What changed is the boundary — the box on the screen now takes
`442.13`, the way the receipt writes it, instead of `44213`.

The failure this prevents is quiet rather than loud. Typing the amount from a
receipt into a field that wanted cents produced a purchase a hundred times too
small, with no error, no warning, and a plausible-looking number on the page.
"""

from __future__ import annotations

from decimal import Decimal

from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse

from homeautoshop.accounts.models import Role, User
from homeautoshop.core.measurements import Money
from homeautoshop.core.moneyform import MoneyFormField, parse_amount
from homeautoshop.parts.models import Part, StockLot
from homeautoshop.work.models import WorkOrder
from homeautoshop.purchasing.models import Expense, Purchase, PurchaseLine, Vendor
from homeautoshop.work.models import WorkOrder

from homeautoshop.assets.models import Asset

VIN = "1M8GDM9AXKP042788"


class ParsingTests(TestCase):
    def test_an_amount_off_a_receipt(self):
        self.assertEqual(parse_amount("442.13"), 44213)

    def test_the_way_a_receipt_actually_prints_it(self):
        self.assertEqual(parse_amount("$1,234.56"), 123456)
        self.assertEqual(parse_amount("  $ 9.06 "), 906)

    def test_a_whole_number_means_whole_units_not_cents(self):
        """The heart of it: 12 is twelve dollars, not twelve cents."""
        self.assertEqual(parse_amount("12"), 1200)

    def test_nothing_is_rounded_through_a_float(self):
        for written, minor in (("0.07", 7), ("1.10", 110), ("19.99", 1999), ("0.29", 29)):
            with self.subTest(written=written):
                self.assertEqual(parse_amount(written), minor)

    def test_a_currency_with_no_minor_unit(self):
        """JPY has no cents; 1200 yen is 1200, not 120000."""
        self.assertEqual(parse_amount("1200", "JPY"), 1200)

    def test_a_currency_with_three_places(self):
        self.assertEqual(parse_amount("1.234", "KWD"), 1234)

    def test_something_that_is_not_an_amount_is_refused_not_guessed(self):
        for written in ("", "   ", "abc", "-", "."):
            with self.subTest(written=written):
                with self.assertRaises(ValidationError):
                    parse_amount(written)


class FieldTests(TestCase):
    def test_what_is_stored_is_shown_as_an_amount(self):
        field = MoneyFormField()
        self.assertEqual(field.prepare_value(44213), Decimal("442.13"))

    def test_what_is_typed_is_stored_as_minor_units(self):
        self.assertEqual(MoneyFormField().to_python("442.13"), 44213)

    def test_a_rejected_entry_comes_back_exactly_as_it_was_typed(self):
        """Redisplaying it as anything else loses what the person meant."""
        self.assertEqual(MoneyFormField().prepare_value("4,4.2.13"), "4,4.2.13")

    def test_a_round_trip_changes_nothing(self):
        field = MoneyFormField()
        for minor in (0, 7, 906, 44213, 123456789):
            with self.subTest(minor=minor):
                self.assertEqual(field.to_python(str(field.prepare_value(minor))), minor)


class PurchaseScreenTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="andy", password="x" * 16, role=Role.ADMIN)
        self.client.force_login(self.user)
        self.vendor = Vendor.objects.create(name="RockAuto")
        self.purchase = Purchase.objects.create(vendor=self.vendor)
        self.part = Part.objects.create(name="Brake pads")

    def test_a_line_typed_in_dollars_is_stored_in_minor_units(self):
        self.client.post(
            reverse("purchase_line_add", args=[self.purchase.pk]),
            {
                "part": str(self.part.pk),
                "description": "Front pads",
                "qty_ordered": "1",
                "unit_price_minor": "358.79",
                "core_charge_minor": "0",
            },
        )
        line = PurchaseLine.objects.get()
        self.assertEqual(line.unit_price_minor, 35879)

    def test_a_line_priced_with_a_dollar_sign_is_still_understood(self):
        self.client.post(
            reverse("purchase_line_add", args=[self.purchase.pk]),
            {"description": "Filter", "qty_ordered": "1", "unit_price_minor": "$12.40"},
        )
        self.assertEqual(PurchaseLine.objects.get().unit_price_minor, 1240)

    def test_an_unreadable_price_is_refused_rather_than_stored_as_zero(self):
        response = self.client.post(
            reverse("purchase_line_add", args=[self.purchase.pk]),
            {"description": "Filter", "qty_ordered": "1", "unit_price_minor": "twelve"},
            follow=True,
        )
        self.assertEqual(PurchaseLine.objects.count(), 0)
        self.assertContains(response, "Enter an amount")

    def test_the_totals_form_takes_dollars(self):
        response = self.client.get(reverse("purchase_create"))
        self.assertNotContains(response, "minor units")

    def test_totals_typed_in_dollars_are_stored_in_minor_units(self):
        self.client.post(
            reverse("purchase_create"),
            {
                "vendor": str(self.vendor.pk),
                "status": self.purchase.status,
                "tax_minor": "39.85",
                "shipping_minor": "55.96",
                "discount_minor": "0",
            },
        )
        made = Purchase.objects.exclude(pk=self.purchase.pk).get()
        self.assertEqual(made.tax_minor, 3985)
        self.assertEqual(made.shipping_minor, 5596)

    def test_a_stored_total_is_shown_back_as_the_amount_that_was_typed(self):
        """Redisplay, tested on the form itself: there is no edit screen yet."""
        from homeautoshop.purchasing.views import PurchaseForm

        self.purchase.tax_minor = 3985
        self.purchase.save()
        rendered = str(PurchaseForm(instance=self.purchase)["tax_minor"])
        self.assertIn('value="39.85"', rendered)


class ExpenseScreenTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="andy", password="x" * 16, role=Role.ADMIN)
        self.client.force_login(self.user)
        self.asset = Asset.objects.create(nickname="Red truck", vin=VIN)
        self.wo = WorkOrder.objects.create(asset=self.asset, title="Front brakes")

    def test_sixty_dollars_of_machine_work_is_sixty_dollars(self):
        self.client.post(
            reverse("work_order_expense_add", args=[self.wo.pk]),
            {"category": "machine_work", "amount_minor": "60.00", "incurred_on": "2026-08-29"},
        )
        self.assertEqual(Expense.objects.get().amount_minor, 6000)


class LabourRateSettingTests(TestCase):
    """The setting that silently multiplies every cost report by a hundred."""

    def setUp(self):
        self.user = User.objects.create_user(username="andy", password="x" * 16, role=Role.ADMIN)
        self.client.force_login(self.user)

    def test_the_rate_is_typed_as_an_hourly_rate(self):
        self.client.post(reverse("settings", args=["costs"]), {"LABOR_RATE_MINOR": "25.00"})
        from homeautoshop.core.runtime import conf

        self.assertEqual(conf.LABOR_RATE_MINOR, 2500)

    def test_the_stored_rate_is_shown_back_as_a_rate(self):
        self.client.post(reverse("settings", args=["costs"]), {"LABOR_RATE_MINOR": "25.00"})
        page = self.client.get(reverse("settings", args=["costs"]))
        self.assertContains(page, 'value="25.00"')
        self.assertNotContains(page, "minor units")

    def test_an_amount_that_does_not_parse_is_reported_not_stored(self):
        response = self.client.post(
            reverse("settings", args=["costs"]), {"LABOR_RATE_MINOR": "lots"}, follow=True
        )
        self.assertContains(response, "Enter an amount")


class NoRawMinorUnitsOnScreenTests(TestCase):
    """A sweep, so the next screen to show an amount is covered in advance."""

    def setUp(self):
        self.user = User.objects.create_user(username="andy", password="x" * 16, role=Role.ADMIN)
        self.client.force_login(self.user)
        self.asset = Asset.objects.create(nickname="Red truck", vin=VIN)

    def test_no_screen_asks_the_reader_to_think_in_minor_units(self):
        """Case-insensitively, which is the whole reason this was reopened.

        This sweep existed, covered the part form, and passed for months while
        that form printed *"Minor units (e.g. cents). Never a float."* under
        the core charge. It matched `"minor units"` against a page that said
        `"Minor units"`, so a guard written against exactly this failure was
        blind to it over one capital letter.
        """
        vendor = Vendor.objects.create(name="RockAuto")
        purchase = Purchase.objects.create(vendor=vendor)
        part = Part.objects.create(name="Brake pads", has_core=True)
        work_order = WorkOrder.objects.create(asset=self.asset, title="Brakes")
        lot = StockLot.objects.create(part=part, qty_on_hand=0)
        screens = {
            "purchase": reverse("purchase_detail", args=[purchase.pk]),
            "purchase form": reverse("purchase_create"),
            "purchase edit": reverse("purchase_edit", args=[purchase.pk]),
            "part form": reverse("part_edit", args=[part.pk]),
            # Carries the add-stock form, whose unit cost had the same note.
            "part": reverse("part_detail", args=[part.pk]),
            "stock lot": reverse("lot_edit", args=[part.pk, lot.pk]),
            # Carries the other-costs form.
            "work order": reverse("work_order_detail", args=[work_order.pk]),
            "cost settings": reverse("settings", args=["costs"]),
            "outbound settings": reverse("settings", args=["outbound"]),
            "vehicle costs": reverse("asset_costs", args=[self.asset.pk]),
        }
        for name, url in screens.items():
            with self.subTest(screen=name):
                page = self.client.get(url).content.decode()
                self.assertNotIn("minor unit", page.lower())

    def test_the_note_still_reaches_the_places_that_do_take_minor_units(self):
        """It is not wrong, only misplaced. The column keeps it for the schema
        and for the Django admin, both of which really do deal in cents."""
        from homeautoshop.core.money import MINOR_UNITS_HELP
        from homeautoshop.parts.models import Part as PartModel

        field = PartModel._meta.get_field("core_value_minor")

        self.assertEqual(str(field.help_text), str(MINOR_UNITS_HELP))

    def test_a_column_with_something_useful_to_say_keeps_saying_it(self):
        """Dropped by identity, not by pattern: the note goes and a real
        explanation stays."""
        from homeautoshop.parts.views import PartForm

        self.assertIn(
            "divide a kit",
            str(PartForm().fields["typical_cost_minor"].help_text),
        )
        self.assertEqual(str(PartForm().fields["core_value_minor"].help_text), "")
