"""
Diagnostics (SPEC §8.3, FR-INT-4..7).

The requirement these tests exist to hold down is FR-INT-4: **extraction never
auto-commits**. Every path into this module — a PDF, a CSV export, an ELM327
read, a hand-typed code — has to land in a draft that a person confirms, and a
draft has to be genuinely invisible until then. A misread VIN or odometer
poisons the vehicle record and every cost-per-distance figure computed from it,
and nothing afterwards would reveal it.
"""

from __future__ import annotations

import json
import pathlib

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse

from homeautoshop.accounts.models import User
from homeautoshop.assets.models import Asset
from homeautoshop.work.models import JobItem, WorkOrder

from . import dtc, engine, profiles as profilelib, services
from .models import (
    CodeDescription,
    CodeStatus,
    DiagnosticCode,
    DiagnosticSession,
    ParseStatus,
    ParserProfile,
    ReviewStatus,
    SessionSource,
)

CORPUS = pathlib.Path(__file__).resolve().parents[2] / "Artifacts" / "samples" / "scan-reports"

# The ISO 3779 worked example: a valid VIN belonging to nobody.
VIN = "1M8GDM9AXKP042788"

# Media goes to the filesystem for the suite regardless of how the instance is
# configured, so tests never reach for MinIO and never depend on it being up.
FILESYSTEM_STORAGE = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}


class CodeDictionaryTests(TestCase):
    """The two layers of §8.3c: derivable structure, and bundled wording."""

    def test_a_generic_code_has_a_standard_description(self):
        text, authoritative = dtc.describe("P0420")
        self.assertIn("Catalyst", text)
        self.assertTrue(authoritative)

    def test_a_numbered_family_is_generated_rather_than_typed(self):
        self.assertIn("Cylinder 7", str(dtc.GENERIC["P0307"]))
        self.assertIn("Injector", str(dtc.GENERIC["P0203"]))

    def test_an_unknown_manufacturer_code_still_says_something_true(self):
        text, authoritative = dtc.describe("P1516")
        self.assertFalse(authoritative)
        self.assertIn("Powertrain", text)
        self.assertIn("manufacturer-specific", text)

    def test_it_does_not_invent_a_fault_for_a_code_it_does_not_know(self):
        """The structural answer names a system, never a failure."""
        text, _authoritative = dtc.describe("P1516")
        self.assertNotIn("sensor", text.lower())
        self.assertNotIn("circuit", text.lower())

    def test_the_second_digit_decides_generic_or_not(self):
        self.assertTrue(dtc.parse("P0420")["is_generic"])
        self.assertFalse(dtc.parse("P1420")["is_generic"])

    def test_a_letter_that_is_not_a_system_is_not_a_code(self):
        self.assertIsNone(dtc.parse("Z0420"))
        self.assertIsNone(dtc.parse("Code"))
        self.assertIsNone(dtc.parse(""))

    def test_a_hex_body_is_accepted_because_p219a_is_real(self):
        self.assertEqual(dtc.parse("P219A")["code"], "P219A")

    def test_the_failure_type_byte_is_kept_out_of_the_identity(self):
        """`B1352` and `B1352-20` are the same fault; recurrence must see that."""
        self.assertEqual(dtc.normalize("B1352-20"), "B1352")
        self.assertEqual(dtc.parse("B1352-20")["failure_type"], "20")

    def test_an_operators_own_wording_is_scoped_to_the_make(self):
        CodeDescription.objects.create(make="Ford", code="P1516", description="TAC module")
        self.assertEqual(dtc.describe("P1516", make="Ford")[0], "TAC module")
        self.assertNotEqual(dtc.describe("P1516", make="Toyota")[0], "TAC module")

    def test_a_generic_code_is_never_shadowed_by_an_operator_note(self):
        """The standard wins. A local note on P0420 would be a private redefinition
        of a code that means the same thing on every vehicle ever built."""
        CodeDescription.objects.create(make="Ford", code="P0420", description="whatever")
        text, authoritative = dtc.describe("P0420", make="Ford")
        self.assertTrue(authoritative)
        self.assertIn("Catalyst", text)


