"""Telling parts from consumables, and making the category do something.

Two facts were stored and neither was usable.

`is_consumable` was on the form, on nothing else. It ranked a part picker
(`PartChoice.tier`) and appeared on no screen, so the only way to find out
whether the shop had marked brake cleaner as a consumable was to open its edit
form — and the parts list, which is where the question is actually asked, could
not tell a case of it from a water pump.

`category` was worse: asked for on every part, indexed twice, quietly matched
by the search box, and **rendered nowhere at all**. It filed nothing. Nobody
could see what a part was filed under, which means nobody could see that the
last one went in under a different spelling of it, which is how free text
becomes four categories that should be one.

So: a kind split, a category filter, and — the part that keeps the filter worth
having — the form offering what has already been typed and snapping to it when
somebody types it out anyway.
"""

from __future__ import annotations

from django.test import TestCase
from django.urls import reverse

from homeautoshop.accounts.models import Role, User
from homeautoshop.parts.models import Category, Part, PartKitItem
from homeautoshop.parts.services import categories, categories_for, category_for


class Base(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="andy", password="x" * 16, role=Role.ADMIN
        )
        self.client.force_login(self.user)
        self.url = reverse("part_list")

    def part(self, name, *, categories=(), **kwargs):
        part = Part.objects.create(name=name, **kwargs)
        if categories:
            part.categories.set(categories_for(list(categories)))
        return part

    def tab(self, label, n):
        """One kind tab as it actually renders, count included.

        Asserted whole rather than as a bare `(2)`, which would match any
        number anywhere on the page and pass for the wrong reason.
        """
        return '%s <span class="mono muted">(%d)</span>' % (label, n)

    def shelf(self):
        """A shelf with both kinds on it, which is the case that motivated all
        of this: one list in which brake cleaner and a water pump are peers."""
        self.pump = self.part("Water pump", categories=["Cooling"])
        self.hose = self.part("Radiator hose", categories=["Cooling"])
        self.pads = self.part("Brake pads", categories=["Brakes"])
        self.cleaner = self.part(
            "Brake cleaner", categories=["Brakes"], is_consumable=True
        )
        self.rags = self.part("Shop rags", categories=["Shop"], is_consumable=True)


class TheKindSplitTests(Base):
    def test_consumables_alone(self):
        self.shelf()

        page = self.client.get(self.url, {"kind": "consumable"})

        self.assertContains(page, "Brake cleaner")
        self.assertContains(page, "Shop rags")
        self.assertNotContains(page, "Water pump")

    def test_parts_alone(self):
        self.shelf()

        page = self.client.get(self.url, {"kind": "part"})

        self.assertContains(page, "Water pump")
        self.assertNotContains(page, "Brake cleaner")

    def test_everything_by_default(self):
        self.shelf()

        page = self.client.get(self.url)

        self.assertContains(page, "Water pump")
        self.assertContains(page, "Brake cleaner")

    def test_a_kind_nobody_recognizes_shows_the_catalog(self):
        """Not an empty screen. A hand-edited URL or a bookmark from a renamed
        value should fail open — an empty parts list is indistinguishable from
        a shop that owns nothing, which is a much worse answer than too much."""
        self.shelf()

        page = self.client.get(self.url, {"kind": "banana"})

        self.assertContains(page, "Water pump")
        self.assertContains(page, "Brake cleaner")

    def test_the_row_says_which_it_is(self):
        """The badge, not the word — `Consumables` is also the tab beside it,
        so a bare substring would pass with the badge deleted."""
        self.shelf()

        page = self.client.get(self.url).content.decode()

        self.assertIn(">Consumable<", page)

    def test_and_stops_saying_so_where_every_row_is_one(self):
        """A badge on everything is a badge nobody reads."""
        self.shelf()

        page = self.client.get(self.url, {"kind": "consumable"}).content.decode()

        self.assertNotIn(">Consumable<", page)


