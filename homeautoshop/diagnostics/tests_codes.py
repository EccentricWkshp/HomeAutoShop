"""
Manufacturer-specific trouble codes: bundled lists, and a page per code.

§8.3c said manufacturer codes "come from the operator, once, per make", which
was the right answer for a code nobody publishes and needlessly bleak for one
somebody does. Ford publishes three thousand of them. A shop with a Ford in it
was being asked to key in `B1352 Ignition Key-In Circuit Failure` to learn what
its own scan tool had just read.

Two things follow, and both are tested here.

* **A published list can be transcribed and shipped**, under the SAE set and
  above a note somebody typed — the order matters and is asserted, because the
  whole value of the layering is that the screen can say which one answered.
* **A code is a link.** It used to be five characters of monospace and a dead
  end: where the meaning was an em dash, the only way to fill it in was an
  inline box that exists on *draft* sessions alone, so a code read last year
  could never be named at all.
"""

from __future__ import annotations

import json
from io import StringIO
from pathlib import Path

from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse

from homeautoshop.accounts.models import AssetAccess, Role, User
from homeautoshop.assets.models import Asset

from . import codelistlib, dtc, services
from .models import CodeDescription, DiagnosticCode, DiagnosticSession, ReviewStatus

# Real Ford codes, from the list this ships. `B1352` is the one on the mower
# screenshot that started this.
FORD_CODE = "B1352"
FORD_MEANING = "Ignition Key-In Circuit Failure"


def shipped_lists():
    """Every code list in the image. `_`-prefixed files are registers.

    Since the split these are the ISO/SAE sets and nothing else: they answer
    for every vehicle ever built, so an instance that has never reached a
    network still knows what `P0420` means. A manufacturer's list is published
    instead — see `published_lists`.
    """
    folder = Path(dtc.codelists.__file__).parent
    return [p for p in sorted(folder.glob("*.json")) if not p.name.startswith("_")]


def published_lists():
    """Every manufacturer list published for a shop to install."""
    from homeautoshop.core.management.commands.build_dtc_list import catalog_codes

    return sorted(catalog_codes().glob("*.json"))


def install(*makes):
    """Install published lists, the way an offline operator would.

    Through the ordinary command, so a test never reaches a state the
    application cannot: everything goes through `codelistlib.load`, which is
    the one validator the catalog and an uploaded file also use.
    """
    call_command("install_code_list", *makes, stdout=StringIO(), stderr=StringIO())


class Installed:
    """Mixin: the published lists a test class needs, installed once.

    Manufacturer lists are no longer in the image, so a test about what Ford's
    list says has to arrange for Ford's list to be here. Class-level, because
    three thousand rows per test is a slow way to say the same thing.
    """

    makes: tuple = ()

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        if cls.makes:
            install(*cls.makes)

    def setUp(self):
        super().setUp()
        dtc._lists.cache_clear()
        self.addCleanup(dtc._lists.cache_clear)


class TheBundledListTests(TestCase):
    """The files themselves, before anything looks anything up in them."""

    def setUp(self):
        dtc._lists.cache_clear()

    def test_only_the_standards_own_sets_are_in_the_image(self):
        """The split. Ninety makes would put eighteen thousand definitions in
        every image so that each shop could use a few hundred; the ISO/SAE sets
        are finite and answer for everything, so they stay."""
        for path in shipped_lists():
            data = json.loads(path.read_text(encoding="utf-8"))
            with self.subTest(file=path.name):
                self.assertEqual(data.get("scope"), "iso_sae")

    def test_no_manufacturer_list_answers_until_one_is_installed(self):
        self.assertEqual(dtc.makes_with_lists(), [])
        self.assertIsNone(dtc.code_list_for("Ford"))

    def test_every_code_in_every_shipped_list_is_code_shaped(self):
        """The transcription reads a PDF. A dropped character turns a real
        definition into a lookup against a code that cannot exist — and it
        would sit there being wrong rather than failing."""
        for path in shipped_lists():
            data = json.loads(path.read_text(encoding="utf-8"))
            for code in data["codes"]:
                with self.subTest(file=path.name, code=code):
                    self.assertIsNotNone(dtc.parse(code))
                    self.assertEqual(dtc.parse(code)["code"], code)

    def test_every_shipped_list_says_where_it_came_from(self):
        """A transcription of somebody's document that does not name the
        document cannot be checked against it."""
        for path in shipped_lists():
            data = json.loads(path.read_text(encoding="utf-8"))
            with self.subTest(file=path.name):
                self.assertTrue(data.get("make"))
                self.assertTrue(data.get("source"))

    def test_no_description_is_empty(self):
        for path in shipped_lists():
            data = json.loads(path.read_text(encoding="utf-8"))
            for code, text in data["codes"].items():
                with self.subTest(file=path.name, code=code):
                    self.assertTrue(text.strip())


