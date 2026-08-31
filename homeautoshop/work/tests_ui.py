"""
Work orders: what the screen lets you do (SPEC FR-WO-*, REFERENCE.md §1).

Four things reported from actual use, each of which the model was technically
right about and the person in front of it was not wrong to expect:

* A work order started by mistake could not be un-started.
* The parent picker was empty and said nothing about why.
* The status form refused a transition for a field it had never marked needed.
* There was no way to delete a work order at all.

And one that was simply unfinished: the tool box asked for a WrenchLedger id
typed from memory, next to a `list=` pointing at a datalist that was never
rendered.
"""

from __future__ import annotations

import json
from unittest import mock

from django.test import TestCase
from django.urls import reverse

from homeautoshop.accounts.models import Role, User
from homeautoshop.assets.models import Asset
from homeautoshop.work.models import (
    JobItem,
    ShopTool,
    WorkOrder,
    WorkOrderStatus,
    WorkOrderType,
)

VIN = "1M8GDM9AXKP042788"


class Base(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="andy", password="x" * 16, role=Role.ADMIN)
        self.client.force_login(self.user)
        self.asset = Asset.objects.create(nickname="Red truck", vin=VIN)
        self.wo = WorkOrder.objects.create(asset=self.asset, title="Front brakes")

    def move(self, work_order, status, **extra):
        return self.client.post(
            reverse("work_order_transition", args=[work_order.pk]),
            {"status": status, **extra},
            follow=True,
        )


class BackwardsTests(Base):
    """Starting a job by accident is not a rare event in a home shop."""

    def test_a_work_order_started_by_mistake_can_be_un_started(self):
        self.move(self.wo, WorkOrderStatus.IN_PROGRESS)
        self.move(self.wo, WorkOrderStatus.PLANNED)
        self.wo.refresh_from_db()
        self.assertEqual(self.wo.status, WorkOrderStatus.PLANNED)

    def test_the_screen_offers_it_rather_than_only_the_model_allowing_it(self):
        self.move(self.wo, WorkOrderStatus.IN_PROGRESS)
        page = self.client.get(reverse("work_order_detail", args=[self.wo.pk]))
        self.assertIn(
            WorkOrderStatus.PLANNED, [value for value, _label in page.context["next_statuses"]]
        )

    def test_waiting_and_on_hold_can_go_back_too(self):
        for status in (WorkOrderStatus.WAITING_ON_PARTS, WorkOrderStatus.ON_HOLD):
            with self.subTest(status=status):
                work_order = WorkOrder.objects.create(asset=self.asset, title="x")
                self.move(work_order, WorkOrderStatus.IN_PROGRESS)
                self.move(work_order, status, blocked_reason="a caliper")
                self.move(work_order, WorkOrderStatus.PLANNED)
                work_order.refresh_from_db()
                self.assertEqual(work_order.status, WorkOrderStatus.PLANNED)

    def test_going_back_does_not_erase_when_it_first_started(self):
        """History is not rewritten — the same rule reopening already follows."""
        self.move(self.wo, WorkOrderStatus.IN_PROGRESS)
        self.wo.refresh_from_db()
        started = self.wo.started_at
        self.assertIsNotNone(started)
        self.move(self.wo, WorkOrderStatus.PLANNED)
        self.wo.refresh_from_db()
        self.assertEqual(self.wo.started_at, started)

    def test_completing_still_needs_what_it_always_needed(self):
        """The graph got more permissive; the rules on a state did not."""
        self.move(self.wo, WorkOrderStatus.IN_PROGRESS)
        response = self.move(self.wo, WorkOrderStatus.COMPLETE)
        self.wo.refresh_from_db()
        if self.asset.has_meter:
            self.assertContains(response, "meter reading")
            self.assertEqual(self.wo.status, WorkOrderStatus.IN_PROGRESS)


class RequiredFieldTests(Base):
    """Bad UX, said plainly: you found out by pressing the button."""

    def test_the_page_says_which_field_each_status_needs(self):
        # Only for the statuses actually reachable from here — `planned` cannot
        # go straight to waiting, so listing its requirement would be noise.
        self.move(self.wo, WorkOrderStatus.IN_PROGRESS)
        page = self.client.get(reverse("work_order_detail", args=[self.wo.pk]))
        self.assertEqual(
            page.context["status_requirements"].get(WorkOrderStatus.WAITING_ON_PARTS),
            "blocked_reason",
        )

    def test_it_reaches_the_browser_as_data_rather_than_as_guesswork(self):
        self.move(self.wo, WorkOrderStatus.IN_PROGRESS)
        page = self.client.get(reverse("work_order_detail", args=[self.wo.pk])).content.decode()
        self.assertIn('id="status-requirements"', page)
        self.assertIn("blocked_reason", page)

    def test_the_meter_is_not_claimed_to_be_required_on_something_with_no_meter(self):
        """Marking a field required that the server will not ask for is the
        same failure in the other direction."""
        equipment = Asset.objects.create(
            nickname="Bench grinder", asset_kind="equipment", meter="none"
        )
        work_order = WorkOrder.objects.create(asset=equipment, title="Rewire")
        self.move(work_order, WorkOrderStatus.IN_PROGRESS)
        page = self.client.get(reverse("work_order_detail", args=[work_order.pk]))
        if not equipment.has_meter:
            self.assertNotIn(
                WorkOrderStatus.COMPLETE, page.context["status_requirements"]
            )

    def test_the_field_is_labelled_for_what_it_is(self):
        page = self.client.get(reverse("work_order_detail", args=[self.wo.pk]))
        self.assertContains(page, "What it is waiting for")


