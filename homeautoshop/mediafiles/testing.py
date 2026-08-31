"""
Storage isolation for tests, in one place.

Three test modules had each written their own version of "point media at a
temporary directory", and the differences between them were not deliberate:
one omitted `staticfiles` and only got away with it because it never rendered
a template. Overriding `STORAGES` replaces the **whole mapping**, so a
media-only override makes the first `{% static %}` tag in the suite raise
`InvalidStorageError` — a failure that looks nothing like its cause.

Not named `tests_*`, so the runner does not try to collect it.
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from django.test import override_settings

#: Django looks this up on the first `{% static %}` tag. Always include it.
STATICFILES = {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"}

#: What `docker-compose.yml` really sets, so a test can fail the way the
#: deployed stack did: presigned URLs signed against a container-only host.
COMPOSE_S3 = {
    "staticfiles": STATICFILES,
    "default": {
        "BACKEND": "homeautoshop.mediafiles.storage.S3Storage",
        "OPTIONS": {
            "endpoint_url": "http://storage:9000",
            "bucket": "homeautoshop",
            "access_key": "x",
            "secret_key": "y",
            "region": "us-east-1",
            "public_endpoint": "",
        },
    },
}


def local_storage(root: Path):
    """An `override_settings` putting uploads in `root` and keeping statics."""
    return override_settings(
        MEDIA_ROOT=root,
        STORAGES={
            "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
            "staticfiles": STATICFILES,
        },
    )


class LocalMediaMixin:
    """Uploads land in a temporary directory that is removed afterwards.

    Without this a test run talks to whatever `STORAGE_*` the developer has in
    their `.env` — which on a machine set up for Compose means every test
    blocks on `http://storage:9000` and fails after botocore's retries.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.storage = local_storage(self.tmp)
        self.storage.enable()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.addCleanup(self.storage.disable)
        super().setUp()
