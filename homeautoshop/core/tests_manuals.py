"""The shop's own manual libraries (SPEC §8.5).

Three ship — LEMON, CHARM, ALLDATA — because those were the three the author
knew. Until the Manual libraries page, adding a fourth meant the Django admin,
so the shipped three quietly defined what a "manual library" could be.
"""

from __future__ import annotations

from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse

from homeautoshop.accounts.models import Role, User
from homeautoshop.assets.models import Asset, AssetServiceInfoLink, ServiceInfoProvider


class ManualLibrariesBase(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user(
            username="boss", password="x" * 16, role=Role.ADMIN
        )
        self.client.force_login(self.admin)
        self.asset = Asset.objects.create(nickname="Truck", make="Ford", year=2007)

    def add(self, follow=False, **data):
        return self.client.post(
            reverse("manual_libraries"),
            {
                "name": "Workshop Manuals Online",
                "address": "https://manuals.example.test/",
                "deep_link_depth": "make_year",
                "access": "free",
                **data,
            },
            follow=follow,
        )


class AddingALibraryTests(ManualLibrariesBase):
    def test_a_name_and_an_address_make_a_library(self):
        self.add()

        library = ServiceInfoProvider.objects.get(name="Workshop Manuals Online")
        self.assertEqual(library.slug, "workshop-manuals-online")
        self.assertEqual(library.base_urls, ["https://manuals.example.test"])
        self.assertTrue(library.is_enabled)

    def test_the_browse_link_is_as_deep_as_the_site_is_regular(self):
        """The depth is asked for; the template it needs is implied, because
        nobody adding a library should have to know what `{make}` means."""
        self.add()

        library = ServiceInfoProvider.objects.get(name="Workshop Manuals Online")
        self.assertEqual(library.url_template, "{make}/{year}/")
        self.assertEqual(library.browse_url(self.asset), "https://manuals.example.test/Ford/2007/")

    def test_a_root_only_library_offers_its_front_page(self):
        self.add(deep_link_depth="root")

        library = ServiceInfoProvider.objects.get(name="Workshop Manuals Online")
        self.assertEqual(library.url_template, "")
        self.assertEqual(library.browse_url(self.asset), "https://manuals.example.test")

    def test_every_vehicle_offers_it(self):
        self.add()

        page = self.client.get(reverse("asset_detail", args=[self.asset.pk]))

        self.assertContains(page, "Workshop Manuals Online")
        self.assertContains(page, "https://manuals.example.test/Ford/2007/")

    def test_two_of_the_same_name_are_refused(self):
        self.add()
        response = self.add(name="workshop manuals online", follow=True)

        self.assertEqual(ServiceInfoProvider.objects.count(), 1)
        self.assertContains(response, "already a library called")

    def test_an_address_that_is_not_a_web_address_is_refused(self):
        response = self.add(address="manuals on the NAS", follow=True)

        self.assertFalse(ServiceInfoProvider.objects.exists())
        self.assertContains(response, "not a web address")

    def test_slugs_stay_unique_across_the_trash(self):
        self.add()
        ServiceInfoProvider.objects.get(name="Workshop Manuals Online").delete()
        self.add(name="Workshop Manuals Online 2")

        # Different name, so allowed; the slug is truncated and disambiguated
        # against the trashed row rather than colliding with it.
        self.assertEqual(ServiceInfoProvider.all_objects.count(), 2)
        self.assertEqual(
            len({row.slug for row in ServiceInfoProvider.all_objects.all()}), 2
        )


class SwitchingAndRemovingTests(ManualLibrariesBase):
    def setUp(self):
        super().setUp()
        self.add()
        self.library = ServiceInfoProvider.objects.get(name="Workshop Manuals Online")
        self.pin = AssetServiceInfoLink.objects.create(
            asset=self.asset,
            provider=self.library,
            url="https://manuals.example.test/vehicles/ford-f150-2007/",
            label="F-150",
        )

    def act(self, action):
        return self.client.post(
            reverse("manual_library_action", args=[self.library.pk]), {"action": action}
        )

    def test_off_hides_it_from_every_vehicle_and_keeps_the_pins(self):
        self.act("toggle")

        page = self.client.get(reverse("asset_detail", args=[self.asset.pk]))
        self.assertNotContains(page, "Workshop Manuals Online")
        self.assertTrue(AssetServiceInfoLink.objects.filter(pk=self.pin.pk).exists())

        self.act("toggle")
        self.assertContains(
            self.client.get(reverse("asset_detail", args=[self.asset.pk])), "F-150"
        )

    def test_removal_is_soft_and_takes_the_pins_with_it(self):
        self.act("delete")

        self.assertTrue(ServiceInfoProvider.all_objects.get(pk=self.library.pk).is_deleted)
        self.assertTrue(AssetServiceInfoLink.all_objects.get(pk=self.pin.pk).is_deleted)
        self.assertContains(self.client.get(reverse("trash")), "Workshop Manuals Online")

    def test_restoring_it_brings_the_pins_back(self):
        self.act("delete")

        self.client.post(reverse("trash_restore", args=["service_info_provider", self.library.pk]))

        self.assertFalse(AssetServiceInfoLink.all_objects.get(pk=self.pin.pk).is_deleted)
        self.assertContains(
            self.client.get(reverse("asset_detail", args=[self.asset.pk])), "F-150"
        )

    def test_the_seed_survives_a_shipped_library_in_the_trash(self):
        """`slug` is unique across the table. Re-seeding used to look only at
        the live rows, not find the trashed one, try to insert it again and
        fail the whole seed on the one thing the operator did on purpose."""
        call_command("seed", verbosity=0)
        ServiceInfoProvider.objects.get(slug="charm").delete()

        call_command("seed", verbosity=0)

        self.assertTrue(ServiceInfoProvider.all_objects.get(slug="charm").is_deleted)

    def test_it_is_written_down_who_removed_it(self):
        from homeautoshop.core.models import AuditLog

        self.act("delete")

        entry = AuditLog.objects.get(entity_id=self.library.pk, action=AuditLog.Action.DELETE)
        self.assertEqual(entry.user, self.admin)


class WhoMayManageLibrariesTests(ManualLibrariesBase):
    def test_a_member_is_refused(self):
        member = User.objects.create_user(username="pat", password="x" * 16)
        self.client.force_login(member)

        self.assertEqual(self.client.get(reverse("manual_libraries")).status_code, 403)
        self.assertEqual(self.add().status_code, 403)

    def test_the_page_is_reachable_from_settings(self):
        page = self.client.get(reverse("settings"))
        self.assertContains(page, reverse("manual_libraries"))
