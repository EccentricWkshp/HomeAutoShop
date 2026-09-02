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
from pathlib import Path

from django.test import TestCase
from django.urls import reverse

from homeautoshop.accounts.models import AssetAccess, Role, User
from homeautoshop.assets.models import Asset

from . import dtc, services
from .models import CodeDescription, DiagnosticCode, DiagnosticSession, ReviewStatus

# Real Ford codes, from the list this ships. `B1352` is the one on the mower
# screenshot that started this.
FORD_CODE = "B1352"
FORD_MEANING = "Ignition Key-In Circuit Failure"


class TheBundledListTests(TestCase):
    """The file itself, before anything looks anything up in it."""

    def setUp(self):
        dtc._lists.cache_clear()

    def test_a_list_is_shipped_for_ford(self):
        self.assertIn("Ford", dtc.makes_with_lists())

    def test_every_code_in_every_shipped_list_is_code_shaped(self):
        """The transcription reads a PDF. A dropped character turns a real
        definition into a lookup against a code that cannot exist — and it
        would sit there being wrong rather than failing."""
        for path in sorted(Path(dtc.codelists.__file__).parent.glob("*.json")):
            data = json.loads(path.read_text(encoding="utf-8"))
            for code in data["codes"]:
                with self.subTest(file=path.name, code=code):
                    self.assertIsNotNone(dtc.parse(code))
                    self.assertEqual(dtc.parse(code)["code"], code)

    def test_every_shipped_list_says_where_it_came_from(self):
        """A transcription of somebody's document that does not name the
        document cannot be checked against it."""
        for path in sorted(Path(dtc.codelists.__file__).parent.glob("*.json")):
            data = json.loads(path.read_text(encoding="utf-8"))
            with self.subTest(file=path.name):
                self.assertTrue(data.get("make"))
                self.assertTrue(data.get("source"))

    def test_no_description_is_empty(self):
        for path in sorted(Path(dtc.codelists.__file__).parent.glob("*.json")):
            data = json.loads(path.read_text(encoding="utf-8"))
            for code, text in data["codes"].items():
                with self.subTest(file=path.name, code=code):
                    self.assertTrue(text.strip())


class WhichLayerAnswersTests(TestCase):
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
            is_generic=dtc.parse(code)["is_generic"],
        )


class TheCodePageTests(CodeCase):
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


class DefiningACodeTests(CodeCase):
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


class TheOpenCodesTableTests(CodeCase):
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

        self.assertIn("Autolamp On Circuit Short To Battery", page)

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
