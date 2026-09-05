"""The part chooser, which used to be a list of everything (SPEC §7.2).

Reported as: *"the parts list is going to only grow since entries are never
removed, even when used. This means someone doing minimal work on a single
vehicle or any amount of work on more than one vehicle is going to have a
wasteland of previous parts to sift through on each part chooser."*

Every chooser was `Part.objects.all()[:500]` rendered into a `<select>`. That
is a control which gets steadily worse the more the application is used, and at
five hundred it stopped listing parts at all without saying so.

The fix is not a smaller catalog, and that matters more than it sounds.
**Planning is the act of finding the gap between what is on the shelf and what
has to be bought**, so a chooser offering only what is in stock would remove
the rows the planner opened it to find. Nothing here is hidden: typing searches
every part by every identifier. What changes is the resting state — a shortlist
assembled from relevance, with the whole catalog one search behind it.
"""

from __future__ import annotations

import json
from decimal import Decimal

from django.test import TestCase
from django.urls import reverse

from homeautoshop.accounts.models import Role, User
from homeautoshop.assets.models import Asset
from homeautoshop.parts.models import (
    Part, PartFitment, PartKitItem, StockLot, StockTransaction,
)
from homeautoshop.parts.services import SHORTLIST, candidates, resolve_part
from homeautoshop.purchasing.models import Purchase, Vendor
from homeautoshop.work.models import PartRequirement, WorkOrder


def stock(part, qty=1):
    lot = StockLot.objects.create(part=part, qty_on_hand=0, unit_cost_minor=100)
    StockTransaction.record(lot, qty, StockTransaction.Reason.RECEIVE)
    return lot


class Base(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="andy", password="x" * 16, role=Role.ADMIN
        )
        self.client.force_login(self.user)
        self.asset = Asset.objects.create(nickname="Aero", make="Suzuki", model="Aerio")


class ShortlistTests(Base):
    def setUp(self):
        super().setUp()
        self.fits_and_stocked = Part.objects.create(name="Aero oil filter")
        self.fits_but_missing = Part.objects.create(name="Aero fuel pump")
        self.stocked = Part.objects.create(name="Zip ties")
        self.consumable = Part.objects.create(name="Brake cleaner", is_consumable=True)
        self.stranger = Part.objects.create(name="F150 tailgate cable")

        for part in (self.fits_and_stocked, self.fits_but_missing):
            PartFitment.objects.create(
                part=part,
                asset=self.asset,
                confidence=PartFitment.Confidence.CONFIRMED,
            )
        stock(self.fits_and_stocked, 3)
        stock(self.stocked, 40)

    def names(self, *args, **kwargs):
        return [choice.part.name for choice in candidates(*args, **kwargs)]

    def test_a_part_that_fits_and_is_missing_outranks_one_on_the_shelf(self):
        """The whole point, and what a stock-only filter would get wrong.

        A part that fits this vehicle and is not in stock is the most useful
        row on the screen during planning: it *is* the gap. Ranking it below
        the zip ties — or worse, filtering it out for not being on hand —
        hides the answer somebody opened the chooser to find.
        """
        offered = self.names(asset=self.asset)

        self.assertLess(
            offered.index("Aero fuel pump"),
            offered.index("Zip ties"),
            "a part that has to be bought ranked below one that does not",
        )

    def test_on_hand_and_fitting_comes_first(self):
        self.assertEqual(self.names(asset=self.asset)[0], "Aero oil filter")

    def test_another_vehicles_parts_are_not_offered_unprompted(self):
        self.assertNotIn("F150 tailgate cable", self.names(asset=self.asset))

    def test_but_they_are_never_hidden(self):
        """Nothing leaves the catalog — only the default view."""
        self.assertIn("F150 tailgate cable", self.names("tailgate", asset=self.asset))

    def test_consumables_are_offered_because_they_fit_everything(self):
        self.assertIn("Brake cleaner", self.names(asset=self.asset))

    def test_the_shortlist_stays_short(self):
        for n in range(40):
            stock(Part.objects.create(name=f"Widget {n:02d}"), 1)

        self.assertEqual(len(candidates(asset=self.asset)), SHORTLIST)

    def test_with_no_vehicle_it_is_the_shelf_and_the_consumables(self):
        """A purchase order is not about one car, and still should not open
        onto every part ever bought."""
        offered = self.names()

        self.assertIn("Zip ties", offered)
        self.assertIn("Brake cleaner", offered)
        self.assertNotIn("F150 tailgate cable", offered)

    def test_a_part_known_not_to_fit_is_not_promoted_by_having_been_tried(self):
        """"We tried it and it did not fit" is a stronger fact than silence,
        and promoting it would offer the one part known to be wrong."""
        PartFitment.objects.create(
            part=self.stranger,
            asset=self.asset,
            confidence=PartFitment.Confidence.DOES_NOT_FIT,
        )
        self.assertNotIn("F150 tailgate cable", self.names(asset=self.asset))

    def test_a_kit_is_not_offered_as_a_part_of_itself(self):
        stock(self.stranger, 1)
        self.assertNotIn(
            "F150 tailgate cable", self.names(exclude=[self.stranger.pk])
        )

    def test_a_nonsense_id_in_exclude_is_ignored_rather_than_fatal(self):
        """It arrives from a URL, and a 500 is a poor answer to a typo."""
        self.assertTrue(candidates(exclude=["not-a-uuid", ""]))

    def test_what_is_on_hand_is_one_query_for_the_whole_list(self):
        """A chooser is a loop, and a property that queries makes it a page of
        queries — the reason the old list read the table once and then asked
        again per row."""
        for n in range(6):
            stock(Part.objects.create(name=f"Widget {n}"), 1)

        with self.assertNumQueries(5):
            candidates(asset=self.asset)


