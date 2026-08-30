"""
The LubeLogger import, reachable from the UI (SPEC §8.6).

It worked from the command line and nowhere else, so unless you had read
docs/DEVELOPMENT.md you had no way to know it existed. A migration path nobody
can find is not a migration path.

The gate is the part worth defending. LubeLogger serves locale-formatted
numbers unless its invariant flag is set, and `1.234,56` read as `1.23` is
wrong money that looks entirely plausible. The importer refuses to run when the
check fails; this screen must refuse in exactly the same place, or the UI
becomes the way around the safeguard.
"""

from __future__ import annotations

from unittest.mock import patch

from django.test import TestCase, override_settings
from django.urls import reverse

from homeautoshop.accounts.models import User
from homeautoshop.assets.models import Asset
from homeautoshop.core.integrations.importer import Report, VehicleMatch
from homeautoshop.core.integrations.lubelogger import Diagnosis, NotConfigured
from homeautoshop.work.models import WorkOrder

CONFIGURED = {
    "LUBELOGGER_URL": "https://lubelogger.home.arpa",
    "LUBELOGGER_API_KEY": "secret",
}

HEALTHY = Diagnosis(
    reachable=True, authenticated=True, invariant=True, vehicle_count=3, message=""
)
LOCALE_TROUBLE = Diagnosis(
    reachable=True, authenticated=True, invariant=False, vehicle_count=3,
    message="Saw 1.234,56 in a cost field",
)


class AccessTests(TestCase):
    def setUp(self):
        self.url = reverse("lubelogger_import")

    def test_a_member_cannot_reach_it(self):
        """Wiring another system into this one is an administrator's decision."""
        self.client.force_login(
            User.objects.create_user(username="helper", password="x" * 16, role="member")
        )
        self.assertEqual(self.client.get(self.url).status_code, 403)

    def test_it_is_linked_from_the_admin_menu(self):
        """The reported problem was that nothing pointed at it."""
        admin = User.objects.create_user(username="boss", password="x" * 16, role="admin")
        self.client.force_login(admin)
        self.assertContains(self.client.get(reverse("dashboard")), self.url)


class UnconfiguredTests(TestCase):
    def setUp(self):
        self.client.force_login(
            User.objects.create_user(username="boss", password="x" * 16, role="admin")
        )

    @override_settings(LUBELOGGER_URL="", LUBELOGGER_API_KEY="")
    def test_it_says_what_is_missing_rather_than_hiding(self):
        page = self.client.get(reverse("lubelogger_import"))
        self.assertContains(page, "LUBELOGGER_URL")
        self.assertContains(page, "No instance set")

    @override_settings(LUBELOGGER_URL="https://lubelogger.home.arpa", LUBELOGGER_API_KEY="")
    def test_a_url_with_no_key_still_offers_the_controls(self):
        """A LubeLogger on the LAN often needs no key, and one of them imported
        history from the command line while this screen said it was not
        configured. Whether a key is needed is the server's answer to give."""
        page = self.client.get(reverse("lubelogger_import"))
        self.assertNotContains(page, "No instance set")
        self.assertContains(page, "Check the connection")
        self.assertContains(page, "Preview the import")


@override_settings(**CONFIGURED)
class ImportTests(TestCase):
    def setUp(self):
        self.client.force_login(
            User.objects.create_user(username="boss", password="x" * 16, role="admin")
        )
        self.url = reverse("lubelogger_import")

    @patch("homeautoshop.core.integrations.importer.run_import")
    @patch("homeautoshop.core.integrations.lubelogger.LubeLoggerClient")
    def test_a_locale_failure_stops_the_import_dead(self, client_cls, run_import):
        client_cls.return_value.check.return_value = LOCALE_TROUBLE

        response = self.client.post(self.url, {"action": "commit"})

        run_import.assert_not_called()
        self.assertContains(response, "1.234,56")

    @patch("homeautoshop.core.integrations.importer.run_import")
    @patch("homeautoshop.core.integrations.lubelogger.LubeLoggerClient")
    def test_an_unreachable_instance_stops_the_import_dead(self, client_cls, run_import):
        client_cls.return_value.check.return_value = Diagnosis(reachable=False)
        self.client.post(self.url, {"action": "commit"})
        run_import.assert_not_called()

    @patch("homeautoshop.core.integrations.importer.run_import")
    @patch("homeautoshop.core.integrations.lubelogger.LubeLoggerClient")
    def test_preview_is_a_dry_run(self, client_cls, run_import):
        client_cls.return_value.check.return_value = HEALTHY
        run_import.return_value = Report(dry_run=True, created={"fuel": 12})

        response = self.client.post(self.url, {"action": "preview"})

        self.assertTrue(run_import.call_args.kwargs["dry_run"])
        self.assertContains(response, "nothing was written")
        self.assertContains(response, "12")

    @patch("homeautoshop.core.integrations.importer.run_import")
    @patch("homeautoshop.core.integrations.lubelogger.LubeLoggerClient")
    def test_committing_is_not_a_dry_run(self, client_cls, run_import):
        client_cls.return_value.check.return_value = HEALTHY
        run_import.return_value = Report(dry_run=False, created={"fuel": 12})

        self.client.post(self.url, {"action": "commit"})
        self.assertFalse(run_import.call_args.kwargs["dry_run"])

    @patch("homeautoshop.core.integrations.importer.run_import")
    @patch("homeautoshop.core.integrations.lubelogger.LubeLoggerClient")
    def test_creating_missing_vehicles_is_opt_in(self, client_cls, run_import):
        client_cls.return_value.check.return_value = HEALTHY
        run_import.return_value = Report()

        self.client.post(self.url, {"action": "preview"})
        self.assertFalse(run_import.call_args.kwargs["create_missing"])

        self.client.post(self.url, {"action": "preview", "create_missing": "on"})
        self.assertTrue(run_import.call_args.kwargs["create_missing"])

    @patch("homeautoshop.core.integrations.importer.run_import")
    @patch("homeautoshop.core.integrations.lubelogger.LubeLoggerClient")
    def test_unmatched_vehicles_are_named_not_merged(self, client_cls, run_import):
        client_cls.return_value.check.return_value = HEALTHY
        run_import.return_value = Report(
            dry_run=True,
            unmatched=[VehicleMatch(external_id="7", label="2004 Tundra", how="unmatched")],
        )

        response = self.client.post(self.url, {"action": "preview"})
        self.assertContains(response, "2004 Tundra")
        self.assertContains(response, "never merged on its own")

    @patch("homeautoshop.core.integrations.lubelogger.LubeLoggerClient")
    def test_a_check_alone_imports_nothing(self, client_cls):
        client_cls.return_value.check.return_value = HEALTHY
        with patch("homeautoshop.core.integrations.importer.run_import") as run_import:
            self.client.post(self.url, {"action": "check"})
            run_import.assert_not_called()

    @patch("homeautoshop.core.integrations.lubelogger.LubeLoggerClient")
    def test_a_broken_import_reports_instead_of_five_hundred(self, client_cls):
        client_cls.return_value.check.return_value = HEALTHY
        with patch(
            "homeautoshop.core.integrations.importer.run_import",
            side_effect=ValueError("comma decimal in cost"),
        ):
            response = self.client.post(self.url, {"action": "preview"})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "comma decimal in cost")

    @patch(
        "homeautoshop.core.integrations.lubelogger.LubeLoggerClient",
        side_effect=NotConfigured("no url"),
    )
    def test_losing_the_configuration_mid_session_is_reported(self, _client_cls):
        response = self.client.post(self.url, {"action": "check"})
        self.assertEqual(response.status_code, 200)