class TheCountsTests(Base):
    def test_each_side_says_how_many_it_holds(self):
        self.shelf()

        page = self.client.get(self.url).content.decode()

        self.assertIn(self.tab("All", 5), page)
        self.assertIn(self.tab("Parts", 3), page)
        self.assertIn(self.tab("Consumables", 2), page)

    def test_they_are_counted_under_the_filters_already_applied(self):
        """The reason they are worth rendering. "Consumables (0)" answers the
        question before the tab is opened; a tab that turns out to be empty
        only answers it afterwards."""
        self.shelf()

        page = self.client.get(self.url, {"category": "Cooling"}).content.decode()

        self.assertIn(self.tab("All", 2), page)
        self.assertIn(self.tab("Parts", 2), page)
        self.assertIn(self.tab("Consumables", 0), page)

    def test_the_tally_is_of_parts_not_of_rows(self):
        """The `.order_by()` this depends on is invisible and load-bearing:
        the default ordering is `name`, Django adds ordering columns to the
        GROUP BY, and the tally silently becomes one row per part — seven
        groups of one instead of one group of seven."""
        for n in range(7):
            self.part("Filter %d" % n, is_consumable=True)

        page = self.client.get(self.url).content.decode()

        self.assertIn(self.tab("Consumables", 7), page)
        self.assertIn(self.tab("Parts", 0), page)

    def test_a_part_matching_a_search_twice_is_counted_once(self):
        """`matching` joins cross-references, so a row can arrive more than
        once. Counted distinctly, or the tabs disagree with the list under
        them — which is worse than either number alone."""
        part = self.part("Brake pads", categories=["Brakes"], manufacturer="Brakeco")
        part.cross_refs.create(system="upc", value="Brake-1")

        page = self.client.get(self.url, {"q": "Brake"}).content.decode()

        self.assertIn(self.tab("All", 1), page)
        self.assertIn("One part.", page)


class TheCategoryFilterTests(Base):
    def test_it_narrows_to_the_category(self):
        self.shelf()

        page = self.client.get(self.url, {"category": "Cooling"})

        self.assertContains(page, "Water pump")
        self.assertNotContains(page, "Brake pads")

    def test_it_matches_whatever_the_casing_is(self):
        """The importer takes whatever the file says, so a shop can hold both
        spellings. A loose match makes a filter redundant; an exact one makes
        it hide rows, and only one of those is worth having."""
        self.part("Water pump", categories=["Cooling"])
        self.part("Thermostat", categories=["cooling"])

        page = self.client.get(self.url, {"category": "Cooling"})

        self.assertContains(page, "Water pump")
        self.assertContains(page, "Thermostat")

    def test_the_category_is_on_the_row(self):
        """It was rendered on no screen in the application. A filter on an
        invisible field is a filter nobody can form the intention to use."""
        self.shelf()

        page = self.client.get(self.url).content.decode()

        self.assertIn("Cooling", page)

    def test_and_the_row_is_the_way_to_the_rest_of_the_group(self):
        self.shelf()

        page = self.client.get(self.url).content.decode()

        self.assertIn("?category=Cooling", page)

    def test_the_picker_offers_what_has_been_typed(self):
        self.shelf()

        page = self.client.get(self.url).content.decode()

        self.assertIn('<option value="Brakes"', page)
        self.assertIn('<option value="Cooling"', page)

    def test_a_category_nobody_has_used_is_not_offered(self):
        self.part("Water pump", categories=["Cooling"])
        self.part("Unfiled thing")

        page = self.client.get(self.url).content.decode()

        self.assertIn('<option value="Cooling"', page)
        self.assertNotIn('<option value=""></option>', page)


