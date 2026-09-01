"""
Parser profiles as YAML, and the ones that ship (FR-INT-7, OQ-12).

A profile is portable on purpose: it is the unit of work that adds a scan tool,
and the one thing in this application worth handing to another operator. Per
OQ-12 profiles are exportable and belong in a portable bundle — unlike pinned
service-manual links, which reveal which vehicles you own and travel only in an
encrypted backup.

Import is deliberately narrow. A profile is executable-ish data — regexes run
against somebody's report — so `safe_load` is used, unknown top-level keys are
refused rather than ignored, and every pattern is compiled before the row is
written. A profile that would have raised `re.error` on the operator's first
upload is rejected at import, where the error can still say which field.
"""

from __future__ import annotations

import re

import yaml
from django.utils.translation import gettext_lazy as _

from .models import MediaType, ParserProfile, ProfileSource

FIELDS = (
    "name",
    # Who wrote it, and what it was proven against. A profile is regexes run
    # over somebody's scan report: unlike a schedule, whose intervals you can
    # read and judge, its correctness is only knowable by running it against
    # real hardware output. Two files both called "XTOOL D8" — one from
    # somebody holding the tool, one guessed — are otherwise indistinguishable,
    # and the name alone is far too thin a thing to trust.
    "author",
    "verified_against",
    "tool_vendor",
    "tool_model",
    "version",
    "media_type",
    "engine",
    "fingerprint",
    "field_extractors",
    "table_extractor",
    "notes",
)


def _captures(raw) -> list[str]:
    """The reports a profile claims, as a list however it was written.

    A single string is accepted and wrapped, because somebody with one report
    should not have to remember YAML list syntax to say so — and because the
    difference between one and several is a judgment the publishing rules
    make, not a thing to enforce with a parse error.
    """
    if raw in (None, "", []):
        return []
    if isinstance(raw, str):
        return [raw[:120]]
    if isinstance(raw, list) and all(isinstance(v, str) for v in raw):
        return [v[:120] for v in raw][:20]
    raise ProfileInvalid(
        _("`verified_against` is a report name, or a list of them.")
    )


class ProfileInvalid(ValueError):
    """The YAML parsed, and is not a usable profile."""


def to_yaml(profile: ParserProfile) -> str:
    data = {name: getattr(profile, name) for name in FIELDS}
    data = {k: v for k, v in data.items() if v not in ("", {}, None)}
    return yaml.safe_dump(data, sort_keys=False, allow_unicode=True)


def from_yaml(text: str, *, source: str = ProfileSource.IMPORTED) -> ParserProfile:
    """Parse and validate a profile document, returning an unsaved row."""
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ProfileInvalid(_("That is not valid YAML: %(detail)s") % {"detail": exc}) from exc

    if not isinstance(data, dict):
        raise ProfileInvalid(_("A profile is a mapping of keys to values."))

    unknown = sorted(set(data) - set(FIELDS))
    if unknown:
        # Refused rather than ignored. A typo in a key name would otherwise
        # produce a profile that imports cleanly and extracts nothing.
        raise ProfileInvalid(
            _("Unrecognized key(s): %(keys)s") % {"keys": ", ".join(unknown)}
        )

    if not str(data.get("name", "")).strip():
        raise ProfileInvalid(_("A profile needs a name."))

    media_type = str(data.get("media_type", MediaType.PDF))
    if media_type not in MediaType.values:
        raise ProfileInvalid(
            _("%(value)s is not a media type this build reads.") % {"value": media_type}
        )

    profile = ParserProfile(
        name=str(data["name"])[:120],
        tool_vendor=str(data.get("tool_vendor", ""))[:60],
        tool_model=str(data.get("tool_model", ""))[:60],
        version=int(data.get("version", 1) or 1),
        media_type=media_type,
        engine=str(data.get("engine", ""))[:40],
        fingerprint=data.get("fingerprint") or {},
        field_extractors=data.get("field_extractors") or {},
        table_extractor=data.get("table_extractor") or {},
        notes=str(data.get("notes", "")),
        author=str(data.get("author", ""))[:80],
        verified_against=_captures(data.get("verified_against")),
        source=source,
    )
    for where, pattern in _patterns(profile):
        try:
            re.compile(pattern)
        except re.error as exc:
            raise ProfileInvalid(
                _("%(where)s has an invalid pattern: %(detail)s")
                % {"where": where, "detail": exc}
            ) from exc
    return profile


def _patterns(profile: ParserProfile):
    for index, signal in enumerate((profile.fingerprint or {}).get("signals") or []):
        if pattern := signal.get("pattern"):
            yield f"fingerprint.signals[{index}]", pattern
    for name, rule in (profile.field_extractors or {}).items():
        if isinstance(rule, dict) and (pattern := rule.get("pattern")):
            yield f"field_extractors.{name}", pattern
    table = profile.table_extractor or {}
    if pattern := table.get("row_pattern"):
        yield "table_extractor.row_pattern", pattern
    for pattern in (table.get("row_filters") or {}).get("drop_if_matches") or []:
        yield "table_extractor.row_filters", pattern


# --------------------------------------------------------------------------
# Seed data
# --------------------------------------------------------------------------

