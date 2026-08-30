"""
Recurring work, without a second scheduler (SPEC §5.1, P-3).

There is already a job queue; what was missing was anything to put a job on it
on a timer. The options were a cron entry inside the container, a Celery beat
process, or this: the worker asks, on each pass, whether each recurring job is
due and enqueues it if so.

This wins on the spec's own terms. A cron entry is invisible from the
application and cannot be inspected from the health screen; a beat process is
another container to run and supervise, which P-3 exists to avoid. Here the
answer to *"is the nightly backup actually running"* is a row in the same table
as everything else.

Two properties worth stating because they are easy to get wrong:

* **Idempotent.** Enqueuing is guarded on there being no pending or running job
  of that type already, so a worker restarted in a loop cannot pile up a
  thousand backups.
* **Not a real-time guarantee.** A job due at 03:00 runs on the first pass
  after 03:00. For a backup and two integration pulls that is exactly right,
  and anything needing better than that does not belong on this queue.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from django.conf import settings
from django.utils import timezone

from .models import Job, Setting

log = logging.getLogger(__name__)

LAST_RUN_PREFIX = "schedule.last."


def recurring() -> list[tuple[str, timedelta]]:
    """What runs on a timer, and how often.

    Read fresh each pass rather than captured at import, so changing a setting
    and restarting the app is enough — no code path has an interval baked in.
    """
    plan: list[tuple[str, timedelta]] = [
        ("backup.run", timedelta(days=1)),
    ]
    if settings.REMINDERS_ENABLED:
        plan.append(("reminders.evaluate", timedelta(hours=12)))
    if settings.LUBELOGGER_URL and settings.LUBELOGGER_MODE in ("pull", "pull_push_odometer"):
        plan.append(("lubelogger.sync", timedelta(hours=settings.LUBELOGGER_SYNC_HOURS)))
    if settings.WRENCHLEDGER_API_KEY:
        plan.append(("wrenchledger.sync", timedelta(hours=settings.WRENCHLEDGER_SYNC_HOURS)))
    return plan


def _last_run(job_type: str):
    from django.utils.dateparse import parse_datetime

    stamp = Setting.get(f"{LAST_RUN_PREFIX}{job_type}")
    if not stamp:
        return None
    when = parse_datetime(str(stamp))
    if when is None:
        return None
    return when if timezone.is_aware(when) else timezone.make_aware(when)


def tick(*, now=None) -> int:
    """Enqueue whatever is due. Returns how many jobs were added."""
    now = now or timezone.now()
    added = 0
    for job_type, every in recurring():
        last = _last_run(job_type)
        if last is not None and (now - last) < every:
            continue
        # A pending or running job of the same type means the previous run has
        # not finished. Queuing another would not make it finish sooner.
        if Job.objects.filter(
            type=job_type, state__in=(Job.State.PENDING, Job.State.RUNNING)
        ).exists():
            continue
        Job.objects.create(type=job_type)
        Setting.put(f"{LAST_RUN_PREFIX}{job_type}", now.isoformat())
        added += 1
    return added
