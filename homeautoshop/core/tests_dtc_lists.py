"""
`manage.py build_dtc_list` — transcribing somebody's published code list.

The source documents are not in this repository, so the only way to check a
committed table against what it came from is to run the transcription again.
That is why this is a command; these are the properties that make its output
worth trusting.

**The copy check is the part that earns its keep.** Multi-make compilations
circulate widely and are mostly one make's list with the attribution filed off.
Three were examined while building this and all three had the fault: a "Duramax"
troubleshooting manual whose 671 manufacturer-specific codes were 670 Ford ones,
611 word for word; the most-starred OBD code dataset on GitHub, which is the
same Ford document served as make-agnostic; and a second database that had
merged two makes' definitions into single strings — `P1106` reading "Dual
Alternator Lower Fault Manifold Absolute Pressure (MAP) Sensor Circuit
Intermittent High Voltage", which is Ford's definition with somebody else's
welded on. Shipping any of them would put one make's definitions on another's
vehicle, which is the exact failure that scoping definitions by make exists to
prevent.
"""

from __future__ import annotations

import json
from io import StringIO
from unittest import mock
from pathlib import Path

from django.core.management import CommandError, call_command
from django.test import SimpleTestCase, TestCase

from homeautoshop.diagnostics import dtc, manuals, manuals

PAGE = """
<table>
  <tr><th>Code</th><th>System</th><th>Definition</th></tr>
  <tr>
    <td class="c-code"><a id="p1234"></a>P1234</td>
    <td class="c-cat"><span class="badge">Air &amp; Fuel Metering</span></td>
    <td class="c-def"><strong>Wastegate Position Sensor Performance</strong>
      <div class="dtc-def">Long paragraph of diagnostic advice that is not the definition.</div></td>
  </tr>
  <tr>
    <td class="c-code">B2345</td>
    <td class="c-cat"><span class="badge">Body</span></td>
    <td class="c-def"><strong>Driver Seat Position Sensor</strong></td>
  </tr>
</table>
"""


def only(written: dict) -> dict:
    """The codes of a bundle's single document.

    A published list is one file per *manufacturer* holding one or more
    documents, so there is a level here that a transcription of one document
    does not care about.
    """
    return written["documents"][0]["codes"]


def published(make: str) -> dict:
    """Every code published for a make, read off the catalog folder.

    A manufacturer's list is no longer in the image — it is published for a
    shop to install — so a test about what is *published* reads the catalog
    rather than asking `dtc`, which answers with what happens to be installed.
    """
    from homeautoshop.core.management.commands.build_dtc_list import (
        catalog_codes,
        slug_for,
    )

    data = json.loads(
        (catalog_codes() / f"{slug_for(make)}.json").read_text(encoding="utf-8")
    )
    codes: dict = {}
    for document in data["documents"]:
        codes.update({c: t for c, t in document["codes"].items() if c not in codes})
    return codes


class TranscribingTests(TestCase):
    def setUp(self):
        dtc._lists.cache_clear()
        self.addCleanup(dtc._lists.cache_clear)

    def run_it(self, document: str, name: str = "page.html", **options) -> dict:
        folder = Path(self.enterContext(__import__("tempfile").TemporaryDirectory()))
        (folder / name).write_text(document, encoding="utf-8")
        out = folder / "out.json"
        call_command(
            "build_dtc_list", str(folder / name), make=options.pop("make", "Testla"),
            out=str(out), stdout=StringIO(), stderr=StringIO(), **options,
        )
        return json.loads(out.read_text(encoding="utf-8"))

    def test_an_html_table_becomes_codes_and_definitions(self):
        written = self.run_it(PAGE)
        self.assertEqual(
            only(written),
            {
                "P1234": "Wastegate Position Sensor Performance",
                "B2345": "Driver Seat Position Sensor",
            },
        )

    def test_the_category_column_is_not_mistaken_for_the_definition(self):
        """A three-column table puts a category in the middle. Taking the first
        cell after the code filed "Air & Fuel Metering" as the meaning of every
        code on both of the lists this ships."""
        written = self.run_it(PAGE)
        self.assertNotIn("Air & Fuel Metering", only(written).values())

    def test_the_advice_paragraph_is_not_the_definition(self):
        """These pages put a short name in a heading and a paragraph of
        diagnostic advice under it. The advice is worth reading and is not what
        belongs in a column that says what a code means."""
        self.assertNotIn("Long paragraph", json.dumps(self.run_it(PAGE)))

    def test_a_plain_text_list_still_works(self):
        written = self.run_it("P1234 Wastegate stuck\nB2345 Seat sensor\n", name="list.txt")
        self.assertEqual(only(written)["P1234"], "Wastegate stuck")

    def test_a_line_that_is_not_a_code_is_skipped_not_guessed_at(self):
        out = StringIO()
        folder = Path(self.enterContext(__import__("tempfile").TemporaryDirectory()))
        (folder / "list.txt").write_text("P14 Truncated code line\nP1234 Real one\n")
        call_command(
            "build_dtc_list", str(folder / "list.txt"), make="Testla",
            out=str(folder / "out.json"), stdout=out, stderr=StringIO(),
        )
        written = json.loads((folder / "out.json").read_text())

        self.assertEqual(list(only(written)), ["P1234"])
        self.assertIn("P14 Truncated code line", out.getvalue())

    def test_a_document_that_defines_one_code_twice_reports_it(self):
        """Two meanings for one code inside one manufacturer's own document
        means the reading is wrong somewhere."""
        out = StringIO()
        folder = Path(self.enterContext(__import__("tempfile").TemporaryDirectory()))
        (folder / "list.txt").write_text("P1234 One thing\nP1234 A different thing\n")
        call_command(
            "build_dtc_list", str(folder / "list.txt"), make="Testla",
            out=str(folder / "out.json"), stdout=out, stderr=StringIO(),
        )
        self.assertIn("conflicts with", out.getvalue())

    def test_the_file_records_the_make_its_aliases_and_its_source(self):
        written = self.run_it(
            PAGE, make="Testla", alias=["Tesler"], source="Somebody's handbook"
        )
        self.assertEqual(written["make"], "Testla")
        self.assertEqual(written["aliases"], ["Tesler"])
        self.assertEqual(written["documents"][0]["source"], "Somebody's handbook")

    def test_nothing_code_shaped_is_an_error_rather_than_an_empty_file(self):
        with self.assertRaises(CommandError):
            self.run_it("<p>A page about something else entirely.</p>")