class ThePublishedListsTests(TestCase):
    """The manufacturer lists in `catalog/codes/`, before anything installs one.

    Same guarantees the bundled files get. Moving them out of the image did
    not make them somebody else's problem — they are still published from this
    repository, and a transcription that cannot be checked against its source
    is no better for being installable.
    """

    def test_every_one_is_readable_by_the_validator_that_will_install_it(self):
        """The check that matters most: a file that would not install cannot
        be published at all. `build_catalog --check` runs this in the suite,
        and this says the same thing where a reader will look for it."""
        for path in published_lists():
            with self.subTest(file=path.name):
                codelistlib.parse(path.read_text(encoding="utf-8"))

    def test_every_document_says_where_it_came_from(self):
        for path in published_lists():
            data = json.loads(path.read_text(encoding="utf-8"))
            for document in data["documents"]:
                with self.subTest(file=path.name):
                    self.assertTrue(document.get("source"))

    def test_every_code_is_code_shaped(self):
        for path in published_lists():
            data = json.loads(path.read_text(encoding="utf-8"))
            for document in data["documents"]:
                for code in document["codes"]:
                    with self.subTest(file=path.name, code=code):
                        self.assertEqual(dtc.parse(code)["code"], code)

    def test_none_of_them_claims_to_be_the_standard(self):
        """An installable list covers one manufacturer. One that installed
        itself as the standard would be a stranger's wording presented as
        fact on every vehicle in the shop."""
        for path in published_lists():
            data = json.loads(path.read_text(encoding="utf-8"))
            for document in data["documents"]:
                with self.subTest(file=path.name):
                    self.assertEqual(document.get("scope", "make"), "make")

    def test_ford_is_published_and_answers_once_installed(self):
        install("Ford")
        dtc._lists.cache_clear()
        self.addCleanup(dtc._lists.cache_clear)

        self.assertIn("Ford", dtc.makes_with_lists())
        self.assertEqual(dtc.explain(FORD_CODE, make="Ford").text, FORD_MEANING)


class WhichLayerAnswersTests(Installed, TestCase):
    makes = ("Ford",)

    """Four sources, ranked, each saying so."""

    def setUp(self):
        dtc._lists.cache_clear()

    def test_a_ford_code_is_answered_by_fords_own_list(self):
        found = dtc.explain(FORD_CODE, make="Ford")
        self.assertEqual(found.text, FORD_MEANING)
        self.assertEqual(found.source, dtc.MAKE)
        self.assertEqual(found.make, "Ford")
        self.assertTrue(found.citation, "a shipped answer must name its document")

    def test_it_is_answered_the_same_for_however_the_make_is_spelled(self):
        """vPIC says FORD; a person types Ford."""
        for spelling in ("FORD", "ford", " Ford "):
            with self.subTest(make=spelling):
                self.assertEqual(dtc.explain(FORD_CODE, make=spelling).text, FORD_MEANING)

    def test_lincoln_and_mercury_read_fords_list(self):
        """It is the Ford Motor Company Group's document, and they are the
        same modules with a different badge. Three copies of one table is how
        two of them go stale."""
        for make in ("Lincoln", "Mercury"):
            with self.subTest(make=make):
                self.assertEqual(dtc.explain(FORD_CODE, make=make).text, FORD_MEANING)

    def test_the_same_code_on_another_make_is_not_answered(self):
        """The whole reason a definition is scoped to a make. Ford's B1352 is
        not evidence about anybody else's B1352."""
        found = dtc.explain(FORD_CODE, make="Toyota")
        self.assertEqual(found.source, dtc.STRUCTURE)
        self.assertFalse(found.is_known)

    def test_a_generic_code_still_comes_from_the_standard(self):
        """Ford's list restates the generic set in its own older phrasing.
        SAE defines those identically for every vehicle ever built, so nothing
        below it gets to answer."""
        found = dtc.explain("P0420", make="Ford")
        self.assertEqual(found.source, dtc.STANDARD)
        self.assertTrue(found.is_authoritative)
        self.assertIn("Catalyst", found.text)

    def test_a_note_typed_in_the_shop_outranks_the_shipped_list(self):
        """The person holding the vehicle outranks a document about it — the
        same rule that keeps a corrected VIN decode from being clobbered."""
        CodeDescription.objects.create(
            make="Ford", code=FORD_CODE, description="Key-in-ignition switch, driver door"
        )
        found = dtc.explain(FORD_CODE, make="Ford")

        self.assertEqual(found.source, dtc.OPERATOR)
        self.assertEqual(found.text, "Key-in-ignition switch, driver door")
        self.assertFalse(found.is_authoritative)

    def test_a_code_nobody_has_a_word_for_still_answers_with_its_shape(self):
        found = dtc.explain("P1FFF", make="Sterling")
        self.assertEqual(found.source, dtc.STRUCTURE)
        self.assertIn("Powertrain", found.text)
        self.assertFalse(found.is_known)

    def test_something_that_is_not_a_code_gets_no_answer_at_all(self):
        """Different from a code nobody has defined, and the caller has to be
        able to tell those apart."""
        self.assertIsNone(dtc.explain("Bananas"))

    def test_the_old_two_value_shape_still_works(self):
        text, authoritative = dtc.describe("P0420")
        self.assertTrue(authoritative)
        self.assertIn("Catalyst", text)


class CodeCase(TestCase):
    def setUp(self):
        dtc._lists.cache_clear()
        self.user = User.objects.create_user("andy", password="correct-horse-battery")
        self.client.force_login(self.user)
        self.truck = Asset.objects.create(nickname="Work truck", make="FORD", model="F-150")

    def read(self, code=FORD_CODE, *, asset=None, description="", status="open"):
        session = DiagnosticSession.objects.create(
            asset=asset or self.truck, tool="XTOOL", review_status=ReviewStatus.CONFIRMED
        )
        return DiagnosticCode.objects.create(
            session=session, code=code, description=description, status=status,
            is_iso_sae=dtc.parse(code)["is_iso_sae"],
        )


