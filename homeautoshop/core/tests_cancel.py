"""
A way out of every form that fills a page (SPEC §9.1, NFR-A-*).

Reported as "super annoying", and it is the right complaint. A full-page form
has replaced whatever the reader was looking at, and with only a Save on it the
exits are the browser's back button — which re-posts on some forms and warns
about it on others — or saving changes nobody wanted to keep. On a phone, run
as an installed PWA, there is no visible back button at all.

The test is written against the **rendered page** rather than against the
partial, because the partial being correct means nothing if a screen forgets to
include it. It is also written as a sweep rather than one assertion per form, so
the next full-page form to be added is covered before anybody remembers to
cover it.
"""

from __future__ import annotations

import re

from django.test import TestCase
from django.urls import reverse

from homeautoshop.accounts.models import Role, User
from homeautoshop.assets.models import Asset
from homeautoshop.parts.models import Part
from homeautoshop.people.models import Person
from homeautoshop.work.models import WorkOrder

VIN = "1M8GDM9AXKP042788"


class CancelTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="andy", password="x" * 16, role=Role.ADMIN)
        self.client.force_login(self.user)
        self.asset = Asset.objects.create(nickname="Red truck", vin=VIN)
        self.person = Person.objects.create(display_name="Andy")
        self.part = Part.objects.create(name="Brake pads")
        self.wo = WorkOrder.objects.create(asset=self.asset, title="Front brakes")

    def screens(self) -> dict[str, str]:
        """Every form that takes over the page, creating and editing."""
        return {
            "new vehicle": reverse("asset_create"),
            "edit vehicle": reverse("asset_edit", args=[self.asset.pk]),
            "new work order": reverse("work_order_create"),
            "edit work order": reverse("work_order_edit", args=[self.wo.pk]),
            "new part": reverse("part_create"),
            "edit part": reverse("part_edit", args=[self.part.pk]),
            "new person": reverse("person_create"),
            "edit person": reverse("person_edit", args=[self.person.pk]),
            "new purchase": reverse("purchase_create"),
            "settings": reverse("settings", args=["shop"]),
        }

    def test_every_full_page_form_offers_a_way_out(self):
        for name, url in self.screens().items():
            with self.subTest(screen=name):
                page = self.client.get(url).content.decode()
                self.assertIn("formactions", page, f"{name} has no Save/Cancel pair")
                self.assertRegex(
                    page,
                    r'class="btn"\s+href="[^"]+">\s*(Cancel|Discard changes)',
                    f"{name} has a Save with nothing beside it",
                )

    def test_the_way_out_goes_somewhere_that_exists(self):
        """A cancel pointing at an empty href is a button that does nothing."""
        for name, url in self.screens().items():
            with self.subTest(screen=name):
                page = self.client.get(url).content.decode()
                targets = re.findall(
                    r'class="btn" href="([^"]*)">\s*(?:Cancel|Discard changes)', page
                )
                self.assertTrue(targets, f"{name} has no cancel link")
                for target in targets:
                    self.assertTrue(target.startswith("/"), f"{name}: {target!r}")
                    self.assertLess(
                        self.client.get(target).status_code, 400, f"{name} → {target}"
                    )

    def test_creating_cancels_to_the_list_and_editing_to_the_record(self):
        """Back to where you came from, not to a fixed page.

        Editing had a cancel and creating did not, which is backwards: opening
        the wrong "new" form is the easier mistake to make.
        """
        new = self.client.get(reverse("asset_create")).content.decode()
        self.assertIn(f'href="{reverse("asset_list")}"', new)

        editing = self.client.get(reverse("asset_edit", args=[self.asset.pk])).content.decode()
        self.assertIn(f'href="{reverse("asset_detail", args=[self.asset.pk])}"', editing)

    def test_cancelling_writes_nothing(self):
        """It is a link, not a submit. Nothing to confirm and nothing to undo."""
        before = Asset.objects.count()
        self.client.get(reverse("asset_create"))
        self.client.get(reverse("asset_list"))
        self.assertEqual(Asset.objects.count(), before)

    def test_the_settings_screen_says_what_cancelling_means_there(self):
        """There is no record to go back to, so "Cancel" would be vague —
        it reloads the group and shows what is actually stored."""
        page = self.client.get(reverse("settings", args=["shop"])).content.decode()
        self.assertIn("Discard changes", page)
