"""
`migrate_storage` — moving files between drivers without touching the database
(SPEC §5.1).

Every test here runs the command between two filesystem directories rather than
against a real object store. What is worth defending is the copy loop — that a
file arrives under the exact name its row records, that a second run is a
no-op, that a file the source cannot produce is named rather than silently
dropped — and none of that is S3-specific. A test that needed a live object
store would be a test of boto3, and would be the one test nobody can run.
"""

from __future__ import annotations

import shutil
import tempfile
from io import BytesIO, StringIO
from pathlib import Path
from unittest import mock

from django.core.files.base import ContentFile
from django.core.files.storage import FileSystemStorage
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase
from django.utils import timezone

from homeautoshop.core.management.commands import migrate_storage
from homeautoshop.mediafiles.models import Media
from homeautoshop.mediafiles.testing import local_storage


def jpeg_bytes(color=(180, 40, 40)) -> bytes:
    from PIL import Image

    buffer = BytesIO()
    Image.new("RGB", (24, 24), color).save(buffer, format="JPEG")
    return buffer.getvalue()


class MigrateStorageTests(TestCase):
    def setUp(self):
        self.source_root = Path(tempfile.mkdtemp())
        self.destination_root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.source_root, True)
        self.addCleanup(shutil.rmtree, self.destination_root, True)

        self.source = FileSystemStorage(location=self.source_root)
        self.destination = FileSystemStorage(location=self.destination_root)

        # The command asks for a storage by driver name; here both are
        # directories, so the run exercises the loop rather than botocore.
        patcher = mock.patch.object(
            migrate_storage,
            "build_storage",
            lambda driver: self.source if driver == "s3" else self.destination,
        )
        patcher.start()
        self.addCleanup(patcher.stop)

        # Rows are written with the source as the default storage, so
        # `media.file.name` is genuinely what the source holds.
        self.override = local_storage(self.source_root)
        self.override.enable()
        self.addCleanup(self.override.disable)

    def add(self, data: bytes | None = None) -> Media:
        data = jpeg_bytes() if data is None else data
        media = Media(kind=Media.Kind.PHOTO, mime="image/jpeg", sha256=Media.hash_bytes(data))
        media.file.save("bushing.jpg", ContentFile(data), save=False)
        media.save()
        return media

    def migrate(self, *args) -> str:
        out = StringIO()
        call_command("migrate_storage", "--to", "filesystem", *args, stdout=out)
        return out.getvalue()

    def test_the_file_arrives_under_the_name_the_database_records(self):
        """The point of the command: the store changes, the row does not."""
        media = self.add()
        name = media.file.name

        output = self.migrate()

        self.assertTrue(self.destination.exists(name))
        media.refresh_from_db()
        self.assertEqual(media.file.name, name)
        self.assertIn("Copied 1 file(s)", output)

    def test_the_bytes_arrive_unchanged(self):
        data = jpeg_bytes(color=(20, 90, 160))
        media = self.add(data)

        self.migrate()

        with self.destination.open(media.file.name) as handle:
            self.assertEqual(handle.read(), data)

    def test_a_second_run_copies_nothing(self):
        """Resumable: a run that died halfway is finished by running it again."""
        self.add()
        self.migrate()

        output = self.migrate()

        self.assertIn("Copied 0 file(s)", output)
        self.assertIn("Already at the destination", output)

    def test_a_dry_run_writes_nothing(self):
        media = self.add()

        output = self.migrate("--dry-run")

        self.assertFalse(self.destination.exists(media.file.name))
        self.assertIn("Would copy 1 file(s)", output)

    def test_derivatives_travel_with_the_original(self):
        media = self.add()
        # The same two steps `derive()` takes: Media is append-only, so a
        # derivative is written with save=False and committed by naming it.
        media.thumb.save("thumb.jpg", ContentFile(jpeg_bytes()), save=False)
        media.save(update_fields=["thumb"])

        output = self.migrate()

        self.assertTrue(self.destination.exists(media.thumb.name))
        self.assertIn("Copied 2 file(s)", output)

    def test_a_soft_deleted_row_travels_too(self):
        """A 30-day undelete has to restore the photo, not a broken link."""
        media = self.add()
        Media.all_objects.filter(pk=media.pk).update(deleted_at=timezone.now())

        self.migrate()

        self.assertTrue(self.destination.exists(media.file.name))

    def test_a_file_the_source_does_not_have_is_named_not_skipped_silently(self):
        media = self.add()
        self.source.delete(media.file.name)

        output = self.migrate()

        self.assertIn("missing from s3", output)
        self.assertIn(media.file.name, output)
        self.assertIn("Keep the s3 store", output)

    def test_one_missing_file_does_not_abandon_the_rest(self):
        broken = self.add()
        intact = self.add(jpeg_bytes(color=(10, 10, 10)))
        self.source.delete(broken.file.name)

        self.migrate()

        self.assertTrue(self.destination.exists(intact.file.name))

    def test_bytes_that_do_not_match_the_recorded_hash_are_not_copied(self):
        """"Copied" has to mean "arrived intact", or the report is worthless."""
        media = self.add()
        Media.all_objects.filter(pk=media.pk).update(sha256="0" * 64)

        output = self.migrate()

        self.assertFalse(self.destination.exists(media.file.name))
        self.assertIn("checksum did not match", output)
        self.assertIn("Keep the s3 store", output)

    def test_a_clean_run_says_what_to_do_next(self):
        self.add()

        output = self.migrate()

        self.assertIn("STORAGE_DRIVER=filesystem", output)
        self.assertNotIn("Keep the s3 store", output)

    def test_moving_a_driver_onto_itself_is_refused(self):
        with self.assertRaises(CommandError):
            call_command("migrate_storage", "--to", "s3", "--from", "s3", stdout=StringIO())
