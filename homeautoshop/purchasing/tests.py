"""
HTTP-level tests for the money path.

The model tests in `parts/tests.py` prove the arithmetic. These prove the
*wiring* — that every URL resolves to a view whose signature matches, and that
a real browser round-trip produces the numbers the model layer promises. A
signature mismatch between a URL kwarg and a view parameter is invisible to
model tests and produces a 500 at the worst moment.
"""

from __future__ import annotations

from django.test import TestCase
from django.urls import reverse

from homeautoshop.accounts.models import User
from homeautoshop.assets.models import Asset
from homeautoshop.parts.models import Location, Part, PartUsage, StockLot
from homeautoshop.work.models import TimeEntry, WorkOrder

from .models import Expense, Purchase, PurchaseLine, Vendor


class UrlWiringTests(TestCase):
    """Every route must resolve to a view that accepts its URL arguments.

    A mismatch between a URL keyword and a view parameter is invisible to model
    tests and shows up as a 500 the first time someone uses the feature. This
    walks the URLconf and compares each pattern's named groups against its
    view's signature.
    """

    def test_view_signatures_match_their_url_arguments(self):
        import inspect

        from django.urls import get_resolver
        from django.urls.resolvers import URLPattern, URLResolver

        problems: list[str] = []

        def walk(patterns, prefix=""):
            for entry in patterns:
                if isinstance(entry, URLResolver):
                    walk(entry.url_patterns, prefix + str(entry.pattern))
                    continue
                if not isinstance(entry, URLPattern):
                    continue
                view = entry.callback
                if not callable(view) or inspect.isclass(view):
                    continue
                try:
                    signature = inspect.signature(view)
                except (TypeError, ValueError):
                    continue
                accepts_any = any(
                    p.kind is inspect.Parameter.VAR_KEYWORD
                    for p in signature.parameters.values()
                )
                if accepts_any:
                    continue
                for group in entry.pattern.regex.groupindex:
                    if group not in signature.parameters:
                        problems.append(
                            f"{prefix}{entry.pattern} -> {view.__module__}.{view.__name__}() "
                            f"does not accept {group!r}"
                        )

        walk(get_resolver().url_patterns)
        self.assertEqual(problems, [], "; ".join(problems))


