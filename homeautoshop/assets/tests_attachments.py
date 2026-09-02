"""
Attachments on a vehicle: what they are called, and how they are added.

Four faults, reported off one screen — a John Deere mower whose Photos card was
empty while Recent activity announced "4 photos".

* **A document was called a photograph.** Every attachment took the title
  "Photo" and every day's worth was counted with the photo plural, so four PDFs
  of a parts manual read as four photographs. The row named the wrong thing and,
  opened, led to files the Photos card had never held.
* **A document had no name of its own.** It arrived as `31P8770110E1.pdf` —
  what the manufacturer's part system called it — so four manuals were four
  indistinguishable serial numbers. Links beside them had carried a label all
  along.
* **Two controls read as two ways to do the same thing.** "Choose files" and
  "Add documents" are a sequence, and nothing said so, or said that anything
  had been chosen.
* **Uploading a photo checked nobody.** `document_upload`, three lines away,
  had always called `require`.
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse

from homeautoshop.accounts.models import AssetAccess, Role, User
from homeautoshop.assets.models import Asset
from homeautoshop.mediafiles.models import Media, MediaLink
from homeautoshop.mediafiles.services import ingest
from homeautoshop.mediafiles.testing import local_storage


class AttachmentCase(TestCase):
    """Media needs somewhere to live, so every case here needs storage."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.storage = local_storage(self.tmp)
        self.storage.enable()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.addCleanup(self.storage.disable)

        self.user = User.objects.create_user("andy", password="correct-horse-battery")
        self.client.force_login(self.user)
        self.asset = Asset.objects.create(nickname="Mower")

    def add_document(self, name: str, *, caption: str = "") -> MediaLink:
        ingest(
            SimpleUploadedFile(name, name.encode(), content_type="application/pdf"),
            entity=self.asset,
            caption=caption,
        )
        return MediaLink.objects.filter(entity_id=self.asset.pk).order_by("-created_at").first()

    def add_photo(self, name: str) -> MediaLink:
        ingest(
            SimpleUploadedFile(name, name.encode(), content_type="image/jpeg"),
            entity=self.asset,
        )
        return MediaLink.objects.filter(entity_id=self.asset.pk).order_by("-created_at").first()

    def story(self) -> list[dict]:
        from .views import _group_media, _timeline

        return _group_media(_timeline(self.asset))


class WhatTheStoryCallsAnAttachmentTests(AttachmentCase):
    def test_a_days_documents_are_counted_as_documents(self):
        """The reported bug. Four PDFs, and the row said "4 photos"."""
        for i in range(4):
            self.add_document(f"manual{i}.pdf")

        story = self.story()

        self.assertEqual(len(story), 1)
        self.assertEqual(story[0]["kind"], "media_group")
        self.assertIn("4", story[0]["title"])
        self.assertIn("document", story[0]["title"])
        self.assertNotIn("photo", story[0]["title"])

    def test_photographs_and_documents_do_not_share_a_group(self):
        """A group carries a noun, so one group cannot hold both."""
        for i in range(2):
            self.add_photo(f"shot{i}.jpg")
        for i in range(3):
            self.add_document(f"manual{i}.pdf")

        titles = sorted(e["title"] for e in self.story())

        self.assertEqual(len(titles), 2)
        self.assertIn("2 photos", titles[0])
        self.assertIn("3 documents", titles[1])

    def test_one_document_is_titled_by_its_file_name(self):
        """A photograph is worth calling "Photo" — `IMG_4032.jpg` says nothing.
        A document's file name is the only thing separating it from the three
        beside it."""
        self.add_document("31P8770110E1.pdf")
        self.assertEqual(self.story()[0]["title"], "31P8770110E1.pdf")

    def test_one_photograph_is_still_called_a_photograph(self):
        self.add_photo("IMG_4032.jpg")
        self.assertEqual(self.story()[0]["title"], "Photo")

    def test_a_name_somebody_gave_it_wins_over_both(self):
        self.add_document("31P8770110E1.pdf", caption="Deck belt diagram")
        self.assertEqual(self.story()[0]["title"], "Deck belt diagram")


