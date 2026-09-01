"""Documents, Links, and seeing the report before downloading it.

Three requests, and each turned out to be covering something already slightly
wrong rather than only adding something new:

* **Documents.** Every attachment landed in the Photos grid, because the view
  handed the template `MediaLink.for_entity(asset)` unfiltered. A PDF of the
  title therefore appeared as a blank thumbnail nobody could read. The file
  decides which section it belongs to (FR-DOC-10), not the form it arrived by.
* **Links.** A page about *this vehicle* — the forum thread, the diagram, the
  video — had nowhere to live. `AssetServiceInfoLink` looks similar and is not
  the same thing: it pins one address per configured provider (§8.5).
* **The report.** The button started a download. This is the document you hand
  to a buyer and the one place sensitive specs are deliberately withheld, so
  how complete it looks is the whole question — and that is worth answering
  while there is still time to fill a gap in.
"""

from __future__ import annotations

import io

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse

from homeautoshop.accounts.models import Role, User
from homeautoshop.assets.models import Asset, AssetLink, AssetSpec, SpecGroup
from homeautoshop.mediafiles.models import Media


def a_pdf(name="title.pdf"):
    return SimpleUploadedFile(name, b"%PDF-1.4 not really", content_type="application/pdf")


def a_photo(name="front.png"):
    # A one-pixel PNG, so the ingest path treats it as a photograph.
    raw = (
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
        b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01"
        b"\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
    )
    return SimpleUploadedFile(name, raw, content_type="image/png")


class Base(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="andy", password="x" * 16, role=Role.ADMIN
        )
        self.client.force_login(self.user)
        self.asset = Asset.objects.create(nickname="Aero", make="Ford", model="F-250")
        self.page = reverse("asset_detail", args=[self.asset.pk])


class DocumentsTests(Base):
    def test_a_document_is_attached_and_listed(self):
        self.client.post(
            reverse("asset_document_upload", args=[self.asset.pk]), {"files": a_pdf()}
        )

        self.assertContains(self.client.get(self.page), "title.pdf")

    def test_a_document_does_not_land_in_the_photo_grid(self):
        """The bug underneath the request. `for_entity` returns every
        attachment, and the grid rendered all of them as thumbnails."""
        self.client.post(
            reverse("asset_document_upload", args=[self.asset.pk]), {"files": a_pdf()}
        )

        response = self.client.get(self.page)

        self.assertEqual(len(response.context["documents"]), 1)
        self.assertEqual(response.context["photos"], [])

    def test_and_a_photo_does_not_land_in_documents(self):
        """The same rule from the other side: which section a file goes to is
        read off the file, so uploading a photo through the document form
        still puts it under Photos."""
        self.client.post(
            reverse("asset_document_upload", args=[self.asset.pk]), {"files": a_photo()}
        )

        response = self.client.get(self.page)

        self.assertEqual(len(response.context["photos"]), 1)
        self.assertEqual(response.context["documents"], [])

    def test_a_document_opens_in_its_own_tab(self):
        """FR-DOC-10 — a photograph enlarges in place, a document goes where
        the browser's own viewer can page and search it."""
        self.client.post(
            reverse("asset_document_upload", args=[self.asset.pk]), {"files": a_pdf()}
        )
        media = Media.objects.get()

        page = self.client.get(self.page).content.decode()

        self.assertIn(reverse("media_file", args=[media.pk]), page)
        self.assertIn('rel="noopener noreferrer"', page)

    def test_it_can_be_taken_off_again(self):
        self.client.post(
            reverse("asset_document_upload", args=[self.asset.pk]), {"files": a_pdf()}
        )

        page = self.client.get(self.page).content.decode()

        self.assertIn(reverse("media_unlink", args=[Media.objects.get().links.get().pk]), page)