#: The signals below are measured, not guessed. `pdf_metadata` is deliberately
#: absent: the D8 writes none at all, so a fingerprint resting on `/Producer`
#: could never fire — one of six assumptions the sample corpus overturned (see
#: SCHEMA-PARSER-PROFILES.md §1).
XTOOL_D8 = """
name: XTOOL D8 - DTC report
tool_vendor: XTOOL
tool_model: D8
version: 1
media_type: pdf
engine: xtool_d8
fingerprint:
  threshold: 0.5
  signals:
    - kind: doc_text
      pattern: '(?i)this report is only responsible'
      weight: 0.3
    - kind: doc_text
      pattern: '\\bD8-\\d{6}\\b'
      weight: 0.3
    - kind: doc_text
      pattern: '(?i)vehicle\\s+information'
      weight: 0.2
    - kind: doc_text
      pattern: '(?i)mileage'
      weight: 0.2
notes: >
  Positional rather than declarative. This format separates a module banner
  from a section heading by color and prints a cell's first line above its own
  row, neither of which survives text extraction - so the rules live in
  homeautoshop/scantools/xtool_d8.py and this row names them.
"""

#: A worked declarative profile, and the answer to "can this thing actually
#: parse anything without code?". Generic enough to read the DTC table out of
#: most tools' plain-text or CSV-ish exports, which is exactly the case §8.3b
#: says to prefer when a tool offers it.
GENERIC_TEXT = r"""
name: Generic code list - plain text
tool_vendor: ''
tool_model: ''
version: 1
media_type: text
fingerprint:
  threshold: 0.5
  signals:
    - kind: doc_text
      pattern: '[PBCU][0-9A-F]{4}'
      weight: 0.6
    - kind: doc_text
      pattern: '(?i)(trouble|fault|dtc)\s*code'
      weight: 0.4
field_extractors:
  vin:
    strategy: label_anchored
    labels: ['VIN', 'VIN Code', 'Vehicle Identification Number']
    pattern: '([A-HJ-NPR-Z0-9]{17})'
    validate: vin_check_digit
    confidence: 0.6
  odometer:
    strategy: label_anchored
    labels: ['Odometer', 'Mileage', 'ODO']
    pattern: '([\d,.]+)'
    coerce: { type: number }
    confidence: 0.6
  performed_on:
    strategy: label_anchored
    labels: ['Date', 'Report Date', 'Test Date']
    coerce: { type: datetime, formats: ['%Y-%m-%d %H:%M', '%Y-%m-%d', '%m/%d/%Y'] }
    confidence: 0.6
table_extractor:
  locate:
    headings: ['Trouble Code', 'Fault Code', 'DTC', 'Code']
    stop_at: ['Live Data', 'Readiness', 'End of Report']
  row_pattern: '([PBCU][0-9A-F]{4}(?:-[0-9A-F]{2})?)[\s:.-]+(.*)'
  columns:
    - { role: code, group: 1, validate: dtc_format }
    - { role: description, group: 2 }
  row_filters:
    drop_if_matches:
      - '(?i)no (fault|trouble) codes? (found|detected)'
"""

SEED = (XTOOL_D8, GENERIC_TEXT)


# --------------------------------------------------------------------------
# The profiles that exist, without a database
# --------------------------------------------------------------------------


def catalog_profiles(root=None) -> list[ParserProfile]:
    """Every profile published in this repository's catalog folder.

    Unsaved rows, read straight off disk. The corpus tooling needs to know
    *what a profile makes of a report* long before anybody installs it — that
    is the whole point of `verified_against` — and requiring a database for
    that would mean the badge could only be checked on a running instance
    rather than in the pull request that adds the profile.
    """
    from pathlib import Path

    from django.conf import settings

    folder = Path(root) if root else Path(settings.BASE_DIR) / "catalog" / "profiles"
    if not folder.is_dir():
        return []
    found = []
    for path in sorted(folder.glob("*.yaml")):
        try:
            found.append(from_yaml(path.read_text(encoding="utf-8")))
        except ProfileInvalid:
            # Skipped, not raised. `build_catalog` is where a broken catalog
            # file is somebody's problem, and it names the file and the line;
            # here it would only stop an unrelated capture from being read.
            continue
    return found


def available(root=None) -> list[ParserProfile]:
    """Bundled profiles plus catalog ones — everything a report could match.

    Ordered bundled-first only for determinism; `engine.detect` picks on score,
    so a catalog profile written for the exact tool beats a generic one that
    happens to also match, whichever order they arrive in.
    """
    bundled = [from_yaml(document, source=ProfileSource.BUILTIN) for document in SEED]
    return bundled + catalog_profiles(root)


def seed(*, revive: bool = False) -> int:
    """Install the bundled profiles. Idempotent, and never clobbers an edit.

    A profile the operator has changed is theirs. Re-seeding bumps nothing and
    overwrites nothing — a new bundled version arrives as a new `version` row,
    which is what versioning is for.

    **A removed profile stays removed** on the boot path, for the same reason
    the schedule and checklist seeders leave a deleted template deleted: an
    operator who took the generic text profile off this instance meant it, and
    should not have to do it again after every restart. `(name, version)` is
    uniquely constrained regardless of `deleted_at`, so re-creating one would
    fail on the constraint rather than merely annoy.

    `revive=True` is the other intent — the same person changing their mind,
    which is a deliberate act and gets its own button (`restore_builtins`).
    Without it, removing the XTOOL D8 profile would be a one-way door once the
    trash aged out, and the catalog deliberately publishes nothing that
    duplicates what ships.
    """
    installed = 0
    for document in SEED:
        candidate = from_yaml(document, source=ProfileSource.BUILTIN)
        existing = ParserProfile.all_objects.filter(
            name=candidate.name, version=candidate.version
        ).first()
        if existing is not None:
            if revive and existing.is_deleted:
                existing.restore()
                installed += 1
            continue
        candidate.save()
        installed += 1
    return installed
