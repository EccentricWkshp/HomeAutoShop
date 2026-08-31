"""
What a stored file looks like on a page (SPEC §5.1, FR-DOC-3).

The reported symptom was a broken image where a purchase receipt should be.
The cause was not the storage fix from earlier — that works — but an
assumption underneath it: that everything attached to a record is a picture.
A receipt is normally a PDF, `derive()` skipped anything that was not an
image, and so `display_url` fell back to the original and the page put
`application/pdf` inside an `<img>`.

These tests are written against the two halves of that separately, because
they fail separately: a PDF should *gain* a picture, and anything that cannot
have one must not be asked to draw itself anyway.
"""

from __future__ import annotations

from io import BytesIO

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse

from homeautoshop.accounts.models import Role, User
from homeautoshop.assets.models import Asset
from homeautoshop.mediafiles.models import Media, MediaLink
from homeautoshop.mediafiles.services import derive, ingest
from homeautoshop.mediafiles.testing import LocalMediaMixin
from homeautoshop.purchasing.models import Purchase, Vendor

VIN = "1M8GDM9AXKP042788"


def a_pdf(text: str = "Receipt") -> bytes:
    """A one-page PDF with a text layer, built rather than committed.

    A binary fixture would be a file nobody in the repository can read or
    check, for something this small.
    """
    import pypdfium2  # noqa: F401  — assert the renderer is installed

    from reportlab.pdfgen import canvas  # type: ignore

    buffer = BytesIO()
    page = canvas.Canvas(buffer)
    page.drawString(100, 700, text)
    page.save()
    return buffer.getvalue()


def a_png(colour=(200, 30, 30)) -> bytes:
    from PIL import Image

    buffer = BytesIO()
    Image.new("RGB", (900, 700), colour).save(buffer, format="PNG")
    return buffer.getvalue()


class PdfPreviewTests(LocalMediaMixin, TestCase):
    """A PDF is a document with a picture in it."""

    def setUp(self):
        super().setUp()
        try:
            self.pdf = a_pdf()
        except ImportError:  # pragma: no cover - reportlab is a dev dependency
            self.skipTest("reportlab is not installed")

    def upload(self) -> Media:
        media, _ = ingest(
            SimpleUploadedFile("receipt.pdf", self.pdf, content_type="application/pdf")
        )
        return media

    def test_the_first_page_becomes_the_thumbnail(self):
        media = self.upload()
        derive(media)
        media.refresh_from_db()
        self.assertTrue(media.thumb, "a PDF still has no picture")
        self.assertTrue(media.preview)

    def test_the_thumbnail_is_an_image_a_browser_can_draw(self):
        media = self.upload()
        derive(media)
        media.refresh_from_db()
        from PIL import Image

        with media.thumb.open("rb") as fh:
            image = Image.open(fh)
            image.load()
        self.assertEqual(image.format, "JPEG")
        self.assertLessEqual(max(image.size), 400)

    def test_the_document_keeps_its_own_type(self):
        """The preview is a picture of it, not a replacement for it."""
        media = self.upload()
        derive(media)
        media.refresh_from_db()
        self.assertEqual(media.mime, "application/pdf")
        self.assertEqual(media.kind, Media.Kind.DOCUMENT)
        self.assertIn(str(media.pk), media.url_for())

    def test_page_size_is_not_recorded_as_the_documents_dimensions(self):
        """Those pixels are ours, not the file's; claiming them would be a lie."""
        media = self.upload()
        derive(media)
        media.refresh_from_db()
        self.assertIsNone(media.width)
        self.assertIsNone(media.height)