class ParentTests(Base):
    """An empty dropdown with no explanation reads as broken."""

    def test_with_no_projects_it_says_what_makes_one_available(self):
        page = self.client.get(reverse("work_order_edit", args=[self.wo.pk]))
        self.assertContains(page, "set a work order&#x27;s type to Project")

    def test_a_project_is_offered(self):
        project = WorkOrder.objects.create(
            asset=self.asset, title="Engine swap", type=WorkOrderType.PROJECT
        )
        page = self.client.get(reverse("work_order_edit", args=[self.wo.pk]))
        self.assertIn(project, page.context["form"].fields["parent"].queryset)

    def test_a_work_order_is_not_offered_as_its_own_parent(self):
        project = WorkOrder.objects.create(
            asset=self.asset, title="Engine swap", type=WorkOrderType.PROJECT
        )
        page = self.client.get(reverse("work_order_edit", args=[project.pk]))
        self.assertNotIn(project, page.context["form"].fields["parent"].queryset)

    def test_nor_is_anything_already_underneath_it(self):
        """Either one makes a cycle, and the timeline walks it forever."""
        top = WorkOrder.objects.create(
            asset=self.asset, title="Restoration", type=WorkOrderType.PROJECT
        )
        middle = WorkOrder.objects.create(
            asset=self.asset, title="Bodywork", type=WorkOrderType.PROJECT, parent=top
        )
        bottom = WorkOrder.objects.create(
            asset=self.asset, title="Rear quarter", type=WorkOrderType.PROJECT, parent=middle
        )
        page = self.client.get(reverse("work_order_edit", args=[top.pk]))
        offered = page.context["form"].fields["parent"].queryset
        self.assertNotIn(middle, offered)
        self.assertNotIn(bottom, offered)

    def test_descendants_are_found_however_deep(self):
        top = WorkOrder.objects.create(asset=self.asset, title="a", type=WorkOrderType.PROJECT)
        mid = WorkOrder.objects.create(asset=self.asset, title="b", parent=top)
        low = WorkOrder.objects.create(asset=self.asset, title="c", parent=mid)
        self.assertEqual(top.descendant_ids(), {mid.pk, low.pk})


class DeleteTests(Base):
    """The ones most worth deleting are the ones made while learning."""

    def test_it_can_be_deleted_from_any_status(self):
        for status in (
            WorkOrderStatus.PLANNED,
            WorkOrderStatus.IN_PROGRESS,
            WorkOrderStatus.ON_HOLD,
        ):
            with self.subTest(status=status):
                work_order = WorkOrder.objects.create(asset=self.asset, title="scratch")
                WorkOrder.objects.filter(pk=work_order.pk).update(status=status)
                self.client.post(reverse("work_order_delete", args=[work_order.pk]))
                self.assertFalse(WorkOrder.objects.filter(pk=work_order.pk).exists())

    def test_it_goes_to_the_trash_rather_than_being_destroyed(self):
        """P-5. A mis-click has to cost nothing."""
        self.client.post(reverse("work_order_delete", args=[self.wo.pk]))
        self.assertTrue(WorkOrder.all_objects.filter(pk=self.wo.pk).exists())
        self.assertIsNotNone(WorkOrder.all_objects.get(pk=self.wo.pk).deleted_at)

    def test_deleting_a_parent_with_children_is_refused_and_says_why(self):
        """Otherwise the children point at a row nobody can see or restore."""
        project = WorkOrder.objects.create(
            asset=self.asset, title="Engine swap", type=WorkOrderType.PROJECT
        )
        WorkOrder.objects.create(asset=self.asset, title="Head gasket", parent=project)
        response = self.client.post(
            reverse("work_order_delete", args=[project.pk]), follow=True
        )
        self.assertContains(response, "sit under this one")
        self.assertTrue(WorkOrder.objects.filter(pk=project.pk).exists())

    def test_it_is_recorded(self):
        from homeautoshop.core.models import AuditLog

        self.client.post(reverse("work_order_delete", args=[self.wo.pk]))
        self.assertTrue(
            AuditLog.objects.filter(
                entity_type="WorkOrder", action=AuditLog.Action.DELETE
            ).exists()
        )

    def test_a_get_does_not_delete_anything(self):
        response = self.client.get(reverse("work_order_delete", args=[self.wo.pk]))
        self.assertEqual(response.status_code, 405)
        self.assertTrue(WorkOrder.objects.filter(pk=self.wo.pk).exists())

    def test_the_button_asks_first(self):
        page = self.client.get(reverse("work_order_detail", args=[self.wo.pk]))
        self.assertContains(page, "data-confirm")


