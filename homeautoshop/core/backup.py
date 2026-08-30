"""
Backup, restore, and portability (SPEC §13).

Two distinct things live here, and conflating them is a common mistake:

* **Backup** is for getting *this* instance back after a disk dies. It is
  database-native and fast.
* **Export** is for owning your data (P-4). It is a self-describing ZIP of
  newline-delimited JSON plus every media file, and it must be usable
  **without this application**. That is the point.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import zipfile
from datetime import timedelta
from pathlib import Path

from django.apps import apps
from django.conf import settings
from django.core import serializers
from django.db import connection
from django.utils import timezone

log = logging.getLogger(__name__)


class BackupFailed(RuntimeError):
    """The database half of a backup did not happen.

    Raised instead of letting `CalledProcessError` escape, because the exit
    status alone does not tell an operator what to do and the backup is the
    one subsystem where a silent, unexplained failure costs the most.
    """

EXPORTED_APPS = ("core", "accounts", "people", "assets", "work", "mediafiles")
SCHEMA_VERSION = 1

# Never leaves the instance in an export: password hashes and token hashes are
# credentials, not data the operator needs in a portable archive.
REDACTED_FIELDS = {
    "accounts.User": ("password",),
    "accounts.ApiToken": ("token_hash",),
}


def _iter_models():
    for app_label in EXPORTED_APPS:
        for model in apps.get_app_config(app_label).get_models():
            yield model


def _serialize(model) -> list[dict]:
    manager = getattr(model, "all_objects", model._default_manager)
    rows = json.loads(serializers.serialize("json", manager.all()))
    label = f"{model._meta.app_label}.{model.__name__}"
    for row in rows:
        for field_name in REDACTED_FIELDS.get(label, ()):
            row["fields"].pop(field_name, None)
    return rows


def build_export(destination: Path | None = None) -> Path:
    """Produce a portable ZIP: NDJSON per table, every media file, a manifest.

    The README inside is not decoration — an export you cannot read without the
    app that wrote it is not portability.
    """
    stamp = timezone.now()
    destination = destination or (settings.BACKUP_DIR / f"export-{stamp:%Y%m%d-%H%M%S}.zip")
    destination.parent.mkdir(parents=True, exist_ok=True)

    manifest = {
        "application": "HomeAutoShop",
        "schema_version": SCHEMA_VERSION,
        "exported_at": stamp.isoformat(),
        "shop_name": settings.SHOP_NAME,
        "tables": {},
        "media_files": 0,
    }

    with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as archive:
        for model in _iter_models():
            label = f"{model._meta.app_label}.{model.__name__}"
            rows = _serialize(model)
            archive.writestr(
                f"data/{label}.ndjson",
                "\n".join(json.dumps(r, default=str) for r in rows),
            )
            manifest["tables"][label] = len(rows)

        media_root = Path(settings.MEDIA_ROOT)
        if media_root.exists():
            for path in media_root.rglob("*"):
                if path.is_file():
                    archive.write(path, f"media/{path.relative_to(media_root).as_posix()}")
                    manifest["media_files"] += 1

        archive.writestr("manifest.json", json.dumps(manifest, indent=2))
        archive.writestr("README.md", _EXPORT_README.format(**manifest))

    log.info("export written to %s", destination)
    return destination


_EXPORT_README = """# HomeAutoShop export

Exported {exported_at} from "{shop_name}" (schema version {schema_version}).

## Layout

- `data/<app>.<Model>.ndjson` — one JSON object per line, in Django's
  serialization format: `{{"model": ..., "pk": ..., "fields": {{...}}}}`.
- `media/` — every uploaded original and derivative, at the same relative
  paths recorded in `mediafiles.Media.file`.
- `manifest.json` — row counts per table and the media file count.

## Reading this without HomeAutoShop

The NDJSON files are plain text. Every record carries a UUIDv7 primary key, and
foreign keys are those UUIDs, so the data can be loaded into any store without
this application. Photos and documents are ordinary files; nothing is in a
proprietary container.

## What is not here

Password hashes and API token hashes are omitted deliberately — they are
credentials, not your data. Restoring an instance from a backup (rather than
this export) preserves logins.
"""


def run_backup() -> Path:
    """Take a backup of the database and media.

    On Postgres the database half should be `pg_dump`; on SQLite the file is
    copied under a lock. Media is synced separately because it dominates
    storage by two orders of magnitude (C-3).
    """
    stamp = timezone.now()
    target = settings.BACKUP_DIR / f"{stamp:%Y%m%d-%H%M%S}"
    target.mkdir(parents=True, exist_ok=True)

    if connection.vendor == "sqlite":
        source = Path(connection.settings_dict["NAME"])
        if source.exists():
            with connection.cursor() as cursor:
                cursor.execute("PRAGMA wal_checkpoint(FULL);")
            shutil.copy2(source, target / "database.sqlite3")
    else:
        # pg_dump runs in the container that has the client binaries.
        cfg = connection.settings_dict
        dump = target / "database.dump"
        env_password = {"PGPASSWORD": cfg.get("PASSWORD") or ""}
        result = subprocess.run(
            [
                "pg_dump",
                "--format=custom",
                f"--dbname={cfg['NAME']}",
                f"--host={cfg['HOST']}",
                f"--port={cfg['PORT'] or 5432}",
                f"--username={cfg['USER']}",
                f"--file={dump}",
            ],
            capture_output=True,
            text=True,
            env={**os.environ, **env_password},
        )
        if result.returncode != 0:
            # The message this discards is the one that matters. pg_dump
            # refuses to read a server newer than itself, so a Postgres major
            # upgrade breaks backups while everything else keeps working — and
            # a bare exit status turns that into a mystery. The nightly job
            # would have gone on failing silently until someone needed it.
            detail = result.stderr.strip() or f"exit status {result.returncode}"
            log.error("pg_dump failed: %s", detail)
            raise BackupFailed(detail)

    media_root = Path(settings.MEDIA_ROOT)
    if media_root.exists():
        shutil.copytree(media_root, target / "media", dirs_exist_ok=True)

    (target / "manifest.json").write_text(
        json.dumps(
            {
                "created_at": stamp.isoformat(),
                "vendor": connection.vendor,
                "schema_version": SCHEMA_VERSION,
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    from .models import AuditLog, Setting

    Setting.put("last_backup_at", stamp.isoformat())
    AuditLog.objects.create(
        entity_type="Backup", action=AuditLog.Action.BACKUP, summary=str(target), source="system"
    )
    prune_backups()
    return target


def prune_backups() -> int:
    """Grandfather-father-son retention (SPEC §13.1)."""
    root = settings.BACKUP_DIR
    if not root.exists():
        return 0
    runs = sorted((p for p in root.iterdir() if p.is_dir()), reverse=True)
    keep: set[Path] = set(runs[: settings.BACKUP_RETENTION_DAILY])

    seen_weeks: set[str] = set()
    seen_months: set[str] = set()
    for run in runs:
        try:
            when = timezone.datetime.strptime(run.name, "%Y%m%d-%H%M%S")
        except ValueError:
            keep.add(run)
            continue
        week = f"{when.isocalendar().year}-{when.isocalendar().week}"
        month = f"{when.year}-{when.month}"
        if week not in seen_weeks and len(seen_weeks) < settings.BACKUP_RETENTION_WEEKLY:
            seen_weeks.add(week)
            keep.add(run)
        if month not in seen_months and len(seen_months) < settings.BACKUP_RETENTION_MONTHLY:
            seen_months.add(month)
            keep.add(run)

    removed = 0
    for run in runs:
        if run not in keep:
            shutil.rmtree(run, ignore_errors=True)
            removed += 1
    return removed


def last_backup_age_days() -> float | None:
    """None when no backup has ever run — which the dashboard says loudly."""
    from .models import Setting

    raw = Setting.get("last_backup_at")
    if not raw:
        return None
    when = timezone.datetime.fromisoformat(raw)
    if timezone.is_naive(when):
        when = timezone.make_aware(when)
    return (timezone.now() - when) / timedelta(days=1)
