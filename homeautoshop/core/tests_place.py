"""Keeping your place when a form updates the page (SPEC §9.2).

Reported as: page updates reload and jump to the top, which is jarring. Every
action here is post-redirect-get, which is right and stays — but on a long work
order it costs the reader their position on every tick of a checkbox, and on a
phone that is the whole interaction.

Two layers, and both are tested because both have to be true:

* **With no script**, the redirect carries a fragment naming the region the form
  was in, so the browser lands there instead of at the top.
* **With script**, `liveform.js` posts in the background and replaces only that
  region, so nothing navigates at all. It is tested here through its
  preconditions — that the regions it needs are marked, that the messages live
  region exists before anything is announced into it — because the swap itself
  is browser behaviour this suite cannot execute.
"""

from __future__ import annotations

from django.test import TestCase
from django.urls import reverse

from homeautoshop.accounts.models import Role, User
from homeautoshop.assets.models import Asset
from homeautoshop.parts.models import Part
from homeautoshop.work.models import JobItem, WorkOrder


class Base(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="andy", password="x" * 16, role=Role.ADMIN
        )
        self.client.force_login(self.user)
        self.asset = Asset.objects.create(nickname="Aero")
        self.wo = WorkOrder.objects.create(asset=self.asset, title="Brakes")
        self.item = JobItem.objects.create(work_order=self.wo, title="Pads")
        self.page = reverse("work_order_detail", args=[self.wo.pk])


class AnchorTests(Base):
    """The no-script half."""

    def post(self, url, data=None, **extra):
        return self.client.post(
            url, data or {}, HTTP_REFERER="http://testserver" + self.page, **extra
        )

    def test_a_redirect_lands_where_the_form_was(self):
        response = self.post(
            reverse("job_item_toggle", args=[self.wo.pk, self.item.pk]),
            {"_anchor": "job-items"},
        )
        self.assertEqual(response["Location"], self.page + "#job-items")

    def test_without_an_anchor_nothing_changes(self):
        response = self.post(reverse("job_item_toggle", args=[self.wo.pk, self.item.pk]))
        self.assertEqual(response["Location"], self.page)

    def test_a_redirect_to_another_page_is_left_alone(self):
        """Scrolling to a target that is not on the page it lands on is at best
        nothing and at worst a jump somewhere arbitrary."""
        response = self.post(
            reverse("work_order_delete", args=[self.wo.pk]), {"_anchor": "job-items"}
        )
        self.assertNotIn("#", response["Location"])

    def test_a_get_is_never_touched(self):
        response = self.client.get(self.page, {"_anchor": "job-items"})
        self.assertEqual(response.status_code, 200)

    def test_an_anchor_that_is_not_a_slug_is_ignored(self):
        """It ends up in a header. Ids in these templates are short slugs."""
        response = self.post(
            reverse("job_item_toggle", args=[self.wo.pk, self.item.pk]),
            {"_anchor": "x\r\nLocation: http://evil.example"},
        )
        self.assertEqual(response["Location"], self.page)

    def test_every_form_in_a_live_region_carries_one(self):
        """Otherwise the region is enhanced for script users and unchanged for
        everybody else, which is the wrong half to fix."""
        import re
        from pathlib import Path

        for name in ("work/detail.html", "parts/detail.html"):
            markup = (Path("templates") / name).read_text(encoding="utf-8")
            for region in re.finditer(r'data-live="([a-z-]+)"', markup):
                block = markup[region.start():markup.index("</section>", region.start())]
                with self.subTest(template=name, region=region.group(1)):
                    self.assertEqual(
                        block.count("{% csrf_token %}"),
                        block.count('name="_anchor"'),
                        "a form in this region would still jump to the top",
                    )


class LiveRegionTests(Base):
    """The preconditions `liveform.js` depends on."""

    def test_the_regions_it_replaces_are_marked(self):
        page = self.client.get(self.page).content.decode()
        for region in ("job-items", "log", "time", "parts-used"):
            self.assertIn('data-live="%s"' % region, page)

    def test_a_marked_region_has_an_id_to_scroll_to(self):
        """The same name does both jobs — the script finds the region by it and
        the browser scrolls to it — so they cannot disagree."""
        import re

        page = self.client.get(self.page).content.decode()
        for name in re.findall(r'data-live="([a-z-]+)"', page):
            self.assertIn('id="%s"' % name, page)

    def test_the_messages_list_is_there_before_there_are_any(self):
        """A live region has to exist before content is put into it, or the
        announcement is made into an element nothing was watching."""
        page = self.client.get(self.page).content.decode()
        self.assertIn('class="messages"', page)
        self.assertIn('aria-live="polite"', page)

    def test_the_script_is_loaded_and_deferred(self):
        page = self.client.get(self.page).content.decode()
        self.assertIn("liveform.js", page)
        self.assertLess(
            page.index("forms.js"),
            page.index("liveform.js"),
            "a cancelled confirmation must stop the post, not just the navigation",
        )

    def test_the_page_still_works_with_the_script_absent(self):
        """Nothing here is load-bearing: the same POST, unenhanced, still does
        the work and still redirects."""
        response = self.client.post(
            reverse("job_item_toggle", args=[self.wo.pk, self.item.pk]),
            {"_anchor": "job-items"},
            HTTP_REFERER="http://testserver" + self.page,
        )
        self.item.refresh_from_db()
        self.assertEqual(self.item.status, JobItem.Status.DONE)
        self.assertEqual(response.status_code, 302)


class PartPageRegionTests(Base):
    def test_the_part_page_regions_are_marked(self):
        part = Part.objects.create(name="Oil filter")
        page = self.client.get(reverse("part_detail", args=[part.pk])).content.decode()
        for region in ("stock-lots", "used-on", "other-numbers", "fits"):
            self.assertIn('data-live="%s"' % region, page)
