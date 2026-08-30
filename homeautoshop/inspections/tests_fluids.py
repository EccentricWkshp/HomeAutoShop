"""
Fluids are filed by where you check them, not by being fluids.

An inspection area answers "where am I standing?". Engine oil, coolant, brake
fluid and washer fluid are checked with the hood up; differential and
transfer-case fluid are checked on your back. A separate "Fluids" area sent you
round the vehicle twice, so it was retired — and these tests hold that line,
including for the inspections recorded before it changed.
"""

from __future__ import annotations

from django.test import TestCase

from homeautoshop.accounts.models import User
from homeautoshop.assets.models import Asset

from .models import Area, InspectionPoint, InspectionResult, InspectionTemplate
from .seed import install as install_templates
from .services import start


class FluidFilingTests(TestCase):
    def setUp(self):
        install_templates()

    def test_no_builtin_point_is_filed_under_fluids(self):
        stragglers = InspectionPoint.objects.filter(area="fluids")
        self.assertEqual(list(stragglers), [], "a built-in is still filed under the retired area")

    def test_every_builtin_point_uses_a_real_area(self):
        valid = {value for value, _label in Area.choices}
        for point in InspectionPoint.objects.all():
            self.assertIn(point.area, valid, f"{point.name} has an unknown area")

    def test_fluids_reside_where_you_check_them(self):
        annual = InspectionTemplate.objects.get(slug="annual-safety")
        areas = {p.name: p.area for p in annual.points.all()}

        for name in (
            "Engine oil condition",
            "Coolant condition",
            "Brake fluid condition",
            "Power steering fluid",
            "Automatic transmission fluid",
            "Washer fluid",
        ):
            self.assertEqual(areas.get(name), Area.UNDER_HOOD, name)

        # These are not under the hood on any vehicle, so they are not filed there.
        for name in (
            "Manual transmission fluid",
            "Transfer case fluid",
            "Front differential fluid",
            "Rear differential fluid",
        ):
            self.assertEqual(areas.get(name), Area.UNDER_VEHICLE, name)

    def test_the_annual_check_covers_the_whole_fluid_set(self):
        annual = InspectionTemplate.objects.get(slug="annual-safety")
        names = {p.name for p in annual.points.all()}
        for fluid in (
            "Engine oil condition",
            "Coolant condition",
            "Brake fluid condition",
            "Clutch fluid",
            "Power steering fluid",
            "Automatic transmission fluid",
            "Washer fluid",
            "Diesel exhaust fluid (DEF)",
            "Manual transmission fluid",
            "Transfer case fluid",
            "Front differential fluid",
            "Rear differential fluid",
        ):
            self.assertIn(fluid, names)

        # The vague point it replaced is gone, not sitting alongside it.
        self.assertNotIn("Fluid levels", names)

    def test_fluids_a_vehicle_may_not_have_are_marked_optional(self):
        annual = InspectionTemplate.objects.get(slug="annual-safety")
        optional = {p.name for p in annual.points.filter(is_optional=True)}
        for name in ("Transfer case fluid", "Manual transmission fluid", "Clutch fluid"):
            self.assertIn(name, optional)
        # Every vehicle has engine oil.
        self.assertNotIn("Engine oil condition", optional)


class HistoricalAreaTests(TestCase):
    """A retired area must not make an old inspection unreadable."""

    def test_an_old_snapshot_still_renders_its_area_name(self):
        asset = Asset.objects.create(nickname="Old truck")
        template = InspectionTemplate.objects.create(name="Legacy", slug="legacy")
        InspectionPoint.objects.create(template=template, name="Engine oil", area="under_hood")
        inspection = start(asset, template)

        # Rewrite one result to look like a record taken before the change.
        result = inspection.results.first()
        result.point_snapshot = {**result.point_snapshot, "area": "fluids"}
        result.save()

        result = InspectionResult.objects.get(pk=result.pk)
        self.assertEqual(result.area, "fluids")
        self.assertEqual(result.area_display, "Fluids")

    def test_the_migration_refiles_template_points_but_not_history(self):
        """Templates are configuration; snapshots are history."""
        asset = Asset.objects.create(nickname="Truck")
        template = InspectionTemplate.objects.create(name="Legacy", slug="legacy")
        point = InspectionPoint.objects.create(
            template=template, name="Engine oil", area="under_hood"
        )
        inspection = start(asset, template)
        result = inspection.results.first()
        result.point_snapshot = {**result.point_snapshot, "area": "fluids"}
        result.save()

        # Call the shipped migration function, not a hand-rolled equivalent —
        # otherwise this asserts against a copy of the logic rather than it.
        from importlib import import_module

        from django.apps import apps as registry

        migration = import_module(
            "homeautoshop.inspections.migrations.0002_alter_inspectionpoint_area"
        )
        InspectionPoint.objects.filter(pk=point.pk).update(area="fluids")
        migration.refile_fluids(registry, None)

        point.refresh_from_db()
        result.refresh_from_db()
        self.assertEqual(point.area, Area.UNDER_HOOD)
        self.assertEqual(result.point_snapshot["area"], "fluids")


class SeedPruningTests(TestCase):
    def test_reinstalling_drops_points_a_builtin_no_longer_defines(self):
        install_templates()
        annual = InspectionTemplate.objects.get(slug="annual-safety")
        InspectionPoint.objects.create(
            template=annual, name="Fluid levels", area="under_hood", sequence=99
        )

        install_templates()
        self.assertFalse(annual.points.filter(name="Fluid levels").exists())

    def test_reinstalling_is_idempotent(self):
        install_templates()
        before = InspectionPoint.objects.count()
        install_templates()
        self.assertEqual(InspectionPoint.objects.count(), before)
