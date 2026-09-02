"""Sharing templates, by file and by catalog (SPEC §17 R-1, OQ-2).

R-1 was deferred because a repository "needs a trust model and a network
dependency, both in tension with P-1", and because "import/export already
covers the ninety-percent case". The second half was not true: parser profiles
had import/export and **schedule templates had none at all**, which is the
artifact a community repository is mostly about. So the format came first, and
the catalog is a way of delivering files that format already accepts.

The tests that matter here are the trust ones. A catalog is a postman:

* `test_a_catalog_file_gets_no_privilege` — a downloaded file goes through
  the identical validator an uploaded one does. There is no second path.
* `test_an_entry_cannot_redirect_the_fetch` — an index entry names a path
  inside the catalog, never a URL. Without this the repository would choose
  which host this instance talks to and the allowlist would be checking
  somebody else's decision.
* `test_installing_does_not_schedule_anything` — a template lands in the list;
  putting it on a vehicle stays a deliberate act by somebody who looked at it.
"""

from __future__ import annotations

import io
import json
import pathlib
import socket
import tempfile
from unittest import mock

from django.conf import settings
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings
from django.urls import reverse

from homeautoshop.accounts.models import Role, User
from homeautoshop.assets.models import Asset
from homeautoshop.core import catalog as catalog_lib
from homeautoshop.core.outbound import OutboundBlocked, Response
from homeautoshop.diagnostics.models import ParserProfile
from homeautoshop.inspections import templatelib as checklistlib
from homeautoshop.inspections.models import InspectionTemplate
from homeautoshop.maintenance import templatelib
from homeautoshop.maintenance.models import (
    AssetServiceItem,
    ScheduleTemplate,
    ServiceDefinition,
)

GOOD = """
name: Generic diesel, severe service
slug: generic-diesel-severe
description: For a truck that tows.
asset_kinds: [vehicle]
items:
  - name: Engine oil and filter
    translation_key: engine_oil
    category: engine
    interval_distance: 5000
    interval_unit: mi
    interval_months: 6
  - name: Fuel filter
    interval_distance: 15000
    interval_unit: mi
"""

BASE = "https://raw.githubusercontent.com/example/catalog/main/"

INDEX = {
    "entries": [
        {
            "kind": "schedule",
            "slug": "generic-diesel-severe",
            "name": "Generic diesel, severe service",
            "path": "schedules/diesel-severe.json",
            "author": "somebody",
            "description": "For a truck that tows.",
        },
        {"kind": "nonsense", "name": "?", "path": "x"},
    ]
}


class Base(TestCase):
    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user(
            username="andy", password="x" * 16, role=Role.ADMIN
        )
        self.client.force_login(self.user)


class TemplateFileTests(Base):
    """The format that had to exist before a catalog could mean anything."""

    def test_a_template_is_read_and_written(self):
        template = templatelib.load(GOOD)

        self.assertEqual(template.name, "Generic diesel, severe service")
        self.assertEqual(template.items.count(), 2)
        self.assertEqual(template.source, ScheduleTemplate.Source.IMPORTED)

    def test_definitions_travel_with_it(self):
        """A file assuming the receiving shop already had "Engine oil and
        filter" under that exact name would import as an empty schedule on
        half the instances that tried it."""
        templatelib.load(GOOD)

        self.assertTrue(ServiceDefinition.objects.filter(name="Fuel filter").exists())

    def test_an_existing_definition_is_reused_rather_than_duplicated(self):
        existing = ServiceDefinition.objects.create(
            name="Engine oil and filter", translation_key="engine_oil"
        )

        template = templatelib.load(GOOD)

        self.assertIn(existing, [i.definition for i in template.items.all()])

    def test_it_matches_on_the_translation_key_before_the_name(self):
        """A shop running in French has its own name for the same shipped
        item, and matching on name alone gives it an English duplicate every
        time it imports anything."""
        existing = ServiceDefinition.objects.create(
            name="Vidange d'huile moteur", translation_key="engine_oil"
        )

        template = templatelib.load(GOOD)

        self.assertIn(existing, [i.definition for i in template.items.all()])

    def test_a_round_trip_survives(self):
        first = templatelib.load(GOOD)
        text = templatelib.to_yaml(first)
        first.name = "Renamed so the second can import"
        first.save()

        second = templatelib.load(text.replace("Generic diesel", "Second copy"))

        self.assertEqual(second.items.count(), 2)

    def test_an_unknown_field_is_refused_rather_than_ignored(self):
        """Either a newer format this instance cannot honor or a typo, and
        both mean the schedule that gets imported is not the one written."""
        with self.assertRaises(templatelib.TemplateInvalid):
            templatelib.load(GOOD + "\nsurprise: true\n")

    def test_an_unknown_unit_is_refused(self):
        """`5000 furlongs` silently read as miles is a schedule wrong by a
        factor nobody would spot."""
        with self.assertRaises(templatelib.TemplateInvalid) as caught:
            templatelib.load(GOOD.replace("interval_unit: mi", "interval_unit: furlongs"))

        self.assertIn("furlongs", str(caught.exception))

    def test_an_item_with_no_interval_is_refused(self):
        bad = """
name: Broken
items:
  - name: Something
"""
        with self.assertRaises(templatelib.TemplateInvalid):
            templatelib.load(bad)

    def test_a_duplicate_name_is_refused_rather_than_overwriting(self):
        """A template gets applied to vehicles. Quietly replacing one with a
        stranger's file would change what the shop believes is due without
        anybody deciding to."""
        templatelib.load(GOOD)

        with self.assertRaises(templatelib.TemplateInvalid):
            templatelib.load(GOOD)

    def test_a_huge_file_is_refused_before_it_is_parsed(self):
        with self.assertRaises(templatelib.TemplateInvalid):
            templatelib.load("name: x\n" + ("# padding\n" * 200000))

    def test_it_round_trips_through_the_screen(self):
        template = templatelib.load(GOOD)

        body = self.client.get(
            reverse("template_export", args=[template.pk])
        ).content.decode()

        self.assertIn("Generic diesel", body)
        self.assertIn("Fuel filter", body)


@override_settings(CATALOG_URL=BASE)
class CatalogTests(Base):
    def index(self, payload=None):
        return mock.patch(
            "homeautoshop.core.catalog.fetch_json",
            return_value=Response(200, payload or INDEX, 5),
        )

    def test_the_published_list_is_read(self):
        with self.index():
            published = catalog_lib.index()

        self.assertEqual(len(published.schedules), 1)
        self.assertEqual(published.schedules[0].name, "Generic diesel, severe service")

    def test_an_entry_this_version_cannot_read_is_counted_not_hidden(self):
        """Silently showing a short list would look like the catalog was
        smaller than it is."""
        with self.index():
            self.assertEqual(catalog_lib.index().skipped, 1)

    def test_what_is_already_here_is_marked(self):
        templatelib.load(GOOD)

        with self.index():
            self.assertTrue(catalog_lib.index().schedules[0].installed)

    def test_a_catalog_file_gets_no_privilege(self):
        """The whole trust model. A downloaded file runs through the identical
        validator an uploaded one does, so the catalog cannot be trusted
        into doing anything a stranger's email could not."""
        entry = catalog_lib.Entry(
            kind="schedule", slug="x", name="x", path="schedules/bad.json"
        )
        with mock.patch(
            "homeautoshop.core.catalog.fetch_text",
            return_value="name: x\nsurprise: true\nitems: []",
        ):
            with self.assertRaises(templatelib.TemplateInvalid):
                catalog_lib.install(entry)

    def test_an_entry_cannot_redirect_the_fetch(self):
        """An index entry names a file inside the catalog. If it could name
        a URL, the repository would choose which host this instance talks to
        and the allowlist would be rubber-stamping somebody else's decision."""
        for path in (
            "https://evil.example/x.json",
            "//evil.example/x.json",
            "../../etc/passwd",
            "/absolute.json",
        ):
            with self.subTest(path=path):
                with self.assertRaises(catalog_lib.CatalogUnavailable):
                    catalog_lib.resolve(path)

    def test_an_ordinary_path_resolves_under_the_catalog(self):
        self.assertEqual(
            catalog_lib.resolve("schedules/diesel.json"),
            BASE + "schedules/diesel.json",
        )

    def test_offline_mode_refuses_it_like_everything_else(self):
        with mock.patch(
            "homeautoshop.core.catalog.fetch_json",
            side_effect=OutboundBlocked("Offline Mode is on"),
        ):
            with self.assertRaises(catalog_lib.CatalogUnavailable):
                catalog_lib.index()

    def test_an_unreachable_catalog_is_a_sentence_not_an_error_page(self):
        """It is additive by design, so failing to reach it must not break a
        working screen."""
        with mock.patch(
            "homeautoshop.core.catalog.fetch_json",
            side_effect=OutboundBlocked("Offline Mode is on"),
        ):
            page = self.client.get(reverse("catalog_browse"))

        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "Offline Mode")

    def test_nothing_is_fetched_without_being_asked(self):
        """No background check, no poll, nothing on start-up. The dashboard
        is the page everybody lands on, and it must not touch the catalog."""
        with mock.patch("homeautoshop.core.catalog.fetch_json") as called:
            self.client.get(reverse("dashboard"))
            self.client.get(reverse("template_list"))

        called.assert_not_called()


