"""
The public-example manifests, read into a fetch plan (SPEC §8.3a, FR-INT-7).

Somebody went looking for scan-tool output on the public web and wrote down
what they found: four `automotive_scan_tool_public_examples*.json` files in the
corpus folder, 402 entries between them, naming reports, live-data exports and
configuration dumps from eighty-odd vendors. The manifests are research notes —
they are git-ignored, and `.gitignore` says why: they are not ours to
republish, and some of the URLs carry the VIN of the vehicle the report was
made for.

What this module does is turn those notes into a plan: which entries can
actually be fetched, what to call the file, and which tool folder it belongs
in. The fetching itself is `manage.py fetch_scan_samples`.

**The master file is a superset.** All four were checked entry by entry and
the three smaller ones contribute nothing the master lacks — but they are read
and merged anyway, keyed on URL, because that costs nothing and the next
manifest somebody drops in this folder should not need code changed to be seen.
Where two manifests describe the same URL the richer record wins, which is how
the master's `capture_medium` and `parser_scope` survive a merge with the
schema-1.0 file that predates both fields.

**Nothing here goes near the network.** A plan is inspectable, diffable and
testable without fetching anything, which is what makes `--dry-run` worth
having.
"""

from __future__ import annotations

import json
import pathlib
import re
from dataclasses import dataclass
from urllib.parse import unquote, urlparse

from .fixtures import CORPUS

#: The research notes, whatever somebody has dropped in the corpus folder.
MANIFEST_GLOB = "automotive_scan_tool_public_examples*.json"

#: Access types that name a file a program can retrieve. The rest —
#: `web_page`, `forum_post`, `document_page`, `image-on-page` — are pages a
#: human reads, and scraping somebody's forum thread for an attachment is a
#: different act from fetching a file they published at a URL.
FETCHABLE = frozenset(
    {
        "raw_file",
        "direct_file",
        "direct_document",
        "direct_image",
        "shared_file",
        "forum_attachment",
    }
)

#: What the application has a reason to read. `live_data` is in the schema
#: (`ScanReport.live_data`), so a CSV of it is a parser target; a proprietary
#: race-logger binary is not, and this application will never own a decoder for
#: `.xrk` or `.mlg`.
REPORT_SCOPES = frozenset(
    {"diagnostic_report", "configuration", "protocol_capture", "test_fixture", "other"}
)

#: Live data is worth having where it arrives as text somebody can read.
TEXTUAL_FORMATS = frozenset({"csv", "txt", "log", "json", "tsv"})

#: Words that name no tool and only make a folder longer.
NOISE_WORDS = frozenset({"family", "ecosystem", "devices", "the", "and"})


@dataclass(slots=True)
class Sample:
    """One fetchable entry, with everywhere it is going already decided."""

    entry: dict
    folder: str
    filename: str

    @property
    def url(self) -> str:
        return self.entry.get("direct_url") or self.entry.get("source_url") or ""

    @property
    def scope(self) -> str:
        return self.entry.get("parser_scope") or "other"

    @property
    def vendor(self) -> str:
        return self.entry.get("vendor") or ""

    @property
    def target(self) -> pathlib.Path:
        return CORPUS / self.folder / ORIGINALS / self.filename


#: Fetched files live in a subfolder of their tool's folder, and that subfolder
#: is ignored whole. The operator's own reports sit beside their captures and
#: are ignored by extension, which works because a scan tool writes a PDF and a
#: phone writes a JPEG. A file off the public web has whatever extension its
#: publisher felt like, so an extension list is the wrong shape of rule for it:
#: one `.dat` nobody anticipated and somebody else's report is in a public
#: repository forever. A directory cannot leak that way.
ORIGINALS = "originals"


def manifests(root: pathlib.Path | None = None) -> list[pathlib.Path]:
    return sorted((root or CORPUS).glob(MANIFEST_GLOB))


