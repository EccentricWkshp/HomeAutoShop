"""`/api/v1/parts` — the catalog, carried through to the API (SPEC §10).

The parts screen learned to tell a part from a consumable and to file by
category, and the API had never heard of a part at all. `grep -riw part` across
`homeautoshop/api/` returned nothing: no schema, no route, and no op in the
sync batch. The only way a part reached a client was `/api/v1/search`, which
returns `str(result)` for every group — so a part arrived as
`Brakeco Brake pads BP-1` and nothing else, category and consumable included.

What matters here is less that the endpoint exists than that it answers the
*same* questions the same way. Both go through `matching()` and both read
`KINDS`, because two implementations of "what counts as a consumable" is
exactly how the screen and the API come to disagree while each looks right on
its own.
"""

from __future__ import annotations

from decimal import Decimal

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from homeautoshop.accounts.models import Role, User
from homeautoshop.parts.models import Location, Part, StockLot, StockTransaction
from homeautoshop.parts.services import categories_for


class Base(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="andy", password="x" * 16, role=Role.ADMIN
        )
        self.client.force_login(self.user)

    def part(self, name, *, categories=(), **kwargs):
        part = Part.objects.create(name=name, **kwargs)
        if categories:
            part.categories.set(categories_for(list(categories)))
        return part

    def get(self, path):
        response = self.client.get("/api/v1" + path)
        self.assertEqual(response.status_code, 200, response.content[:400])
        return response.json()

    def shelf(self):
        self.pump = self.part("Water pump", categories=["Cooling"])
        self.pads = self.part("Brake pads", categories=["Brakes"])
        self.cleaner = self.part(
            "Brake cleaner", categories=["Brakes"], is_consumable=True
        )
        self.rags = self.part("Shop rags", categories=["Shop"], is_consumable=True)


class TheCatalogIsReadableTests(Base):
    def test_a_part_comes_back_with_the_two_fields_that_started_this(self):
        self.shelf()

        rows = self.get("/parts")

        cleaner = next(r for r in rows if r["name"] == "Brake cleaner")
        self.assertTrue(cleaner["is_consumable"])
        self.assertEqual(cleaner["categories"], ["Brakes"])

    def test_one_part_by_id(self):
        self.shelf()

        row = self.get("/parts/%s" % self.pads.pk)

        self.assertEqual(row["name"], "Brake pads")
        self.assertFalse(row["is_consumable"])

    def test_a_part_that_does_not_exist_is_a_404(self):
        self.shelf()

        response = self.client.get(
            "/api/v1/parts/00000000-0000-7000-8000-000000000000"
        )

        self.assertEqual(response.status_code, 404)

    def test_it_needs_authentication(self):
        self.shelf()
        self.client.logout()

        self.assertEqual(self.client.get("/api/v1/parts").status_code, 401)


class TheSameFiltersAsTheScreenTests(Base):
    def test_kind_splits_parts_from_consumables(self):
        self.shelf()

        consumables = self.get("/parts?kind=consumable")
        parts = self.get("/parts?kind=part")

        self.assertEqual(
            sorted(r["name"] for r in consumables), ["Brake cleaner", "Shop rags"]
        )
        self.assertEqual(sorted(r["name"] for r in parts), ["Brake pads", "Water pump"])

    def test_no_kind_is_everything(self):
        self.shelf()

        self.assertEqual(len(self.get("/parts")), 4)

    def test_a_kind_nobody_recognizes_is_refused_here_and_not_on_the_screen(self):
        """The one deliberate difference, and it is worth being explicit about.

        On the screen an unrecognized `kind` shows the catalog: a URL gets
        typed by hand, and an empty parts page is indistinguishable from a shop
        that owns nothing. A client sending `kind=banana` has a bug instead,
        and answering it with the whole catalog hides the bug inside data that
        looks perfectly fine.
        """
        self.shelf()

        self.assertEqual(self.client.get("/api/v1/parts?kind=banana").status_code, 422)

        screen = self.client.get(reverse("part_list"), {"kind": "banana"})
        self.assertContains(screen, "Water pump")

    def test_category_narrows_and_ignores_casing(self):
        self.shelf()
        self.part("Thermostat", categories=["cooling"])

        rows = self.get("/parts?category=Cooling")

        self.assertEqual(
            sorted(r["name"] for r in rows), ["Thermostat", "Water pump"]
        )

    def test_a_search_narrows_by_every_identifier(self):
        self.shelf()
        part = self.part("Mystery box", manufacturer="Acme")
        part.cross_refs.create(system="upc", value="0123456789012")

        self.assertEqual(len(self.get("/parts?q=0123456789012")), 1)
        self.assertEqual(len(self.get("/parts?q=Acme")), 1)

    def test_the_three_compose(self):
        self.shelf()

        rows = self.get("/parts?q=Brake&category=Brakes&kind=consumable")

        self.assertEqual([r["name"] for r in rows], ["Brake cleaner"])

    def test_the_limit_is_capped_rather_than_trusted(self):
        for n in range(30):
            self.part("Filter %02d" % n)

        self.assertEqual(len(self.get("/parts?limit=5")), 5)
        self.assertEqual(len(self.get("/parts?limit=100000")), 30)