class TheCodePageTests(Installed, CodeCase):
    makes = ("Ford",)

    def test_it_shows_the_meaning_and_who_says_so(self):
        page = self.client.get(
            reverse("code_reference", args=[FORD_CODE]), {"make": "Ford"}
        ).content.decode()

        self.assertIn(FORD_MEANING, page)
        self.assertIn("Ford", page)

    def test_it_says_plainly_when_nobody_has_defined_it(self):
        page = self.client.get(
            reverse("code_reference", args=["B2FFF"]), {"make": "Ford"}
        ).content.decode()

        self.assertIn("Nobody has said", page)
        # And never invents something plausible to fill the gap.
        self.assertIn("worse than a blank", page)

    def test_it_reports_the_shape_for_a_code_nothing_has_heard_of(self):
        page = self.client.get(reverse("code_reference", args=["C2ABC"])).content.decode()
        self.assertIn("Chassis", page)

    def test_something_that_is_not_a_code_is_not_a_page(self):
        self.assertEqual(
            self.client.get(reverse("code_reference", args=["Bananas"])).status_code, 404
        )

    def test_it_lists_where_the_code_has_turned_up(self):
        """Often the real answer: a definition cannot tell you it came back
        twice on the same truck after the same repair."""
        self.read()
        page = self.client.get(reverse("code_reference", args=[FORD_CODE])).content.decode()

        self.assertIn("Work truck", page)
        self.assertIn(reverse("asset_diagnostics", args=[self.truck.pk]), page)

    def test_the_make_is_taken_from_where_it_was_last_seen(self):
        """Arriving with no make named, the shop's own reading is the best
        guess available and is better than answering for nobody."""
        self.read()
        response = self.client.get(reverse("code_reference", args=[FORD_CODE]))
        self.assertEqual(response.context["make"], "FORD")
        self.assertContains(response, FORD_MEANING)

    def test_a_helper_sees_only_sightings_on_vehicles_they_were_given(self):
        helper = User.objects.create_user(
            "sam", password="correct-horse-battery", role=Role.HELPER
        )
        theirs = Asset.objects.create(nickname="Their truck", make="FORD")
        AssetAccess.objects.create(user=helper, asset=theirs, level="read")
        self.read(asset=self.truck)
        self.read(asset=theirs)

        self.client.force_login(helper)
        page = self.client.get(reverse("code_reference", args=[FORD_CODE])).content.decode()

        self.assertIn("Their truck", page)
        self.assertNotIn("Work truck", page)


class DefiningACodeTests(Installed, CodeCase):
    makes = ("Ford",)

    def url(self, code=FORD_CODE):
        return reverse("code_define", args=[code])

    def test_a_definition_is_recorded_for_the_make(self):
        response = self.client.post(
            self.url(), {"make": "Ford", "description": "Key-in switch, driver door"}
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            CodeDescription.objects.get(code=FORD_CODE).description,
            "Key-in switch, driver door",
        )

    def test_it_needs_no_reading_to_exist(self):
        """The point of the page. `code_describe` names a code on a reading you
        are looking at; a code whose only session is two years old, or one you
        have not scanned yet, is still one you can write down."""
        self.assertFalse(DiagnosticCode.objects.exists())

        self.client.post(self.url("P1899"), {"make": "Ford", "description": "Learned it"})

        self.assertTrue(CodeDescription.objects.filter(code="P1899").exists())

    def test_it_reaches_readings_already_stored_that_had_no_meaning(self):
        blank = self.read()
        self.client.post(self.url(), {"make": "Ford", "description": "Key-in switch"})

        blank.refresh_from_db()
        self.assertEqual(blank.description, "Key-in switch")

    def test_it_does_not_overwrite_what_the_tool_printed(self):
        """A reading is what the tool said. Somebody's later note is a better
        answer for the blanks and is not licence to rewrite the record."""
        printed = self.read(description="Fault Of Ignition Key In Circuit")
        self.client.post(self.url(), {"make": "Ford", "description": "Key-in switch"})

        printed.refresh_from_db()
        self.assertEqual(printed.description, "Fault Of Ignition Key In Circuit")

    def test_the_make_is_required(self):
        response = self.client.post(self.url(), {"description": "Something"}, follow=True)

        self.assertContains(response, "needs a make")
        self.assertFalse(CodeDescription.objects.exists())

    def test_clearing_it_removes_the_note(self):
        self.client.post(self.url(), {"make": "Ford", "description": "Wrong guess"})
        self.client.post(self.url(), {"make": "Ford", "description": "  "})

        self.assertFalse(CodeDescription.objects.exists())
        # And falls back to what Ford's own list says.
        self.assertEqual(dtc.explain(FORD_CODE, make="Ford").source, dtc.MAKE)

    def test_clearing_it_unwinds_only_the_text_it_wrote(self):
        mine = self.read()
        printed = self.read(description="Fault Of Ignition Key In Circuit")
        self.client.post(self.url(), {"make": "Ford", "description": "Wrong guess"})

        self.client.post(self.url(), {"make": "Ford", "description": ""})

        mine.refresh_from_db()
        printed.refresh_from_db()
        self.assertEqual(mine.description, "")
        self.assertEqual(printed.description, "Fault Of Ignition Key In Circuit")

    def test_one_make_spelled_two_ways_is_still_one_row(self):
        """vPIC writes FORD and a person types Ford. The unique constraint is
        on the exact string, so creating blind makes two rows for one make and
        the lookup then picks whichever sorts first."""
        self.client.post(self.url(), {"make": "FORD", "description": "First"})
        self.client.post(self.url(), {"make": "Ford", "description": "Second"})

        self.assertEqual(CodeDescription.objects.count(), 1)
        self.assertEqual(dtc.explain(FORD_CODE, make="ford").text, "Second")

    def test_it_is_a_post(self):
        self.assertEqual(self.client.get(self.url()).status_code, 405)

    def test_a_helper_may_read_a_code_but_not_define_one(self):
        """A definition is instance-wide: no per-vehicle grant can authorise
        writing something every vehicle in the shop then reads."""
        helper = User.objects.create_user(
            "sam", password="correct-horse-battery", role=Role.HELPER
        )
        self.client.force_login(helper)

        self.assertEqual(
            self.client.get(reverse("code_reference", args=[FORD_CODE])).status_code, 200
        )
        self.assertEqual(
            self.client.post(self.url(), {"make": "Ford", "description": "no"}).status_code, 403
        )
        self.assertFalse(CodeDescription.objects.exists())


