"""A page must never predate the edit the reader just made (SPEC §5.4, §9.4).

Reported as: add a part, a purchase, anything — and the screen that comes back
does not have it on it. Reload and there it is. To the person standing there,
the application threw their typing away and then changed its mind.

The cause is one word in `sw.js`. Pages were **stale-while-revalidate**: answer
from the cache, refresh behind the reader. That is the right policy for content
nobody in the room is editing, and the wrong one here, because every write in
this application is post-redirect-get — so the page that lands *immediately
after adding something* is precisely the page SWR answers from a copy taken
before it existed. The refresh does arrive, and lands in the cache, which is
why the manual reload shows it.

Offline still works and that is not negotiable (P-7). What changed is which of
the two answers wins when both are available.

Tested as source. There is no JS runtime in the image — the bargain
`parts/tests_elm327.py` writes down — so this asserts about the policy the file
states rather than executing it, and about the server behavior it depends on.
"""

from __future__ import annotations

import re
from pathlib import Path

from django.conf import settings
from django.test import TestCase
from django.urls import reverse

from homeautoshop.accounts.models import Role, User
from homeautoshop.parts.models import Part
from homeautoshop.purchasing.models import Purchase, Vendor


def worker() -> str:
    return (Path(settings.BASE_DIR) / "static" / "sw.js").read_text(encoding="utf-8")


class ThePolicyTests(TestCase):
    def test_pages_are_not_answered_from_the_cache_first(self):
        """The whole bug in one assertion."""
        source = worker()

        self.assertNotIn("staleWhileRevalidate", source)
        self.assertIn("networkFirst", source)

    def test_the_page_handler_is_the_network_first_one(self):
        source = worker()

        self.assertIn("event.respondWith(networkFirst(request));", source)

    def test_a_page_still_comes_from_the_cache_when_there_is_no_network(self):
        """P-7. The priority between the two changed; the offline half did
        not, and losing it to fix a staleness bug would be a bad trade."""
        source = worker()
        body = source[source.index("function networkFirst"):]

        self.assertIn(".catch(", body)
        self.assertIn("caches.match(request)", body)
        self.assertIn('caches.match("/static/offline.html")', body)

    def test_static_assets_are_still_cache_first(self):
        """They carry the version in the cache name, so they are safe to serve
        from it — and they are the ones worth serving fast."""
        source = worker()

        self.assertIn("cacheFirst(request, isThumbnail(url) ? MEDIA : SHELL)", source)

    def test_a_write_drops_the_cached_pages(self):
        """The second half. Even network-first leaves the *cached* copy of the
        list stale after a write, which is what an offline reader would then be
        shown."""
        source = worker()

        self.assertIn("caches.delete(PAGES)", source)

    def test_but_only_when_the_write_actually_landed(self):
        """Offline, the fetch rejects, the write goes to the queue in
        `offline.js`, and those cached pages are the only pages there are."""
        source = worker()
        block = source[source.index('if (request.method !== "GET")'):source.index("var url = new URL")]

        self.assertIn("response.ok", block)
        self.assertIn(".catch(", block)


class TheServerDoesNotAskForItToBeCachedTests(TestCase):
    """A worker is not the only thing that can serve a stale page."""

    def setUp(self):
        self.user = User.objects.create_user(
            username="andy", password="x" * 16, role=Role.ADMIN
        )
        self.client.force_login(self.user)

    def test_a_signed_in_page_is_not_publicly_cacheable(self):
        """A shared cache holding one household's parts list is both a
        staleness bug and a privacy one."""
        response = self.client.get(reverse("part_list"))
        cache_control = response.headers.get("Cache-Control", "")

        self.assertNotIn("public", cache_control)