class DeclarativeProfileTests(TestCase):
    """A profile is data — this is the proof that it is, not just the claim."""

    def setUp(self):
        profilelib.seed()
        self.profile = ParserProfile.objects.get(name__startswith="Generic code list")

    def _document(self, text: str) -> engine.Document:
        return engine.Document(text=text, media_type="text")

    def test_it_reads_codes_out_of_plain_text_with_no_code_involved(self):
        document = self._document(
            "Trouble Code\nP0420 Catalyst below threshold\nP0301 Cylinder 1 misfire\n"
        )
        extraction = engine.apply(self.profile, document)
        self.assertEqual([c["code"] for c in extraction.codes], ["P0420", "P0301"])

    def test_a_label_anchored_field_finds_its_value(self):
        document = self._document(f"Vehicle Information\nVIN: {VIN}\nMileage: 84,120\n")
        extraction = engine.apply(self.profile, document)
        self.assertEqual(extraction.value("vin"), VIN)
        self.assertEqual(extraction.value("odometer"), "84120")

    def test_a_valid_check_digit_raises_confidence_and_a_bad_one_lowers_it(self):
        good = engine.apply(self.profile, self._document(f"VIN: {VIN}\nDTC\n"))
        bad = engine.apply(self.profile, self._document("VIN: 1M8GDM9A1KP042788\nDTC\n"))
        self.assertGreater(good.fields["vin"].confidence, bad.fields["vin"].confidence)

    def test_a_failed_validator_lowers_confidence_rather_than_dropping_the_value(self):
        """Hiding a VIN the tool got wrong hides the very thing worth seeing."""
        extraction = engine.apply(self.profile, self._document("VIN: 1M8GDM9A1KP042788\nDTC\n"))
        self.assertEqual(extraction.value("vin"), "1M8GDM9A1KP042788")

    def test_a_no_codes_line_is_not_read_as_a_code(self):
        document = self._document("Trouble Code\nNo fault codes found\n")
        self.assertEqual(engine.apply(self.profile, document).codes, [])

    def test_the_stop_marker_keeps_live_data_out_of_the_code_table(self):
        document = self._document(
            "Trouble Code\nP0420 Catalyst\nLive Data\nP0101 this is a heading not a code\n"
        )
        codes = [c["code"] for c in engine.apply(self.profile, document).codes]
        self.assertEqual(codes, ["P0420"])

    def test_fingerprinting_scores_over_the_whole_document(self):
        """The D8's strongest signal is not on page one of four reports in nine."""
        document = self._document("page one\n" * 50 + "P0420 Trouble Code")
        self.assertGreater(engine.score(self.profile, document), 0.5)

    def test_a_profile_for_another_media_type_is_never_chosen(self):
        document = engine.Document(text="P0420 Trouble Code", media_type="pdf")
        chosen, _score = engine.detect(ParserProfile.objects.all(), document)
        self.assertIsNone(chosen)

    def test_a_switched_off_profile_is_never_chosen(self):
        self.profile.is_active = False
        self.profile.save()
        document = self._document("P0420 Trouble Code")
        chosen, _score = engine.detect(ParserProfile.objects.all(), document)
        self.assertIsNone(chosen)