class ItRefusesSomebodyElsesListTests(TestCase):
    """The check that stopped three real documents being shipped."""

    def setUp(self):
        dtc._lists.cache_clear()
        self.addCleanup(dtc._lists.cache_clear)
        # Ten of Ford's own manufacturer-specific codes, verbatim, offered as
        # though they were another make's — which is what every compilation
        # examined turned out to be.
        ford = published("Ford")
        borrowed = [c for c in sorted(ford) if not dtc.parse(c)["is_iso_sae"]][:10]
        self.copy = "".join(f"{c} {ford[c]}\n" for c in borrowed)

    def call(self, **options):
        folder = Path(self.enterContext(__import__("tempfile").TemporaryDirectory()))
        (folder / "list.txt").write_text(self.copy, encoding="utf-8")
        out, path = StringIO(), folder / "out.json"
        try:
            call_command(
                "build_dtc_list", str(folder / "list.txt"), make="Chevrolet",
                out=str(path), stdout=out, stderr=StringIO(), **options,
            )
            refused = False
        except CommandError:
            refused = True
        return out.getvalue(), path, refused

    def test_a_copy_of_a_held_list_is_refused(self):
        _printed, path, refused = self.call()
        self.assertTrue(refused)
        self.assertFalse(path.exists(), "a refused list must not also be written")

    def test_it_says_whose_list_it_is_and_shows_the_evidence(self):
        """Refusing without saying why leaves somebody with a file they cannot
        tell apart from a good one."""
        printed, _path, _refused = self.call()

        self.assertIn("Ford", printed)
        self.assertIn("word for word", printed)

    def test_force_lets_somebody_who_disagrees_through(self):
        _printed, path, refused = self.call(force=True)
        self.assertFalse(refused)
        self.assertTrue(path.exists())

    def test_a_genuinely_different_list_passes(self):
        folder = Path(self.enterContext(__import__("tempfile").TemporaryDirectory()))
        (folder / "list.txt").write_text(
            "P1500 Something only this make does\nP1501 And another\n", encoding="utf-8"
        )
        out = folder / "out.json"
        call_command(
            "build_dtc_list", str(folder / "list.txt"), make="Testla",
            out=str(out), stdout=StringIO(), stderr=StringIO(),
        )
        self.assertTrue(out.exists())


class ProbingModelYearsTests(TestCase):
    """`crawl_dtc_manuals` — which model year to look in.

    This is the bug the whole command is shaped to avoid and still walked
    into twice: a search rule that reads back as an absence. A depth limit of
    four rejected Volkswagen's links and reported the make as having no code
    pages. Then taking the newest four model years did the same to BMW, Volvo
    and Jaguar, each of which lists every year from 1996 to 2025 and keeps its
    code pages in 2004, 2011 and 2017.
    """

    @staticmethod
    def _order(newest, oldest):
        from homeautoshop.core.management.commands.crawl_dtc_manuals import _year_order

        return _year_order(list(range(newest, oldest - 1, -1)))

    def test_every_year_is_reached(self):
        """The point of the fix. A year left untried is a make reported empty
        on the strength of never having looked."""
        order = self._order(2025, 1996)
        self.assertEqual(sorted(order), list(range(1996, 2026)))

    def test_no_year_is_probed_twice(self):
        order = self._order(2025, 1996)
        self.assertEqual(len(order), len(set(order)))

    def test_the_newest_two_are_tried_first(self):
        """A recent manual covers the codes an older one had and adds the ones
        since, so it is still the better read where there is a choice."""
        self.assertEqual(self._order(2025, 1996)[:2], [2025, 2024])

    def test_the_rest_is_spread_rather_than_marched_down(self):
        """Five probes should reach most of a thirty-year range, not just the
        top of it. Marching down from 2025 needs twenty-two before it sees a
        year BMW actually has codes for."""
        early = self._order(2025, 1996)[:6]
        self.assertLess(min(early), 2010, f"first six probes stayed recent: {early}")

    def test_a_short_range_is_handled_without_special_casing(self):
        for oldest in (2025, 2024, 2023):
            with self.subTest(oldest=oldest):
                order = self._order(2025, oldest)
                self.assertEqual(sorted(order), list(range(oldest, 2026)))