@override_settings(CATALOG_URL=BASE)
class InstallingTests(Base):
    def install(self):
        with mock.patch(
            "homeautoshop.core.catalog.fetch_json",
            return_value=Response(200, INDEX, 5),
        ), mock.patch("homeautoshop.core.catalog.fetch_text", return_value=GOOD):
            return self.client.post(
                reverse("catalog_install"),
                {"kind": "schedule", "path": "schedules/diesel-severe.json"},
                follow=True,
            )

    def test_an_entry_is_installed(self):
        self.install()

        self.assertTrue(
            ScheduleTemplate.objects.filter(name="Generic diesel, severe service").exists()
        )

    def test_installing_does_not_schedule_anything(self):
        """A schedule that silently attached itself to a truck would be a
        stranger deciding when its brakes get checked."""
        Asset.objects.create(nickname="Aero")

        self.install()

        self.assertEqual(AssetServiceItem.objects.count(), 0)

    def test_and_the_message_says_so(self):
        self.assertContains(self.install(), "until you apply it")

    def test_a_path_not_in_the_index_is_refused(self):
        """Otherwise the form field is an operator-supplied URL fragment and
        this is a fetch-anything button wearing an install button's clothes."""
        with mock.patch(
            "homeautoshop.core.catalog.fetch_json",
            return_value=Response(200, INDEX, 5),
        ):
            response = self.client.post(
                reverse("catalog_install"),
                {"kind": "schedule", "path": "schedules/something-else.json"},
            )

        self.assertEqual(response.status_code, 404)
        self.assertFalse(ScheduleTemplate.objects.filter(source="imported").exists())

    def test_it_takes_a_post(self):
        self.assertEqual(
            self.client.get(reverse("catalog_install")).status_code, 405
        )

    def test_only_an_administrator_may_install(self):
        member = User.objects.create_user(
            username="pat", password="x" * 16, role=Role.MEMBER
        )
        self.client.force_login(member)

        response = self.client.post(
            reverse("catalog_install"),
            {"kind": "schedule", "path": "schedules/diesel-severe.json"},
        )

        self.assertEqual(response.status_code, 403)


class NotConfiguredTests(Base):
    @override_settings(CATALOG_URL="")
    def test_an_instance_that_never_looks_is_not_degraded(self):
        page = self.client.get(reverse("catalog_browse"))

        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "No catalog address is set")

    @override_settings(CATALOG_URL="")
    def test_and_the_templates_screen_does_not_offer_it(self):
        page = self.client.get(reverse("template_list"))
        self.assertNotContains(page, reverse("catalog_browse"))


CHECKLIST = """
name: Pre-purchase inspection
slug: ppi
translation_key: template.ppi
description: Walk a vehicle you are considering buying.
vehicle_classes: [car, truck]
version: 1
points:
  - area: tires_wheels
    name: Tire tread depth
    translation_key: point.tire_tread
    result_type: measurement
    measurement_unit: /32in
    positions: [LF, RF, LR, RR]
    sub_positions: [outer, center, inner]
    thresholds:
      fail: {lte: 2}
      attention: {lte: 4}
      pass: {gt: 4}
    photo_required: on_attention
    is_safety_critical: true
  - area: under_vehicle
    name: Frame and rocker corrosion
    result_type: status
    photo_required: on_attention
    is_safety_critical: true
"""


class ChecklistFileTests(Base):
    """Inspection templates as YAML (FR-DVI-13).

    Promised by the requirement, documented in full by
    `SCHEMA-INSPECTION-TEMPLATES.md`, and never implemented — the third
    capability described as existing that was not, after schedule template
    import/export and the per-vehicle authorization scaffold.

    The worked example in §1 of that document is what these tests import, so
    the file somebody writes from reading the contract is the file that works.
    """

    def test_the_documented_example_imports(self):
        template = checklistlib.load(CHECKLIST)

        self.assertEqual(template.name, "Pre-purchase inspection")
        self.assertEqual(template.points.count(), 2)

    def test_positions_and_thresholds_survive(self):
        """A tire point is four wheels by three positions, and the thresholds
        are what turn a measurement into an opinion."""
        template = checklistlib.load(CHECKLIST)
        tread = template.points.get(name="Tire tread depth")

        self.assertEqual(tread.positions, ["LF", "RF", "LR", "RR"])
        self.assertEqual(tread.sub_positions, ["outer", "center", "inner"])
        self.assertEqual(tread.thresholds["fail"], {"lte": 2})
        self.assertTrue(tread.is_safety_critical)

    def test_a_round_trip_survives(self):
        first = checklistlib.load(CHECKLIST)
        text = checklistlib.to_yaml(first)
        first.name = "Renamed"
        first.save()

        second = checklistlib.load(text.replace("Pre-purchase", "Second copy"))

        self.assertEqual(second.points.count(), 2)
        self.assertEqual(
            second.points.get(name="Tire tread depth").thresholds["attention"],
            {"lte": 4},
        )

    def test_an_unknown_comparison_is_refused(self):
        """A threshold decides the machine's opinion of a measurement. One
        that parses and means something else is a confident wrong answer about
        a brake pad, found on a driveway rather than here."""
        with self.assertRaises(checklistlib.TemplateInvalid) as caught:
            checklistlib.load(CHECKLIST.replace("lte: 2", "roughly: 2"))

        self.assertIn("roughly", str(caught.exception))

    def test_a_bound_that_is_not_a_number_is_refused(self):
        with self.assertRaises(checklistlib.TemplateInvalid):
            checklistlib.load(CHECKLIST.replace("lte: 2", "lte: thin"))

    def test_a_backwards_between_is_refused(self):
        with self.assertRaises(checklistlib.TemplateInvalid):
            checklistlib.load(CHECKLIST.replace("{lte: 2}", "{between: [9, 1]}"))

    def test_a_rule_cannot_grade_to_something_only_a_person_decides(self):
        """`not_applicable` is an answer somebody gives about a car with no
        rear brakes to measure. No rule reaches that conclusion."""
        with self.assertRaises(checklistlib.TemplateInvalid):
            checklistlib.load(
                CHECKLIST.replace("fail: {lte: 2}", "not_applicable: {lte: 2}")
            )

    def test_thresholds_on_a_status_point_are_refused(self):
        """A status point records no number, so the rule could never fire —
        and a rule that can never fire looks like one that passed."""
        bad = CHECKLIST.replace(
            "    result_type: status\n    photo_required: on_attention",
            "    result_type: status\n    thresholds:\n      fail: {lte: 1}\n"
            "    photo_required: on_attention",
        )
        with self.assertRaises(checklistlib.TemplateInvalid):
            checklistlib.load(bad)

    def test_an_unknown_field_is_refused(self):
        with self.assertRaises(checklistlib.TemplateInvalid):
            checklistlib.load(CHECKLIST + "\nsurprise: true\n")

    def test_a_retired_area_is_kept_and_reported(self):
        """`fluids` was retired and old inspections still render under it, so
        a template written against the older vocabulary is readable — but
        nobody should have to wonder why it groups oddly."""
        data = checklistlib.parse(
            CHECKLIST.replace("area: under_vehicle", "area: fluids")
        )

        self.assertEqual(checklistlib.unknown_areas(data), ["fluids"])

    def test_it_exports_from_the_screen(self):
        template = checklistlib.load(CHECKLIST)

        body = self.client.get(
            reverse("checklist_export", args=[template.pk])
        ).content.decode()

        self.assertIn("Tire tread depth", body)

    def test_and_imports_through_it(self):
        self.client.post(reverse("checklist_import"), {"yaml": CHECKLIST}, follow=True)

        self.assertTrue(
            InspectionTemplate.objects.filter(name="Pre-purchase inspection").exists()
        )

    def test_a_refusal_names_the_point(self):
        response = self.client.post(
            reverse("checklist_import"),
            {"yaml": CHECKLIST.replace("lte: 2", "roughly: 2")},
            follow=True,
        )

        self.assertContains(response, "Point 1")


