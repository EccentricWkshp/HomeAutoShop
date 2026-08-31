"""
Text recognition (SPEC FR-DOC-5, FR-COST-4, §14 `OCR_ENABLED`).

Tesseract itself is not exercised here — a test that shells out to a binary
which may or may not be installed tells you about the machine, not the code.
What *is* tested is everything around it, which is where every bug so far has
been:

* The `OCR_ENABLED` switch, which §14 documented and nothing read.
* The language string, which was never passed, so two of the three language
  packs the image installed could not be reached.
* Image-only PDFs, which Pillow cannot open at all — the whole file failed on
  exactly the case that OCR exists for.
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path
from unittest import mock

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings

from homeautoshop.core.jobs import HANDLERS
from homeautoshop.core.models import Job
from homeautoshop.core.schedule import recurring
from homeautoshop.mediafiles import services
from homeautoshop.mediafiles.models import Media


def a_document(name: str = "receipt.pdf", mime: str = "application/pdf") -> SimpleUploadedFile:
    return SimpleUploadedFile(name, b"%PDF-1.4 not really a pdf", content_type=mime)


class LocalStorage(TestCase):
    """Bytes go to a temp directory, not to MinIO.

    Without this the suite inherits STORAGE_DRIVER from the ambient .env and
    every upload here spends its time retrying a connection to a container.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.storage = override_settings(
            MEDIA_ROOT=self.tmp,
            STORAGES={"default": {"BACKEND": "django.core.files.storage.FileSystemStorage"}},
        )
        self.storage.enable()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.addCleanup(self.storage.disable)


class SwitchTests(LocalStorage):
    """§14 promised `OCR_ENABLED`. Nothing in the codebase read it."""

    @override_settings(OCR_ENABLED=True)
    def test_an_uploaded_document_is_queued_for_reading(self):
        media, _ = services.ingest(a_document(), kind=Media.Kind.DOCUMENT)
        self.assertEqual(media.ocr_status, Media.OcrStatus.PENDING)
        self.assertTrue(Job.objects.filter(type="media.ocr").exists())

    @override_settings(OCR_ENABLED=False)
    def test_switching_it_off_queues_nothing(self):
        services.ingest(a_document(), kind=Media.Kind.DOCUMENT)
        self.assertFalse(Job.objects.filter(type="media.ocr").exists())

    @override_settings(OCR_ENABLED=False)
    def test_but_the_file_still_records_that_it_wanted_reading(self):
        """`failed` would be a lie: nothing was attempted.

        It matters which one, because the sweep below looks for `pending`.
        Marking these `failed` is how a switch becomes one-way.
        """
        media, _ = services.ingest(a_document(), kind=Media.Kind.DOCUMENT)
        self.assertEqual(media.ocr_status, Media.OcrStatus.PENDING)

    @override_settings(OCR_ENABLED=False)
    def test_running_the_job_anyway_leaves_it_pending(self):
        media, _ = services.ingest(a_document(), kind=Media.Kind.DOCUMENT)
        services.ocr(media)
        media.refresh_from_db()
        self.assertEqual(media.ocr_status, Media.OcrStatus.PENDING)


class BacklogTests(LocalStorage):
    """Turning OCR back on has to mean something for what came before."""

    def setUp(self):
        super().setUp()
        with override_settings(OCR_ENABLED=False):
            self.media, _ = services.ingest(a_document(), kind=Media.Kind.DOCUMENT)
        Job.objects.all().delete()

    @override_settings(OCR_ENABLED=True)
    def test_the_sweep_picks_up_what_was_missed(self):
        HANDLERS["media.ocr_sweep"]({})
        self.assertEqual(
            list(Job.objects.filter(type="media.ocr").values_list("payload__media_id", flat=True)),
            [str(self.media.pk)],
        )

    @override_settings(OCR_ENABLED=True)
    def test_it_does_not_queue_the_same_file_twice(self):
        HANDLERS["media.ocr_sweep"]({})
        HANDLERS["media.ocr_sweep"]({})
        self.assertEqual(Job.objects.filter(type="media.ocr").count(), 1)

    @override_settings(OCR_ENABLED=False)
    def test_the_sweep_is_a_no_op_while_ocr_is_off(self):
        HANDLERS["media.ocr_sweep"]({})
        self.assertFalse(Job.objects.filter(type="media.ocr").exists())

    @override_settings(OCR_ENABLED=True)
    def test_it_is_scheduled_rather_than_needing_to_be_asked_for(self):
        self.assertIn("media.ocr_sweep", [job for job, _every in recurring()])

    @override_settings(OCR_ENABLED=False)
    def test_and_is_not_scheduled_when_there_is_nothing_to_sweep(self):
        self.assertNotIn("media.ocr_sweep", [job for job, _every in recurring()])


class LanguageTests(TestCase):
    """The image installed three language packs and the code asked for none."""

    def setUp(self):
        services._usable_languages.cache_clear()
        self.addCleanup(services._usable_languages.cache_clear)

    @override_settings(OCR_LANGUAGES="eng+fra")
    def test_the_configured_languages_are_actually_requested(self):
        with mock.patch.object(
            services, "tesseract_status", return_value={"installed": ["eng", "fra", "spa"]}
        ):
            self.assertEqual(services._ocr_language(), "eng+fra")

    @override_settings(OCR_LANGUAGES="eng+deu")
    def test_a_language_the_image_lacks_does_not_take_the_others_down(self):
        """Tesseract fails the whole call for one missing language.

        So adding a fourth language to the compose file without rebuilding
        would otherwise stop OCR working at all, rather than for German.
        """
        with mock.patch.object(
            services, "tesseract_status", return_value={"installed": ["eng", "fra"]}
        ):
            self.assertEqual(services._ocr_language(), "eng")

    @override_settings(OCR_LANGUAGES="deu")
    def test_with_nothing_configured_installed_it_still_reads_the_numbers(self):
        """A price, a date and a part number survive the wrong alphabet."""
        with mock.patch.object(
            services, "tesseract_status", return_value={"installed": ["eng"]}
        ):
            self.assertEqual(services._ocr_language(), "eng")

    def test_the_setting_accepts_the_way_people_actually_write_lists(self):
        from config.settings import env  # noqa: F401  (documents the source)

        self.assertEqual("+".join("eng, fra  spa".replace(",", " ").split()), "eng+fra+spa")


