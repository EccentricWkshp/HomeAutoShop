"""
Move stored files from one storage driver to the other (SPEC §5.1).

Media lives under MEDIA_ROOT by default. An operator who wants it somewhere
else — a NAS, rented storage, a host that is not the one running the
application — sets `STORAGE_DRIVER=s3` and points it at an object store of
their own, and this is what carries the files they already have across. Without
it that driver only works on an instance that has never stored anything, which
is not a useful place to offer a choice.

**The database is never touched.** Every file keeps the exact name recorded in
its row, so the only thing that changes is which store answers for it. Three
properties follow, and each is deliberate:

* **Resumable.** A run that dies halfway is finished by running it again:
  anything already at the destination is skipped rather than re-copied.
* **Reversible.** The wrong direction is undone by the opposite direction.
* **Safe.** Nothing is ever deleted from the source. Emptying the old store is
  a decision worth taking deliberately, after reading the report and looking at
  the application, rather than one this command takes for you while you are
  still reading its output.

Both stores have to be reachable while it runs — worth saying, because the
obvious order (reconfigure, restart, then move the files) leaves the source
unreachable at the moment it is needed. Move first, then change
`STORAGE_DRIVER` and restart.
"""

from __future__ import annotations

from django.conf import settings
from django.core.files.base import ContentFile
from django.core.files.storage import FileSystemStorage, Storage
from django.core.management.base import BaseCommand, CommandError

from homeautoshop.mediafiles.models import Media

DRIVERS = ("filesystem", "s3")

#: Every file field on Media. `file` is the immutable original; `thumb` and
#: `preview` are regenerable, and are copied anyway because regenerating them
#: is a queue and a wait for the worker, and copying them is a few megabytes.
FILE_FIELDS = ("file", "thumb", "preview")

#: How many problem files to name before summarising. Long enough to see the
#: shape of a failure, short enough that the useful line at the end is still on
#: the screen.
LISTED = 20


def build_storage(driver: str) -> Storage:
    """A storage for `driver`, independent of what STORAGE_DRIVER currently is.

    Both ends have to be constructible at once, and the configured default is
    only ever one of them — including halfway through the migration, when the
    setting has been flipped but the files have not yet moved.
    """
    if driver == "filesystem":
        return FileSystemStorage(location=settings.MEDIA_ROOT)
    from homeautoshop.mediafiles.storage import S3Storage

    return S3Storage(**settings.S3_OPTIONS)


def other(driver: str) -> str:
    return next(candidate for candidate in DRIVERS if candidate != driver)


class Command(BaseCommand):
    help = "Copy every stored file from one storage driver to the other. Deletes nothing."

    def add_arguments(self, parser):
        parser.add_argument(
            "--to",
            dest="destination",
            choices=DRIVERS,
            required=True,
            help="Where the files are going.",
        )
        parser.add_argument(
            "--from",
            dest="source",
            choices=DRIVERS,
            default=None,
            help="Where they are now. Defaults to the driver --to is not.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help=(
                "Read and verify every file, write none. Not a cheap check: it is the "
                "same read the real run does, so a clean dry run means the source is "
                "intact and readable."
            ),
        )

    def handle(self, *args, **options):
        destination_name = options["destination"]
        source_name = options["source"] or other(destination_name)
        if source_name == destination_name:
            raise CommandError(f"--from and --to are both {source_name}; there is nothing to move.")

        source = build_storage(source_name)
        destination = build_storage(destination_name)
        dry_run = options["dry_run"]

        self.stdout.write(f"{source_name} -> {destination_name}" + (" (dry run)" if dry_run else ""))

        copied = skipped = copied_bytes = 0
        missing: list[str] = []
        mismatched: list[str] = []

        # `all_objects`: a soft-deleted row is still restorable from the trash,
        # so its file has to travel with the rest. Leaving those behind would
        # turn a 30-day undelete into a 30-day undelete of a broken link.
        for media in Media.all_objects.iterator():
            for field_name in FILE_FIELDS:
                name = getattr(media, field_name).name
                if not name:
                    continue
                if destination.exists(name):
                    skipped += 1
                    continue

                try:
                    with source.open(name) as handle:
                        data = handle.read()
                except Exception as exc:
                    # A row pointing at a file the store does not have is a
                    # pre-existing inconsistency, not a reason to abandon the
                    # nine hundred files that are fine. Collected, reported at
                    # the end, and enough to stop the "you may now delete the
                    # source" line from being printed.
                    missing.append(f"{name} ({exc.__class__.__name__})")
                    continue

                # The original is the only one carrying a recorded hash, and
                # the only one that cannot be regenerated. Checking it here is
                # what makes "copied" mean "arrived intact".
                if field_name == "file" and media.sha256 and Media.hash_bytes(data) != media.sha256:
                    mismatched.append(name)
                    continue

                if not dry_run:
                    written = destination.save(name, ContentFile(data))
                    if written != name:
                        # `save()` renames rather than overwrites. `exists()`
                        # just said there was nothing there, so a rename means
                        # the destination changed under the run — stop, rather
                        # than leave a file under a name no row points at.
                        destination.delete(written)
                        raise CommandError(
                            f"{destination_name} stored {name} as {written}. "
                            "Something else is writing to it; nothing further was copied."
                        )

                copied += 1
                copied_bytes += len(data)

        self._report(
            copied=copied,
            copied_bytes=copied_bytes,
            skipped=skipped,
            missing=missing,
            mismatched=mismatched,
            source_name=source_name,
            destination_name=destination_name,
            dry_run=dry_run,
        )

    def _report(self, **r) -> None:
        verb = "Would copy" if r["dry_run"] else "Copied"
        self.stdout.write(f"{verb} {r['copied']} file(s), {r['copied_bytes']:,} bytes.")
        if r["skipped"]:
            self.stdout.write(f"Already at the destination, left alone: {r['skipped']}.")

        for label, rows in (
            (f"missing from {r['source_name']}", r["missing"]),
            ("checksum did not match the database", r["mismatched"]),
        ):
            if not rows:
                continue
            self.stdout.write(self.style.WARNING(f"{len(rows)} file(s) {label}:"))
            for row in rows[:LISTED]:
                self.stdout.write(f"  {row}")
            if len(rows) > LISTED:
                self.stdout.write(f"  ... and {len(rows) - LISTED} more")

        if r["missing"] or r["mismatched"]:
            self.stdout.write(
                self.style.WARNING(
                    f"Keep the {r['source_name']} store: the files above are not accounted for."
                )
            )
        elif r["dry_run"]:
            self.stdout.write("Every file read cleanly. Run again without --dry-run to copy.")
        else:
            self.stdout.write(
                self.style.SUCCESS(
                    f"Every stored file is now in {r['destination_name']}. "
                    f"Set STORAGE_DRIVER={r['destination_name']}, restart, confirm the photos "
                    f"load, and only then delete the {r['source_name']} store."
                )
            )