class SearchEndpointTests(Base):
    def setUp(self):
        super().setUp()
        self.pump = Part.objects.create(
            name="Fuel pump", part_number="E2237", manufacturer="Airtex"
        )
        PartFitment.objects.create(
            part=self.pump, asset=self.asset, confidence=PartFitment.Confidence.CONFIRMED
        )
        self.refrigerant = Part.objects.create(name="R-134a", unit="lb")
        stock(self.refrigerant, Decimal("30"))

    def rows(self, **params):
        response = self.client.get(reverse("part_search"), params)
        return json.loads(response.content)["results"]

    def test_a_part_number_finds_it(self):
        self.assertEqual(self.rows(q="E2237")[0]["name"], str(self.pump))

    def test_a_row_says_what_is_on_the_shelf(self):
        [row] = self.rows(q="134a")
        self.assertIn("30", row["detail"])
        self.assertIn("pounds", row["detail"])

    def test_a_row_with_nothing_on_the_shelf_says_so(self):
        """The planning half. "none on hand" beside a part that fits is the
        gap, stated where the choice is made rather than afterwards."""
        [row] = self.rows(q="E2237", asset=str(self.asset.pk))
        self.assertIn("none on hand", row["detail"])
        self.assertIn("fits this vehicle", row["detail"])

    def test_a_row_carries_its_own_step_and_units(self):
        """The quantity box beside the chooser cannot know in the markup
        whether it is about gaskets or kilograms. The chosen part says, exactly
        as the `<option>` attributes used to."""
        [row] = self.rows(q="134a")
        self.assertEqual(row["step"], "0.001")
        self.assertIn("kg", row["units"])

        [row] = self.rows(q="E2237")
        self.assertEqual(row["step"], "1")
        self.assertEqual(row["units"], ["each"])

    def test_nothing_typed_answers_with_the_shortlist(self):
        self.assertTrue(self.rows(asset=str(self.asset.pk)))

    def test_a_malformed_vehicle_id_is_not_a_crash(self):
        response = self.client.get(reverse("part_search"), {"asset": "x"})
        self.assertEqual(response.status_code, 200)

    def test_it_needs_a_login(self):
        self.client.logout()
        self.assertEqual(self.client.get(reverse("part_search")).status_code, 302)


class ResolveTests(Base):
    def setUp(self):
        super().setUp()
        self.pump = Part.objects.create(name="Fuel pump")

    def test_the_id_wins_when_the_script_filled_it_in(self):
        part, problem = resolve_part({"part": str(self.pump.pk), "part_query": "nonsense"})
        self.assertEqual(part, self.pump)
        self.assertEqual(problem, "")

    def test_a_typed_name_resolves_with_no_script_at_all(self):
        part, _problem = resolve_part({"part": "", "part_query": "fuel pump"})
        self.assertEqual(part, self.pump)

    def test_two_candidates_are_not_guessed_between(self):
        Part.objects.create(name="Fuel pump relay")
        part, problem = resolve_part({"part_query": "fuel pum"})
        self.assertIsNone(part)
        self.assertIn("More than one", problem)

    def test_an_exact_name_beats_a_partial_match(self):
        Part.objects.create(name="Fuel pump relay")
        part, _problem = resolve_part({"part_query": "Fuel pump"})
        self.assertEqual(part, self.pump)

    def test_a_name_matching_nothing_says_which_name(self):
        part, problem = resolve_part({"part_query": "flux capacitor"})
        self.assertIsNone(part)
        self.assertIn("flux capacitor", problem)


