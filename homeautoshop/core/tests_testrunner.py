"""Where `--parallel` is allowed, and what a worker has to rebuild.

Parallelism roughly halves CI, and on a developer's machine it would quietly
break the one guarantee §13.2 was written to keep. Both halves of that are
decided here rather than left to whoever types the command.

Two things make it a policy rather than a preference:

* **SQLite workers get an in-memory database.** `settings.py` deliberately asks
  for a file-backed test database so the backup path is exercised rather than
  assumed; a backup that copies the database file has nothing to copy when
  there is no file. PostgreSQL clones to disk and its backup path shells out to
  `pg_dump`, so it has neither problem.
* **A worker inherits none of the runner's setup.** Python 3.14 starts workers
  with `forkserver` on Linux, so the socket guard installed in the parent is
  simply absent in the child — and a suite allowed back onto the network fails
  by passing, which is the one failure nobody investigates.
"""

from __future__ import annotations

import re
from pathlib import Path

from django.conf import settings
from django.test import TestCase, override_settings

from .testrunner import IsolatedParallelSuite, Runner, _init_parallel_worker

# Both backends are named explicitly rather than inherited from whatever is
# configured. The suite runs on SQLite locally and PostgreSQL in CI, and a test
# that reads the ambient backend asserts the opposite thing in the two places —
# which is exactly how this file first failed.
POSTGRES = {
    "default": {"ENGINE": "django.db.backends.postgresql", "NAME": "homeautoshop"}
}
SQLITE = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}}
WORKFLOWS = Path(settings.BASE_DIR) / ".github" / "workflows"


class ParallelPolicyTests(TestCase):
    @override_settings(DATABASES=SQLITE)
    def test_parallel_is_declined_on_sqlite(self):
        """Not an error and not a silent downgrade — a stated one."""
        self.assertEqual(Runner(parallel=4, verbosity=0).parallel, 1)

    @override_settings(DATABASES=POSTGRES)
    def test_parallel_is_honoured_on_postgresql(self):
        self.assertEqual(Runner(parallel=4, verbosity=0).parallel, 4)

    @override_settings(DATABASES=POSTGRES)
    def test_asking_for_one_process_stays_one(self):
        """The guard must not turn a serial run into a parallel one."""
        self.assertEqual(Runner(parallel=1, verbosity=0).parallel, 1)

    def test_workers_are_started_by_the_runner_s_own_initialiser(self):
        """Django's default would leave the network open in every worker."""
        self.assertIs(Runner.parallel_test_suite, IsolatedParallelSuite)
        self.assertIs(IsolatedParallelSuite.init_worker, _init_parallel_worker)

    def test_a_worker_rebuilds_all_three_pieces_of_the_environment(self):
        """Read as source: the body cannot be exercised outside a real worker,
        and a missing line here is invisible until it matters."""
        source = Path(
            settings.BASE_DIR, "homeautoshop", "core", "testrunner.py"
        ).read_text(encoding="utf-8")
        body = source.split("def _init_parallel_worker")[1].split("\nclass ")[0]
        self.assertIn("_guarded(socket.socket.connect)", body)
        self.assertIn("PASSWORD_HASHERS", body)
        self.assertIn("BACKUP_DIR", body)


class WorkflowTests(TestCase):
    """CI is where the speed-up is actually collected, so it is checked."""

    def _workflow(self, name: str) -> str:
        return (WORKFLOWS / name).read_text(encoding="utf-8")

    def test_ci_runs_the_suite_in_parallel(self):
        self.assertIn("manage.py test --parallel auto", self._workflow("ci.yml"))

    def test_the_release_verification_runs_the_suite_in_parallel(self):
        self.assertIn("manage.py test --parallel auto", self._workflow("release.yml"))

    def test_every_suite_invocation_in_ci_asks_for_it(self):
        """A second, serial invocation added later would give back the time
        without anybody noticing which run got slower."""
        for name in ("ci.yml", "release.yml"):
            for call in re.findall(r"manage\.py test[^\n]*", self._workflow(name)):
                self.assertIn("--parallel", call, f"{name}: {call}")