class NoPreviewTests(LocalMediaMixin, TestCase):
    """What happens when there is no picture to be had."""

    def test_a_file_with_no_picture_offers_no_image_source(self):
        media, _ = ingest(SimpleUploadedFile("notes.csv", b"a,b\n1,2\n", content_type="text/csv"))
        derive(media)
        media.refresh_from_db()
        self.assertFalse(media.has_image_preview)
        self.assertEqual(media.display_url, "", "a CSV was offered as an image source")

    def test_the_link_to_the_file_itself_still_works(self):
        """No picture is not the same as no file."""
        media, _ = ingest(SimpleUploadedFile("notes.csv", b"a,b\n", content_type="text/csv"))
        self.assertTrue(media.url_for())

    def test_a_format_browsers_refuse_to_draw_is_not_offered_as_one(self):
        """HEIC off an iPhone is a real image and an undrawable one."""
        media = Media(kind=Media.Kind.PHOTO, mime="image/heic")
        media.file.save("photo.heic", SimpleUploadedFile("photo.heic", b"x"), save=False)
        media.save()
        self.assertFalse(media.has_image_preview)

    def test_derivation_that_cannot_read_the_file_does_not_fail_the_job(self):
        """A corrupt upload is not a reason to leave a job retrying for ever."""
        media = Media(kind=Media.Kind.PHOTO, mime="image/png")
        media.file.save("broken.png", SimpleUploadedFile("broken.png", b"nope"), save=False)
        media.save()
        derive(media)  # must not raise
        media.refresh_from_db()
        self.assertIsNotNone(media.derived_at)
        self.assertFalse(media.thumb)

    def test_a_badge_names_the_kind_of_file(self):
        media, _ = ingest(
            SimpleUploadedFile("Order 12345.pdf", b"%PDF-1.4 ", content_type="application/pdf")
        )
        self.assertEqual(media.extension_label, "PDF")


class ReceiptOnThePageTests(LocalMediaMixin, TestCase):
    """The screen the report came from."""

    def setUp(self):
        super().setUp()
        self.user = User.objects.create_user(username="andy", password="x" * 16, role=Role.ADMIN)
        self.client.force_login(self.user)
        self.purchase = Purchase.objects.create(
            vendor=Vendor.objects.create(name="RockAuto")
        )

    def attach(self, name: str, data: bytes, content_type: str) -> Media:
        media, _ = ingest(
            SimpleUploadedFile(name, data, content_type=content_type),
            entity=self.purchase,
            role=MediaLink.Role.RECEIPT,
        )
        return media

    def page(self) -> str:
        return self.client.get(
            reverse("purchase_detail", args=[self.purchase.pk])
        ).content.decode()

    def test_a_pdf_receipt_is_never_put_inside_an_img(self):
        """The bug, stated as a test: no `<img>` pointing at a document."""
        media = self.attach("receipt.pdf", b"%PDF-1.4 not renderable", "application/pdf")
        page = self.page()
        self.assertNotIn(f'<img src="{media.url_for()}"', page)
        self.assertIn("PDF", page, "the receipt is not shown as anything at all")

    def test_the_receipt_is_still_reachable(self):
        media = self.attach("receipt.pdf", b"%PDF-1.4 not renderable", "application/pdf")
        self.assertIn(f'href="{media.url_for()}"', self.page())

    def test_a_photographed_receipt_still_shows_as_a_picture(self):
        media = self.attach("receipt.png", a_png(), "image/png")
        derive(media)
        media.refresh_from_db()
        self.assertIn(f'<img src="{media.display_url}"', self.page())

    def test_amounts_are_not_described_as_minor_units(self):
        """They render as $442.13; the caption said they were cents."""
        self.assertNotIn("minor units", self.page())


class BackfillTests(LocalMediaMixin, TestCase):
    def test_the_command_queues_files_that_have_no_preview(self):
        from django.core.management import call_command

        from homeautoshop.core.models import Job

        media, _ = ingest(
            SimpleUploadedFile("old.pdf", b"%PDF-1.4 ", content_type="application/pdf")
        )
        Job.objects.all().delete()

        call_command("rederive")
        self.assertEqual(
            Job.objects.filter(type="media.derive", payload__media_id=str(media.pk)).count(), 1
        )

    def test_it_leaves_alone_what_could_never_have_one(self):
        from django.core.management import call_command

        from homeautoshop.core.models import Job

        ingest(SimpleUploadedFile("notes.csv", b"a,b\n", content_type="text/csv"))
        Job.objects.all().delete()

        call_command("rederive")
        self.assertEqual(Job.objects.filter(type="media.derive").count(), 0)

    def test_it_does_not_queue_the_same_file_twice(self):
        from django.core.management import call_command

        from homeautoshop.core.models import Job

        ingest(SimpleUploadedFile("old.pdf", b"%PDF-1.4 ", content_type="application/pdf"))
        Job.objects.all().delete()

        call_command("rederive")
        call_command("rederive")
        self.assertEqual(Job.objects.filter(type="media.derive").count(), 1)