class EveryCodeIsAWayInTests(CodeCase):
    def test_the_session_screen_links_its_codes(self):
        code = self.read()
        page = self.client.get(
            reverse("session_detail", args=[code.session.pk])
        ).content.decode()

        self.assertIn(reverse("code_reference", args=[FORD_CODE]), page)

    def test_the_vehicles_open_codes_link_too(self):
        self.read()
        page = self.client.get(
            reverse("asset_diagnostics", args=[self.truck.pk])
        ).content.decode()

        self.assertIn(reverse("code_reference", args=[FORD_CODE]), page)


class TheSharedWritePathTests(CodeCase):
    """`code_describe` and `code_define` write the same thing the same way."""

    def test_describing_from_a_session_uses_the_same_service(self):
        code = self.read()
        self.client.post(
            reverse("code_describe", args=[code.pk]), {"description": "Key-in switch"}
        )

        self.assertEqual(
            CodeDescription.objects.get(code=FORD_CODE).description, "Key-in switch"
        )
        code.refresh_from_db()
        self.assertEqual(code.description, "Key-in switch")

    def test_the_service_reports_what_it_reached(self):
        self.read()
        self.read()
        touched = services.record_description(
            make="FORD", code=FORD_CODE, text="Key-in switch"
        )
        self.assertEqual(touched, 2)


class TheOpenCodesTableTests(Installed, CodeCase):
    makes = ("Ford",)

    """The screen the report came from, when the report named nothing.

    A raw ELM327 read carries codes and no wording at all, and a stored reading
    deliberately keeps only a description that is somebody's — so this column
    was an em dash for a Ford code with Ford's own list sitting on disk.
    """

    def page(self):
        return self.client.get(
            reverse("asset_diagnostics", args=[self.truck.pk])
        ).content.decode()

    def test_a_code_the_report_did_not_name_is_named_from_the_makes_list(self):
        self.read(description="")
        page = self.page()

        self.assertIn(FORD_MEANING, page)
        self.assertIn("Ford", page)

    def test_the_lookup_is_not_written_into_the_record(self):
        """Otherwise today's phrasing freezes into the reading and "has anyone
        named this yet?" stops being answerable."""
        code = self.read(description="")
        self.page()

        code.refresh_from_db()
        self.assertEqual(code.description, "")

    def test_the_makes_own_list_outranks_what_the_tool_printed(self):
        """A tool is a third party rendering somebody else's definition. It
        truncates, and it sometimes declines outright — the reported case is a
        Ford whose tool answered *"Please See The Vehicle Service Manual."*"""
        self.read(code="B1695", description="Please See The Vehicle Service Manual.")
        page = self.page()

        self.assertIn(published_text("B1695", "Ford"), page)
        self.assertIn("Ford's own list", page)

    def test_what_the_tool_read_is_still_printed(self):
        """Ranked below is not discarded. It is what the tool actually said,
        and dropping it would edit the record on the reader's behalf."""
        self.read(code="B1695", description="Please See The Vehicle Service Manual.")
        self.assertIn("Please See The Vehicle Service Manual.", self.page())

    def test_a_code_nobody_can_name_offers_the_way_to_name_it(self):
        self.read(code="B2FFF", description="")
        self.assertIn("Say what it means", self.page())


