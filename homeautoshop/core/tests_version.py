"""The version is one string, and this is what keeps it that way.

`VERSION` at the repository root is the source of truth: the release workflow
reads it to name the image tag, the build stamps it into
`org.opencontainers.image.version`, and `homeautoshop.__version__` reads it for
the application. None of those can disagree, because none of them holds a copy.

SPEC.md's release-history appendix is the one place that legitimately restates
it, because a release note has to say which release it is describing. So that
is the pair worth gating: a release where the number was bumped in one and not
the other is a release whose own changelog names the wrong version, and nobody
notices until somebody is trying to work out what they are running.
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
