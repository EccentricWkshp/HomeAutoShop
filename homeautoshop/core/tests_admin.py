"""Every model is reachable in the admin, and every page it offers loads.

Two separate claims, and the second is the one a static check cannot make.
`manage.py check` validates `list_display` and friends against the model, but
`SoftDeleteAdmin` builds its columns and its queryset at request time — so the
only way to know the trash is really visible is to ask for the page.
"""

from __future__ import annotations

from django.apps import apps
from django.contrib import admin
from django.test import TestCase
from django.urls import reverse

from homeautoshop.accounts.models import Role, User

#: Apps whose tables are Django's own plumbing rather than this shop's data.
PLUMBING = {"admin", "auth", "contenttypes", "sessions"}


class EveryModelIsRegisteredTests(TestCase):
    """The gap that made a bad import impossible to clear up.

    A purchase line, a provenance row and a media link were all unreachable
    through the admin, so leftovers from a half-deleted order could be neither
    found nor removed without a database shell.
    """

    def test_no_model_is_missing_from_the_admin(self):
        registered = set(admin.site._registry)
        missing = sorted(
            model._meta.label
            for model in apps.get_models()
            if model._meta.app_label not in PLUMBING and model not in registered
        )

        self.assertEqual(
            missing,
            [],
            "these have no admin page, so nothing in them can be inspected or "
            "cleaned up: register them",
        )


class TheAdminSiteIsTheOneEverythingRegisteredOnTests(TestCase):
    """A trap worth a test of its own, because it fails silently.

    `admin.site` is a lazy proxy that resolves by importing whatever
    `default_site` names. If that module also runs `@admin.register`, each
    decorator asks for `admin.site` while the proxy is still mid-setup, the
    re-entrant call builds a *second* site, and the registrations land on the
    throwaway — so seven models quietly had no admin page while every check
    passed. The site class lives in a module that registers nothing.
    """

    def test_the_site_class_is_the_shops_own(self):
        from homeautoshop.core.adminsite import ShopAdminSite

        self.assertIsInstance(admin.site, ShopAdminSite)

    def test_the_module_that_defines_it_registers_nothing(self):
        import homeautoshop.core.adminsite as module

        self.assertNotIn("register", dir(module))
        self.assertFalse(
            [name for name in dir(module) if name.endswith("Admin")],
            "defining a ModelAdmin here risks re-entering admin.site during setup",
        )

    def test_the_core_models_are_registered_on_it(self):
        """The seven that went missing when they were not."""
        from homeautoshop.core.models import (
            AuditLog, Credential, ExternalRef, Job, NotificationChannel,
            NotificationSent, Setting,
        )

        for model in (
            AuditLog, Credential, ExternalRef, Job, NotificationChannel,
            NotificationSent, Setting,
        ):
            with self.subTest(model=model.__name__):
                self.assertIn(model, admin.site._registry)


class EveryAdminPageLoadsTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser(
            "root", password="correct-horse-battery", role=Role.ADMIN
        )
        self.client.force_login(self.admin)

    def test_every_changelist_renders(self):
        for model, model_admin in admin.site._registry.items():
            meta = model._meta
            with self.subTest(model=meta.label):
                url = reverse(f"admin:{meta.app_label}_{meta.model_name}_changelist")
                self.assertEqual(self.client.get(url).status_code, 200)

    def test_a_trashed_row_is_visible_and_marked(self):
        """The point of reading `all_objects` here. Everywhere else in the
        application a soft-deleted row is hidden, which is right — the admin is
        the one place somebody has come to deal with it."""
        from homeautoshop.assets.models import Asset

        asset = Asset.objects.create(nickname="Parts car")
        asset.delete()

        url = reverse("admin:assets_asset_changelist")
        self.assertNotContains(self.client.get(url), "Parts car")
        self.assertContains(self.client.get(url, {"trashed": "1"}), "Parts car")
        self.assertContains(self.client.get(url, {"trashed": "all"}), "Parts car")

    def test_the_trash_can_be_emptied_from_the_admin(self):
        from homeautoshop.assets.models import Asset

        asset = Asset.objects.create(nickname="Parts car")
        asset.delete()

        self.client.post(
            # The filter lives in the query string, which is where the
            # changelist reads it from — the admin's own action form posts back
            # to the filtered URL for exactly this reason.
            reverse("admin:assets_asset_changelist") + "?trashed=1",
            {
                "action": "hard_delete_selected",
                "_selected_action": [str(asset.pk)],
                "index": "0",
            },
            follow=True,
        )

        self.assertFalse(Asset.all_objects.filter(pk=asset.pk).exists())

    def test_and_a_row_can_be_taken_back_out_of_it(self):
        from homeautoshop.assets.models import Asset

        asset = Asset.objects.create(nickname="Parts car")
        asset.delete()

        self.client.post(
            # The filter lives in the query string, which is where the
            # changelist reads it from — the admin's own action form posts back
            # to the filtered URL for exactly this reason.
            reverse("admin:assets_asset_changelist") + "?trashed=1",
            {
                "action": "restore_selected",
                "_selected_action": [str(asset.pk)],
                "index": "0",
            },
            follow=True,
        )

        self.assertTrue(Asset.objects.filter(pk=asset.pk).exists())

    def test_deleting_a_purchase_from_the_admin_takes_its_lines(self):
        """The exact path that produced the orphans: `delete_selected` goes
        through the queryset, which had no cascade at all."""
        from homeautoshop.purchasing.models import Purchase, PurchaseLine, Vendor

        vendor = Vendor.objects.create(name="RockAuto")
        purchase = Purchase.objects.create(vendor=vendor, order_number="99")
        PurchaseLine.objects.create(
            purchase=purchase, description_as_ordered="Gasket", extended_minor=400
        )

        self.client.post(
            reverse("admin:purchasing_purchase_changelist"),
            {
                "action": "delete_selected",
                "_selected_action": [str(purchase.pk)],
                "post": "yes",
            },
            follow=True,
        )

        self.assertFalse(Purchase.objects.filter(pk=purchase.pk).exists())
        self.assertFalse(PurchaseLine.objects.filter(purchase_id=purchase.pk).exists())

    def test_there_is_one_page_that_says_what_is_in_the_trash(self):
        """The question a per-model filter cannot answer. "Is this row deleted?"
        is the wrong one when clearing up — nobody knows which of fifty
        changelists to open."""
        from homeautoshop.assets.models import Asset

        asset = Asset.objects.create(nickname="Parts car")
        asset.delete()

        response = self.client.get(reverse("admin:trash_overview"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Assets")
        self.assertContains(response, reverse("admin:assets_asset_changelist") + "?trashed=1")

    def test_it_counts_what_is_past_the_retention_window_separately(self):
        from datetime import timedelta

        from django.utils import timezone

        from homeautoshop.assets.models import Asset

        old = Asset.objects.create(nickname="Old")
        old.delete()
        Asset.all_objects.filter(pk=old.pk).update(
            deleted_at=timezone.now() - timedelta(days=400)
        )

        response = self.client.get(reverse("admin:trash_overview"))

        row = next(r for r in response.context["rows"] if r["label"] == "Assets")
        self.assertEqual(row["count"], 1)
        self.assertEqual(row["expired"], 1)

    def test_an_empty_trash_says_so_rather_than_showing_a_bare_table(self):
        response = self.client.get(reverse("admin:trash_overview"))

        self.assertContains(response, "Nothing is in the trash")

    def test_the_admin_index_links_to_it(self):
        """A page nobody can find is the same failure as a soft delete listed
        nowhere."""
        response = self.client.get(reverse("admin:index"))

        self.assertContains(response, reverse("admin:trash_overview"))

    def test_a_table_with_nothing_deleted_is_not_listed(self):
        from homeautoshop.assets.models import Asset

        Asset.objects.create(nickname="Daily driver")

        response = self.client.get(reverse("admin:trash_overview"))

        self.assertEqual(response.context["rows"], [])

    def test_the_credential_secret_is_never_rendered(self):
        """§17.1: a credential can be replaced or cleared, never displayed
        back. Giving it an admin page must not have quietly undone that."""
        from homeautoshop.core.models import Credential

        Credential.objects.create(key="vin_api", ciphertext="s3cr3t-ciphertext")

        response = self.client.get(reverse("admin:core_credential_changelist"))

        self.assertContains(response, "vin_api")
        self.assertNotContains(response, "s3cr3t-ciphertext")
