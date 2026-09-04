"""The test runner: it closes the network, and it makes password hashing cheap.

Both jobs live here for the same reason. A `DiscoverRunner` subclass is built
by `manage.py test` and by nothing else, so neither behaviour has any route
into a deployment — which matters more for the second, since it deliberately
weakens a security setting.

**Password hashing.** Argon2 is chosen in §12.2 precisely because it is slow:
~93 ms a hash on a development machine, which is the whole point when somebody
is guessing at one. The suite creates users constantly and attacks no hash at
all, so it was paying that thousands of times over for nothing — about half the
runtime of the entire suite. MD5 under the runner is Django's own documented
advice for this, and `tests_hashing.py` asserts that the settings file still
ships Argon2, reading it off disk because by the time a test runs this runner
has already replaced the value in memory.

**Parallelism.** `--parallel` is honoured on PostgreSQL and declined on SQLite,
because Django hands each SQLite worker an *in-memory* database and §13.2 asks
for a file-backed one so the backup path is exercised rather than assumed. CI
runs PostgreSQL and passes `--parallel auto`; a developer's machine runs SQLite
and is told, once, why it is going serially. Workers rebuild this runner's
environment themselves — see `_init_parallel_worker`, and note that the network
guard is *not* inherited by them.

The original job follows.

---

The test runner, which will not let the suite reach the internet.

Found the hard way. A test overrode `CATALOG_URL`, which puts that host on the
derived outbound allowlist, and a mock patched the wrong function — so the
suite made a **real request to raw.githubusercontent.com** and reported the
resulting 404 as a test failure. It failed loudly that time. The next one might
not: a mock that misses on a *successful* fetch produces a passing test that
proves nothing, and a suite whose results depend on somebody's network is a
suite that fails on a train and passes at a desk.

So every socket the suite opens is checked, and anything that is not loopback
raises with the host named. That turns "why did this test 404" into "this test
tried to reach raw.githubusercontent.com", which is the sentence somebody
needs.

**Loopback stays open** because the tests genuinely use it: the live-server
tests and anything talking to a local database or cache connect to 127.0.0.1,
and blocking those would be blocking the suite rather than the network.

This guards the *test* process only. Nothing here is imported by the
application, and production code has no idea it exists — a guard that made
`outbound.py` behave differently under test would be testing something other
than what ships.
"""

from __future__ import annotations

import socket
import tempfile
from pathlib import Path

from django.conf import settings
from django.test.runner import DiscoverRunner, ParallelTestSuite, _init_worker

#: Fast, and not something anybody could mistake for a password hasher.
TEST_PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]

#: Addresses a test may legitimately open. Unix sockets carry no address and
#: are left alone; they are how a local Postgres is often reached.
LOOPBACK = frozenset({"127.0.0.1", "::1", "localhost", "0.0.0.0", ""})


class NetworkUsedInTests(AssertionError):
    """A test tried to open a socket to somewhere real."""


def _guarded(original):
    def connect(self, address, *args, **kwargs):
        host = ""
        if isinstance(address, tuple) and address:
            host = str(address[0])
        elif isinstance(address, str):
            # A Unix socket path, not a host.
            return original(self, address, *args, **kwargs)

        if host not in LOOPBACK:
            raise NetworkUsedInTests(
                f"The test suite tried to reach {host}. Tests must not use the "
                f"network: mock the fetch (`fetch_json`/`fetch_text` on the "
                f"module under test, not on `core.outbound`) so the result does "
                f"not depend on somebody having a connection."
            )
        return original(self, address, *args, **kwargs)

    return connect


def _init_parallel_worker(*args, **kwargs):
    """Rebuild this runner's environment inside a `--parallel` worker.

    A worker is a separate process, and on Python 3.14 the default start method
    on Linux is `forkserver` — so nothing `setup_test_environment` did in the
    parent is present here and all of it has to be done again.

    The network guard is the one that matters. Without this, `--parallel` would
    quietly restore the suite's ability to reach the internet: no error, no
    warning, just tests that pass for the wrong reason. That is the failure
    this runner was written to prevent in the first place.

    Each worker also gets **its own media and backup directories**. Django
    isolates the database per worker and nothing else, and a backup is a folder
    named for the second it was taken in — so two workers backing up in the
    same second write into one directory and then make assertions about each
    other's files. Tests that already point storage at their own temporary
    directory are unaffected.
    """
    _init_worker(*args, **kwargs)

    from django.test.runner import _worker_id

    socket.socket.connect = _guarded(socket.socket.connect)
    socket.socket.connect_ex = _guarded(socket.socket.connect_ex)
    settings.PASSWORD_HASHERS = TEST_PASSWORD_HASHERS

    root = Path(tempfile.gettempdir()) / f"homeautoshop-test-worker-{_worker_id}"
    for name in ("MEDIA_ROOT", "BACKUP_DIR"):
        directory = root / name.lower()
        directory.mkdir(parents=True, exist_ok=True)
        setattr(settings, name, directory)


class IsolatedParallelSuite(ParallelTestSuite):
    init_worker = _init_parallel_worker


class Runner(DiscoverRunner):
    """`DiscoverRunner`, with the network closed and the hashing cheap."""

    parallel_test_suite = IsolatedParallelSuite

    def __init__(self, *args, **kwargs):
        """Refuse `--parallel` on SQLite rather than run it wrongly.

        Django gives each parallel worker an **in-memory** SQLite database.
        `settings.py` deliberately asks for a file-backed one so that the
        backup and restore paths are exercised rather than assumed (§13.2) —
        and a backup that copies the database file has nothing to copy when the
        database is in memory. Several tests then fail, and the honest reading
        of those failures is that the suite cannot make that promise in this
        mode.

        Postgres has no such problem: its clones are real databases and the
        backup path there shells out to `pg_dump`. So parallel is used in CI,
        where Postgres is what runs, and declined here, where SQLite is.

        Declining out loud beats two worse options — failing with errors that
        look like broken tests, or skipping the backup tests and quietly
        cancelling the guarantee they exist to keep.
        """
        super().__init__(*args, **kwargs)
        if self.parallel > 1 and self._database_is_sqlite():
            self.parallel = 1
            if self.verbosity >= 1:
                # Plain ASCII: this prints to a developer's console, and a
                # Windows one renders a section sign as a replacement
                # character, which makes a working message look broken.
                print(
                    "Running serially: --parallel needs a database that clones "
                    "to disk, and SQLite workers get an in-memory one, which "
                    "the backup tests (SPEC 13.2) cannot be checked against. "
                    "Set DATABASE_URL to a PostgreSQL instance to use it."
                )

    @staticmethod
    def _database_is_sqlite() -> bool:
        engine = settings.DATABASES.get("default", {}).get("ENGINE", "")
        return "sqlite" in engine

    def setup_test_environment(self, **kwargs):
        super().setup_test_environment(**kwargs)
        self._real_connect = socket.socket.connect
        self._real_connect_ex = socket.socket.connect_ex
        socket.socket.connect = _guarded(self._real_connect)
        socket.socket.connect_ex = _guarded(self._real_connect_ex)

        # Kept rather than assumed, so teardown restores what was actually
        # there — and so a test that genuinely needs the real hasher can put it
        # back knowing what it had.
        self._production_password_hashers = settings.PASSWORD_HASHERS
        settings.PASSWORD_HASHERS = TEST_PASSWORD_HASHERS

    def teardown_test_environment(self, **kwargs):
        settings.PASSWORD_HASHERS = self._production_password_hashers
        socket.socket.connect = self._real_connect
        socket.socket.connect_ex = self._real_connect_ex
        super().teardown_test_environment(**kwargs)
