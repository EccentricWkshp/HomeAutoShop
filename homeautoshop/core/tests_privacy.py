"""
No private infrastructure in a public repository.

The specification was written against a real installation, and it showed: a real
hostname on somebody's real domain appeared in four documents and one test, and
went to a public GitHub repository with them. Nothing secret leaked — but a
private subdomain is a fact about a person's network that a design document has
no business publishing, and once pushed it is not really retractable.

The guard cannot be a list of forbidden domains, because writing the domain into
the test puts it straight back into the repository. So the rule runs the other
way round: **an example hostname must be one of the names reserved for
examples**, and every real third-party service is named explicitly below. A new
personal hostname matches neither and fails.

RFC 2606 reserves `example.com`/`.org`/`.net` and the `.test`, `.invalid` and
`.localhost` TLDs. RFC 8375 reserves `home.arpa` for residential networks, which
is what the install guide already uses for LAN examples.
"""

from __future__ import annotations

import re
from pathlib import Path

from django.conf import settings
from django.test import TestCase

#: Reserved for documentation and private networks, by RFC.
RESERVED = re.compile(
    r"(^|\.)(example\.(com|org|net)|home\.arpa|localhost)$|\.(test|invalid|localhost|local)$",
    re.I,
)

#: Real services these documents legitimately name: the products integrated
#: with, the public APIs consumed, and the libraries linked. Anything outside
#: this set and the reserved names above is somebody's own infrastructure.
KNOWN_THIRD_PARTIES = {
    "api.nhtsa.gov",
    "vpic.nhtsa.dot.gov",
    "www.nhtsa.gov",
    "nhtsa.gov",
    "static.nhtsa.gov",
    "lubelogger.com",
    "www.lubelogger.com",
    "wrench-ledger.app",
    "www.wrench-ledger.app",
    "charm.li",
    "lemon-manuals.la",
    "lemon-manuals.org.ua",
    "www.alldatadiy.com",
    "alldatadiy.com",
    "github.com",
    "docs.djangoproject.com",
    "www.djangoproject.com",
    "letsencrypt.org",
    "acme-v02.api.letsencrypt.org",
    "acme-staging-v02.api.letsencrypt.org",
    "developer.mozilla.org",
    "hub.docker.com",
    "pypi.org",
    "www.w3.org",
    "schema.org",
    "homeautoshop.local",
}

HOST = re.compile(r"https?://([A-Za-z0-9._-]+)")

#: Where a hostname would be a leak. Application code is excluded: a default
#: like `storage:9000` is a container name, not a person's network.
SCANNED = ("Artifacts", "docs", "README.md", "PRIVACY.md", ".env.example")


class NoPrivateHostnamesTests(TestCase):
    def _files(self):
        root = Path(settings.BASE_DIR)
        for entry in SCANNED:
            target = root / entry
            if target.is_file():
                yield target
            elif target.is_dir():
                for path in sorted(target.rglob("*")):
                    if path.suffix in (".md", ".yaml", ".yml", ".txt") and path.is_file():
                        yield path

    def test_documents_name_no_private_infrastructure(self):
        root = Path(settings.BASE_DIR)
        offenders: list[str] = []

        for path in self._files():
            text = path.read_text(encoding="utf-8", errors="ignore")
            for host in set(HOST.findall(text)):
                bare = host.lower().rstrip(".")
                if bare in KNOWN_THIRD_PARTIES or RESERVED.search(bare):
                    continue
                # A bare word with no dot is a container or service name.
                if "." not in bare:
                    continue
                offenders.append(f"{path.relative_to(root)}: {bare}")

        self.assertEqual(
            sorted(set(offenders)),
            [],
            "Documentation hostnames must be RFC-reserved examples "
            "(example.com, home.arpa, .test) or a service named in "
            "KNOWN_THIRD_PARTIES. Found: " + "; ".join(sorted(set(offenders))),
        )

    def test_the_guard_would_actually_catch_one(self):
        """A check that cannot fail is not a check."""
        self.assertIsNone(RESERVED.search("lubelog.somebodys-domain.casa"))
        self.assertIsNotNone(RESERVED.search("lubelogger.home.arpa"))
        self.assertIsNotNone(RESERVED.search("shop.example.com"))
