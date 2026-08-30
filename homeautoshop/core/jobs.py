"""
Job runner (SPEC §5.2, P-3).

A Postgres-backed queue: no Redis, no broker. Jobs are idempotent and retried
with exponential backoff; a job that exhausts its attempts lands in `failed`
and is visible in admin health rather than disappearing.
"""

from __future__ import annotations

import logging
import traceback

from django.utils import timezone

from .models import Job

log = logging.getLogger(__name__)

HANDLERS: dict[str, callable] = {}


def handler(job_type: str):
    def register(fn):
        HANDLERS[job_type] = fn
        return fn

    return register


@handler("media.derive")
def _media_derive(payload: dict) -> None:
    from homeautoshop.mediafiles.models import Media
    from homeautoshop.mediafiles.services import derive

    media = Media.objects.filter(pk=payload["media_id"]).first()
    if media:
        derive(media)


@handler("media.ocr")
def _media_ocr(payload: dict) -> None:
    from homeautoshop.mediafiles.models import Media
    from homeautoshop.mediafiles.services import ocr

    media = Media.objects.filter(pk=payload["media_id"]).first()
    if media:
        ocr(media)


@handler("reminders.evaluate")
def _reminders(payload: dict) -> None:
    from .notifications import run

    run()


@handler("backup.run")
def _backup_run(payload: dict) -> None:
    from .backup import run_backup

    run_backup()


@handler("lubelogger.sync")
def _lubelogger_sync(payload: dict) -> None:
    """Scheduled incremental pull (FR-INT-13).

    Not due, or not switched on, is not an error — the job runs on a timer and
    decides for itself, so a mode change takes effect without anything being
    rescheduled.
    """
    from .integrations import sync

    try:
        sync.run(force=bool(payload.get("force")))
    except sync.SyncSkipped as skipped:
        log.info("lubelogger sync skipped: %s", skipped)


@handler("wrenchledger.sync")
def _wrenchledger_sync(payload: dict) -> None:
    """Refresh tool availability (SPEC §8.7, FR-WL-5).

    A failure here is logged and dropped rather than retried into the ground.
    Tool availability is a convenience; nothing depends on it being current,
    and the UI already shows how old the answer is.
    """
    from django.conf import settings

    from .integrations import wrenchledger

    if not settings.WRENCHLEDGER_API_KEY or settings.OFFLINE_MODE:
        return
    try:
        wrenchledger.sync(tool_ids=payload.get("tool_ids"))
    except Exception as exc:  # noqa: BLE001
        log.info("wrenchledger sync did not complete: %s", exc)


def claim(limit: int = 10) -> list[Job]:
    """Claim due jobs. The update is atomic per row, so two workers cannot
    both take the same job."""
    now = timezone.now()
    ready = Job.objects.filter(state=Job.State.PENDING, run_after__lte=now).order_by("run_after")[:limit]
    claimed = []
    for job in list(ready):
        updated = Job.objects.filter(pk=job.pk, state=Job.State.PENDING).update(
            state=Job.State.RUNNING, locked_at=now
        )
        if updated:
            job.refresh_from_db()
            claimed.append(job)
    return claimed


def run_one(job: Job) -> bool:
    fn = HANDLERS.get(job.type)
    job.attempts += 1
    if fn is None:
        job.state = Job.State.FAILED
        job.last_error = f"no handler for {job.type}"
        job.finished_at = timezone.now()
        job.save()
        return False
    try:
        fn(job.payload or {})
    except Exception:
        job.last_error = traceback.format_exc(limit=6)[-2000:]
        if job.attempts >= job.max_attempts:
            job.state = Job.State.FAILED
            job.finished_at = timezone.now()
            log.error("job %s (%s) failed permanently", job.pk, job.type)
        else:
            job.state = Job.State.PENDING
            job.run_after = timezone.now() + job.backoff()
        job.save()
        return False

    job.state = Job.State.DONE
    job.finished_at = timezone.now()
    job.last_error = ""
    job.save()
    return True


def drain(limit: int = 100) -> int:
    """Run every due job now. Used by the worker loop and by tests."""
    done = 0
    for job in claim(limit):
        if run_one(job):
            done += 1
    return done
