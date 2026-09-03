"""
Reviewing a bench tester's readings (SPEC §8.3a, §7.9, FR-INT-4).

The parser itself is tested in `scantools/tests_topdon.py`, against real OCR of
real paper. This is the half that only exists once there is a database and a
person: that a photographed printout becomes a session, that the review screen
shows each receipt whole, that an operator can correct a misread voltage — and
that correcting it does not touch what the machine read.

That last one is the point of the whole shape. `extraction` answers "what did
the tool actually say?" and stops being able to the moment an edit overwrites
it, which is why the corrected copy lives in its own column.
"""

from __future__ import annotations

import shutil
import tempfile
from io import BytesIO
from pathlib import Path
from unittest import mock

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone, translation

from homeautoshop.accounts.models import Role, User
from homeautoshop.assets.models import Asset
from homeautoshop.diagnostics import engine, profiles, services
from homeautoshop.diagnostics.models import DiagnosticSession, ReviewStatus, SessionSource
from homeautoshop.scantools import fixtures

STATICFILES = {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"}

CAPTURE = "20260830_105647.words.json"
BATTERY_CAPTURE = "20260830_105614.words.json"


def a_photo(name: str = "slip.jpg") -> SimpleUploadedFile:
    from PIL import Image

    buffer = BytesIO()
    Image.new("RGB", (40, 30), (255, 255, 255)).save(buffer, format="JPEG")
    return SimpleUploadedFile(name, buffer.getvalue(), content_type="image/jpeg")


def captured(name: str = CAPTURE) -> tuple[list, list]:
    """A real capture from the corpus: its words and the frame they are in."""
    path = fixtures.find(name)
    return fixtures.pages(path), fixtures.image_size(path)


class Base(TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.storage = override_settings(
            MEDIA_ROOT=self.tmp,
            STORAGES={
                "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
                "staticfiles": STATICFILES,
            },
        )
        self.storage.enable()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.addCleanup(self.storage.disable)

        self.user = User.objects.create_user(
            username="andy", password="x" * 16, role=Role.ADMIN
        )
        self.client.force_login(self.user)
        self.asset = Asset.objects.create(nickname="Red truck")
        profiles.seed()

    def upload(self, name: str = CAPTURE) -> DiagnosticSession:
        pages, size = captured(name)
        with (
            mock.patch(
                "homeautoshop.mediafiles.services.read_image_words", return_value=pages
            ),
            mock.patch(
                "homeautoshop.mediafiles.services.image_size", return_value=tuple(size)
            ),
        ):
            self.client.post(
                reverse("session_import", args=[self.asset.pk]),
                {"report": a_photo()},
                follow=True,
            )
        # The newest, not "the one" — several of these upload twice.
        return DiagnosticSession.objects.filter(asset=self.asset).latest("created_at")


@override_settings(LANGUAGE_CODE="en-us")
class ImportingABenchTesterPhotoTests(Base):
    def test_a_photographed_printout_becomes_a_session_with_its_results(self):
        session = self.upload()

        self.assertEqual(session.source, SessionSource.PHOTO)
        self.assertEqual(
            [r["kind"] for r in session.test_results], ["cranking", "charging"]
        )

    def test_the_profile_that_read_it_is_recorded(self):
        session = self.upload()

        self.assertIsNotNone(session.parser_profile)
        self.assertEqual(session.parser_profile.engine, "topdon_bt600_plus")

    def test_the_words_are_kept_so_it_can_be_read_again(self):
        """A photograph's geometry used to be recognized and thrown away."""
        session = self.upload()

        self.assertTrue(session.extracted_words)
        self.assertIn("conf", session.extracted_words[0][0])

    def test_the_session_is_dated_by_the_last_test_on_the_strip(self):
        """Print order is not time order: the cranking test is printed above a
        charging test taken forty-two seconds earlier."""
        session = self.upload()

        self.assertEqual(
            timezone.localtime(session.performed_on).strftime("%H:%M:%S"), "14:55:18"
        )

    def test_it_is_a_draft_and_nothing_is_in_the_history(self):
        session = self.upload()

        self.assertEqual(session.review_status, ReviewStatus.DRAFT)

    def test_the_tester_is_named_rather_than_the_vehicle(self):
        session = self.upload()

        self.assertEqual(session.tool, "TOPDON")
        self.assertEqual(session.tool_model, "BT600 Plus")


@override_settings(LANGUAGE_CODE="en-us")
class TheReviewScreenTests(Base):
    def setUp(self):
        super().setUp()
        self.session = self.upload()
        self.url = reverse("session_detail", args=[self.session.pk])

    def test_each_receipt_gets_its_own_card(self):
        page = self.client.get(self.url).content.decode()

        self.assertIn("Cranking test", page)
        self.assertIn("Charging test", page)

    def test_both_voltages_are_shown_and_neither_replaces_the_other(self):
        page = self.client.get(self.url).content.decode()

        for value in ("8.85", "14.69", "14.58"):
            self.assertIn(value, page)

    def test_the_unit_is_beside_the_box_and_not_inside_it(self):
        """A field holding `12.62 V` is a field somebody types `12.6V` into."""
        page = self.client.get(self.url).content.decode()

        self.assertIn('value="8.85"', page)
        self.assertIn('<span class="unit">V</span>', page)

    def test_a_doubtful_reading_is_marked_for_a_second_look(self):
        page = self.client.get(self.url).content.decode()

        self.assertIn("hard to read", page)

    def test_the_crop_of_the_paper_is_offered_beside_each_value(self):
        page = self.client.get(self.url).content.decode()

        self.assertIn('class="crop"', page)
        self.assertIn("aspect-ratio:", page)

    def test_the_crop_measurements_are_not_localised(self):
        """A stylesheet does not read French. `33,33%` is not a length."""
        self.addCleanup(translation.deactivate)
        page = self.client.get(
            self.url, headers={"accept-language": "fr-ca"}
        ).content.decode()

        for line in page.splitlines():
            if "transform: translate(" in line:
                self.assertNotIn(",", line.split("transform: translate(")[1].split(")")[0].replace(", ", ""))

    def test_a_printed_label_is_not_repeated_when_it_says_the_same_thing(self):
        """`Charge CHARGE` is a word said twice. `Internal resistance
        INTERNAL R` tells the reader which row on the paper to look at."""
        session = self.upload(BATTERY_CAPTURE)
        page = self.client.get(
            reverse("session_detail", args=[session.pk])
        ).content.decode()

        self.assertNotIn("Charge <span class=\"small muted mono\"> CHARGE", page)
        self.assertIn("INTERNAL R", page)

    def test_under_a_translated_locale_the_printed_label_is_kept(self):
        """The paper is in English whatever the shop speaks, which is exactly
        when knowing what it says is most useful.

        `HEALTH` rather than `CHARGE`: the French for charge is charge, so that
        one is still a word said twice and is still hidden.
        """
        self.addCleanup(translation.deactivate)
        session = self.upload(BATTERY_CAPTURE)
        page = self.client.get(
            reverse("session_detail", args=[session.pk]),
            headers={"accept-language": "fr-ca"},
        ).content.decode()

        self.assertIn("Santé", page)
        self.assertIn("HEALTH", page)

    def test_what_the_tester_was_told_is_shown_apart_from_what_it_measured(self):
        session = self.upload(BATTERY_CAPTURE)
        page = self.client.get(
            reverse("session_detail", args=[session.pk])
        ).content.decode()

        self.assertIn("Set on the tester", page)
        self.assertIn(
            "typed into the tester before the test",
            page,
        )

    def test_confidence_is_about_the_photograph_not_about_the_reading(self):
        """A doubt beside a rated capacity was read as doubt about the
        battery's rating. It is doubt about the picture."""
        page = self.client.get(self.url).content.decode()

        self.assertIn("hard to read", page)
        self.assertNotIn("low — check it", page)

    def test_the_crop_is_offset_by_a_transform_not_by_the_container(self):
        """`inset-*` percentages resolve against a span in a table cell, whose
        width table layout decides — so every crop drifted upward in proportion
        to how far down the receipt it was."""
        page = self.client.get(self.url).content.decode()

        self.assertIn("transform: translate(", page)

    def test_a_bench_tester_is_not_asked_about_trouble_codes(self):
        """It cannot have any, so "No codes in this scan." is furniture."""
        page = self.client.get(self.url).content.decode()

        self.assertNotIn("No codes in this scan.", page)

    def test_but_a_scan_tool_that_found_none_still_says_so(self):
        """There, finding nothing *is* the answer — and it is the profile that
        says which kind of tool this is, rather than the empty list."""
        self.session.parser_profile.reports = ["codes"]
        self.session.parser_profile.save(update_fields=["reports"])
        page = self.client.get(self.url).content.decode()

        self.assertIn("No codes in this scan.", page)

    def test_a_session_with_no_tester_results_shows_no_empty_section(self):
        self.session.test_results = []
        self.session.save(update_fields=["test_results"])

        self.assertNotContains(self.client.get(self.url), "Test results")


@override_settings(LANGUAGE_CODE="en-us")
class CorrectingAReadingTests(Base):
    def setUp(self):
        super().setUp()
        self.session = self.upload()
        self.url = reverse("session_correct", args=[self.session.pk])

    def value(self, index: int, key: str) -> dict:
        self.session.refresh_from_db()
        result = self.session.test_results[index]
        for found in result["readings"] + [result["performed_on"]]:
            if found["key"] == key:
                return found
        raise AssertionError(f"no {key} on result {index}")

    def test_a_misread_reading_can_be_typed_over(self):
        self.client.post(self.url, {"tr-1-ripple": "14"}, follow=True)

        self.assertEqual(self.value(1, "ripple")["value"], "14")

    def test_and_is_recorded_as_somebody_having_looked(self):
        self.client.post(self.url, {"tr-1-ripple": "14"}, follow=True)
        found = self.value(1, "ripple")

        self.assertTrue(found["corrected"])
        self.assertEqual(found["confidence"], 1.0)

    def test_what_the_machine_read_is_never_overwritten(self):
        """The whole reason this lives in its own column.

        `extraction` answers "what did the tool actually say?", and it stops
        being able to the moment an edit lands on top of it.
        """
        before = dict(self.session.extraction)
        self.client.post(self.url, {"tr-1-ripple": "14"}, follow=True)
        self.session.refresh_from_db()

        self.assertEqual(self.session.extraction, before)
        self.assertEqual(self.value(1, "ripple")["raw"], "12mvV")

    def test_a_value_that_is_not_a_number_is_refused_and_said_so(self):
        response = self.client.post(self.url, {"tr-1-ripple": "about twelve"}, follow=True)

        self.assertEqual(self.value(1, "ripple")["value"], "12")
        self.assertContains(response, "has to be a number")

    def test_a_clock_that_is_not_a_clock_is_refused(self):
        response = self.client.post(
            self.url, {"tr-0-performed_on": "some time on Tuesday"}, follow=True
        )

        self.assertEqual(self.value(0, "performed_on")["value"], "2026-01-31T14:55:18")
        self.assertContains(response, "not a date and time")

    def test_a_reading_can_be_cleared_when_the_paper_says_nothing(self):
        self.client.post(self.url, {"tr-1-ripple": ""}, follow=True)

        self.assertEqual(self.value(1, "ripple")["value"], "")
        self.assertTrue(self.value(1, "ripple")["corrected"])

    def test_the_verdict_is_the_testers_word_and_is_not_editable(self):
        self.client.post(self.url, {"tr-0-verdict": "cranking_normal"}, follow=True)
        self.session.refresh_from_db()

        self.assertEqual(self.session.test_results[0]["verdict"]["value"], "cranking_low")

    def test_a_corrected_clock_reaches_the_session_itself(self):
        """Asking for a value and then filing the scan under the misreading is
        worse than not offering the box."""
        self.client.post(
            self.url, {"tr-0-performed_on": "2026-01-31 14:55:19"}, follow=True
        )
        self.session.refresh_from_db()

        self.assertEqual(
            timezone.localtime(self.session.performed_on).strftime("%H:%M:%S"),
            "14:55:19",
        )

    def test_and_the_session_is_still_dated_by_the_latest_receipt(self):
        """Correcting the earlier of two receipts must not re-date the visit."""
        self.client.post(
            self.url, {"tr-1-performed_on": "2026-01-31 14:00:00"}, follow=True
        )
        self.session.refresh_from_db()

        self.assertEqual(
            timezone.localtime(self.session.performed_on).strftime("%H:%M:%S"),
            "14:55:18",
        )

    def test_the_review_screen_shows_that_a_correction_landed(self):
        """The extraction is never edited, so without this the reader retyped a
        misread clock and came back to the misreading, presented as the
        reading."""
        self.client.post(
            self.url, {"tr-0-performed_on": "2026-01-31 15:01:02"}, follow=True
        )
        page = self.client.get(
            reverse("session_detail", args=[self.session.pk])
        ).content.decode()

        self.assertIn("corrected to 2026-01-31 15:01:02", page)
        self.assertIn("struck", page)

    def test_and_says_nothing_where_nothing_was_corrected(self):
        page = self.client.get(
            reverse("session_detail", args=[self.session.pk])
        ).content.decode()

        self.assertNotIn("corrected to", page)

    def test_a_confirmed_session_is_not_rewritten(self):
        self.session.confirm(self.user)
        self.client.post(self.url, {"tr-1-ripple": "99"}, follow=True)

        self.assertEqual(self.value(1, "ripple")["value"], "12")

    def test_confirming_carries_the_corrections_in_with_it(self):
        self.client.post(
            reverse("session_confirm", args=[self.session.pk]),
            {
                "performed_on": "2026-01-31 14:55:18",
                "tool": "TOPDON",
                "tool_model": "BT600 Plus",
                "odometer": "",
                "odometer_unit": "",
                "notes": "",
                "tr-1-ripple": "14",
            },
            follow=True,
        )
        self.session.refresh_from_db()

        self.assertEqual(self.session.review_status, ReviewStatus.CONFIRMED)
        self.assertEqual(self.value(1, "ripple")["value"], "14")


@override_settings(LANGUAGE_CODE="en-us")
class ReadingAPhotographAgainTests(Base):
    """A re-parse has to know it is looking at a photograph (FR-INT-5)."""

    def test_a_photo_session_reparses_as_an_image_not_as_a_pdf(self):
        session = self.upload()
        session.raw_media = None
        session.save(update_fields=["raw_media"])

        services.reparse(session)
        session.refresh_from_db()

        self.assertEqual(session.parser_profile.engine, "topdon_bt600_plus")
        self.assertEqual(len(session.test_results), 2)

    def test_the_document_it_builds_says_image(self):
        session = self.upload()
        session.raw_media = None
        session.save(update_fields=["raw_media"])

        self.assertEqual(services._document_for(session).media_type, "image")

    def test_the_pixels_are_read_again_where_the_original_is_still_there(self):
        """An improvement to the image pipeline is worth nothing to the reports
        already uploaded unless re-reading means re-reading the picture."""
        session = self.upload()
        pages, size = captured()
        with (
            mock.patch(
                "homeautoshop.mediafiles.services.read_image_words", return_value=pages
            ) as read,
            mock.patch(
                "homeautoshop.mediafiles.services.image_size", return_value=tuple(size)
            ),
        ):
            services.reparse(session)

        read.assert_called()

    def test_an_unreachable_original_falls_back_to_what_was_stored(self):
        """The object store being down is not a reason to refuse a re-parse."""
        session = self.upload()
        with mock.patch(
            "homeautoshop.mediafiles.models.Media.file",
            new_callable=mock.PropertyMock,
            side_effect=OSError("gone"),
        ):
            found = services._document_for(session)

        self.assertEqual(found.media_type, "image")
        self.assertEqual(found.pages, session.extracted_words)


class TheRivalSlipTests(TestCase):
    """Another maker's battery slip must not be handed to this parser."""

    def test_a_midtronics_printout_is_not_claimed(self):
        slip = (
            "MIDTRONICS  GR8\n"
            "BATTERY TEST  GOOD BATTERY\n"
            "MEASURED  612 CCA\n"
            "RATED     590 CCA\n"
            "VOLTAGE   12.61 V\n"
        )
        found, _score = engine.detect(
            profiles.available(), engine.Document(text=slip, media_type="image")
        )

        self.assertIsNone(found)

    def test_and_the_five_real_photographs_are(self):
        available = profiles.available()
        for capture in fixtures.samples():
            if fixtures.tool(capture) != "topdon bt600 plus":
                continue
            with self.subTest(capture=capture.name):
                found, _score = engine.detect(available, fixtures.document(capture))
                self.assertIsNotNone(found)
                self.assertEqual(found.engine, "topdon_bt600_plus")


@override_settings(LANGUAGE_CODE="en-us")
class ShowingAReadingRatherThanTheCharactersTests(Base):
    """What is shown once a scan is in the history, where there is no box.

    Reported three times over, as three different values, and it was one bug:
    the read-only branch showed `raw` — the characters the value was read from
    — instead of the reading. So a confirmed battery test claimed a health of
    `79% CS,`, where the `CS,` was a smudge on the line above, and there was no
    way to be rid of it because `raw` is deliberately never edited.

    **Nothing is re-derived when a scan is confirmed.** Confirming sets a status
    and retires what the scan replaced; it does not touch a reading. The draft
    screen looked right because it renders the *input*, whose value is the
    reading, and the confirmed screen rendered the column beside it.
    """

    #: Two photographs, because the three values reported were on two slips.
    SMUDGED = BATTERY_CAPTURE                     # `79%` read with a smudge beside it
    STRAY_COLON = "20260830_105624.words.json"    # `: 687CCA`, and `04.02mnN`

    def confirmed(self, capture: str):
        session = self.upload(capture)
        services.confirm(session, user=self.user)
        page = self.client.get(
            reverse("session_detail", args=[session.pk])
        ).content.decode()
        return session, page

    @staticmethod
    def reading(session, key: str) -> dict:
        for value in session.test_results[0]["readings"]:
            if value["key"] == key:
                return value
        raise AssertionError(key)

    def test_a_smudge_on_the_next_line_is_not_part_of_the_health(self):
        session, page = self.confirmed(self.SMUDGED)

        self.assertEqual(self.reading(session, "health")["raw"], "79% CS,")
        # The value column, specifically. The characters still appear in the
        # column beside the crop, which is the one place they belong.
        self.assertNotIn('<span class="mono">79% CS,</span>', page)
        self.assertIn('<span class="mono">79 %</span>', page)

    def test_nor_is_the_labels_own_colon_part_of_the_capacity(self):
        """`MEASURED: 687CCA` came back with the colon a character late."""
        session, page = self.confirmed(self.STRAY_COLON)

        self.assertEqual(self.reading(session, "measured")["raw"], "687CCA")
        self.assertIn(">687 CCA<", page)

    def test_and_the_unit_is_the_labels_whatever_the_glyph_became(self):
        session, page = self.confirmed(self.STRAY_COLON)
        found = self.reading(session, "internal_r")

        self.assertEqual(found["raw"], "04.02mnN")
        self.assertEqual(found["value"], "4.02")
        self.assertEqual(found["unit"], "mΩ")
        self.assertIn(">4.02 mΩ<", page)

    def test_the_characters_are_still_shown_beside_the_crop(self):
        """`raw` has a column of its own — that is what it is for."""
        _session, page = self.confirmed(self.STRAY_COLON)

        self.assertIn("04.02mnN", page)

    def test_confirming_changes_no_reading(self):
        session, _page = self.confirmed(self.SMUDGED)
        before = [dict(v) for v in session.test_results[0]["readings"]]
        again = self.upload(self.SMUDGED)

        self.assertEqual(before, [dict(v) for v in again.test_results[0]["readings"]])


@override_settings(LANGUAGE_CODE="en-us")
class ReadingTheSameReportTwiceTests(Base):
    """A re-reading is not a second scan (FR-INT-5).

    Re-parsing a confirmed report makes a new draft rather than rewriting the
    original, which is right and is what lets two profiles' answers be
    compared. Confirming that draft used to leave *both* in the history: the
    same battery test twice, identical but for the row it sat on, and no way to
    take either out because removal was offered on drafts alone.
    """

    def setUp(self):
        super().setUp()
        self.session = self.upload()
        services.confirm(self.session, user=self.user)

    def confirmed(self):
        return DiagnosticSession.objects.filter(
            asset=self.asset, review_status=ReviewStatus.CONFIRMED
        )

    def reread(self):
        self.client.post(reverse("session_reparse", args=[self.session.pk]), {}, follow=True)
        return DiagnosticSession.objects.filter(
            asset=self.asset, review_status=ReviewStatus.DRAFT
        ).latest("created_at")

    def test_re_reading_makes_a_draft_that_knows_what_it_re_read(self):
        draft = self.reread()

        self.assertEqual(draft.supersedes_id, self.session.pk)
        self.assertEqual(self.confirmed().count(), 1)

    def test_confirming_it_replaces_rather_than_duplicates(self):
        draft = self.reread()
        services.confirm(draft, user=self.user)

        self.assertEqual(self.confirmed().count(), 1)
        self.assertEqual(self.confirmed().get().pk, draft.pk)

    def test_and_the_one_it_replaced_is_in_the_trash_not_gone(self):
        draft = self.reread()
        _recurring, displaced = services.confirm(draft, user=self.user)

        self.assertEqual(displaced.pk, self.session.pk)
        self.assertTrue(DiagnosticSession.all_objects.get(pk=self.session.pk).is_deleted)

    def test_the_draft_says_so_before_it_is_confirmed(self):
        draft = self.reread()
        page = self.client.get(reverse("session_detail", args=[draft.pk])).content.decode()

        self.assertIn("replaces that one", page)

    def test_the_same_photograph_uploaded_again_is_the_same_report(self):
        """Media is deduplicated by SHA-256, so this is the same question asked
        of the bytes rather than of the operator."""
        again = self.upload()

        self.assertEqual(again.supersedes_id, self.session.pk)

    def test_a_different_vehicles_reading_is_not_replaced(self):
        other = Asset.objects.create(nickname="Van")
        elsewhere = DiagnosticSession.objects.create(
            asset=other, raw_media=self.session.raw_media, review_status=ReviewStatus.CONFIRMED
        )
        draft = self.reread()
        services.confirm(draft, user=self.user)
        elsewhere.refresh_from_db()

        self.assertFalse(elsewhere.is_deleted)


@override_settings(LANGUAGE_CODE="en-us")
class TakingAScanBackOutTests(Base):
    def setUp(self):
        super().setUp()
        self.session = self.upload()
        services.confirm(self.session, user=self.user)

    def test_a_confirmed_scan_can_be_removed_from_the_history(self):
        self.client.post(reverse("session_discard", args=[self.session.pk]), follow=True)

        self.assertTrue(DiagnosticSession.all_objects.get(pk=self.session.pk).is_deleted)

    def test_the_screen_offers_it(self):
        page = self.client.get(
            reverse("session_detail", args=[self.session.pk])
        ).content.decode()

        self.assertIn("Remove from the history", page)

    def test_and_says_it_is_recoverable(self):
        response = self.client.post(
            reverse("session_discard", args=[self.session.pk]), follow=True
        )

        self.assertContains(response, "trash for 30 days")

    def test_the_trash_knows_about_sessions(self):
        """A soft delete listed nowhere is a delete that is permanent and
        invisible at once — the third time this repository has had to say so."""
        from homeautoshop.core.views import TRASHABLE

        self.assertIn("diagnostic_session", TRASHABLE)

    def test_a_removed_scan_stops_contributing_open_codes(self):
        """A join does not consult the related model's manager."""
        from homeautoshop.diagnostics.models import DiagnosticCode

        DiagnosticCode.objects.create(session=self.session, code="P0420")
        self.session.delete()
        page = self.client.get(
            reverse("asset_diagnostics", args=[self.asset.pk])
        ).content.decode()

        self.assertNotIn("P0420", page)


@override_settings(LANGUAGE_CODE="en-us")
class TheWayBackTests(Base):
    """The crumb pointed at the import queue whatever the scan was, and that
    list holds drafts — so from a confirmed scan the way back led to a page
    that does not contain what you were looking at."""

    def setUp(self):
        super().setUp()
        self.session = self.upload()

    def crumb(self, **params) -> str:
        page = self.client.get(
            reverse("session_detail", args=[self.session.pk]), params
        ).content.decode()
        return page.split('<nav class="crumb"', 1)[1].split("</nav>", 1)[0]

    def test_a_draft_goes_back_to_the_queue_it_is_waiting_in(self):
        self.assertIn(reverse("diagnostic_queue"), self.crumb())

    def test_but_back_to_the_vehicle_when_that_is_where_it_came_from(self):
        self.assertIn(
            reverse("asset_diagnostics", args=[self.asset.pk]), self.crumb(**{"from": "vehicle"})
        )

    def test_and_a_confirmed_scan_always_goes_back_to_the_vehicle(self):
        services.confirm(self.session, user=self.user)

        self.assertIn(reverse("asset_diagnostics", args=[self.asset.pk]), self.crumb())

    def test_the_vehicle_page_says_where_its_links_come_from(self):
        page = self.client.get(
            reverse("asset_diagnostics", args=[self.asset.pk])
        ).content.decode()

        self.assertIn("?from=vehicle", page)


@override_settings(LANGUAGE_CODE="en-us")
class WhatATesterCanEvenReportTests(Base):
    """Declared by the profile rather than guessed from an empty list."""

    def setUp(self):
        super().setUp()
        self.session = self.upload()
        services.confirm(self.session, user=self.user)

    def test_a_battery_testers_profile_says_it_reports_no_codes(self):
        self.assertEqual(self.session.parser_profile.reports, ["test_results"])
        self.assertFalse(self.session.shows_codes)

    def test_a_scan_tools_profile_says_it_does(self):
        found = [p for p in profiles.available() if p.engine == "xtool_d8"][0]

        self.assertIn("codes", found.reports)
        self.assertTrue(found.could_report("codes"))

    def test_a_profile_that_has_not_said_is_not_taken_to_mean_nothing(self):
        """Every profile in the catalog predates this field."""
        found = [p for p in profiles.available() if not p.reports]

        self.assertTrue(found)
        self.assertTrue(found[0].could_report("codes"))

    def test_the_vehicles_scan_list_does_not_report_zero_codes(self):
        page = self.client.get(
            reverse("asset_diagnostics", args=[self.asset.pk])
        ).content.decode()

        self.assertNotIn("0 codes", page)

    def test_it_says_what_the_tester_actually_found(self):
        page = self.client.get(
            reverse("asset_diagnostics", args=[self.asset.pk])
        ).content.decode()

        self.assertIn("CRANKING LOW", page)

    def test_a_scan_tool_that_found_none_still_says_so(self):
        self.session.parser_profile.reports = ["codes"]
        self.session.parser_profile.save(update_fields=["reports"])
        page = self.client.get(
            reverse("asset_diagnostics", args=[self.asset.pk])
        ).content.decode()

        self.assertIn("0 codes", page)

    def test_a_nonsense_declaration_is_refused_rather_than_ignored(self):
        from homeautoshop.diagnostics.profiles import ProfileInvalid, from_yaml

        with self.assertRaises(ProfileInvalid):
            from_yaml("name: X\nmedia_type: text\nreports: [codez]\n")