@override_settings(CATALOG_URL=BASE)
class ChecklistsInTheCatalogTests(Base):
    INDEX = {
        "entries": [
            {
                "kind": "checklist",
                "slug": "ppi",
                "name": "Pre-purchase inspection",
                "path": "checklists/ppi.json",
            }
        ]
    }

    def test_a_published_checklist_installs(self):
        with mock.patch(
            "homeautoshop.core.catalog.fetch_json",
            return_value=Response(200, self.INDEX, 5),
        ), mock.patch("homeautoshop.core.catalog.fetch_text", return_value=CHECKLIST):
            self.client.post(
                reverse("catalog_install"),
                {"kind": "checklist", "path": "checklists/ppi.json"},
                follow=True,
            )

        self.assertTrue(
            InspectionTemplate.objects.filter(name="Pre-purchase inspection").exists()
        )

    def test_it_goes_through_the_same_validator(self):
        """No privileged path for any kind, checklists included."""
        entry = catalog_lib.Entry(
            kind="checklist", slug="x", name="x", path="checklists/bad.json"
        )
        bad = CHECKLIST.replace("lte: 2", "roughly: 2")
        with mock.patch("homeautoshop.core.catalog.fetch_text", return_value=bad):
            with self.assertRaises(checklistlib.TemplateInvalid):
                catalog_lib.install(entry)

    def test_one_already_here_is_marked(self):
        checklistlib.load(CHECKLIST)

        with mock.patch(
            "homeautoshop.core.catalog.fetch_json",
            return_value=Response(200, self.INDEX, 5),
        ):
            self.assertTrue(catalog_lib.index().checklists[0].installed)


class BuildingTheIndexTests(TestCase):
    """`manage.py build_catalog` (SPEC §8.1b).

    Contributing used to mean adding a file *and* hand-editing an index, with
    a `{"body": "…"}` wrapper around the YAML so the only guarded fetcher this
    codebase had could read it. Both were implementation details facing the
    wrong way: forgetting the index row published nothing and said nothing,
    and the wrapper asked a contributor to JSON-escape a file by hand.

    So the file is the file, and the index is generated from it — validated on
    the way, which moves the first failure from somebody's garage to the pull
    request.
    """

    def build(self, root, **options):
        out = io.StringIO()
        call_command("build_catalog", root=str(root), stdout=out, stderr=out, **options)
        return out.getvalue()

    def catalog(self, **files):
        root = pathlib.Path(tempfile.mkdtemp())
        for name, body in files.items():
            folder = root / name.split("/")[0]
            folder.mkdir(parents=True, exist_ok=True)
            (root / name).write_text(body, encoding="utf-8")
        for folder in ("schedules", "checklists", "profiles"):
            (root / folder).mkdir(parents=True, exist_ok=True)
        return root

    def test_it_writes_an_entry_per_file(self):
        root = self.catalog(**{"schedules/diesel.yaml": GOOD})

        self.build(root)

        index = json.loads((root / "index.json").read_text(encoding="utf-8"))
        self.assertEqual(len(index["entries"]), 1)
        self.assertEqual(index["entries"][0]["kind"], "schedule")
        self.assertEqual(index["entries"][0]["path"], "schedules/diesel.yaml")

    def test_the_name_comes_out_of_the_file(self):
        """So the index cannot disagree with what it points at."""
        root = self.catalog(**{"schedules/diesel.yaml": GOOD})

        self.build(root)

        entry = json.loads((root / "index.json").read_text(encoding="utf-8"))["entries"][0]
        self.assertEqual(entry["name"], "Generic diesel, severe service")
        self.assertIn("truck that tows", entry["description"])

    def test_both_kinds_are_walked(self):
        root = self.catalog(
            **{"schedules/diesel.yaml": GOOD, "checklists/ppi.yaml": CHECKLIST}
        )

        self.build(root)

        kinds = {
            e["kind"]
            for e in json.loads((root / "index.json").read_text(encoding="utf-8"))["entries"]
        }
        self.assertEqual(kinds, {"schedule", "checklist"})

    def test_a_file_that_would_not_import_fails_the_build(self):
        """The check that matters. A template nobody can install must not be
        publishable, and the person who wrote it should hear about it while
        they are still looking at it."""
        root = self.catalog(**{"schedules/broken.yaml": "name: x\nsurprise: true\n"})

        with self.assertRaises(CommandError):
            self.build(root)

        self.assertFalse((root / "index.json").exists())

    def test_every_bad_file_is_named_not_just_the_first(self):
        """Somebody fixing three files wants three errors, not three runs."""
        root = self.catalog(**{
            "schedules/a.yaml": "name: a\nsurprise: true\n",
            "schedules/b.yaml": "name: b\nitems: []\n",
        })

        with self.assertRaises(CommandError) as caught:
            self.build(root)

        self.assertIn("2 file(s)", str(caught.exception))

    def test_check_passes_on_a_current_index(self):
        root = self.catalog(**{"schedules/diesel.yaml": GOOD})
        self.build(root)

        self.assertIn("current", self.build(root, check=True))

    def test_check_fails_on_a_stale_one(self):
        """What CI runs, so a forgotten rebuild is a red build rather than a
        catalog that disagrees with its own files."""
        root = self.catalog(**{"schedules/diesel.yaml": GOOD})
        self.build(root)
        (root / "checklists" / "ppi.yaml").write_text(CHECKLIST, encoding="utf-8")

        with self.assertRaises(CommandError):
            self.build(root, check=True)

    def test_the_repository_catalog_is_current_and_valid(self):
        """The real one, checked on every run — it is the file an instance
        fetches, and nothing else in the suite would notice it rotting."""
        self.assertIn(
            "current",
            self.build(pathlib.Path(settings.BASE_DIR) / "catalog", check=True),
        )

    def test_the_published_files_are_plain_yaml(self):
        """Not a JSON envelope. A contributor commits what they exported."""
        published = list(
            (pathlib.Path(settings.BASE_DIR) / "catalog" / "schedules").glob("*")
        )

        self.assertTrue(published)
        for path in published:
            with self.subTest(file=path.name):
                self.assertEqual(path.suffix, ".yaml")


class ReadingAnUploadTests(TestCase):
    """The one reader all three import screens use (`core/imports.py`).

    Parser profiles established the shape and the two new kinds copied it.
    Three copies is where that stops being a coincidence — a fourth kind would
    have been a fourth, each free to drift in what it accepted.
    """

    def setUp(self):
        self.user = User.objects.create_user(
            username="andy", password="x" * 16, role=Role.ADMIN
        )
        self.client.force_login(self.user)

    def test_a_pasted_template_imports(self):
        self.client.post(reverse("template_import"), {"yaml": GOOD}, follow=True)

        self.assertTrue(ScheduleTemplate.objects.filter(source="imported").exists())

    def test_an_uploaded_one_does_too(self):
        upload = SimpleUploadedFile("s.yaml", GOOD.encode(), content_type="text/yaml")

        self.client.post(reverse("template_import"), {"template": upload}, follow=True)

        self.assertTrue(ScheduleTemplate.objects.filter(source="imported").exists())

    def test_the_file_wins_when_both_are_given(self):
        """Somebody who picked a file and left a half-typed paste behind meant
        the file, and importing the paste would be the less expected of the
        two outcomes."""
        upload = SimpleUploadedFile("s.yaml", GOOD.encode(), content_type="text/yaml")

        self.client.post(
            reverse("template_import"),
            {"template": upload, "yaml": "name: Pasted\nitems: []\n"},
            follow=True,
        )

        self.assertEqual(
            ScheduleTemplate.objects.get().name, "Generic diesel, severe service"
        )

    def test_neither_says_so(self):
        response = self.client.post(reverse("template_import"), {}, follow=True)
        self.assertContains(response, "Choose a file or paste one in")

    def test_the_profile_screen_reads_the_same_way(self):
        """It set the pattern; it now shares the implementation."""
        response = self.client.post(reverse("profile_import"), {}, follow=True)
        self.assertContains(response, "Choose a file or paste one in")


class TheNetworkIsClosedTests(TestCase):
    """The suite may not reach the internet (`core/testrunner.py`).

    Found the hard way: a test overrode `CATALOG_URL` — which puts that host
    on the derived allowlist — and a mock patched the wrong function, so the
    suite made a real request to raw.githubusercontent.com and reported the
    404 as a test failure. That one was loud. A mock that misses on a
    *succeeding* fetch is a passing test that proves nothing, and a suite whose
    result depends on somebody's connection fails on a train.
    """

    def test_reaching_a_real_host_is_refused_by_name(self):
        from homeautoshop.core.testrunner import NetworkUsedInTests

        with self.assertRaises(NetworkUsedInTests) as caught:
            socket.socket().connect(("raw.githubusercontent.com", 443))

        self.assertIn("raw.githubusercontent.com", str(caught.exception))

    def test_loopback_is_left_alone(self):
        """The suite genuinely uses it — a local database, a live server —
        and blocking that would be blocking the tests, not the network."""
        from homeautoshop.core.testrunner import NetworkUsedInTests

        sock = socket.socket()
        sock.settimeout(0.1)
        try:
            sock.connect(("127.0.0.1", 1))
        except NetworkUsedInTests:
            self.fail("loopback must stay open")
        except OSError:
            pass  # Refused or timed out, which is the point: it was attempted.
        finally:
            sock.close()

    def test_an_unmocked_catalog_fetch_names_the_guard_not_the_server(self):
        """The exact incident, as a test.

        Without the guard this reached a real server and came back HTTP 404,
        which says nothing about what went wrong. `_get` catches the refusal
        and reports it by type, so the failure now carries the guard's name
        all the way up to the caller.
        """
        with override_settings(CATALOG_URL=BASE):
            with self.assertRaises(catalog_lib.CatalogUnavailable) as caught:
                catalog_lib.index(force=True)

        self.assertIn("NetworkUsedInTests", str(caught.exception))