class TheDefinitionYouSetIsTheOneYouSeeTests(CodeCase):
    """The reported bug, from the screen it was reported on.

    `B1695` went on reading *"Please See The Vehicle Service Manual."* after a
    definition had been recorded for it. Three things had to line up: the
    reading's own column was consulted first and ahead of everything, the
    backfill only touches readings whose description is *blank* so it could not
    reach this one, and the tool had answered with a non-answer that filled the
    column anyway. The definition was stored, was reused, and was never once
    displayed.
    """

    SHRUG = "Please See The Vehicle Service Manual."

    def setUp(self):
        super().setUp()
        self.code = self.read(code="B1695", description=self.SHRUG)

    def define(self, text="Autolamp relay, driver's side"):
        return self.client.post(
            reverse("code_define", args=["B1695"]), {"make": "Ford", "description": text}
        )

    def test_the_definition_is_what_the_vehicle_page_shows(self):
        self.define()
        page = self.client.get(
            reverse("asset_diagnostics", args=[self.truck.pk])
        ).content.decode()

        self.assertIn("Autolamp relay, driver&#x27;s side", page)

    def test_and_what_the_session_shows(self):
        self.define()
        page = self.client.get(
            reverse("session_detail", args=[self.code.session.pk])
        ).content.decode()

        self.assertIn("Autolamp relay, driver&#x27;s side", page)

    def test_and_what_the_code_page_shows(self):
        self.define()
        response = self.client.get(
            reverse("code_reference", args=["B1695"]), {"make": "Ford"}
        )

        self.assertEqual(response.context["definition"].source, dtc.OPERATOR)
        self.assertContains(response, "Autolamp relay, driver&#x27;s side")

    def test_the_reading_itself_is_left_as_the_tool_wrote_it(self):
        """A reading is evidence. A definition recorded afterwards is a better
        answer for the screen and is not licence to rewrite what was read."""
        self.define()
        self.code.refresh_from_db()
        self.assertEqual(self.code.description, self.SHRUG)

    def test_a_job_made_from_it_is_not_titled_with_the_shrug(self):
        """"B1695 — Please See The Vehicle Service Manual." is a complaint
        field answering nothing."""
        self.define()
        self.client.post(reverse("code_promote", args=[self.code.pk]))

        from homeautoshop.work.models import WorkOrder

        self.assertIn("Autolamp relay", WorkOrder.objects.get().title)

    def test_the_session_still_offers_the_box_when_the_tool_shrugged(self):
        """The inline form required an *empty* description, so a tool that
        answers "see the service manual" filled the column and locked the box
        away — on the very screen where somebody is reviewing the scan.

        On a make with no bundled list, since Ford's own list already answers
        this one and there is then nothing to ask for.
        """
        toyota = Asset.objects.create(nickname="The Hilux", make="Toyota")
        code = self.read(code="B1695", asset=toyota, description=self.SHRUG)

        draft = self.client.get(
            reverse("session_detail", args=[code.session.pk])
        ).content.decode()

        self.assertIn("What does it mean on this make?", draft)
        # And it is offered *because* the tool shrugged, not despite it.
        self.assertIn(self.SHRUG, draft)



def published_text(code: str, make: str) -> str:
    """What the catalog publishes for a code, from its top-ranked document.

    Tests read this rather than spelling a definition out, because a literal
    only records which document was on top the day it was written. `B1695` was
    `Autolamp On Circuit Short To Battery` from a compilation and is `Autolamp
    On Circuit Failure` now that Ford's own manuals are published above it —
    both correct, and neither worth failing a test about ranking over.
    """
    from homeautoshop.core.management.commands.build_dtc_list import (
        catalog_codes,
        slug_for,
    )

    data = json.loads(
        (catalog_codes() / f"{slug_for(make)}.json").read_text(encoding="utf-8")
    )
    documents = sorted(data["documents"], key=lambda d: -int(d.get("precedence") or 0))
    return next(d["codes"][code] for d in documents if code in d["codes"])