class MoneyFlowTests(TestCase):
    """Buy it, receive it, fit it, cost it — through HTTP, as a person would."""

    def setUp(self):
        self.user = User.objects.create_user("andy", password="correct-horse-battery")
        self.client.force_login(self.user)
        self.asset = Asset.objects.create(nickname="Red truck", meter_unit="mi")
        self.vendor = Vendor.objects.create(name="RockAuto", return_window_days=30)
        self.part = Part.objects.create(name="Brake pads", manufacturer="Akebono")
        self.shelf = Location.objects.create(name="Shelf B3")

    def _purchase_two_at_100_plus_10_shipping(self) -> PurchaseLine:
        purchase = Purchase.objects.create(vendor=self.vendor, shipping_minor=1000)
        return PurchaseLine.objects.create(
            purchase=purchase, part=self.part, qty_ordered=2, unit_price_minor=10000
        )

    def test_receiving_through_the_ui_creates_stock_at_landed_cost(self):
        line = self._purchase_two_at_100_plus_10_shipping()
        response = self.client.post(
            reverse("purchase_line_receive", args=[line.purchase.pk, line.pk]),
            {"qty": "2", "location": str(self.shelf.pk)},
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        lot = StockLot.objects.get()
        self.assertEqual(lot.qty_on_hand, 2)
        self.assertEqual(lot.unit_cost_minor, 10500)  # $100 + $10/2 shipping
        self.assertEqual(lot.location, self.shelf)

    def test_using_a_part_draws_stock_and_records_fitment(self):
        line = self._purchase_two_at_100_plus_10_shipping()
        line.receive()
        wo = WorkOrder.objects.create(asset=self.asset, title="Front brakes")

        self.client.post(
            reverse("work_order_part_use", args=[wo.pk]),
            {"part": str(self.part.pk), "qty": "1"},
        )
        self.assertEqual(self.part.on_hand, 1)
        usage = PartUsage.objects.get()
        self.assertEqual(usage.unit_cost_minor, 10500)
        self.assertTrue(self.part.fitments.filter(asset=self.asset, confidence="confirmed_installed").exists())

    def test_a_shortfall_is_recorded_rather_than_refused(self):
        wo = WorkOrder.objects.create(asset=self.asset, title="Front brakes")
        self.client.post(
            reverse("work_order_part_use", args=[wo.pk]),
            {"part": str(self.part.pk), "qty": "2"},
            follow=True,
        )
        # Nothing on the shelf, but the part is installed — the record says so.
        usage = PartUsage.objects.get()
        self.assertEqual(usage.source, PartUsage.Source.PURCHASED)
        self.assertEqual(usage.qty, 2)

    def test_expense_and_time_land_on_the_work_order(self):
        wo = WorkOrder.objects.create(asset=self.asset, title="Front brakes")
        self.client.post(
            reverse("work_order_expense_add", args=[wo.pk]),
            {"category": "machine_work", "amount_minor": "60.00",
             "incurred_on": "2026-08-29", "description": "Turn rotors"},
        )
        self.client.post(
            reverse("work_order_time_add", args=[wo.pk]),
            {"hours": "2.5", "category": "wrenching", "note": "Both sides"},
        )
        expense = Expense.objects.get()
        self.assertEqual(expense.amount_minor, 6000)
        self.assertEqual(expense.asset_id, self.asset.pk)  # inherited from the work order
        self.assertEqual(TimeEntry.objects.get().minutes, 150)

    def test_the_workbench_shows_the_running_cost(self):
        line = self._purchase_two_at_100_plus_10_shipping()
        line.receive()
        wo = WorkOrder.objects.create(asset=self.asset, title="Front brakes")
        self.client.post(reverse("work_order_part_use", args=[wo.pk]),
                         {"part": str(self.part.pk), "qty": "1"})
        self.client.post(reverse("work_order_expense_add", args=[wo.pk]),
                         {"category": "machine_work", "amount_minor": "60.00", "incurred_on": "2026-08-29"})

        response = self.client.get(reverse("work_order_detail", args=[wo.pk]))
        self.assertContains(response, "Cost so far")
        self.assertContains(response, "Akebono")

    def test_asset_cost_page_states_that_fuel_is_excluded(self):
        """FR-COST-3 — stated plainly rather than quietly omitted.

        The requirement is that the page says so, not that it says so in
        particular words: the copy used to argue the case ("by design: it is
        not a repair cost, and this is a repair system") and now just states
        the fact, which is what the reader needed either way.
        """
        response = self.client.get(reverse("asset_costs", args=[self.asset.pk]))
        self.assertContains(response, "Fuel is not included")

    def test_core_can_be_marked_returned_from_the_shelf(self):
        cored = Part.objects.create(name="Alternator", has_core=True, core_value_minor=4500)
        wo = WorkOrder.objects.create(asset=self.asset, title="Charging")
        usage = PartUsage.objects.create(work_order=wo, part=cored, qty=1)
        self.assertTrue(usage.owes_core)

        self.client.post(reverse("core_returned", args=[usage.pk]), {"next": reverse("inventory")})
        usage.refresh_from_db()
        self.assertTrue(usage.core_returned)
        self.assertIsNotNone(usage.core_returned_on)


class ReportViewTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("andy", password="correct-horse-battery")
        self.client.force_login(self.user)

    def test_key_pages_render(self):
        for name in ("part_list", "inventory", "purchase_list", "vendor_list", "reports"):
            with self.subTest(page=name):
                self.assertEqual(self.client.get(reverse(name)).status_code, 200)

    def test_every_report_exports_to_csv(self):
        """FR-REP-4 — no report is a dead end."""
        for kind in ("spend", "warranties", "assets"):
            with self.subTest(kind=kind):
                response = self.client.get(reverse("export_csv", args=[kind]))
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response["Content-Type"], "text/csv")
                self.assertIn("attachment", response["Content-Disposition"])