class TheAuthorTravelsTests(Base):
    """Who published a template (§8.1b).

    `author` was displayed on the browse cards and produced by nothing — a
    dead field. It is now carried by the file, emitted into the index, and
    kept after install, because "who said these intervals were right" is the
    question the catalog's review rules exist to answer, and `source =
    imported` only says the template is not ours.
    """

    def test_a_schedule_keeps_its_author(self):
        template = templatelib.load(GOOD.replace("name:", "author: Somebody\nname:", 1))
        self.assertEqual(template.author, "Somebody")

    def test_a_checklist_does_too(self):
        template = checklistlib.load(
            CHECKLIST.replace("name:", "author: Somebody\nname:", 1)
        )
        self.assertEqual(template.author, "Somebody")

    def test_it_survives_a_round_trip(self):
        first = templatelib.load(GOOD.replace("name:", "author: Somebody\nname:", 1))
        text = templatelib.to_yaml(first)
        first.name = "Renamed"
        first.save()

        self.assertEqual(templatelib.load(text.replace("Generic", "Second")).author, "Somebody")

    def test_the_index_carries_it(self):
        root = pathlib.Path(tempfile.mkdtemp())
        (root / "schedules").mkdir()
        for folder in ("checklists", "profiles"):
            (root / folder).mkdir()
        (root / "schedules" / "s.yaml").write_text(
            GOOD.replace("name:", "author: Somebody\nname:", 1), encoding="utf-8"
        )

        call_command("build_catalog", root=str(root), stdout=io.StringIO())

        index = json.loads((root / "index.json").read_text(encoding="utf-8"))
        self.assertEqual(index["entries"][0]["author"], "Somebody")

    def test_an_anonymous_template_carries_no_empty_field(self):
        """A blank author on every entry would be noise in a file people read."""
        root = pathlib.Path(tempfile.mkdtemp())
        for folder in ("schedules", "checklists", "profiles"):
            (root / folder).mkdir()
        (root / "schedules" / "s.yaml").write_text(GOOD, encoding="utf-8")

        call_command("build_catalog", root=str(root), stdout=io.StringIO())

        index = json.loads((root / "index.json").read_text(encoding="utf-8"))
        self.assertNotIn("author", index["entries"][0])


class TheShippedExamplesTests(TestCase):
    """What is actually published in this repository.

    These are the first thing a contributor reads and the first thing an
    operator installs, so they are held to the rules the README asks of
    everybody else — and checked on every run, since nothing else in the suite
    would notice them rotting.
    """

    def examples(self):
        root = pathlib.Path(settings.BASE_DIR) / "catalog"
        return sorted(root.glob("*/*.yaml"))

    def templates(self):
        """Schedules and checklists — the kinds a person can judge by reading."""
        return [p for p in self.examples() if p.parent.name != "profiles"]

    def profiles(self):
        return [p for p in self.examples() if p.parent.name == "profiles"]

    def test_there_are_some(self):
        self.assertGreaterEqual(len(self.examples()), 4)

    def test_every_one_imports(self):
        """Through the real validator, one at a time, each in its own right.

        *The* real validator — the one `build_catalog` picks for that folder.
        This used to read "schedules go to the schedule parser, everything else
        is a checklist", which was true of a catalog holding two kinds and
        started handing parser profiles to the checklist validator the moment a
        third kind was published.
        """
        build_catalog = build_catalog_module()
        for path in self.examples():
            with self.subTest(file=path.name):
                kind = build_catalog.FOLDERS.get(path.parent.name)
                self.assertIsNotNone(kind, f"{path.parent.name}/ is not a catalog folder")
                build_catalog._read(kind, path.read_text(encoding="utf-8"))

    def test_every_one_names_its_author(self):
        for path in self.examples():
            with self.subTest(file=path.name):
                self.assertIn("author:", path.read_text(encoding="utf-8"))

    def test_every_one_says_it_is_generic(self):
        """The README asks reviewers to check that a template is honest about
        being generic. The shipped ones have to pass their own rule: they will
        be applied to vehicles nobody who wrote them has seen."""
        for path in self.templates():
            with self.subTest(file=path.name):
                body = path.read_text(encoding="utf-8").lower()
                self.assertTrue(
                    any(word in body for word in ("generic", "manual", "check")),
                    "an example must say what it is and is not",
                )

    def test_safety_items_are_marked_as_such(self):
        """A brake item that grades like an air filter is the failure mode
        this flag exists to prevent.

        Templates only. A parser profile has no severity to set — it reads what
        a scan tool printed — and this caught the BlueDriver profile for the
        word `Brakes` inside `Anti Lock Brakes`, which is a module name in a
        report rather than a service anybody grades.
        """
        for path in self.templates():
            body = path.read_text(encoding="utf-8")
            if "Brake" not in body:
                continue
            with self.subTest(file=path.name):
                self.assertTrue(
                    "severity: safety" in body or "is_safety_critical: true" in body,
                    "a checklist or schedule naming brakes must flag them",
                )

    def test_every_profile_says_what_it_was_written_against(self):
        """The honesty rule for a profile, which is not the one for a template.

        A schedule has to admit it is generic because it will be applied to
        vehicles nobody who wrote it has seen. A profile's equivalent is that
        its correctness cannot be judged by reading it at all, so it has to say
        what real reports it was developed against — and, where it is not
        proven, that it is not.
        """
        for path in self.profiles():
            with self.subTest(file=path.name):
                body = path.read_text(encoding="utf-8")
                self.assertIn("notes:", body, "a profile must say what it reads")
                self.assertTrue(
                    "written against" in body.lower()
                    or "developed against" in body.lower(),
                    "a profile must name the reports behind it",
                )
                if "verified_against:" not in body:
                    self.assertIn(
                        "nproven",
                        body,
                        "a profile with no proven reports must say so in its notes",
                    )


def build_catalog_module():
    from homeautoshop.core.management.commands import build_catalog

    return build_catalog


