"""
Getting an uploaded file back out again (SPEC FR-DOC-8, §12.3).

Every photo on every page was a broken image, and the reason is worth a test of
its own: object storage signs a URL against the endpoint the *application*
talks to, and in Compose that is `http://storage:9000` — a hostname that exists
only on the container network. Nothing in the test suite noticed, because
nothing in the test suite had ever looked at what a page put in a `src`.

So the assertion that matters here is not "the view returns 200". It is **the
container's own hostname never reaches a template**.
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from django.core.files.base import ContentFile
from django.test import TestCase, override_settings
from django.urls import reverse

from homeautoshop.accounts.models import User
from homeautoshop.assets.models import Asset
from homeautoshop.mediafiles.models import Media, MediaLink
from homeautoshop.mediafiles.testing import STATICFILES, UNREACHABLE_S3, LocalMediaMixin

VIN = "1M8GDM9AXKP042788"

class LocalMedia(LocalMediaMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.user = User.objects.create_user(username="andy", password="x" * 16)
        self.client.force_login(self.user)

        self.media = Media(kind=Media.Kind.PHOTO, original_filename="bushing.jpg", mime="image/jpeg")
        self.media.file.save("bushing.jpg", ContentFile(b"pretend-jpeg-bytes"), save=False)
        self.media.save()


class ServingTests(LocalMedia):
    def test_a_file_comes_back_with_its_own_bytes(self):
        response = self.client.get(reverse("media_file", args=[self.media.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(b"".join(response.streaming_content), b"pretend-jpeg-bytes")

    def test_it_is_shown_rather_than_downloaded(self):
        """This is a photo on a page, not a file somebody asked to save."""
        response = self.client.get(reverse("media_file", args=[self.media.pk]))
        self.assertTrue(response["Content-Disposition"].startswith("inline"))
        self.assertEqual(response["Content-Type"], "image/jpeg")

    def test_reading_a_photo_needs_a_login(self):
        """The reason this route is worth the bytes it costs.

        A presigned URL is a bearer token in a querystring: copied out of an
        address bar it works for anybody, signed in or not.
        """
        self.client.logout()
        response = self.client.get(reverse("media_file", args=[self.media.pk]))
        self.assertNotEqual(response.status_code, 200)

    def test_it_is_not_cached_where_somebody_else_could_read_it(self):
        response = self.client.get(reverse("media_file", args=[self.media.pk]))
        self.assertIn("private", response["Cache-Control"])

    def test_a_row_whose_file_is_gone_is_a_404_not_a_crash(self):
        """A bucket emptied by hand outlives the row that points into it."""
        Path(self.media.file.path).unlink()
        self.assertEqual(
            self.client.get(reverse("media_file", args=[self.media.pk])).status_code, 404
        )

    def test_an_invented_variant_is_refused(self):
        self.assertEqual(
            self.client.get(reverse("media_file_variant", args=[self.media.pk, "raw"])).status_code,
            404,
        )

    def test_a_missing_thumbnail_falls_back_rather_than_404ing(self):
        """Derivation is a background job, so a page can render first."""
        response = self.client.get(reverse("media_file_variant", args=[self.media.pk, "thumb"]))
        self.assertEqual(response.status_code, 200)


class NoInternalHostnamesTests(LocalMedia):
    """The actual bug, asserted against a page rather than against a helper."""

    @override_settings(STORAGES=UNREACHABLE_S3)
    def test_a_url_never_names_the_container_network(self):
        self.assertNotIn("storage:9000", self.media.url_for())
        self.assertNotIn("storage:9000", self.media.display_url)

    @override_settings(STORAGES=UNREACHABLE_S3)
    def test_and_neither_does_the_page_the_photo_is_on(self):
        asset = Asset.objects.create(nickname="Red truck", vin=VIN)
        MediaLink.objects.create(
            media=self.media,
            entity_type="Asset",
            entity_id=asset.pk,
            role=MediaLink.Role.OTHER,
        )
        page = self.client.get(reverse("asset_detail", args=[asset.pk])).content.decode()
        self.assertNotIn("storage:9000", page)
        self.assertIn(reverse("media_file", args=[self.media.pk]), page)

    def test_an_operator_who_published_their_object_store_gets_the_direct_route(self):
        """The redirect is what the `s3` driver is for; it just cannot be the default."""
        published = {"staticfiles": STATICFILES, "default": dict(UNREACHABLE_S3["default"])}
        published["default"]["OPTIONS"] = dict(UNREACHABLE_S3["default"]["OPTIONS"])
        published["default"]["OPTIONS"]["public_endpoint"] = "https://files.example.com"
        with override_settings(STORAGES=published):
            from homeautoshop.mediafiles import views

            self.assertEqual(views._public_endpoint(), "https://files.example.com")

    def test_but_a_public_endpoint_equal_to_the_internal_one_is_not_one(self):
        """`public_endpoint` defaults to the internal address in the backend.
        Treating that as "published" is how this bug got shipped."""
        from homeautoshop.mediafiles import views

        with override_settings(STORAGES=UNREACHABLE_S3):
            self.assertEqual(views._public_endpoint(), "")
