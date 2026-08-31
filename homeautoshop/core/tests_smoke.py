"""
Every page renders.

This exists because of a bug it would have caught: a template shipped for weeks
with an unterminated string on line 7 — `{% translate "…to say. %}` — and no
test ever rendered that page, so nothing noticed. A `TemplateSyntaxError` is not
a subtle failure; it is a 500 for anybody who clicks the link. The only reason it
survived was that nothing walked the routes.

So this walks them. It asserts almost nothing about *content* — the module tests
do that — and only that every GET route reachable by a signed-in admin comes
back without raising. Cheap, and it fails on the whole class of mistake that
otherwise reaches the operator first.
"""

from __future__ import annotations

from django.test import TestCase, override_settings
from django.urls import get_resolver
from django.utils import timezone

from homeautoshop.accounts.models import User
from homeautoshop.assets.models import Asset, AssetSpec, Recall
from homeautoshop.core.models import NotificationChannel
from homeautoshop.diagnostics.models import DiagnosticSession, ParserProfile
from homeautoshop.diagnostics.profiles import seed as seed_profiles
from homeautoshop.inspections.models import Inspection
from homeautoshop.maintenance.models import AssetServiceItem, ServiceDefinition
from homeautoshop.parts.models import Part, PartFitment, StockLot
from homeautoshop.people.models import Person
from homeautoshop.purchasing.models import Purchase, Vendor
from django.core.files.base import ContentFile

from homeautoshop.mediafiles.models import Media
from homeautoshop.work.models import JobItem, WorkOrder

VIN = "1M8GDM9AXKP042788"

#: Routes this cannot call blind. Not exemptions from working — each is covered
#: by a module test that supplies what it needs.
SKIP = {
    "logout",            # POST only
    "asset_report",      # a PDF, exercised in the reports tests
    "export_csv",        # streams a file, and needs a `kind`
    "profile_export",    # a file download, exercised in the profile tests
    "service_worker",    # not a page; has its own test
    "healthz",
    "readyz",
}

#: Placeholders for captured arguments, filled from the fixtures below.
FILLERS: dict[str, str] = {}