class MoreThanOneMakeTests(Installed, TestCase):
    makes = ("Ford", "Lincoln", "Mercury", "Kenworth", "VW", "Mercedes-Benz",
             "Chevrolet", "GMC", "Cadillac",
             "Buick", "Pontiac", "Chrysler", "Dodge", "Jeep", "Ram", "Toyota",
             "Lexus", "Honda", "Acura", "Nissan", "Infiniti", "Audi", "BMW",
             "Mini", "Subaru", "Mazda", "Kia", "Hyundai", "Volvo")

    """Fifty-odd lists now, and they must not answer for each other."""

    def setUp(self):
        dtc._lists.cache_clear()

    def test_a_badge_with_its_own_list_reads_its_own_list(self):
        """Every make with a document of its own gets it. Aliasing them to
        a corporate sibling would hand a Cadillac Chevrolet's wording, and the
        pages disagree: they share 431 codes and word only 208 alike."""
        for make in ("Chevrolet", "GMC", "Cadillac", "Buick", "Pontiac",
                     "Chrysler", "Dodge", "Jeep", "Ram", "Toyota", "Lexus",
                     "Honda", "Acura", "Nissan", "Infiniti", "Audi", "BMW",
                     "Mini", "Subaru", "Mazda", "Kia", "Hyundai", "Volvo"):
            with self.subTest(make=make):
                self.assertEqual(dtc.code_list_for(make).make, make)

    def test_a_badge_without_one_reads_the_document_that_covers_it(self):
        """An alias is for a badge whose codes are in somebody else's document,
        not for one that merely shares an owner."""
        for make, expected in (
            ("Peterbilt", "Kenworth"), ("Volkswagen", "VW"),
            ("Mercedes", "Mercedes-Benz"),
        ):
            with self.subTest(make=make):
                self.assertEqual(dtc.code_list_for(make).make, expected)

    def test_a_badge_that_gains_its_own_document_stops_deferring(self):
        """Lincoln and Mercury read Ford's list for as long as that was the
        only one covering them. Now that each has its own — read from its own
        service manuals — that leads, and Ford's stays behind it for the codes
        it does not carry rather than being dropped."""
        for make in ("Lincoln", "Mercury"):
            with self.subTest(make=make):
                covering = dtc.code_lists_for(make)

                self.assertEqual(covering[0].make, make)
                self.assertIn("Ford", [c.make for c in covering[1:]])

    def test_a_makers_own_code_means_different_things_to_different_makers(self):
        """The whole reason lists are scoped, and the corpus states it outright:
        Ford's `B1352` is `Ignition Key-In Circuit Failure` and Toyota's is
        `Acoustic Vehicle Alerting Speaker 2 Circuit Open`. One number, two
        unrelated faults, and answering either from the other's document would
        send somebody to the wrong end of the car.

        This used to assert that Toyota had no answer at all, which was true
        only while no Toyota document had been published. The claim being made
        was never that the code is unanswerable — it is that Ford's answer is
        not evidence about anybody else's.
        """
        ford = dtc.explain("B1352", make="Ford")
        toyota = dtc.explain("B1352", make="Toyota")

        self.assertEqual(ford.source, dtc.MAKE)
        self.assertEqual(toyota.source, dtc.MAKE)
        self.assertNotEqual(ford.text, toyota.text)

    def test_a_makers_own_code_is_not_answered_for_a_make_without_a_list(self):
        """And where nothing covers the make, nothing is invented."""
        self.assertEqual(dtc.explain("B1352", make="Rivian").source, dtc.STRUCTURE)

    def test_each_list_answers_its_own_manufacturer_codes(self):
        """A manufacturer-controlled code is answered by its make's own list,
        in the words of the highest-ranked document published for that make.

        The wording is read from the catalog rather than written here. It used
        to be pinned — `P1516` was `Throttle command/actual throttle position
        signal variation` — and then a manufacturer's own service manuals were
        published above the compilation, which says `Throttle Actuator Control
        (TAC) Module Throttle Actuator Position Performance` for the same code.
        Both are that make's answer at the time they were published; a literal
        string here only asserts which document happened to be on top the day
        it was written, and fails every time a better one is added.
        """
        for code, make in (("P1516", "Chevrolet"), ("P1155", "Toyota"),
                           ("P1250", "Audi")):
            with self.subTest(code=code, make=make):
                found = dtc.explain(code, make=make)

                self.assertEqual(found.source, dtc.MAKE)
                self.assertEqual(found.text, published_text(code, make))



    def test_sibling_brands_keep_their_own_wording(self):
        """Chevrolet and Buick share modules and therefore codes, and each page
        describes them in its own words. Merging them would have to pick one."""
        chevrolet = dtc.code_list_for("Chevrolet").codes
        buick = dtc.code_list_for("Buick").codes
        shared = set(chevrolet) & set(buick)

        self.assertGreater(len(shared), 100)
        self.assertTrue(any(chevrolet[c] != buick[c] for c in shared))


class AGenericCodeIsGenericEverywhereTests(TestCase):
    """A published list answering a standard code, for any make at all.

    A generic code means the same thing on every vehicle ever built, so a
    manufacturer's wording for one is evidence about the *standard* rather than
    about that manufacturer. The hand-written table here is 144 codes; the
    bundled lists carry 797 more between them, and locking those to their own
    badge would tell a Toyota nothing about `P0351` because the list that
    happens to define it is Ford's.
    """

    def setUp(self):
        dtc._lists.cache_clear()

    def test_a_make_with_no_list_of_its_own_still_gets_an_answer(self):
        found = dtc.explain("P0351", make="Toyota")
        self.assertEqual(found.source, dtc.PUBLISHED)
        self.assertTrue(found.is_known)
        self.assertIn("gnition coil", found.text)

    def test_it_says_whose_wording_it_is(self):
        found = dtc.explain("P0351", make="Toyota")
        self.assertTrue(found.make)
        self.assertTrue(found.citation)

    def test_the_hand_written_standard_still_wins(self):
        found = dtc.explain("P0420", make="Toyota")
        self.assertEqual(found.source, dtc.STANDARD)
        self.assertTrue(found.is_authoritative)

    def test_it_never_reaches_a_manufacturer_code(self):
        """The layer exists because generic codes are shared. Applying it to
        `P1xxx` would be the exact mistake the make scoping prevents."""
        self.assertEqual(dtc.explain("P1516", make="Toyota").source, dtc.STRUCTURE)

    def test_the_answer_does_not_wobble_between_calls(self):
        """Several lists may define one generic code. Whichever answers, it has
        to be the same one every time or the page changes under the reader."""
        first = dtc.explain("P0101", make="Toyota")
        self.assertEqual([dtc.explain("P0101", make="Toyota").text for _ in range(4)],
                         [first.text] * 4)

    def test_it_is_ranked_above_what_the_tool_printed(self):
        found = dtc.explain("P0351", make="Toyota", reported="See service manual")
        self.assertEqual(found.source, dtc.PUBLISHED)


