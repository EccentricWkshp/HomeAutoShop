"""Fetch the public scan-tool samples named in the corpus manifests (§8.3a).

The manifests in `Artifacts/samples/scan-reports/` are somebody's notes about
where scan-tool output lives on the public web. Notes rot: forum attachments
are pruned, vendor CDNs rotate their paths, a gist gets deleted. This pulls the
files down while the links still work, so the corpus is a thing this repository
has rather than a thing it points at.

    python manage.py fetch_scan_samples --dry-run
    python manage.py fetch_scan_samples
    python manage.py fetch_scan_samples --vendor ross-tech

**What lands where.** The file goes in `<tool folder>/originals/`, which is
git-ignored whole — these are other people's reports, published to be read
rather than redistributed by us, and several carry a VIN. What gets committed
is `sources.json` beside it: vendor, digest, size, license and the URL, so the
attribution survives and a later copy can be checked against this one. Then
`capture_scan_samples` writes the redacted capture that the parser corpus is
actually made of.

**It is polite.** One request at a time, a real User-Agent, a pause between
requests to the same host, and it never re-fetches a file it already has
unless asked. Re-running it after a failure retries only what failed.
"""

from __future__ import annotations

import hashlib
import time
import urllib.error
import urllib.request
from collections import defaultdict
from datetime import date

from django.core.management.base import BaseCommand, CommandError

#: Sent so the far end can see who is asking. A fetcher that hides behind a
#: browser string is a fetcher whose operator did not want to be identified,
#: and that is not the position to be in when pulling somebody's file.
USER_AGENT = (
    "HomeAutoShop-corpus/1.0 (parser sample collection; "
    "https://github.com/eccentricworkshop/homeautoshop)"
)

#: Seconds between requests to the same host. Most of these come from a
#: handful of hosts, and 169 back-to-back requests to raw.githubusercontent is
#: rude whether or not it is rate-limited.
HOST_DELAY = 1.0

#: A report is a document. Anything past this is not one, and is far more
#: likely to be an error page, a login wall or a video than a sample.
MAX_BYTES = 40 * 1024 * 1024

TIMEOUT = 45


