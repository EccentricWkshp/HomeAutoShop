"""The parts screen, which used to stop without saying so (FR-PART-1, FR-INV-8).

Two silent caps on one page. Browsing took the first two hundred rows of the
table; searching took `find`'s default twenty-five. Neither said anything, so a
shop with four hundred parts looked exactly like a shop with two hundred, and
the only way to find out otherwise was to go looking for something that should
have been there and not find it.

The choosers were fixed earlier by replacing the list with a search. This is
the same defect one screen over — and the reason to fix it here differently is
that a catalogue is a thing people browse, so the answer is page numbers and a
count rather than a search box.
"""

from __future__ import annotations

from django.test import TestCase
from django.urls import reverse

from homeautoshop.accounts.models import Role, User
from homeautoshop.parts.models import Part, PartKitItem
from homeautoshop.parts.services import find
from homeautoshop.parts.views import PAGE_SIZE


class Base(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="andy", password="x" * 16, role=Role.ADMIN
        )
        self.client.force_login(self.user)
        self.url = reverse("part_list")

    def stock(self, n, prefix="Widget"):
        Part.objects.bulk_create(
            [Part(name=f"{prefix} {i:04d}") for i in range(n)]
        )


class BrowsingTests(Base):
    def test_a_short_catalogue_fits_on_one_page(self):
        self.stock(5)

        page = self.client.get(self.url)

        self.assertNotContains(page, "Page 1 of")
        self.assertContains(page, "5 parts")

    def test_a_long_one_is_paged_rather_than_cut(self):
        self.stock(PAGE_SIZE + 20)

        page = self.client.get(self.url)

        self.assertContains(page, "Page 1 of 2")
        self.assertContains(page, "%d parts" % (PAGE_SIZE + 20))

    def test_the_second_page_holds_the_rest(self):
        self.stock(PAGE_SIZE + 20)

        page = self.client.get(self.url, {"page": 2})

        self.assertContains(page, "Widget %04d" % (PAGE_SIZE + 19))

    def test_the_count_is_of_everything_not_of_the_page(self):
        """The number that makes a truncation visible. Reporting the page's own
        length would have said "100 parts" about a catalogue of 320."""
        self.stock(320)

        page = self.client.get(self.url)

        self.assertContains(page, "320 parts")

    def test_a_page_past_the_end_lands_on_the_last_one(self):
        self.stock(5)
        self.assertEqual(self.client.get(self.url, {"page": 99}).status_code, 200)

    def test_nonsense_in_the_page_parameter_is_not_a_crash(self):
        self.stock(5)
        self.assertEqual(
            self.client.get(self.url, {"page": "banana"}).status_code, 200
        )


class SearchingTests(Base):
    def test_a_search_no_longer_stops_at_twenty_five(self):
        """`find`'s default is right for a chooser and wrong for this screen:
        a search matching forty parts reported fifteen of them as not
        existing."""
        self.stock(40, prefix="Filter")

        page = self.client.get(self.url, {"q": "Filter"})

        self.assertContains(page, "40 parts")

    def test_the_default_limit_still_applies_to_a_chooser(self):
        """Uncapping the screen must not uncap the picker, where twenty-five
        rows is already more than anybody scrolls."""
        self.stock(40, prefix="Filter")

        self.assertEqual(len(find("Filter")), 25)
        self.assertEqual(len(find("Filter", limit=None)), 40)

    def test_paging_through_a_search_keeps_the_search(self):
        """Otherwise the second page of a search is the second page of the
        whole catalogue, which is a different list wearing the same heading."""
        self.stock(PAGE_SIZE + 20, prefix="Filter")
        self.stock(10, prefix="Gasket")

        page = self.client.get(self.url, {"q": "Filter"}).content.decode()

        self.assertIn("q=Filter&amp;page=2", page)

    def test_and_the_second_page_of_it_is_still_the_search(self):
        self.stock(PAGE_SIZE + 20, prefix="Filter")
        self.stock(10, prefix="Gasket")

        page = self.client.get(self.url, {"q": "Filter", "page": 2})

        self.assertContains(page, "%d parts" % (PAGE_SIZE + 20))
        self.assertNotContains(page, "Gasket")


class KitNestingSurvivesTests(Base):
    def test_a_kit_still_carries_its_contents(self):
        """FR-INV-8, which paging must not quietly undo."""
        kit = Part.objects.create(name="A/C kit")
        drier = Part.objects.create(name="Receiver drier")
        PartKitItem.objects.create(kit=kit, part=drier, quantity=1)

        page = self.client.get(self.url).content.decode()

        self.assertIn("Receiver drier", page)
        # Nested under the kit rather than listed again at the top level.
        self.assertEqual(page.count("Receiver drier"), 1)

    def test_the_page_count_is_of_parts_not_of_visible_rows(self):
        """Contents fold into their kit, so a page shows fewer rows than it
        holds parts. The count says parts, which is what somebody asking "how
        many do I have" means."""
        kit = Part.objects.create(name="A/C kit")
        for n in range(3):
            PartKitItem.objects.create(
                kit=kit, part=Part.objects.create(name=f"Bit {n}"), quantity=1
            )

        page = self.client.get(self.url)

        self.assertContains(page, "4 parts")
