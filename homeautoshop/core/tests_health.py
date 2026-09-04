"""The instance health screen (FR-ADM-8).

Two things this page has to do that a smoke test cannot see. It renders either
way; what matters is whether it answers the questions somebody opens it with.

* **Which machine is this?** A shop accumulates instances — the NAS, the
  laptop that was going to be a test, the Pi in the garage — and every one of
  them is titled with the same shop name. Restoring a backup onto the wrong one
  is the mistake this card exists to prevent.
* **What is the queue doing?** A count says three jobs are pending. It cannot
  distinguish three thumbnails from a backup that has been retrying for an
  hour, and the second is the only one worth getting out of bed for.
"""

from __future__ import annotations

from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from homeautoshop.accounts.models import Role, User

from .models import Job


class HealthPageTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = User.objects.create_user(
            username="boss", password="x" * 16, role=Role.ADMIN
        )

    def setUp(self):
        self.client.force_login(self.admin)

    def _page(self) -> str:
        response = self.client.get(reverse("health"))
        self.assertEqual(response.status_code, 200)
        return response.content.decode()

    def test_it_names_the_machine_it_is_running_on(self):
        import platform
        import socket

        page = self._page()
        self.assertIn(socket.gethostname(), page)
        self.assertIn(platform.machine(), page)
        self.assertIn(platform.python_version(), page)

    @override_settings(APP_REVISION="")
    def test_a_locally_built_image_says_so_rather_than_showing_nothing(self):
        """Blank is an answer, and it is one worth spelling out.

        The release build stamps the commit in; a local `docker compose build`
        does not. An empty field would read as a missing feature instead of as
        "this was not one of the published releases", which is the first thing
        worth knowing about a surprising bug report.
        """
        self.assertIn("built here", self._page())

    @override_settings(APP_REVISION="c2cead7e92559e686e9d6e142eb73ea4ef377cd7")
    def test_a_published_image_shows_the_commit_it_came_from(self):
        page = self._page()
        self.assertIn("c2cead7e92559e686e9d6e142eb73ea4ef377cd7", page)
        self.assertNotIn("built here", page)

    def test_the_queue_names_the_jobs_rather_than_counting_them(self):
        Job.objects.create(type="backup.run")
        Job.objects.create(type="media.thumbnail", state=Job.State.RUNNING)
        page = self._page()
        self.assertIn("backup.run", page)
        self.assertIn("media.thumbnail", page)

    def test_a_finished_job_does_not_fill_the_queue(self):
        """Otherwise the list is a log, and the thing that is stuck is off the bottom."""
        Job.objects.create(type="media.thumbnail", state=Job.State.DONE)
        self.assertIn("Nothing waiting or running", self._page())

    def test_a_failed_job_shows_the_error_it_reported(self):
        Job.objects.create(
            type="backup.run",
            state=Job.State.FAILED,
            last_error="pg_dump: error: aborting because of server version mismatch",
            finished_at=timezone.now(),
        )
        page = self._page()
        self.assertIn("server version mismatch", page)

    def test_somebody_who_cannot_manage_settings_cannot_see_it(self):
        """Not a 403: this page names hostnames, addresses and the database."""
        self.client.force_login(
            User.objects.create_user(username="mechanic", password="x" * 16, role=Role.MEMBER)
        )
        self.assertEqual(self.client.get(reverse("health")).status_code, 404)
