"""
The add/edit screen shows only the fields the chosen kind has (FR-EQP-1).

One table holds both a truck and a mower, which is right — they share ninety
percent of their behaviour. What was wrong is that the form showed both kinds'
fields to both kinds, so adding a lawnmower offered a VIN, a licence plate, a
registration expiry and a vehicle class. None of those exist on a mower, and
`Asset.clean()` refuses a VIN on equipment outright: the form was inviting an
entry the record would then reject.

Two properties matter more than the tidiness:

* **The server decides.** Sections are hidden before any script runs, so the
  screen is right with JavaScript off and cannot flash the wrong fields.
* **A field the screen is not showing never fails validation.** An error
  attached to a hidden box is one nobody can act on, so changing the kind
  *clears* the other kind's fields instead of arguing about them.
"""

from __future__ import annotations

from django.test import TestCase
from django.urls import reverse

from homeautoshop.accounts.models import User
from homeautoshop.assets.models import Asset, AssetKind
from homeautoshop.assets.views import FIELD_KINDS, SECTIONS, AssetForm

VIN = "1FTFW1ET5DFC10312"


def visible(form) -> list[str]:
    """The field names the screen actually puts in front of somebody."""
    return [
        row["field"].name
        for section in form.sections()
        for row in section["fields"]
        if not section["hidden"] and not row["hidden"]
    ]


class LayoutTests(TestCase):
    """Properties of the layout table itself, checked without rendering."""

    def test_every_field_on_the_form_has_a_place_in_a_section(self):
        """A field added to `Meta.fields` and forgotten here would vanish.

        Not fail — vanish. It would still post, still save and still never be
        seen, which is the kind of gap that is only found by somebody
        wondering why they cannot enter a colour any more.
        """
        placed = [name for _kind, _title, names in SECTIONS for name in names]
        self.assertEqual(sorted(placed), sorted(AssetForm.Meta.fields))

    def test_no_field_is_placed_twice(self):
        """Rendered twice, a field posts twice — and a QueryDict keeps the
        last value, so the copy nobody could see would be the one that won."""
        placed = [name for _kind, _title, names in SECTIONS for name in names]
        self.assertEqual(len(placed), len(set(placed)))

    def test_a_gated_field_is_never_inside_an_already_gated_section(self):
        """One attribute deciding one thing. A vehicle-only field inside the
        vehicle-only section would carry two gates that could disagree."""
        for kind, title, names in SECTIONS:
            if not kind:
                continue
            for name in names:
                with self.subTest(section=str(title), field=name):
                    self.assertEqual(FIELD_KINDS.get(name), kind)


class WhatEachKindIsOfferedTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="andy", password="x" * 16)
        self.client.force_login(self.user)

    def test_a_new_asset_starts_as_a_vehicle_and_is_offered_a_vin(self):
        form = self.client.get(reverse("asset_create")).context["form"]
        shown = visible(form)

        self.assertEqual(form.current_kind(), AssetKind.VEHICLE)
        for name in ("vin", "plate", "plate_region", "plate_expires_on", "vehicle_class"):
            self.assertIn(name, shown)

    def test_a_vehicle_is_not_offered_a_serial_number(self):
        """`manufacturer`, `model_number` and `serial_number` are the
        equipment spelling of make and model; on a car they are a second,
        emptier copy of boxes already on the screen."""
        form = self.client.get(reverse("asset_create")).context["form"]
        shown = visible(form)

        for name in ("manufacturer", "model_number", "serial_number"):
            self.assertNotIn(name, shown)

    def test_equipment_is_offered_no_vin_plate_or_registration(self):
        mower = Asset.objects.create(nickname="Mower", asset_kind=AssetKind.EQUIPMENT)
        form = self.client.get(reverse("asset_edit", args=[mower.pk])).context["form"]
        shown = visible(form)

        for name in ("vin", "plate", "plate_region", "plate_expires_on", "vehicle_class"):
            with self.subTest(field=name):
                self.assertNotIn(name, shown)

    def test_equipment_is_offered_the_things_it_does_have(self):
        """FR-EQP-1 names them: manufacturer, model and serial numbers, engine
        details and purchase information."""
        mower = Asset.objects.create(nickname="Mower", asset_kind=AssetKind.EQUIPMENT)
        form = self.client.get(reverse("asset_edit", args=[mower.pk])).context["form"]
        shown = visible(form)

        for name in ("manufacturer", "model_number", "serial_number", "engine",
                     "fuel_type", "year", "meter", "meter_unit", "acquired_on"):
            with self.subTest(field=name):
                self.assertIn(name, shown)

    def test_both_kinds_keep_the_fields_neither_kind_is_without(self):
        for kind in (AssetKind.VEHICLE, AssetKind.EQUIPMENT):
            form = AssetForm(initial={"asset_kind": kind})
            with self.subTest(kind=kind):
                for name in ("nickname", "status", "notes", "meter"):
                    self.assertIn(name, visible(form))

    def test_arriving_from_the_equipment_tab_opens_the_equipment_form(self):
        """The list screen already knows which kind you were looking at.
        Dropping it means the first thing on a fresh form is changing it back.
        """
        form = self.client.get(
            reverse("asset_create"), {"kind": "equipment"}
        ).context["form"]

        self.assertEqual(form.current_kind(), AssetKind.EQUIPMENT)
        self.assertIn("serial_number", visible(form))
        self.assertNotIn("vin", visible(form))
        # FR-EQP-2: and it opens on the meter equipment actually has.
        self.assertEqual(form["meter"].value(), "engine_hours")
        self.assertEqual(form["meter_unit"].value(), "hours")

    def test_an_unknown_kind_in_the_url_is_ignored(self):
        form = self.client.get(reverse("asset_create"), {"kind": "boat"}).context["form"]
        self.assertEqual(form.current_kind(), AssetKind.VEHICLE)

    def test_the_list_carries_the_tab_you_were_on_into_the_add_button(self):
        page = self.client.get(reverse("asset_list"), {"kind": "equipment"}).content.decode()
        self.assertIn('href="%s?kind=equipment"' % reverse("asset_create"), page)

    def test_a_rejected_form_still_shows_the_kind_that_was_submitted(self):
        """The page comes back bound. If it reverted to vehicle, somebody
        correcting one mistake would find their equipment fields gone."""
        response = self.client.post(
            reverse("asset_create"), {"asset_kind": "equipment", "manufacturer": "Honda"}
        )

        form = response.context["form"]
        self.assertEqual(form.current_kind(), AssetKind.EQUIPMENT)
        self.assertIn("manufacturer", visible(form))
        self.assertNotIn("vin", visible(form))


class WhatIsRenderedTests(TestCase):
    """The gating reaches the HTML, not just the form object."""

    def setUp(self):
        self.user = User.objects.create_user(username="andy", password="x" * 16)
        self.client.force_login(self.user)

    def test_the_vehicle_sections_are_marked_hidden_for_equipment(self):
        mower = Asset.objects.create(nickname="Mower", asset_kind=AssetKind.EQUIPMENT)
        page = self.client.get(reverse("asset_edit", args=[mower.pk])).content.decode()

        self.assertIn('<section class="card" data-kind="vehicle" hidden>', page)
        self.assertIn('<section class="card" data-kind="equipment">', page)

    def test_the_equipment_section_is_marked_hidden_for_a_vehicle(self):
        page = self.client.get(reverse("asset_create")).content.decode()

        self.assertIn('<section class="card" data-kind="vehicle">', page)
        self.assertIn('<section class="card" data-kind="equipment" hidden>', page)

    def test_a_gated_field_inside_a_shared_section_is_marked_too(self):
        """`make` and `model` sit beside `year`, which both kinds have."""
        mower = Asset.objects.create(nickname="Mower", asset_kind=AssetKind.EQUIPMENT)
        page = self.client.get(reverse("asset_edit", args=[mower.pk])).content.decode()

        self.assertIn('<div class="field" data-kind="vehicle" hidden>', page)

    def test_no_input_is_rendered_twice(self):
        """The whole reason each field is placed exactly once. Two boxes with
        one name post two values and the reader can only see one of them."""
        page = self.client.get(reverse("asset_create")).content.decode()

        for name in AssetForm.Meta.fields:
            with self.subTest(field=name):
                self.assertEqual(page.count('name="%s"' % name), 1)

    def test_equipment_is_not_offered_the_vin_lookup(self):
        """FR-EQP-3 — hidden rather than shown-and-failing."""
        mower = Asset.objects.create(nickname="Mower", asset_kind=AssetKind.EQUIPMENT)
        page = self.client.get(reverse("asset_edit", args=[mower.pk])).content.decode()

        # The registration section is the only thing carrying the scan button,
        # and on this page it is hidden whole.
        section = page.split('<section class="card" data-kind="vehicle" hidden>')[1]
        section = section.split("</section>")[0]
        self.assertIn('name="vin"', section)