class OnlyDefinitionsEndTheSearchTests(TestCase):
    """Naming a code is not defining it, and the crawl has to know the
    difference when it decides it is finished with a make.

    Jaguar is the case. Its 2009 listing names 830 codes and defines none;
    its 2017 single-page manual defines 233. Treating the first year that
    yielded *anything* as the answer took the index and never looked again.
    """

    def _run(self, library, *, vehicles=1):
        """`library` maps a model year to what its one vehicle yields."""
        from homeautoshop.core.management.commands.crawl_dtc_manuals import Command

        command = Command()
        command.stdout = StringIO()
        command.state = {"makes": {}, "empty": {}}
        opened = []

        command._children = lambda url: (
            [(str(year), f"/M/{year}/") for year in library]
            if url == "/M/"
            else [(f"Sedan {n}", f"{url}Sedan{n}/") for n in (1, 2, 3)]
        )

        def harvest(url):
            year = int(url.split("/")[2])
            opened.append(year)
            return library[year]

        command._harvest_vehicle = harvest
        command._harvest_make("M", "/M/", {"since": 1996, "years": 0, "vehicles": vehicles})
        return command.state, opened

    def test_a_year_that_only_names_codes_does_not_end_the_search(self):
        state, opened = self._run({
            2025: manuals.Harvest(),
            2009: manuals.Harvest(undefined={"P1234": "", "P1235": ""}, shape="index"),
            2004: manuals.Harvest(codes={"P1111": "Fuel pump relay circuit"}, shape="table"),
        })

        self.assertIn(2004, opened, "gave up at the index year")
        self.assertEqual(state["makes"]["M"]["codes"], {"P1111": "Fuel pump relay circuit"})

    def test_the_names_the_index_year_gave_are_still_kept(self):
        """They are worth having — "this make has a P1234" is a real fact,
        just not the one a lookup answers."""
        state, _ = self._run({
            2009: manuals.Harvest(undefined={"P1234": ""}, shape="index"),
            2004: manuals.Harvest(codes={"P1111": "Fuel pump relay circuit"}, shape="table"),
        })

        self.assertEqual(state["makes"]["M"]["named_only"], ["P1234"])

    def test_a_year_that_defines_codes_does_end_it(self):
        """The budget still has to stop somewhere; definitions are where."""
        state, opened = self._run({
            2025: manuals.Harvest(codes={"P1111": "Fuel pump relay circuit"}, shape="table"),
            2009: manuals.Harvest(undefined={"P1234": ""}, shape="index"),
        })

        self.assertEqual(opened, [2025])

    def test_a_make_that_only_ever_names_codes_is_recorded_rather_than_lost(self):
        state, _ = self._run({2009: manuals.Harvest(undefined={"P1234": ""}, shape="index")})

        self.assertEqual(state["makes"]["M"]["codes"], {})
        self.assertEqual(state["makes"]["M"]["named_only"], ["P1234"])
        self.assertNotIn("M", state["empty"])

    def test_a_make_older_than_obd_ii_says_so(self):
        """Opel's listing stops in 1979. That is a fact about the make, not
        a hole in the harvest, and the two must not read alike."""
        from homeautoshop.core.management.commands.crawl_dtc_manuals import Command

        command = Command()
        command.stdout = StringIO()
        command.state = {"makes": {}, "empty": {}}
        command._children = lambda url: [(str(y), f"/M/{y}/") for y in (1960, 1979)]
        command._harvest_make("Opel", "/M/", {"since": 1996, "years": 0, "vehicles": 1})

        self.assertIn("1996", command.state["empty"]["Opel"])
        self.assertIn("1979", command.state["empty"]["Opel"])

    def test_site_chrome_is_not_a_make_with_no_codes(self):
        """"About LEMON Manuals" sits in the same listing as the makes."""
        from homeautoshop.core.management.commands.crawl_dtc_manuals import Command

        command = Command()
        command.stdout = StringIO()
        command.state = {"makes": {}, "empty": {}}
        command._children = lambda url: [("Privacy", "/about/privacy/")]
        command._harvest_make("About", "/about/", {"since": 1996, "years": 0, "vehicles": 1})

        self.assertEqual(command.state["empty"], {})
        self.assertEqual(command.state["makes"], {})

    def test_the_vehicle_budget_is_spread_across_years_not_spent_in_one(self):
        """A thin year is not evidence the make is thin. Reading two more
        vehicles from BMW's 2009 pages finds the same 19 codes; the budget is
        better spent on a year that has not been looked at."""
        state, opened = self._run({
            2025: manuals.Harvest(codes={"P1111": "Fuel pump relay circuit"}),
            2009: manuals.Harvest(codes={"P2222": "Throttle actuator control"}),
        }, vehicles=4)

        self.assertEqual(sorted(set(opened)), [2009, 2025], f"stayed in one year: {opened}")
        self.assertEqual(set(state["makes"]["M"]["codes"]), {"P1111", "P2222"})


