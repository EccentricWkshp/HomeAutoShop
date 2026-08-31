"""
Reading a photographed printout (SPEC §7.9, FR-DOC-5, FR-INT-4).

Plenty of shop equipment prints paper and nothing else — a battery tester, a
charging-system tester, an alignment rack. What the operator has afterwards is
a phone photo, and the import would not take one: the file box accepted PDFs
and text, so a JPEG was decoded as UTF-8 and handed to the parsers as line
noise. This is precisely the case OCR was specified for.

Tesseract is not exercised here. What is exercised is that a photo is
recognized as one, routed to OCR rather than to the CSV sniffer, and recorded
as having come from a picture — because the review screen has more reason to
doubt a value guessed from pixels than one parsed from a column.
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

from homeautoshop.accounts.models import Role, User
from homeautoshop.assets.models import Asset
from homeautoshop.diagnostics import engine
from homeautoshop.diagnostics.models import DiagnosticSession, SessionSource
from homeautoshop.mediafiles.models import Media

VIN = "1M8GDM9AXKP042788"
STATICFILES = {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"}

BATTERY_SLIP = """MIDTRONICS  GR8
BATTERY TEST  GOOD BATTERY
MEASURED  612 CCA
RATED     590 CCA
VOLTAGE   12.61 V
"""


def a_photo(name: str = "slip.jpg") -> SimpleUploadedFile:
    """A real JPEG, so the signature check is doing the work, not the name."""
    from PIL import Image

    buffer = BytesIO()
    Image.new("RGB", (40, 30), (255, 255, 255)).save(buffer, format="JPEG")
    return SimpleUploadedFile(name, buffer.getvalue(), content_type="image/jpeg")


class RecognitionTests(TestCase):
    """A photo has to be spotted before anything can be done with it."""

    def test_a_jpeg_is_read_as_an_image_not_as_text(self):
        with mock.patch(
            "homeautoshop.mediafiles.services.read_image_text", return_value=BATTERY_SLIP
        ):
            document = engine.read(a_photo())
        self.assertEqual(document.media_type, "image")
        self.assertIn("612 CCA", document.text)

    def test_the_signature_decides_rather_than_the_file_name(self):
        """A phone hands over a HEIC named `.jpg` often enough to matter."""
        with mock.patch(
            "homeautoshop.mediafiles.services.read_image_text", return_value="x"
        ):
            self.assertEqual(engine.read(a_photo("report.pdf")).media_type, "image")

    def test_a_csv_is_still_a_csv(self):
        upload = SimpleUploadedFile("d.csv", b"code,desc\nP0301,Misfire\n", content_type="text/csv")
        self.assertEqual(engine.read(upload).media_type, "csv")

    def test_an_unreadable_photo_gives_empty_text_rather_than_raising(self):
        """A report that could not be read still becomes a session the operator
        can map by hand (FR-INT-6), which beats an error page."""
        from homeautoshop.mediafiles import services

        with mock.patch.dict("sys.modules", {"pytesseract": None}):
            self.assertEqual(services.read_image_text(b"\xff\xd8\xff not a jpeg"), "")

    @override_settings(OCR_ENABLED=False)
    def test_with_ocr_switched_off_it_says_nothing_rather_than_guessing(self):
        from homeautoshop.mediafiles import services

        self.assertEqual(services.read_image_text(b"\xff\xd8\xffanything"), "")


class ScannedPdfTests(TestCase):
    """§7.9 always promised this and it was never wired up."""

    def test_a_pdf_with_no_text_layer_falls_back_to_reading_the_pixels(self):
        with (
            mock.patch(
                "homeautoshop.scantools.xtool_d8.words_from_pdf", return_value=[[]]
            ),
            mock.patch(
                "homeautoshop.mediafiles.services.read_pdf_text_by_ocr",
                return_value=BATTERY_SLIP,
            ) as ocr,
        ):
            document = engine.read(
                SimpleUploadedFile("s.pdf", b"%PDF-1.4 scanned", content_type="application/pdf")
            )
        ocr.assert_called_once()
        self.assertIn("612 CCA", document.text)

    def test_a_pdf_that_has_a_text_layer_is_not_rasterised(self):
        pages = [[{"text": "P0301", "x0": 0, "top": 0}]]
        with (
            mock.patch("homeautoshop.scantools.xtool_d8.words_from_pdf", return_value=pages),
            mock.patch("homeautoshop.mediafiles.services.read_pdf_text_by_ocr") as ocr,
        ):
            engine.read(SimpleUploadedFile("s.pdf", b"%PDF-1.4", content_type="application/pdf"))
        ocr.assert_not_called()


class ImportTests(TestCase):
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

        self.user = User.objects.create_user(username="andy", password="x" * 16, role=Role.ADMIN)
        self.client.force_login(self.user)
        self.asset = Asset.objects.create(nickname="Red truck", vin=VIN)

    def upload(self, field="report", **kwargs):
        with mock.patch(
            "homeautoshop.mediafiles.services.read_image_text", return_value=BATTERY_SLIP
        ):
            return self.client.post(
                reverse("session_import", args=[self.asset.pk]),
                {field: a_photo(), **kwargs},
                follow=True,
            )

    def test_a_photographed_printout_becomes_a_session(self):
        self.upload()
        session = DiagnosticSession.objects.get(asset=self.asset)
        self.assertIn("612 CCA", session.extracted_text)

    def test_it_is_recorded_as_having_come_from_a_photo(self):
        """The review screen has more reason to doubt pixels than a column."""
        self.upload()
        self.assertEqual(
            DiagnosticSession.objects.get(asset=self.asset).source, SessionSource.PHOTO
        )

    def test_the_original_photo_is_kept_and_kept_as_a_photo(self):
        """It gets a thumbnail like any other picture, not a document icon."""
        self.upload()
        session = DiagnosticSession.objects.get(asset=self.asset)
        self.assertIsNotNone(session.raw_media)
        self.assertEqual(session.raw_media.kind, Media.Kind.PHOTO)

    def test_the_camera_button_posts_to_the_same_place(self):
        """Two controls, one endpoint — see the template for why they cannot
        share a name."""
        self.upload(field="report_photo")
        self.assertTrue(DiagnosticSession.objects.filter(asset=self.asset).exists())

    def test_the_page_offers_it_rather_than_leaving_it_to_be_discovered(self):
        page = self.client.get(reverse("asset_diagnostics", args=[self.asset.pk]))
        self.assertContains(page, "image/*")
        self.assertContains(page, "A photo works too")

    def test_nothing_uploaded_is_a_message_not_a_crash(self):
        response = self.client.post(
            reverse("session_import", args=[self.asset.pk]), {}, follow=True
        )
        self.assertContains(response, "Choose a report or a photo")
