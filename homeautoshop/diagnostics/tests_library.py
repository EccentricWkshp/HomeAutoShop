"""
Reading a manual library off disk instead of crawling the site that serves it.

The real corpus is 31 GB and 279,988 vehicles, which is not a fixture. So these
build a small library with the MTBL writer in `tests_mtbl`, in the shape the
real one has: an `index.json` of vehicles, a routing table per vehicle, and the
pages those tables point at.

What is worth testing here is the *shape*, because that is what a later dump
can change under us — the edition read while this was written already names a
2005 Isuzu `Ascender LS, 4.2 S, 4WD` where the live site calls the same vehicle
`Ascender 4WD L6-4.2L`.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from django.test import SimpleTestCase

from . import library
from .tests_mtbl import write

PAGE = """
<table>
 <tr><th>Code</th><th>Description</th></tr>
 <tr><td>P1500</td><td>Wastegate position sensor performance</td></tr>
</table>
"""


def a_library(folder: Path, vehicles=None, *, page=PAGE) -> Path:
    """A library on disk, in the layout the real one uses."""
    vehicles = vehicles or [
        {
            "make": "Testla", "years": ["2016"], "model": "Roadster",
            "engine": "2D Convertible", "uriPath": "/Testla/2016/Roadster/",
            "rootUriTable": "uri_table_root_aaaa",
        },
        {
            "make": "Testla", "years": ["2004"], "model": "Coupe",
            "engine": "L4-1.6L", "uriPath": "/Testla/2004/Coupe/",
            "rootUriTable": "uri_table_root_bbbb",
        },
    ]
    (folder / "index.json").write_text(
        json.dumps({"database": "testbed", "vehicles": vehicles}), encoding="utf-8"
    )
    # Sorted by key, as an MTBL is.
    pairs = sorted([
        (b"html_act_conglomerate_tree_one", page.encode()),
        (b"html_act_conglomerate_tree_two", b"<p>no codes here</p>"),
        (b"uri_table_root_aaaa", json.dumps({
            "final": {
                "": "html_root_x",
                "Repair%20and%20Diagnosis%20%28Single%20Page%29/":
                    "html_act_conglomerate_tree_one",
            },
            "prefix": {"Repair%20and%20Diagnosis/": "uri_table_act_x"},
        }).encode()),
        (b"uri_table_root_bbbb", json.dumps({
            "final": {
                "Repair%20and%20Diagnosis%20%28Single%20Page%29/":
                    "html_act_conglomerate_tree_two",
            },
        }).encode()),
    ])
    write(folder / "pages.mtbl", pairs)
    return folder


class Base(SimpleTestCase):
    def library(self, **options):
        folder = Path(self.enterContext(tempfile.TemporaryDirectory())) / "lemon"
        folder.mkdir(parents=True)
        a_library(folder, **options)
        opened = library.Library(folder)
        self.addCleanup(opened.close)
        return opened


class ReadingTheVehiclesTests(Base):
    def test_they_come_back_with_what_a_citation_needs(self):
        newest = self.library().vehicles()[0]

        self.assertEqual(newest.make, "Testla")
        self.assertEqual(newest.year, 2016)
        self.assertEqual(newest.name, "2016 Roadster 2D Convertible")

    def test_a_record_covering_several_years_reports_the_newest(self):
        lib = self.library(vehicles=[{
            "make": "Testla", "years": ["2004", "2005", "2006"], "model": "Coupe",
            "engine": "L4", "uriPath": "/x/", "rootUriTable": "uri_table_root_bbbb",
        }])
        self.assertEqual(lib.vehicles()[0].year, 2006)

    def test_a_record_with_no_routing_table_is_not_a_vehicle(self):
        lib = self.library(vehicles=[
            {"make": "Testla", "years": ["2016"], "model": "A", "engine": "",
             "uriPath": "/a/", "rootUriTable": "uri_table_root_aaaa"},
            {"make": "Testla", "years": ["2016"], "model": "B", "engine": ""},
        ])
        self.assertEqual(len(lib.vehicles()), 1)

    def test_the_path_is_readable_rather_than_escaped(self):
        lib = self.library(vehicles=[{
            "make": "Testla", "years": ["2016"], "model": "A", "engine": "",
            "uriPath": "/Testla/2016/Roadster%20Base%2C%20RWD/",
            "rootUriTable": "uri_table_root_aaaa",
        }])
        self.assertEqual(lib.vehicles()[0].path, "/Testla/2016/Roadster Base, RWD/")


class FindingTheManualTests(Base):
    def test_the_single_page_key_is_what_comes_back(self):
        """One key holding the whole manual is what makes this cheap. The
        alternative is descending a routing table a level at a time."""
        lib = self.library()
        newest = lib.vehicles()[0]

        self.assertEqual(lib.manual_key(newest), "html_act_conglomerate_tree_one")

    def test_the_key_rather_than_the_document(self):
        """So a caller can notice it has already read this manual for another
        vehicle. On the real corpus that is two reads saved in every three."""
        lib = self.library()
        keys = {lib.manual_key(v) for v in lib.vehicles()}

        self.assertEqual(len(keys), 2)
        self.assertTrue(all(isinstance(k, str) for k in keys))

    def test_a_vehicle_whose_table_is_missing_says_so_rather_than_failing(self):
        lib = self.library(vehicles=[{
            "make": "Testla", "years": ["2016"], "model": "A", "engine": "",
            "uriPath": "/a/", "rootUriTable": "uri_table_root_nowhere",
        }])
        self.assertEqual(lib.manual_key(lib.vehicles()[0]), "")
        self.assertEqual(lib.routing(lib.vehicles()[0]), {})

    def test_the_page_comes_back_as_text(self):
        lib = self.library()
        page = lib.page(lib.manual_key(lib.vehicles()[0]))

        self.assertIn("Wastegate", page)

    def test_a_key_that_is_not_there(self):
        self.assertEqual(self.library().page("html_act_conglomerate_tree_nope"), "")


class WhichEditionThisIsTests(Base):
    def test_the_build_is_recorded(self):
        """A later dump is a different document, not a newer version of this
        one, and anything transcribed from it should be able to say which."""
        self.assertIn("testbed", self.library().build)


class WhatItRefusesTests(SimpleTestCase):
    def test_a_folder_with_no_index(self):
        folder = Path(self.enterContext(tempfile.TemporaryDirectory()))
        with self.assertRaises(library.NotALibrary):
            library.Library(folder)

    def test_a_folder_with_an_index_and_no_pages(self):
        folder = Path(self.enterContext(tempfile.TemporaryDirectory()))
        (folder / "index.json").write_text('{"vehicles": []}', encoding="utf-8")
        with self.assertRaises(library.NotALibrary) as caught:
            library.Library(folder)
        self.assertIn("pages.mtbl", str(caught.exception))
