"""
A published manual library, read from the files rather than from the website.

Two of these libraries distribute their whole corpus as a pair of files: an
`index.json` naming every vehicle, and a `pages.mtbl` holding every page keyed
by content hash. Given both, there is nothing left to crawl — the same answer
comes back every time, no part of it is sampled because a request budget ran
out, and somebody else's server is not involved at all.

The layout, worked out by reading it:

* **`index.json`** lists one record per vehicle — make, model years, model,
  engine, its URL path, and the key of its **routing table**. The one this was
  written against holds 279,988 vehicles across 65 makes, 1960 to 2025.
* **A routing table** is JSON with two sections. `final` maps an exact path
  under the vehicle to a content key; `prefix` maps a path prefix to *another*
  routing table, which is how a manual with hundreds of sections stays a small
  lookup. Descending it costs a request per level.
* **`Repair and Diagnosis (Single Page)` is a tree, not the manual.** Its key
  says so — `html_act_conglomerate_tree_…` — and it is worth being explicit
  because the document is 19 MB, mentions hundreds of codes, and contains **no
  tables at all**. Where a make writes its node labels as `P0420 Catalyst
  efficiency below threshold` a reader gets real definitions out of it, which
  is how a 2016 Jaguar yields 1,249; where a make labels its nodes `B0104` it
  yields almost nothing. Reading only this put Volkswagen and Audi at zero and
  Chevrolet at 63.
* **The content is in the pages the tree points at**, reached by walking the
  routing tables to their `final` entries. That is thousands of small local
  lookups per vehicle and takes about a second, and it is the difference
  between 63 codes for the whole of Chevrolet and 691 from one 2015 Camaro.

**Content is shared between vehicles**, being keyed by hash — 805 Jaguars from
1996 on are covered by 315 distinct trees, and the pages beneath them are
shared more heavily still. So a caller that remembers which content keys it has
already read does a fraction of the work for the same answer.

Nothing here reads a code table. That is `manuals.read`, which does not care
where its HTML came from, and is the same code the crawler feeds.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote

from . import mtbl

#: The path, within a vehicle, of the navigation tree over its whole manual.
SINGLE_PAGE = "Single%20Page"

#: How deep a routing table may nest before this stops descending. Six was
#: enough for every vehicle tried; the limit is here so a table that points at
#: itself is a bounded walk rather than a hang.
DEEPEST = 8

#: Parsed routing tables kept between vehicles. Sibling vehicles share most of
#: their tree, so re-walking it per vehicle is most of the cost of a make.
#: Bounded, and dropped wholesale when full: an eviction policy would cost more
#: thought than it saves against a corpus this shape.
MOST_CACHED = 150_000

INDEX = "index.json"
PAGES = "pages.mtbl"


class NotALibrary(ValueError):
    """The folder does not hold a manual library this can read."""


@dataclass(frozen=True, slots=True)
class Vehicle:
    make: str
    years: tuple[str, ...]
    model: str
    engine: str
    path: str
    table: str

    @property
    def year(self) -> int:
        """The newest model year this record covers, or 0 if it names none."""
        numbers = [int(y) for y in self.years if str(y).isdigit()]
        return max(numbers) if numbers else 0

    @property
    def name(self) -> str:
        """What to write in a citation. A list read from one vehicle and
        offered for the whole make is a wider claim than the document makes,
        and §8.1a's answer to that is that the source names the vehicle."""
        return " ".join(p for p in (str(self.year), self.model, self.engine) if p)

    def __str__(self) -> str:
        return f"{self.make} {self.name}"