@override_settings(
    STORAGES={
        "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    }
)
class EveryPageRendersTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        seed_profiles()
        cls.admin = User.objects.create_user(
            username="boss", password="x" * 16, role="admin"
        )
        cls.asset = Asset.objects.create(
            nickname="Red truck", vin=VIN, make="Ford", model="F-150", year=2013
        )
        cls.person = Person.objects.create(display_name="Andy")
        cls.wo = WorkOrder.objects.create(asset=cls.asset, title="Front brakes")
        cls.item = JobItem.objects.create(work_order=cls.wo, title="Pads")
        cls.part = Part.objects.create(name="Brake pads", part_number="D1234")
        cls.lot = StockLot.objects.create(part=cls.part, qty_on_hand=1)
        cls.fitment = PartFitment.objects.create(
            part=cls.part, make="Ford", model="F-150", year_from=2010, year_to=2014
        )
        cls.vendor = Vendor.objects.create(name="RockAuto")
        cls.purchase = Purchase.objects.create(vendor=cls.vendor)
        cls.spec = AssetSpec.objects.create(
            asset=cls.asset, group="tires", name="Front pressure", value="35"
        )
        cls.recall = Recall.objects.create(
            asset=cls.asset, campaign_number="24V001", component="Brakes"
        )
        cls.session = DiagnosticSession.objects.create(asset=cls.asset, tool="XTOOL")
        cls.profile = ParserProfile.objects.filter(tool_model="D8").first()
        cls.channel = NotificationChannel.objects.create(
            name="Andy", kind="email", target="andy@example.com"
        )
        definition = ServiceDefinition.objects.create(
            name="Oil and filter", translation_key="service.oil", category="engine"
        )
        cls.service_item = AssetServiceItem.objects.create(
            asset=cls.asset, definition=definition, interval_distance=5000
        )
        cls.inspection = Inspection.objects.create(
            asset=cls.asset, template_name="Winter prep"
        )
        # Photos are served by the application now rather than linked straight
        # to object storage, so the route that does it is a page like any other.
        cls.media = Media(kind=Media.Kind.PHOTO, original_filename="a.jpg", mime="image/jpeg")
        # One save, not two: Media is append-only, so attaching the file
        # afterwards is an edit and is refused.
        cls.media.file.save("a.jpg", ContentFile(b"not really a jpeg"), save=False)
        cls.media.save()

    def setUp(self):
        self.client.force_login(self.admin)

    def test_every_get_route_renders(self):
        resolver = get_resolver()
        checked = 0
        # `reverse_dict` is keyed by both name and view callable, so the string
        # keys are filtered before sorting — mixing the two is a TypeError.
        names = sorted(k for k in resolver.reverse_dict if isinstance(k, str))
        for name in names:
            if name in SKIP or name.startswith("api"):
                continue
            possibilities, _pattern, _defaults, _converters = resolver.reverse_dict[name]
            # Each possibility is `(url_template, [captured names])`.
            _template, captured = possibilities[0]

            url = self._url_for(name, captured)
            if url is None:
                continue

            with self.subTest(route=name):
                response = self.client.get(url)
                # 200 or a redirect are both fine — several of these are POST
                # endpoints that bounce a GET, which is correct behavior.
                self.assertIn(
                    response.status_code,
                    (200, 302, 405),
                    f"{name} ({url}) returned {response.status_code}",
                )
            checked += 1

        # A floor, so a URLconf that silently stops resolving does not turn this
        # into a test that passes by checking nothing.
        self.assertGreater(checked, 40)

    def _url_for(self, name: str, captured: list[str]) -> str | None:
        from django.urls import NoReverseMatch, reverse

        by_name = {
            "pk": str(self.asset.pk),
            "spec_id": str(self.spec.pk),
            "recall_id": str(self.recall.pk),
            "item_id": str(self.item.pk),
            "channel_id": str(self.channel.pk),
            "kind": "vehicles",
            "name": "wrenchledger",
            "variant": "thumb",
        }
        # Routes whose `pk` is not an asset.
        if name in {
            "work_order_detail", "work_order_edit", "work_order_transition", "note_create",
            "job_item_create", "job_item_toggle", "job_item_edit", "job_item_move",
            "job_item_delete", "work_order_photo", "work_order_part_use",
            "work_order_time_add", "work_order_expense_add", "job_item_tool_add",
            "job_item_tool_remove",
        }:
            by_name["pk"] = str(self.wo.pk)
        elif name in {
            "part_detail", "part_edit", "crossref_add", "lot_add", "lot_count",
            "lot_edit", "lot_delete", "lot_open_kit", "lot_close_kit", "part_use",
            "fitment_add", "fitment_edit", "fitment_delete",
        }:
            by_name["pk"] = str(self.part.pk)
            by_name["fitment_id"] = str(self.fitment.pk)
            by_name["lot_id"] = str(self.lot.pk)
        elif name in {"person_detail", "person_edit"}:
            by_name["pk"] = str(self.person.pk)
        elif name in {"user_detail", "user_set_active", "user_set_password"}:
            by_name["pk"] = str(self.admin.pk)
        elif name in {
            "purchase_detail", "purchase_line_add", "purchase_line_receive",
            "purchase_receipt_upload",
        }:
            by_name["pk"] = str(self.purchase.pk)
        elif name in {
            "session_detail", "session_confirm", "session_discard", "session_reparse",
            "session_map",
        }:
            by_name["pk"] = str(self.session.pk)
        elif name in {"media_file", "media_file_variant"}:
            by_name["pk"] = str(self.media.pk)
            by_name["variant"] = "thumb"
        elif name in {"profile_toggle"}:
            by_name["pk"] = str(self.profile.pk)
        elif name in {
            "inspection_detail", "inspection_complete", "inspection_convert",
            "inspection_add_check", "inspection_abandon", "inspection_resume",
            "inspection_delete",
        }:
            by_name["pk"] = str(self.inspection.pk)

        try:
            return reverse(name, kwargs={key: by_name[key] for key in captured})
        except (NoReverseMatch, KeyError):
            # A route needing an id this fixture set does not have. Its own
            # module test covers it; guessing an id here would only produce a
            # 404 that proves nothing.
            return None


class MobileLayoutTests(TestCase):
    """Guards on the two stylesheet rules whose removal breaks a phone.

    Not a layout test — nothing here renders anything. These are the two
    mistakes that were actually made, written down so that undoing either one
    fails loudly instead of shipping:

    * The account menu was anchored to a summary a few characters wide and
      opened off the left edge of the screen, with the labels unreadable.
    * `overflow-x: hidden` on the body is the obvious fix for sideways scroll
      and the wrong one: it makes the body a scroll container, and the sticky
      header then sticks to *that* rather than to the viewport.

    Comments are stripped before matching. The ad-hoc version of this check
    passed on the prose explaining why `hidden` was avoided, which is a good
    argument for stripping them.
    """

    @staticmethod
    def _stylesheet() -> str:
        import re
        from pathlib import Path

        from django.conf import settings

        css = (Path(settings.BASE_DIR) / "static" / "app.css").read_text(encoding="utf-8")
        return re.sub(r"/\*.*?\*/", "", css, flags=re.S)

    def test_the_account_menu_is_not_anchored_to_itself_on_a_phone(self):
        import re

        css = self._stylesheet()
        block = re.search(r"@media \(max-width: 799px\) \{(.*?)\n\}", css, re.S)
        self.assertIsNotNone(block, "the phone-width block is gone")
        mobile = block.group(1)
        self.assertIn("position: static", mobile)
        self.assertIn("inset-inline:", mobile)

    def test_the_body_does_not_become_a_scroll_container(self):
        """`overflow-x: hidden` here would silently unstick the header."""
        self.assertNotIn("overflow-x: hidden", self._stylesheet())

    def test_grid_and_flex_children_are_allowed_to_shrink(self):
        """Without this one wide table stretches its track and the page with it."""
        self.assertIn("min-width: 0", self._stylesheet())

    def test_the_viewport_meta_does_not_block_zoom(self):
        user = User.objects.create_user(username="andy", password="x" * 16)
        self.client.force_login(user)
        page = self.client.get("/").content.decode()
        self.assertIn("width=device-width", page)
        self.assertNotIn("user-scalable=no", page)
        self.assertNotIn("maximum-scale", page)


class TemplateCommentTests(TestCase):
    """`{# #}` is single-line only, and a multi-line one renders to the page.

    Django's comment tag does not span lines. `{# first\n second #}` is not a
    comment at all — it is text, and it appears on screen exactly as written.
    Four of them shipped, including a note about ad placement sitting in the
    middle of the integrations screen.

    `{% comment %}` is the tag that spans lines. This fails the build on the
    other one, because reading a template will not reliably catch it: the thing
    looks like a comment.
    """

    def test_no_template_carries_a_multi_line_hash_comment(self):
        import re
        from pathlib import Path

        from django.conf import settings

        offenders = []
        for path in sorted((Path(settings.BASE_DIR) / "templates").rglob("*.html")):
            text = path.read_text(encoding="utf-8")
            for match in re.finditer(r"\{#.*?#\}", text, re.S):
                if "\n" in match.group(0):
                    offenders.append(f"{path.name}:{text[: match.start()].count(chr(10)) + 1}")
        self.assertEqual(
            offenders, [], "use {% comment %} for anything spanning lines: " + ", ".join(offenders)
        )

    def test_django_really_does_render_a_multi_line_one(self):
        """The premise, asserted rather than assumed."""
        from django.template import Context, Template

        self.assertEqual(Template("A{# one line #}B").render(Context({})), "AB")
        self.assertIn("{#", Template("A{# one\ntwo #}B").render(Context({})))


class SectionNamingTests(TestCase):
    """A section is called one thing, in the menu and on the page it opens.

    Reported as the only route back to the purchase list being "the non-obvious
    route of going back through the buying link in the menu" — non-obvious
    because the menu said *Buying*, the page said *Purchases*, and nothing on
    screen connected the two words. Someone retracing their steps scans the menu
    for the name of the place they were, and it was not there.

    The rule is deliberately loose: the menu's word has to appear *somewhere* in
    the heading, not equal it. "Vehicles" opening a page headed "Vehicles &
    equipment" is findable; "Buying" opening one headed "Purchases" is not.

    Read out of the menu itself rather than listed here, so a tenth section is
    covered the day it is added.
    """

    def setUp(self):
        self.client.force_login(
            User.objects.create_user(username="boss", password="x" * 16, role="admin")
        )

    def sections(self) -> list[tuple[str, str]]:
        import re

        from django.template.loader import render_to_string

        nav = render_to_string("partials/_sections.html")
        return re.findall(r'<a href="([^"]+)">([^<]+)</a>', nav)

    def test_the_menu_is_not_empty(self):
        """Or the test below passes by checking nothing."""
        self.assertGreaterEqual(len(self.sections()), 9)

    def test_every_section_page_answers_to_its_menu_name(self):
        import re

        from django.utils.html import strip_tags

        wrong = []
        for href, label in self.sections():
            page = self.client.get(href).content.decode()
            found = re.search(r"<h1[^>]*>(.*?)</h1>", page, re.S)
            heading = strip_tags(found.group(1)).strip() if found else "(no heading)"
            if label.strip().lower() not in heading.lower():
                wrong.append(f"menu says {label.strip()!r}, page says {heading!r}")
        self.assertEqual(wrong, [], "; ".join(wrong))


class WayBackTests(TestCase):
    """Every page about one record offers a route to what it belongs to.

    The report was a purchase: opened from a work order, and then the only ways
    to the other purchases were the browser's Back button or the section menu,
    where the list is filed under a different word than the page uses. That is
    not a fault of one template — it is a fault of not having decided that a
    detail page owes the reader an exit.

    A source-level check rather than a rendered one, because rendering eight
    kinds of record needs eight fixtures and the thing being defended is that
    nobody adds a ninth detail page without one.
    """

    #: Pages about a single record. Each hangs off a list or a parent, and each
    #: has to say which.
    DETAIL_TEMPLATES = (
        "accounts/user.html",
        "assets/detail.html",
        "assets/recalls.html",
        "assets/specs.html",
        "core/asset_costs.html",
        "diagnostics/asset.html",
        "diagnostics/elm327.html",
        "diagnostics/session.html",
        "inspections/detail.html",
        "inspections/wear.html",
        "maintenance/schedule.html",
        "parts/detail.html",
        "people/detail.html",
        "purchasing/detail.html",
        "work/detail.html",
    )

    def test_every_detail_page_offers_a_way_back(self):
        from pathlib import Path

        from django.conf import settings

        root = Path(settings.BASE_DIR) / "templates"
        missing = [
            name
            for name in self.DETAIL_TEMPLATES
            if "partials/_back.html" not in (root / name).read_text(encoding="utf-8")
        ]
        self.assertEqual(missing, [], "no way back from: " + ", ".join(missing))

    def test_the_way_back_is_not_also_a_button(self):
        """One idiom, not two.

        Six of these carried a "Back to vehicle" button in the row beside Edit
        and Schedule — a way out dressed as one more thing to do, and on the
        vehicle page it was the eighth button of eight. The crumb replaced them,
        and this fails if one grows back.
        """
        from pathlib import Path

        from django.conf import settings

        root = Path(settings.BASE_DIR) / "templates"
        offenders = [
            path.name
            for path in sorted(root.rglob("*.html"))
            if 'class="btn"' in path.read_text(encoding="utf-8")
            and "Back to vehicle" in path.read_text(encoding="utf-8")
        ]
        self.assertEqual(offenders, [])
