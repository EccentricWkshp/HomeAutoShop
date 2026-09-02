"""
Installing and removing a manufacturer's code list.

The ISO/SAE sets ship in the image because they answer for every vehicle ever
built and there are three and a half thousand of them. A manufacturer's list
does neither: there are ninety-odd makes, a shop owns three, and bundling
them all would put eighteen thousand definitions in every image so that each
operator could use a few hundred. Parser profiles were split for the same
reason, and these tests are about the seam that split creates.

`codelistlib` is the whole trust model, because `catalog.install` calls it and
gets nothing a pasted file would not. So most of what is here is refusals.
"""

from __future__ import annotations

import json
from io import StringIO

from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse

from homeautoshop.accounts.models import Role, User

from . import codelistlib, dtc
from .models import InstalledCodeList


def a_list(**changes) -> str:
    payload = {
        "make": "Testla",
        "aliases": ["Tesler"],
        "version": 3,
        "author": "Somebody",
        "documents": [
            {
                "source": "Testla service manual",
                "precedence": 0,
                "codes": {"P1500": "Wastegate position sensor performance"},
            }
        ],
    }
    payload.update(changes)
    return json.dumps(payload)


class WhatItRefusesTests(TestCase):
    """Written for a hostile file rather than a friendly one. `CATALOG_URL` is
    a setting, so the editorial review that protects the default catalog is
    somebody else's process or nobody's the moment it is pointed elsewhere."""

    def refuse(self, **changes):
        with self.assertRaises(codelistlib.CodeListInvalid) as caught:
            codelistlib.parse(a_list(**changes))
        return str(caught.exception)

    def test_a_file_claiming_to_be_the_standard(self):
        """The one that matters. An ISO/SAE list is presented as fact, because
        such a code means the same thing on every vehicle ever built. A
        stranger's wording installing itself with that authority is the exact
        failure scoping by make exists to prevent — so it is refused outright
        rather than quietly downgraded to a manufacturer list."""
        said = self.refuse(documents=[{
            "source": "A standard, honestly",
            "scope": "iso_sae",
            "codes": {"P0420": "Catalyst below threshold"},
        }])
        self.assertIn("ISO/SAE", said)

    def test_a_document_that_will_not_say_where_it_came_from(self):
        """Every definition is quoted beside who says so. An unattributed one
        on a diagnostic screen is worth less than no definition."""
        self.refuse(documents=[{"source": "", "codes": {"P1500": "Something"}}])

    def test_a_key_that_is_not_a_trouble_code(self):
        """A table with `Cylinder 4` in the key column is not a code table,
        whatever it says on the front."""
        said = self.refuse(documents=[{
            "source": "A document", "codes": {"Cylinder 4": "Misfire"},
        }])
        self.assertIn("Cylinder 4", said)

    def test_a_definition_that_says_nothing(self):
        """§8.3c refuses invented wording; an empty string offered as a
        definition is the same failure with less effort."""
        self.refuse(documents=[{"source": "A document", "codes": {"P1500": "   "}}])

    def test_a_field_this_reader_does_not_know(self):
        """So a file cannot smuggle in something a later version might start
        honouring."""
        self.refuse(trust_me=True)

    def test_a_list_with_no_documents_at_all(self):
        self.refuse(documents=[])

    def test_a_list_with_no_make(self):
        self.refuse(make="  ")

    def test_something_that_is_not_json(self):
        with self.assertRaises(codelistlib.CodeListInvalid):
            codelistlib.parse("make: Testla\\n")

    def test_a_version_that_is_not_a_number(self):
        self.refuse(version="3.0.1")


class WhatItAcceptsTests(TestCase):
    def test_the_codes_arrive(self):
        held = codelistlib.load(a_list())

        self.assertEqual(held.make, "Testla")
        self.assertEqual(held.version, 3)
        self.assertEqual(held.code_count, 1)

    def test_characters_are_repaired_on_the_way_in(self):
        """An installed list is held to the standard a bundled one is, rather
        than to whatever its publisher's extractor managed."""
        held = codelistlib.load(a_list(documents=[{
            "source": "A document",
            "codes": {"P1500": "Sensor, LH – voltage low"},
        }]))

        self.assertEqual(
            held.documents[0]["codes"]["P1500"], "Sensor, LH - voltage low"
        )

    def test_installing_again_replaces_rather_than_doubles(self):
        """Two copies of Ford's list differing only in version is not a make
        covered by two documents — it is the same document twice, and a lookup
        would quote whichever sorted first."""
        codelistlib.load(a_list())
        codelistlib.load(a_list(version=4))

        self.assertEqual(InstalledCodeList.objects.count(), 1)
        self.assertEqual(InstalledCodeList.objects.get().version, 4)