class OneBundlePerManufacturerTests(TestCase):
    """A shop installs *Ford*, not three documents it is then asked to rank.

    A make covered by more than one published document — a summary of the
    badge, and one vehicle's own service manual — keeps both in one file, with
    `precedence` saying which answers first. That is what makes the catalog
    entry a thing somebody can choose.
    """

    def run_it(self, folder, text, *, source, **options):
        (folder / "list.txt").write_text(text, encoding="utf-8")
        call_command(
            "build_dtc_list", str(folder / "list.txt"), make="Testla",
            source=source, out=str(folder / "testla.json"),
            stdout=StringIO(), stderr=StringIO(), **options,
        )
        return json.loads((folder / "testla.json").read_text(encoding="utf-8"))

    def setUp(self):
        self.folder = Path(self.enterContext(__import__("tempfile").TemporaryDirectory()))

    def test_a_second_document_joins_the_first_rather_than_replacing_it(self):
        self.run_it(self.folder, "P1500 From the summary\n", source="A summary")
        written = self.run_it(
            self.folder, "P1501 From the manual\n", source="A service manual",
            precedence=10,
        )

        self.assertEqual(len(written["documents"]), 2)
        self.assertEqual(
            {d["source"] for d in written["documents"]},
            {"A summary", "A service manual"},
        )

    def test_the_documents_are_ordered_by_precedence(self):
        """A vehicle's own manual outranks a third party's summary of the
        make, and nothing in the files says so."""
        self.run_it(self.folder, "P1500 From the summary\n", source="A summary")
        written = self.run_it(
            self.folder, "P1501 From the manual\n", source="A service manual",
            precedence=10,
        )

        self.assertEqual(written["documents"][0]["source"], "A service manual")

    def test_re_running_one_transcription_replaces_that_document(self):
        """Re-running after a fix is the ordinary case. The alternative is a
        bundle that grows a near-duplicate every time somebody corrects it."""
        self.run_it(self.folder, "P1500 Mis-read\n", source="A summary")
        written = self.run_it(self.folder, "P1500 Read properly\n", source="A summary")

        self.assertEqual(len(written["documents"]), 1)
        self.assertEqual(written["documents"][0]["codes"]["P1500"], "Read properly")

    def test_the_version_goes_up_when_the_content_changes(self):
        """What the browse screen compares against: a shop that installed this
        in March needs telling there is a newer one, and cannot be asked to
        compare three thousand rows by eye."""
        first = self.run_it(self.folder, "P1500 One thing\n", source="A summary")
        second = self.run_it(self.folder, "P1500 A correction\n", source="A summary")

        self.assertEqual(first["version"], 1)
        self.assertEqual(second["version"], 2)

    def test_the_version_stays_put_when_nothing_changed(self):
        """Otherwise it counts how often somebody ran the command, which is
        not a question anybody is asking."""
        self.run_it(self.folder, "P1500 One thing\n", source="A summary")
        again = self.run_it(self.folder, "P1500 One thing\n", source="A summary")

        self.assertEqual(again["version"], 1)