class ChooserOnThePagesTests(Base):
    """Every screen that chose a part, and none of them a catalog any more."""

    def setUp(self):
        super().setUp()
        self.pump = Part.objects.create(name="Fuel pump")
        stock(self.pump, 2)
        self.wo = WorkOrder.objects.create(asset=self.asset, title="Fuel")

    def assertPicker(self, page, field_id):
        self.assertIn('data-part-search="%s"' % reverse("part_search"), page)
        self.assertIn('id="%s"' % field_id, page)
        self.assertNotIn('<option value="%s"' % self.pump.pk, page)

    def test_the_work_order_asks_instead_of_listing(self):
        page = self.client.get(
            reverse("work_order_detail", args=[self.wo.pk])
        ).content.decode()
        self.assertPicker(page, "id_needed_part")
        self.assertPicker(page, "id_used_part")

    def test_the_work_order_chooser_knows_which_vehicle_it_is_on(self):
        """Without this the shortlist is only the shelf, and the ranking that
        makes it a shortlist rather than a sample has nothing to rank by."""
        page = self.client.get(
            reverse("work_order_detail", args=[self.wo.pk])
        ).content.decode()
        self.assertIn('data-asset="%s"' % self.asset.pk, page)

    def test_the_kit_page_asks_too_and_leaves_itself_out(self):
        kit = Part.objects.create(name="A/C kit")
        page = self.client.get(reverse("part_detail", args=[kit.pk])).content.decode()
        self.assertPicker(page, "id_kit_part")
        self.assertIn('data-exclude="%s"' % kit.pk, page)

    def test_an_order_line_asks_too(self):
        purchase = Purchase.objects.create(vendor=Vendor.objects.create(name="RockAuto"))
        page = self.client.get(
            reverse("purchase_detail", args=[purchase.pk])
        ).content.decode()
        self.assertPicker(page, "id_part")

    def test_the_work_order_offers_the_units_the_chosen_part_uses(self):
        """Reported alongside this: the work order asked for a unitless
        quantity while the part page had a picker beside the same box."""
        page = self.client.get(
            reverse("work_order_detail", args=[self.wo.pk])
        ).content.decode()
        self.assertIn('data-units-from="id_used_part_chosen"', page)
        self.assertIn('name="qty_unit"', page)


class UnenhancedTests(Base):
    """With the script blocked, the typed name is all the server gets."""

    def setUp(self):
        super().setUp()
        self.pump = Part.objects.create(name="Fuel pump")
        stock(self.pump, 2)
        self.wo = WorkOrder.objects.create(asset=self.asset, title="Fuel")

    def test_a_job_can_need_a_part_named_only_by_typing_it(self):
        self.client.post(
            reverse("work_order_part_require", args=[self.wo.pk]),
            {"part": "", "part_query": "Fuel pump", "qty": "1"},
        )
        self.assertEqual(PartRequirement.objects.get().part, self.pump)

    def test_a_part_can_be_used_the_same_way(self):
        self.client.post(
            reverse("work_order_part_use", args=[self.wo.pk]),
            {"part": "", "part_query": "Fuel pump", "qty": "1"},
        )
        self.assertEqual(self.pump.on_hand, Decimal(1))

    def test_a_name_matching_nothing_is_refused_and_says_which(self):
        response = self.client.post(
            reverse("work_order_part_require", args=[self.wo.pk]),
            {"part_query": "flux capacitor", "qty": "1"},
            follow=True,
        )
        self.assertEqual(PartRequirement.objects.count(), 0)
        self.assertContains(response, "flux capacitor")

    def test_a_kit_item_can_be_named_by_typing_it(self):
        kit = Part.objects.create(name="A/C kit")
        self.client.post(
            reverse("kit_item_add", args=[kit.pk]),
            {"part_query": "Fuel pump", "quantity": "1"},
        )
        self.assertEqual(PartKitItem.objects.get().part, self.pump)

    def test_an_order_line_with_no_part_is_still_a_line(self):
        """"Not cataloged" is a real answer on an order: the description
        carries what was bought, and refusing the line would lose the money."""
        purchase = Purchase.objects.create(vendor=Vendor.objects.create(name="RockAuto"))
        self.client.post(
            reverse("purchase_line_add", args=[purchase.pk]),
            {"part": "", "part_query": "", "description": "Shop rag bundle",
             "qty_ordered": "1", "unit_price_minor": "$12.40"},
        )

        line = purchase.lines.get()
        self.assertIsNone(line.part)
        self.assertEqual(line.description_as_ordered, "Shop rag bundle")


class AddWhatIsNotThereTests(Base):
    def test_a_search_that_found_nothing_starts_the_new_part_form(self):
        """The planning case: the part being named is one nobody has bought
        yet, so the search that failed becomes the first field of the form that
        fixes it rather than something to retype."""
        page = self.client.get(reverse("part_create"), {"name": "Fuel pump"})
        self.assertContains(page, 'value="Fuel pump"')

    def test_the_chooser_offers_that_route(self):
        wo = WorkOrder.objects.create(asset=self.asset, title="Fuel")
        page = self.client.get(
            reverse("work_order_detail", args=[wo.pk])
        ).content.decode()
        self.assertIn("data-new-part", page)