class ChangingTheKindTests(TestCase):
    """A vehicle turned into equipment, and back."""

    def setUp(self):
        self.user = User.objects.create_user(username="andy", password="x" * 16)
        self.client.force_login(self.user)
        self.truck = Asset.objects.create(
            nickname="Old truck", vin=VIN, plate="7ABC123", plate_region="CA",
            vehicle_class="truck", make="Ford", model="F-150", year=2013,
        )

    def _submit(self, **extra):
        data = {"nickname": "Old truck", "asset_kind": "equipment", "year": "2013",
                "status": "active", "meter": "engine_hours", "meter_unit": "hours"}
        data.update(extra)
        return self.client.post(reverse("asset_edit", args=[self.truck.pk]), data)

    def test_the_vehicle_fields_are_cleared_rather_than_argued_about(self):
        """`Asset.clean()` refuses a VIN on equipment, and the box carrying it
        is off the screen by then — so that error could never be fixed."""
        response = self._submit(vin=VIN, plate="7ABC123", vehicle_class="truck")

        self.assertEqual(response.status_code, 302)
        self.truck.refresh_from_db()
        self.assertEqual(self.truck.asset_kind, AssetKind.EQUIPMENT)
        self.assertEqual(self.truck.vin, "")
        self.assertEqual(self.truck.plate, "")
        self.assertEqual(self.truck.vehicle_class, "")

    def test_a_vin_that_would_not_validate_does_not_block_the_change(self):
        """Whatever is still sitting in the hidden box, valid or not, is not
        this record's problem once it is no longer a vehicle."""
        response = self._submit(vin="NOTAREALVIN000000")

        self.assertEqual(response.status_code, 302)
        self.assertEqual(Asset.objects.get(pk=self.truck.pk).vin, "")

    def test_a_date_field_is_cleared_to_null_not_to_an_empty_string(self):
        """`plate_expires_on` is nullable; "" would be a database error rather
        than a cleared field."""
        self.truck.plate_expires_on = "2030-01-01"
        self.truck.save()

        self._submit(plate_expires_on="2030-01-01")

        self.assertIsNone(Asset.objects.get(pk=self.truck.pk).plate_expires_on)

    def test_what_the_new_kind_keeps_is_kept(self):
        response = self._submit(year="2013", engine="Kohler 7000", manufacturer="Kohler")

        self.assertEqual(response.status_code, 302)
        mower = Asset.objects.get(pk=self.truck.pk)
        self.assertEqual(mower.year, 2013)
        self.assertEqual(mower.engine, "Kohler 7000")
        self.assertEqual(mower.manufacturer, "Kohler")

    def test_a_field_cleared_by_the_change_is_not_recorded_as_a_correction(self):
        """FR-VEH-4 keeps a human's edit safe from a re-decode. A make emptied
        because the thing stopped being a vehicle is nobody's edit, and
        recording it would pin that blank over every future decode."""
        self.truck.decoded_raw = {"Make": "FORD"}
        self.truck.save()

        self._submit(make="Ford", model="F-150")

        self.assertEqual(Asset.objects.get(pk=self.truck.pk).field_overrides, {})

    def test_saving_equipment_through_the_form_gets_an_hour_meter(self):
        """FR-EQP-2. The model has always done this on save; the form now
        shows it, so the two agree before the button is pressed."""
        response = self.client.post(
            reverse("asset_create"),
            {"nickname": "Mower", "asset_kind": "equipment", "status": "active",
             "meter": "odometer", "meter_unit": "mi"},
        )

        self.assertEqual(response.status_code, 302)
        mower = Asset.objects.get(nickname="Mower")
        self.assertEqual(mower.meter, "engine_hours")
        self.assertEqual(mower.meter_unit, "hours")
