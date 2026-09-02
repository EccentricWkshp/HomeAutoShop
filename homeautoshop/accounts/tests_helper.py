"""The `helper` role and per-vehicle access (SPEC §12.2a, R-2).

A helper is somebody you let work on one vehicle. They see that vehicle and
everything about it, they record what they did, and they see nothing else.

**The thing this file is really guarding is the shape of the enforcement.**
§12.2 promised that routing decisions through `can()` would make this role
"policy rules, not an audit of every view". It did not hold: of 225 view
functions, 48 called `require()` and only 19 named a resource, all in one app.
The apps where a helper actually works had none. A boundary maintained by 225
people remembering is not a boundary.

So the outer fence is a request gate over an allow-list of URL names, and the
test that matters most here is `test_every_url_is_classified` — it fails when
somebody adds a screen without deciding whether a helper may see it, which is
the failure mode that produced this situation in the first place.
"""

from __future__ import annotations

from django.test import TestCase
from django.urls import get_resolver, reverse

from homeautoshop.accounts.models import AssetAccess, Role, User, can
from homeautoshop.accounts.policy import HELPER_URLS, asset_of, visible_assets
from homeautoshop.assets.models import Asset, AssetSpec, SpecGroup
from homeautoshop.parts.models import Part
from homeautoshop.people.models import Person
from homeautoshop.work.models import JobItem, WorkOrder


#: Every route a helper may not reach. Written down rather than derived so
#: that adding a screen is a decision — see `test_every_url_is_classified`.
#: The gate itself needs no such list: anything absent from `HELPER_URLS` is
#: already refused, and this is the record that keeps the refusal visible.
CLOSED_TO_HELPERS = frozenset({
    "asset_costs", "asset_create", "asset_delete", "asset_edit",
    "asset_report", "asset_report_csv", "asset_report_pdf", "backup_delete", "backup_download", "backup_now",
    "backups", "core_list", "core_update", "crossref_add",
    "crossref_remove", "data_import", "dismiss_product_link",
    "expense_delete", "expense_edit", "export_csv", "fitment_add",
    "fitment_delete", "fitment_edit", "health", "inspection_delete",
    "integration_sync", "integration_test", "integrations", "inventory",
    "job_item_tool_add", "job_item_tool_remove", "kit_item_add",
    "kit_item_remove", "labels", "location_create", "location_delete",
    "location_edit", "lot_add", "lot_close_kit", "lot_count", "lot_delete",
    "lot_edit", "lot_open_kit", "lubelogger_import", "lubelogger_link",
    "order_import", "ownership_add", "ownership_end", "part_create",
    "part_delete", "part_edit", "person_create", "person_delete",
    "person_detail", "person_edit", "person_list", "plate_lookup",
    "profile_delete", "profile_export", "profile_import", "profile_list",
    "profile_toggle",
    "purchase_create", "purchase_delete", "purchase_detail",
    "purchase_edit", "purchase_line_add", "purchase_line_delete",
    "purchase_line_edit", "purchase_line_receive",
    "purchase_line_unreceive", "purchase_list", "purchase_receipt_upload",
    "recall_check", "reminder_channel_action", "reminder_channel_add",
    "reminders", "reports", "service_info_pin", "service_info_unpin",
    "service_info_visibility", "settings", "settings_restart", "setup",
    "code_define",
    "spec_add", "spec_copy", "spec_delete", "spec_edit", "spec_pin",
    "spec_from_decode", "spec_from_scan", "sync_queue", "tool_delete",
    "template_list", "template_import", "template_export",
    "checklist_import", "checklist_export",
    "template_delete", "checklist_delete",
    "restore_builtins",
    "catalog_browse", "catalog_install", "codelist_delete", "codelist_import",
    "tool_list", "tool_search", "trash", "trash_restore", "user_access",
    "user_create", "user_delete", "user_detail", "user_list", "user_set_active",
    "user_set_password", "vendor_create", "vendor_delete", "vendor_edit",
    "vendor_list", "vin_decode", "vin_read", "vin_validate",
    "work_order_delete", "work_order_expense_add",
    "work_order_order_shortfall",
})