class ProvingAProfileTests(TestCase):
    """Provenance for parser profiles (FR-INT-7, §8.1b).

    Raised as: *"I could make one up without the hardware or verification,
    which should have less community trust than one published by the
    manufacturer. Without an author, we're relying on the name alone."*

    Exactly right, and more true for profiles than for anything else in the
    catalog. A schedule states its intervals in prose a reader can judge. A
    profile is regexes run over somebody's scan report, and whether it reads
    that report correctly is not knowable by looking — two files both called
    "XTOOL D8", one from somebody holding the tool and one guessed, are
    indistinguishable by name.

    So a profile carries an `author`, and it may name captured reports from
    the corpus. Naming them is not a claim: `build_catalog` **runs** the
    profile against each and refuses to publish a file that cannot read them.

    **Several, not one** — a profile overfitted to a single report passes a
    single-report check by construction, which is the failure the whole
    mechanism exists to catch.
    """

    def profile(self, **overrides):
        from homeautoshop.diagnostics import profiles as profilelib
        from homeautoshop.scantools import fixtures

        # The D8's own captures, not simply the first two in the corpus. These
        # tests build a profile out of the shipped D8 row and then ask whether
        # verification passes, so the reports have to be ones a D8 wrote — and
        # `samples()[:2]` stopped being that when the corpus grew reports from
        # a dozen other tools and sorted an Autel to the front.
        samples = [
            s
            for s in fixtures.samples()
            if fixtures.BUILT_IN_PARSERS.get(fixtures.tool(s)) == "xtool_d8"
        ]
        if len(samples) < build_catalog_module().PROVEN_AT_LEAST:
            self.skipTest("the captured D8 corpus is not in this checkout")

        profilelib.seed()
        shipped = ParserProfile.objects.get(tool_model="D8")
        text = profilelib.to_yaml(shipped)
        for key, value in overrides.items():
            text += "%s: %s\n" % (key, value)
        stems = [fixtures.stem(c) for c in samples[:2]]
        return text, stems

    def naming(self, stems):
        return "verified_against:\n" + "".join("  - %s\n" % s for s in stems)

    def test_a_profile_that_reads_them_is_proven(self):
        build_catalog = build_catalog_module()
        text, stems = self.profile()

        name, _slug, _description, _author, _extra = build_catalog._read(
            "profile", text + self.naming(stems)
        )

        self.assertTrue(name)

    def test_one_report_is_not_enough(self):
        """A profile overfitted to a single capture passes a single-report
        check by construction. Two different reports is the smallest number
        that says anything about generalizing."""
        build_catalog = build_catalog_module()
        text, stems = self.profile()

        with self.assertRaises(build_catalog.NotVerified) as caught:
            build_catalog._read("profile", text + self.naming(stems[:1]))

        self.assertIn("at least", str(caught.exception))

    def test_a_plausible_invention_does_not_get_the_badge(self):
        """The case raised: somebody writes a profile for hardware they do not
        have. It parses, it looks like the real thing, and it does not
        recognize a genuine report from that tool — which is why the badge is
        earned by running it rather than by claiming it."""
        build_catalog = build_catalog_module()
        _text, stems = self.profile()
        invented = (
            "name: Definitely A Real Scanner\n"
            "tool_vendor: Acme\n"
            "tool_model: X9\n"
            "media_type: pdf\n"
            "author: Somebody\n"
            "fingerprint:\n"
            "  threshold: 0.7\n"
            "  signals:\n"
            "    - kind: doc_text\n"
            "      pattern: 'ACME DIAGNOSTIC SUITE'\n"
            "      weight: 1\n"
        ) + self.naming(stems)

        with self.assertRaises(build_catalog.NotVerified) as caught:
            build_catalog._read("profile", invented)

        self.assertIn("does not recognize", str(caught.exception))

    def test_an_extractor_that_finds_nothing_is_caught(self):
        """The other way a guessed profile is wrong: it imports cleanly, it
        recognizes the report, and then it reads air."""
        from homeautoshop.diagnostics import profiles as profilelib

        build_catalog = build_catalog_module()
        text, stems = self.profile()
        profile = profilelib.from_yaml(text)
        profile.field_extractors = {
            **(profile.field_extractors or {}),
            "odometer": {"strategy": "regex", "pattern": "NOTHING MATCHES THIS EVER"},
        }
        profile.verified_against = stems

        with self.assertRaises(build_catalog.NotVerified) as caught:
            build_catalog._read("profile", profilelib.to_yaml(profile))

        self.assertIn("extracts nothing", str(caught.exception))

    def test_naming_a_report_that_is_not_in_the_corpus_is_refused(self):
        """Otherwise the badge is granted by typing a filename."""
        build_catalog = build_catalog_module()
        text, stems = self.profile()

        with self.assertRaises(build_catalog.NotVerified) as caught:
            build_catalog._read(
                "profile", text + self.naming([stems[0], "NoSuchCapture"])
            )

        self.assertIn("NoSuchCapture", str(caught.exception))

    def test_a_profile_may_be_published_unproven(self):
        """Scan reports carry VINs. Somebody with hardware but no report they
        can publish should still be able to contribute — the screen says the
        profile is unproven, which is honest rather than a bar."""
        build_catalog = build_catalog_module()
        text, _stems = self.profile(author="Somebody")

        name, _slug, _description, author, _extra = build_catalog._read("profile", text)

        self.assertTrue(name)
        self.assertEqual(author, "Somebody")

    def test_the_author_and_reports_survive_a_round_trip(self):
        from homeautoshop.diagnostics import profiles as profilelib

        text, stems = self.profile()
        profile = profilelib.from_yaml(
            text + "author: Somebody\n" + self.naming(stems)
        )

        self.assertEqual(profile.author, "Somebody")
        self.assertEqual(profile.verified_against, stems)
        self.assertIn("author: Somebody", profilelib.to_yaml(profile))

    def test_a_single_name_is_accepted_and_wrapped(self):
        """Somebody with one report should not have to remember YAML list
        syntax to say so; whether one is enough is a publishing rule, not a
        parse error."""
        from homeautoshop.diagnostics import profiles as profilelib

        text, stems = self.profile()

        profile = profilelib.from_yaml(text + "verified_against: %s\n" % stems[0])

        self.assertEqual(profile.verified_against, [stems[0]])

    def test_the_index_marks_a_proven_profile(self):
        text, stems = self.profile(author="Somebody")
        root = pathlib.Path(tempfile.mkdtemp())
        for folder in ("schedules", "checklists", "profiles"):
            (root / folder).mkdir(parents=True)
        (root / "profiles" / "d8.yaml").write_text(
            text + self.naming(stems), encoding="utf-8"
        )

        call_command("build_catalog", root=str(root), stdout=io.StringIO())

        entry = json.loads((root / "index.json").read_text(encoding="utf-8"))["entries"][0]
        self.assertTrue(entry["verified"])
        self.assertEqual(entry["author"], "Somebody")

    def test_and_leaves_an_unproven_one_unmarked(self):
        """Absence of the badge is the signal, so it must not be granted by
        default to anything that merely parsed."""
        text, _stems = self.profile()
        root = pathlib.Path(tempfile.mkdtemp())
        for folder in ("schedules", "checklists", "profiles"):
            (root / folder).mkdir(parents=True)
        (root / "profiles" / "d8.yaml").write_text(text, encoding="utf-8")

        call_command("build_catalog", root=str(root), stdout=io.StringIO())

        entry = json.loads((root / "index.json").read_text(encoding="utf-8"))["entries"][0]
        self.assertNotIn("verified", entry)

    def test_the_browse_screen_distinguishes_them(self):
        user = User.objects.create_user(
            username="andy", password="x" * 16, role=Role.ADMIN
        )
        self.client.force_login(user)
        index = {"entries": [
            {"kind": "profile", "name": "Real D8", "path": "profiles/a.yaml",
             "author": "Somebody", "verified": True},
            {"kind": "profile", "name": "Made up", "path": "profiles/b.yaml"},
        ]}

        with override_settings(CATALOG_URL=BASE):
            with mock.patch(
                "homeautoshop.core.catalog.fetch_json",
                return_value=Response(200, index, 5),
            ):
                page = self.client.get(reverse("catalog_browse"))

        self.assertContains(page, "Proven against real reports")
        self.assertContains(page, "Not proven")


class CapturingAFixtureTests(TestCase):
    """`manage.py capture_fixture` (§8.1b).

    The badge is earned by running a profile against captured reports, so
    somebody has to contribute the captures. Written because the README came
    to describe this command — and a documented command that does not exist is
    precisely the failure SPEC §19 is about.

    Thin on purpose: `scantools/capture.py` already redacts, and this only
    adds the fixture generation. The first draft hand-rolled its own VIN
    handling, which was worse in every respect — it missed tool serials and it
    would have mangled part numbers of the same shape, because it did not key
    off the check digit the way the real one does.
    """

    def report(self, text):
        """A PDF the word extractor can read, built rather than fixtured so
        the test does not need the corpus to be in the checkout."""
        from reportlab.lib.pagesizes import letter
        from reportlab.pdfgen import canvas

        buffer = io.BytesIO()
        page = canvas.Canvas(buffer, pagesize=letter)
        page.drawString(72, 720, text)
        page.save()

        path = pathlib.Path(tempfile.mkdtemp()) / ("Cap%d.pdf" % abs(hash(text)))
        path.write_bytes(buffer.getvalue())
        return path

    #: The corpus is filed one folder per scanner, so a capture has to say
    #: which one it came from. A throwaway name keeps the test out of the
    #: real tool folders.
    TOOL = "test scanner"

    def run_it(self, path):
        out = io.StringIO()
        call_command("capture_fixture", str(path), tool=self.TOOL, stdout=out, stderr=out)
        return out.getvalue()

    def written(self, path):
        from homeautoshop.scantools import capture, fixtures

        made = capture.capture_path(path, self.TOOL)
        return made, fixtures.fixture_path(made)

    def cleanup(self, *paths):
        for path in paths:
            path.unlink(missing_ok=True)
        folder = paths[0].parent if paths else None
        if folder is not None and folder.is_dir() and not any(folder.iterdir()):
            folder.rmdir()

    def test_it_writes_both_halves_of_a_fixture(self):
        source = self.report("DTC P0301 cylinder 1 misfire")
        capture_file, expected = self.written(source)
        try:
            out = self.run_it(source)

            self.assertIn("Wrote", out)
            self.assertTrue(capture_file.exists())
            self.assertTrue(expected.exists())
        finally:
            self.cleanup(capture_file, expected)

    def a_valid_vin(self) -> str:
        """A check-digit-valid VIN, computed rather than written down.

        The tree is swept for real VINs on every run (`scantools.tests`), and
        a valid one committed here would either fail that sweep or need an
        exception added to it. Neither is worth it to test a redactor: the
        check digit is a function, so the test can build a VIN that satisfies
        it without any particular vehicle's number appearing in the source.
        """
        from homeautoshop.assets import vin as vinlib

        # Assembled from fragments so no seventeen-character run appears in
        # this file at all — the sweep matches on shape first, and a literal
        # here would trip it however it was derived.
        body = "1HGCM826" + "0" + "3A004352"
        return body[:8] + vinlib.check_digit(body) + body[9:]

    def test_a_real_vin_is_replaced_before_it_reaches_the_corpus(self):
        """The whole point. A report carries a VIN, the corpus is public, and
        a contributor's own vehicle is what lands in it by accident once and
        permanently."""
        real = self.a_valid_vin()
        source = self.report("VIN %s DTC P0301" % real)
        capture_file, expected = self.written(source)
        try:
            out = self.run_it(source)

            self.assertIn("synthetic stand-ins", out)
            self.assertNotIn(real, capture_file.read_text(encoding="utf-8"))
        finally:
            self.cleanup(capture_file, expected)

    def test_a_part_number_of_the_same_shape_survives(self):
        """Seventeen characters is not a VIN. The check digit is what tells a
        calibration ID from a vehicle, and mangling one would quietly corrupt
        the fixture the profile is proven against."""
        part = "ABCDEFGH123456789"
        source = self.report("Calibration %s" % part)
        capture_file, expected = self.written(source)
        try:
            self.run_it(source)

            self.assertIn(part, capture_file.read_text(encoding="utf-8"))
        finally:
            self.cleanup(capture_file, expected)

    def test_a_report_with_nothing_to_redact_says_so(self):
        """No VIN found is usually a report without one, and occasionally a
        report whose VIN did not survive extraction — worth a second look
        before it goes somewhere public."""
        source = self.report("DTC P0420 catalyst efficiency")
        capture_file, expected = self.written(source)
        try:
            self.assertIn("No VIN was found", self.run_it(source))
        finally:
            self.cleanup(capture_file, expected)

    def test_a_missing_file_says_so(self):
        with self.assertRaises(CommandError):
            self.run_it(pathlib.Path("nowhere/at/all.pdf"))