class TheyComposeTests(Base):
    def test_a_search_inside_a_category_inside_a_kind(self):
        self.shelf()
        self.part("Brake cleaner refill", categories=["Brakes"], is_consumable=True)

        page = self.client.get(
            self.url, {"q": "Brake", "category": "Brakes", "kind": "consumable"}
        )

        self.assertContains(page, "Brake cleaner")
        self.assertNotContains(page, "Brake pads")

    def test_paging_keeps_every_filter(self):
        """The regression `_pager.html` said in prose that it did not have.

        Its comment claimed any query the page carries is preserved; the markup
        named `q` and nothing else, so a category filter would have been
        dropped on page 2 by a partial that documents itself as not doing that.
        """
        for n in range(140):
            self.part("Pad %03d" % n, categories=["Brakes"], is_consumable=True)

        page = self.client.get(
            self.url, {"category": "Brakes", "kind": "consumable"}
        ).content.decode()

        self.assertIn("category=Brakes", page)
        self.assertIn("kind=consumable", page)
        self.assertIn("page=2", page)

    def test_switching_kind_keeps_the_search_and_drops_the_page(self):
        """Page 4 of the catalog has no business becoming page 4 of a shorter
        list, and a search typed before the switch is still the question."""
        self.shelf()

        page = self.client.get(
            self.url, {"q": "Brake", "page": "1"}
        ).content.decode()

        self.assertIn("q=Brake&amp;kind=consumable", page)
        self.assertNotIn("kind=consumable&amp;page=", page)

    def test_a_filtered_list_stays_flat(self):
        """Kit contents fold under their kit while browsing (FR-INV-8) and
        must not while narrowed: folding a row that matched under a kit that
        matched for its own reasons hides the answer behind a heading."""
        kit = self.part("Brake kit", categories=["Brakes"])
        pad = self.part("Brake pad", categories=["Brakes"])
        PartKitItem.objects.create(kit=kit, part=pad, quantity=1)

        browsing = self.client.get(self.url).content.decode()
        filtered = self.client.get(
            self.url, {"category": "Brakes"}
        ).content.decode()

        self.assertIn("in Brake kit", filtered)
        self.assertNotIn("in Brake kit", browsing)

    def test_nothing_matched_is_not_no_parts_yet(self):
        """Two different facts, and the second one is alarming when false."""
        self.shelf()

        page = self.client.get(self.url, {"category": "Suspension"})

        self.assertContains(page, "Nothing matched.")
        self.assertNotContains(page, "No parts yet.")


class SeveralPerPartTests(Base):
    """The question that turned the field into a table.

    A headlight bulb is electrical and it is lighting. One field made somebody
    choose, and the part was then unfindable from the other direction — which
    is the exact failure the category existed to fix. It also invited the
    choice being dodged: with one box, `Electrical/Lighting` gets typed, and
    that is a third category matching neither filter.
    """

    def test_a_headlight_is_found_from_either_side(self):
        self.part("H11 bulb", categories=["Electrical", "Lighting"])
        self.part("Wiring harness", categories=["Electrical"])
        self.part("Lens", categories=["Lighting"])

        electrical = self.client.get(self.url, {"category": "Electrical"})
        lighting = self.client.get(self.url, {"category": "Lighting"})

        self.assertContains(electrical, "H11 bulb")
        self.assertContains(lighting, "H11 bulb")
        self.assertNotContains(lighting, "Wiring harness")

    def test_the_row_names_every_one_of_them(self):
        self.part("H11 bulb", categories=["Electrical", "Lighting"])

        page = self.client.get(self.url).content.decode()

        self.assertIn("?category=Electrical", page)
        self.assertIn("?category=Lighting", page)

    def test_a_compound_written_in_one_cell_reads_as_two(self):
        """What somebody writes when a form has room for one answer."""
        self.assertEqual(
            [c.name for c in categories_for("Electrical/Lighting")],
            ["Electrical", "Lighting"],
        )
        self.assertEqual(
            [c.name for c in categories_for("Brakes, Hydraulics")],
            ["Brakes", "Hydraulics"],
        )

    def test_the_same_category_twice_in_one_list_is_once(self):
        self.assertEqual(len(categories_for("Brakes, brakes , BRAKES")), 1)


