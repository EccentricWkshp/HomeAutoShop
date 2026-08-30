"""Work order lifecycle and workbench (SPEC §7.3, REFERENCE.md §1)."""

from __future__ import annotations

from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse

from homeautoshop.accounts.models import User
from homeautoshop.assets.models import Asset, UsageReading

from .models import JobItem, WorkOrder, WorkOrderNote, WorkOrderStatus


class NumberingTests(TestCase):
    def setUp(self):
        self.asset = Asset.objects.create(nickname="Truck")

    def test_numbers_are_sequential_within_a_year(self):
        first = WorkOrder.objects.create(asset=self.asset, title="Brakes")
        second = WorkOrder.objects.create(asset=self.asset, title="Oil")
        self.assertTrue(first.number.startswith("WO-"))
        self.assertEqual(int(second.number[-4:]) - int(first.number[-4:]), 1)

    def test_a_soft_deleted_work_order_does_not_free_its_number(self):
        first = WorkOrder.objects.create(asset=self.asset, title="Brakes")
        first.delete()
        second = WorkOrder.objects.create(asset=self.asset, title="Oil")
        self.assertNotEqual(first.number, second.number)


class LifecycleTests(TestCase):
    def setUp(self):
        self.asset = Asset.objects.create(nickname="Truck")
        self.wo = WorkOrder.objects.create(asset=self.asset, title="Front brakes")

    def test_happy_path(self):
        self.wo.transition_to(WorkOrderStatus.IN_PROGRESS)
        self.assertIsNotNone(self.wo.started_at)
        self.wo.transition_to(WorkOrderStatus.COMPLETE, odometer_out=120_000)
        self.assertIsNotNone(self.wo.completed_at)

    def test_illegal_transition_is_refused(self):
        # planned -> complete skips the work actually happening.
        with self.assertRaises(ValidationError):
            self.wo.transition_to(WorkOrderStatus.COMPLETE, odometer_out=1)

    def test_completing_requires_the_meter_reading(self):
        """FR-WO-9 — the one number that makes the record useful later."""
        self.wo.transition_to(WorkOrderStatus.IN_PROGRESS)
        with self.assertRaises(ValidationError):
            self.wo.transition_to(WorkOrderStatus.COMPLETE)

    def test_meterless_asset_completes_without_a_reading(self):
        trailer = Asset.objects.create(nickname="Trailer", meter="none")
        wo = WorkOrder.objects.create(asset=trailer, title="Bearings")
        wo.transition_to(WorkOrderStatus.IN_PROGRESS)
        wo.transition_to(WorkOrderStatus.COMPLETE)
        self.assertEqual(wo.status, WorkOrderStatus.COMPLETE)

    def test_blocking_requires_a_reason_so_the_dashboard_can_explain_itself(self):
        self.wo.transition_to(WorkOrderStatus.IN_PROGRESS)
        with self.assertRaises(ValidationError):
            self.wo.transition_to(WorkOrderStatus.WAITING_ON_PARTS)
        self.wo.blocked_reason = "Caliper on order"
        self.wo.transition_to(WorkOrderStatus.WAITING_ON_PARTS)
        self.assertEqual(self.wo.status, WorkOrderStatus.WAITING_ON_PARTS)

    def test_abandoned_is_a_first_class_outcome(self):
        # Home shop projects genuinely get abandoned; recording that honestly
        # beats an eternally open work order.
        self.wo.transition_to(WorkOrderStatus.ABANDONED)
        self.assertEqual(self.wo.status, WorkOrderStatus.ABANDONED)
        self.assertNotIn(self.wo, WorkOrder.objects.open())

    def test_reopening_clears_completion_without_rewriting_history(self):
        self.wo.transition_to(WorkOrderStatus.IN_PROGRESS)
        self.wo.transition_to(WorkOrderStatus.COMPLETE, odometer_out=1000)
        self.wo.transition_to(WorkOrderStatus.IN_PROGRESS)
        self.assertIsNone(self.wo.completed_at)
        self.assertIsNotNone(self.wo.started_at)


class JobItemTests(TestCase):
    def setUp(self):
        self.asset = Asset.objects.create(nickname="Truck")
        self.wo = WorkOrder.objects.create(asset=self.asset, title="Saturday")

    def test_items_complete_independently(self):
        JobItem.objects.create(work_order=self.wo, title="Oil change")
        pads = JobItem.objects.create(work_order=self.wo, title="Brake pads")
        JobItem.objects.create(work_order=self.wo, title="Chase rattle")
        pads.status = JobItem.Status.DONE
        pads.save()
        self.assertEqual(self.wo.job_item_progress, (1, 3))
        self.assertIsNotNone(pads.completed_at)


class NoteTests(TestCase):
    def test_notes_are_append_only(self):
        asset = Asset.objects.create(nickname="Truck")
        wo = WorkOrder.objects.create(asset=asset, title="Brakes")
        note = WorkOrderNote.objects.create(work_order=wo, body="Slide pins seized")
        note.body = "Rewritten"
        with self.assertRaises(ValidationError):
            note.save()


class WorkOrderViewTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("andy", password="correct-horse-battery")
        self.client.force_login(self.user)
        self.asset = Asset.objects.create(nickname="Truck")
        self.wo = WorkOrder.objects.create(asset=self.asset, title="Front brakes")

    def test_detail_renders(self):
        response = self.client.get(reverse("work_order_detail", args=[self.wo.pk]))
        self.assertContains(response, "Front brakes")
        self.assertContains(response, self.wo.number)

    def test_note_and_item_capture(self):
        self.client.post(reverse("note_create", args=[self.wo.pk]), {"body": "Pads at 2mm"})
        self.client.post(reverse("job_item_create", args=[self.wo.pk]), {"title": "Replace pads"})
        self.assertEqual(self.wo.notes.count(), 1)
        self.assertEqual(self.wo.job_items.count(), 1)

    def test_completion_records_a_reading_on_the_asset(self):
        """Completing captures the meter into history without double entry."""
        self.client.post(
            reverse("work_order_transition", args=[self.wo.pk]), {"status": "in_progress"}
        )
        self.client.post(
            reverse("work_order_transition", args=[self.wo.pk]),
            {"status": "complete", "odometer_out": "142000"},
        )
        self.wo.refresh_from_db()
        self.assertEqual(self.wo.status, WorkOrderStatus.COMPLETE)
        reading = UsageReading.objects.filter(asset=self.asset).first()
        self.assertIsNotNone(reading)
        self.assertEqual(float(reading.value), 142000.0)
        self.assertEqual(reading.source, "work_order")

    def test_illegal_transition_surfaces_as_a_message_not_a_crash(self):
        response = self.client.post(
            reverse("work_order_transition", args=[self.wo.pk]),
            {"status": "complete", "odometer_out": "1"},
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        self.wo.refresh_from_db()
        self.assertEqual(self.wo.status, WorkOrderStatus.PLANNED)