@override_settings(CATALOG_URL=BASE)
class WhenTheCatalogIsNotThereTests(Base):
    """What a failure says, which is the only part of a failure that helps.

    Written after the real one: the address was right, the files were right,
    and the repository was private — so the catalog served 404 to everybody
    and the screen said `HTTP 404`. That is the literal truth and close to
    useless, because a raw file host answers 404 for a repository it will not
    admit exists, and a private fork looks exactly like a working one until
    something reads from it.
    """

    def failing(self, status):
        from homeautoshop.core.outbound import OutboundFailed

        return mock.patch(
            "homeautoshop.core.catalog.fetch_json",
            side_effect=OutboundFailed("HTTP %d" % status, status=status),
        )

    def test_a_404_names_the_likely_causes(self):
        with self.failing(404):
            with self.assertRaises(catalog_lib.CatalogUnavailable) as caught:
                catalog_lib.index(force=True)

        message = str(caught.exception)
        self.assertIn("public", message)
        self.assertIn("pushed", message)

    def test_another_failure_is_reported_as_itself(self):
        """A 500 is the server's problem and inventing causes for it would be
        guessing at somebody else's outage."""
        with self.failing(500):
            with self.assertRaises(catalog_lib.CatalogUnavailable) as caught:
                catalog_lib.index(force=True)

        self.assertIn("500", str(caught.exception))
        self.assertNotIn("public", str(caught.exception))

    def test_the_screen_shows_it_rather_than_breaking(self):
        with self.failing(404):
            page = self.client.get(reverse("catalog_browse"))

        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "public")

    def test_a_missing_file_explains_itself_too(self):
        """An entry can be listed and its file absent — a half-pushed commit,
        which is the same confusion one level down."""
        from homeautoshop.core.outbound import OutboundFailed

        entry = catalog_lib.Entry(
            kind="schedule", slug="x", name="x", path="schedules/gone.yaml"
        )
        with mock.patch(
            "homeautoshop.core.catalog.fetch_text",
            side_effect=OutboundFailed("HTTP 404", status=404),
        ):
            with self.assertRaises(catalog_lib.CatalogUnavailable) as caught:
                catalog_lib.fetch_file(entry)

        self.assertIn("schedules/gone.yaml", str(caught.exception))


class RemovingATemplateTests(Base):
    """Templates could be added and never taken away.

    Reported as: *"there doesn't seem to be any way to remove templates now,
    even if bad, out of date, not needed"* — and *"they should be removeable
    both from the templates page and the catalog, the two obvious places
    someone would check."*

    The second half is the same lesson the cores screen taught: a control that
    exists in one of the two places somebody looks is a control they hunt for.
    """

    def schedule(self):
        return templatelib.load(GOOD)

    def test_a_schedule_is_removed_from_the_list(self):
        template = self.schedule()

        self.client.post(reverse("template_delete", args=[template.pk]))

        self.assertFalse(ScheduleTemplate.objects.filter(pk=template.pk).exists())

    def test_it_is_soft_so_the_trash_still_has_it(self):
        template = self.schedule()

        self.client.post(reverse("template_delete", args=[template.pk]))

        self.assertTrue(ScheduleTemplate.all_objects.filter(pk=template.pk).exists())

    def test_what_was_already_scheduled_is_untouched(self):
        """Applying materializes the items onto the vehicle, pointing at
        service definitions rather than at the template. Saying so is what
        makes the button pressable."""
        from homeautoshop.maintenance.services import apply_template

        asset = Asset.objects.create(nickname="Aero")
        template = self.schedule()
        apply_template(asset, template)
        before = AssetServiceItem.objects.filter(asset=asset).count()

        response = self.client.post(
            reverse("template_delete", args=[template.pk]), follow=True
        )

        self.assertEqual(AssetServiceItem.objects.filter(asset=asset).count(), before)
        self.assertGreater(before, 0)
        self.assertContains(response, "untouched")

    def test_a_checklist_goes_the_same_way(self):
        template = checklistlib.load(CHECKLIST)

        self.client.post(reverse("checklist_delete", args=[template.pk]))

        self.assertFalse(InspectionTemplate.objects.filter(pk=template.pk).exists())

    def test_a_built_in_may_go_too(self):
        """Somebody who runs no diesels should not scroll past a diesel
        schedule forever."""
        from homeautoshop.maintenance import seed

        seed.install()
        builtin = ScheduleTemplate.objects.filter(source="builtin").first()

        self.client.post(reverse("template_delete", args=[builtin.pk]))

        self.assertFalse(ScheduleTemplate.objects.filter(pk=builtin.pk).exists())

    def test_and_seeding_again_does_not_bring_it_back(self):
        """The trap under the whole feature. `slug` is uniquely constrained
        without regard to `deleted_at`, so the alive manager would miss the
        soft-deleted row and the create would fail on the constraint —
        re-seeding would crash, not merely argue with the operator."""
        from homeautoshop.maintenance import seed

        seed.install()
        builtin = ScheduleTemplate.objects.filter(source="builtin").first()
        slug = builtin.slug
        self.client.post(reverse("template_delete", args=[builtin.pk]))

        seed.install()  # must not raise

        self.assertFalse(ScheduleTemplate.objects.filter(slug=slug).exists())

    def test_the_same_holds_for_a_built_in_checklist(self):
        from homeautoshop.inspections import seed as inspection_seed

        inspection_seed.install()
        builtin = InspectionTemplate.objects.filter(source="builtin").first()
        slug = builtin.slug
        self.client.post(reverse("checklist_delete", args=[builtin.pk]))

        inspection_seed.install()

        self.assertFalse(InspectionTemplate.objects.filter(slug=slug).exists())

    def test_the_templates_page_offers_it(self):
        template = self.schedule()

        page = self.client.get(reverse("template_list"))

        self.assertContains(page, reverse("template_delete", args=[template.pk]))

    def test_and_so_does_the_catalog(self):
        """The other of the two obvious places."""
        template = self.schedule()
        index = {"entries": [{
            "kind": "schedule", "slug": template.slug, "name": template.name,
            "path": "schedules/x.yaml",
        }]}

        with override_settings(CATALOG_URL=BASE):
            with mock.patch(
                "homeautoshop.core.catalog.fetch_json",
                return_value=Response(200, index, 5),
            ):
                page = self.client.get(reverse("catalog_browse"))

        self.assertContains(page, reverse("template_delete", args=[template.pk]))

    def test_a_catalog_entry_knows_which_local_row_it_is(self):
        template = self.schedule()
        index = {"entries": [{
            "kind": "schedule", "slug": template.slug, "name": "A different name",
            "path": "schedules/x.yaml",
        }]}

        with override_settings(CATALOG_URL=BASE):
            with mock.patch(
                "homeautoshop.core.catalog.fetch_json",
                return_value=Response(200, index, 5),
            ):
                entry = catalog_lib.index(force=True).schedules[0]

        self.assertTrue(entry.installed)
        self.assertEqual(entry.installed_pk, str(template.pk))

    def test_something_in_the_trash_does_not_count_as_installed(self):
        """Offering to remove it again would be offering to do nothing."""
        template = self.schedule()
        template.delete()
        index = {"entries": [{
            "kind": "schedule", "slug": template.slug, "name": template.name,
            "path": "schedules/x.yaml",
        }]}

        with override_settings(CATALOG_URL=BASE):
            with mock.patch(
                "homeautoshop.core.catalog.fetch_json",
                return_value=Response(200, index, 5),
            ):
                entry = catalog_lib.index(force=True).schedules[0]

        self.assertFalse(entry.installed)

    def test_it_takes_a_post(self):
        template = self.schedule()
        self.assertEqual(
            self.client.get(reverse("template_delete", args=[template.pk])).status_code,
            405,
        )

    def test_a_member_cannot_remove_one(self):
        template = self.schedule()
        member = User.objects.create_user(
            username="pat", password="x" * 16, role=Role.MEMBER
        )
        self.client.force_login(member)

        response = self.client.post(reverse("template_delete", args=[template.pk]))

        self.assertEqual(response.status_code, 403)
        self.assertTrue(ScheduleTemplate.objects.filter(pk=template.pk).exists())


