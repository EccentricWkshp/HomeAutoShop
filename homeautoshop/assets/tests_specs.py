"""
Adding a vehicle spec (SPEC §7.9, FR-SPEC-1..4).

Reported from use: entering a name already on file returned a 500. There is no
edit screen for a spec, so refusing the entry would have left "delete it and
type it again" as the only way to correct a digit — which is worse than the
error. Re-entering a name means correcting it.
"""

from __future__ import annotations

from django.test import TestCase
from django.urls import reverse

from homeautoshop.accounts.models import User
from homeautoshop.assets.models import Asset, AssetSpec


class SpecAddTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="andy", password="x" * 16)
        self.client.force_login(self.user)
        self.asset = Asset.objects.create(nickname="Red truck")
        self.url = reverse("spec_add", args=[self.asset.pk])

    def _post(self, follow=False, **overrides):
        payload = {"group": "access", "name": "Door Code", "value": "1234"}
        payload.update(overrides)
        return self.client.post(self.url, payload, follow=follow)

    def test_a_spec_can_be_added(self):
        self.assertEqual(self._post().status_code, 302)
        spec = AssetSpec.objects.get(asset=self.asset)
        self.assertEqual((spec.name, spec.value), ("Door Code", "1234"))

    def test_entering_the_same_name_updates_rather_than_exploding(self):
        self._post()
        response = self._post(value="5678")

        self.assertEqual(response.status_code, 302)
        spec = AssetSpec.objects.get(asset=self.asset)
        self.assertEqual(spec.value, "5678")
        self.assertEqual(AssetSpec.objects.filter(asset=self.asset).count(), 1)

    def test_the_update_says_so(self):
        self._post()
        response = self._post(value="5678", follow=True)
        self.assertContains(response, "Updated Door Code")

    def test_a_spec_can_be_deleted_and_added_again(self):
        self._post()
        spec = AssetSpec.objects.get(asset=self.asset)
        self.client.post(reverse("spec_delete", args=[self.asset.pk, spec.pk]))
        self.assertEqual(AssetSpec.objects.filter(asset=self.asset).count(), 0)

        self.assertEqual(self._post(value="9999").status_code, 302)
        self.assertEqual(AssetSpec.objects.get(asset=self.asset).value, "9999")

    def test_the_same_name_in_another_group_is_a_separate_spec(self):
        self._post()
        self._post(group="fluids", name="Door Code", value="different")
        self.assertEqual(AssetSpec.objects.filter(asset=self.asset).count(), 2)

    def test_a_security_group_defaults_to_sensitive(self):
        """Key and radio codes stay out of reports whether or not it is ticked."""
        self._post()
        self.assertTrue(AssetSpec.objects.get(asset=self.asset).is_sensitive)

    def test_an_update_cannot_quietly_unmark_something_sensitive_by_omission(self):
        self._post()
        self._post(value="5678")
        self.assertTrue(AssetSpec.objects.get(asset=self.asset).is_sensitive)

    def test_an_incomplete_spec_is_refused_with_a_message(self):
        response = self.client.post(self.url, {"group": "access", "name": ""}, follow=True)
        self.assertEqual(AssetSpec.objects.count(), 0)
        self.assertContains(response, "needs a group, a name and a value")


class SpecEditTests(TestCase):
    """Correcting a spec in place (FR-SPEC-1).

    The add form upserts by name, so it can change a value but can never rename
    anything — and deleting and retyping loses the provenance and the pinning
    along with the typo.
    """

    def setUp(self):
        self.user = User.objects.create_user(username="andy", password="x" * 16)
        self.client.force_login(self.user)
        self.asset = Asset.objects.create(nickname="Red truck")
        self.spec = AssetSpec.objects.create(
            asset=self.asset, group="tires", name="Front pressure", value="32", unit="psi"
        )
        self.list_url = reverse("asset_specs", args=[self.asset.pk])
        self.edit_url = reverse("spec_edit", args=[self.asset.pk, self.spec.pk])

    def _payload(self, **overrides):
        payload = {
            "group": "tires",
            "name": "Front pressure",
            "value": "32",
            "unit": "psi",
            "condition": "",
            "source": "manual",
        }
        payload.update(overrides)
        return payload

    def test_the_list_offers_an_edit_link(self):
        page = self.client.get(self.list_url)
        self.assertContains(page, f"?edit={self.spec.pk}")

    def test_asking_to_edit_returns_a_filled_in_form(self):
        page = self.client.get(self.list_url, {"edit": str(self.spec.pk)})
        self.assertEqual(page.context["editing"], self.spec)
        self.assertEqual(page.context["edit_form"]["value"].value(), "32")

    def test_an_unknown_id_is_ignored_rather_than_raising(self):
        page = self.client.get(self.list_url, {"edit": "01a05378-a0f7-7343-b0fb-807fa1194191"})
        self.assertEqual(page.status_code, 200)
        self.assertIsNone(page.context["editing"])

    def test_a_value_can_be_corrected(self):
        self.client.post(self.edit_url, self._payload(value="35"))
        self.spec.refresh_from_db()
        self.assertEqual(self.spec.value, "35")

    def test_a_spec_can_be_renamed(self):
        """The thing the add form cannot do, which is why this exists."""
        self.client.post(self.edit_url, self._payload(name="Front tire pressure"))
        self.spec.refresh_from_db()
        self.assertEqual(self.spec.name, "Front tire pressure")
        self.assertEqual(AssetSpec.objects.filter(asset=self.asset).count(), 1)

    def test_renaming_onto_another_spec_is_refused_with_a_reason(self):
        AssetSpec.objects.create(
            asset=self.asset, group="tires", name="Rear pressure", value="38", unit="psi"
        )
        response = self.client.post(
            self.edit_url, self._payload(name="Rear pressure"), follow=True
        )

        self.spec.refresh_from_db()
        self.assertEqual(self.spec.name, "Front pressure")
        self.assertContains(response, "already on the sheet under that group")

    def test_sensitivity_can_be_cleared_here_because_the_box_is_shown(self):
        """Unlike the add form, where an unticked box is an omission."""
        code = AssetSpec.objects.create(
            asset=self.asset, group="access", name="Door Code", value="1234"
        )
        self.assertTrue(code.is_sensitive)

        self.client.post(
            reverse("spec_edit", args=[self.asset.pk, code.pk]),
            {"group": "access", "name": "Door Code", "value": "1234", "source": "manual"},
        )
        code.refresh_from_db()
        self.assertFalse(code.is_sensitive)

    def test_editing_someone_elses_vehicle_spec_is_a_miss(self):
        other = Asset.objects.create(nickname="Other")
        response = self.client.post(
            reverse("spec_edit", args=[other.pk, self.spec.pk]), self._payload(value="99")
        )
        self.assertEqual(response.status_code, 404)
        self.spec.refresh_from_db()
        self.assertEqual(self.spec.value, "32")

    def test_an_invalid_edit_returns_to_the_form_rather_than_saving(self):
        response = self.client.post(self.edit_url, self._payload(value=""), follow=True)
        self.spec.refresh_from_db()
        self.assertEqual(self.spec.value, "32")
        self.assertContains(response, "needs a group, a name and a value")
