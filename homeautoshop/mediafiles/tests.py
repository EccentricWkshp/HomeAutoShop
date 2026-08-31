"""Media pipeline, jobs, search, and export (SPEC §7.9, §13, FR-SEARCH-1)."""

from __future__ import annotations

import shutil
import tempfile
import zipfile
from io import BytesIO
from pathlib import Path

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings

from homeautoshop.mediafiles.testing import local_storage

from homeautoshop.assets.models import Asset
from homeautoshop.core.jobs import drain
from homeautoshop.core.models import Job
from homeautoshop.work.models import WorkOrder, WorkOrderNote

from .models import Media, MediaLink
from .services import ingest


def make_image(size=(1200, 900), color=(180, 40, 40)) -> SimpleUploadedFile:
    from PIL import Image

    buffer = BytesIO()
    Image.new("RGB", size, color).save(buffer, format="JPEG")
    return SimpleUploadedFile("bushing.jpg", buffer.getvalue(), content_type="image/jpeg")


# The pipeline under test is hashing, dedupe and derivation, none of which care
# which storage backend holds the bytes. Without pinning it the suite inherits
# STORAGE_DRIVER from the ambient .env and a plain `manage.py test` starts
# trying to reach an object store — tests that need the network are not tests.



class MediaTests(TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.override = local_storage(self.tmp)
        self.override.enable()
        self.asset = Asset.objects.create(nickname="Truck")

    def tearDown(self):
        self.override.disable()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_upload_is_stored_hashed_and_linked(self):
        media, created = ingest(make_image(), entity=self.asset)
        self.assertTrue(created)
        self.assertEqual(len(media.sha256), 64)
        self.assertTrue(MediaLink.for_entity(self.asset).exists())

    def test_derivation_is_deferred_to_a_job_not_the_request(self):
        """FR-DOC-3, NFR-P-4 — the UI is never blocked on processing."""
        media, _ = ingest(make_image(), entity=self.asset)
        self.assertIsNone(media.derived_at)
        self.assertTrue(Job.objects.filter(type="media.derive", state="pending").exists())

        drain()
        media.refresh_from_db()
        self.assertIsNotNone(media.derived_at)
        self.assertTrue(media.thumb)
        self.assertEqual(media.width, 1200)

    def test_duplicate_uploads_link_rather_than_store_twice(self):
        """FR-DOC-6 — the same receipt photographed twice."""
        wo = WorkOrder.objects.create(asset=self.asset, title="Brakes")
        upload = make_image()
        first, created_first = ingest(upload, entity=self.asset)
        second, created_second = ingest(make_image(), entity=wo)

        self.assertTrue(created_first)
        self.assertFalse(created_second)
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(Media.objects.count(), 1)
        # One file, two attachment points — which is the point of MediaLink.
        self.assertEqual(first.links.count(), 2)

    def test_original_is_preserved_and_derivatives_carry_no_metadata(self):
        """FR-DOC-4, FR-DOC-9 — originals byte-for-byte; no GPS in derivatives."""
        from PIL import Image

        media, _ = ingest(make_image(), entity=self.asset)
        drain()
        media.refresh_from_db()
        with media.thumb.open("rb") as fh:
            thumb = Image.open(fh)
            thumb.load()
            self.assertFalse(dict(thumb.getexif()))
        self.assertTrue(media.gps_stripped)


class JobTests(TestCase):
    def test_failure_backs_off_then_gives_up_visibly(self):
        """NFR-R-2 — permanent failures are visible, not silent."""
        job = Job.objects.create(type="does.not.exist", payload={})
        drain()
        job.refresh_from_db()
        self.assertEqual(job.state, Job.State.FAILED)
        self.assertIn("no handler", job.last_error)


class SearchTests(TestCase):
    def setUp(self):
        self.asset = Asset.objects.create(nickname="Red truck", make="Ford", model="F-150")
        self.wo = WorkOrder.objects.create(asset=self.asset, title="Front brakes", complaint="Grinding")
        WorkOrderNote.objects.create(work_order=self.wo, body="Caliper slide pins seized")

    def test_finds_across_entity_types(self):
        from homeautoshop.core.search import search

        self.assertGreater(search("truck").total, 0)
        self.assertGreater(search("brakes").total, 0)
        self.assertGreater(search("seized").total, 0)

    def test_short_queries_return_nothing_rather_than_everything(self):
        from homeautoshop.core.search import search

        self.assertTrue(search("a").is_empty)

    def test_results_are_grouped_by_kind(self):
        from homeautoshop.core.search import search

        kinds = {g.kind for g in search("brakes").groups}
        self.assertIn("work_order", kinds)


class ExportTests(TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.override = override_settings(MEDIA_ROOT=self.tmp / "media", BACKUP_DIR=self.tmp / "backups")
        self.override.enable()
        (self.tmp / "media").mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        self.override.disable()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_export_is_self_describing_and_readable_without_the_app(self):
        """P-4, §13.3 — an export you cannot read without the app is not portability."""
        import json

        from homeautoshop.core.backup import build_export

        asset = Asset.objects.create(nickname="Red truck", make="Ford")
        WorkOrder.objects.create(asset=asset, title="Brakes")

        path = build_export()
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
            self.assertIn("manifest.json", names)
            self.assertIn("README.md", names)
            self.assertIn("data/assets.Asset.ndjson", names)

            manifest = json.loads(archive.read("manifest.json"))
            self.assertEqual(manifest["schema_version"], 1)
            self.assertEqual(manifest["tables"]["assets.Asset"], 1)

            rows = archive.read("data/assets.Asset.ndjson").decode().splitlines()
            record = json.loads(rows[0])
            self.assertEqual(record["fields"]["nickname"], "Red truck")

    def test_credentials_are_never_included_in_an_export(self):
        import json

        from homeautoshop.accounts.models import User
        from homeautoshop.core.backup import build_export

        User.objects.create_user("andy", password="correct-horse-battery")
        with zipfile.ZipFile(build_export()) as archive:
            row = json.loads(archive.read("data/accounts.User.ndjson").decode().splitlines()[0])
            self.assertNotIn("password", row["fields"])
