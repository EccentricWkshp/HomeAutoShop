"""
Scheduled LubeLogger pull sync (SPEC §8.6, FR-INT-13, FR-INT-16).

Deferred to Phase 4 deliberately: the one-time import (Phase 2) captured nearly
all the value, and a sync is a different proposition with a different risk
profile. Everything OQ-9 says still holds — this is a convenience, and an
instance with it switched off is not degraded.

Three properties this file is responsible for:

* **Incremental.** Each run pulls a window starting a little before the last
  successful run, so a record edited at the source after being imported is seen
  again. The overlap is cheap because every write is idempotent anyway.
* **Never destructive.** A source deletion is never propagated and a locally
  edited record is never overwritten — both already true of the importer, and
  both are why this can run unattended at all.
* **Never surprising.** It runs only when the operator has chosen a sync mode,
  never in Offline Mode, and it records what it did where the operator can see
  it.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from django.conf import settings
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from homeautoshop.core.models import Setting

log = logging.getLogger(__name__)

LAST_SYNC_KEY = "lubelogger.last_sync"
LAST_RESULT_KEY = "lubelogger.last_result"

#: How far back to reach past the last run. A record edited at the source
#: after it was imported carries its original date, so a window that starts
#: exactly at the last run would never see the edit.
OVERLAP_DAYS = 14

#: The window for a first scheduled run, when nothing has ever synced. Not
#: unbounded: an operator who switches sync on years after the one-time import
#: wants the recent past, not a re-walk of the whole history — and if they do
#: want that, the one-time import is right there and says what it will do.
FIRST_RUN_DAYS = 90

SYNC_MODES = {"pull", "pull_push_odometer"}


class SyncSkipped(Exception):
    """A reason not to sync that is not an error."""


def due(*, now=None) -> bool:
    """Whether a scheduled pull should happen at all."""
    if settings.LUBELOGGER_MODE not in SYNC_MODES:
        return False
    if settings.OFFLINE_MODE or not settings.LUBELOGGER_URL:
        return False
    last = Setting.get(LAST_SYNC_KEY)
    if not last:
        return True
    now = now or timezone.now()
    from django.utils.dateparse import parse_datetime

    when = parse_datetime(last)
    if when is None:
        return True
    if timezone.is_naive(when):
        when = timezone.make_aware(when, timezone.get_current_timezone())
    return (now - when) >= timedelta(hours=settings.LUBELOGGER_SYNC_HOURS)


def window_start(*, now=None):
    now = now or timezone.now()
    last = Setting.get(LAST_SYNC_KEY)
    if not last:
        return (now - timedelta(days=FIRST_RUN_DAYS)).date()
    from django.utils.dateparse import parse_datetime

    when = parse_datetime(last)
    if when is None:
        return (now - timedelta(days=FIRST_RUN_DAYS)).date()
    if timezone.is_naive(when):
        when = timezone.make_aware(when, timezone.get_current_timezone())
    return (when - timedelta(days=OVERLAP_DAYS)).date()


def run(*, client=None, force: bool = False) -> dict:
    """Pull whatever is new. Returns a summary for the health screen."""
    if not force and not due():
        raise SyncSkipped(_("Not due, or sync is not switched on."))
    if settings.LUBELOGGER_MODE not in SYNC_MODES:
        raise SyncSkipped(_("LubeLogger is not set to sync."))
    if settings.OFFLINE_MODE:
        raise SyncSkipped(_("Offline Mode is on, so nothing is fetched."))

    from .importer import run_import

    started = timezone.now()
    since = window_start(now=started)

    # `create_missing=False` on purpose. A scheduled job silently creating a
    # vehicle from a source record is how an instance grows a duplicate of a
    # car it already has, under a different name, unattended. Matching a new
    # source vehicle stays a decision somebody makes on the import screen.
    report = run_import(dry_run=False, create_missing=False, since=since, client=client)

    summary = {
        "at": started.isoformat(),
        "since": since.isoformat(),
        "created": report.total_created,
        "conflicts": len(report.conflicts),
        "unmatched": len(report.unmatched),
        "errors": report.errors[:5],
    }
    # Only a clean-enough run advances the marker. Moving it after a run that
    # errored halfway would put the records it never reached permanently
    # outside every future window.
    if not report.errors:
        Setting.put(LAST_SYNC_KEY, started.isoformat())
    Setting.put(LAST_RESULT_KEY, summary)

    if settings.LUBELOGGER_MODE == "pull_push_odometer":
        summary["pushed"] = push_odometer(client=client)
    return summary


def push_odometer(*, client=None, limit: int = 200) -> int:
    """Send garage-captured readings back to LubeLogger (FR-INT-16, MAY).

    Opt-in through the mode, one direction, one record type. Only readings this
    application originated are sent — pushing back a reading that came *from*
    LubeLogger would write its own data at it and grow a duplicate on every
    run, which is exactly the bidirectional mess §8.6 puts out of scope.
    """
    from homeautoshop.assets.models import UsageReading
    from homeautoshop.core.models import ExternalRef

    from .lubelogger import SOURCE, LubeLoggerClient

    client = client or LubeLoggerClient()
    instance = client.base_url

    imported = set(
        ExternalRef.objects.filter(
            source_system=SOURCE, source_instance_url=instance, entity_type="UsageReading"
        ).values_list("entity_id", flat=True)
    )
    pushed_marker = "lubelogger_push"
    already = set(
        ExternalRef.objects.filter(
            source_system=pushed_marker, source_instance_url=instance
        ).values_list("entity_id", flat=True)
    )

    vehicles = {
        ref.entity_id: ref.external_id
        for ref in ExternalRef.objects.filter(
            source_system=SOURCE, source_instance_url=instance, external_type="vehicle"
        )
    }

    sent = 0
    readings = (
        UsageReading.objects.exclude(pk__in=imported | already)
        .filter(asset_id__in=vehicles, meter="odometer")
        .order_by("read_on")[:limit]
    )
    for reading in readings:
        try:
            client.push_odometer(
                vehicles[reading.asset_id],
                value=reading.value,
                on=reading.read_on,
                note=reading.note or "",
            )
        except Exception as exc:  # noqa: BLE001 - one failure is not a stop
            log.warning("odometer push failed for %s: %s", reading.pk, exc)
            continue
        ExternalRef.objects.update_or_create(
            source_system=pushed_marker,
            source_instance_url=instance,
            external_type="odometer",
            external_id=str(reading.pk),
            defaults={"entity_type": "UsageReading", "entity_id": reading.pk},
        )
        sent += 1
    return sent