class ProfileYamlTests(TestCase):
    """FR-INT-7 — profiles import, export, and version as YAML."""

    def test_a_profile_survives_a_round_trip(self):
        profilelib.seed()
        original = ParserProfile.objects.get(name__startswith="Generic code list")
        clone = profilelib.from_yaml(profilelib.to_yaml(original))
        self.assertEqual(clone.field_extractors, original.field_extractors)
        self.assertEqual(clone.table_extractor, original.table_extractor)

    def test_a_broken_regex_is_refused_at_import_not_at_first_upload(self):
        with self.assertRaises(profilelib.ProfileInvalid) as caught:
            profilelib.from_yaml(
                "name: Bad\nmedia_type: text\n"
                "field_extractors:\n  vin:\n    pattern: '([unclosed'\n"
            )
        self.assertIn("field_extractors.vin", str(caught.exception))

    def test_an_unrecognized_key_is_refused_rather_than_ignored(self):
        """A typo'd key would otherwise import cleanly and extract nothing."""
        with self.assertRaises(profilelib.ProfileInvalid) as caught:
            profilelib.from_yaml("name: Typo\nfield_extractor: {}\n")
        self.assertIn("field_extractor", str(caught.exception))

    def test_a_media_type_this_build_cannot_read_is_refused(self):
        with self.assertRaises(profilelib.ProfileInvalid):
            profilelib.from_yaml("name: Ledger\nmedia_type: xlsx\n")

    def test_yaml_that_is_not_a_mapping_is_refused(self):
        with self.assertRaises(profilelib.ProfileInvalid):
            profilelib.from_yaml("- just\n- a\n- list\n")

    def test_seeding_twice_installs_nothing_the_second_time(self):
        first = profilelib.seed()
        self.assertGreater(first, 0)
        self.assertEqual(profilelib.seed(), 0)

    def test_seeding_never_overwrites_an_edited_profile(self):
        profilelib.seed()
        profile = ParserProfile.objects.get(name__startswith="Generic code list")
        profile.notes = "mine now"
        profile.save()
        profilelib.seed()
        profile.refresh_from_db()
        self.assertEqual(profile.notes, "mine now")


