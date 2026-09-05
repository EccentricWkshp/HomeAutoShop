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
from homeautoshop.parts.models import Part

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


class EveryFieldSaysWhatItIsTests(TestCase):
    """Reported as: the tooltip on most fields just says "Please fill out this
    field" instead of something useful.

    It did, and that is the browser's answer for a required control that
    carries no description of its own — a true sentence about the box that
    tells the reader nothing they could not already see. `core/described.py`
    puts a description on every field and `forms.js` hands it to the bubble.

    Held down here because the failure is silent in both directions: a field
    added without a description looks perfectly correct until somebody leaves
    it empty, and a description that is never rendered looks correct forever.
    """

    #: Forms that cannot be built with no arguments. A new one that needs
    #: something belongs here rather than being skipped — the point of the
    #: sweep is that it covers everything.
    def builders(self) -> dict:
        from homeautoshop.assets.models import Asset
        from homeautoshop.work.models import JobItem, TimeEntry, WorkOrder

        asset = Asset.objects.create(nickname="Red truck")
        order = WorkOrder.objects.create(asset=asset, title="Brakes")
        return {
            "JobItemEditForm": {
                "instance": JobItem.objects.create(work_order=order, title="Bleed")
            },
            "TimeEntryForm": {
                "instance": TimeEntry.objects.create(work_order=order, minutes=30)
            },
        }

    @staticmethod
    def form_classes() -> list:
        """Every form in the application, found rather than listed.

        A list would be a second place to remember, and the one form somebody
        forgets to add to it is exactly the one this is for.
        """
        import importlib
        import inspect
        import pkgutil

        from django import forms as djforms

        import homeautoshop

        found = []
        for info in pkgutil.walk_packages(
            homeautoshop.__path__, prefix="homeautoshop."
        ):
            name = info.name
            if ".migrations" in name or ".tests" in name or name.endswith("tests"):
                continue
            module = importlib.import_module(name)
            for attr, obj in vars(module).items():
                if not inspect.isclass(obj) or obj.__module__ != name:
                    continue
                if issubclass(obj, (djforms.Form, djforms.ModelForm)):
                    found.append((name, attr, obj))
        return found

    def test_the_sweep_actually_finds_the_forms(self):
        """A walk that quietly found nothing would pass every test below."""
        names = {attr for _module, attr, _cls in self.form_classes()}

        self.assertIn("AssetForm", names)
        self.assertIn("PartForm", names)
        self.assertGreater(len(names), 20)

    def test_every_field_of_every_form_describes_itself(self):
        from django import forms as djforms

        builders = self.builders()
        for module, attr, form_class in self.form_classes():
            with self.subTest(form=f"{module}.{attr}"):
                form = form_class(**builders.get(attr, {}))
                for name, field in form.fields.items():
                    if isinstance(field.widget, djforms.HiddenInput):
                        # Nothing to hover, and never the subject of a bubble.
                        continue
                    self.assertTrue(
                        field.widget.attrs.get("title"),
                        f"{attr}.{name} says nothing about what it is — add a "
                        f"line to its `descriptions`",
                    )

    def test_a_description_reaches_the_rendered_page(self):
        user = User.objects.create_user(username="andy", password="x" * 16, role=Role.ADMIN)
        self.client.force_login(user)
        part = Part.objects.create(name="Brake cleaner")

        page = self.client.get(reverse("part_detail", args=[part.pk])).content.decode()

        self.assertIn("How many you are putting on the shelf", page)


class EveryRequiredControlSaysWhatItWantsTests(TestCase):
    """The same rule for the controls written by hand in a template.

    A `required` attribute is the whole trigger for *"Please fill out this
    field"*, so the line this draws is that anything the browser can raise a
    bubble over has a sentence for it to say instead. An optional box never
    reaches that path.
    """

    CONTROL = re.compile(r"<(?:input|select|textarea)\b[^>]*>", re.S)

    def controls(self):
        root = Path(django_settings.BASE_DIR) / "templates"
        for path in sorted(root.rglob("*.html")):
            markup = path.read_text(encoding="utf-8")
            for tag in self.CONTROL.findall(markup):
                if re.search(r"\brequired\b", tag):
                    yield path.relative_to(root), tag

    def test_the_sweep_finds_them(self):
        self.assertGreater(len(list(self.controls())), 10)

    def test_each_one_carries_a_description(self):
        for where, tag in self.controls():
            with self.subTest(template=str(where), control=tag[:60]):
                self.assertIn("title=", tag)


class TheBubbleOnlyBorrowsOneMessageTests(TestCase):
    """`forms.js` replaces the missing-value message and no other.

    Every other message the browser produces — out of range, wrong format,
    too long — already names the actual problem. A general description
    swapped into those would take away the one useful sentence.
    """

    @staticmethod
    def source() -> str:
        return (Path(django_settings.BASE_DIR) / "static" / "forms.js").read_text(
            encoding="utf-8"
        )

    def test_only_a_missing_value_is_reworded(self):
        self.assertIn("valueMissing", self.source())

    def test_a_stale_message_cannot_hold_a_filled_field_invalid(self):
        """A custom message is itself a reason a field is invalid. Cleared
        before the state is read, and again as soon as anything is typed."""
        source = self.source()

        self.assertIn('setCustomValidity("")', source)
        self.assertIn('addEventListener("input", undescribe', source)
        self.assertIn('addEventListener("change", undescribe', source)
