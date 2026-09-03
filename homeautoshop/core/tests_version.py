"""The version is one string, and this is what keeps it that way.

`VERSION` at the repository root is the source of truth: the release workflow
reads it to name the image tag, the build stamps it into
`org.opencontainers.image.version`, and `homeautoshop.__version__` reads it for
the application. None of those can disagree, because none of them holds a copy.

SPEC.md restates it twice, and both are legitimate: the header block says which
version the document describes, and the release-history appendix says which
release each note is about. Both are maintained by hand, at opposite ends of a
two-thousand-line file, which is the arrangement that drifts. So both are gated
— a release where the number was bumped in one place and not the other is a
release whose own changelog names the wrong version, and nobody notices until
somebody is trying to work out what they are running.

Gating only the appendix was itself an example: it looked like coverage and
left the header free to disagree.
"""

from __future__ import annotations

import pathlib
import re
import unittest

import homeautoshop

REPO = pathlib.Path(__file__).resolve().parents[2]
VERSION_FILE = REPO / "VERSION"
SPEC = REPO / "Artifacts" / "SPEC.md"

#: Loose on purpose. This refuses an empty file, a stray heading and the
#: `0.0.0+unknown` fallback reaching a release, without deciding for a future
#: release whether it may carry a `-rc1`.
SEMVER = re.compile(r"^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$")

#: The first data row of the appendix table: `| 0.7.0 | What it changed |`.
RELEASE_ROW = re.compile(r"^\|\s*(\d+\.\d+\.\d+[^\s|]*)\s*\|")

#: The header block's own field: `| **Version** | 0.7.0 |`.
HEADER_VERSION = re.compile(r"^\|\s*\*\*Version\*\*\s*\|\s*([^\s|]+)\s*\|")


def _newest_release_in_spec(text: str) -> str | None:
    """The version on the first table row after the release-history heading.

    Read positionally rather than by sorting, because the table is maintained
    newest-first and a sort would quietly disagree with it the first time
    somebody wrote 0.10.0 — which sorts below 0.7.0 as a string and above it
    as a release.
    """
    after = text.split("## Appendix — Release history", 1)
    if len(after) != 2:
        return None
    for line in after[1].splitlines():
        if found := RELEASE_ROW.match(line.strip()):
            return found.group(1)
    return None


class VersionTests(unittest.TestCase):
    def test_version_file_holds_a_release_number(self):
        self.assertTrue(VERSION_FILE.exists(), "VERSION is missing from the repository")
        raw = VERSION_FILE.read_text(encoding="utf-8")
        version = raw.strip()
        self.assertRegex(version, SEMVER, f"{version!r} is not a release number")
        self.assertEqual(
            raw,
            version + "\n",
            "VERSION must hold the number and one trailing newline, nothing else — "
            "it is read by `cat` in the release workflow.",
        )

    def test_package_reports_the_file(self):
        """The fallback must never be what a build stamps onto an image."""
        self.assertEqual(homeautoshop.__version__, VERSION_FILE.read_text(encoding="utf-8").strip())
        self.assertNotIn("unknown", homeautoshop.__version__)

    def test_spec_release_history_names_the_same_version(self):
        """A property of the repository, so it skips where there is no checkout.

        SPEC.md is not in the image: `.dockerignore` keeps every `*.md` out
        except README.md. Inside the container there is nothing to compare
        against and nothing has gone wrong.
        """
        if not SPEC.exists():
            self.skipTest("not a checkout — SPEC.md is deliberately not in the image")
        newest = _newest_release_in_spec(SPEC.read_text(encoding="utf-8"))
        self.assertIsNotNone(newest, "no release row found under the release-history heading")
        self.assertEqual(
            newest,
            homeautoshop.__version__,
            "VERSION and SPEC.md's release history disagree. Bump both, or the "
            "release notes describe a version nobody is running.",
        )

    def test_spec_header_names_the_same_version(self):
        """The document's header field, which is the other hand-maintained copy."""
        if not SPEC.exists():
            self.skipTest("not a checkout — SPEC.md is deliberately not in the image")
        for line in SPEC.read_text(encoding="utf-8").splitlines():
            if found := HEADER_VERSION.match(line.strip()):
                self.assertEqual(
                    found.group(1),
                    homeautoshop.__version__,
                    "VERSION and SPEC.md's header block disagree.",
                )
                return
        self.fail("no `| **Version** | … |` row in SPEC.md's header block")
