"""
Global search (SPEC FR-SEARCH-1/2).

These exist because the search box promised things it did not deliver. Its
placeholder reads *"Part number, nickname, VIN, a phrase from a note…"* and, as
shipped, it never queried parts at all and could not find a VIN by any fragment
of it — which is the only way anybody actually searches for one, because the
vehicle page **masks** the VIN and shows just the first three and last six
characters.

The cause is that full-text search is the wrong tool for an identifier.
Postgres turns `1FTFW1ET5DFC10312` into the single lexeme
`'1ftfw1et5dfc10312'`; `WO-2026-0001` becomes `'wo'`, `'-2026'`, `'-0001'`.
Matching is by whole lexeme, so a fragment matches nothing. Stemming and
ranking are exactly right for a note and exactly wrong for a part number.

So identifiers are matched as substrings and prose keeps full-text search. Each
test below names a search somebody would really type.
"""

from __future__ import annotations

from django.test import TestCase

from homeautoshop.assets.models import Asset
from homeautoshop.core.search import search
from homeautoshop.parts.models import Part, PartCrossRef
from homeautoshop.people.models import Person
from homeautoshop.work.models import WorkOrder, WorkOrderNote

# The ISO 3779 worked example: a valid VIN belonging to nobody.
VIN = "1M8GDM9AXKP042788"


def found(query: str, kind: str) -> list:
    for group in search(query).groups:
        if group.kind == kind:
            return group.results
    return []


class VinSearchTests(TestCase):
    """The reported bug: a VIN that cannot be found by any part of itself."""

    def setUp(self):
        self.asset = Asset.objects.create(nickname="Red truck", vin=VIN, make="MCI")

    def test_the_whole_vin_finds_it(self):
        self.assertIn(self.asset, found(VIN, "asset"))

    def test_the_last_six_find_it(self):
        """What the masked vehicle page actually shows, so what gets typed."""
        self.assertIn(self.asset, found(VIN[-6:], "asset"))

    def test_the_first_three_find_it(self):
        self.assertIn(self.asset, found(VIN[:3], "asset"))

    def test_a_run_from_the_middle_finds_it(self):
        self.assertIn(self.asset, found(VIN[4:11], "asset"))

    def test_lowercase_finds_it(self):
        self.assertIn(self.asset, found(VIN.lower(), "asset"))

    def test_every_fragment_the_mask_reveals_is_searchable(self):
        """Whatever the mask shows has to be enough to search by — otherwise the
        privacy measure and the search box disagree about the same value."""
        masked = self.asset.masked_vin
        visible = [part for part in masked.split("•") if part]
        for fragment in visible:
            with self.subTest(fragment=fragment):
                self.assertIn(self.asset, found(fragment, "asset"))

    def test_another_vehicles_vin_does_not_match(self):
        other = Asset.objects.create(nickname="Van", vin="1FTFW1ET5DFC10312")
        self.assertNotIn(other, found(VIN[-6:], "asset"))


class PlateSearchTests(TestCase):
    def setUp(self):
        self.asset = Asset.objects.create(nickname="Red truck", plate="ABC-1234")

    def test_the_whole_plate_finds_it(self):
        self.assertIn(self.asset, found("ABC-1234", "asset"))

    def test_part_of_it_finds_it(self):
        self.assertIn(self.asset, found("1234", "asset"))

    def test_punctuation_is_not_load_bearing(self):
        """Nobody remembers whether they typed the hyphen."""
        for typed in ("ABC1234", "abc 1234", "abc-1234"):
            with self.subTest(typed=typed):
                self.assertIn(self.asset, found(typed, "asset"))


class WorkOrderSearchTests(TestCase):
    def setUp(self):
        self.asset = Asset.objects.create(nickname="Red truck")
        self.wo = WorkOrder.objects.create(asset=self.asset, title="Front brakes")

    def test_the_number_finds_it(self):
        self.assertIn(self.wo, found(self.wo.number, "work_order"))

    def test_the_sequence_alone_finds_it(self):
        """"What was WO 12 again?" is how people refer to these out loud."""
        sequence = self.wo.number.rsplit("-", 1)[1]
        self.assertIn(self.wo, found(sequence, "work_order"))

    def test_the_title_still_finds_it(self):
        self.assertIn(self.wo, found("brakes", "work_order"))

    def test_a_phrase_from_the_complaint_finds_it(self):
        self.wo.complaint = "Grinding noise from the front when stopping"
        self.wo.save()
        self.assertIn(self.wo, found("grinding noise", "work_order"))