class WhatItAnswersTests(TestCase):
    def setUp(self):
        dtc._lists.cache_clear()
        self.addCleanup(dtc._lists.cache_clear)

    def test_nothing_until_it_is_installed(self):
        self.assertEqual(dtc.explain("P1500", make="Testla").source, dtc.STRUCTURE)

    def test_and_the_makers_own_wording_once_it_is(self):
        codelistlib.load(a_list())

        found = dtc.explain("P1500", make="Testla")
        self.assertEqual(found.source, dtc.MAKE)
        self.assertIn("Wastegate", found.text)

    def test_an_alias_reads_it_too(self):
        codelistlib.load(a_list())

        self.assertEqual(dtc.explain("P1500", make="Tesler").source, dtc.MAKE)

    def test_removing_it_stops_it_answering(self):
        """The cache is why this is worth a test. `_lists` is consulted on
        every lookup and held for the life of the process; without dropping it
        on a write, a removal looks like it did not work."""
        held = codelistlib.load(a_list())
        self.assertEqual(dtc.explain("P1500", make="Testla").source, dtc.MAKE)

        held.delete()

        self.assertEqual(dtc.explain("P1500", make="Testla").source, dtc.STRUCTURE)

    def test_the_standard_still_answers_with_nothing_installed(self):
        """The reason the ISO/SAE sets stayed in the image. An instance that
        has never reached a network still knows what P0420 means."""
        self.assertEqual(dtc.explain("P0420", make="Testla").source, dtc.STANDARD)


class TheOfflineWayInTests(TestCase):
    """`install_code_list` and the upload form.

    P-1 says an instance that reaches nothing must still work. While the lists
    were bundled, offline meant *no worse*; now that they are published, the
    route that does not involve the network has to be built rather than
    assumed.
    """

    def setUp(self):
        dtc._lists.cache_clear()
        self.addCleanup(dtc._lists.cache_clear)
        self.user = User.objects.create_user(
            "andy", password="correct-horse-battery", role=Role.ADMIN
        )
        self.client.force_login(self.user)

    def test_a_published_make_installs_by_name(self):
        call_command("install_code_list", "Ford", stdout=StringIO(), stderr=StringIO())

        self.assertTrue(InstalledCodeList.objects.filter(make="Ford").exists())
        self.assertEqual(dtc.explain("B1352", make="Ford").source, dtc.MAKE)

    def test_a_make_nobody_publishes_says_so_rather_than_installing_nothing(self):
        from django.core.management.base import CommandError

        with self.assertRaises(CommandError):
            call_command("install_code_list", "Tucker", stdout=StringIO(), stderr=StringIO())

    def test_a_file_can_be_uploaded(self):
        response = self.client.post(
            reverse("codelist_import"), {"yaml": a_list()}, follow=True
        )

        self.assertContains(response, "Testla")
        self.assertTrue(InstalledCodeList.objects.filter(make="Testla").exists())

    def test_an_upload_gets_the_same_refusal_the_catalog_would(self):
        """No privileged path for catalog content, and no privileged path for
        an operator's own file either. That equivalence is the trust model."""
        response = self.client.post(
            reverse("codelist_import"),
            {"yaml": a_list(documents=[{
                "source": "A standard, honestly",
                "scope": "iso_sae",
                "codes": {"P0420": "Catalyst below threshold"},
            }])},
            follow=True,
        )

        self.assertContains(response, "refused")
        self.assertFalse(InstalledCodeList.objects.exists())

    def test_removing_one_from_the_screen(self):
        held = codelistlib.load(a_list())

        self.client.post(reverse("codelist_delete", args=[held.pk]), follow=True)

        self.assertFalse(InstalledCodeList.objects.filter(pk=held.pk).exists())


