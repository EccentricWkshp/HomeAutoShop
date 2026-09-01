"""The shared template catalog (SPEC §17 R-1, OQ-2).

Somebody has already worked out the severe-service schedule for a 7.3 Power
Stroke, and written the parser profile for an Autel scanner. Retyping either
from a forum post is the kind of work this application exists to stop, and it
is the one thing an instance genuinely cannot produce for itself.

R-1 was deferred because a repository "needs a trust model and a network
dependency, both in tension with P-1". Both are real, and neither is a reason
to leave it unbuilt — they are a specification of how to build it.

**The network dependency is made to obey P-1 rather than argued with.**

* Nothing is fetched unless somebody presses a button. There is no background
  check, no update poll, no phone-home on boot — the same line FR-INT-10 holds
  for service-information providers.
* Every request goes through `outbound.fetch_json`, so Offline Mode refuses it
  and the allowlist governs it, exactly as for every other outbound call.
* Nothing depends on it. The bundled templates ship in the image and the
  catalog is additive; an instance that never reaches it is not degraded,
  and one that reached it yesterday keeps what it installed.

**The trust model has two layers, and only one of them is in this file.**

The outer layer is editorial: the default catalog is a folder in this
project's own repository, so a template becomes published by being reviewed
and merged. That is a real and considerable protection, and it is the one that
addresses the question actually worth worrying about — whether a schedule's
intervals are sensible — which no amount of code can check.

The inner layer is everything below, and it exists because `CATALOG_URL` is
a **setting**. An operator may point this at their club's repository or their
own fork, and on that day the editorial layer is somebody else's process or
nobody's. So the code is written as though the catalog were a postman, not
an authority: the mechanical guards cost nothing, they do not weaken with a
reviewed source, and they are all that stands where the review does not reach.

The decisive rule is that **a downloaded file goes through exactly the same
validator as one an operator uploads by hand** — `templatelib.parse` and
`profiles.from_yaml`, unchanged, with their `safe_load` and their refusal of
unknown keys. There is no privileged path for catalog content, so the
catalog cannot be trusted into doing anything an emailed file could not.

Three further rules, each closing something the first does not:

* **The index cannot redirect the fetch.** An entry names a *path*, and that
  path is resolved under the configured base URL. An entry carrying an
  absolute URL, a scheme, a host, or `..` is refused. Without this the
  repository would choose which host this instance talks to, and the
  allowlist would be checking a decision somebody else made.
* **Nothing installed is applied.** Installing a template adds it to the list
  of templates; putting it on a vehicle stays a separate, deliberate act by
  somebody who looked at it. A schedule that silently attached itself to a
  truck would be a stranger deciding when its brakes get checked.
* **Where it came from is recorded**, as `source=imported`, so a template that
  turns out to be wrong can be found and so nothing pretends to be builtin.

What is deliberately *not* here: signatures. A signature would prove a file
came from whoever holds a key, which is not the question — the question is
whether the intervals are right for this truck, and no signature answers that.
The honest protections are the validator, the review before installing, and
the fact that a template does nothing until somebody applies it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import urljoin, urlparse

from django.core.cache import cache
from django.utils.translation import gettext_lazy as _

from .outbound import OutboundBlocked, OutboundFailed, fetch_json, fetch_text
from .runtime import conf

#: How long a fetched index is kept. Long enough that browsing the catalog
#: is not a request per page load, short enough that a correction published
#: this morning is visible this afternoon.
INDEX_TTL = 60 * 60
CACHE_KEY = "catalog:index"

#: What an entry may be. A kind this instance does not know is skipped rather
#: than refused, so an older instance browsing a newer catalog sees what it
#: can use instead of an error about something it was never going to install.
KINDS = ("schedule", "profile", "checklist")


class CatalogUnavailable(Exception):
    """The catalog could not be read. Never fatal — it is always optional."""


@dataclass(frozen=True, slots=True)
class Entry:
    kind: str
    slug: str
    name: str
    path: str
    description: str = ""
    author: str = ""
    applies_to: tuple[str, ...] = ()
    #: A parser profile proven against a captured report at publish time
    #: (see `build_catalog.verify`). Never set for the other kinds, whose
    #: correctness is readable rather than testable, and never a claim the
    #: file makes about itself — the index only carries it because the build
    #: ran the profile and it passed.
    verified: bool = False
    installed: bool = False

    @property
    def is_schedule(self) -> bool:
        return self.kind == "schedule"


@dataclass(slots=True)
class Catalog:
    entries: list[Entry] = field(default_factory=list)
    source: str = ""
    skipped: int = 0

    @property
    def schedules(self) -> list[Entry]:
        return [e for e in self.entries if e.kind == "schedule"]

    @property
    def profiles(self) -> list[Entry]:
        return [e for e in self.entries if e.kind == "profile"]

    @property
    def checklists(self) -> list[Entry]:
        return [e for e in self.entries if e.kind == "checklist"]


def base_url() -> str:
    """Where the catalog lives, with a trailing slash so paths resolve under it."""
    url = (conf.CATALOG_URL or "").strip()
    return url if url.endswith("/") else url + "/"


def is_configured() -> bool:
    return bool(base_url().strip("/"))


def resolve(path: str) -> str:
    """The absolute URL for a catalog path, or a refusal.

    The load-bearing check. An index entry names a path *within* the
    catalog; if it could name a URL, the repository would be choosing which
    host this instance talks to and the allowlist would be rubber-stamping
    somebody else's decision rather than the operator's.
    """
    path = str(path or "").strip()
    if not path:
        raise CatalogUnavailable(_("That catalog entry names no file."))
    if "://" in path or path.startswith("//") or urlparse(path).scheme:
        raise CatalogUnavailable(
            _("A catalog entry may name a file, not another address.")
        )
    if path.startswith("/") or ".." in path.split("/"):
        raise CatalogUnavailable(
            _("A catalog entry may not point outside the catalog.")
        )

    base = base_url()
    resolved = urljoin(base, path)
    # Belt and braces: `urljoin` is well behaved, and this asserts the result
    # rather than trusting the argument checks above to have been exhaustive.
    if not resolved.startswith(base):
        raise CatalogUnavailable(
            _("A catalog entry may not point outside the catalog.")
        )
    return resolved


def index(*, force: bool = False, user=None) -> Catalog:
    """The published list, fetched on request and cached (never on a schedule)."""
    if not is_configured():
        raise CatalogUnavailable(_("No catalog address is configured."))

    cached = None if force else cache.get(CACHE_KEY)
    if cached is None:
        try:
            response = fetch_json(
                urljoin(base_url(), "index.json"),
                purpose="catalog.index",
                user=user,
            )
        except OutboundBlocked as exc:
            raise CatalogUnavailable(str(exc))
        except OutboundFailed as exc:
            raise CatalogUnavailable(
                _("The catalog did not answer: %(detail)s") % {"detail": exc}
            )
        cached = response.data
        cache.set(CACHE_KEY, cached, INDEX_TTL)

    return _read(cached)


def _read(payload) -> Catalog:
    """Turn the published JSON into entries, skipping what makes no sense."""
    rows = payload.get("entries") if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        raise CatalogUnavailable(_("The catalog is not in a shape this can read."))

    catalog = Catalog(source=base_url())
    for row in rows:
        if not isinstance(row, dict):
            catalog.skipped += 1
            continue
        kind = str(row.get("kind") or "")
        if kind not in KINDS or not row.get("path") or not row.get("name"):
            catalog.skipped += 1
            continue
        applies = row.get("applies_to") or []
        catalog.entries.append(
            Entry(
                kind=kind,
                slug=str(row.get("slug") or "")[:64],
                name=str(row["name"])[:120],
                path=str(row["path"])[:200],
                description=str(row.get("description") or "")[:500],
                author=str(row.get("author") or "")[:80],
                applies_to=tuple(str(a)[:32] for a in applies if isinstance(a, str)),
                verified=bool(row.get("verified")),
            )
        )
    return _mark_installed(catalog)


def _mark_installed(catalog: Catalog) -> Catalog:
    """Say which entries this shop already has, so installing twice is not offered."""
    from homeautoshop.diagnostics.models import ParserProfile
    from homeautoshop.inspections.models import InspectionTemplate
    from homeautoshop.maintenance.models import ScheduleTemplate

    have = {
        "schedule": set(ScheduleTemplate.all_objects.values_list("name", flat=True))
        | set(ScheduleTemplate.all_objects.values_list("slug", flat=True)),
        "profile": set(ParserProfile.all_objects.values_list("name", flat=True)),
        "checklist": set(InspectionTemplate.all_objects.values_list("name", flat=True))
        | set(InspectionTemplate.all_objects.values_list("slug", flat=True)),
    }

    catalog.entries = [
        Entry(
            **{
                **{f: getattr(entry, f) for f in Entry.__slots__ if f != "installed"},
                "installed": entry.name in have[entry.kind]
                or (bool(entry.slug) and entry.slug in have[entry.kind]),
            }
        )
        for entry in catalog.entries
    ]
    return catalog


def fetch_file(entry: Entry, *, user=None) -> str:
    """The file behind one entry, as the plain text somebody wrote.

    Published raw. It was briefly wrapped as `{"body": "..."}` so that
    `fetch_json` — then the only guarded fetcher — could be reused, which put
    an implementation detail of this codebase in front of everybody who wanted
    to contribute a template: they would have had to JSON-escape a YAML file
    by hand. The guardrails now live in `outbound._get` and both fetchers
    share them, so the file can be a file.
    """
    url = resolve(entry.path)
    try:
        body = fetch_text(url, purpose="catalog.file", user=user)
    except OutboundBlocked as exc:
        raise CatalogUnavailable(str(exc))
    except OutboundFailed as exc:
        raise CatalogUnavailable(
            _("That file could not be read: %(detail)s") % {"detail": exc}
        )

    if not body.strip():
        raise CatalogUnavailable(_("That catalog file is empty."))
    return body


def install(entry: Entry, *, user=None):
    """Bring one entry in, through the ordinary import path and no other.

    The whole trust model in one line: this calls the same validator an
    operator's own upload calls. A catalog file gets no privilege that a
    file emailed by a stranger would not, which is the only version of this
    feature that is safe to build.
    """
    text = fetch_file(entry, user=user)

    if entry.is_schedule:
        from homeautoshop.maintenance import templatelib
        from homeautoshop.maintenance.models import ScheduleTemplate

        return templatelib.load(text, source=ScheduleTemplate.Source.IMPORTED)

    if entry.kind == "checklist":
        from homeautoshop.inspections import templatelib as checklistlib

        return checklistlib.load(text)

    from homeautoshop.diagnostics import profiles as profilelib

    profile = profilelib.from_yaml(text)
    profile.created_by = user
    profile.save()
    return profile
