"""
Getting rid of an inspection, and adding to one (SPEC §7.8, FR-DVI-3/6).

The gap these cover was reported from actual use: an inspection started by
mistake could not be abandoned or deleted from anywhere in the UI, even though
`Inspection.Status.ABANDONED` had existed since the first migration and nothing
ever set it. A status nothing can reach is not a feature.
"""

from __future__ import annotations

from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse

from homeautoshop.accounts.models import User
from homeautoshop.assets.models import Asset

from .models import Area, Inspection, InspectionResult, InspectionTemplate
from .seed import install as install_templates
from .services import abandon, add_check, complete, resume, start


class LifecycleTests(TestCase):
    def setUp(self):
        install_templates()
        self.user = User.objects.create_user(username="andy", password="x" * 16)
        self.asset = Asset.objects.create(nickname="Red truck")
        self.template = InspectionTemplate.objects.get(slug="winter-prep")
        self.inspection = start(self.asset, self.template, user=self.user)
        self.client.force_login(self.user)

    def test_abandoning_keeps_the_record_and_everything_on_it(self):
        recorded = self.inspection.results.count()
        abandon(self.inspection)
        self.inspection.refresh_from_db()

        self.assertEqual(self.inspection.status, Inspection.Status.ABANDONED)
        self.assertFalse(self.inspection.is_draft)
        # Abandoning is not deleting: the walk still happened.
        self.assertEqual(self.inspection.results.count(), recorded)
        self.assertTrue(Inspection.objects.filter(pk=self.inspection.pk).exists())

    def test_an_abandoned_inspection_can_be_picked_back_up(self):
        abandon(self.inspection)
        resume(self.inspection)
        self.inspection.refresh_from_db()
        self.assertTrue(self.inspection.is_draft)

    def test_a_finished_inspection_cannot_be_abandoned(self):
        complete(self.inspection, force=True)
        with self.assertRaises(ValidationError):
            abandon(self.inspection)

    def test_deleting_hides_it_but_keeps_it_recoverable(self):
        pk = self.inspection.pk
        response = self.client.post(reverse("inspection_delete", args=[pk]))
        self.assertRedirects(response, reverse("inspection_list"))

        self.assertFalse(Inspection.objects.filter(pk=pk).exists())
        self.assertTrue(Inspection.all_objects.filter(pk=pk).exists())
        self.assertIsNotNone(Inspection.all_objects.get(pk=pk).deleted_at)

    def test_a_member_may_delete_their_own_inspection(self):
        """`trash.manage` is admin-only and gates restoring, not discarding."""
        member = User.objects.create_user(username="helper", password="x" * 16, role="member")
        self.client.force_login(member)
        response = self.client.post(reverse("inspection_delete", args=[self.inspection.pk]))
        self.assertEqual(response.status_code, 302)
        self.assertFalse(Inspection.objects.filter(pk=self.inspection.pk).exists())

    def test_a_deleted_inspection_is_restorable_from_the_trash(self):
        self.client.post(reverse("inspection_delete", args=[self.inspection.pk]))
        admin = User.objects.create_user(username="boss", password="x" * 16, role="admin")
        self.client.force_login(admin)

        self.assertContains(self.client.get(reverse("trash")), self.template.name)
        self.client.post(reverse("trash_restore", args=["inspection", self.inspection.pk]))
        self.assertTrue(Inspection.objects.filter(pk=self.inspection.pk).exists())

    def test_the_detail_page_offers_a_way_out(self):
        """The reported bug: no affordance existed to get rid of one."""
        page = self.client.get(reverse("inspection_detail", args=[self.inspection.pk]))
        self.assertContains(page, reverse("inspection_abandon", args=[self.inspection.pk]))
        self.assertContains(page, reverse("inspection_delete", args=[self.inspection.pk]))


class AdHocCheckTests(TestCase):
    def setUp(self):
        install_templates()
        self.user = User.objects.create_user(username="andy", password="x" * 16)
        self.asset = Asset.objects.create(nickname="Red truck")
        self.inspection = start(
            self.asset, InspectionTemplate.objects.get(slug="winter-prep"), user=self.user
        )
        self.client.force_login(self.user)

    def test_a_check_can_be_added_without_touching_the_template(self):
        template_points = InspectionTemplate.objects.get(slug="winter-prep").points.count()
        result = add_check(
            self.inspection, name="Transfer case fluid", area=Area.UNDER_VEHICLE
        )

        self.assertEqual(result.name, "Transfer case fluid")
        self.assertEqual(result.area, Area.UNDER_VEHICLE)
        self.assertTrue(result.is_ad_hoc)
        self.assertIsNone(result.point_id)
        # No template was edited, so no other inspection changes.
        self.assertEqual(
            InspectionTemplate.objects.get(slug="winter-prep").points.count(), template_points
        )

    def test_a_unit_makes_it_a_measurement(self):
        result = add_check(self.inspection, name="Diff fluid level", area=Area.UNDER_VEHICLE, unit="mm")
        self.assertTrue(result.takes_measurement)
        self.assertEqual(result.unit, "mm")

        without = add_check(self.inspection, name="Skid plate", area=Area.UNDER_VEHICLE)
        self.assertFalse(without.takes_measurement)

    def test_an_added_check_sorts_after_the_template_points_in_its_area(self):
        result = add_check(self.inspection, name="Block heater cord", area=Area.UNDER_HOOD)
        under_hood = [
            r for r in self.inspection.results.all() if r.area == Area.UNDER_HOOD
        ]
        self.assertEqual(under_hood[-1].pk, result.pk)

    def test_it_cannot_be_added_to_a_finished_inspection(self):
        complete(self.inspection, force=True)
        with self.assertRaises(ValidationError):
            add_check(self.inspection, name="Too late", area=Area.UNDER_HOOD)

    def test_a_nameless_check_is_refused(self):
        with self.assertRaises(ValidationError):
            add_check(self.inspection, name="   ", area=Area.UNDER_HOOD)

    def test_an_added_check_can_be_removed_but_a_template_point_cannot(self):
        mine = add_check(self.inspection, name="Block heater cord", area=Area.UNDER_HOOD)
        theirs = self.inspection.results.exclude(pk=mine.pk).first()

        self.client.post(reverse("result_remove", args=[self.inspection.pk, mine.pk]))
        self.assertFalse(InspectionResult.objects.filter(pk=mine.pk).exists())

        # Dropping a template point would quietly change what the inspection
        # claims to have covered. "Not applicable" is the answer there.
        self.client.post(reverse("result_remove", args=[self.inspection.pk, theirs.pk]))
        self.assertTrue(InspectionResult.objects.filter(pk=theirs.pk).exists())

    def test_an_added_check_converts_to_work_like_any_other(self):
        from .models import ResultStatus
        from .services import convert_to_work_order, record

        result = add_check(self.inspection, name="Transfer case fluid", area=Area.UNDER_VEHICLE)
        record(result, status=ResultStatus.FAIL, note="Milky")
        _work_order, items = convert_to_work_order(self.inspection, user=self.user)

        self.assertTrue(any("Transfer case fluid" in item.title for item in items))
