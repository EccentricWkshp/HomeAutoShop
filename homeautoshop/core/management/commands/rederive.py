"""
Re-run derivation for files that have no preview (FR-DOC-3).

Derivation happens once, when a file is uploaded, and that is the right place
for it — but it means anything uploaded before a capability existed keeps the
result it got at the time, for ever. PDFs are the case that prompted this:
they used to get no preview at all, so every receipt already in the instance
would have stayed a labeled tile while only new ones showed their first page.

Deliberately a command rather than a scheduled sweep. A sweep would re-queue
the same permanently-unrenderable files — an encrypted PDF, a HEIC on a box
without pillow-heif — every few hours for the life of the instance, to arrive
at the same answer each time. This is run once, by someone who knows why.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand
from django.db.models import Q

from homeautoshop.core.models import Job
from homeautoshop.mediafiles.models import BROWSER_IMAGE_MIMES, Media

#: Only what could gain a picture. A CSV never will, and queueing it would be
#: work done to reach the conclusion we can already reach here.
COULD_HAVE_A_PREVIEW = Q(mime__in=BROWSER_IMAGE_MIMES) | Q(mime="application/pdf")


class Command(BaseCommand):
    help = "Queue thumbnail generation for stored files that have none."

    def add_arguments(self, parser):
        parser.add_argument(
            "--all",
            action="store_true",
            help="Include files that already have a thumbnail, and rebuild them.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would be queued, and queue nothing.",
        )

    def handle(self, *args, **options):
        media = Media.objects.filter(COULD_HAVE_A_PREVIEW)
        if not options["all"]:
            media = media.filter(Q(thumb="") | Q(thumb__isnull=True))

        pending = set(
            Job.objects.filter(
                type="media.derive", state__in=(Job.State.PENDING, Job.State.RUNNING)
            ).values_list("payload__media_id", flat=True)
        )
        targets = [pk for pk in media.values_list("pk", flat=True) if str(pk) not in pending]

        if not targets:
            self.stdout.write("Nothing to do: every stored file already has its preview.")
            return

        if options["dry_run"]:
            self.stdout.write(f"Would queue {len(targets)} file(s).")
            return

        Job.objects.bulk_create(
            [Job(type="media.derive", payload={"media_id": str(pk)}) for pk in targets]
        )
        self.stdout.write(
            self.style.SUCCESS(f"Queued {len(targets)} file(s). The worker will pick them up.")
        )