class TheStandardsOwnListsTests(TestCase):
    """The P, B, C and U sets, which belong to no manufacturer.

    A generic list is matched to no make at all. Keying it to a name would let
    it answer a manufacturer code, and the `B` and `U` pages carry a few
    hundred `B1xxx` and `U1xxx` with nothing to say whose they are — an
    unattributed manufacturer definition being the one thing this design
    refuses. Those are dropped at transcription.
    """

    def setUp(self):
        dtc._lists.cache_clear()

    def test_they_are_shipped(self):
        generic = [e for e in dtc._every_list() if e.is_iso_sae]
        self.assertTrue(generic)
        for entry in generic:
            with self.subTest(list=entry.make):
                self.assertTrue(entry.source)

    def test_they_hold_no_manufacturer_codes_at_all(self):
        for entry in dtc._every_list():
            if not entry.is_iso_sae:
                continue
            for code in entry.codes:
                with self.subTest(list=entry.make, code=code):
                    self.assertTrue(dtc.parse(code)["is_iso_sae"])

    def test_no_vehicle_is_ever_matched_to_one(self):
        for entry in dtc._every_list():
            if entry.is_iso_sae:
                with self.subTest(list=entry.make):
                    self.assertIsNone(dtc.code_list_for(entry.make))

    def test_they_do_not_appear_as_makes_a_list_is_held_for(self):
        for name in dtc.makes_with_lists():
            self.assertNotIn("OBD-II", name)

    def test_the_standards_own_list_answers_before_a_manufacturers(self):
        """Several documents define one generic code. A list that *is* the
        standard's list is the better authority for it than one
        manufacturer's restatement, so it is consulted first."""
        order = [e.is_iso_sae for e in dtc._every_list()]
        self.assertEqual(order, sorted(order, reverse=True))

    def test_generic_coverage_is_far_wider_than_the_hand_written_table(self):
        """The point of shipping them. The table in `dtc.py` is what somebody
        wrote out by hand the way the standard phrases it; these are the
        published sets."""
        answerable = set(dtc.ISO_SAE)
        for entry in dtc._every_list():
            answerable |= {c for c in entry.codes if dtc.parse(c)["is_iso_sae"]}
        self.assertGreater(len(answerable), 3000)


class TheRegisterOfWhatWasKeptOutTests(TestCase):
    """`codelists/_rejected.json` — documents examined and refused.

    The overlap check in `build_dtc_list` compares a new list against the ones
    *already held*, which makes it depend on build order. Rebuilt from empty in
    alphabetical order, `Citroen` is read before `Ford` and nothing catches
    that the page is Ford's list — which is exactly what happened during a
    clean rebuild here. A finding that survives only in somebody's memory of a
    terminal session is not a finding.
    """

    def register(self) -> dict:
        path = Path(dtc.codelists.__file__).parent / "_rejected.json"
        return json.loads(path.read_text(encoding="utf-8"))

    def test_every_entry_names_the_document_and_the_evidence(self):
        for entry in self.register()["documents"]:
            with self.subTest(file=entry["file"]):
                self.assertTrue(entry["file"])
                self.assertTrue(entry["claimed"])
                self.assertGreater(len(entry["evidence"]), 60, "a refusal needs a reason")

    def test_nothing_it_names_is_also_shipped(self):
        """The one check that would catch somebody re-adding a rejected list
        without reading why it was rejected."""
        rejected = {e["file"] for e in self.register()["documents"]}
        for path in shipped_lists():
            data = json.loads(path.read_text(encoding="utf-8"))
            with self.subTest(file=path.name):
                self.assertNotIn(data.get("source", ""), rejected)
        # And the published ones, which is where the fifty-odd makes the
        # register is actually about now live.
        for path in published_lists():
            data = json.loads(path.read_text(encoding="utf-8"))
            for document in data["documents"]:
                with self.subTest(file=path.name):
                    self.assertNotIn(document.get("source", ""), rejected)

    def test_the_register_is_not_loaded_as_a_code_list(self):
        for entry in dtc._every_list():
            self.assertNotIn("rejected", entry.make.lower())


class TwoDocumentsForOneMakeTests(Installed, TestCase):
    makes = ("Suzuki", "Toyota", "Lamborghini")

    """A make can be covered by more than one published document.

    Suzuki has both: a summary of the whole badge, and one vehicle's own
    service manual, which between them share 11 codes out of 155. Keying a
    make to a single list let the second silently replace the first — 144
    codes gone, and nothing anywhere would have reported it.
    """

    def setUp(self):
        dtc._lists.cache_clear()

    def test_a_make_can_be_covered_by_several(self):
        self.assertGreater(len(dtc.code_lists_for("Suzuki")), 1)

    def test_they_are_ordered_by_precedence(self):
        """A vehicle's own service manual outranks a third party's summary of
        the make, and nothing in the files works that out by itself."""
        found = dtc.code_lists_for("Suzuki")
        self.assertEqual([e.precedence for e in found], sorted(
            (e.precedence for e in found), reverse=True
        ))
        self.assertIn("Aerio", found[0].source)

    def test_a_code_only_the_second_document_has_is_still_answered(self):
        """The whole point. Neither document is complete on its own."""
        lists = dtc.code_lists_for("Suzuki")
        only_in_later = set(lists[-1].codes) - set(lists[0].codes)
        self.assertTrue(only_in_later)

        code = sorted(only_in_later)[0]
        self.assertEqual(dtc.explain(code, make="Suzuki").source, dtc.MAKE)

    def test_the_answer_names_the_document_it_came_from(self):
        """Two documents disagree about wording, so "Suzuki says" is not
        enough — the reader needs to know which Suzuki document."""
        found = dtc.explain("B1015", make="Suzuki")

        self.assertEqual(found.source, dtc.MAKE)
        self.assertIn("Aerio", found.citation)

    def test_a_manual_for_one_vehicle_is_recorded_as_being_exactly_that(self):
        """It is read for every Suzuki, which is a wider claim than the
        document makes. The citation is what keeps that honest, so it has to
        name the vehicle rather than just the make."""
        manual = dtc.code_lists_for("Suzuki")[0]

        self.assertIn("2004", manual.source)
        self.assertIn("Aerio", manual.source)

    def test_one_list_is_still_the_ordinary_case(self):
        """Drawn against Lamborghini rather than Toyota: the manual library
        covers Toyota, so it has a second document now, and a make the library
        never covered is what "one document" looks like from here on."""
        self.assertEqual(len(dtc.code_lists_for("Lamborghini")), 1)
        self.assertEqual(dtc.code_list_for("Lamborghini").make, "Lamborghini")


