"""
Filling the spec sheet from something other than typing (FR-SPEC-1, §8.1, §8.3a).

Two sources, and they are not equally trustworthy — which is why neither writes
anything on its own.

A **VIN decode** is a registration record. It holds no torque values, no fluid
capacities and no cold tire pressures, and it is frequently wrong about
trim-dependent detail. So a decoded field is promoted one at a time, by a person
who decided that answer was worth relying on.

A **scan report** is read off the vehicle. Module part and calibration numbers
are exact, which makes them worth importing — but a report names a vehicle, and
attaching one car's identifiers to another produces a spec sheet that is wrong
in a way nothing afterwards would reveal. Hence the VIN check.
"""

from __future__ import annotations

import pathlib

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse

from homeautoshop.accounts.models import User
from homeautoshop.assets.models import Asset, AssetSpec

CORPUS = pathlib.Path(__file__).resolve().parents[2] / "Artifacts" / "samples" / "scan-reports"
# Resolved rather than joined: the corpus is filed one folder per scanner, and
# a flat join silently pointed at nothing when the folders arrived.
SILVERADO = next(iter(sorted(CORPUS.rglob("Silverado202504120859.pdf"))), CORPUS / "missing.pdf")


class PromoteFromDecodeTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="andy", password="x" * 16)
        self.client.force_login(self.user)
        self.asset = Asset.objects.create(
            nickname="Work truck",
            vin="1FTFW1ET5DFC10312",
            decoded_raw={
                "EngineHP": "365",
                "GVWR": "Class 2F: 7,001 - 8,000 lb",
                "TPMS": "Direct",
                "BrakeSystemType": "Hydraulic",
                "EngineCylinders": "",
            },
        )
        self.url = reverse("spec_from_decode", args=[self.asset.pk])

    def test_a_decoded_field_becomes_a_spec_with_its_provenance(self):
        self.client.post(self.url, {"key": "EngineHP"})

        spec = AssetSpec.objects.get(asset=self.asset)
        self.assertEqual(spec.name, "Horsepower")
        self.assertEqual(spec.value, "365")
        self.assertEqual(spec.unit, "hp")
        self.assertEqual(spec.group, "electrical")
        self.assertEqual(spec.source, "decoded")

    def test_the_group_follows_the_field(self):
        self.client.post(self.url, {"key": "TPMS"})
        self.assertEqual(AssetSpec.objects.get(asset=self.asset).group, "tires")

    def test_an_unmapped_field_still_promotes_rather_than_being_refused(self):
        self.asset.decoded_raw = {"PlantCity": "DEARBORN"}
        self.asset.save()
        self.client.post(self.url, {"key": "PlantCity"})
        self.assertEqual(AssetSpec.objects.get(asset=self.asset).group, "other")

    def test_promoting_twice_corrects_rather_than_duplicating(self):
        self.client.post(self.url, {"key": "EngineHP"})
        self.asset.decoded_raw = {**self.asset.decoded_raw, "EngineHP": "400"}
        self.asset.save()
        self.client.post(self.url, {"key": "EngineHP"})

        self.assertEqual(AssetSpec.objects.filter(asset=self.asset).count(), 1)
        self.assertEqual(AssetSpec.objects.get(asset=self.asset).value, "400")

    def test_an_empty_field_writes_nothing(self):
        response = self.client.post(self.url, {"key": "EngineCylinders"}, follow=True)
        self.assertEqual(AssetSpec.objects.count(), 0)
        self.assertContains(response, "nothing recorded for that field")

    def test_a_field_the_decode_never_returned_writes_nothing(self):
        self.client.post(self.url, {"key": "Invented"})
        self.assertEqual(AssetSpec.objects.count(), 0)

    def test_the_panel_offers_the_button(self):
        page = self.client.get(reverse("asset_detail", args=[self.asset.pk]))
        self.assertContains(page, reverse("spec_from_decode", args=[self.asset.pk]))
        self.assertContains(page, 'value="EngineHP"')


class ImportFromScanReportTests(TestCase):
    """Uses a real report, so the parser and this path are exercised together."""

    def setUp(self):
        self.user = User.objects.create_user(username="andy", password="x" * 16)
        self.client.force_login(self.user)
        # Read from the report rather than written down. The sample is a real
        # vehicle, and its VIN has no business being a literal in a file that
        # gets committed — the redaction guard catches exactly this, and caught
        # this very test.
        self.asset = Asset.objects.create(nickname="Silverado", vin=self._corpus_vin())
        self.url = reverse("spec_from_scan", args=[self.asset.pk])

    @staticmethod
    def _corpus_vin() -> str:
        if not SILVERADO.exists():
            return ""
        from homeautoshop.scantools import parse

        return parse(str(SILVERADO)).vehicle.vin

    def _upload(self, path=SILVERADO):
        return SimpleUploadedFile(
            path.name, path.read_bytes(), content_type="application/pdf"
        )

    def _skip_without_corpus(self):
        if not SILVERADO.exists():
            self.skipTest("the original reports are not in this checkout")

    def test_a_report_is_previewed_before_anything_is_written(self):
        self._skip_without_corpus()
        response = self.client.post(self.url, {"report": self._upload()})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "24261370")          # software part number
        self.assertContains(response, "nothing has been written yet")
        self.assertEqual(AssetSpec.objects.count(), 0)

    def test_confirming_records_the_identifiers(self):
        self._skip_without_corpus()
        preview = self.client.post(self.url, {"report": self._upload()})
        payload = preview.context["payload"]

        self.client.post(self.url, {"confirm": "1", "payload": payload})

        specs = {s.name: s for s in AssetSpec.objects.filter(asset=self.asset)}
        self.assertEqual(specs["Software part number"].value, "24261370")
        self.assertEqual(specs["Calibration part number"].value, "24261373")
        self.assertEqual(specs["Software part number"].source, "scan_tool")
        self.assertEqual(specs["Software part number"].group, "electrical")

    def test_the_vin_is_not_imported_as_a_spec(self):
        self._skip_without_corpus()
        preview = self.client.post(self.url, {"report": self._upload()})
        self.client.post(self.url, {"confirm": "1", "payload": preview.context["payload"]})
        self.assertFalse(AssetSpec.objects.filter(name="VIN").exists())

    def test_a_report_for_another_vehicle_is_refused(self):
        self._skip_without_corpus()
        # The ISO 3779 worked example: a valid VIN belonging to nobody.
        other = Asset.objects.create(nickname="Another truck", vin="1M8GDM9AXKP042788")
        response = self.client.post(
            reverse("spec_from_scan", args=[other.pk]), {"report": self._upload()}, follow=True
        )
        self.assertContains(response, "not this vehicle")
        self.assertEqual(AssetSpec.objects.count(), 0)

    def test_re_importing_corrects_rather_than_duplicating(self):
        self._skip_without_corpus()
        for _ in range(2):
            preview = self.client.post(self.url, {"report": self._upload()})
            self.client.post(self.url, {"confirm": "1", "payload": preview.context["payload"]})
        self.assertEqual(AssetSpec.objects.filter(name="Software part number").count(), 1)

    def test_something_that_is_not_a_report_is_reported_not_raised(self):
        rubbish = SimpleUploadedFile("notes.pdf", b"not a pdf at all", "application/pdf")
        response = self.client.post(self.url, {"report": rubbish}, follow=True)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "could not be read as a scan report")
        self.assertEqual(AssetSpec.objects.count(), 0)

    def test_no_file_is_a_message_not_a_crash(self):
        response = self.client.post(self.url, {}, follow=True)
        self.assertContains(response, "Choose a scan-tool report")