class ToolLookupTests(Base):
    """It asked for an id typed from memory, beside a datalist that was never
    rendered — autocompletion promised and not delivered."""

    def setUp(self):
        super().setUp()
        self.item = JobItem.objects.create(work_order=self.wo, title="Pads")
        self.tool = ShopTool.objects.create(
            tool_id="wl_9912", name="Breaker bar", brand="Tekton", model='1/2"'
        )

    def search(self, query):
        response = self.client.get(reverse("tool_search"), {"q": query})
        return json.loads(response.content)

    def test_a_tool_already_here_is_found_by_name(self):
        found = self.search("breaker")
        self.assertEqual([row["id"] for row in found["results"]], ["wl_9912"])
        self.assertEqual(found["results"][0]["name"], "Breaker bar")

    def test_it_works_with_wrenchledger_unreachable(self):
        """The tools somebody reaches for repeatedly are the cached ones."""
        with mock.patch("homeautoshop.work.readiness.enabled", return_value=False):
            self.assertTrue(self.search("breaker")["results"])

    def test_a_one_letter_query_asks_nobody_anything(self):
        self.assertEqual(self.search("b"), {"results": [], "remote": False})

    def test_wrenchledger_results_are_added_when_it_answers(self):
        client = mock.Mock()
        client.search_tools.return_value = [
            {"id": "wl_1", "name": "Torque wrench", "brand": "CDI"},
            # Already known here; must not appear twice.
            {"id": "wl_9912", "name": "Breaker bar"},
        ]
        with (
            mock.patch("homeautoshop.work.readiness.enabled", return_value=True),
            mock.patch(
                "homeautoshop.core.integrations.wrenchledger.WrenchLedgerClient",
                return_value=client,
            ),
        ):
            found = self.search("wrench")
        self.assertEqual(sorted(row["id"] for row in found["results"]), ["wl_1", "wl_9912"])
        self.assertTrue(found["remote"])

    def test_a_failing_remote_still_returns_the_local_half(self):
        with (
            mock.patch("homeautoshop.work.readiness.enabled", return_value=True),
            mock.patch(
                "homeautoshop.core.integrations.wrenchledger.WrenchLedgerClient",
                side_effect=RuntimeError("down"),
            ),
        ):
            found = self.search("breaker")
        self.assertEqual([row["id"] for row in found["results"]], ["wl_9912"])
        self.assertFalse(found["remote"])

    def test_a_name_typed_and_submitted_resolves_without_script(self):
        self.client.post(
            reverse("job_item_tool_add", args=[self.wo.pk, self.item.pk]),
            {"tool_query": "Breaker bar"},
        )
        self.assertTrue(self.item.tools.filter(tool=self.tool).exists())

    def test_an_unknown_name_is_still_recorded(self):
        """WrenchLedger is never load-bearing (FR-WL-7)."""
        self.client.post(
            reverse("job_item_tool_add", args=[self.wo.pk, self.item.pk]),
            {"tool_query": "Bearing splitter"},
        )
        self.assertTrue(
            self.item.tools.filter(tool__name="Bearing splitter").exists()
        )

    def test_an_ambiguous_name_is_a_question_not_a_guess(self):
        ShopTool.objects.create(tool_id="wl_9913", name="Breaker bar")
        response = self.client.post(
            reverse("job_item_tool_add", args=[self.wo.pk, self.item.pk]),
            {"tool_query": "Breaker bar"},
            follow=True,
        )
        self.assertContains(response, "More than one tool")

    def test_the_form_is_a_search_box_pointing_somewhere_real(self):
        """Guarded on WrenchLedger being on, because that is when it renders —
        an unguarded version of this passes by finding nothing."""
        with mock.patch("homeautoshop.work.readiness.enabled", return_value=True):
            page = self.client.get(
                reverse("work_order_detail", args=[self.wo.pk])
            ).content.decode()
        self.assertIn("toolpicker", page)
        self.assertIn(reverse("tool_search"), page)

    def test_the_datalist_that_never_existed_is_no_longer_referenced(self):
        """`list="tool-options"` pointed at nothing for the whole of Phase 4:
        autocompletion promised in the markup and wired to no element."""
        with mock.patch("homeautoshop.work.readiness.enabled", return_value=True):
            page = self.client.get(
                reverse("work_order_detail", args=[self.wo.pk])
            ).content.decode()
        self.assertNotIn('list="tool-options"', page)