class LinksTests(Base):
    def add(self, url="https://example.com/thread/1", **extra):
        return self.client.post(
            reverse("asset_link_add", args=[self.asset.pk]), {"url": url, **extra}
        )

    def test_a_link_is_kept_against_the_vehicle(self):
        self.add(label="The fix", notes="post 47 has the trick")

        link = AssetLink.objects.get()
        self.assertEqual(link.asset, self.asset)
        self.assertEqual(link.label, "The fix")

    def test_it_shows_on_the_vehicle(self):
        self.add(label="The fix")
        self.assertContains(self.client.get(self.page), "The fix")

    def test_an_unlabeled_link_is_named_by_where_it_goes(self):
        """The bare URL is a poor label and a truncated one is worse."""
        self.add(url="https://www.ford-trucks.com/forums/12345-no-start")

        self.assertEqual(AssetLink.objects.get().display_label, "www.ford-trucks.com")

    def test_only_http_addresses_are_accepted(self):
        """A URL field is rendered straight into an href, and `javascript:` in
        an href is script execution — so the scheme is checked on the way in
        rather than trusted from a form a POST never goes through."""
        response = self.client.post(
            reverse("asset_link_add", args=[self.asset.pk]),
            {"url": "javascript:alert(1)"},
            follow=True,
        )

        self.assertFalse(AssetLink.objects.exists())
        self.assertContains(response, "starting with http")

    def test_a_link_is_removed(self):
        self.add()
        link = AssetLink.objects.get()

        self.client.post(reverse("asset_link_delete", args=[self.asset.pk, link.pk]))

        self.assertFalse(AssetLink.objects.filter(pk=link.pk).exists())

    def test_a_link_on_another_vehicle_is_not_removable_here(self):
        other = Asset.objects.create(nickname="Barn find")
        link = AssetLink.objects.create(asset=other, url="https://example.com")

        response = self.client.post(
            reverse("asset_link_delete", args=[self.asset.pk, link.pk])
        )

        self.assertEqual(response.status_code, 404)
        self.assertTrue(AssetLink.objects.filter(pk=link.pk).exists())

    def test_it_takes_a_post(self):
        self.assertEqual(
            self.client.get(reverse("asset_link_add", args=[self.asset.pk])).status_code,
            405,
        )


class ReportPreviewTests(Base):
    def test_the_button_leads_to_a_page_not_a_download(self):
        page = self.client.get(self.page)

        self.assertContains(page, reverse("asset_report", args=[self.asset.pk]))
        self.assertNotContains(page, reverse("asset_report_pdf", args=[self.asset.pk]))

    def test_the_page_shows_what_the_document_will_say(self):
        self.asset.vin = "1M8GDM9AXKP042788"
        self.asset.save()

        page = self.client.get(reverse("asset_report", args=[self.asset.pk]))

        self.assertContains(page, "1M8GDM9AXKP042788")
        self.assertContains(page, "Identity")

    def test_and_offers_the_download_from_there(self):
        page = self.client.get(reverse("asset_report", args=[self.asset.pk]))
        self.assertContains(page, reverse("asset_report_pdf", args=[self.asset.pk]))

    def test_the_pdf_is_still_a_pdf(self):
        response = self.client.get(reverse("asset_report_pdf", args=[self.asset.pk]))

        self.assertEqual(response["Content-Type"], "application/pdf")
        self.assertTrue(response.content.startswith(b"%PDF"))
        self.assertIn("attachment", response["Content-Disposition"])

    def test_both_are_drawn_from_the_same_description(self):
        """The reason the refactor was worth doing: a preview rendered from a
        second query is a preview that lies the first time either is edited."""
        from homeautoshop.core.reports import report_sections

        sections = report_sections(self.asset)
        page = self.client.get(reverse("asset_report", args=[self.asset.pk]))

        # Both renderers apply the same rule: a section with no rows and
        # nothing to say about being empty is drawn by neither. Asserting both
        # halves is what makes this a check on their agreement rather than on
        # the page alone.
        for section in sections:
            with self.subTest(section=section.title):
                if section.is_empty and not section.note:
                    self.assertNotContains(page, section.title)
                else:
                    self.assertContains(page, section.title)

    def test_it_says_a_sensitive_spec_was_withheld(self):
        """Said out loud rather than silently done. Somebody handing this to a
        buyer should know the key code is not in it — and somebody who wanted
        it there should find out here rather than from the buyer."""
        AssetSpec.objects.create(
            asset=self.asset, group=SpecGroup.ACCESS, name="Key code", value="X4192"
        )

        page = self.client.get(reverse("asset_report", args=[self.asset.pk]))

        self.assertContains(page, "marked sensitive")
        self.assertNotContains(page, "X4192")

    def test_costs_can_be_left_out_and_the_choice_carries_to_the_pdf(self):
        page = self.client.get(
            reverse("asset_report", args=[self.asset.pk]), {"costs": "0"}
        )

        self.assertFalse(page.context["include_costs"])
        self.assertContains(page, "costs=0")

    def test_an_empty_section_says_so_rather_than_vanishing(self):
        """A gap in the record should read as a gap, here as in the PDF."""
        page = self.client.get(reverse("asset_report", args=[self.asset.pk]))
        self.assertContains(page, "No completed work recorded")

    def test_it_needs_a_login(self):
        self.client.logout()
        response = self.client.get(reverse("asset_report", args=[self.asset.pk]))
        self.assertEqual(response.status_code, 302)