class NamingADocumentTests(AttachmentCase):
    """FR-DOC-1 — documents get a title, the way links always have."""

    def test_renaming_shows_the_name_and_keeps_the_file_name_underneath(self):
        link = self.add_document("31P8770110E1.pdf")

        self.client.post(
            reverse("media_rename", args=[link.pk]), {"caption": "Deck belt diagram"}
        )

        link.refresh_from_db()
        self.assertEqual(link.caption, "Deck belt diagram")
        page = self.client.get(reverse("asset_detail", args=[self.asset.pk])).content.decode()
        self.assertIn("Deck belt diagram", page)
        # Still there: it is what you search for on the vendor's site.
        self.assertIn("31P8770110E1.pdf", page)

    def test_clearing_the_name_falls_back_to_the_file_name(self):
        link = self.add_document("31P8770110E1.pdf", caption="Deck belt diagram")

        self.client.post(reverse("media_rename", args=[link.pk]), {"caption": "  "})

        link.refresh_from_db()
        self.assertEqual(link.caption, "")
        page = self.client.get(reverse("asset_detail", args=[self.asset.pk])).content.decode()
        self.assertIn("31P8770110E1.pdf", page)

    def test_the_name_lives_on_the_attachment_not_on_the_file(self):
        """One receipt hangs off both a purchase and a work order (§6.2), and
        what it should be called there is a property of that attachment."""
        link = self.add_document("receipt.pdf")
        other = Asset.objects.create(nickname="Truck")
        second = MediaLink.objects.create(
            media=link.media,
            entity_type="Asset",
            entity_id=other.pk,
            role=MediaLink.Role.RECEIPT,
        )

        self.client.post(reverse("media_rename", args=[link.pk]), {"caption": "Blade order"})

        second.refresh_from_db()
        self.assertEqual(second.caption, "")

    def test_the_card_offers_the_rename(self):
        self.add_document("31P8770110E1.pdf")
        page = self.client.get(reverse("asset_detail", args=[self.asset.pk])).content.decode()
        self.assertIn("Rename", page)

    def test_a_rename_is_a_post(self):
        link = self.add_document("manual.pdf")
        self.assertEqual(self.client.get(reverse("media_rename", args=[link.pk])).status_code, 405)


class WhoMayTouchAnAttachmentTests(AttachmentCase):
    """§12.2a — the object-level half, on the routes that write."""

    def setUp(self):
        super().setUp()
        self.helper = User.objects.create_user(
            "sam", password="correct-horse-battery", role=Role.HELPER
        )
        self.other = Asset.objects.create(nickname="Someone else's truck")
        AssetAccess.objects.create(user=self.helper, asset=self.other, level="write")

    def test_a_helper_cannot_put_photos_on_a_vehicle_they_were_not_given(self):
        """The gap as found: of the two upload routes, only the document one
        checked anything, and they sit three lines apart."""
        self.client.force_login(self.helper)

        response = self.client.post(
            reverse("asset_photo_upload", args=[self.asset.pk]),
            {"files": SimpleUploadedFile("x.jpg", b"jpeg", content_type="image/jpeg")},
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(MediaLink.objects.filter(entity_id=self.asset.pk).count(), 0)

    def test_a_helper_cannot_rename_a_file_on_a_vehicle_they_were_not_given(self):
        link = self.add_document("manual.pdf")
        self.client.force_login(self.helper)

        response = self.client.post(
            reverse("media_rename", args=[link.pk]), {"caption": "mine now"}
        )

        self.assertEqual(response.status_code, 403)
        link.refresh_from_db()
        self.assertEqual(link.caption, "")

    def test_a_helper_cannot_detach_a_file_from_a_vehicle_they_were_not_given(self):
        link = self.add_document("manual.pdf")
        self.client.force_login(self.helper)

        response = self.client.post(reverse("media_unlink", args=[link.pk]))

        self.assertEqual(response.status_code, 403)
        self.assertTrue(MediaLink.objects.filter(pk=link.pk).exists())

    def test_a_helper_may_still_work_on_the_vehicle_they_were_given(self):
        """The check is about which vehicle, not about being a helper."""
        self.client.force_login(self.helper)

        response = self.client.post(
            reverse("asset_photo_upload", args=[self.other.pk]),
            {"files": SimpleUploadedFile("x.jpg", b"jpeg", content_type="image/jpeg")},
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(MediaLink.objects.filter(entity_id=self.other.pk).count(), 1)

    def test_a_helper_may_rename_a_file_on_the_vehicle_they_were_given(self):
        """Naming the manual you just attached is part of working on the
        vehicle, so the route is open to helpers and the grant decides."""
        ingest(
            SimpleUploadedFile("manual.pdf", b"pdf", content_type="application/pdf"),
            entity=self.other,
        )
        link = MediaLink.objects.get(entity_id=self.other.pk)
        self.client.force_login(self.helper)

        response = self.client.post(
            reverse("media_rename", args=[link.pk]), {"caption": "Deck belt diagram"}
        )

        self.assertEqual(response.status_code, 302)
        link.refresh_from_db()
        self.assertEqual(link.caption, "Deck belt diagram")


class AddingFilesReadsAsOneActionTests(AttachmentCase):
    def test_both_forms_declare_what_the_submit_should_say(self):
        """The count comes from the server so it can be translated; the script
        only substitutes it."""
        page = self.client.get(reverse("asset_detail", args=[self.asset.pk])).content.decode()

        self.assertIn("Upload %(n)s photo", page)
        self.assertIn("Upload %(n)s document", page)
        self.assertEqual(page.count("data-upload-submit"), 2)

    def test_pressing_upload_with_nothing_chosen_says_so(self):
        """Silence read as a page that did nothing for no reason."""
        response = self.client.post(
            reverse("asset_document_upload", args=[self.asset.pk]), {}, follow=True
        )

        self.assertContains(response, "Choose a file first")
        self.assertEqual(Media.objects.count(), 0)

    def test_the_photo_form_says_the_same_thing_about_photos(self):
        response = self.client.post(
            reverse("asset_photo_upload", args=[self.asset.pk]), {}, follow=True
        )
        self.assertContains(response, "Choose a photo first")
