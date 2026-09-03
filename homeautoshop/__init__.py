"""HomeAutoShop.

The version is read from the `VERSION` file at the repository root rather than
written out here, so that one string answers to the image tag, the OCI label,
the release tag and anything on screen at once. A version duplicated into a
Dockerfile is a version that will disagree with this one on some release nobody
is watching, and the disagreement shows up as a support question about which of
two numbers the operator is actually running.

It stays a plain file rather than package metadata because the things that need
it are not all Python: the release workflow reads it with `cat` to name the
image tag, and the build reads it to stamp `org.opencontainers.image.version`.
"""

from __future__ import annotations

import pathlib

_VERSION_FILE = pathlib.Path(__file__).resolve().parent.parent / "VERSION"

try:
    __version__ = _VERSION_FILE.read_text(encoding="utf-8").strip()
except OSError:
    # A checkout or an image missing the file. Reported rather than guessed:
    # a wrong version is worse than an obviously absent one, and this string
    # is meant to be conspicuous in a bug report.
    __version__ = "0.0.0+unknown"