class Command(BaseCommand):
    help = "Download the public scan-tool samples the corpus manifests name."

    def add_arguments(self, parser):
        parser.add_argument(
            "--all",
            action="store_true",
            help=(
                "Fetch every fetchable entry, including proprietary logger "
                "binaries. By default only reports, configuration dumps and "
                "textual live-data exports are pulled — the formats this "
                "application could ever read."
            ),
        )
        parser.add_argument(
            "--vendor",
            default="",
            help="Only entries whose vendor or tool folder contains this text.",
        )
        parser.add_argument(
            "--limit", type=int, default=0, help="Stop after this many downloads."
        )
        parser.add_argument(
            "--refresh",
            action="store_true",
            help="Re-fetch files already on disk instead of skipping them.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Print the plan and fetch nothing.",
        )

    def handle(self, *args, **options):
        from homeautoshop.scantools import manifest

        if not manifest.manifests():
            raise CommandError(
                f"No {manifest.MANIFEST_GLOB} in {manifest.CORPUS}. These are "
                f"research notes and are git-ignored, so a fresh clone has none."
            )

        samples = manifest.plan(everything=options["all"])
        if needle := options["vendor"].lower().strip():
            samples = [
                s
                for s in samples
                if needle in s.vendor.lower() or needle in s.folder.lower()
            ]
        if not samples:
            raise CommandError("Nothing in the manifests matches that.")

        if options["dry_run"]:
            return self._report_plan(samples)

        self._fetch(samples, manifest, options)

    # -- plan ------------------------------------------------------------

    def _report_plan(self, samples) -> None:
        by_folder: dict[str, list] = defaultdict(list)
        for sample in samples:
            by_folder[sample.folder].append(sample)
        for folder in sorted(by_folder):
            group = by_folder[folder]
            have = sum(1 for s in group if s.target.exists())
            self.stdout.write(f"{folder}  ({len(group)} sample(s), {have} already here)")
            for sample in group:
                mark = "=" if sample.target.exists() else "+"
                self.stdout.write(f"  {mark} {sample.filename}  [{sample.scope}]")
        self.stdout.write(
            self.style.SUCCESS(
                f"{len(samples)} sample(s) across {len(by_folder)} tool folder(s). "
                f"Nothing was fetched."
            )
        )

    # -- fetch -----------------------------------------------------------

    def _fetch(self, samples, manifest, options) -> None:
        today = date.today().isoformat()
        last_seen: dict[str, float] = {}
        records: dict[str, dict[str, dict]] = defaultdict(dict)

        # Start from what is already recorded so a partial run keeps what an
        # earlier one learned. Keyed on the file, so a re-fetch replaces its
        # own record rather than appending a second one.
        for folder in {s.folder for s in samples}:
            for record in manifest.read_sources(folder).get("sources") or []:
                records[folder][record.get("file", "")] = record

        fetched = skipped = failed = 0
        for sample in samples:
            if options["limit"] and fetched >= options["limit"]:
                break
            if sample.target.exists() and not options["refresh"]:
                skipped += 1
                # Its provenance is rewritten anyway, from the record already
                # held. The rules for what may be published change — a VIN
                # turned out to be in the manifest's *titles* as well as its
                # URLs — and a record that is only written on the download
                # would keep whatever was allowable the day it was fetched.
                # Re-running this is how a corpus gets the new rule applied.
                _refresh(manifest, records, sample, today)
                continue

            self._pause(sample.url, last_seen)
            try:
                body = self._get(sample.url)
            except Exception as exc:  # noqa: BLE001 - every failure is reportable
                failed += 1
                self.stderr.write(
                    self.style.WARNING(f"  ! {sample.folder}/{sample.filename}: {exc}")
                )
                continue

            sample.filename = _corrected(sample.filename, body)
            sample.target.parent.mkdir(parents=True, exist_ok=True)
            sample.target.write_bytes(body)
            digest = hashlib.sha256(body).hexdigest()
            record = manifest.record_for(
                sample,
                digest=digest,
                size=len(body),
                media_type=_media_type(body, sample.filename),
                retrieved_on=today,
            )
            records[sample.folder][record["file"]] = record
            fetched += 1
            self.stdout.write(
                f"  + {sample.folder}/{sample.filename}  {len(body):,} bytes"
            )

        for folder, by_file in records.items():
            if by_file:
                manifest.write_sources(folder, list(by_file.values()))

        self.stdout.write(
            self.style.SUCCESS(
                f"Fetched {fetched}, already had {skipped}, failed {failed}. "
                f"Provenance written to sources.json in "
                f"{len([f for f, r in records.items() if r])} folder(s)."
            )
        )
        if fetched:
            self.stdout.write(
                "Next: `manage.py capture_scan_samples` to write the redacted "
                "captures. The downloads themselves stay out of git."
            )

    def _pause(self, url: str, last_seen: dict[str, float]) -> None:
        from urllib.parse import urlparse

        host = urlparse(url).netloc
        elapsed = time.monotonic() - last_seen.get(host, 0.0)
        if elapsed < HOST_DELAY:
            time.sleep(HOST_DELAY - elapsed)
        last_seen[host] = time.monotonic()

    def _get(self, url: str) -> bytes:
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
                body = response.read(MAX_BYTES + 1)
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"HTTP {exc.code}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"{exc.reason}") from exc
        if len(body) > MAX_BYTES:
            raise RuntimeError(f"larger than {MAX_BYTES // 1024 // 1024} MB")
        if not body:
            raise RuntimeError("empty response")
        if _is_html_error(body):
            # A CDN that has lost the file answers 200 with a page saying so,
            # and a corpus that quietly accepts those is a corpus of error
            # pages. Cheap to detect, and the alternative is finding out when
            # a parser fails on it months later.
            raise RuntimeError("served an HTML page rather than a file")
        return body


def _refresh(manifest, records, sample, today: str) -> None:
    """Rebuild a kept file's provenance under today's rules.

    The digest, size and retrieval date come from the record already held —
    nothing was downloaded, so nothing about the file changed — and everything
    else is derived again from the manifest entry.
    """
    key = f"{manifest.ORIGINALS}/{sample.filename}"
    held = records[sample.folder].get(key)
    if held is None:
        return
    records[sample.folder][key] = manifest.record_for(
        sample,
        digest=held.get("sha256", ""),
        size=int(held.get("bytes") or 0),
        media_type=held.get("media_type", ""),
        retrieved_on=held.get("retrieved_on") or today,
    )


def _is_html_error(body: bytes) -> bool:
    head = body[:512].lstrip().lower()
    return head.startswith((b"<!doctype html", b"<html"))


def _media_type(body: bytes, filename: str) -> str:
    """What this actually is, read off the bytes rather than the name.

    The manifest's `format` is sometimes simply wrong — the entry filed as an
    AEM `.daq` serves a file beginning `EMERALD v1.00` — and the extension a
    publisher chose is no better a witness. The same answer the import screen
    would reach, from the same function, so the provenance record and the
    parser cannot disagree about what a file is.
    """
    from homeautoshop.diagnostics import engine

    return engine.media_type(body, filename=filename)


def _corrected(filename: str, body: bytes) -> str:
    """Give a file the extension its bytes deserve.

    Only for the cases where the mismatch would confuse a reader later: a PDF
    called `.csv` is worth renaming, a `.log` that turns out to be CSV-shaped
    is not — plenty of tools name their exports that way on purpose.
    """
    if body[:5] == b"%PDF-" and not filename.lower().endswith(".pdf"):
        return filename.rsplit(".", 1)[0] + ".pdf"
    return filename
