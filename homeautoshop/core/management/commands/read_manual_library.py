"""
Harvest trouble-code definitions from a manual library held on disk.

This is `crawl_dtc_manuals` without the crawl. Given the library's own two
files — `index.json` and `pages.mtbl` — every page is a local lookup, so the
things that shaped the crawler stop applying: there is no request budget, no
delay between fetches, no guessing which model year might have code pages, and
no load on anybody else's server. What took a day and sampled two vehicles per
make finishes in minutes and reads until the answer stops growing.

The crawler stays. Not everybody has thirty gigabytes of library on a NAS, and
it is the only way in for a site that publishes no dump.

**It reads the pages, not the tree over them.** `Repair and Diagnosis (Single
Page)` sounds like the whole manual and is a navigation document: 19 MB, no
tables. Reading only that put Volkswagen and Audi at zero and Chevrolet at 63,
because those makes label their tree nodes with a bare code where Jaguar labels
them with a code and its meaning. The definitions are in the pages the tree
points at, reached by walking the routing tables.

**Content is shared and read once.** Pages are keyed by hash, so sibling
vehicles overwhelmingly point at the same ones; a key already read is skipped
rather than parsed again.

**It reads across the model years, not down from the newest.** The same rule
the crawler had to learn: coverage is clumpy, and a make whose code tables sit
in 2016 is not helped by six manuals from 2025. Manuals are taken a round at a
time across a spread of years, so the first handful sample the whole range.

**It stops when it stops learning.** A make is finished when `--patience`
vehicles in a row contribute no definition that is not already held. That is a
statement about the make being covered rather than about any one document,
which matters: an earlier version stopped on documents that defined nothing,
and six of those in a row concluded that Jaguar — 1,249 definitions in a single
2016 manual — had no codes at all.

**The makes are named as a shop names them.** The corpus files Ram under
`Dodge and Ram`, calls Nissan `Nissan-Datsun`, and calls GM `General Motors`.
Those are its names for its own filing, not the ones a catalog entry should
carry, so `make_of` maps them on the way in.

**Nothing here is published.** What lands in `--out` is staging, one file per
make with `source` left empty, exactly as the crawler leaves it: whether
somebody else's compilation may be republished is not a question a command
gets to answer. `build` records which edition of the library it came from,
because a later dump is a different document rather than a newer version of
this one — the edition read here calls a 2005 Isuzu `Ascender LS, 4.2 S, 4WD`
where the live site calls the same vehicle `Ascender 4WD L6-4.2L`.

    python manage.py read_manual_library --root /mnt/lemon --dry-run
    python manage.py read_manual_library --root /mnt/lemon --make Jaguar
    python manage.py read_manual_library --root /mnt/lemon --make Ram
    python manage.py read_manual_library --root /mnt/lemon
"""

from __future__ import annotations

import json
import re
import time
from collections import defaultdict
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from homeautoshop.diagnostics import dtc, library, manuals

#: Which pages under a vehicle are worth opening. Deliberately loose: reading
#: a page that turns out not to be a code table costs a local lookup and
#: returns nothing, while missing one costs a definition nobody else has.
DTC_PATH = re.compile(r"trouble\s*code|\bDTCs?\b", re.I)

#: OBD-II was mandated in the US for model year 1996. Earlier vehicles carry a
#: manufacturer's own flash-code numbering that shares nothing with `P0420`.
OBD2_FROM = 1996

#: Vehicles in a row that may contribute nothing new before a make is called
#: covered. Counted over vehicles rather than documents: a document that
#: defines nothing is evidence about that document, and treating it as evidence
#: about the make is what put Jaguar at zero.
PATIENCE = 12

#: What the corpus calls a make, and what this project files it under.
#: `Nissan-Datsun` carries a name dropped in 1986, and `General Motors` names
#: the group where a shop writes GM on the ticket.
RENAMED = {
    "Nissan-Datsun": "Nissan",
    "General Motors": "GM",
    # The catalog has covered this make as VW since before the harvest; a
    # second entry under the longer name would split one make across two.
    "Volkswagen": "VW",
}

