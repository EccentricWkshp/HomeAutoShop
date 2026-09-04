"""
A job whose worker died, and why it was not merely an untidy row (SPEC §5.2).

`claim` marks a job `running` and stamps `locked_at`; `run_one` moves it on to
`done`, `pending` or `failed`. Nothing covered the case in between — a worker
killed mid-job by a redeploy, an OOM, or the host going down — so the row
stayed `running` for ever. `locked_at` was written by `claim` and read by
nothing at all, which is the shape of a mechanism designed and then not
finished.

What made it matter is `schedule.tick`: it skips a job type that already has a
`pending` or `running` row, on the sound reasoning that queuing a second will
not make the first finish sooner. So **one orphaned `backup.run` silently ends
every future backup** — and the backups screen disabled "Back up now" for the
same reason, removing the way an operator would notice by taking one by hand.
The instance goes on looking healthy for a week, until the "last backup was N
days ago" alert fires.

Found on a live instance: a `backup.run` marked running for eighteen hours,
displayed as a cheerful "running" pill, with no backup taken since.
"""

from __future__ import annotations

from datetime import timedelta

from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from homeautoshop.accounts.models import Role, User
from homeautoshop.core import jobs, schedule
from homeautoshop.core.models import Job


def stalled_job(job_type="backup.run", *, age_minutes=180, **extra):
    """A job holding a lock older than any worker could still be working on."""
    return Job.objects.create(
        type=job_type,
        state=Job.State.RUNNING,
        locked_at=timezone.now() - timedelta(minutes=age_minutes),
        **extra,
    )


class ReclaimTests(TestCase):
    def test_a_stalled_job_goes_back_on_the_queue(self):
        job = stalled_job()
        self.assertEqual(jobs.reclaim(), 1)
        job.refresh_from_db()
        self.assertEqual(job.state, Job.State.PENDING)
        self.assertEqual(job.last_error, jobs.STALLED)
        self.assertIsNone(job.locked_at)

    def test_a_job_still_within_the_window_is_left_alone(self):
        """A slow backup is not a dead one, and reclaiming it starts a second."""
        job = stalled_job(age_minutes=5)
        self.assertEqual(jobs.reclaim(), 0)
        job.refresh_from_db()
        self.assertEqual(job.state, Job.State.RUNNING)

    def test_a_running_row_with_no_lock_is_stalled_whatever_the_clock_says(self):
        """`claim` always stamps one, so a row without it predates the lock."""
        job = Job.objects.create(type="backup.run", state=Job.State.RUNNING, locked_at=None)
        self.assertEqual(jobs.reclaim(), 1)
        job.refresh_from_db()
        self.assertEqual(job.state, Job.State.PENDING)

    def test_pending_and_done_jobs_are_not_touched(self):
        pending = Job.objects.create(type="backup.run")
        done = Job.objects.create(type="media.derive", state=Job.State.DONE)
        self.assertEqual(jobs.reclaim(), 0)
        pending.refresh_from_db()
        done.refresh_from_db()
        self.assertEqual(pending.state, Job.State.PENDING)
        self.assertEqual(done.state, Job.State.DONE)

    def test_a_job_that_keeps_killing_its_worker_eventually_gives_up(self):
        """The part that is easy to get wrong, and why attempts are counted here.

        `run_one` increments in memory and saves at the end, so a process that
        dies mid-job never records the try. Without counting it on reclaim, a
        job that kills its worker every time — the export that runs the
        container out of memory is the real one — is reclaimed, retried, and
        kills it again, for ever. It has to be able to reach `failed`.
        """
        job = stalled_job(max_attempts=3)
        for _ in range(3):
            jobs.reclaim()
            job.refresh_from_db()
            if job.state == Job.State.FAILED:
                break
            job.state = Job.State.RUNNING
            job.locked_at = timezone.now() - timedelta(minutes=180)
            job.save()
        job.refresh_from_db()
        self.assertEqual(job.state, Job.State.FAILED)
        self.assertEqual(job.attempts, 3)
        self.assertIsNotNone(job.finished_at)

    @override_settings(JOB_STALE_AFTER_MINUTES=1)
    def test_the_window_is_configurable(self):
        stalled_job(age_minutes=5)
        self.assertEqual(jobs.reclaim(), 1)

    def test_draining_reclaims_first(self):
        """Or a queue holding nothing but an orphan is one the worker sleeps on."""
        job = stalled_job()
        jobs.drain()
        job.refresh_from_db()
        self.assertNotEqual(job.state, Job.State.RUNNING)


class BackupsBlockedTests(TestCase):
    """The consequence, which is the reason any of this is worth fixing."""

    def test_a_stalled_backup_stops_every_future_backup(self):
        """`tick` skips a type that already has a row in flight. Reasonable —
        and fatal once a row can be in flight for ever."""
        stalled_job()
        schedule.tick()
        # Asked about `backup.run` alone: `tick` enqueues every recurring type,
        # so its return count answers a different question than this one.
        self.assertFalse(
            Job.objects.filter(type="backup.run", state=Job.State.PENDING).exists()
        )

    def test_reclaiming_lets_the_schedule_run_again(self):
        stalled_job()
        jobs.reclaim()
        self.assertTrue(
            Job.objects.filter(type="backup.run", state=Job.State.PENDING).exists()
        )


class BackupScreenTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="andy", password="x" * 16, role=Role.ADMIN
        )
        self.client.force_login(self.user)

    def test_a_stalled_job_is_named_rather_than_shown_as_running(self):
        """It displayed a "running" pill for eighteen hours on a live instance."""
        stalled_job()
        page = self.client.get(reverse("backups")).content.decode()
        self.assertIn("not coming back", page)

    def test_a_stalled_job_does_not_disable_taking_one_by_hand(self):
        """The lockout that removed the way to notice.

        A job genuinely in flight still disables the buttons — queuing a second
        backup behind a live one is what that guard is for.
        """
        stalled_job()
        page = self.client.get(reverse("backups")).content.decode()
        self.assertNotIn("disabled", page)

    def test_a_healthy_running_job_still_disables_them(self):
        stalled_job(age_minutes=1)
        page = self.client.get(reverse("backups")).content.decode()
        self.assertIn("disabled", page)
        self.assertNotIn("not coming back", page)

    def test_giving_up_on_it_unblocks_the_schedule(self):
        job = stalled_job()
        self.client.post(reverse("backup_stop"))
        job.refresh_from_db()
        self.assertEqual(job.state, Job.State.FAILED)
        schedule.tick()
        self.assertTrue(
            Job.objects.filter(type="backup.run", state=Job.State.PENDING).exists()
        )

    def test_giving_up_records_the_attempt_rather_than_erasing_it(self):
        """Somebody reading this screen next week needs to know it happened."""
        job = stalled_job()
        self.client.post(reverse("backup_stop"))
        self.assertTrue(Job.objects.filter(pk=job.pk).exists())
        job.refresh_from_db()
        self.assertEqual(job.last_error, jobs.STALLED)

    def test_a_member_cannot_stop_it(self):
        self.client.force_login(
            User.objects.create_user(username="sam", password="x" * 16, role=Role.MEMBER)
        )
        job = stalled_job()
        self.assertEqual(self.client.post(reverse("backup_stop")).status_code, 403)
        job.refresh_from_db()
        self.assertEqual(job.state, Job.State.RUNNING)
