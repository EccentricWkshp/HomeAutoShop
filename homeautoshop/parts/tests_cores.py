"""Cores, and where they live (FR-PUR-4).

Reported as: *"I know I saw a button to mark the core returned but I can't find
it again... I finally found it — cores are listed under Shelf, which is not
obvious at all. I stumbled into it twice, that means it's in the wrong place.
Cores are a function of parts, not storage locations."*

Stumbling into something twice is a stronger signal than never finding it: the
screen was reachable, so where it was filed was the whole problem. A core is a
deposit on a part somebody fitted, and the bin it came out of has nothing to do
with whether the old one went back to the counter.

So: its own screen under Parts, showing the returned as well as the owed —
because the question is *what happened to this core*, and a list of debts alone
cannot answer it — and every "core owed" pill in the application links to it,
so the button is wherever the problem gets mentioned.
"""

from __future__ import annotations

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from homeautoshop.accounts.models import Role, User
from homeautoshop.assets.models import Asset
from homeautoshop.parts.models import Part, PartUsage
from homeautoshop.work.models import WorkOrder


class Base(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="andy", password="x" * 16, role=Role.ADMIN
        )
        self.client.force_login(self.user)
        self.asset = Asset.objects.create(nickname="Aero")
        self.part = Part.objects.create(
            name="ACDelco Caliper 18FR2451", has_core=True, core_value_minor=4500
        )
        self.url = reverse("core_list")

    def owed(self, **kwargs):
        return PartUsage.objects.create(
            part=self.part, qty=1, asset=self.asset, **kwargs
        )


class WhereItLivesTests(Base):
    def test_the_shelf_no_longer_carries_them(self):
        """A core is a fact about a part, not about a storage location."""
        self.owed()

        page = self.client.get(reverse("inventory")).content.decode()

        self.assertNotIn(reverse("core_update"), page)

    def test_the_parts_screen_is_the_way_in(self):
        page = self.client.get(reverse("part_list"))
        self.assertContains(page, reverse("core_list"))

    def test_and_it_says_how_many_are_owed(self):
        """An unreturned core is money already gone, and a link carrying the
        number gets pressed where a bare word does not."""
        self.owed()
        self.owed()

        page = self.client.get(reverse("part_list")).content.decode()

        self.assertIn(">2<", page.replace(" ", "").replace("\n", ""))

    def test_every_core_owed_pill_leads_here(self):
        """The button belongs wherever the problem is mentioned. It was
        mentioned in two other places and offered in neither."""
        wo = WorkOrder.objects.create(asset=self.asset, title="Brakes")
        PartUsage.objects.create(part=self.part, qty=1, work_order=wo)

        for url in (
            reverse("work_order_detail", args=[wo.pk]),
            reverse("part_detail", args=[self.part.pk]),
        ):
            with self.subTest(page=url):
                page = self.client.get(url).content.decode()
                self.assertIn('href="%s"' % reverse("core_list"), page)


class WhatItShowsTests(Base):
    def test_the_owed_ones(self):
        self.owed()
        self.assertContains(self.client.get(self.url), "ACDelco Caliper")

    def test_and_the_returned_ones(self):
        """A core marked returned by a slip has to be findable to be undone."""
        self.owed(core_returned=True, core_returned_on=timezone.localdate())

        page = self.client.get(self.url)

        self.assertContains(page, "ACDelco Caliper")
        self.assertContains(page, "Still owed")

    def test_it_totals_what_is_outstanding(self):
        """The point of the feature is money that walks out of the shop, so
        the figure belongs on the screen."""
        self.owed()
        self.owed()

        self.assertContains(self.client.get(self.url), "$90.00")

    def test_a_part_with_no_core_charge_recorded_is_not_counted_as_nothing(self):
        """A total that treats unknown as zero understates exactly the thing it
        exists to report."""
        vague = Part.objects.create(name="Starter", has_core=True)
        PartUsage.objects.create(part=vague, qty=1, asset=self.asset)
        self.owed()

        self.assertContains(self.client.get(self.url), "$45.00")

    def test_a_part_without_a_core_is_not_here_at_all(self):
        plain = Part.objects.create(name="Oil filter")
        PartUsage.objects.create(part=plain, qty=1, asset=self.asset)

        self.assertNotContains(self.client.get(self.url), "Oil filter")

    def test_a_core_owed_with_no_job_behind_it_still_lists(self):
        """FR-INV-10 — plenty of what a home garage fitted was never a job in
        here, and the deposit is owed just the same."""
        self.owed()

        page = self.client.get(self.url)

        self.assertContains(page, "ACDelco Caliper")
        self.assertContains(page, "Aero")


class MarkingThemTests(Base):
    def test_several_at_once(self):
        """Cores come back to the counter in an armful. Ticking the boxes and
        pressing one button is the difference between recording the trip and
        not bothering."""
        first, second = self.owed(), self.owed()

        self.client.post(
            reverse("core_update"), {"usage": [str(first.pk), str(second.pk)]}
        )

        first.refresh_from_db()
        second.refresh_from_db()
        self.assertTrue(first.core_returned)
        self.assertTrue(second.core_returned)

    def test_the_date_is_recorded(self):
        usage = self.owed()

        self.client.post(reverse("core_update"), {"usage": str(usage.pk)})

        usage.refresh_from_db()
        self.assertEqual(usage.core_returned_on, timezone.localdate())

    def test_one_marked_by_mistake_goes_back_to_owed(self):
        usage = self.owed(core_returned=True, core_returned_on=timezone.localdate())

        self.client.post(
            reverse("core_update"), {"usage": str(usage.pk), "state": "owed"}
        )

        usage.refresh_from_db()
        self.assertFalse(usage.core_returned)
        self.assertIsNone(usage.core_returned_on)

    def test_ticking_nothing_says_so_rather_than_claiming_success(self):
        response = self.client.post(reverse("core_update"), follow=True)
        self.assertContains(response, "Choose a core first")

    def test_a_part_with_no_core_cannot_be_marked_through_it(self):
        """The list is filtered, so an id typed in by hand is the only way to
        reach this — and it should not work."""
        plain = Part.objects.create(name="Oil filter")
        usage = PartUsage.objects.create(part=plain, qty=1, asset=self.asset)

        self.client.post(reverse("core_update"), {"usage": str(usage.pk)})

        usage.refresh_from_db()
        self.assertFalse(usage.core_returned)

    def test_it_takes_a_post(self):
        self.assertEqual(self.client.get(reverse("core_update")).status_code, 405)

    def test_it_needs_a_login(self):
        self.client.logout()
        usage = self.owed()

        self.client.post(reverse("core_update"), {"usage": str(usage.pk)})

        usage.refresh_from_db()
        self.assertFalse(usage.core_returned)
