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
from django.utils.translation import gettext_lazy as _
from .runtime import conf

log = logging.getLogger(__name__)


class BackupFailed(RuntimeError):
    """The database half of a backup did not happen.

    Raised instead of letting `CalledProcessError` escape, because the exit
    status alone does not tell an operator what to do and the backup is the
    one subsystem where a silent, unexplained failure costs the most.
    """

EXPORTED_APPS = ("core", "accounts", "people", "assets", "work", "mediafiles")
SCHEMA_VERSION = 1

#: What a database file has to start with to be the thing it claims to be.
#: Checked because the alternative is discovering it at `pg_restore`, after the
#: running database has already been dropped.
DUMP_MAGIC = {
    "postgresql": (b"PGDMP", "database.dump"),
    "sqlite": (b"SQLite format 3\x00", "database.sqlite3"),
}


class UploadRejected(ValueError):
    """The offered files are not a backup this instance could restore."""


def assemble_uploaded(dump, export=None) -> tuple[Path, list[str]]:
    """Rebuild a restorable backup directory from the two halves the UI hands out.

    There is a loop in this application that does not close on its own, and
    this is it. `backup_download` streams a backup folder's *database file*
    alone — the media tree is gigabytes and is meant to be copied, not
    downloaded — and an export downloads as its own ZIP. Neither carries
    `manifest.json`, because the manifest lives in the folder rather than in
    either file. So the two artifacts the Backup screen gives you are exactly
    the two artifacts `manage.py restore` refuses, and moving an instance to a
    new machine meant hand-writing a manifest after reading the source.

    Given the same two files back, this writes the folder that was taken apart:
    the database file under the name the vendor expects, `media/` out of the
    export, and the manifest neither of them carried.

    Returns the folder and a list of things the operator should know — empty
    when there is nothing worth saying. Restoring is still a command they run
    themselves; nothing here touches the live database.
    """
    magic, dump_name = DUMP_MAGIC.get(connection.vendor, (None, "database.dump"))
    head = dump.read(len(magic) if magic else 16)
    dump.seek(0)
    if magic and not head.startswith(magic):
        raise UploadRejected(
            _(
                "That file does not start like a %(vendor)s dump. A backup taken from a "
                "different database engine cannot be restored here — the portable export "
                "is the path between engines."
            )
            % {"vendor": connection.vendor}
        )

    notes: list[str] = []
    schema_version = SCHEMA_VERSION
    created_at = timezone.now().isoformat()
    media_members: list[zipfile.ZipInfo] = []
    archive: zipfile.ZipFile | None = None

    if export is not None:
        try:
            archive = zipfile.ZipFile(export)
        except zipfile.BadZipFile as exc:
            raise UploadRejected(_("That export is not a readable ZIP: %(why)s") % {"why": exc}) from exc
        try:
            manifest = json.loads(archive.read("manifest.json"))
        except KeyError as exc:
            raise UploadRejected(
                _("That ZIP has no manifest.json, so it is not an export from this application.")
            ) from exc
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise UploadRejected(_("That export's manifest.json is unreadable: %(why)s") % {"why": exc}) from exc

        # The export is the only evidence available about which schema the
        # dump beside it was taken under, since a dump carries no version this
        # can read. Believing it is better than asserting the running one,
        # which would defeat the check `restore` makes for exactly this case.
        schema_version = manifest.get("schema_version", SCHEMA_VERSION)
        created_at = manifest.get("exported_at") or created_at
        media_members = [
            info
            for info in archive.infolist()
            if not info.is_dir() and info.filename.startswith("media/")
        ]
        if not media_members:
            notes.append(_("The export held no media, so this backup restores the database only."))
    else:
        notes.append(
            _(
                "No export was supplied, so there is no media to restore and no way to "
                "tell which schema version the dump was taken under. It is recorded as "
                "this build's, which means restore cannot catch a dump that is too old."
            )
        )

    stamp = timezone.now()
    target = Path(settings.BACKUP_DIR) / f"{stamp:%Y%m%d-%H%M%S}-uploaded"
    target.mkdir(parents=True, exist_ok=True)

    with open(target / dump_name, "wb") as handle:
        for chunk in iter(lambda: dump.read(1024 * 1024), b""):
            handle.write(chunk)

    if archive is not None:
        media_root = (target / "media").resolve()
        for info in media_members:
            # Zip entries are attacker-controlled paths. `..` in one of them
            # writes wherever it likes, and this archive arrived over an upload
            # form. Resolve first, then refuse anything that left the folder.
            destination = (media_root / Path(info.filename).relative_to("media")).resolve()
            if not destination.is_relative_to(media_root):
                raise UploadRejected(_("That export contains an unsafe path: %(name)s") % {"name": info.filename})
            destination.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info) as source, open(destination, "wb") as handle:
                shutil.copyfileobj(source, handle)
        archive.close()

    (target / "manifest.json").write_text(
        json.dumps(
            {
                "created_at": created_at,
                "vendor": connection.vendor,
                "schema_version": schema_version,
                "media": "included" if media_members else "none",
                # Not written by `run_backup`. It says this folder was put back
                # together from downloads rather than taken here, which is the
                # first thing worth knowing when one of these restores oddly.
                "assembled_from_upload": True,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return target, notes

# Never leaves the instance in an export: password hashes and token hashes are
# credentials, not data the operator needs in a portable archive.
REDACTED_FIELDS = {
    "accounts.User": ("password",),
    "accounts.ApiToken": ("token_hash",),
}

# Skipped outright rather than field-redacted (§17.1). Integration credentials
# live in their own table precisely so this can be a whole-table decision: the
# alternative is per-field surgery on every row and one forgotten column.
EXCLUDED_MODELS = {"core.Credential"}

#: The database table behind them, for the physical backup below, where there
#: are no models to skip — only a dump.
EXCLUDED_TABLES = ("core_credential",)


def _iter_models():
    for app_label in EXPORTED_APPS:
        for model in apps.get_app_config(app_label).get_models():
            if f"{model._meta.app_label}.{model.__name__}" in EXCLUDED_MODELS:
                continue
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
        "shop_name": conf.SHOP_NAME,
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

Password hashes, API token hashes, and the integration credentials you entered
in the settings screen are all omitted deliberately — they are credentials, not
your data. An instance built from this archive will need each integration
re-authenticated, and it says which ones on the first screen after a restore.

Restoring from a *backup* (rather than this export) preserves logins. It does
not preserve integration credentials either, and for the same reason.
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
            copy = target / "database.sqlite3"
            shutil.copy2(source, copy)
            # The **copy** is stripped, never the live file. Deleting from the
            # running database to make a clean backup would be a spectacular
            # way to lose every credential in the shop.
            _strip_credentials(copy)
    else:
        # pg_dump runs in the container that has the client binaries.
        cfg = connection.settings_dict
        dump = target / "database.dump"
        env_password = {"PGPASSWORD": cfg.get("PASSWORD") or ""}
        result = subprocess.run(
            [
                "pg_dump",
                "--format=custom",
                # The schema is kept and the rows are not (§17.1). A physical
                # dump excludes nothing by default, so a credential entered in
                # the UI would otherwise ride along to whatever NAS or laptop
                # this archive is carried to.
                *[f"--exclude-table-data={table}" for table in EXCLUDED_TABLES],
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

    # This copies MEDIA_ROOT and nothing else, so with an object store selected
    # the photos are not in here. Left unsaid, such an archive looks complete
    # and the gap appears at a restore, which is the one moment it cannot be
    # closed — so it is said here, and recorded in the manifest for the restore
    # to say again.
    media_external = settings.STORAGE_DRIVER != "filesystem"
    if media_external:
        log.warning(
            "STORAGE_DRIVER=%s: photos and documents are in the object store and are "
            "NOT in this backup. Back that store up separately, or bring the files "
            "onto the filesystem with `manage.py migrate_storage --to filesystem`.",
            settings.STORAGE_DRIVER,
        )

    (target / "manifest.json").write_text(
        json.dumps(
            {
                "created_at": stamp.isoformat(),
                "vendor": connection.vendor,
                "schema_version": SCHEMA_VERSION,
                "media": "external" if media_external else "included",
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


def _strip_credentials(database: Path) -> None:
    """Empty the credential table in a copied SQLite file (§17.1)."""
    import sqlite3

    handle = None
    try:
        handle = sqlite3.connect(database)
        for table in EXCLUDED_TABLES:
            handle.execute(f"DELETE FROM {table}")  # noqa: S608 - fixed names
        handle.commit()
    except sqlite3.Error as exc:
        # A backup containing credentials is worse than no backup: it is one
        # that quietly breaks the promise made on the settings screen.
        log.error("could not strip credentials from %s: %s", database, exc)
        if handle is not None:
            handle.close()
            handle = None
        database.unlink(missing_ok=True)
        raise BackupFailed(f"credentials could not be excluded from the backup: {exc}") from exc
    finally:
        # `with sqlite3.connect(...)` commits; it does **not** close. The
        # connection outlived the block, and on Windows an open handle stops
        # the file being deleted — so a backup could be taken and then not
        # pruned, or not removed from the backup screen, with nothing raised
        # anywhere to say why.
        if handle is not None:
            handle.close()


def prune_backups() -> int:
    """Grandfather-father-son retention (SPEC §13.1)."""
    root = settings.BACKUP_DIR
    if not root.exists():
        return 0
    runs = sorted((p for p in root.iterdir() if p.is_dir()), reverse=True)
    keep: set[Path] = set(runs[: conf.BACKUP_RETENTION_DAILY])

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
        if week not in seen_weeks and len(seen_weeks) < conf.BACKUP_RETENTION_WEEKLY:
            seen_weeks.add(week)
            keep.add(run)
        if month not in seen_months and len(seen_months) < conf.BACKUP_RETENTION_MONTHLY:
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