class TheCategoryListTests(Base):
    def test_it_offers_what_the_form_offers(self):
        self.shelf()

        self.assertEqual(self.get("/parts/categories"), ["Brakes", "Cooling", "Shop"])

    def test_it_collapses_spellings_the_same_way(self):
        """A client building a picker from this must not offer `Brakes` and
        `brakes` as two entries that select identical rows."""
        self.part("Pads", categories=["Brakes"])
        self.part("Shoes", categories=["Brakes"])
        self.part("Fluid", categories=["brakes"])

        self.assertEqual(self.get("/parts/categories"), ["Brakes"])

    def test_the_route_is_not_swallowed_by_the_id_route(self):
        """`categories` is not a UUID, so `/parts/{part_id}` must not claim
        it — a detail route that matches anything is how a sibling endpoint
        disappears with a 404 that looks like missing data."""
        self.assertEqual(self.client.get("/api/v1/parts/categories").status_code, 200)


class StockOnTheRowTests(Base):
    def stock(self, part, qty):
        where = Location.objects.create(name="Shelf A")
        lot = StockLot.objects.create(part=part, location=where, qty_on_hand=0)
        StockTransaction.record(lot, Decimal(qty), StockTransaction.Reason.RECEIVE)
        return lot

    def test_on_hand_is_the_ledger_projection(self):
        self.shelf()
        self.stock(self.pads, 4)

        row = self.get("/parts/%s" % self.pads.pk)

        self.assertEqual(row["on_hand"], 4.0)

    def test_is_low_is_answered_rather_than_left_to_the_client(self):
        self.shelf()
        self.pads.min_quantity = Decimal("6")
        self.pads.save()
        self.stock(self.pads, 4)

        row = self.get("/parts/%s" % self.pads.pk)

        self.assertTrue(row["is_low"])
        self.assertEqual(row["min_quantity"], 6.0)

    def test_a_part_with_no_minimum_is_never_low(self):
        self.shelf()

        row = self.get("/parts/%s" % self.pads.pk)

        self.assertIsNone(row["min_quantity"])
        self.assertFalse(row["is_low"])

    def test_a_search_does_not_multiply_the_quantity_by_the_cross_references(self):
        """The reason `shelf_quantities` exists instead of an `annotate`.

        `matching` joins cross-references when there is a search. Summing lots
        across that join multiplies every quantity by the number of
        cross-references the part carries, and `.distinct()` does not fix an
        aggregate — it just makes the wrong number look considered. Two refs
        and one lot of four would report eight.
        """
        part = self.part("Brake pads", manufacturer="Brakeco")
        part.cross_refs.create(system="upc", value="0123456789012")
        part.cross_refs.create(system="oem", value="BP-1")
        self.stock(part, 4)

        rows = self.get("/parts?q=Brakeco")

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["on_hand"], 4.0)

    def test_the_response_costs_the_same_whatever_its_length(self):
        """`Part.on_hand` and `Part.is_low` are properties that each query, so
        reading them per row would make a fifty-row response a hundred and one
        queries. Asserted as **scale-invariance** rather than against a fixed
        total: the absolute count includes the session, the settings and a
        savepoint, none of which this is about, and pinning it would make the
        test fail the next time somebody adds a setting.
        """
        for n in range(3):
            self.stock(self.part("Filter %02d" % n), 2)
        with CaptureQueriesContext(connection) as few:
            self.client.get("/api/v1/parts")

        for n in range(3, 40):
            self.stock(self.part("Filter %02d" % n), 2)
        with CaptureQueriesContext(connection) as many:
            self.client.get("/api/v1/parts")

        self.assertEqual(len(many), len(few), "a row is costing its own query")