class ReadingALibraryOffDiskTests(TestCase):
    """`read_manual_library` — the crawl, without the crawl.

    Given the library's own files every page is a local lookup, so the request
    budget that shaped the crawler is gone. What is left is the part that was
    never about the network: which pages to open, and when a make has been
    covered. Both went wrong here in the way they went wrong in the crawler,
    which is why they are tested.
    """

    TABLE = ("<table><tr><th>Code</th><th>Description</th></tr>"
             "<tr><td>P1500</td><td>Wastegate position sensor</td></tr></table>")

    def _command(self, pages, under=None):
        """A command wired to a library that is a dict.

        `pages` is content key -> HTML. `under` is vehicle key -> the page
        paths that vehicle reaches, defaulting to one page named so that the
        path filter lets it through.
        """
        from homeautoshop.core.management.commands.read_manual_library import Command

        self.reads = []
        under = under or {}

        def pages_for(vehicle):
            return under.get(vehicle.key, {"Quick Lookups/DTC Index": vehicle.key})

        def page(key):
            self.reads.append(key)
            return pages.get(key, "")

        command = Command()
        command.stdout = StringIO()
        command.state = {"makes": {}, "empty": {}}
        command.library = type("FakeLibrary", (), {
            "manual_key": staticmethod(lambda v: v.key),
            "pages_for": staticmethod(pages_for),
            "page": staticmethod(page),
        })()
        return command

    @staticmethod
    def _vehicle(year, key, name=""):
        return type("V", (), {"year": year, "key": key, "name": name or f"{year} car"})()

    def test_a_vehicle_whose_pages_define_nothing_does_not_end_the_search(self):
        """The bug this class exists for, in its second form. Reading six
        documents that happen to define nothing concluded that Jaguar — 1,249
        definitions in one 2016 manual — had none at all."""
        from homeautoshop.core.management.commands.read_manual_library import PATIENCE

        vehicles = [self._vehicle(y, f"k{y}") for y in range(2025, 2013, -1)]
        pages = {f"k{y}": "<p>nothing</p>" for y in range(2025, 2013, -1)}
        pages["k2016"] = self.TABLE

        command = self._command(pages)
        # The shipped patience, because that is the number that has to be big
        # enough to walk past a run of barren years and reach the one that
        # pays. Eleven of these twelve define nothing.
        command._harvest("Testla", vehicles, {"manuals": 150, "patience": PATIENCE})

        self.assertIn("P1500", command.state["makes"]["Testla"]["codes"])

    def test_vehicles_that_add_nothing_new_do_end_it(self):
        """The budget still has to stop somewhere, and the same table over and
        over is what "covered" looks like."""
        vehicles = [self._vehicle(y, f"k{y}") for y in range(2025, 2000, -1)]
        pages = {f"k{y}": self.TABLE for y in range(2025, 2000, -1)}

        command = self._command(pages)
        command._harvest("Testla", vehicles, {"manuals": 150, "patience": 3})

        self.assertEqual(command.state["makes"]["Testla"]["manuals_read"], 4)

    def test_a_make_whose_pages_define_nothing_is_recorded_with_why(self):
        vehicles = [self._vehicle(y, f"k{y}") for y in range(2025, 2020, -1)]
        command = self._command({f"k{y}": "<p>nothing</p>" for y in range(2025, 2020, -1)})
        command._harvest("Testla", vehicles, {"manuals": 150, "patience": 3})

        self.assertIn("Testla", command.state["empty"])
        self.assertIn("none defined anything", command.state["empty"]["Testla"])

    def test_a_page_shared_between_vehicles_is_read_once(self):
        """Pages are keyed by content hash, so sibling vehicles overwhelmingly
        point at the same ones. Parsing a 12,000-page vehicle again for its
        near-identical sibling is the whole cost of this command."""
        vehicles = [self._vehicle(y, f"k{y}") for y in range(2025, 2015, -1)]
        under = {f"k{y}": {"DTC Index": "shared"} for y in range(2025, 2015, -1)}

        command = self._command({"shared": self.TABLE}, under=under)
        command._harvest("Testla", vehicles, {"manuals": 150, "patience": 3})

        self.assertEqual(self.reads, ["shared"])
        self.assertEqual(command.state["makes"]["Testla"]["pages_read"], 1)

    def test_a_page_that_is_not_about_codes_is_not_opened(self):
        """Reading one costs a lookup and returns nothing, but a vehicle
        reaches twelve thousand pages and almost none of them are code
        tables."""
        vehicles = [self._vehicle(2025, "k")]
        under = {"k": {"Wiring Diagrams/Starting": "wiring", "Quick Lookups/DTC Index": "codes"}}

        command = self._command({"codes": self.TABLE, "wiring": "<p>x</p>"}, under=under)
        command._harvest("Testla", vehicles, {"manuals": 150, "patience": 3})

        self.assertEqual(self.reads, ["codes"])


class SpreadingTheManualsTests(TestCase):
    """Which manual to open first, when a make has three thousand of them."""

    @staticmethod
    def _spread(years):
        from homeautoshop.core.management.commands.read_manual_library import _spread

        vehicles = {
            f"key-{y}-{n}": type("V", (), {"year": y, "name": f"{y}"})()
            for y in years for n in range(2)
        }
        return _spread(vehicles)

    def test_every_manual_is_ordered_exactly_once(self):
        order = self._spread(range(2025, 1995, -1))
        keys = [k for k, _v in order]

        self.assertEqual(len(keys), 60)
        self.assertEqual(len(set(keys)), 60)

    def test_the_first_few_cover_the_range_rather_than_the_top_of_it(self):
        """Marching down from the newest is what let six manuals from 2025
        decide a make had no codes."""
        first = [v.year for _k, v in self._spread(range(2025, 1995, -1))[:6]]

        self.assertLess(min(first), 2010, f"the first six stayed recent: {first}")

    def test_a_year_is_not_exhausted_before_another_is_tried(self):
        """Round at a time across the years: two manuals from 2025 tell you
        less than one from 2025 and one from 2004."""
        first = [v.year for _k, v in self._spread(range(2025, 1995, -1))[:4]]

        self.assertEqual(len(set(first)), 4)


