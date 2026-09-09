"""Re-file attachments whose type was taken from the browser's word for it.

    python manage.py refile_media            # report
    python manage.py refile_media --yes      # write

`ingest` used to record the `Content-Type` a multipart upload arrived with, and
decide from it whether a file was a photograph or a document. That claim is
made by the browser, and on Windows it is made by looking the extension up in
the registry — `.webp` and `.avif` frequently have no entry, so the browser
sends `application/octet-stream` for a file whose first twelve bytes say
plainly what it is.

The consequence was not cosmetic. Such a file was filed as a **document**, so
it appeared in the wrong card; and because nothing that is not an image gets a
preview, no thumbnail was ever made for it, so it sat there as a labeled gray
box that could not be looked at without downloading it.

New uploads read the bytes (`mediafiles/sniff.py`). This is for the ones
already stored, and it is the same rule applied to them: only where the file's
own first bytes disagree with what was recorded. Nothing is re-typed on a
guess, and a file this cannot identify is left exactly as it is.

Reporting is the default, as it is for anything that rewrites rows nobody
asked it to touch.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand

from homeautoshop.core.models import Job
from homeautoshop.mediafiles import sniff
from homeautoshop.mediafiles.models import Media
from homeautoshop.mediafiles.services import IMAGE_MIMES

#: Enough for every signature in `sniff`, and short enough that this reads a
#: few kilobytes over an entire library rather than gigabytes.
HEAD = 32


class Command(BaseCommand):
    help = "Correct attachments whose recorded type disagrees with their bytes."

    def add_arguments(self, parser):
        parser.add_argument(
            "--yes", action="store_true", help="Write the corrections."
        )
        parser.add_argument(
            "--limit", type=int, default=0, help="Stop after this many rows."
        )

    def handle(self, *args, **options):
        write = options["yes"]
        limit = options["limit"]

        corrected = 0
        derived = 0
        unreadable = 0
        looked_at = 0

        # Only the two kinds that were ever decided by reading a header.
        # `scan_export` and `audio_note` are statements about what a file is
        # *for*, made by the code that stored it, and no amount of looking at
        # the bytes is a better answer to that question.
        rows = Media.all_objects.filter(
            kind__in=(Media.Kind.PHOTO, Media.Kind.DOCUMENT)
        ).order_by("created_at")
        for media in rows.iterator():
            if limit and looked_at >= limit:
                break
            looked_at += 1
            if not media.file:
                continue
            try:
                with media.file.open("rb") as fh:
                    head = fh.read(HEAD)
            except (OSError, ValueError):
                # A row whose file is not where it says. Counted and skipped:
                # a missing file is a different problem and not one this
                # command should be silently half-fixing.
                unreadable += 1
                continue

            found = sniff.from_bytes(head)
            if not found or found == media.mime:
                continue

            kind = Media.Kind.PHOTO if found in IMAGE_MIMES else Media.Kind.DOCUMENT
            moves = kind != media.kind
            self.stdout.write(
                f"{media.original_filename or media.pk}: "
                f"{media.mime or '(none)'} -> {found}"
                + (f", {media.kind} -> {kind}" if moves else "")
            )
            corrected += 1
            if not write:
                continue

            media.mime = found
            media.kind = kind
            media.save(update_fields=["mime", "kind", "updated_at"])
            # A file that was never an image never had a preview made. Ask for
            # one now, rather than leaving a corrected row that still cannot be
            # looked at.
            if kind == Media.Kind.PHOTO and not media.thumb:
                Job.objects.create(
                    type="media.derive", payload={"media_id": str(media.pk)}
                )
                derived += 1

        self.stdout.write("")
        self.stdout.write(f"{looked_at} attachment(s) read, {corrected} misfiled.")
        if unreadable:
            self.stdout.write(f"{unreadable} could not be opened and were left alone.")
        if not write:
            self.stdout.write("Nothing was changed. Run again with --yes to correct them.")
            return
        self.stdout.write(f"{derived} queued for a thumbnail.")