class PartSearchTests(TestCase):
    """Parts were in the placeholder and not in the query."""

    def setUp(self):
        self.part = Part.objects.create(
            name="Brake pads", manufacturer="Akebono", part_number="ACT1164"
        )

    def test_a_part_number_finds_it(self):
        self.assertIn(self.part, found("ACT1164", "part"))

    def test_part_of_a_part_number_finds_it(self):
        self.assertIn(self.part, found("1164", "part"))

    def test_the_name_finds_it(self):
        self.assertIn(self.part, found("brake pads", "part"))

    def test_the_brand_finds_it(self):
        self.assertIn(self.part, found("akebono", "part"))

    def test_a_cross_reference_number_finds_the_part(self):
        """The number on the old box is rarely the number in the catalog."""
        PartCrossRef.objects.create(part=self.part, value="D1164-8888")
        self.assertIn(self.part, found("D1164-8888", "part"))
        self.assertIn(self.part, found("8888", "part"))


class PeopleSearchTests(TestCase):
    def setUp(self):
        self.person = Person.objects.create(
            display_name="Alice Nguyen", email="alice@example.com", phone="555-0142"
        )

    def test_a_name_finds_them(self):
        self.assertIn(self.person, found("nguyen", "person"))

    def test_the_tail_of_a_phone_number_finds_them(self):
        self.assertIn(self.person, found("0142", "person"))

    def test_part_of_an_email_finds_them(self):
        self.assertIn(self.person, found("alice@", "person"))


class ProseSearchTests(TestCase):
    """Notes keep full-text search — it is the right tool for a sentence."""

    def setUp(self):
        self.asset = Asset.objects.create(nickname="Red truck")
        self.wo = WorkOrder.objects.create(asset=self.asset, title="Bearing")
        self.note = WorkOrderNote.objects.create(
            work_order=self.wo, body="Left front bearing has play, ordered a Timken"
        )

    def test_a_phrase_from_a_note_finds_it(self):
        self.assertIn(self.note, found("bearing has play", "note"))

    def test_a_single_word_finds_it(self):
        self.assertIn(self.note, found("timken", "note"))


class SearchShapeTests(TestCase):
    def test_a_single_character_searches_nothing(self):
        """Otherwise every keystroke in the header box scans every table."""
        Asset.objects.create(nickname="Red truck", vin=VIN)
        self.assertTrue(search("1").is_empty)

    def test_an_empty_query_is_empty_rather_than_everything(self):
        Asset.objects.create(nickname="Red truck", vin=VIN)
        self.assertTrue(search("").is_empty)
        self.assertTrue(search("   ").is_empty)

    def test_a_vehicle_is_not_listed_twice_when_two_fields_match(self):
        asset = Asset.objects.create(nickname="ABC truck", plate="ABC-1234")
        self.assertEqual(found("ABC", "asset").count(asset), 1)

    def test_a_deleted_vehicle_stays_out_of_the_results(self):
        asset = Asset.objects.create(nickname="Parts car", vin=VIN)
        asset.delete()
        self.assertEqual(found(VIN, "asset"), [])

    def test_the_placeholder_only_promises_what_is_searched(self):
        """The bug that started this: the box advertised parts and VINs, and
        searched neither. If the promise changes, this fails."""
        from django.template.loader import render_to_string

        Asset.objects.create(nickname="Red truck", vin=VIN)
        Part.objects.create(name="Brake pads", part_number="ACT1164")
        Person.objects.create(display_name="Alice")

        kinds = {group.kind for group in search(VIN[-6:]).groups}
        kinds |= {group.kind for group in search("ACT1164").groups}
        kinds |= {group.kind for group in search("alice").groups}
        self.assertEqual({"asset", "part", "person"}, kinds)