class MakesAreNamedAsAShopNamesThemTests(SimpleTestCase):
    """`make_of` — the corpus files vehicles under its own names.

    Two of them are simply older (`Nissan-Datsun`, `General Motors`) and one
    hides a second make inside the first.
    """

    @staticmethod
    def _vehicle(make, year, model):
        return type("V", (), {"make": make, "year": year, "model": model})()

    def _make(self, make, year, model):
        from homeautoshop.core.management.commands.read_manual_library import make_of

        return make_of(self._vehicle(make, year, model))

    def test_a_name_the_make_stopped_using_is_brought_forward(self):
        self.assertEqual(self._make("Nissan-Datsun", 2020, "Altima"), "Nissan")

    def test_the_group_is_named_the_way_a_ticket_names_it(self):
        self.assertEqual(self._make("General Motors", 2015, "Yukon"), "GM")

    def test_a_make_the_corpus_names_plainly_is_left_alone(self):
        self.assertEqual(self._make("Toyota", 2020, "Camry"), "Toyota")

    def test_the_truck_lines_become_their_own_make(self):
        for model in ("1500", "1500 Classic", "2500 HD", "ProMaster City", "C/V"):
            with self.subTest(model):
                self.assertEqual(self._make("Dodge and Ram", 2015, model), "Ram")

    def test_the_cars_stay_with_dodge(self):
        for model in ("Charger", "Challenger", "Grand Caravan", "Durango", "Viper"):
            with self.subTest(model):
                self.assertEqual(self._make("Dodge and Ram", 2015, model), "Dodge")

    def test_a_ram_van_is_a_dodge(self):
        """The case the model name alone gets backwards. Ram was a model line
        of Dodge's before it was a make, so a 1998 `Ram Van` is a Dodge — as
        much as an F-150 is a Ford rather than a make standing beside it."""
        self.assertEqual(self._make("Dodge and Ram", 1998, "Ram Van"), "Dodge")
        self.assertEqual(self._make("Dodge and Ram", 2003, "Ram Wagon"), "Dodge")

    def test_a_truck_from_before_the_split_is_a_dodge(self):
        self.assertEqual(self._make("Dodge and Ram", 2005, "Pickup"), "Dodge")
        self.assertEqual(self._make("Dodge and Ram", 2008, "1500"), "Dodge")