#: Ram is a make of its own, and the corpus still files it with Dodge. The
#: truck and van lines go across; the cars stay.
#:
#: Splitting needs the model *and* the year, because the model alone gets it
#: backwards: `Ram Van` and `Ram Wagon` ran from the 1970s to 2003 and are
#: Dodges, the older use of the word, while the vehicles that became Ram are
#: named for their tonnage. Nothing appears on both sides after the split, so
#: the two lists are clean.
#:
#: The boundary year is soft — the brands separated over two model years, and
#: sources disagree about which side 2010 sits on. It decides only which of two
#: heavily overlapping lists a 2010 truck contributes to, since both makes are
#: harvested from the same service manuals.
RAM_FROM = 2011
RAM_MODELS = re.compile(
    r"^(?:1500|2500|3500|4500|5500|ProMaster|Dakota|Pickup|Cab & Chassis|C/V)\b",
    re.I,
)


def make_of(vehicle) -> str:
    """The make this project files a vehicle under."""
    if vehicle.make == "Dodge and Ram":
        return (
            "Ram"
            if vehicle.year >= RAM_FROM and RAM_MODELS.match(vehicle.model)
            else "Dodge"
        )
    return RENAMED.get(vehicle.make, vehicle.make)


#: And a ceiling regardless, so one enormous make cannot run all afternoon.
#: Ford is covered by 3,882 distinct manuals and Chevrolet by 3,470; reading
#: every one of them would be most of a day for the last few per cent.
MOST_MANUALS = 150


def _spread(manuals_for: dict) -> list:
    """The manuals, ordered so the first handful cover the whole year range.

    Taken a round at a time across the years, in the order `_year_order` puts
    them: the two newest first, because a recent manual covers what an older
    one had, and then a bisection of the rest so a few reads land near anywhere
    in thirty years. Marching down from the newest is what let six manuals from
    2025 decide that Jaguar had no codes.
    """
    from homeautoshop.core.management.commands.crawl_dtc_manuals import _year_order

    by_year: dict[int, list] = defaultdict(list)
    for key, vehicle in manuals_for.items():
        by_year[vehicle.year].append((key, vehicle))

    order = _year_order(sorted(by_year, reverse=True))
    out, round_at = [], 0
    while len(out) < len(manuals_for):
        for year in order:
            if round_at < len(by_year[year]):
                out.append(by_year[year][round_at])
        round_at += 1
    return out