class ThePublishedSetComplementsTheBuiltInsTests(TestCase):
    """A catalog entry that duplicates a shipped template is worse than none.

    Reported from a screenshot: the Apply dropdown showed `Small engine /
    equipment` beside `Small engine equipment`, one shipped and one published
    by me, covering the same ground. Four more of the seed entries turned out
    to be near-twins of built-ins for the same reason — they were written
    without checking what already ships.

    A duplicate does not merely add noise. It makes somebody choose between
    two things that are the same, and there is no information anywhere on the
    screen that would let them choose correctly.
    """

    def builtins(self):
        from homeautoshop.inspections.seed import TEMPLATES as CHECKLISTS
        from homeautoshop.maintenance.seed import TEMPLATES as SCHEDULES

        slugs = set(SCHEDULES) | set(CHECKLISTS)
        names = {v[0] for v in SCHEDULES.values()} | {v[0] for v in CHECKLISTS.values()}
        return slugs, names

    def published(self):
        root = pathlib.Path(settings.BASE_DIR) / "catalog"
        return json.loads((root / "index.json").read_text(encoding="utf-8"))["entries"]

    def test_no_published_entry_repeats_a_built_in(self):
        slugs, names = self.builtins()

        for entry in self.published():
            with self.subTest(entry=entry["slug"]):
                self.assertNotIn(entry["slug"], slugs)
                self.assertNotIn(entry["name"], names)

    def test_and_none_repeats_another(self):
        entries = self.published()
        slugs = [e["slug"] for e in entries]
        names = [e["name"] for e in entries]

        self.assertEqual(len(slugs), len(set(slugs)))
        self.assertEqual(len(names), len(set(names)))

    def test_the_shipped_seed_is_still_reachable_by_name(self):
        """The guard above compares against the seed module, so it only means
        anything while that module is what actually seeds an instance."""
        from homeautoshop.maintenance import seed

        seed.install()

        self.assertTrue(
            ScheduleTemplate.objects.filter(source="builtin", slug="gas-normal").exists()
        )


class GettingABuiltInBackTests(Base):
    """What happens after somebody removes a shipped template.

    Asked directly: *"they aren't in the catalog so there is no way to get it
    back unless someone has exported it previously."* Correct, and worse than
    stated — templates were soft-deleted and absent from the trash screen, so
    a removal was permanent **and** invisible at the same time.

    Two ways home now, because they cover different lengths of time. Inside
    thirty days it is in the trash with everything else. After that the trash
    has aged out, and the shipped set is still in the image — so an explicit
    action puts it back. The catalog deliberately publishes nothing that
    duplicates a built-in, so it was never going to be the answer here.
    """

    def a_builtin(self):
        from homeautoshop.maintenance import seed

        seed.install()
        return ScheduleTemplate.objects.filter(source="builtin").first()

    def test_a_removed_template_is_in_the_trash(self):
        template = self.a_builtin()
        self.client.post(reverse("template_delete", args=[template.pk]))

        page = self.client.get(reverse("trash"))

        self.assertContains(page, template.name)

    def test_and_can_be_restored_from_there(self):
        template = self.a_builtin()
        self.client.post(reverse("template_delete", args=[template.pk]))

        self.client.post(reverse("trash_restore", args=["schedule_template", template.pk]))

        self.assertTrue(ScheduleTemplate.objects.filter(pk=template.pk).exists())

    def test_a_removed_checklist_is_there_too(self):
        from homeautoshop.inspections import seed as checklist_seed

        checklist_seed.install()
        template = InspectionTemplate.objects.filter(source="builtin").first()
        self.client.post(reverse("checklist_delete", args=[template.pk]))

        self.assertContains(self.client.get(reverse("trash")), template.name)

    def test_the_shipped_set_can_be_put_back_at_any_time(self):
        """The path that still works after the trash has aged out."""
        template = self.a_builtin()
        slug = template.slug
        self.client.post(reverse("template_delete", args=[template.pk]))
        # Past the trash: the row is gone for good, as a purge would leave it.
        ScheduleTemplate.all_objects.filter(pk=template.pk).delete()

        self.client.post(reverse("restore_builtins"), follow=True)

        self.assertTrue(ScheduleTemplate.objects.filter(slug=slug).exists())

    def test_it_revives_one_still_sitting_in_the_trash(self):
        template = self.a_builtin()
        self.client.post(reverse("template_delete", args=[template.pk]))

        self.client.post(reverse("restore_builtins"))

        self.assertTrue(ScheduleTemplate.objects.filter(pk=template.pk).exists())

    def test_it_leaves_what_you_wrote_alone(self):
        """It restores the shipped set, not everything ever deleted."""
        mine = templatelib.load(GOOD)
        self.client.post(reverse("template_delete", args=[mine.pk]))

        self.client.post(reverse("restore_builtins"))

        self.assertFalse(ScheduleTemplate.objects.filter(pk=mine.pk).exists())

    def test_and_says_when_there_was_nothing_to_do(self):
        # All three seeders, because the action covers all three: a shop
        # missing its checklists or its parser profiles genuinely does have
        # something put back, and the message would be wrong to say otherwise.
        from homeautoshop.diagnostics import profiles as profile_seed
        from homeautoshop.inspections import seed as checklist_seed
        from homeautoshop.maintenance import seed

        seed.install()
        checklist_seed.install()
        profile_seed.seed()

        response = self.client.post(reverse("restore_builtins"), follow=True)

        self.assertContains(response, "already here")

    def test_restarting_still_does_not_bring_one_back(self):
        """The distinction the whole design rests on: booting respects the
        removal, and only somebody pressing the button overrides it."""
        from homeautoshop.maintenance import seed

        template = self.a_builtin()
        slug = template.slug
        self.client.post(reverse("template_delete", args=[template.pk]))

        seed.install()

        self.assertFalse(ScheduleTemplate.objects.filter(slug=slug).exists())

    def test_a_member_cannot_restore(self):
        member = User.objects.create_user(
            username="pat", password="x" * 16, role=Role.MEMBER
        )
        self.client.force_login(member)

        self.assertEqual(
            self.client.post(reverse("restore_builtins")).status_code, 403
        )


class ParserProfilesAreOneOfTheTemplatesTests(Base):
    """Reported as: the only way to that page is the scan queue.

    It was, and the page it led to listed the third of three things that are
    all the same kind of thing — YAML with an author and a source, installed
    from the same catalog through the same validator. The templates page even
    said in its own copy that the catalog publishes "scan-tool profiles" while
    listing none of them.
    """

    def a_profile(self):
        from homeautoshop.diagnostics import profiles as profile_seed
        from homeautoshop.diagnostics.models import ParserProfile

        profile_seed.seed()
        return ParserProfile.objects.first()

    def test_the_templates_page_lists_them(self):
        profile = self.a_profile()
        page = self.client.get(reverse("template_list"))
        self.assertContains(page, profile.name)

    def test_the_old_address_still_goes_somewhere(self):
        """It was linked and bookmarkable; a dead link is worse than a hop."""
        response = self.client.get(reverse("profile_list"))
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response["Location"].startswith(reverse("template_list")))

    def test_the_scan_queue_button_points_at_the_page_that_lists_them(self):
        page = self.client.get(reverse("diagnostic_queue")).content.decode()
        self.assertIn(reverse("template_list") + "#profiles", page)

    def test_it_is_reachable_without_knowing_about_scans(self):
        """The actual complaint: findability, not the absence of a screen."""
        page = self.client.get(reverse("dashboard")).content.decode()
        self.assertIn(reverse("template_list"), page)