class ReportCsvTests(Base):
    """FR-REP-2 says PDF *and CSV*; only the PDF existed.

    Found by sweeping the SPEC for claims of a portable format rather than by
    tripping over it — the fourth such claim this session that described
    something never built, after schedule template import/export, the
    per-vehicle authorization scaffold, and inspection checklist YAML.
    """

    def rows(self, **params):
        body = self.client.get(
            reverse("asset_report_csv", args=[self.asset.pk]), params
        ).content.decode()
        return [line for line in body.splitlines()]

    def test_it_is_a_csv(self):
        response = self.client.get(reverse("asset_report_csv", args=[self.asset.pk]))

        self.assertEqual(response["Content-Type"], "text/csv")
        self.assertIn("attachment", response["Content-Disposition"])
        self.assertIn(".csv", response["Content-Disposition"])

    def test_it_carries_the_same_sections(self):
        from homeautoshop.core.reports import report_sections

        self.asset.vin = "1M8GDM9AXKP042788"
        self.asset.save()
        body = "\n".join(self.rows())

        for section in report_sections(self.asset):
            if section.is_empty and not section.note:
                continue
            with self.subTest(section=section.title):
                self.assertIn(section.title, body)
        self.assertIn("1M8GDM9AXKP042788", body)

    def test_each_section_keeps_its_own_header_row(self):
        """A vehicle report is six differently shaped tables. Flattening them
        would produce a file whose header row lies about most of its rows."""
        from django.utils import timezone

        from homeautoshop.work.models import WorkOrder

        WorkOrder.objects.create(
            asset=self.asset,
            title="Brakes",
            status="complete",
            completed_at=timezone.now(),
        )
        body = "\n".join(self.rows())

        self.assertIn("Field,Value", body)
        self.assertIn("Date,Meter,Work,Parts", body)

    def test_an_empty_section_says_so_here_too(self):
        """The same skip rule the page and the PDF apply: a section with
        nothing in it and something to say prints the sentence, not a header
        row describing rows that are not there."""
        body = "\n".join(self.rows())

        self.assertIn("No completed work recorded", body)
        self.assertNotIn("Date,Meter,Work,Parts", body)

    def test_leaving_costs_out_leaves_them_out_here_too(self):
        body = "\n".join(self.rows(costs="0"))
        self.assertNotIn("Cost of ownership", body)

    def test_a_sensitive_spec_never_reaches_it(self):
        """The exclusion lives in `report_sections`, so it holds for every
        renderer rather than for the ones that remembered."""
        AssetSpec.objects.create(
            asset=self.asset, group=SpecGroup.ACCESS, name="Key code", value="X4192"
        )

        self.assertNotIn("X4192", "\n".join(self.rows()))

    def test_the_page_offers_it(self):
        page = self.client.get(reverse("asset_report", args=[self.asset.pk]))
        self.assertContains(page, reverse("asset_report_csv", args=[self.asset.pk]))

    def test_it_needs_a_login(self):
        self.client.logout()
        response = self.client.get(reverse("asset_report_csv", args=[self.asset.pk]))
        self.assertEqual(response.status_code, 302)