class VehicleTimelineTests(LocalMediaMixin, TestCase):
    """The timeline links to files; a link needs a target."""

    def setUp(self):
        super().setUp()
        self.user = User.objects.create_user(username="andy", password="x" * 16, role=Role.ADMIN)
        self.client.force_login(self.user)
        self.asset = Asset.objects.create(nickname="Red truck", vin=VIN)

    def test_a_document_in_the_timeline_still_has_somewhere_to_go(self):
        media, _ = ingest(
            SimpleUploadedFile("manual.pdf", b"%PDF-1.4 ", content_type="application/pdf"),
            entity=self.asset,
        )
        page = self.client.get(reverse("asset_detail", args=[self.asset.pk])).content.decode()
        self.assertIn(media.url_for(), page)


class ServedTypeTests(LocalMediaMixin, TestCase):
    """The bytes and the label have to agree.

    Getting the preview generated was only most of the fix: the response still
    carried the *original's* content type, so a JPEG rendered from a PDF
    arrived announced as `application/pdf` and the browser was within its
    rights to refuse to draw it.
    """

    def setUp(self):
        super().setUp()
        self.user = User.objects.create_user(username="andy", password="x" * 16, role=Role.ADMIN)
        self.client.force_login(self.user)

    def store(self, name: str, data: bytes, content_type: str) -> Media:
        media, _ = ingest(SimpleUploadedFile(name, data, content_type=content_type))
        return media

    def test_a_pdf_thumbnail_arrives_as_an_image(self):
        try:
            pdf = a_pdf()
        except ImportError:  # pragma: no cover
            self.skipTest("reportlab is not installed")
        media = self.store("receipt.pdf", pdf, "application/pdf")
        derive(media)
        media.refresh_from_db()

        response = self.client.get(media.display_url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "image/jpeg")

    def test_the_original_still_arrives_as_itself(self):
        media = self.store("receipt.pdf", b"%PDF-1.4 ", "application/pdf")
        response = self.client.get(media.url_for())
        self.assertEqual(response["Content-Type"], "application/pdf")

    def test_a_derivative_is_not_offered_under_the_originals_name(self):
        try:
            pdf = a_pdf()
        except ImportError:  # pragma: no cover
            self.skipTest("reportlab is not installed")
        media = self.store("receipt.pdf", pdf, "application/pdf")
        derive(media)
        media.refresh_from_db()
        disposition = self.client.get(media.display_url)["Content-Disposition"]
        self.assertNotIn(".pdf", disposition)


class InlineSafetyTests(LocalMediaMixin, TestCase):
    """These bytes come back from this application's own origin.

    Uploads keep whatever content type the browser claimed, and nothing
    restricts what may be uploaded — so serving that type back inline means an
    SVG or an HTML file would run its own script with the reader's session
    behind it. Rendering is allowed for the formats that cannot.
    """

    def setUp(self):
        super().setUp()
        self.user = User.objects.create_user(username="andy", password="x" * 16, role=Role.ADMIN)
        self.client.force_login(self.user)

    def store(self, name: str, data: bytes, content_type: str) -> Media:
        media, _ = ingest(SimpleUploadedFile(name, data, content_type=content_type))
        return media

    def test_an_svg_is_handed_over_rather_than_rendered(self):
        media = self.store(
            "logo.svg",
            b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>',
            "image/svg+xml",
        )
        response = self.client.get(media.url_for())
        self.assertIn("attachment", response["Content-Disposition"])
        self.assertNotEqual(response["Content-Type"], "image/svg+xml")

    def test_html_claiming_to_be_html_is_not_served_as_html(self):
        media = self.store("note.html", b"<script>alert(1)</script>", "text/html")
        response = self.client.get(media.url_for())
        self.assertIn("attachment", response["Content-Disposition"])
        self.assertNotIn("text/html", response["Content-Type"])

    def test_a_photo_is_still_shown_in_place(self):
        media = self.store("photo.png", a_png(), "image/png")
        response = self.client.get(media.url_for())
        self.assertIn("inline", response["Content-Disposition"])
        self.assertEqual(response["Content-Type"], "image/png")

    def test_a_receipt_pdf_is_still_shown_in_place(self):
        media = self.store("receipt.pdf", b"%PDF-1.4 ", "application/pdf")
        response = self.client.get(media.url_for())
        self.assertIn("inline", response["Content-Disposition"])

    def test_nothing_is_left_to_the_browsers_own_guess(self):
        media = self.store("photo.png", a_png(), "image/png")
        self.assertEqual(self.client.get(media.url_for())["X-Content-Type-Options"], "nosniff")