class RemovingAParserProfileTests(Base):
    """Switching one off was the only way to say no to it.

    The same gap schedules had before they were removable, and scheduled items
    had before that: a profile for a tool nobody here owns, or one superseded
    by a better version, stayed in the list forever — and the catalog could
    install one that nothing could take away.
    """

    def a_profile(self):
        from homeautoshop.diagnostics import profiles as profile_seed
        from homeautoshop.diagnostics.models import ParserProfile

        profile_seed.seed()
        return ParserProfile.objects.first()

    def test_a_profile_can_be_removed(self):
        from homeautoshop.diagnostics.models import ParserProfile

        profile = self.a_profile()
        self.client.post(reverse("profile_delete", args=[profile.pk]))
        self.assertFalse(ParserProfile.objects.filter(pk=profile.pk).exists())

    def test_removal_is_soft_and_lands_in_the_trash(self):
        profile = self.a_profile()
        self.client.post(reverse("profile_delete", args=[profile.pk]))
        self.assertContains(self.client.get(reverse("trash")), profile.name)

    def test_and_can_be_restored_from_there(self):
        from homeautoshop.diagnostics.models import ParserProfile

        profile = self.a_profile()
        self.client.post(reverse("profile_delete", args=[profile.pk]))

        self.client.post(reverse("trash_restore", args=["parser_profile", profile.pk]))

        self.assertTrue(ParserProfile.objects.filter(pk=profile.pk).exists())

    def test_a_scan_it_already_read_keeps_its_result(self):
        """`SET_NULL` plus a version column of its own — the session goes on
        saying which version read it after the profile is gone."""
        from homeautoshop.assets.models import Asset
        from homeautoshop.diagnostics.models import DiagnosticSession

        profile = self.a_profile()
        asset = Asset.objects.create(nickname="Truck")
        session = DiagnosticSession.objects.create(
            asset=asset, parser_profile=profile, parser_version=profile.version
        )

        self.client.post(reverse("profile_delete", args=[profile.pk]))

        session.refresh_from_db()
        self.assertEqual(session.parser_version, profile.version)

    def test_restarting_does_not_bring_a_removed_one_back(self):
        """Same rule as the templates: booting respects the removal.

        Not only a matter of not arguing with the operator — `(name, version)`
        is uniquely constrained regardless of `deleted_at`, so re-creating one
        would fail on the constraint rather than merely annoy.
        """
        from homeautoshop.diagnostics import profiles as profile_seed
        from homeautoshop.diagnostics.models import ParserProfile

        profile = self.a_profile()
        self.client.post(reverse("profile_delete", args=[profile.pk]))

        profile_seed.seed()

        self.assertFalse(ParserProfile.objects.filter(pk=profile.pk).exists())

    def test_but_restore_shipped_templates_does(self):
        """Otherwise removing the profile that reads XTOOL D8 reports is a
        one-way door: the catalog deliberately publishes no twin of it."""
        from homeautoshop.diagnostics.models import ParserProfile

        profile = self.a_profile()
        name, version = profile.name, profile.version
        self.client.post(reverse("profile_delete", args=[profile.pk]))
        # Past the trash, as a purge would leave it.
        ParserProfile.all_objects.filter(pk=profile.pk).delete()

        self.client.post(reverse("restore_builtins"), follow=True)

        self.assertTrue(ParserProfile.objects.filter(name=name, version=version).exists())

    def test_it_still_leaves_what_you_wrote_alone(self):
        from homeautoshop.diagnostics import profiles as profilelib
        from homeautoshop.diagnostics.models import ParserProfile

        mine = profilelib.from_yaml(profilelib.GENERIC_TEXT)
        mine.name = "Mine"
        mine.source = "user"
        mine.save()
        self.client.post(reverse("profile_delete", args=[mine.pk]))

        self.client.post(reverse("restore_builtins"))

        self.assertFalse(ParserProfile.objects.filter(pk=mine.pk).exists())


CODE_LIST = json.dumps({
    "make": "Testla",
    "aliases": ["Tesler"],
    "version": 3,
    "documents": [{
        "source": "Testla service manual",
        "codes": {"P1500": "Wastegate position sensor performance"},
    }],
})

CODES_INDEX = {
    "entries": [{
        "kind": "codes",
        "slug": "testla",
        "name": "Testla",
        "path": "codes/testla.json",
        "description": "1 code for Testla, from Testla service manual",
        "version": 3,
        "applies_to": ["Tesler"],
    }]
}


@override_settings(CATALOG_URL=BASE)
class PublishingCodeListsTests(Base):
    """A manufacturer's code list, published rather than bundled.

    The split is the point: the ISO/SAE sets answer for every vehicle ever
    built and stay in the image, while ninety makes of manufacturer codes
    would be eighteen thousand definitions shipped so that each shop could use
    a few hundred. The same reasoning that publishes parser profiles.
    """

    def browse(self, payload=None):
        return mock.patch(
            "homeautoshop.core.catalog.fetch_json",
            return_value=Response(200, payload or CODES_INDEX, 5),
        )

    def install(self, body=CODE_LIST):
        with self.browse(), mock.patch(
            "homeautoshop.core.catalog.fetch_text", return_value=body
        ):
            return self.client.post(
                reverse("catalog_install"),
                {"kind": "codes", "path": "codes/testla.json"},
                follow=True,
            )

    def test_it_is_offered_on_the_browse_screen(self):
        with self.browse():
            published = catalog_lib.index()

        self.assertEqual(len(published.codes), 1)
        self.assertEqual(published.codes[0].name, "Testla")
        self.assertEqual(published.codes[0].version, 3)

    def test_installing_one_makes_it_answer(self):
        from homeautoshop.diagnostics import dtc

        self.addCleanup(dtc._lists.cache_clear)
        self.install()

        self.assertEqual(dtc.explain("P1500", make="Testla").source, dtc.MAKE)

    def test_the_message_counts_what_arrived_rather_than_talking_about_vehicles(self):
        """A code list is a dictionary, not a template. It starts answering
        the moment it is installed, and there is nothing to apply."""
        response = self.install()

        self.assertContains(response, "1 definitions")
        self.assertNotContains(response, "until you apply it")

    def test_what_is_already_installed_is_marked(self):
        self.install()

        with self.browse():
            entry = catalog_lib.index().codes[0]

        self.assertTrue(entry.installed)
        self.assertEqual(entry.installed_version, 3)
        self.assertFalse(entry.update_available)

    def test_a_newer_published_version_is_offered_not_applied(self):
        """A definition somebody is reading today should not change under them
        because a catalog was edited this morning."""
        self.install()
        newer = {"entries": [{**CODES_INDEX["entries"][0], "version": 4}]}

        with self.browse(newer):
            # Forced, because installing already fetched and cached the index.
            # That cache is deliberate — browsing should not be a request per
            # page load — and "Check again" is the button that bypasses it.
            entry = catalog_lib.index(force=True).codes[0]

        self.assertTrue(entry.update_available)
        self.assertEqual(entry.installed_version, 3)

    def test_installing_the_newer_one_replaces_rather_than_doubles(self):
        from homeautoshop.diagnostics.models import InstalledCodeList

        self.install()
        self.install(json.dumps({**json.loads(CODE_LIST), "version": 4}))

        self.assertEqual(InstalledCodeList.objects.count(), 1)
        self.assertEqual(InstalledCodeList.objects.get().version, 4)

    def test_a_file_claiming_to_be_the_standard_is_refused(self):
        """The catalog gets no privileged path. `CATALOG_URL` is a setting, so
        the editorial review protecting the default catalog is somebody else's
        process the moment it is pointed elsewhere — and an ISO/SAE list is
        presented to the operator as fact."""
        from homeautoshop.diagnostics.models import InstalledCodeList

        response = self.install(json.dumps({
            "make": "Testla",
            "documents": [{
                "source": "A standard, honestly",
                "scope": "iso_sae",
                "codes": {"P0420": "Catalyst below threshold"},
            }],
        }))

        self.assertContains(response, "refused")
        self.assertFalse(InstalledCodeList.objects.exists())

    def test_the_makes_it_also_covers_are_published_with_it(self):
        """Ford's list is the Ford Motor Company Group's. A shop with a
        Lincoln has to be able to find it, or the badge on the wing is the one
        thing stopping them installing the list that covers their vehicle."""
        with self.browse():
            self.assertEqual(catalog_lib.index().codes[0].applies_to, ("Tesler",))


class TheImportBoxesTests(Base):
    """Picking a file used to give no sign it had done anything.

    The label still read "Choose a file", the box beside it stayed empty, and
    the only way to find out whether the picker had taken anything was to press
    Import and see. The file is now read into the box, so the screen shows what
    will be sent — better feedback than a filename, because the question is not
    *did it take my file* but *is this the right one*, and these are documents
    somebody may hold three near-identical copies of.

    The reading is done in the browser and is an enhancement only: with the
    script blocked, the file input posts the file and the server reads it, the
    way it always did.
    """

    def setUp(self):
        super().setUp()
        self.body = self.client.get(reverse("template_list")).content.decode()

    def test_every_import_form_is_wired_for_it(self):
        """All four, because they are the same control four times over and one
        of them behaving differently is worse than none of them doing."""
        self.assertEqual(self.body.count("data-text-import"), 4 * 3)

    def test_the_code_list_import_is_one_of_them(self):
        """The one this was reported against."""
        import re

        where = reverse("codelist_import")
        forms = re.findall(r"<form[^>]*%s[^>]*>" % re.escape(where), self.body)

        self.assertEqual(len(forms), 1)
        self.assertIn("data-text-import", forms[0])

    def test_the_form_still_posts_a_file_without_the_script(self):
        """The enhancement clears the picker once it has the text. Nothing may
        depend on that having happened."""
        self.assertIn('enctype="multipart/form-data"', self.body)
        self.assertIn('name="codelist"', self.body)