class Base(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user(
            username="andy", password="x" * 16, role=Role.ADMIN
        )
        self.helper = User.objects.create_user(
            username="sam", password="x" * 16, role=Role.HELPER
        )
        self.mine = Asset.objects.create(nickname="Aero")
        self.theirs = Asset.objects.create(nickname="Barn find")
        self.client.force_login(self.helper)

    def grant(self, asset=None, level="write"):
        return AssetAccess.objects.create(
            user=self.helper, asset=asset or self.mine, level=level
        )


class TheGateTests(Base):
    """The outer fence: which screens exist for a helper at all."""

    def test_every_url_is_classified(self):
        """The test that would have prevented this whole situation.

        Every named route is either opened to helpers or written down here as
        closed. A route in neither set fails this, so adding a screen forces
        somebody to decide whether a helper may see it — which is precisely
        the decision that never got made for the 206 views the original
        scaffold left unguarded.

        Closed is the default and the safe direction: a new screen is already
        refused by the gate before anybody runs the suite. This test is not
        what protects the data; it is what stops the list drifting out of
        anybody's view, the way `can()` quietly did.
        """
        names = {
            pattern.name
            for pattern in get_resolver().url_patterns
            if getattr(pattern, "name", None)
        }

        self.assertEqual(
            names - HELPER_URLS - CLOSED_TO_HELPERS,
            set(),
            "a new screen: decide whether a helper may see it, then add its "
            "name to HELPER_URLS in policy.py or to CLOSED_TO_HELPERS here",
        )
        self.assertEqual(
            HELPER_URLS - names - {"login", "logout"},
            set(),
            "the helper allow-list names routes that no longer exist",
        )

    def test_a_screen_a_helper_has_no_business_on_is_refused(self):
        for name in ("inventory", "purchase_list", "vendor_list", "person_list",
                     "reports", "user_list", "settings", "trash", "labels"):
            with self.subTest(url=name):
                self.assertEqual(self.client.get(reverse(name)).status_code, 403)

    def test_the_money_screens_are_refused_even_for_their_own_vehicle(self):
        """A grant is permission to work on a truck, not to see what the truck
        has cost its owner."""
        self.grant()

        for name in ("asset_costs", "asset_report", "asset_report_pdf", "asset_report_csv"):
            with self.subTest(url=name):
                response = self.client.get(reverse(name, args=[self.mine.pk]))
                self.assertEqual(response.status_code, 403)

    def test_a_member_is_untouched_by_any_of_it(self):
        member = User.objects.create_user(
            username="pat", password="x" * 16, role=Role.MEMBER
        )
        self.client.force_login(member)

        self.assertEqual(self.client.get(reverse("inventory")).status_code, 200)

    def test_the_catalog_is_readable_and_not_writable(self):
        """A helper says which filter they fitted. That is not the same as
        editing the shop's parts list."""
        part = Part.objects.create(name="Oil filter")

        self.assertEqual(
            self.client.get(reverse("part_detail", args=[part.pk])).status_code, 200
        )
        self.assertEqual(
            self.client.post(reverse("part_detail", args=[part.pk])).status_code, 403
        )


class TheVehicleTests(Base):
    """The inner fence: which vehicle, once the screen is allowed."""

    def test_a_granted_vehicle_opens(self):
        self.grant()
        self.assertEqual(
            self.client.get(reverse("asset_detail", args=[self.mine.pk])).status_code,
            200,
        )

    def test_one_they_were_not_given_does_not(self):
        """Without this the gate would be worthless: every vehicle screen is
        on the allow-list, so an id in the URL is all it would take."""
        self.grant()
        self.assertEqual(
            self.client.get(reverse("asset_detail", args=[self.theirs.pk])).status_code,
            403,
        )

    def test_a_read_grant_does_not_let_them_write(self):
        self.grant(level="read")
        order = WorkOrder.objects.create(asset=self.mine, title="Brakes")

        self.assertFalse(can(self.helper, "work.edit", order))
        self.assertTrue(can(self.helper, "work.read", order))

    def test_permission_follows_the_relation_to_the_vehicle(self):
        """A job item's permission is really its work order's vehicle's, and a
        view holding a job item should not have to know that."""
        order = WorkOrder.objects.create(asset=self.mine, title="Brakes")
        item = JobItem.objects.create(work_order=order, title="Pads")

        self.assertEqual(asset_of(item), self.mine)

    def test_a_work_order_on_a_vehicle_they_lack_is_refused(self):
        self.grant()
        order = WorkOrder.objects.create(asset=self.theirs, title="Not yours")

        self.assertEqual(
            self.client.get(reverse("work_order_detail", args=[order.pk])).status_code,
            403,
        )


class WhatTheySeeTests(Base):
    def test_a_listing_shows_only_their_vehicles(self):
        self.grant()

        page = self.client.get(reverse("asset_list")).content.decode()

        self.assertIn("Aero", page)
        self.assertNotIn("Barn find", page)

    def test_the_dashboard_does_not_name_a_vehicle_they_lack(self):
        self.grant()
        WorkOrder.objects.create(asset=self.theirs, title="Secret job")

        page = self.client.get(reverse("dashboard")).content.decode()

        self.assertNotIn("Barn find", page)
        self.assertNotIn("Secret job", page)

    def test_search_is_not_the_back_door(self):
        """A helper barred from a vehicle's page who could still find its work
        orders by typing its name has not been barred from anything."""
        self.grant()
        WorkOrder.objects.create(asset=self.theirs, title="Barn find gearbox")

        page = self.client.get(reverse("search"), {"q": "Barn"}).content.decode()

        self.assertNotIn("Barn find gearbox", page)

    def test_the_address_book_is_not_searchable_either(self):
        self.grant()
        Person.objects.create(display_name="Barnaby Smith")

        page = self.client.get(reverse("search"), {"q": "Barn"}).content.decode()

        self.assertNotIn("Barnaby", page)

    def test_a_helper_is_not_shown_the_key_code(self):
        """Somebody let into the garage is not let into the glovebox. The same
        `is_sensitive` line a vehicle report already holds (C-5)."""
        self.grant()
        AssetSpec.objects.create(
            asset=self.mine, group=SpecGroup.ACCESS, name="Key code", value="X4192"
        )

        page = self.client.get(reverse("asset_specs", args=[self.mine.pk]))

        self.assertNotContains(page, "X4192")

    def test_but_the_ordinary_specs_are_still_there(self):
        self.grant()
        AssetSpec.objects.create(
            asset=self.mine, group="fluids", name="Oil capacity", value="4.7 qt"
        )

        self.assertContains(
            self.client.get(reverse("asset_specs", args=[self.mine.pk])), "4.7 qt"
        )

    def test_visible_assets_is_everything_for_everybody_else(self):
        self.assertEqual(visible_assets(self.admin).count(), 2)
        self.assertEqual(visible_assets(self.helper).count(), 0)


class GrantingTests(Base):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.admin)
        self.page = reverse("user_detail", args=[self.helper.pk])

    def test_an_admin_gives_a_vehicle(self):
        self.client.post(
            reverse("user_access", args=[self.helper.pk]),
            {"asset": str(self.mine.pk), "level": "write"},
        )

        grant = AssetAccess.objects.get(user=self.helper)
        self.assertEqual(grant.asset, self.mine)
        self.assertEqual(grant.level, "write")

    def test_and_takes_it_away_again(self):
        self.grant()

        self.client.post(
            reverse("user_access", args=[self.helper.pk]),
            {"asset": str(self.mine.pk), "revoke": "1"},
        )

        self.assertFalse(AssetAccess.objects.exists())

    def test_granting_the_same_one_twice_changes_the_level(self):
        """Rather than failing on the unique constraint, which would read as a
        bug to somebody who just wanted to upgrade a helper to write."""
        self.grant(level="read")

        self.client.post(
            reverse("user_access", args=[self.helper.pk]),
            {"asset": str(self.mine.pk), "level": "write"},
        )

        self.assertEqual(AssetAccess.objects.get().level, "write")

    def test_the_page_lists_them(self):
        self.grant()
        self.assertContains(self.client.get(self.page), "Aero")

    def test_grants_stay_visible_when_the_role_is_not_helper(self):
        """A role changed away from helper leaves its rows behind. Hiding them
        would make changing it back a surprise."""
        self.grant()
        self.helper.role = Role.MEMBER
        self.helper.save()

        page = self.client.get(self.page)

        self.assertContains(page, "Aero")
        self.assertContains(page, "not a helper")

    def test_a_helper_cannot_grant_themselves_anything(self):
        self.client.force_login(self.helper)

        response = self.client.post(
            reverse("user_access", args=[self.helper.pk]),
            {"asset": str(self.mine.pk), "level": "write"},
        )

        self.assertEqual(response.status_code, 403)
        self.assertFalse(AssetAccess.objects.exists())

    def test_choosing_nothing_says_so(self):
        response = self.client.post(
            reverse("user_access", args=[self.helper.pk]), {}, follow=True
        )
        self.assertContains(response, "Choose a vehicle first")
