"""The test runner, which will not let the suite reach the internet.

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

from django.test.runner import DiscoverRunner

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


class Runner(DiscoverRunner):
    """`DiscoverRunner`, with the network closed."""

    def setup_test_environment(self, **kwargs):
        super().setup_test_environment(**kwargs)
        self._real_connect = socket.socket.connect
        self._real_connect_ex = socket.socket.connect_ex
        socket.socket.connect = _guarded(self._real_connect)
        socket.socket.connect_ex = _guarded(self._real_connect_ex)

    def teardown_test_environment(self, **kwargs):
        socket.socket.connect = self._real_connect
        socket.socket.connect_ex = self._real_connect_ex
        super().teardown_test_environment(**kwargs)