class ImageOnlyPdfTests(LocalStorage):
    """The case OCR exists for, and the one that failed outright."""

    def setUp(self):
        super().setUp()
        self.media, _ = services.ingest(a_document(), kind=Media.Kind.DOCUMENT)

    @override_settings(OCR_ENABLED=True)
    def test_a_pdf_with_a_text_layer_is_never_rasterised(self):
        """Faster and more accurate, so it must be tried first."""
        with (
            mock.patch.object(services, "_pdf_text", return_value="OIL FILTER 12.99"),
            mock.patch.object(services, "_pdf_pages") as raster,
        ):
            services.ocr(self.media)
        raster.assert_not_called()
        self.media.refresh_from_db()
        self.assertEqual(self.media.ocr_status, Media.OcrStatus.DONE)
        self.assertIn("OIL FILTER", self.media.ocr_text)

    @override_settings(OCR_ENABLED=True, OCR_LANGUAGES="eng")
    def test_a_pdf_with_no_text_layer_falls_back_to_reading_the_pixels(self):
        page = mock.Mock()
        fake_tesseract = mock.Mock()
        fake_tesseract.image_to_string.return_value = "SCANNED RECEIPT"
        with (
            mock.patch.object(services, "_pdf_text", return_value="   "),
            mock.patch.object(services, "_pdf_pages", return_value=iter([page])),
            mock.patch.dict("sys.modules", {"pytesseract": fake_tesseract}),
            mock.patch.object(services, "_ocr_language", return_value="eng"),
        ):
            services.ocr(self.media)

        fake_tesseract.image_to_string.assert_called_once_with(page, lang="eng")
        self.media.refresh_from_db()
        self.assertEqual(self.media.ocr_status, Media.OcrStatus.DONE)
        self.assertEqual(self.media.ocr_text, "SCANNED RECEIPT")

    @override_settings(OCR_ENABLED=True)
    def test_an_unreadable_file_fails_that_file_and_nothing_else(self):
        with mock.patch.object(services, "_pdf_text", side_effect=RuntimeError("corrupt")):
            services.ocr(self.media)
        self.media.refresh_from_db()
        self.assertEqual(self.media.ocr_status, Media.OcrStatus.FAILED)

    @override_settings(OCR_ENABLED=True, OCR_PDF_MAX_PAGES=2)
    def test_a_long_document_is_read_only_as_far_as_the_cap(self):
        """A hundred-page manual should not be an hour of worker time."""
        rendered = []

        class FakePage:
            def render(self, scale):
                bitmap = mock.Mock()
                bitmap.to_pil.return_value = f"page-{len(rendered)}"
                rendered.append(scale)
                return bitmap

            def close(self):
                pass

        class FakeDocument:
            def __init__(self, data):
                pass

            def __len__(self):
                return 50

            def __getitem__(self, index):
                return FakePage()

            def close(self):
                pass

        fake_pypdfium2 = mock.Mock(PdfDocument=FakeDocument)
        with mock.patch.dict("sys.modules", {"pypdfium2": fake_pypdfium2}):
            pages = list(services._pdf_pages(self.media))

        self.assertEqual(len(pages), 2)
        # 300 DPI: Tesseract's accuracy falls off sharply below it.
        self.assertEqual(rendered, [300 / 72, 300 / 72])


class HealthReportingTests(TestCase):
    """A capability nobody can check is one that fails quietly."""

    def setUp(self):
        services._usable_languages.cache_clear()
        self.addCleanup(services._usable_languages.cache_clear)

    @override_settings(OCR_ENABLED=True, OCR_LANGUAGES="eng+fra")
    def test_it_reports_what_is_configured_against_what_is_installed(self):
        fake = mock.Mock()
        fake.get_tesseract_version.return_value = "5.5.0"
        fake.get_languages.return_value = ["eng", "osd"]
        with mock.patch.dict("sys.modules", {"pytesseract": fake}):
            status = services.tesseract_status()

        self.assertEqual(status["version"], "5.5.0")
        self.assertEqual(status["configured"], ["eng", "fra"])
        self.assertEqual(status["missing"], ["fra"])

    @override_settings(OCR_ENABLED=True)
    def test_a_missing_binary_is_reported_rather_than_raised(self):
        fake = mock.Mock()
        fake.get_tesseract_version.side_effect = OSError("no tesseract")
        with mock.patch.dict("sys.modules", {"pytesseract": fake}):
            status = services.tesseract_status()
        self.assertEqual(status["version"], "")

    def test_the_health_screen_says_so(self):
        from homeautoshop.accounts.models import Role, User

        user = User.objects.create_user(username="andy", password="x" * 16, role=Role.ADMIN)
        self.client.force_login(user)
        with mock.patch.object(
            services,
            "tesseract_status",
            return_value={"enabled": True, "version": "", "configured": ["eng"],
                          "installed": [], "missing": [], "binary": ""},
        ):
            page = self.client.get("/health/")
        self.assertContains(page, "Tesseract is not installed")