@override_settings(STORAGES=FILESYSTEM_STORAGE)
class ImportFlowTests(TestCase):
    """FR-INT-4 — nothing reaches vehicle history without a person."""

    def setUp(self):
        profilelib.seed()
        self.user = User.objects.create_user(username="andy", password="x" * 16)
        self.client.force_login(self.user)
        self.asset = Asset.objects.create(nickname="Work truck", vin=VIN, make="Ford")

    def _upload(self, body: str, name: str = "codes.csv"):
        return SimpleUploadedFile(name, body.encode("utf-8"), content_type="text/csv")

    def _import(self, body: str = "Trouble Code\nP0420 Catalyst below threshold\n"):
        return self.client.post(
            reverse("session_import", args=[self.asset.pk]),
            {"report": self._upload(body, "report.txt")},
            follow=True,
        )

    def test_an_imported_report_lands_as_a_draft(self):
        self._import()
        session = DiagnosticSession.objects.get()
        self.assertEqual(session.review_status, ReviewStatus.DRAFT)
        self.assertEqual(session.parse_status, ParseStatus.PARSED)

    def test_a_draft_is_not_in_the_vehicles_history(self):
        self._import()
        page = self.client.get(reverse("asset_diagnostics", args=[self.asset.pk]))
        self.assertEqual(list(page.context["sessions"]), [])
        self.assertEqual(len(page.context["drafts"]), 1)

    def test_a_drafts_codes_are_not_in_the_open_list_either(self):
        """Half-admitting a draft would be worse than not queueing it at all."""
        self._import()
        page = self.client.get(reverse("asset_diagnostics", args=[self.asset.pk]))
        self.assertEqual(list(page.context["open_codes"]), [])

    def test_confirming_admits_it(self):
        self._import()
        session = DiagnosticSession.objects.get()
        self.client.post(
            reverse("session_confirm", args=[session.pk]),
            {"performed_on": session.performed_on.isoformat(), "tool": "XTOOL"},
        )
        session.refresh_from_db()
        self.assertEqual(session.review_status, ReviewStatus.CONFIRMED)
        self.assertEqual(session.reviewed_by, self.user)

    def test_the_raw_report_is_kept_so_it_can_be_read_again(self):
        self._import()
        session = DiagnosticSession.objects.get()
        self.assertIsNotNone(session.raw_media)
        self.assertIn("P0420", session.extracted_text)

    def test_a_report_for_another_vehicle_is_refused(self):
        response = self._import(f"Trouble Code\nVIN: {VIN}\nP0420 Catalyst\n")
        self.assertEqual(DiagnosticSession.objects.count(), 1)

        other = Asset.objects.create(nickname="Another truck", vin="1FTFW1ET5DFC10312")
        response = self.client.post(
            reverse("session_import", args=[other.pk]),
            {"report": self._upload(f"Trouble Code\nVIN: {VIN}\nP0420 Catalyst\n", "r.txt")},
            follow=True,
        )
        self.assertContains(response, "not this vehicle")
        self.assertEqual(other.diagnostic_sessions.count(), 0)

    def test_the_refusal_does_not_leak_the_whole_vin(self):
        other = Asset.objects.create(nickname="Another truck", vin="1FTFW1ET5DFC10312")
        response = self.client.post(
            reverse("session_import", args=[other.pk]),
            {"report": self._upload(f"Trouble Code\nVIN: {VIN}\nP0420\n", "r.txt")},
            follow=True,
        )
        self.assertNotContains(response, VIN)

    def test_a_file_that_is_not_a_report_is_reported_not_raised(self):
        response = self.client.post(
            reverse("session_import", args=[self.asset.pk]),
            {"report": self._upload("shopping list\nmilk\n", "notes.txt")},
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Map the values by hand")

    def test_no_file_is_a_message_not_a_crash(self):
        response = self.client.post(
            reverse("session_import", args=[self.asset.pk]), {}, follow=True
        )
        self.assertContains(response, "Choose a scan-tool report")

    def test_discarding_a_draft_leaves_nothing_behind(self):
        self._import()
        session = DiagnosticSession.objects.get()
        self.client.post(reverse("session_discard", args=[session.pk]))
        self.assertEqual(DiagnosticSession.objects.count(), 0)
        self.assertEqual(DiagnosticSession.all_objects.count(), 1)


@override_settings(STORAGES=FILESYSTEM_STORAGE)
class ReparseTests(TestCase):
    """FR-INT-5 — a better profile re-reads history without a re-upload."""

    def setUp(self):
        profilelib.seed()
        self.user = User.objects.create_user(username="andy", password="x" * 16)
        self.client.force_login(self.user)
        self.asset = Asset.objects.create(nickname="Work truck", vin=VIN, make="Ford")
        self.session = services.session_from_upload(
            self.asset,
            SimpleUploadedFile(
                "r.txt", b"Trouble Code\nP0420 Catalyst below threshold\n", "text/plain"
            ),
            user=self.user,
        )

    def test_it_works_from_the_stored_text_with_no_file_present(self):
        self.session.raw_media = None
        self.session.save()
        services.reparse(self.session)
        self.assertEqual(self.session.codes.count(), 1)

    def test_re_reading_a_confirmed_scan_makes_a_new_draft(self):
        """The original stays exactly as the person who vouched for it saw it."""
        services.confirm(self.session, user=self.user)
        self.client.post(reverse("session_reparse", args=[self.session.pk]))

        self.assertEqual(DiagnosticSession.objects.count(), 2)
        self.session.refresh_from_db()
        self.assertEqual(self.session.review_status, ReviewStatus.CONFIRMED)
        new = DiagnosticSession.objects.exclude(pk=self.session.pk).get()
        self.assertEqual(new.review_status, ReviewStatus.DRAFT)

    def test_a_confirmed_sessions_codes_are_immutable(self):
        services.confirm(self.session, user=self.user)
        with self.assertRaises(ValueError):
            services._replace_codes(self.session, [{"code": "P0300"}])

    def test_re_reading_a_draft_rewrites_it_in_place(self):
        self.client.post(reverse("session_reparse", args=[self.session.pk]))
        self.assertEqual(DiagnosticSession.objects.count(), 1)

    def test_the_session_records_which_profile_read_it(self):
        self.assertIsNotNone(self.session.parser_profile)
        self.assertEqual(self.session.parser_version, self.session.parser_profile.version)


@override_settings(STORAGES=FILESYSTEM_STORAGE)
class MappingWizardTests(TestCase):
    """FR-INT-6 — an unmatched report is still usable, and can teach a profile."""

    def setUp(self):
        self.user = User.objects.create_user(username="andy", password="x" * 16)
        self.client.force_login(self.user)
        self.asset = Asset.objects.create(nickname="Work truck", vin=VIN, make="Ford")
        # No profiles seeded: this is deliberately the before-any-profile case.
        self.session = services.session_from_upload(
            self.asset,
            SimpleUploadedFile(
                "export.csv",
                b"Fault,Meaning,Kind\nP0420,Catalyst,Stored\nP0301,Misfire,Pending\n",
                "text/csv",
            ),
            user=self.user,
        )

    def test_an_unmatched_report_still_produces_a_session(self):
        self.assertEqual(self.session.parse_status, ParseStatus.UNMATCHED)
        self.assertEqual(self.session.review_status, ReviewStatus.DRAFT)

    def test_the_wizard_guesses_the_obvious_columns(self):
        page = self.client.get(reverse("session_map", args=[self.session.pk]))
        by_role = {entry["role"]: entry for entry in page.context["roles"]}
        chosen = [c["name"] for c in by_role["code"]["columns"] if c["selected"]]
        self.assertEqual(chosen, ["Fault"])

    def test_mapping_by_hand_produces_codes(self):
        self.client.post(
            reverse("session_map", args=[self.session.pk]),
            {"map_code": "Fault", "map_description": "Meaning", "map_state": "Kind"},
        )
        self.session.refresh_from_db()
        self.assertEqual(self.session.parse_status, ParseStatus.PARSED)
        self.assertEqual(
            sorted(self.session.codes.values_list("code", flat=True)), ["P0301", "P0420"]
        )

    def test_the_state_column_is_read_rather_than_assumed(self):
        self.client.post(
            reverse("session_map", args=[self.session.pk]),
            {"map_code": "Fault", "map_state": "Kind"},
        )
        self.assertEqual(self.session.codes.get(code="P0301").state, "pending")

    def test_mapping_the_wrong_column_is_refused_with_a_reason(self):
        response = self.client.post(
            reverse("session_map", args=[self.session.pk]), {"map_code": "Meaning"}, follow=True
        )
        self.assertContains(response, "looks like a trouble code")
        self.assertEqual(self.session.codes.count(), 0)

    def test_the_mapping_can_be_saved_as_a_profile(self):
        self.client.post(
            reverse("session_map", args=[self.session.pk]),
            {
                "map_code": "Fault",
                "map_description": "Meaning",
                "save_profile": "1",
                "profile_name": "Autel MK808 export",
            },
        )
        profile = ParserProfile.objects.get(name="Autel MK808 export")
        self.assertEqual(profile.source, "user")
        self.assertTrue(profile.fingerprint["signals"])

    def test_a_saved_profile_reads_the_next_one_of_the_same_shape(self):
        """Learn-from-example is only worth anything if the result actually matches."""
        self.client.post(
            reverse("session_map", args=[self.session.pk]),
            {"map_code": "Fault", "save_profile": "1", "profile_name": "Autel MK808 export"},
        )
        again = services.session_from_upload(
            self.asset,
            SimpleUploadedFile(
                "second.csv", b"Fault,Meaning,Kind\nP0171,Lean,Stored\n", "text/csv"
            ),
            user=self.user,
        )
        self.assertEqual(again.parse_status, ParseStatus.PARSED)
        self.assertEqual(again.codes.get().code, "P0171")

    def test_typed_codes_work_when_the_file_is_not_a_table(self):
        session = services.session_from_upload(
            self.asset,
            SimpleUploadedFile("scan.txt", b"a photo of a screen, basically", "text/plain"),
            user=self.user,
        )
        self.client.post(
            reverse("session_map", args=[session.pk]),
            {"codes": "P0420 Catalyst\nnot a code at all\nP0301"},
        )
        self.assertEqual(sorted(session.codes.values_list("code", flat=True)), ["P0301", "P0420"])


@override_settings(STORAGES=FILESYSTEM_STORAGE)
class CodeWorkflowTests(TestCase):
    """Code → work order → addressed → recurring (§8.3c)."""

    def setUp(self):
        profilelib.seed()
        self.user = User.objects.create_user(username="andy", password="x" * 16)
        self.client.force_login(self.user)
        self.asset = Asset.objects.create(nickname="Work truck", vin=VIN, make="Ford")

    def _scan(self, codes: str = "P0420 Catalyst", confirm: bool = True) -> DiagnosticSession:
        session = services.session_from_codes(
            self.asset,
            [{"code": part.split()[0]} for part in codes.split(",")],
            user=self.user,
        )
        if confirm:
            services.confirm(session, user=self.user)
        return session

    def test_a_code_becomes_a_work_order_with_the_car_as_the_complaint(self):
        code = self._scan().codes.get()
        self.client.post(reverse("code_promote", args=[code.pk]))

        order = WorkOrder.objects.get()
        self.assertIn("P0420", order.title)
        self.assertIn("P0420", order.complaint)
        self.assertEqual(order.asset, self.asset)

    def test_promoting_links_the_code_to_the_job_item(self):
        code = self._scan().codes.get()
        self.client.post(reverse("code_promote", args=[code.pk]))
        code.refresh_from_db()
        self.assertIsNotNone(code.resolved_by_job_item)

    def test_a_draft_code_cannot_become_work(self):
        """A draft is not history, so it is not something to act on either."""
        code = self._scan(confirm=False).codes.get()
        response = self.client.post(reverse("code_promote", args=[code.pk]), follow=True)
        self.assertContains(response, "Confirm the scan first")
        self.assertEqual(WorkOrder.objects.count(), 0)

    def test_completing_the_job_item_marks_the_code_addressed(self):
        code = self._scan().codes.get()
        self.client.post(reverse("code_promote", args=[code.pk]))
        code.refresh_from_db()

        item = code.resolved_by_job_item
        item.status = JobItem.Status.DONE
        item.save()

        code.refresh_from_db()
        self.assertEqual(code.status, CodeStatus.ADDRESSED)

    def test_a_code_that_comes_back_after_being_fixed_is_flagged(self):
        first = self._scan()
        first.codes.update(status=CodeStatus.ADDRESSED)

        later = services.session_from_codes(self.asset, [{"code": "P0420"}], user=self.user)
        recurring = services.confirm(later, user=self.user)

        self.assertEqual(recurring, 1)
        self.assertEqual(later.codes.get().status, CodeStatus.RECURRING)

    def test_a_code_that_was_ignored_is_not_a_recurrence(self):
        """Living with a code is not a fix that failed."""
        first = self._scan()
        first.codes.update(status=CodeStatus.IGNORED)

        later = services.session_from_codes(self.asset, [{"code": "P0420"}], user=self.user)
        self.assertEqual(services.confirm(later, user=self.user), 0)

    def test_another_vehicles_history_does_not_flag_this_one(self):
        other = Asset.objects.create(nickname="Van", vin="1FTFW1ET5DFC10312")
        theirs = services.session_from_codes(other, [{"code": "P0420"}], user=self.user)
        services.confirm(theirs, user=self.user)
        theirs.codes.update(status=CodeStatus.ADDRESSED)

        mine = services.session_from_codes(self.asset, [{"code": "P0420"}], user=self.user)
        self.assertEqual(services.confirm(mine, user=self.user), 0)

    def test_a_reading_cannot_be_edited_into_being_right(self):
        code = self._scan().codes.get()
        code.code = "P0300"
        with self.assertRaises(Exception):
            code.save(update_fields=["code"])

    def test_an_operators_wording_spreads_to_every_vehicle_of_that_make(self):
        session = services.session_from_codes(self.asset, [{"code": "P1516"}], user=self.user)
        services.confirm(session, user=self.user)
        code = session.codes.get()

        sibling = Asset.objects.create(nickname="Other Ford", make="Ford")
        theirs = services.session_from_codes(sibling, [{"code": "P1516"}], user=self.user)

        self.client.post(
            reverse("code_describe", args=[code.pk]), {"description": "Throttle actuator"}
        )
        self.assertEqual(theirs.codes.get().description, "Throttle actuator")


@override_settings(STORAGES=FILESYSTEM_STORAGE)
class Elm327Tests(TestCase):
    """§8.3c — the browser reads the car, and the result is still a draft."""

    def setUp(self):
        self.user = User.objects.create_user(username="andy", password="x" * 16)
        self.client.force_login(self.user)
        self.asset = Asset.objects.create(nickname="Work truck", vin=VIN, make="Ford")

    def test_the_page_states_its_requirements_rather_than_hiding_a_button(self):
        page = self.client.get(reverse("elm327", args=[self.asset.pk]))
        self.assertContains(page, "Chromium")
        self.assertContains(page, "HTTPS")

    def test_a_read_lands_as_a_draft_like_everything_else(self):
        response = self.client.post(
            reverse("elm_capture", args=[self.asset.pk]),
            json.dumps({"adapter": "ELM327", "codes": [{"code": "P0420", "state": "stored"}]}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        session = DiagnosticSession.objects.get()
        self.assertEqual(session.source, SessionSource.ELM327)
        self.assertEqual(session.review_status, ReviewStatus.DRAFT)
        self.assertEqual(session.codes.get().code, "P0420")

    def test_rubbish_from_the_adapter_is_dropped_not_stored(self):
        self.client.post(
            reverse("elm_capture", args=[self.asset.pk]),
            json.dumps({"codes": [{"code": "ZZZZZ"}, {"code": "P0301"}]}),
            content_type="application/json",
        )
        self.assertEqual(
            list(DiagnosticSession.objects.get().codes.values_list("code", flat=True)), ["P0301"]
        )

    def test_a_malformed_body_is_a_400_not_a_500(self):
        response = self.client.post(
            reverse("elm_capture", args=[self.asset.pk]),
            "not json",
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)


class XtoolProfileTests(TestCase):
    """The built-in parser reached through the profile machinery.

    Skipped where the corpus is absent — the captured word geometry is
    committed, the reports themselves are not (see the corpus README).
    """

    def setUp(self):
        profilelib.seed()

    def _capture(self):
        from homeautoshop.scantools import fixtures

        samples = fixtures.samples()
        if not samples:
            self.skipTest("the captured corpus is not in this checkout")
        return fixtures.pages(samples[0])

    def test_the_d8_profile_names_a_built_in_parser(self):
        profile = ParserProfile.objects.get(tool_model="D8")
        self.assertEqual(profile.engine, "xtool_d8")
        self.assertIn(profile.engine, engine.BUILTINS)

    def test_a_profile_naming_a_parser_this_build_lacks_warns_rather_than_raises(self):
        profile = ParserProfile.objects.get(tool_model="D8")
        profile.engine = "some_tool_from_the_future"
        extraction = engine.apply(profile, engine.Document(text="", media_type="pdf"))
        self.assertTrue(extraction.warnings)

    def test_it_reads_a_real_captured_report(self):
        pages = self._capture()
        profile = ParserProfile.objects.get(tool_model="D8")
        extraction = engine.apply(
            profile, engine.Document(pages=pages, media_type="pdf")
        )
        self.assertEqual(extraction.value("tool_vendor"), "XTOOL")
        self.assertEqual(extraction.value("tool_model"), "D8")

    def test_the_fingerprint_recognizes_a_real_report(self):
        pages = self._capture()
        text = "\n".join(" ".join(str(w.get("text", "")) for w in page) for page in pages)
        document = engine.Document(text=text, pages=pages, media_type="pdf")
        chosen, value = engine.detect(ParserProfile.objects.all(), document)
        self.assertIsNotNone(chosen)
        self.assertEqual(chosen.tool_model, "D8")
        self.assertGreaterEqual(value, 0.5)
