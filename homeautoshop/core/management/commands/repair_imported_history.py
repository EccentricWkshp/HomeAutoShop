"""Correct two things the LubeLogger importer used to record wrongly.

    python manage.py repair_imported_history          # report
    python manage.py repair_imported_history --yes    # write

**Imported jobs were stamped as completed the moment they were imported.** The
importer set `opened_at` to the date on the record and `completed_at` to
`timezone.now()`. Nothing else in the application writes history that way — the
CSV importer beside it stamps both with the record's own date — and the printed
vehicle report orders and dates its service history by `completed_at`. So an
eleven-year history imported on a Sunday afternoon printed as eleven years of
work completed that Sunday afternoon, in no particular order, on the one
document you would hand to a buyer.

**Imported readings were filed under the odometer whatever the meter was.**
Three call sites passed the literal string "odometer" while everything else
that writes a reading uses `asset.meter`. On an hour-metered machine that made
two series under two names on one asset.

Both writers are fixed. This is for what they already wrote, and it is
deliberately narrow at both ends:

* A job is only corrected when its completion stamp is within a day of the
  moment the row was created, which is the signature of the bug. A job somebody
  reopened and genuinely finished later has a completion date that means
  something, and it is left alone.
* A reading is only relabeled when its own unit still matches the asset's. A
  different unit means the asset's meter was changed after the import, and
  renaming the series would then be filing miles under hours.

Reporting is the default, as it is for anything that rewrites rows nobody asked
it to touch.
"""

from __future__ import annotations

from datetime import timedelta

from django.core.management.base import BaseCommand

from homeautoshop.assets.models import UsageReading
from homeautoshop.core.integrations.lubelogger import SOURCE
from homeautoshop.core.models import ExternalRef
from homeautoshop.work.models import WorkOrder

#: How far a completion stamp may sit from the row's creation and still be read
#: as "stamped at import" rather than "somebody finished this later".
IMPORT_DRIFT = timedelta(hours=24)


class Command(BaseCommand):
    help = "Correct completion dates and meter names on rows imported from LubeLogger."

    def add_arguments(self, parser):
        parser.add_argument("--yes", action="store_true", help="Write the corrections.")

    def handle(self, *args, **options):
        write = options["yes"]

        jobs = self._work_orders(write)
        readings = self._readings(write)

        self.stdout.write("")
        self.stdout.write(
            f"{jobs} imported job(s) completed on the day of the import, "
            f"{readings} reading(s) under the wrong meter."
        )
        if not write and (jobs or readings):
            self.stdout.write("Nothing was changed. Run again with --yes to correct them.")

    # -- the two repairs -------------------------------------------------

    def _work_orders(self, write: bool) -> int:
        imported = ExternalRef.objects.filter(
            source_system=SOURCE, entity_type="WorkOrder"
        ).values_list("entity_id", flat=True)
        found = 0
        for wo in WorkOrder.all_objects.filter(pk__in=list(imported)).order_by("opened_at"):
            if not (wo.completed_at and wo.opened_at):
                continue
            if wo.completed_at.date() == wo.opened_at.date():
                continue
            if abs(wo.completed_at - wo.created_at) > IMPORT_DRIFT:
                # Completed by hand, well after the import. That date is a
                # fact about the shop, not an artifact of the importer.
                continue
            found += 1
            self.stdout.write(
                f"{wo.number} {wo.title[:48]}: completed "
                f"{wo.completed_at.date()} -> {wo.opened_at.date()}"
            )
            if write:
                # `update()` rather than `save()`: the correction is one column
                # and a save would run the status machinery over a job that has
                # been complete for years.
                WorkOrder.all_objects.filter(pk=wo.pk).update(completed_at=wo.opened_at)
        return found

    def _readings(self, write: bool) -> int:
        found = 0
        rows = (
            UsageReading.all_objects.filter(source=UsageReading.Source.IMPORT)
            .select_related("asset")
            .order_by("read_on")
        )
        for reading in rows.iterator():
            asset = reading.asset
            if reading.meter == asset.meter:
                continue
            if reading.unit != asset.meter_unit:
                # The asset's meter was changed after this was imported. The
                # reading is in the old unit and renaming it would file a
                # distance under an hour meter.
                continue
            found += 1
            self.stdout.write(
                f"{asset.nickname} {reading.read_on} {reading.value} {reading.unit}: "
                f"{reading.meter} -> {asset.meter}"
            )
            if write:
                # `update()` because a reading is append-only: `save()` refuses
                # to change a recorded field, which is the right rule for the
                # application and the wrong one for a repair of its own writer.
                UsageReading.all_objects.filter(pk=reading.pk).update(meter=asset.meter)
        return found