class Library:
    """One library on disk, opened for reading."""

    def __init__(self, root):
        self.root = Path(root)
        index, pages = self.root / INDEX, self.root / PAGES
        for wanted in (index, pages):
            if not wanted.exists():
                raise NotALibrary(f"{self.root} has no {wanted.name}")
        self.pages = mtbl.Reader(pages)
        self.build = self._build(index)
        self._tables: dict[str, dict] = {}

    def _build(self, index: Path) -> str:
        """Which edition of the corpus this is.

        Recorded because a later dump is a different document, not a newer
        version of this one: the edition read here names a 2005 Isuzu
        `Ascender LS, 4.2 S, 4WD` where the live site calls the same vehicle
        `Ascender 4WD L6-4.2L`. Anything transcribed from it should be able to
        say which build it came from.
        """
        with index.open("r", encoding="utf-8") as handle:
            head = handle.read(4096)
        name = json.loads(head[: head.index('"vehicles"') - 1].rstrip().rstrip(",") + "}")
        return f"{name.get('database', 'library')} {self.root.parent.parent.name[:12]}"

    # -- what is in it ----------------------------------------------------

    def vehicles(self) -> list[Vehicle]:
        """Every vehicle the library covers.

        Read whole rather than streamed: it is 142 MB of JSON and five seconds,
        and every caller wants to group it by make before doing anything.
        """
        with (self.root / INDEX).open("r", encoding="utf-8") as handle:
            listed = json.load(handle).get("vehicles") or []
        return [
            Vehicle(
                make=str(row.get("make") or "").strip(),
                years=tuple(str(y) for y in (row.get("years") or [])),
                model=str(row.get("model") or "").strip(),
                engine=str(row.get("engine") or "").strip(),
                path=unquote(str(row.get("uriPath") or "")),
                table=str(row.get("rootUriTable") or ""),
            )
            for row in listed
            if row.get("rootUriTable")
        ]

    def routing(self, vehicle: Vehicle) -> dict:
        """One vehicle's routing table, or an empty one."""
        raw = self.pages.get(vehicle.table.encode())
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except ValueError:
            return {}

    def manual_key(self, vehicle: Vehicle) -> str:
        """The content key of this vehicle's whole manual, or ''.

        Returning the key rather than the document is what lets a caller notice
        that it has already read this manual for another vehicle. On this
        corpus that is two reads saved in every three.
        """
        for path, key in (self.routing(vehicle).get("final") or {}).items():
            if SINGLE_PAGE in path:
                return str(key)
        return ""

    def pages_for(self, vehicle: Vehicle) -> dict[str, str]:
        """Every page under one vehicle, as `readable path -> content key`.

        Walks the routing tables to their leaves. A `prefix` entry names
        another table, either by key or — some of them do this — inline as the
        table itself, so both shapes are followed. Around 12,000 pages for a
        2015 Camaro, in about two seconds, all of it local.
        """
        found: dict[str, str] = {}
        seen: set[str] = set()

        def walk(node, prefix: str, depth: int) -> None:
            if depth > DEEPEST:
                return
            if isinstance(node, str):
                if node in seen:
                    return
                seen.add(node)
                cached = self._tables.get(node)
                if cached is None:
                    raw = self.pages.get(node.encode())
                    if not raw:
                        return
                    try:
                        cached = json.loads(raw)
                    except ValueError:
                        return
                    if len(self._tables) >= MOST_CACHED:
                        self._tables.clear()
                    self._tables[node] = cached
                node = cached
            if not isinstance(node, dict):
                return
            for path, key in (node.get("final") or {}).items():
                if isinstance(key, str):
                    found[prefix + unquote(str(path))] = key
            for path, key in (node.get("prefix") or {}).items():
                walk(key, prefix + unquote(str(path)), depth + 1)

        walk(vehicle.table, "", 0)
        return found

    def page(self, key: str) -> str:
        """One document, as text. Empty when the key is not in the table."""
        raw = self.pages.get(key.encode())
        return raw.decode("utf-8", "replace") if raw else ""

    # -- housekeeping -----------------------------------------------------

    def close(self) -> None:
        self.pages.close()

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()

    def __repr__(self) -> str:
        return f"<Library {self.root} ({self.build})>"