class Command(BaseCommand):
    help = "Harvest DTC definitions per make from a manual library on disk."

    def add_arguments(self, parser):
        parser.add_argument("--root", required=True, help="Folder holding index.json and pages.mtbl.")
        parser.add_argument(
            "--since", type=int, default=OBD2_FROM,
            help=f"Earliest model year to read (default {OBD2_FROM}, when OBD-II began).",
        )
        parser.add_argument("--make", action="append", default=[], help="Only these. Repeatable.")
        parser.add_argument(
            "--manuals", type=int, default=MOST_MANUALS,
            help=f"Most manuals to read for one make (default {MOST_MANUALS}).",
        )
        parser.add_argument(
            "--patience", type=int, default=PATIENCE,
            help=f"Stop a make after this many manuals in a row add nothing (default {PATIENCE}).",
        )
        parser.add_argument(
            "--out", default="Artifacts/samples/dtc-lists/library",
            help="Where the per-make files and the resume state are written.",
        )
        parser.add_argument("--dry-run", action="store_true", help="Report the size and stop.")
        parser.add_argument("--restart", action="store_true", help="Ignore saved progress.")

    # -- the whole run ----------------------------------------------------

    def handle(self, *args, **options):
        try:
            self.library = library.Library(options["root"])
        except library.NotALibrary as exc:
            raise CommandError(str(exc))

        out = Path(options["out"])
        out.mkdir(parents=True, exist_ok=True)
        self.out = out
        self.state_file = out / "_progress.json"
        self.state = (
            {"build": self.library.build, "makes": {}, "empty": {}}
            if options["restart"] or not self.state_file.exists()
            else json.loads(self.state_file.read_text(encoding="utf-8"))
        )
        if self.state.get("build") != self.library.build:
            raise CommandError(
                f"{out} holds a harvest of {self.state.get('build')!r} and this is "
                f"{self.library.build!r}. Use --restart, or a different --out: mixing "
                "two editions in one folder would leave nothing able to say which "
                "document a definition came from."
            )

        self.stdout.write(f"{self.library!r}")
        started = time.monotonic()
        listed = self.library.vehicles()
        wanted = {m.strip().lower() for m in options["make"]}
        by_make: dict[str, list] = defaultdict(list)
        for vehicle in listed:
            name = make_of(vehicle)
            # Either spelling is accepted for --make, so a run can be repeated
            # from what the corpus says or from what the catalog says.
            if vehicle.year >= options["since"] and (
                not wanted
                or name.lower() in wanted
                or vehicle.make.lower() in wanted
            ):
                by_make[name].append(vehicle)
        self.stdout.write(
            f"{len(listed):,} vehicles, {len(by_make)} makes from {options['since']} "
            f"({time.monotonic() - started:.0f}s)"
        )
        if not by_make:
            raise CommandError("No makes matched — is --root right, and --make spelled as the library spells it?")

        if options["dry_run"]:
            for make, vehicles in sorted(by_make.items(), key=lambda r: -len(r[1])):
                self.stdout.write(f"  {make:24} {len(vehicles):6,} vehicles")
            return

        for make in sorted(by_make):
            if make in self.state["makes"] or make in self.state["empty"]:
                self.stdout.write(f"  {make}: already done")
                continue
            self._harvest(make, by_make[make], options)
            self._save()

        held = self.state["makes"]
        defined = sum(len(v["codes"]) for v in held.values())
        self.stdout.write(
            self.style.SUCCESS(
                f"{len(held)} makes with definitions, {len(self.state['empty'])} without. "
                f"{defined:,} definitions in {out} ({time.monotonic() - started:.0f}s)"
            )
        )

    # -- one make ---------------------------------------------------------

    def _harvest(self, make: str, vehicles: list, options) -> None:
        """Every distinct manual for a make, spread across the years it covers."""
        manuals_for: dict[str, object] = {}
        shared = 0
        for vehicle in sorted(vehicles, key=lambda v: -v.year):
            key = self.library.manual_key(vehicle)
            if not key:
                continue
            if key in manuals_for:
                shared += 1
                continue
            manuals_for[key] = vehicle

        if not manuals_for:
            return self._nothing(make, f"{len(vehicles)} vehicles, none with a manual")

        pooled: dict[str, str] = {}
        named: set[str] = set()
        read_from: list[str] = []
        seen: set[str] = set()
        quiet = read = pages = 0
        for _key, vehicle in _spread(manuals_for):
            if read >= options["manuals"] or quiet >= options["patience"]:
                break
            read += 1
            fresh: dict[str, str] = {}
            for path, key in self.library.pages_for(vehicle).items():
                if key in seen or not DTC_PATH.search(path):
                    continue
                seen.add(key)
                pages += 1
                found = manuals.read(self.library.page(key))
                named |= set(found.undefined)
                fresh.update(
                    {c: t for c, t in found.codes.items() if c not in pooled}
                )
            if not fresh:
                quiet += 1
                continue
            pooled.update(fresh)
            read_from.append(vehicle.name)
            quiet = 0
            self.stdout.write(
                f"  {make} {vehicle.name[:44]:46} +{len(fresh):5} (now {len(pooled)})"
            )

        if not pooled and not named:
            return self._nothing(
                make,
                f"read {read} vehicles and {pages} code pages, none defined anything",
            )

        self.state["makes"][make] = {
            "codes": pooled,
            "named_only": sorted(named - set(pooled)),
            "read_from": read_from,
            "manuals_available": len(manuals_for),
            "manuals_read": read,
            "pages_read": pages,
            "vehicles": len(vehicles),
            "shared": shared,
        }

    def _nothing(self, make: str, why: str) -> None:
        """Record *why* a make yielded nothing, not merely that it did."""
        self.state["empty"][make] = why
        self.stdout.write(self.style.WARNING(f"  {make}: {why}"))

    # -- writing ----------------------------------------------------------

    def _save(self) -> None:
        self.state_file.write_text(json.dumps(self.state), encoding="utf-8")
        for make, held in self.state["makes"].items():
            codes = held["codes"]
            slug = re.sub(r"[^a-z0-9]+", "-", make.lower()).strip("-")
            (self.out / f"{slug}.json").write_text(
                json.dumps(
                    {
                        "make": make,
                        "scope": "make",
                        "aliases": [],
                        "source": "",  # filled in by whoever decides to ship it
                        "build": self.state.get("build", ""),
                        "read_from": held.get("read_from", []),
                        "manufacturer_controlled": sum(
                            1 for c in codes if not dtc.parse(c)["is_iso_sae"]
                        ),
                        "named_only": held.get("named_only", []),
                        "codes": dict(sorted(codes.items())),
                    },
                    indent=1,
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )
