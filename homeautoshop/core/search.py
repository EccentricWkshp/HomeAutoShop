"""
Global search (SPEC FR-SEARCH-1/2).

One box across vehicles, work orders, parts, people, notes, and OCR'd document
text, returning grouped results.

**Identifiers and prose are searched differently, and that distinction is the
whole design.** Full-text search stems and ranks, which is exactly right for
*"grinding noise from the front"* and exactly wrong for a VIN. Postgres turns
`1FTFW1ET5DFC10312` into the single lexeme `'1ftfw1et5dfc10312'` and matches by
whole lexeme, so no fragment of it matches anything — and a fragment is the only
thing anybody types, because the vehicle page masks the VIN and shows just the
first three characters and the last six. The same applies to `WO-2026-0001`
(`'wo'`, `'-2026'`, `'-0001'` — so "0001" finds nothing) and to part numbers.

So identifier columns are matched as substrings, ignoring separators on both
sides, and prose columns keep full-text search where the database offers it.

That is a scan rather than an index lookup. At the scale this application is
built for — NFR-P-1, ≤50 vehicles and ≤10 users — a sequential scan over a few
thousand short strings is faster than the round trip that delivers it, and
correctness on a VIN fragment is worth more than an index on a table this size.

Postgres full-text search is the production path (P-3: the database does
search, so there is no Elasticsearch). SQLite development installs fall back to
`icontains` on the prose columns too, which is correct but unranked — and,
because `icontains` is already a substring match, **the identifier bug above was
invisible on SQLite and only appeared on Postgres**. See docs/DEVELOPMENT.md.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from django.db import connection
from django.db.models import Q, QuerySet, Value
from django.db.models.functions import Replace, Upper
from django.utils.translation import gettext_lazy as _

#: Punctuation people put in identifiers, or leave out, without meaning
#: anything by it. Stripped from the query and from the column before they are
#: compared, so `ABC-1234`, `ABC 1234` and `abc1234` are one plate.
SEPARATORS = (" ", "-", ".", "/", "_")


@dataclass(slots=True)
class Group:
    label: str
    kind: str
    results: list = field(default_factory=list)


@dataclass(slots=True)
class Results:
    query: str
    groups: list[Group] = field(default_factory=list)

    @property
    def total(self) -> int:
        return sum(len(g.results) for g in self.groups)

    @property
    def is_empty(self) -> bool:
        return self.total == 0


def _supports_fts() -> bool:
    return connection.vendor == "postgresql"


def squash(text: str) -> str:
    """Uppercase, with the separators above removed."""
    out = (text or "").upper()
    for character in SEPARATORS:
        out = out.replace(character, "")
    return out


def _identifier_matches(qs: QuerySet, fields: list[str], query: str, limit: int) -> list:
    """Substring match on identifier columns, punctuation ignored."""
    if not fields:
        return []

    squashed = squash(query)
    predicate = Q()
    annotations = {}
    for index, name in enumerate(fields):
        # Two comparisons per column. The plain one catches what the operator
        # typed; the squashed one catches the case the plain one cannot — a
        # stored `ABC-1234` searched for as `abc1234`, where the punctuation is
        # in the database rather than in the query.
        predicate |= Q(**{f"{name}__icontains": query})
        if squashed:
            expression = Upper(name)
            for character in SEPARATORS:
                expression = Replace(expression, Value(character), Value(""))
            alias = f"_identifier_{index}"
            annotations[alias] = expression
            predicate |= Q(**{f"{alias}__contains": squashed})

    return list(qs.annotate(**annotations).filter(predicate)[:limit])


def _prose_matches(qs: QuerySet, fields: list[str], query: str, limit: int) -> list:
    """Rank with Postgres full-text search where available, else filter plainly."""
    if not fields:
        return []

    if _supports_fts():
        from django.contrib.postgres.search import SearchQuery, SearchRank, SearchVector

        vector = SearchVector(*fields)
        search = SearchQuery(query, search_type="websearch")
        return list(
            qs.annotate(rank=SearchRank(vector, search))
            .filter(rank__gt=0)
            .order_by("-rank")[:limit]
        )

    predicate = Q()
    for name in fields:
        predicate |= Q(**{f"{name}__icontains": query})
    return list(qs.filter(predicate)[:limit])


def _find(
    qs: QuerySet,
    query: str,
    *,
    identifiers: list[str] | None = None,
    prose: list[str] | None = None,
    limit: int = 10,
) -> list:
    """Identifier hits first, then prose, de-duplicated.

    Order matters: somebody who typed a VIN fragment wants that vehicle, not one
    whose notes happen to mention the number.
    """
    found: list = []
    seen: set = set()

    for rows in (
        _identifier_matches(qs, identifiers or [], query, limit),
        _prose_matches(qs, prose or [], query, limit),
    ):
        for row in rows:
            if row.pk in seen:
                continue
            seen.add(row.pk)
            found.append(row)

    return found[:limit]


def search(query: str, *, limit_per_group: int = 10, user=None) -> Results:
    """Search, narrowed to what `user` may see (SPEC §12.2a).

    Search is the back door into every other screen: a helper barred from a
    vehicle's page who could still find its work orders by typing its name has
    not been barred from anything. So the narrowing happens here, on the
    querysets, rather than on the results — and the groups a helper has no
    business in at all are not searched.
    """
    from homeautoshop.accounts.policy import is_helper, visible_assets, visible_assets_for

    from homeautoshop.assets.models import Asset
    from homeautoshop.diagnostics import dtc
    from homeautoshop.mediafiles.models import Media
    from homeautoshop.parts.models import Part
    from homeautoshop.people.models import Person
    from homeautoshop.work.models import WorkOrder, WorkOrderNote

    query = (query or "").strip()
    results = Results(query=query)
    # One character matches most of the database and tells the operator nothing.
    if len(query) < 2:
        return results

    def add(label, kind: str, rows: list) -> None:
        if rows:
            results.groups.append(Group(label=label, kind=kind, results=rows))

    add(
        _("Vehicles & equipment"),
        "asset",
        _find(
            visible_assets(user) if user is not None else Asset.objects.all(),
            query,
            identifiers=["vin", "plate", "serial_number", "model_number"],
            prose=["nickname", "make", "model", "trim", "notes"],
            limit=limit_per_group,
        ),
    )
    add(
        _("Work orders"),
        "work_order",
        _find(
            visible_assets_for(user, WorkOrder.objects.select_related("asset"))
            if user is not None
            else WorkOrder.objects.select_related("asset"),
            query,
            identifiers=["number"],
            prose=["title", "complaint", "cause", "correction"],
            limit=limit_per_group,
        ),
    )
    add(
        _("Parts"),
        "part",
        _find(
            Part.objects.all(),
            query,
            # A cross-reference is searched as an identifier of the part it
            # points at: the number printed on the old box is rarely the number
            # in the catalog, and that is the whole reason cross-refs exist.
            identifiers=["part_number", "cross_refs__value"],
            prose=["name", "manufacturer", "category", "notes"],
            limit=limit_per_group,
        ),
    )
    # Trouble codes are a dictionary, not vehicle data, so everybody gets them
    # — including helpers, who can already open the code page. Above the
    # helper cut-off for exactly that reason.
    #
    # This is the entry the reference page was missing. It could only be
    # reached by importing a report and clicking a reading, so answering "what
    # is P0420" meant running a scan first; and the tables already hold the
    # words, so `catalyst efficiency` finds it from the other direction too.
    add(_("Trouble codes"), "code", dtc.find(query, limit=limit_per_group))

    # The address book and the document shelf are not vehicle-scoped, so a
    # helper gets neither rather than a filtered version of each.
    if user is not None and is_helper(user):
        return results
    add(
        _("People"),
        "person",
        _find(
            Person.objects.all(),
            query,
            identifiers=["email", "phone"],
            prose=["display_name", "given_name", "family_name", "notes"],
            limit=limit_per_group,
        ),
    )
    add(
        _("Notes"),
        "note",
        _find(
            visible_assets_for(
                user,
                WorkOrderNote.objects.select_related("work_order", "work_order__asset"),
                "work_order__asset",
            )
            if user is not None
            else WorkOrderNote.objects.select_related("work_order", "work_order__asset"),
            query,
            prose=["body"],
            limit=limit_per_group,
        ),
    )
    add(
        _("Documents"),
        "media",
        _find(
            Media.objects.exclude(ocr_text=""),
            query,
            identifiers=["original_filename"],
            prose=["ocr_text"],
            limit=limit_per_group,
        ),
    )
    return results