class OneSpellingIsAConstraintTests(Base):
    """Convergence stopped being a helper somebody has to remember to call.

    That was the previous shape, and the CSV importer walked straight past it —
    writing whatever the spreadsheet said, in the one place a catalog arrives
    hundreds of rows at a time. A unique index cannot be walked past.
    """

    def test_a_second_spelling_lands_on_the_first_row(self):
        first = category_for("Brakes")

        self.assertEqual(category_for("brakes"), first)
        self.assertEqual(category_for("  BRAKES  "), first)
        self.assertEqual(Category.objects.count(), 1)

    def test_the_database_refuses_a_duplicate_outright(self):
        """Not merely "nothing creates one" — nothing *can*."""
        from django.db import IntegrityError, transaction

        Category.objects.create(name="Brakes")
        with self.assertRaises(IntegrityError), transaction.atomic():
            Category.objects.create(name="brakes")

    def test_whitespace_is_collapsed_before_it_counts(self):
        self.assertEqual(category_for("  Brake   parts ").name, "Brake parts")

    def test_an_empty_category_is_not_a_category(self):
        self.assertIsNone(category_for(""))
        self.assertIsNone(category_for("   "))
        self.assertIsNone(category_for(None))
        self.assertEqual(Category.objects.count(), 0)

    def test_a_genuinely_new_one_is_taken_as_typed(self):
        """Nothing is rejected. An unrecognized category is a new one, which is
        why the form has a box and not only a fixed list."""
        category_for("Brakes")

        self.assertEqual(category_for("Suspension").name, "Suspension")


class TheyAreOfferedTests(Base):
    def test_only_categories_with_something_in_them(self):
        """An empty category is a filter that finds nothing."""
        self.part("Pads", categories=["Brakes"])
        Category.objects.create(name="Abandoned")

        self.assertEqual(categories(), ["Brakes"])

    def test_an_emptied_one_is_kept_so_the_name_comes_back_to_it(self):
        pads = self.part("Pads", categories=["Brakes"])
        pads.categories.clear()

        self.assertEqual(categories(), [])
        self.assertEqual(Category.objects.get().name, "Brakes")
        # And typing it again lands on the same row rather than a second one.
        self.assertEqual(category_for("brakes").name, "Brakes")

    def test_in_reading_order(self):
        self.part("Pads", categories=["brakes"])
        self.part("Pump", categories=["Cooling"])
        self.part("Rags", categories=["ashtray"])

        self.assertEqual(categories(), ["ashtray", "brakes", "Cooling"])

    def test_the_form_offers_them_as_boxes_to_tick(self):
        self.part("Water pump", categories=["Cooling"])
        self.part("Pads", categories=["Brakes"])

        page = self.client.get(reverse("part_create")).content.decode()

        self.assertIn('type="checkbox"', page)
        self.assertIn("Cooling", page)
        self.assertIn("Brakes", page)

    def test_a_new_one_can_still_be_typed(self):
        self.part("Water pump", categories=["Cooling"])

        self.client.post(
            reverse("part_create"),
            {"name": "Strut", "new_categories": "Suspension, Steering"},
        )

        self.assertEqual(
            sorted(c.name for c in Part.objects.get(name="Strut").categories.all()),
            ["Steering", "Suspension"],
        )

    def test_ticked_and_typed_are_both_kept(self):
        """`save_m2m` sets the relation to exactly what the field cleaned, so
        anything added afterwards has to be applied after that or saving drops
        it."""
        cooling = category_for("Cooling")
        self.part("Water pump", categories=["Cooling"])

        self.client.post(
            reverse("part_create"),
            {"name": "Thermostat", "categories": [str(cooling.pk)],
             "new_categories": "Engine"},
        )

        self.assertEqual(
            sorted(c.name for c in Part.objects.get(name="Thermostat").categories.all()),
            ["Cooling", "Engine"],
        )

    def test_typing_one_that_exists_does_not_make_a_second(self):
        self.part("Water pump", categories=["Cooling"])

        self.client.post(
            reverse("part_create"), {"name": "Thermostat", "new_categories": "cooling"}
        )

        self.assertEqual(Category.objects.count(), 1)
        self.assertEqual(
            Part.objects.get(name="Thermostat").categories.get().name, "Cooling"
        )

    def test_an_edit_can_take_one_away(self):
        part = self.part("H11 bulb", categories=["Electrical", "Lighting"])
        keep = category_for("Lighting")

        self.client.post(
            reverse("part_edit", args=[part.pk]),
            {"name": "H11 bulb", "categories": [str(keep.pk)]},
        )

        self.assertEqual([c.name for c in part.categories.all()], ["Lighting"])
