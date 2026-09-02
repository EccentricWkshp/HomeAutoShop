"""
Look through a manual library for definitions of particular codes.

`read_manual_library` reads a make until it stops learning and writes down
everything it found. This reads for a named list of codes instead, which is the
cheaper half of the same job and the one wanted after the fact: a refusal rule
added *after* a harvest leaves entries removed and nothing put back, and a make
covered by an index that names a code without defining it leaves the same gap.
Re-running a whole harvest to answer nine codes costs an hour a make.

**It parses almost nothing.** A page is only handed to `manuals.read` when the
page text actually contains one of the codes being looked for — a substring
test against a string already in memory, against parsing every table on every
one of twelve thousand pages. That is the whole reason this is quick.

**It stops as soon as it has them.** A make ends when every code on its list
has an answer, so the common case — a handful of codes, defined in the first
manuals opened — costs seconds rather than the make's whole budget.

**A code that is never defined is reported as such**, because that is the
answer to the question being asked. `P1000`, `B2000` and `U2000` are the first
number of each manufacturer-controlled block, and a chart lists them to label
the block rather than to define a fault; searching every Chrysler manual for
them and finding nothing is what tells you so.

    python manage.py read_manual_codes --root /mnt/lemon --targets gaps.json
    python manage.py read_manual_codes --root /mnt/lemon --make Audi --code P11A2
"""

from __future__ import annotations

import json
import re
import time
from collections import defaultdict
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from homeautoshop.diagnostics import library, manuals

from .read_manual_library import DTC_PATH, OBD2_FROM, _spread, make_of

#: Manuals to open for one make before giving up on whatever is still missing.
#: Lower than a harvest's, because this is looking for specific codes rather
#: than trying to cover a make: if sixty manuals spread across the years have
#: not defined a code, a hundred more are unlikely to.
MOST_MANUALS = 60


class Command(BaseCommand):
    help = "Search a manual library for definitions of particular codes."

    def add_arguments(self, parser):
        parser.add_argument("--root", required=True, help="Folder holding index.json and pages.mtbl.")
        parser.add_argument(
            "--targets", default="",
            help='JSON file of {"Make": ["P1000", ...]}. Or use --make with --code.',
        )
        parser.add_argument("--make", action="append", default=[], help="A make to search.")
        parser.add_argument("--code", action="append", default=[], help="A code to look for.")
        parser.add_argument(
            "--manuals", type=int, default=MOST_MANUALS,
            help=f"Manuals to open per make (default {MOST_MANUALS}).",
        )
        parser.add_argument("--out", default="", help="Where to write what was found.")

    def handle(self, *args, **options):
        wanted = self._targets(options)
        if not wanted:
            raise CommandError("Nothing to look for — pass --targets, or --make with --code.")

        try:
            self.library = library.Library(options["root"])
        except library.NotALibrary as exc:
            raise CommandError(str(exc))

        by_make: dict[str, list] = defaultdict(list)
        for vehicle in self.library.vehicles():
            name = make_of(vehicle)
            if vehicle.year >= OBD2_FROM and name in wanted:
                by_make[name].append(vehicle)

        started = time.monotonic()
        found: dict[str, dict[str, str]] = {}
        missing: dict[str, list[str]] = {}
        for make in sorted(wanted):
            if make not in by_make:
                self.stdout.write(self.style.WARNING(f"  {make}: not in this library"))
                missing[make] = sorted(wanted[make])
                continue
            got = self._look(make, by_make[make], set(wanted[make]), options["manuals"])
            if got:
                found[make] = got
            still = sorted(set(wanted[make]) - set(got))
            if still:
                missing[make] = still
                self.stdout.write(
                    self.style.WARNING(f"  {make}: no definition for {' '.join(still)}")
                )

        defined = sum(len(v) for v in found.values())
        asked = sum(len(v) for v in wanted.values())
        self.stdout.write(
            self.style.SUCCESS(
                f"{defined} of {asked} codes defined ({time.monotonic() - started:.0f}s)"
            )
        )
        if options["out"]:
            Path(options["out"]).write_text(
                json.dumps({"found": found, "missing": missing}, indent=1, ensure_ascii=False)
                + "\n",
                encoding="utf-8",
            )
            self.stdout.write(f"written to {options['out']}")

    # -- one make ---------------------------------------------------------

    def _look(self, make: str, vehicles: list, wanted: set[str], budget: int) -> dict[str, str]:
        """Definitions for as many of `wanted` as this make's manuals give up."""
        manuals_for: dict[str, object] = {}
        for vehicle in sorted(vehicles, key=lambda v: -v.year):
            key = self.library.manual_key(vehicle)
            if key and key not in manuals_for:
                manuals_for[key] = vehicle
        if not manuals_for:
            return {}

        hunting = re.compile("|".join(sorted(re.escape(c) for c in wanted)))
        out: dict[str, str] = {}
        seen: set[str] = set()
        read = 0
        for _key, vehicle in _spread(manuals_for):
            if read >= budget or not (wanted - set(out)):
                break
            read += 1
            for path, key in self.library.pages_for(vehicle).items():
                if key in seen or not DTC_PATH.search(path):
                    continue
                seen.add(key)
                page = self.library.page(key)
                # The cheap test that makes this worth running: no target code
                # in the text means no reason to parse the tables.
                if not hunting.search(page):
                    continue
                for code, text in manuals.read(page).codes.items():
                    if code in wanted and code not in out:
                        out[code] = text
                        self.stdout.write(f"  {make} {code}: {text[:64]}")
        return out

    @staticmethod
    def _targets(options) -> dict[str, list[str]]:
        if options["targets"]:
            path = Path(options["targets"])
            if not path.exists():
                raise CommandError(f"{path} does not exist.")
            raw = json.loads(path.read_text(encoding="utf-8"))
            return {str(k): [str(c).upper() for c in v] for k, v in raw.items() if v}
        if options["make"] and options["code"]:
            codes = [c.upper() for c in options["code"]]
            return {m: codes for m in options["make"]}
        return {}
