"""
Reading what a file is, rather than what the browser called it.

Reported as: *I added webp images to a vehicle and they didn't show up.* They
were stored and they were filed as documents, so they landed in the Documents
card — and since nothing that is not an image gets a preview, no thumbnail was
ever made for them either. A gray box in the wrong place.

`Content-Type` on a multipart upload is a claim by the client, and on Windows
the browser makes that claim by looking the extension up in the registry.
`.webp` often has no entry, so Chrome and Edge send `application/octet-stream`
for a file whose first twelve bytes say `RIFF….WEBP`.

The images below are made with Pillow rather than typed out, because the point
is what a real file starts with — a handwritten header proves the reader agrees
with its author. The formats Pillow will not write here are given their real
signatures from the specification, which is the next best evidence.
"""

from __future__ import annotations

import io

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase, TestCase
from django.urls import reverse
from PIL import Image

from homeautoshop.accounts.models import Role, User
from homeautoshop.assets.models import Asset
from homeautoshop.mediafiles import sniff
from homeautoshop.mediafiles.models import Media
from homeautoshop.mediafiles.services import ingest


def picture(fmt: str, shade: int = 90) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (48, 32), (shade, 40, 40)).save(buffer, format=fmt)
    return buffer.getvalue()


#: An ISO base media file's first bytes: a box length, `ftyp`, and the brand.
def iso(brand: bytes) -> bytes:
    return b"\x00\x00\x00\x18ftyp" + brand + b"\x00\x00\x00\x00" + brand


class WhatTheBytesSayTests(SimpleTestCase):
    def test_a_real_webp_is_recognized(self):
        self.assertEqual(sniff.from_bytes(picture("WEBP")), "image/webp")

    def test_and_the_other_pictures_are_too(self):
        for fmt, mime in (
            ("JPEG", "image/jpeg"),
            ("PNG", "image/png"),
            ("GIF", "image/gif"),
            ("TIFF", "image/tiff"),
        ):
            with self.subTest(format=fmt):
                self.assertEqual(sniff.from_bytes(picture(fmt)), mime)

    def test_a_riff_file_that_is_not_a_picture_is_not_called_one(self):
        """`RIFF` alone is a container. A WAV starts the same way, and calling
        a sound file a photograph would put it in the Photos card."""
        wave = b"RIFF\x24\x00\x00\x00WAVEfmt "

        self.assertEqual(sniff.from_bytes(wave), "")

    def test_heic_and_avif_are_told_apart_by_their_brand(self):
        self.assertEqual(sniff.from_bytes(iso(b"heic")), "image/heic")
        self.assertEqual(sniff.from_bytes(iso(b"avif")), "image/avif")

    def test_an_iso_file_of_some_other_kind_is_not_guessed_at(self):
        """An MP4 is `ftyp` too, and it is not a photograph."""
        self.assertEqual(sniff.from_bytes(iso(b"isom")), "")

    def test_a_pdf_is_recognized(self):
        self.assertEqual(sniff.from_bytes(b"%PDF-1.7\n%\xe2\xe3"), "application/pdf")

    def test_something_unrecognizable_says_nothing(self):
        self.assertEqual(sniff.from_bytes(b"just some words in a file"), "")


class WhatIsRecordedTests(SimpleTestCase):
    def test_the_bytes_beat_a_vague_claim(self):
        """The reported case, exactly: Windows has no registry entry for
        `.webp`, so the browser says it has no idea."""
        self.assertEqual(
            sniff.content_type(
                picture("WEBP"),
                filename="bay.webp",
                claimed="application/octet-stream",
            ),
            "image/webp",
        )

    def test_the_bytes_beat_a_confident_wrong_claim_too(self):
        """A file renamed, or a browser mapping an extension to the wrong
        thing. The file is the only party with nothing to gain."""
        self.assertEqual(
            sniff.content_type(picture("PNG"), filename="photo.jpg", claimed="image/jpeg"),
            "image/png",
        )

    def test_a_claim_stands_where_the_content_is_unknown(self):
        """A spreadsheet is a fact this knows nothing about, and replacing it
        with a guess from the extension would be a downgrade."""
        kind = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

        self.assertEqual(
            sniff.content_type(b"PK\x03\x04zipped", filename="costs.xlsx", claimed=kind),
            kind,
        )

    def test_the_filename_is_the_last_resort(self):
        self.assertEqual(
            sniff.content_type(b"nothing recognizable", filename="notes.txt", claimed=""),
            "text/plain",
        )

    def test_a_charset_on_the_claim_is_not_part_of_the_type(self):
        self.assertEqual(
            sniff.content_type(b"unknowable", filename="x", claimed="text/csv; charset=utf-8"),
            "text/csv",
        )

    def test_and_a_file_nobody_can_identify_keeps_its_vagueness(self):
        """Still stored, still downloadable — it simply gets a labeled tile."""
        self.assertEqual(
            sniff.content_type(b"\x01\x02\x03", filename="x", claimed="application/octet-stream"),
            "application/octet-stream",
        )


class AWebpUploadTests(TestCase):
    """The whole path, from the upload to the card it lands in."""

    def setUp(self):
        self.user = User.objects.create_user(
            username="andy", password="x" * 16, role=Role.ADMIN
        )
        self.client.force_login(self.user)
        self.asset = Asset.objects.create(nickname="Red truck")

    def upload(self, claimed, shade=90):
        return self.client.post(
            reverse("asset_photo_upload", args=[self.asset.pk]),
            {
                "files": SimpleUploadedFile(
                    "bay.webp", picture("WEBP", shade), content_type=claimed
                )
            },
            follow=True,
        )

    def cards(self, response):
        html = response.content.decode()
        photos = html.split("<h2>Photos</h2>")[1].split("</section>")[0]
        documents = html.split('id="documents"')[1].split("</section>")[0]
        return photos, documents

    def test_it_is_filed_as_a_photograph_whatever_the_browser_claimed(self):
        for claimed in ("image/webp", "application/octet-stream", ""):
            with self.subTest(claimed=claimed or "(nothing)"):
                Media.all_objects.all().delete()
                self.upload(claimed)
                media = Media.objects.get()

                self.assertEqual(media.kind, Media.Kind.PHOTO)
                self.assertEqual(media.mime, "image/webp")

    def test_and_it_appears_in_the_photos_card_straight_away(self):
        """The reported symptom. No job has run at this point: a webp is
        something the browser draws, so the original is shown until there is
        a thumbnail to show instead."""
        photos, documents = self.cards(self.upload("application/octet-stream"))

        self.assertIn("<img", photos)
        self.assertNotIn("No photos yet", photos)
        self.assertNotIn("bay.webp", documents)

    def test_it_is_something_a_browser_will_draw(self):
        self.upload("application/octet-stream")
        media = Media.objects.get()

        self.assertTrue(media.has_image_preview)
        self.assertTrue(media.display_url)
        self.assertTrue(media.opens_in_lightbox)

    def test_a_pdf_is_still_a_document(self):
        """The fix must not turn the other half of the split into noise: a
        receipt is a PDF and belongs under Documents."""
        media, _created = ingest(
            SimpleUploadedFile("receipt.pdf", b"%PDF-1.4\n%stuff", content_type="")
        )

        self.assertEqual(media.kind, Media.Kind.DOCUMENT)
        self.assertEqual(media.mime, "application/pdf")


class RefilingWhatIsAlreadyStoredTests(TestCase):
    """`refile_media`, for the files uploaded before the bytes were read."""

    def misfiled(self) -> Media:
        """A row exactly as the old `ingest` would have written it."""
        media, _created = ingest(
            SimpleUploadedFile("bay.webp", picture("WEBP"), content_type="image/webp")
        )
        Media.objects.filter(pk=media.pk).update(
            mime="application/octet-stream", kind=Media.Kind.DOCUMENT
        )
        return Media.objects.get(pk=media.pk)

    def run_command(self, *args) -> str:
        import io as stdio

        from django.core.management import call_command

        out = stdio.StringIO()
        call_command("refile_media", *args, stdout=out)
        return out.getvalue()

    def test_reporting_is_the_default(self):
        media = self.misfiled()

        output = self.run_command()
        media.refresh_from_db()

        self.assertIn("bay.webp", output)
        self.assertIn("image/webp", output)
        self.assertEqual(media.kind, Media.Kind.DOCUMENT)

    def test_and_it_says_how_to_actually_do_it(self):
        self.misfiled()

        self.assertIn("--yes", self.run_command())

    def test_with_yes_it_corrects_the_type_and_the_card(self):
        media = self.misfiled()

        self.run_command("--yes")
        media.refresh_from_db()

        self.assertEqual(media.mime, "image/webp")
        self.assertEqual(media.kind, Media.Kind.PHOTO)

    def test_and_asks_for_the_thumbnail_that_was_never_made(self):
        """Correcting the row without this leaves a photograph that still
        cannot be looked at."""
        from homeautoshop.core.models import Job

        media = self.misfiled()
        Media.objects.filter(pk=media.pk).update(thumb="")
        Job.objects.all().delete()

        self.run_command("--yes")

        self.assertTrue(
            Job.objects.filter(type="media.derive", payload__media_id=str(media.pk)).exists()
        )

    def test_a_file_it_agrees_with_is_left_alone(self):
        media, _created = ingest(
            SimpleUploadedFile("bay.jpg", picture("JPEG"), content_type="image/jpeg")
        )
        stamp = Media.objects.get(pk=media.pk).updated_at

        self.run_command("--yes")

        self.assertEqual(Media.objects.get(pk=media.pk).updated_at, stamp)

    def test_a_file_it_cannot_identify_is_left_alone_too(self):
        """Nothing is re-typed on a guess."""
        media, _created = ingest(
            SimpleUploadedFile("costs.xlsx", b"PK\x03\x04zipped", content_type="application/zip")
        )

        self.run_command("--yes")
        media.refresh_from_db()

        self.assertEqual(media.mime, "application/zip")

    def test_what_a_file_is_for_is_not_re_decided(self):
        """A scan-tool report is a PDF, and `scan_export` is a statement about
        what it is *for* made by the code that stored it. No amount of reading
        the bytes is a better answer to that question."""
        media, _created = ingest(
            SimpleUploadedFile("scan.pdf", b"%PDF-1.4\n%report", content_type=""),
            kind=Media.Kind.SCAN_EXPORT,
        )
        Media.objects.filter(pk=media.pk).update(mime="application/octet-stream")

        self.run_command("--yes")
        media.refresh_from_db()

        self.assertEqual(media.kind, Media.Kind.SCAN_EXPORT)
        self.assertEqual(media.mime, "application/octet-stream")

    def test_a_row_whose_file_is_missing_is_counted_rather_than_crashed_on(self):
        media = self.misfiled()
        media.file.storage.delete(media.file.name)

        output = self.run_command("--yes")

        self.assertIn("could not be opened", output)
