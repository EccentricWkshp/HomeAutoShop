"""
S3-compatible object storage (SPEC §5.1, FR-DOC-8).

**Not the default.** Media lives under MEDIA_ROOT unless an operator sets
`STORAGE_DRIVER=s3` and points it at an object store of their own — media on a
NAS, on rented storage, or on a host that is not the one running the
application. Nothing is bundled for this: it is vendor-neutral boto3 against
the S3 API, so it works against anything that speaks it, and `migrate_storage`
moves the existing files either direction without touching the database.

Worth knowing before choosing it: `run_backup()` copies MEDIA_ROOT, so media
kept out here is in none of the backups this application takes. It says so when
a backup runs and again before a restore, but backing that store up is the
operator's job.

The two rules that shape this module:

* **The bucket is never public.** Every read is a short-lived presigned URL, so
  a media object is unreachable without an authenticated request that minted it.
* **A storage outage must not hide the service history** (NFR-R-6). Failures
  here raise rather than corrupt, and the app degrades to read-only with a
  banner rather than pretending the file was saved.

**What this module does *not* do by default is hand a URL to a browser.** A
presigned URL is signed against the endpoint the application talks to, which is
reachable from the application and not necessarily from anywhere else. Photos
are therefore served by the application (`mediafiles/views.py`), which also
makes reading one require a login rather than possession of a link.
`public_endpoint` below is for the operator who has genuinely published their
object store; it is blank unless they say otherwise.
"""

from __future__ import annotations

import logging
import threading
from datetime import timedelta
from urllib.parse import urljoin

from django.core.files.base import ContentFile, File
from django.core.files.storage import Storage
from django.utils.deconstruct import deconstructible

log = logging.getLogger(__name__)

# Presigned URLs are deliberately short-lived: long enough to load a page and
# fetch its images, short enough that a copied link is not a lasting leak.
DEFAULT_URL_TTL = timedelta(minutes=15)


@deconstructible
class S3Storage(Storage):
    """Minimal S3 storage. Only what the media pipeline actually uses."""

    def __init__(
        self,
        endpoint_url: str = "",
        bucket: str = "homeautoshop",
        access_key: str = "",
        secret_key: str = "",
        region: str = "us-east-1",
        url_ttl: int = int(DEFAULT_URL_TTL.total_seconds()),
        public_endpoint: str = "",
    ) -> None:
        self.endpoint_url = endpoint_url
        self.bucket = bucket
        self.access_key = access_key
        self.secret_key = secret_key
        self.region = region
        self.url_ttl = url_ttl
        # A browser reaches the store by whatever address it is published on,
        # which is not always the one the application uses, so a presigned URL
        # may need a different host. Falls back to endpoint_url when they agree.
        self.public_endpoint = public_endpoint or endpoint_url
        self._client = None
        self._lock = threading.Lock()
        self._bucket_ready = False

    # -- plumbing --------------------------------------------------------

    @property
    def client(self):
        if self._client is None:
            with self._lock:
                if self._client is None:
                    try:
                        import boto3
                        from botocore.config import Config
                    except ImportError as exc:  # pragma: no cover
                        raise RuntimeError(
                            "STORAGE_DRIVER=s3 requires boto3. Install it, or set "
                            "STORAGE_DRIVER=filesystem."
                        ) from exc
                    self._client = boto3.client(
                        "s3",
                        endpoint_url=self.endpoint_url or None,
                        aws_access_key_id=self.access_key or None,
                        aws_secret_access_key=self.secret_key or None,
                        region_name=self.region,
                        config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
                    )
        return self._client

    def _ensure_bucket(self) -> None:
        """Create the bucket on first write. Idempotent and never public."""
        if self._bucket_ready:
            return
        from botocore.exceptions import ClientError

        try:
            self.client.head_bucket(Bucket=self.bucket)
        except ClientError:
            try:
                self.client.create_bucket(Bucket=self.bucket)
                log.info("created private bucket %s", self.bucket)
            except ClientError as exc:
                if exc.response.get("Error", {}).get("Code") not in (
                    "BucketAlreadyOwnedByYou",
                    "BucketAlreadyExists",
                ):
                    raise
        self._bucket_ready = True

    # -- Storage API -----------------------------------------------------

    def _open(self, name: str, mode: str = "rb") -> File:
        body = self.client.get_object(Bucket=self.bucket, Key=name)["Body"].read()
        return ContentFile(body, name=name)

    def _save(self, name: str, content) -> str:
        self._ensure_bucket()
        content.seek(0)
        extra = {}
        if content_type := getattr(content, "content_type", None):
            extra["ContentType"] = content_type
        self.client.upload_fileobj(content, self.bucket, name, ExtraArgs=extra or None)
        return name

    def exists(self, name: str) -> bool:
        from botocore.exceptions import ClientError

        try:
            self.client.head_object(Bucket=self.bucket, Key=name)
        except ClientError:
            return False
        return True

    def delete(self, name: str) -> None:
        self.client.delete_object(Bucket=self.bucket, Key=name)

    def size(self, name: str) -> int:
        return self.client.head_object(Bucket=self.bucket, Key=name)["ContentLength"]

    def url(self, name: str) -> str:
        """A short-lived presigned URL. The bucket itself is never readable."""
        signed = self.client.generate_presigned_url(
            "get_object",
            Params={"Bucket": self.bucket, "Key": name},
            ExpiresIn=self.url_ttl,
        )
        if self.public_endpoint and self.public_endpoint != self.endpoint_url:
            signed = signed.replace(self.endpoint_url, self.public_endpoint, 1)
        return signed

    def get_accessed_time(self, name):  # pragma: no cover - not tracked by S3
        raise NotImplementedError

    def get_created_time(self, name):  # pragma: no cover
        return self.get_modified_time(name)

    def get_modified_time(self, name):
        return self.client.head_object(Bucket=self.bucket, Key=name)["LastModified"]