class TheRedirectAfterAWriteShowsItTests(TestCase):
    """The server half, which was always right and is worth pinning.

    If these ever fail, the staleness is not the worker's doing.
    """

    def setUp(self):
        self.user = User.objects.create_user(
            username="andy", password="x" * 16, role=Role.ADMIN
        )
        self.client.force_login(self.user)

    def test_a_new_part_is_on_the_page_the_form_redirects_to(self):
        response = self.client.post(
            reverse("part_create"), {"name": "Water pump"}, follow=True
        )

        self.assertContains(response, "Water pump")
        self.assertEqual(Part.objects.count(), 1)

    def test_a_new_purchase_is_on_the_page_the_form_redirects_to(self):
        vendor = Vendor.objects.create(name="NAPA")

        response = self.client.post(
            reverse("purchase_create"),
            {"vendor": str(vendor.pk), "ordered_on": "2026-08-28",
             "status": "ordered", "order_number": "33065705",
             "tax_minor": "$0.00", "shipping_minor": "$0.00",
             "discount_minor": "$0.00"},
            follow=True,
        )

        self.assertContains(response, "33065705")
        self.assertEqual(Purchase.objects.count(), 1)


class AFormSaysWhatItWillDoTests(TestCase):
    """Reported alongside the staleness, and the same kind of fault.

    Editing a purchase opened a page headed **New order** carrying a **Create**
    button, because the create view was written first and the edit view was
    pointed at the same template. There is no way to read that except as "this
    is about to make a second copy of the order I am looking at" — and somebody
    who believes that either abandons the edit, or saves and then goes looking
    for the duplicate.
    """

    def setUp(self):
        self.user = User.objects.create_user(
            username="andy", password="x" * 16, role=Role.ADMIN
        )
        self.client.force_login(self.user)
        self.vendor = Vendor.objects.create(name="NAPA")
        self.purchase = Purchase.objects.create(
            vendor=self.vendor, order_number="33065705"
        )

    def test_editing_an_order_does_not_offer_to_create_one(self):
        page = self.client.get(
            reverse("purchase_edit", args=[self.purchase.pk])
        ).content.decode()

        self.assertNotIn(">Create<", page)
        self.assertIn("Save changes", page)

    def test_and_does_not_call_itself_a_new_order(self):
        page = self.client.get(
            reverse("purchase_edit", args=[self.purchase.pk])
        ).content.decode()

        self.assertNotIn("New order", page)
        self.assertIn("33065705", page)

    def test_creating_one_still_says_create(self):
        page = self.client.get(reverse("purchase_create")).content.decode()

        self.assertIn(">Create<", page)
        self.assertIn("New order", page)

    #: A form template that serves *both* create and edit, recognized the way
    #: every one of them declares it: the Cancel target is chosen by a
    #: conditional on the object being edited. That is a structural fact rather
    #: than a guess, and it picks out exactly the six such forms.
    DUAL = re.compile(
        r"\{%\s*if\s+(\w+)\s*%\}\s*\{%\s*url[^%]*as cancel_to\s*%\}\s*\{%\s*else\s*%\}"
    )

    def test_every_form_that_also_edits_says_so_in_its_heading(self):
        """The half that shipped wrong. A page headed `New order` while editing
        one is a lie about what is on the screen, and it is the first thing the
        reader sees."""
        for path, subject, markup in self.dual_forms():
            with self.subTest(template=path):
                heading = markup[markup.index("<h1>"):markup.index("</h1>")]
                self.assertIn(
                    "{%% if %s %%}" % subject, heading,
                    "%s serves both create and edit and heads both the same" % path,
                )

    def test_and_in_the_label_on_its_button(self):
        """A create label on a page that also edits is the same lie, on the
        control that acts."""
        creating = ('label=_("Create")', 'label=_("Add")', 'label=_("New")')
        for path, _subject, markup in self.dual_forms():
            with self.subTest(template=path):
                includes = [
                    line for line in markup.splitlines()
                    if "_formactions.html" in line
                ]
                claims = [line for line in includes if any(c in line for c in creating)]
                if not claims:
                    # A plain `Save` is honest in both directions, which is what
                    # five of the six do.
                    continue
                self.assertGreater(
                    len(includes), 1,
                    "%s labels its only button Create on a page that also edits"
                    % path,
                )

    def dual_forms(self):
        found = []
        for path in sorted(Path("templates").rglob("*.html")):
            markup = path.read_text(encoding="utf-8")
            match = self.DUAL.search(markup)
            if match and "<h1>" in markup:
                found.append((path.as_posix(), match.group(1), markup))
        self.assertTrue(found, "the sweep found no dual-purpose forms to check")
        return found