class TheTwoIsoSaeSourcesAgreeTests(TestCase):
    """The hand-written table against the published sets bundled beside it.

    There are two ISO/SAE sources in the image and they overlap. The table in
    `dtc.py` is written out the way the standard phrases it and is the
    **authoritative** layer — `is_authoritative` is true for it and the screen
    presents it as fact on every vehicle ever built. The four `obd-ii-*.json`
    files are a transcription of a published J2012 listing, carry 3,350 codes
    the table does not, and answer as `published` rather than as fact.

    So where both define a code, only the table is ever seen — which means a
    wrong row in it is invisible *and* outranks the correct answer sitting next
    to it. Two were found that way and both were real:

    * `P0148` and `P0168` were generated by the oxygen-sensor expansion running
      to a fourth sensor per bank. The blocks end at `P0147` and `P0167`; those
      two codes are *Fuel delivery error* and *Fuel temperature too high*.
    * `C0035`/`C0040`/`C0045`/`C0050` were GM's wheel-speed numbering. The
      published set puts those sensors at `C0031`/`C0034`/`C0037` and marks
      `C0050` ISO/SAE Reserved.

    This is the check that would have caught both on the day they were written.
    Wording differences are expected and fine — the table says *"Mass or volume
    air flow — circuit low"* where the transcription says *"Mass air flow (MAF)
    sensor/volume air flow (VAF) sensor low input"*, which is one fault in two
    voices. What must not happen is the two naming **different faults**.
    """

    #: Codes where the two sources describe the same fault in different words,
    #: reviewed once and allowed. Anything not on this list that disagrees is
    #: either new drift or a contradiction, and both want a person to look.
    DIFFERENT_WORDS = {
        "P0014", "P0102", "P0103", "P0107", "P0108", "P0122",
        "P0130", "P0131", "P0132", "P0135", "P0136", "P0137", "P0138",
        "P0141", "P0142", "P0150", "P0156", "P0162",
        "U0001", "U0100", "U0101", "U0121", "U0140", "U0155",
    }

    #: Below this the two are not saying the same thing in different words.
    #: Chosen from the reviewed set: the loosest genuine restatement here is
    #: `U0155` at 0.32, and the contradictions found sat at 0.08 to 0.26.
    SAME_FAULT = 0.30

    @staticmethod
    def _published() -> dict[str, str]:
        found: dict[str, str] = {}
        for path in shipped_lists():
            for code, text in json.loads(path.read_text(encoding="utf-8"))["codes"].items():
                found.setdefault(code, text)
        return found

    @staticmethod
    def _alike(one: str, other: str) -> float:
        from difflib import SequenceMatcher

        def bare(text):
            return "".join(c for c in text.lower() if c.isalnum())

        return SequenceMatcher(None, bare(one), bare(other)).ratio()

    def test_neither_source_names_a_different_fault_from_the_other(self):
        published = self._published()
        clashes = []
        for code, text in dtc.ISO_SAE.items():
            if code not in published:
                continue
            if self._alike(str(text), published[code]) >= self.SAME_FAULT:
                continue
            clashes.append(f"{code}: {text!r} vs published {published[code]!r}")

        self.assertEqual(
            clashes, [], "the authoritative table contradicts the published set"
        )

    def test_the_allowed_wording_differences_are_still_only_wording(self):
        """The allowlist is reviewed, not a way to stop looking. If one of
        these ever becomes a contradiction the test above catches it; this one
        catches the opposite — an entry kept on the list after the difference
        it was recording went away."""
        published = self._published()
        stale = [
            code
            for code in self.DIFFERENT_WORDS
            if code in published
            and self._alike(str(dtc.ISO_SAE.get(code, "")), published[code]) > 0.95
        ]
        self.assertEqual(stale, [], "allowed as different wording, but identical now")

    def test_the_published_sets_carry_most_of_the_answers(self):
        """The question this class started from: are those four files dead
        weight the image carries and never reads? They are not — they answer
        the overwhelming majority of ISO/SAE codes, and the hand-written table
        is the smaller, vetted core."""
        published = self._published()
        only_published = set(published) - set(dtc.ISO_SAE)

        self.assertGreater(len(only_published), 3000)
        self.assertLess(len(dtc.ISO_SAE), len(published) / 10)

    def test_the_oxygen_sensor_blocks_stop_where_the_standard_stops_them(self):
        """The expansion generated a fourth sensor per bank onto two real fuel
        codes. A generated row is exactly as much of a claim as a typed one."""
        for code in ("P0148", "P0168"):
            with self.subTest(code=code):
                self.assertNotIn(code, dtc.ISO_SAE)

    def test_no_chassis_code_is_asserted_as_the_standards(self):
        """C0xxx is where one manufacturer's chassis numbering is most likely
        to be mistaken for the standard's, and this table is the layer a caller
        may present as fact."""
        self.assertEqual([c for c in dtc.ISO_SAE if c.startswith("C")], [])
