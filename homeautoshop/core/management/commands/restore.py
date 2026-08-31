"""
Restore from a backup (SPEC §13.2).

The disaster-recovery runbook is scenario 7 in the spec: the NAS dies, a new
machine comes up, `docker compose up`, restore last night's backup, and the
shop is back — including every photo.

Two properties matter more than speed here:

* **Refuse rather than half-restore.** A partial restore is worse than none,
  because it looks like it worked.
* **Say what will happen before doing it.** `--dry-run` is the default posture
  in the docs, and the command refuses to overwrite a populated instance
  without `--force`.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import connection

from homeautoshop.core.backup import SCHEMA_VERSION


class Command(BaseCommand):
    help = "Restore the database and media from a backup directory (SPEC §13.2)."

    def add_arguments(self, parser):
        parser.add_argument("source", type=Path, help="Backup directory, e.g. data/backups/20260830-020000")
        parser.add_argument("--force", action="store_true", help="Overwrite a populated instance.")
        parser.add_argument("--dry-run", action="store_true", help="Report what would happen and stop.")
        parser.add_argument("--skip-media", action="store_true")

    def handle(self, *args, **options):
        source: Path = options["source"]
        if not source.is_dir():
            raise CommandError(f"{source} is not a directory")

        manifest_path = source / "manifest.json"
        if not manifest_path.exists():
            raise CommandError(
                f"{source} has no manifest.json — that is not a HomeAutoShop backup."
            )
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

        if manifest.get("schema_version") != SCHEMA_VERSION:
            raise CommandError(
                f"Backup is schema version {manifest.get('schema_version')}, "
                f"this build expects {SCHEMA_VERSION}. Restore with a matching release."
            )

        vendor = manifest.get("vendor")
        if vendor != connection.vendor:
            raise CommandError(
                f"Backup was taken from {vendor}, but this instance runs "
                f"{connection.vendor}. Use the portable export/import path instead."
            )

        dump = source / ("database.sqlite3" if vendor == "sqlite" else "database.dump")
        if not dump.exists():
            raise CommandError(f"No database file in {source} (expected {dump.name}).")

        media_source = source / "media"
        has_media = media_source.is_dir() and not options["skip_media"]

        self.stdout.write(f"Backup taken:  {manifest.get('created_at')}")
        self.stdout.write(f"Database:      {dump.name} ({dump.stat().st_size:,} bytes)")
        self.stdout.write(
            f"Media:         {'yes' if has_media else 'skipped'}"
            + (f" ({sum(1 for p in media_source.rglob('*') if p.is_file()):,} files)" if has_media else "")
        )

        # Said before the restore rather than discovered after it. A backup
        # taken with an object store selected holds the database and whatever
        # happened to be under MEDIA_ROOT — not the photos. Restoring it looks
        # like a success and produces a shop whose pictures are all missing.
        if manifest.get("media") == "external":
            self.stdout.write(
                self.style.WARNING(
                    "This backup was taken with STORAGE_DRIVER set to an object store, so "
                    "the photos and documents are NOT in it. Restore that store from its "
                    "own backup as well, or the service history comes back without them."
                )
            )

        occupied = self._instance_has_data()
        if occupied and not options["force"]:
            raise CommandError(
                "This instance already contains data. Re-run with --force if you intend "
                "to replace it — restoring over live data is not something to do by accident."
            )

        if options["dry_run"]:
            self.stdout.write(self.style.WARNING("Dry run — nothing was changed."))
            return

        if vendor == "sqlite":
            target = Path(connection.settings_dict["NAME"])
            connection.close()
            target.parent.mkdir(parents=True, exist_ok=True)
            for suffix in ("-wal", "-shm"):
                stale = target.with_name(target.name + suffix)
                stale.unlink(missing_ok=True)
            shutil.copy2(dump, target)
        else:
            cfg = connection.settings_dict
            connection.close()
            import os

            subprocess.run(
                [
                    "pg_restore",
                    "--clean",
                    "--if-exists",
                    "--no-owner",
                    f"--dbname={cfg['NAME']}",
                    f"--host={cfg['HOST']}",
                    f"--port={cfg['PORT'] or 5432}",
                    f"--username={cfg['USER']}",
                    str(dump),
                ],
                check=True,
                env={**os.environ, "PGPASSWORD": cfg.get("PASSWORD") or ""},
            )

        if has_media:
            media_root = Path(settings.MEDIA_ROOT)
            media_root.mkdir(parents=True, exist_ok=True)
            shutil.copytree(media_source, media_root, dirs_exist_ok=True)

        self.stdout.write(self.style.SUCCESS(f"Restored from {source}."))
        self.stdout.write("Run `manage.py migrate` if this backup predates the running build.")

    @staticmethod
    def _instance_has_data() -> bool:
        from homeautoshop.assets.models import Asset
        from homeautoshop.work.models import WorkOrder

        try:
            return Asset.all_objects.exists() or WorkOrder.all_objects.exists()
        except Exception:
            # No tables yet: an empty instance is exactly what we want to restore into.
            return False
