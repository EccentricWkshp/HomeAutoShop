"""
Harvest trouble-code definitions from an online manual library, per make.

`build_dtc_list` transcribes one document somebody hands it. This walks a
library — LEMON Manuals, or Operation CHARM, which share a URL shape — and
pools the code tables by make. It exists because the per-make summaries that
circulate free are thin where it matters: a 2004 Aerio's own manual named 144
codes the whole Suzuki summary did not have, nearly all body and chassis.

**One year per make, not every year.** The library holds around 70,000 vehicle
configurations from 1996 on — Suzuki alone is 902 — and codes barely move
between model years of the same make. So for each make this finds a year that
actually has code pages and reads a couple of vehicles from it. Gaps get filled
by running it again with `--vehicles` raised, which is cheap because it
resumes.

**Which year has the codes cannot be assumed.** Coverage is not a recent-first
gradient, it is clumpy: BMW, Volvo and Jaguar each list every model year from
1996 to 2025 and keep their code pages in 2004, 2011 and 2017. Trying the
newest handful and stopping recorded all three as makes with no codes at all.
So the years are probed in a spread across the whole range -- see
:func:`_year_order` -- and a make that still yields nothing is recorded with
the reason, so a gap is visible and says which kind of gap it is.

**It is somebody else's server.** One request at a time with a delay, an
identifying User-Agent, and `/bundle/` — the one path both libraries'
robots.txt disallows — is never fetched. `--dry-run` sizes the job first.

Reading the page is :mod:`homeautoshop.diagnostics.manuals`, which finds the
description column from the table header rather than counting to it. Publishers
disagree about layout, and a positional rule produces rows that say `-` while
looking exactly like definitions.

What lands in `--out` is **staging**, not a shipped list: one JSON per make for
a person to look at. Nothing reaches `diagnostics/codelists/` by itself —
whether this material may be published is a question about somebody else's
compilation, and a crawler does not get to answer it.

    python manage.py crawl_dtc_manuals --dry-run
    python manage.py crawl_dtc_manuals --make Volkswagen --make GMC
    python manage.py crawl_dtc_manuals --vehicles 2
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from homeautoshop.diagnostics import dtc, manuals

#: OBD-II was mandated in the US for model year 1996. Earlier vehicles carry a
#: manufacturer's own flash-code numbering that shares nothing with `P0420`.
OBD2_FROM = 1996

DEFAULT_BASE = "https://lemon-manuals.la"

#: Said plainly, with somewhere to look. A crawler that hides what it is gives
#: an operator no way to ask it to stop.
USER_AGENT = (
    "HomeAutoShop-dtc-harvest/1.0 (self-hosted shop record; "
    "+https://github.com/eccentricworkshop)"
)

#: Never fetched: the one path both libraries' robots.txt disallows.
FORBIDDEN = re.compile(r"/bundle/", re.I)

#: Labels worth descending into on the way to a code table.
TOWARDS_CODES = re.compile(
    r"diagnostic\s*trouble\s*code|\bDTCs?\b|quick\s*lookups|repair\s*and\s*diagnosis", re.I
)

LINK = re.compile(r"""<a[^>]*href=['"]([^'"]+)['"][^>]*>(.*?)</a>""", re.S | re.I)
YEAR = re.compile(r"^(\d{4})/?$")

#: A vehicle's own pages, at most. A GM DTC index links a page per system and
#: there are dozens, so a small budget reads two of them and reports the make
#: as thin rather than as unfinished.
PAGES_PER_VEHICLE = 90

#: Vehicles to open in one model year before deciding that year has no code
#: pages. A vehicle without them is cheap -- its page, its Repair and Diagnosis
#: listing, and nothing matching -- but a year holds dozens, and reading all of
#: them to establish one negative is not worth somebody else's bandwidth.
PROBES_PER_YEAR = 4

#: Vehicles to take from any one model year. The budget is spread across years
#: rather than spent inside one, because a thin year is not evidence that the
#: make is thin — BMW's 2009 pages define 19 codes between them where Jaguar's
#: 2016 single-page manual defines 1,249. Raising `--vehicles` should reach
#: another year, not read two more of the same one.
VEHICLES_PER_YEAR = 2

#: How far below the vehicle a code table sits. Five, because VAG nests it
#: `Repair and Diagnosis / Quick Lookups / DTC Index / DTC Index (6CYL) /
#: Diagnostic Trouble Codes (DTC) Index` — a limit of four found nothing for
#: Volkswagen and reported the whole make as having no code pages, which is
#: the failure mode worth guarding: a depth limit that reads as an absence.
DEEPEST = 6


def _year_order(years: list[int]) -> list[int]:
    """Newest first, then spread across the range rather than marching down it.

    Coverage is not a recent-first gradient, it is clumpy. BMW, Volvo and
    Jaguar each list every model year from 1996 to 2025, and keep their code
    pages in 2004, 2011 and 2017. Taking the newest four and stopping recorded
    all three as makes with no codes at all — the same shape of bug as the
    depth limit above, and the one this command exists to avoid committing: a
    search rule that reads back as an absence.

    So after the two newest, which are still the better manual where there is a
    choice, the remainder is bisected. That puts a probe within a few years of
    anywhere in a thirty-year range inside five tries, and still reaches every
    year eventually.

    `years` is newest-first.
    """
    order = list(years[:2])
    rest = years[2:]
    spans = [(0, len(rest))]
    while spans:
        low, high = spans.pop(0)
        if low >= high:
            continue
        middle = (low + high) // 2
        order.append(rest[middle])
        spans.append((low, middle))
        spans.append((middle + 1, high))
    return order


class Command(BaseCommand):
    help = "Harvest DTC definitions per make from an online manual library."

    def add_arguments(self, parser):
        parser.add_argument("--base", default=DEFAULT_BASE, help="Library root URL.")
        parser.add_argument(
            "--since", type=int, default=OBD2_FROM,
            help=f"Earliest model year to consider (default {OBD2_FROM}, when OBD-II began).",
        )
        parser.add_argument("--make", action="append", default=[], help="Only these. Repeatable.")
        parser.add_argument("--delay", type=float, default=1.0, help="Seconds between requests.")
        parser.add_argument(
            "--vehicles", type=int, default=2,
            help="Vehicles to read per make once a year with codes is found.",
        )
        parser.add_argument(
            "--years", type=int, default=0,
            help="Model years to probe per make; 0 (the default) means every "
                 "year from --since, in the order --year-order picks.",
        )
        parser.add_argument(
            "--out", default="Artifacts/samples/dtc-lists/harvest",
            help="Where the per-make files and the resume state are written.",
        )
        parser.add_argument("--dry-run", action="store_true", help="Report the size and stop.")
        parser.add_argument("--restart", action="store_true", help="Ignore saved progress.")

    def handle(self, *args, **options):
        self.base = options["base"].rstrip("/")
        self.delay = options["delay"]
        self.last_fetch = 0.0
        out = Path(options["out"])
        out.mkdir(parents=True, exist_ok=True)

        self.state_file = out / "_progress.json"
        self.out = out
        self.state = (
            {"makes": {}, "empty": {}}
            if options["restart"] or not self.state_file.exists()
            else json.loads(self.state_file.read_text(encoding="utf-8"))
        )
        if isinstance(self.state.get("empty"), list):
            # Earlier runs recorded only the name. "No codes" and "not a make"
            # and "nothing built after 1979" are three different findings.
            self.state["empty"] = {m: "recorded before reasons were kept" for m in self.state["empty"]}

        wanted = {m.strip().lower() for m in options["make"]}
        makes = [
            (name, url)
            for name, url in self._children(self.base)
            if not wanted or name.lower() in wanted
        ]
        if not makes:
            raise CommandError("No makes matched — is --base right?")
        self.stdout.write(f"{len(makes)} makes at {self.base}")
        if options["dry_run"]:
            self.stdout.write(
                f"Would read up to {options['vehicles']} vehicles from each, probing "
                + (f"up to {options['years']} model years" if options["years"]
                   else "model years across the whole range until one pays out")
                + f" — roughly {len(makes) * options['vehicles'] * 12 * self.delay / 60:.0f} "
                "minutes if codes turn up early, several times that where they do not."
            )
            return

        for name, url in makes:
            if name in self.state["makes"] or name in self.state["empty"]:
                self.stdout.write(f"  {name}: already done")
                continue
            self._harvest_make(name, url, options)
            self._save()

        done = len(self.state["makes"])
        self.stdout.write(
            self.style.SUCCESS(
                f"{done} makes with codes, {len(self.state['empty'])} with none. Files in {out}"
            )
        )

    # -- one make ---------------------------------------------------------

    def _harvest_make(self, make: str, url: str, options) -> None:
        """A model year that actually has code pages, and a couple of vehicles.

        Which year that is cannot be assumed; see :func:`_year_order`. A year
        that pays out nothing after a few vehicles is stepped over rather than
        taken as an answer about the make, and **only definitions count as
        paying out** — an index that names codes without defining them is kept
        but does not end the search.
        """
        listed = {
            int(YEAR.match(label.strip()).group(1)): link
            for label, link in self._children(url)
            if YEAR.match(label.strip())
        }
        if not listed:
            # Site chrome, not a make: "About LEMON Manuals" sits in the same
            # listing as the makes. Recording it as a make with no codes is a
            # different and much more alarming claim than it deserves.
            self.stdout.write(f"  {make}: not a make — no model years listed")
            return

        usable = [y for y in sorted(listed, reverse=True) if y >= options["since"]]
        if not usable:
            # Opel's listing stops in 1979. A make that predates OBD-II has
            # no codes of this kind to find, which is a fact about the make
            # rather than a hole in the harvest.
            return self._nothing(
                make, f"nothing from {options['since']} on "
                      f"(listed {min(listed)}–{max(listed)})"
            )

        order = _year_order(usable)
        if options["years"]:
            order = order[: options["years"]]

        pooled: dict[str, str] = {}
        undefined: set[str] = set()
        tried: list[int] = []
        # Which vehicles the codes were actually read from. A list pooled from
        # one 2016 XE and offered for the whole make is a wider claim than
        # the document makes, and §8.1a's rule for that is that the source
        # names the vehicle. Nothing can write that citation later from a
        # count of years, so it is recorded while it is known.
        vehicles: list[str] = []
        read = 0
        for year in order:
            tried.append(year)
            probes = taken = 0
            for model, vehicle_url in self._children(listed[year]):
                if (read >= options["vehicles"] or probes >= PROBES_PER_YEAR
                        or taken >= VEHICLES_PER_YEAR):
                    break
                probes += 1
                found = self._harvest_vehicle(vehicle_url)
                if not found:
                    continue
                undefined |= set(found.undefined)
                if not found.codes:
                    # An index names codes and defines none, and naming is not
                    # what a lookup is for. Letting it end the year search cost
                    # Jaguar its manual: 2009 lists 830 codes and defines none,
                    # 2016 defines 1,249, and stopping at the first year that
                    # yielded *anything* took the index and never looked again.
                    # Keep the names, keep going.
                    self.stdout.write(
                        f"  {make} {year} {model[:40]}: {len(found.undefined)} named only"
                    )
                    continue
                read += 1
                taken += 1
                vehicles.append(f"{year} {model}")
                probes = 0  # a year that is paying out is worth staying in
                pooled.update({c: t for c, t in found.codes.items() if c not in pooled})
                self.stdout.write(
                    f"  {make} {year} {model[:40]}: {len(found.codes)} defined, "
                    f"{len(found.undefined)} named only"
                )
            if read >= options["vehicles"]:
                break

        if not pooled and not undefined:
            return self._nothing(make, f"no code pages in {len(tried)} model years tried")

        self.state["makes"][make] = {
            "codes": pooled,
            "named_only": sorted(undefined - set(pooled)),
            "years_tried": tried,
            "vehicles": vehicles,
        }

    def _nothing(self, make: str, why: str) -> None:
        """Record *why* a make yielded nothing, not merely that it did."""
        self.state["empty"][make] = why
        self.stdout.write(self.style.WARNING(f"  {make}: {why}"))

    def _harvest_vehicle(self, vehicle_url: str) -> manuals.Harvest | None:
        """Walk one vehicle towards its code tables and read every one found.

        Breadth-first with a page budget, because the libraries disagree about
        depth: one puts the whole table under `A L L Diagnostic Trouble Codes`,
        another nests it under Quick Lookups, a DTC Index, and then a page per
        system. Following labels rather than a fixed path reads both.
        """
        total = manuals.Harvest(shape="none")
        queue, seen, budget = [(vehicle_url, 0)], {vehicle_url}, PAGES_PER_VEHICLE

        while queue and budget > 0:
            url, depth = queue.pop(0)
            if depth > DEEPEST:
                continue
            try:
                body = self._get(url)
            except Exception as exc:
                self.stderr.write(f"    ! {url[-60:]}: {exc}")
                continue
            budget -= 1

            found = manuals.read(body)
            total.codes.update(found.codes)
            total.undefined.update(found.undefined)
            total.dropped += found.dropped
            if found.codes:
                # A page that yielded a table is a leaf; its links go to
                # per-code detail pages and there are hundreds of them.
                continue
            for label, link in self._links(url, body, straight_down=False):
                if link not in seen and TOWARDS_CODES.search(label):
                    seen.add(link)
                    queue.append((link, depth + 1))

        if total.codes:
            total.shape = "table"
        elif total.undefined:
            total.shape = "index"
        return total if total else None

    # -- fetching ---------------------------------------------------------

    def _get(self, url: str) -> str:
        if FORBIDDEN.search(url):
            raise CommandError(f"refusing a disallowed path: {url}")
        wait = self.delay - (time.monotonic() - self.last_fetch)
        if wait > 0:
            time.sleep(wait)
        self.last_fetch = time.monotonic()
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.read().decode("utf-8", "replace")

    def _links(self, url: str, body: str, *, straight_down: bool = True) -> list[tuple[str, str]]:
        """Links below this page, dropping breadcrumbs and site chrome.

        `straight_down` is one segment deeper, which is how the navigation is
        built: root to make to year to model, a level at a time.

        **Below the vehicle it has to be off**, because a Repair and Diagnosis
        page does not link a level at a time — it links straight to
        `Quick Lookups/DTC Index/DTC Index (6CYL)/`, three deeper. Requiring
        one rejected all twenty of Volkswagen's DTC links and reported the
        make as having no code pages at all, which is why a depth rule that
        reads as an absence is worth being careful with.
        """
        here = [p for p in urllib.parse.urlparse(url).path.split("/") if p]
        seen, out = set(), []
        for href, label in LINK.findall(body):
            text = re.sub(r"<[^>]+>", " ", label)
            text = " ".join(text.split())
            if not text or href.startswith(("#", "javascript:")) or FORBIDDEN.search(href):
                continue
            absolute = urllib.parse.urljoin(url if url.endswith("/") else url + "/", href)
            if not absolute.startswith(self.base) or absolute in seen:
                continue
            there = [p for p in urllib.parse.urlparse(absolute).path.split("/") if p]
            if there[: len(here)] != here or len(there) <= len(here):
                continue
            if straight_down and len(there) != len(here) + 1:
                continue
            seen.add(absolute)
            out.append((text, absolute))
        return out

    def _children(self, url: str) -> list[tuple[str, str]]:
        try:
            return self._links(url, self._get(url))
        except (urllib.error.URLError, OSError) as exc:
            self.stderr.write(f"  ! {url}: {exc}")
            return []

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
                        "read_from": held.get("vehicles", []),
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