def entries(root: pathlib.Path | None = None) -> list[dict]:
    """Every entry across every manifest, deduplicated by URL.

    Keyed on the URL actually fetched, falling back to the page it was found
    on, because that is the identity that matters: two manifests listing the
    same file under different titles are one sample, and fetching it twice
    would put two copies of somebody's report in the corpus under two names.
    """
    merged: dict[str, dict] = {}
    for path in manifests(root):
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for entry in document.get("entries") or []:
            if not isinstance(entry, dict):
                continue
            key = entry.get("direct_url") or entry.get("source_url")
            if not key:
                continue
            existing = merged.get(key)
            if existing is None or _richer(entry, existing):
                merged[key] = entry
    return [merged[key] for key in sorted(merged)]


def _richer(candidate: dict, existing: dict) -> bool:
    """More fields filled in wins. Schema versions differ; completeness does not."""
    return _filled(candidate) > _filled(existing)


def _filled(entry: dict) -> int:
    return sum(1 for value in entry.values() if value not in (None, "", [], {}))


def wanted(entry: dict, *, everything: bool = False) -> bool:
    """Is this one worth fetching?

    Two gates. The first is mechanical: an entry with no fetchable URL cannot
    be fetched, whatever it describes. The second is editorial, and it is the
    reason this is a function rather than a filter written at the call site —
    **a corpus is not improved by being larger.** Of the 203 fetchable entries,
    158 are logger telemetry, and 81 of those are one research dataset of race
    laps. Pulling them in would triple the corpus and add nothing a parser
    profile could ever be written against, while burying the thirty-odd real
    diagnostic reports that are the whole point.
    """
    if entry.get("access_type") not in FETCHABLE:
        return False
    if not (entry.get("direct_url") or "").strip():
        return False
    if everything:
        return True
    if (entry.get("parser_scope") or "other") in REPORT_SCOPES:
        return True
    # Live data, kept where it is text. `ScanReport.live_data` models it, so a
    # CSV export is a parser target; `.xrk` and `.mlg` are proprietary
    # containers and this application will never own a decoder for one.
    return (entry.get("format") or "").lower() in TEXTUAL_FORMATS


def plan(*, everything: bool = False, root: pathlib.Path | None = None) -> list[Sample]:
    """Every wanted entry, with its folder and filename settled and unique.

    Names are resolved against each other rather than one at a time: two
    reports from the same tool called `vehicle_report.pdf` are common enough
    that leaving it to chance means one of them silently overwrites the other.
    """
    taken: dict[str, set[str]] = {}
    samples: list[Sample] = []
    for entry in entries(root):
        if not wanted(entry, everything=everything):
            continue
        folder = folder_for(entry)
        used = taken.setdefault(folder, set())
        filename = _unique(filename_for(entry), used)
        used.add(filename.lower())
        samples.append(Sample(entry=entry, folder=folder, filename=filename))
    return samples


def _unique(name: str, used: set[str]) -> str:
    if name.lower() not in used:
        return name
    stem, dot, suffix = name.rpartition(".")
    stem = stem or name
    suffix = f".{suffix}" if dot else ""
    for n in range(2, 100):
        candidate = f"{stem}-{n}{suffix}"
        if candidate.lower() not in used:
            return candidate
    return f"{stem}-{abs(hash(name)) % 10_000}{suffix}"


# --------------------------------------------------------------------------
# Naming
# --------------------------------------------------------------------------

_KEEP = re.compile(r"[^a-z0-9 ._-]+")
_SPACES = re.compile(r"\s+")


def _words(value: str) -> str:
    """Lowercase words, with punctuation that separates them turned into space."""
    text = (value or "").lower().replace("/", " ").replace("+", " ").replace("&", " ")
    text = _KEEP.sub(" ", text)
    text = _SPACES.sub(" ", text).strip()
    return " ".join(w for w in text.split(" ") if w not in NOISE_WORDS)


def folder_for(entry: dict) -> str:
    """Which tool folder a sample belongs in — `ross-tech vcds`, `autel maxisys ultra`.

    The corpus is filed one folder per scanner (see the folder README), and the
    folder is the only record of what produced a report. Built from the
    vendor's leading word plus the product, rather than the whole vendor
    string: `Creosys / PLX Devices` and `Car Scanner / Torque ecosystem` are
    corporate history, not the name of the thing in somebody's hand.
    """
    vendor = _words(entry.get("vendor", ""))
    product = _words(entry.get("product", ""))
    if product in ("unknown", "none", "n a"):
        product = ""
    lead = vendor.split(" ")[0] if vendor else ""

    if not product:
        # A vendor whose product nobody recorded. Said out loud rather than
        # filed under the bare vendor name, which would quietly collect
        # reports from three different tools in one folder.
        name = f"{vendor} unspecified".strip() if vendor else "unfiled"
    elif lead and lead in product.split(" "):
        name = product
    else:
        name = f"{lead} {product}".strip()
    return name[:60].strip(" .-") or "unfiled"


def filename_for(entry: dict) -> str:
    """A safe, stable filename for a fetched sample.

    Taken from whatever the manifest recorded — the attachment name, the title,
    or the last segment of the URL — and then made safe. Made safe includes
    **stripping a VIN out of the name**: two entries here are published as
    `<VIN>_Aug_17`, and a corpus that redacts the inside of a report while
    printing the vehicle on the outside of it has protected nothing.
    """
    raw = (
        (entry.get("attachment_name") or "").strip()
        or (entry.get("title") or "").strip()
        or _basename(entry.get("direct_url") or entry.get("source_url") or "")
    )
    stem, suffix = _split_extension(raw, entry)
    stem = _safe_stem(stem) or "sample"
    return f"{stem}{suffix}"


#: Last path segments that name no file. A gist's download URL ends in `/raw`
#: for every gist there has ever been, so taking the basename gives every one
#: of them the same name and the collision suffix decides which report is
#: `raw-2.txt` — unreadable, and unstable the moment the manifest gains an
#: entry earlier in the sort.
_ANONYMOUS = frozenset({"raw", "download", "file", "files", "attachment", "index", "view"})


def _basename(url: str) -> str:
    """The last path segment that actually names something."""
    parsed = urlparse(url)
    segments = [s for s in unquote(parsed.path or "").split("/") if s]
    while segments and segments[-1].lower().split(".")[0] in _ANONYMOUS:
        segments.pop()
    return segments[-1] if segments else parsed.netloc


#: Extensions a URL can end in that are a real file type rather than a word
#: that happens to follow a dot. Checked against the manifest's own `format`
#: vocabulary plus the handful of containers that appear in these URLs.
_KNOWN_SUFFIXES = frozenset(
    {
        "pdf", "csv", "txt", "log", "json", "xml", "html", "htm", "zip", "gz",
        "jpg", "jpeg", "png", "xlsx", "dat", "brc", "daq", "mlg", "xrk", "llg",
        "lg1", "lg2", "llg5", "emublog", "tsv",
    }
)


def _split_extension(raw: str, entry: dict) -> tuple[str, str]:
    """Separate the name from its extension, preferring what the URL says.

    The manifest's `format` is a description, not always a file type — one
    entry calls itself `image-on-page`, another `pdf-embedded-image` — so it is
    the fallback rather than the source. And it can simply be wrong: the entry
    filed as an AEM `.daq` serves a file whose first bytes read
    `EMERALD v1.00`. The fetcher corrects the extension from the bytes when
    they disagree; this only has to produce something reasonable first.
    """
    stem, dot, suffix = raw.rpartition(".")
    suffix = suffix.lower()
    if dot and suffix in _KNOWN_SUFFIXES:
        return stem, f".{suffix}"

    declared = (entry.get("format") or "").lower()
    url_suffix = _basename(entry.get("direct_url") or "").rpartition(".")[2].lower()
    for candidate in (url_suffix, declared):
        if candidate in _KNOWN_SUFFIXES:
            return raw, f".{candidate}"
    return raw, ".bin"


def _safe_stem(value: str) -> str:
    """Lowercase, hyphenated, and with any real VIN taken out of it."""
    from .capture import redact

    text, _ = redact(value)
    text = text.lower().replace("_", "-")
    text = _KEEP.sub(" ", text).replace(".", " ")
    text = _SPACES.sub(" ", text).strip()
    return "-".join(text.split(" "))[:80].strip("-")


# --------------------------------------------------------------------------
# Provenance
# --------------------------------------------------------------------------

#: What a tool folder records about where its samples came from. Committed,
#: unlike the samples themselves — see the folder README.
SOURCES = "sources.json"


def sources_path(folder: str) -> pathlib.Path:
    return CORPUS / folder / SOURCES


def read_sources(folder: str) -> dict:
    path = sources_path(folder)
    if not path.exists():
        return {"version": 1, "tool": folder, "sources": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return {"version": 1, "tool": folder, "sources": []}
    data.setdefault("sources", [])
    return data


def write_sources(folder: str, records: list[dict], *, note: str = "") -> pathlib.Path:
    path = sources_path(folder)
    path.parent.mkdir(parents=True, exist_ok=True)
    document = {
        "version": 1,
        "tool": folder,
        "note": note or DEFAULT_NOTE,
        "sources": sorted(records, key=lambda r: r.get("file", "")),
    }
    path.write_text(
        json.dumps(document, indent=1, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return path


DEFAULT_NOTE = (
    "Where these samples came from. The files themselves are not committed - "
    "they are somebody else's reports, published for people to read rather "
    "than for us to redistribute. What ships is the redacted capture beside "
    "this file, and this record, so the corpus survives the link going dead "
    "and the attribution survives with it."
)


def carries_a_vin(value: str) -> bool:
    """Does this string carry a VIN, and so have to stay out of the repository?

    Applied to the URL *and* the title, because both come from the same place
    and both had one. The URL case was anticipated — two of these publish the
    vehicle in the path — and the title was not: the manifest titles those same
    two files `<VIN>_Aug_17_2025_08_45_PM_LiveData`, so a record with its URL
    dutifully withheld went into the repository with the VIN written out beside
    it. The tree guard in `scantools/tests.py` is what found it, which is what
    that guard is for — and it found it a second time, in the first draft of
    this docstring, which quoted the VIN to explain the bug.

    Redacting rather than withholding would be wrong here. A stand-in in a
    provenance record is a lie about where the file came from, and a stand-in
    that no capture produced would not be in the manifest of known stand-ins
    either. So the field is dropped; the digest identifies the file.

    Deliberately over-withholding: one Autel download id is a hex UUID that
    happens to satisfy the ISO 3779 check digit. Losing a link is cheap;
    publishing a VIN is not.
    """
    from homeautoshop.assets import vin as vinlib

    for match in re.finditer(r"[A-HJ-NPR-Z0-9]{17}", (value or "").upper()):
        if vinlib.validate(match.group(0)).check_digit_valid:
            return True
    return False


def record_for(
    sample: Sample, *, digest: str, size: int, media_type: str, retrieved_on: str
) -> dict:
    """The committed provenance for one fetched file.

    `retrieved_on` is when *this* copy was pulled, which is the date that
    matters for a link that may since have died; `listed_on` is when the
    manifest's author saw it. Conflating the two would date the corpus to
    somebody else's afternoon.
    """
    entry = sample.entry
    record: dict = {
        "file": f"{ORIGINALS}/{sample.filename}",
        "vendor": entry.get("vendor") or "",
        "product": entry.get("product") or "",
        "artifact_kind": entry.get("artifact_kind") or "",
        "parser_scope": sample.scope,
        "format": entry.get("format") or "",
        "media_type": media_type,
        "sha256": digest,
        "bytes": size,
        "retrieved_on": retrieved_on,
        "listed_on": entry.get("verified_on") or "",
        "manifest_id": entry.get("id") or "",
    }
    title = (entry.get("title") or "").strip()
    if title and not carries_a_vin(title):
        record["title"] = title
    elif title:
        record["title_withheld"] = "carries a VIN"
    if license_ := entry.get("license"):
        record["license"] = license_
    for key in ("direct_url", "source_url"):
        url = entry.get(key) or ""
        if not url:
            continue
        if carries_a_vin(url):
            record[f"{key}_withheld"] = "carries a VIN"
        else:
            record[key] = url
    return record