class PublishingAHarvestTests(TestCase):
    """`catalog_dtc_harvest` — the step a person decides to take.

    The one that earns its tests is the merge. A harvest is much larger than
    the compilation it joins and reads like a replacement for it, and is not
    one: it reads the manuals of the vehicles it sampled, so it can be five
    thousand codes deep and still miss ranges the compilation lists.
    """

    HARVEST = {
        "make": "Testla",
        "scope": "make",
        "source": "",
        "build": "lemon abc123",
        "read_from": ["2015 Model Q Base", "2019 Model Q Sport"],
        "codes": {"P1500": "Wastegate position sensor", "P1501": "Boost too low"},
    }
    OLDER = {
        "make": "Testla",
        "aliases": [],
        "version": 3,
        "author": "HomeAutoShop",
        "documents": [
            {
                "source": "SomeCompilation.net — Testla Codes",
                "precedence": 0,
                "codes": {"P1500": "Wastegate sensor", "P1999": "Only here"},
            }
        ],
    }

    def setUp(self):
        self.staged = Path(self.enterContext(__import__("tempfile").TemporaryDirectory()))
        self.out = Path(self.enterContext(__import__("tempfile").TemporaryDirectory()))

    def _stage(self, harvest=None):
        (self.staged / "testla.json").write_text(
            json.dumps(harvest or self.HARVEST), encoding="utf-8"
        )

    def _publish(self, **options):
        call_command(
            "catalog_dtc_harvest", **{
                "from_dir": str(self.staged), "publication": "example.com",
                "out": str(self.out), "stdout": StringIO(), "stderr": StringIO(),
                **options,
            }
        )
        written = self.out / "testla.json"
        return json.loads(written.read_text(encoding="utf-8")) if written.exists() else None

    def _existing(self):
        (self.out / "testla.json").write_text(json.dumps(self.OLDER), encoding="utf-8")

    def test_the_harvest_becomes_a_document_in_the_bundle(self):
        self._stage()
        bundle = self._publish()

        self.assertEqual(bundle["make"], "Testla")
        self.assertEqual(len(bundle["documents"]), 1)
        self.assertEqual(bundle["documents"][0]["codes"]["P1500"], "Wastegate position sensor")

    def test_the_document_says_where_it_came_from(self):
        """The harvest leaves `source` empty on purpose. Publishing is where it
        is filled in, and the citation names the publication, the edition and
        how much of the make was actually read."""
        self._stage()
        source = self._publish()["documents"][0]["source"]

        self.assertIn("example.com", source)
        self.assertIn("lemon abc123", source)
        self.assertIn("2 vehicles", source)

    def test_a_list_already_published_is_kept_underneath(self):
        """The point of the command. The older document holds P1999, which the
        harvest never saw, and substituting would lose it."""
        self._existing()
        self._stage()
        bundle = self._publish()

        self.assertEqual(len(bundle["documents"]), 2)
        answers = {c for d in bundle["documents"] for c in d["codes"]}
        self.assertIn("P1999", answers)

    def test_the_harvest_answers_first(self):
        self._existing()
        self._stage()
        bundle = self._publish()

        self.assertGreater(
            bundle["documents"][0]["precedence"], bundle["documents"][1]["precedence"]
        )
        self.assertIn("example.com", bundle["documents"][0]["source"])

    def test_publishing_bumps_the_version_so_a_shop_is_told(self):
        self._existing()
        self._stage()

        self.assertEqual(self._publish()["version"], 4)

    def test_running_it_twice_replaces_rather_than_repeats(self):
        self._stage()
        self._publish()
        bundle = self._publish()

        self.assertEqual(len(bundle["documents"]), 1)

    def test_a_dry_run_writes_nothing(self):
        self._stage()

        self.assertIsNone(self._publish(dry_run=True))

    def test_a_make_still_under_the_library_name_is_refused(self):
        """`Dodge and Ram` is the library's filing, not a make. Publishing it
        would put a third bundle beside the dodge and ram the catalog has."""
        self._stage({**self.HARVEST, "make": "Dodge and Ram"})
        out = StringIO()
        call_command(
            "catalog_dtc_harvest", from_dir=str(self.staged), publication="example.com",
            out=str(self.out), stdout=out, stderr=StringIO(),
        )

        self.assertIn("Re-harvest", out.getvalue())
        self.assertEqual(list(self.out.glob("*.json")), [])

    def test_what_is_written_is_something_a_shop_can_install(self):
        """Every route in goes through the one validator, this one included."""
        from homeautoshop.diagnostics import codelistlib

        self._stage()
        bundle = self._publish()

        read = codelistlib.parse(json.dumps(bundle))
        self.assertEqual(read["make"], "Testla")

    def test_the_published_name_survives_a_harvest_that_spells_it_differently(self):
        """The library writes `Mercedes Benz` where the catalog has long said
        `Mercedes-Benz`. Both slug to one file, and taking the harvest's would
        rename a make out from under every shop that installed it — and break
        the alias pointing `Mercedes` at it."""
        (self.out / "testla.json").write_text(
            json.dumps({**self.OLDER, "make": "Test-La", "aliases": ["Testla"]}),
            encoding="utf-8",
        )
        self._stage()
        bundle = self._publish()

        self.assertEqual(bundle["make"], "Test-La")
        self.assertEqual(bundle["aliases"], ["Testla"])

    def test_a_make_is_not_refused_as_a_copy_of_itself(self):
        """The copy check skips a make's own documents by name, so the spelling
        had to be settled before it ran: `Mercedes Benz` compared against a
        bundle published as `Mercedes-Benz` read its own last run as a hundred
        per cent copy and refused it."""
        (self.out / "testla.json").write_text(
            json.dumps({**self.OLDER, "make": "Test-La"}), encoding="utf-8"
        )
        self._stage()
        self._publish()
        again = self._publish()

        self.assertEqual(again["make"], "Test-La")
        self.assertTrue(
            any("example.com" in d["source"] for d in again["documents"])
        )

    def test_a_harvest_that_defines_nothing_is_skipped(self):
        self._stage({**self.HARVEST, "codes": {}})
        out = StringIO()
        call_command(
            "catalog_dtc_harvest", from_dir=str(self.staged), publication="example.com",
            out=str(self.out), stdout=out, stderr=StringIO(),
        )

        self.assertIn("defines nothing", out.getvalue())