@override_settings(**CONFIGURED)
class ManualLinkTests(TestCase):
    """A source vehicle with no usable identifier can still be paired.

    Refusing to guess is only defensible if there is a way to say which vehicle
    is which. Without this, an operator whose LubeLogger records carry no VIN
    and no plate has no route to importing them at all — which is the state the
    integration shipped in.
    """

    def setUp(self):
        self.admin = User.objects.create_user(
            username="boss", password="x" * 16, role="admin"
        )
        self.client.force_login(self.admin)
        self.asset = Asset.objects.create(nickname="Red truck", make="Ford", model="F-150")
        self.url = reverse("lubelogger_link")

    def _refs(self):
        from homeautoshop.core.models import ExternalRef

        return ExternalRef.objects.filter(source_system="lubelogger", external_type="vehicle")

    def test_linking_writes_a_provenance_row_and_nothing_else(self):
        self.client.post(self.url, {"external_id": "7", "asset": str(self.asset.pk)})
        ref = self._refs().get()
        self.assertEqual(ref.external_id, "7")
        self.assertEqual(ref.entity_id, self.asset.pk)
        self.assertEqual(WorkOrder.objects.count(), 0)

    def test_the_importer_honors_the_link_without_guessing_again(self):
        """One pairing, and every future run — including the scheduled pull."""
        from homeautoshop.core.integrations.importer import Importer

        self.client.post(self.url, {"external_id": "7", "asset": str(self.asset.pk)})

        class Client:
            # Must be the instance the link was written against: a pairing is
            # scoped to one LubeLogger, so a mismatched base URL means it is
            # simply not found.
            base_url = CONFIGURED["LUBELOGGER_URL"]

            def vehicles(self):
                return [{"id": 7, "year": 2007, "make": "Nissan", "model": "Frontier"}]

        matches = Importer(Client(), dry_run=True).match_vehicles()
        self.assertEqual(matches[0].asset, self.asset)
        self.assertEqual(matches[0].how, "external_ref")

    def test_relinking_moves_the_pairing_rather_than_adding_one(self):
        other = Asset.objects.create(nickname="Van")
        self.client.post(self.url, {"external_id": "7", "asset": str(self.asset.pk)})
        self.client.post(self.url, {"external_id": "7", "asset": str(other.pk)})
        self.assertEqual(self._refs().count(), 1)
        self.assertEqual(self._refs().get().entity_id, other.pk)

    def test_two_source_vehicles_cannot_point_at_one_vehicle(self):
        """That merges two histories into one record, silently."""
        self.client.post(self.url, {"external_id": "7", "asset": str(self.asset.pk)})
        response = self.client.post(
            self.url, {"external_id": "8", "asset": str(self.asset.pk)}, follow=True
        )
        self.assertContains(response, "already linked")
        self.assertEqual(self._refs().count(), 1)

    def test_a_link_can_be_undone(self):
        self.client.post(self.url, {"external_id": "7", "asset": str(self.asset.pk)})
        self.client.post(self.url, {"external_id": "7"})
        self.assertEqual(self._refs().count(), 0)

    def test_unlinking_something_that_was_never_linked_says_so(self):
        response = self.client.post(self.url, {"external_id": "99"}, follow=True)
        self.assertContains(response, "was not linked")

    def test_a_member_cannot_link(self):
        self.client.force_login(User.objects.create_user(username="andy", password="x" * 16))
        response = self.client.post(self.url, {"external_id": "7", "asset": str(self.asset.pk)})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self._refs().count(), 0)

    def test_a_get_does_not_link(self):
        self.assertEqual(self.client.get(self.url).status_code, 405)