class LookingACodeUpTests(TestCase):
    """`dtc.find` — a lookup that does not start with a scan report.

    The reference page could only be reached by importing a report and
    clicking a reading, so answering "what is P0420" meant running a scan
    first. Both directions are needed: somebody reading a number off a cracked
    screen, and somebody who knows the symptom and not the number.
    """

    def setUp(self):
        dtc._lists.cache_clear()
        self.addCleanup(dtc._lists.cache_clear)

    def codes(self, query, **kwargs):
        return [found.code for found in dtc.find(query, **kwargs)]

    def test_an_exact_code(self):
        self.assertIn("P0420", self.codes("P0420"))

    def test_a_prefix_for_a_number_read_off_a_cracked_screen(self):
        self.assertIn("P0421", self.codes("P042"))

    def test_words_from_the_definition_in_any_order(self):
        """Nobody types a definition verbatim. `catalyst efficiency` is how a
        person asks for "Catalyst system efficiency below threshold", and a
        phrase match answers that with nothing while looking like the code
        does not exist."""
        self.assertIn("P0420", self.codes("catalyst efficiency"))
        self.assertIn("P0420", self.codes("efficiency catalyst"))

    def test_the_standards_answer_comes_first(self):
        """An ISO/SAE code means the same thing on every vehicle ever built.
        One make's restatement of it is the narrower claim."""
        self.assertEqual(dtc.find("P0420")[0].source, dtc.STANDARD)

    def test_an_exact_code_outranks_a_definition_that_mentions_it(self):
        self.assertEqual(dtc.find("P0420")[0].code, "P0420")

    def test_a_manufacturer_code_is_found_once_its_list_is_installed(self):
        self.assertEqual(self.codes("P1500"), [])

        codelistlib.load(a_list())

        found = dtc.find("P1500")
        self.assertEqual([f.code for f in found], ["P1500"])
        self.assertEqual(found[0].make, "Testla")

    def test_every_hit_says_which_authority_answered(self):
        """`P1345` is one thing to GM and another to Toyota. A result that did
        not say which would be worse than no result."""
        codelistlib.load(a_list())

        for found in dtc.find("P1500"):
            self.assertTrue(found.source)
            self.assertTrue(found.make or found.source == dtc.STANDARD)

    def test_one_character_is_not_a_search(self):
        self.assertEqual(dtc.find("P"), [])

    def test_it_stops_at_the_limit(self):
        self.assertLessEqual(len(dtc.find("circuit", limit=5)), 5)


class TheVersionOfAListTests(TestCase):
    """Every list carries its publisher's revision — the standard's included.

    J2012 gets revised and codes get added. A definition presented to an
    operator as fact still came from a particular printing, and "which revision
    is this instance answering from" is a question a transcription has to be
    able to answer or it cannot be checked against its source.
    """

    def setUp(self):
        dtc._lists.cache_clear()
        self.addCleanup(dtc._lists.cache_clear)

    def test_the_bundled_iso_sae_sets_have_one(self):
        standards = [e for e in dtc._every_list() if e.is_iso_sae]

        self.assertTrue(standards)
        for entry in standards:
            with self.subTest(list=entry.make):
                self.assertGreaterEqual(entry.version, 1)

    def test_an_installed_list_reports_the_version_it_was_published_at(self):
        codelistlib.load(a_list(version=7))

        self.assertEqual(dtc.code_list_for("Testla").version, 7)

    def test_an_answer_says_which_revision_it_came_from(self):
        codelistlib.load(a_list(version=7))

        self.assertEqual(dtc.explain("P1500", make="Testla").version, 7)

    def test_the_hand_written_standard_table_has_one_too(self):
        """It is written out the way J2012 phrases it rather than transcribed
        from a file, so it changes with the module — but an answer presented
        as fact should still say which printing it came from, and a zero here
        beside `is_authoritative` reads as a missing value."""
        self.assertEqual(dtc.explain("P0420").version, dtc.ISO_SAE_VERSION)
        self.assertGreaterEqual(dtc.ISO_SAE_VERSION, 1)

    def test_an_answer_from_no_list_at_all_claims_no_version(self):
        """A note typed here, the scan tool's own wording, and structure are
        not revisions of anything."""
        self.assertEqual(dtc.explain("P1500", make="Testla").version, 0)


class TheApiTests(TestCase):
    """`/api/v1/codes` — the quick lookup, without a report or a scan."""

    def setUp(self):
        dtc._lists.cache_clear()
        self.addCleanup(dtc._lists.cache_clear)
        self.user = User.objects.create_user(
            "andy", password="correct-horse-battery", role=Role.ADMIN
        )
        self.client.force_login(self.user)

    def test_searching_by_code(self):
        body = self.client.get("/api/v1/codes?q=P0420").json()

        self.assertEqual(body[0]["code"], "P0420")
        self.assertEqual(body[0]["source"], "standard")
        self.assertTrue(body[0]["is_authoritative"])

    def test_searching_by_meaning(self):
        body = self.client.get("/api/v1/codes?q=catalyst+efficiency").json()

        self.assertIn("P0420", [row["code"] for row in body])

    def test_one_code_with_the_make_that_answers_it(self):
        codelistlib.load(a_list())

        body = self.client.get("/api/v1/codes/P1500?make=Testla").json()

        self.assertEqual(body["source"], "make")
        self.assertEqual(body["make"], "Testla")
        self.assertIn("Wastegate", body["text"])

    def test_the_same_code_without_a_make_falls_back_to_structure(self):
        """The whole reason lists are scoped. Testla's answer is not evidence
        about anybody else's P1500."""
        codelistlib.load(a_list())

        body = self.client.get("/api/v1/codes/P1500").json()

        self.assertEqual(body["source"], "structure")

    def test_something_that_is_not_a_code_is_a_404_not_an_empty_answer(self):
        """A typo and a code nobody has written down are different, and the
        caller has to be able to tell them apart."""
        response = self.client.get("/api/v1/codes/nonsense")

        self.assertEqual(response.status_code, 404)
