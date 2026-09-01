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
  is browser behavior this suite cannot execute.
"""

from __future__ import annotations

from django.test import TestCase
from django.urls import reverse

from homeautoshop.accounts.models import Role, User
from homeautoshop.assets.models import Asset, Recall
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
        everybody else, which is the wrong half to fix.

        Swept across every template rather than a named pair. It was written
        against `work/detail.html` and `parts/detail.html` — the two pages the
        bug was reported on — and the recalls page then shipped a form with no
        anchor and no region, because a gate that only looks where the problem
        was already found cannot catch it anywhere else.
        """
        import re
        from pathlib import Path

        for path in sorted(Path("templates").rglob("*.html")):
            markup = path.read_text(encoding="utf-8")
            for region in re.finditer(r'data-live="([a-z-]+)"', markup):
                block = markup[region.start():markup.index("</section>", region.start())]
                with self.subTest(template=str(path), region=region.group(1)):
                    self.assertEqual(
                        block.count("{% csrf_token %}"),
                        block.count('name="_anchor"'),
                        "a form in this region would still jump to the top",
                    )

    #: Cards with a row-level form that have not been looked at for this yet.
    #:
    #: A card whose form sits inside a `{% for %}` is the shape of the bug —
    #: acting on one row of a list reloads the page and loses the row — but it
    #: is not proof of it. Restoring from the trash removes the row it was on;
    #: a scan import is a flow rather than a list. So these are recorded as
    #: unreviewed rather than as broken, and anything *not* on the list fails.
    #:
    #: That is the property that was missing. The sweep below used to name two
    #: templates, so the recalls page shipped with a form that jumped to the
    #: top and no test had an opinion about it. Fixing a page means deleting
    #: its line here, and a new page starts out failing.
    UNREVIEWED = {
        "assets/detail.html": 4,
        "assets/specs.html": 1,
        "assets/specs_from_scan.html": 1,
        "core/lubelogger.html": 2,
        "core/reminders.html": 1,
        "core/trash.html": 1,
        "diagnostics/asset.html": 1,
        "diagnostics/session.html": 1,
        "inspections/detail.html": 1,
        # Was 2. The scheduled-items card is a live region now — it grew a
        # third row action, and a page that already jumped to the top was not
        # the place to add one.
        "maintenance/schedule.html": 1,
        "parts/cores.html": 1,
        "parts/detail.html": 1,
        "purchasing/detail.html": 2,
        "work/tools.html": 1,
    }

    def test_a_card_acting_on_a_list_row_is_a_region_or_a_known_exception(self):
        """The half that actually missed the recalls page.

        Read off the templates rather than off a rendered page, so a screen
        nobody thought to render in a test is still covered — which is how this
        one got through. Text-only, so it cannot tell whether a form sits
        inside an `{% if %}`; that is the right way round, because a form that
        renders only sometimes still jumps to the top when it does.
        """
        import collections
        import re
        from pathlib import Path

        found = collections.Counter()
        for path in sorted(Path("templates").rglob("*.html")):
            markup = path.read_text(encoding="utf-8")
            for match in re.finditer(r'<section class="card[^"]*"([^>]*)>', markup):
                end = markup.find("</section>", match.start())
                block = markup[match.start():end if end != -1 else len(markup)]
                if 'method="post"' not in block or "data-no-live" in match.group(1):
                    continue
                if "data-live=" in match.group(1):
                    continue
                if "{% for" not in block or "<form" not in block:
                    continue
                if block.index("<form") < block.index("{% for"):
                    continue
                found[path.as_posix().replace("templates/", "")] += 1

        self.assertEqual(
            dict(found),
            self.UNREVIEWED,
            "a card acting on a list row either keeps the reader's place or is "
            "written down here as not yet looked at",
        )


class LiveRegionTests(Base):
    """The preconditions `liveform.js` depends on."""

    def test_the_regions_it_replaces_are_marked(self):
        page = self.client.get(self.page).content.decode()
        for region in (
            "job-items", "log", "time", "parts-used", "parts-needed", "photos",
        ):
            self.assertIn('data-live="%s"' % region, page)

    def test_every_card_that_takes_an_action_is_one(self):
        """Reported as: adding a part under Parts needed jumps to the top. It
        did, because that card was the one section on this page carrying forms
        and no region — and a page where some actions keep your place and others
        do not is harder to trust than one where none of them do."""
        import re

        page = self.client.get(self.page).content.decode()
        for match in re.finditer(r'<section class="card[^"]*"([^>]*)>', page):
            block = page[match.start():page.index("</section>", match.start())]
            if "<form" not in block or 'method="post"' not in block:
                continue
            if "data-no-live" in block:
                # An opt-out, and it has to be written down: a transition that
                # changes the heading, the banner and the cost cannot be a
                # region swap, and saying so beats looking like an oversight.
                continue
            with self.subTest(section=block[:80]):
                self.assertIn("data-live=", match.group(1) + block[:200])

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
            "a canceled confirmation must stop the post, not just the navigation",
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


class RecallPageTests(TestCase):
    """Reported as: the recalls page has the same jump-to-top bug.

    It did, and for the reason these bugs recur — the machinery was built and
    the page was never wired into it. Recording what a dealer said about the
    ninth campaign on a truck reloaded the page and put the reader at the top,
    which on a vehicle carrying a dozen open campaigns is the whole
    interaction, exactly as it was on the work order.

    Anchored per campaign rather than per card, because the form is inside the
    row: landing at the top of the list would leave somebody hunting for the
    one they just recorded, which is the same bug made shorter.
    """

    def setUp(self):
        self.user = User.objects.create_user(
            username="andy", password="x" * 16, role=Role.ADMIN
        )
        self.client.force_login(self.user)
        self.asset = Asset.objects.create(nickname="Aero", vin="1M8GDM9AXKP042788")
        self.recall = Recall.objects.create(
            asset=self.asset, campaign_number="21V-123", component="Brakes"
        )
        self.page = reverse("asset_recalls", args=[self.asset.pk])

    def test_recording_a_status_lands_back_on_the_campaign(self):
        response = self.client.post(
            reverse("recall_status", args=[self.asset.pk, self.recall.pk]),
            {"owner_status": Recall.OwnerStatus.COMPLETED, "_anchor": "recall-%s" % self.recall.pk},
            HTTP_REFERER="http://testserver" + self.page,
        )

        self.assertEqual(
            response.headers["Location"].split("#")[-1], "recall-%s" % self.recall.pk
        )

    def test_the_row_carries_the_id_the_anchor_names(self):
        """The fragment and the element have to agree or the browser scrolls
        nowhere, which looks exactly like the bug still being there."""
        page = self.client.get(self.page).content.decode()

        self.assertIn('id="recall-%s"' % self.recall.pk, page)
        self.assertIn('value="recall-%s"' % self.recall.pk, page)

    def test_the_campaigns_card_is_a_live_region(self):
        page = self.client.get(self.page).content.decode()

        self.assertIn('data-live="campaigns"', page)
        self.assertIn('id="campaigns"', page)

    def test_the_work_is_still_done_with_no_script(self):
        self.client.post(
            reverse("recall_status", args=[self.asset.pk, self.recall.pk]),
            {"owner_status": Recall.OwnerStatus.COMPLETED},
        )

        self.recall.refresh_from_db()
        self.assertEqual(self.recall.owner_status, Recall.OwnerStatus.COMPLETED)
