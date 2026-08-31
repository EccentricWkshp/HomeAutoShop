"""
Form markup that has to be right in the template, not in a script.

Two failures reported from a phone, both of which look like styling and are
not:

* **"Add owner"** was the button's label whatever role was selected, so adding
  a primary driver announced that it was adding an owner.
* **`capture="environment"`** on every photo input. It does not mean "offer the
  camera" — it means *only* the camera, with no way to reach a picture taken
  five minutes earlier. On a phone that is the whole photo library, gone.

Both are asserted against the rendered page rather than against `forms.js`,
because the page has to be correct before any script runs.
"""

from __future__ import annotations

import re
from pathlib import Path

from django.conf import settings as django_settings
from django.test import TestCase
from django.urls import reverse

from homeautoshop.accounts.models import Role, User
from homeautoshop.assets.models import Asset

VIN = "1M8GDM9AXKP042788"


class OwnerRoleTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="andy", password="x" * 16, role=Role.ADMIN)
        self.client.force_login(self.user)
        self.asset = Asset.objects.create(nickname="Red truck", vin=VIN)

    def page(self) -> str:
        return self.client.get(reverse("asset_detail", args=[self.asset.pk])).content.decode()

    def test_the_button_is_labelled_from_the_role(self):
        page = self.page()
        self.assertIn("data-label-from", page)
        self.assertIn("data-label-template", page)

    def test_the_section_is_named_for_everyone_it_holds(self):
        """Co-owners and the primary driver live in this list too, and a
        heading naming one of the three makes the others look misfiled."""
        self.assertIn("Owners and drivers", self.page())

    def test_both_controls_are_labelled(self):
        """They were two bare selects with no labels at all."""
        page = self.page()
        self.assertIn("Their role", page)
        self.assertRegex(page, r'<label for="id_person">')

    def test_all_three_roles_are_offered(self):
        page = self.page()
        for role in ("Owner", "Co-owner", "Primary driver"):
            with self.subTest(role=role):
                self.assertIn(role, page)


class PhotoInputTests(TestCase):
    """`capture` is camera-only. Every upload on the site had it."""

    #: Templates with a photo input somebody uses from a phone.
    SCREENS = (
        "templates/assets/detail.html",
        "templates/work/detail.html",
        "templates/purchasing/detail.html",
        "templates/inspections/detail.html",
        "templates/diagnostics/asset.html",
    )

    def _markup(self, name: str) -> str:
        return (Path(django_settings.BASE_DIR) / name).read_text(encoding="utf-8")

    def test_every_screen_offers_a_way_to_pick_an_existing_photo(self):
        for name in self.SCREENS:
            with self.subTest(screen=name):
                markup = re.sub(r"\{%\s*comment\s*%\}.*?\{%\s*endcomment\s*%\}", "", self._markup(name), flags=re.S)
                inputs = re.findall(r"<input[^>]*type=\"file\"[^>]*>", markup, flags=re.S)
                self.assertTrue(inputs, f"{name} has no file input at all")
                self.assertTrue(
                    any("capture=" not in tag for tag in inputs),
                    f"every file input in {name} forces the camera",
                )

    def test_the_camera_is_still_one_tap_where_it_was(self):
        """The original intent was good (FR-DOC-2); it just cannot be the only
        option. A shop where photographing a part takes four taps is a shop
        where nobody photographs the part."""
        for name in ("templates/assets/detail.html", "templates/work/detail.html"):
            with self.subTest(screen=name):
                self.assertIn('capture="environment"', self._markup(name))

    def test_the_two_controls_say_which_is_which(self):
        user = User.objects.create_user(username="andy", password="x" * 16, role=Role.ADMIN)
        self.client.force_login(user)
        asset = Asset.objects.create(nickname="Red truck", vin=VIN)
        page = self.client.get(reverse("asset_detail", args=[asset.pk])).content.decode()
        self.assertIn("Take a photo", page)
        self.assertIn("Choose photos", page)


class ProgressiveEnhancementTests(TestCase):
    """Everything in forms.js is an enhancement over correct markup."""

    def test_the_script_is_loaded_on_every_page(self):
        from django.templatetags.static import static

        user = User.objects.create_user(username="andy", password="x" * 16)
        self.client.force_login(user)
        page = self.client.get(reverse("dashboard")).content.decode()
        self.assertIn(static("forms.js"), page)

    def test_it_contains_no_validation_of_its_own(self):
        """A rule that only exists in the browser is not a rule. Every check
        forms.js appears to make is repeated — and enforced — on the server."""
        source = (Path(django_settings.BASE_DIR) / "static" / "forms.js").read_text(encoding="utf-8")
        for forbidden in ("preventDefault()", "XMLHttpRequest"):
            if forbidden == "preventDefault()":
                # One use, and only to let somebody answer "no" to a confirm.
                self.assertLessEqual(source.count(forbidden), 1)
            else:
                self.assertNotIn(forbidden, source)