class LookingForParticularCodesTests(SimpleTestCase):
    """`read_manual_codes` — the cheaper half of a harvest.

    It exists because a refusal rule added after a harvest leaves entries taken
    out and nothing put back, and re-reading a make to answer nine codes costs
    an hour. The two things worth testing are that it finds what is there, and
    that it says so plainly when a code is never defined — because that is the
    answer often enough to matter.
    """

    TABLE = ("<table><tr><th>Code</th><th>Description</th></tr>"
             "<tr><td>P11A2</td><td>Cam shift actuator range</td></tr></table>")
    OTHER = ("<table><tr><th>Code</th><th>Description</th></tr>"
             "<tr><td>P0420</td><td>Catalyst below threshold</td></tr></table>")

    def _command(self, pages, under=None):
        from homeautoshop.core.management.commands.read_manual_codes import Command

        self.parsed = []
        under = under or {}

        def page(key):
            self.parsed.append(key)
            return pages.get(key, "")

        command = Command()
        command.stdout = StringIO()
        command.library = type("FakeLibrary", (), {
            "manual_key": staticmethod(lambda v: v.key),
            "pages_for": staticmethod(
                lambda v: under.get(v.key, {"Quick Lookups/DTC Index": v.key})
            ),
            "page": staticmethod(page),
        })()
        return command

    @staticmethod
    def _vehicle(year, key):
        return type("V", (), {"year": year, "key": key, "name": f"{year} car"})()

    def test_it_finds_the_code_it_was_asked_for(self):
        vehicles = [self._vehicle(y, f"k{y}") for y in range(2025, 2020, -1)]
        pages = {f"k{y}": "<p>nothing</p>" for y in range(2025, 2020, -1)}
        pages["k2022"] = self.TABLE

        command = self._command(pages)
        found = command._look("Testla", vehicles, {"P11A2"}, 60)

        self.assertEqual(found, {"P11A2": "Cam shift actuator range"})

    def test_a_code_that_is_never_defined_comes_back_missing(self):
        """`P1000` and `B2000` are the first number of a manufacturer block and
        a chart lists them to label it. Finding nothing is the answer."""
        vehicles = [self._vehicle(y, f"k{y}") for y in range(2025, 2020, -1)]

        command = self._command({f"k{y}": self.OTHER for y in range(2025, 2020, -1)})
        found = command._look("Testla", vehicles, {"P1000"}, 60)

        self.assertEqual(found, {})

    def test_a_page_without_any_wanted_code_is_never_parsed(self):
        """The whole reason this is quick. A vehicle reaches twelve thousand
        pages; parsing the tables on all of them is what a harvest does."""
        vehicles = [self._vehicle(2025, "k")]
        under = {"k": {"DTC Index A": "hit", "DTC Index B": "miss"}}

        command = self._command({"hit": self.TABLE, "miss": self.OTHER}, under=under)
        with mock.patch.object(manuals, "read", wraps=manuals.read) as read:
            command._look("Testla", vehicles, {"P11A2"}, 60)

        self.assertEqual([c.args[0] for c in read.call_args_list], [self.TABLE])

    def test_it_stops_once_every_code_is_answered(self):
        vehicles = [self._vehicle(y, f"k{y}") for y in range(2025, 2015, -1)]
        pages = {f"k{y}": self.TABLE for y in range(2025, 2015, -1)}

        command = self._command(pages)
        command._look("Testla", vehicles, {"P11A2"}, 60)

        self.assertEqual(len(self.parsed), 1)

    def test_a_page_shared_between_vehicles_is_read_once(self):
        vehicles = [self._vehicle(y, f"k{y}") for y in range(2025, 2020, -1)]
        under = {f"k{y}": {"DTC Index": "shared"} for y in range(2025, 2020, -1)}

        command = self._command({"shared": self.OTHER}, under=under)
        command._look("Testla", vehicles, {"P1000"}, 60)

        self.assertEqual(self.parsed, ["shared"])

    def test_codes_can_be_named_on_the_command_line_or_in_a_file(self):
        from homeautoshop.core.management.commands.read_manual_codes import Command

        spelled = Command._targets(
            {"targets": "", "make": ["Audi"], "code": ["p11a2"]}
        )
        self.assertEqual(spelled, {"Audi": ["P11A2"]})

        folder = Path(self.enterContext(__import__("tempfile").TemporaryDirectory()))
        (folder / "gaps.json").write_text(
            json.dumps({"Audi": ["p11a2"], "Ford": []}), encoding="utf-8"
        )
        from_file = Command._targets(
            {"targets": str(folder / "gaps.json"), "make": [], "code": []}
        )

        self.assertEqual(from_file, {"Audi": ["P11A2"]})


class OverlapsAlreadyExaminedTests(SimpleTestCase):
    """`codelists/_rejected.json` — the findings that outlive the session.

    The copy check reports twenty-six word-for-word overlaps on a catalog built
    from factory manuals, and every one is a corporate family sharing a chart
    or a list small enough that a few standard codes dominate the fraction.
    The register is what stops the twenty-seventh — the one that matters —
    arriving as the twenty-seventh line of the same shape.
    """

    def _cleared(self, one, other):
        from homeautoshop.core.management.commands import catalog_dtc_harvest

        catalog_dtc_harvest._CLEARED = None  # read the shipped file, not a cache
        return catalog_dtc_harvest.cleared(one, other)

    def test_a_corporate_family_is_recorded(self):
        """Chrysler wrote the chart and five badges reproduce it."""
        self.assertIsNotNone(self._cleared("Chrysler", "Eagle"))
        self.assertIsNotNone(self._cleared("Dodge", "Plymouth"))

    def test_it_does_not_matter_which_way_round_the_pair_is_given(self):
        self.assertEqual(
            self._cleared("Toyota", "Lexus"), self._cleared("Lexus", "Toyota")
        )

    def test_two_makes_with_no_reason_to_agree_are_not_recorded(self):
        """The check has to keep working. Ford repeating Toyota word for word
        would be the finding this register exists to leave visible."""
        self.assertIsNone(self._cleared("Ford", "Toyota"))

    def test_the_share_recorded_is_the_one_that_was_seen(self):
        """`upto` is what makes it a record rather than a mute button: an
        overlap that grows past what was examined is reported again."""
        self.assertAlmostEqual(self._cleared("Chrysler", "Eagle"), 0.79)

    def test_every_recorded_overlap_says_why(self):
        import json

        from homeautoshop.diagnostics import codelists

        register = json.loads(
            (Path(codelists.__file__).parent / "_rejected.json").read_text(
                encoding="utf-8"
            )
        )
        for entry in register["overlaps"]:
            with self.subTest(entry["makes"]):
                self.assertGreater(len(entry["why"]), 40)
                self.assertGreaterEqual(len(entry["makes"]), 2)
                self.assertTrue(0 < entry["upto"] < 1)
