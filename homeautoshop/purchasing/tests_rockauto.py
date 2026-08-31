"""
Reading a parts order (SPEC FR-PUR-1, FR-PART-2/3/4, §8.3a).

Run against **nine real order confirmations**, captured as redacted text and
geometry (`importers/capture.py`) so the corpus is real without the documents
being in the repository — the originals name a person, their street address,
their phone number and their email, twice each.

Two kinds of test, and both are needed:

* **Frozen expectations**, one per document. Every field the parser reads is
  recorded, so a change that improves one order and quietly breaks another
  shows up as a diff rather than as a wrong price six months later.
* **Named hazards**, spelled out. A frozen blob tells you *that* something
  changed, never *what mattered*. The wrapped part number, the description that
  wraps above its own row, the kit whose contents must not be charged twice,
  and the rebate that used to be glued onto a part number each get a test that
  says why it is there.
"""

from __future__ import annotations

import json
import re
from decimal import Decimal
from pathlib import Path

from django.test import TestCase

from homeautoshop.assets.models import Asset
from homeautoshop.core.models import ExternalRef
from homeautoshop.parts.models import Part, PartCrossRef, PartFitment, PartKitItem
from homeautoshop.purchasing.importers import capture, rockauto, service
from homeautoshop.purchasing.models import Purchase

FIXTURES = Path(__file__).resolve().parent / "importers" / "fixtures"


def captures() -> list[Path]:
    return sorted(FIXTURES.glob("*.capture.json"))


def load(stem: str) -> rockauto.ParsedOrder:
    blob = json.loads((FIXTURES / f"{stem}.capture.json").read_text(encoding="utf-8"))
    return rockauto.parse_document(blob["text"], blob["words"])


class CorpusTests(TestCase):
    def test_there_is_a_corpus_at_all(self):
        """A suite that silently checks nothing is worse than no suite."""
        self.assertGreaterEqual(len(captures()), 9)

    def test_every_order_still_reads_the_way_it_did(self):
        for path in captures():
            stem = path.name.removesuffix(".capture.json")
            with self.subTest(order=stem):
                expected = json.loads(
                    (FIXTURES / f"{stem}.expected.json").read_text(encoding="utf-8")
                )
                self.assertEqual(capture.expected(load(stem)), expected)

    def test_every_order_reconciles_with_its_own_stated_total(self):
        """The strongest check available, and it needs no fixture.

        Lines plus tax plus shipping less any rebate must equal the total the
        document prints. A mis-read price, a dropped line, a quantity read as
        one instead of two — all of them break this, and nothing else in the
        parser can quietly absorb them.
        """
        for path in captures():
            stem = path.name.removesuffix(".capture.json")
            with self.subTest(order=stem):
                order = load(stem)
                self.assertEqual(
                    order.subtotal_minor
                    + order.tax_minor
                    + order.shipping_minor
                    - order.discount_minor,
                    order.total_minor,
                )
                self.assertEqual(order.warnings, [])

    def test_no_fixture_carries_anything_identifying(self):
        """The corpus is a shipping document. This is the whole reason it is
        captured rather than committed."""
        for path in FIXTURES.glob("*.json"):
            with self.subTest(fixture=path.name):
                blob = json.loads(path.read_text(encoding="utf-8"))
                if "words" not in blob:
                    continue
                self.assertFalse(capture.leaked(blob))

    def test_the_redaction_refuses_rather_than_half_doing_it(self):
        """`Ship To:` is two words. Searching each word for it matched nothing,
        the address band was never found, and every capture was written with a
        real name in it. Not finding the block is now the failure."""
        with self.assertRaises(capture.CouldNotRedact):
            capture._address_band([{"text": "Nothing", "x0": 0, "x1": 5, "top": 0}])


class HazardTests(TestCase):
    """The layout traps, each named."""

    def test_a_part_number_broken_across_two_lines_is_one_part_number(self):
        """`90K38156B (90K-` / `38156B)` — printed above and below its own row."""
        order = load("175415205")
        numbers = [line.part_number for line in order.lines]
        self.assertIn("90K38156B (90K-38156B)", numbers)
        self.assertNotIn("38156B)", numbers)

    def test_a_description_that_wraps_upward_lands_on_the_right_row(self):
        """`[Kit Component] A/C Expansion` prints *above* the row it labels,
        so read in reading order it attaches to the line before."""
        order = load("357640871")
        by_number = {line.part_number: line for line in order.lines}
        self.assertEqual(by_number["3411375"].description, "A/C Expansion Valve")
        self.assertEqual(by_number["9642644B"].description, "A/C Compressor & Component Kit")

    def test_a_kit_is_charged_once_and_its_contents_are_still_recorded(self):
        order = load("357640871")
        kit = next(line for line in order.lines if line.part_number == "9642644B")
        components = [line for line in order.lines if line.is_kit_component]

        self.assertFalse(kit.is_kit_component)
        self.assertEqual(kit.total_minor, 35879)
        self.assertEqual(len(components), 3)
        self.assertTrue(all(line.total_minor is None for line in components))
        # And they are exactly the kit price, which is why double counting them
        # would have been so easy to miss.
        self.assertEqual(sum(line.unit_price_minor for line in components), 35879)

    def test_a_rebate_is_money_and_not_part_of_a_part_number(self):
        """`Instant $7 Manufacturer Rebate!` prints as its own row with a
        negative total. Treated as wrapped text it became
        `17D1083MHF1 Instant $7 Manufacturer Rebate!` and its seven dollars
        went missing from the arithmetic."""
        order = load("197262834")
        self.assertEqual(order.discount_minor, 700)
        self.assertEqual(order.adjustments, [("Instant $7 Manufacturer Rebate!", 700)])
        self.assertIn("17D1083MHF1", [line.part_number for line in order.lines])

    def test_a_core_charge_is_kept_apart_from_the_price(self):
        """FR-PUR-4: a core is refundable and is not part of what the part cost."""
        order = load("197262834")
        caliper = next(line for line in order.lines if line.part_number == "18FR2451")
        self.assertEqual(caliper.unit_price_minor, 5379)
        self.assertEqual(caliper.core_minor, 3500)

    def test_a_dash_in_the_core_column_means_no_core_not_zero(self):
        """They read the same on a page and differently in a total."""
        order = load("357640871")
        kit = next(line for line in order.lines if line.part_number == "9642644B")
        self.assertIsNone(kit.core_minor)

    def test_a_quantity_above_one_multiplies(self):
        order = load("357640871")
        line = next(line for line in order.lines if line.part_number == "TC2420")
        self.assertEqual(line.quantity, Decimal(2))
        self.assertEqual(line.total_minor, 2824)

    def test_the_vehicle_heading_groups_the_lines_under_it(self):
        order = load("357640871")
        self.assertEqual(order.vehicles, ["2004 SUZUKI AERIO 2.3L L4"])
        self.assertTrue(all(line.vehicle for line in order.lines))

    def test_a_multi_word_brand_survives(self):
        order = load("252849927")
        self.assertEqual(order.lines[0].brand, "VARIOUS MFR")
        self.assertEqual(order.lines[0].part_number, "SZ2595100")

    def test_something_that_is_not_one_of_these_is_refused_by_name(self):
        with self.assertRaises(rockauto.NotARockAutoOrder):
            rockauto.parse_document(["Amazon order summary"], [[]])


class VehicleHeadingTests(TestCase):
    def test_it_splits_into_something_the_fitment_table_can_hold(self):
        self.assertEqual(
            service.parse_vehicle("2004 SUZUKI AERIO 2.3L L4"),
            {"year": 2004, "make": "Suzuki", "model": "Aerio", "engine": "2.3L L4"},
        )

    def test_a_model_with_spaces_in_it_stays_whole(self):
        details = service.parse_vehicle("2007 FORD F-150 5.4L V8")
        self.assertEqual(details["make"], "Ford")
        self.assertEqual(details["model"], "F-150")

    def test_nonsense_is_nothing_rather_than_a_guess(self):
        self.assertEqual(service.parse_vehicle("not a vehicle"), {})


class ImportTests(TestCase):
    def setUp(self):
        self.order = load("179986706")

    def test_a_dry_run_writes_nothing(self):
        report = service.run(self.order, dry_run=True)
        self.assertEqual(Purchase.objects.count(), 0)
        self.assertEqual(Part.objects.count(), 0)
        self.assertEqual(len(report.outcomes), 4)

    def test_and_says_what_it_would_have_done(self):
        report = service.run(self.order, dry_run=True)
        self.assertEqual(report.parts_created, 4)
        self.assertEqual(report.parts_matched, 0)

    def test_a_real_run_makes_the_purchase_and_its_lines(self):
        service.run(self.order, dry_run=False)
        purchase = Purchase.objects.get()
        self.assertEqual(purchase.order_number, "179986706")
        self.assertEqual(purchase.vendor.name, "RockAuto")
        self.assertEqual(purchase.lines.count(), 4)
        self.assertEqual(purchase.shipping_minor, 2298)
        self.assertEqual(purchase.tax_minor, 1476)

    def test_the_money_adds_up_to_what_the_order_said(self):
        service.run(self.order, dry_run=False)
        self.assertEqual(Purchase.objects.get().total_minor, self.order.total_minor)

    def test_the_parts_arrive_with_their_brand_and_number(self):
        service.run(self.order, dry_run=False)
        part = Part.objects.get(part_number="RK621296")
        self.assertEqual(part.manufacturer, "MOOG")
        self.assertEqual(part.name, "Control Arm")

    def test_the_vendor_number_is_kept_as_a_cross_reference(self):
        """FR-PART-2: the same part has five numbers and all five must find it."""
        service.run(self.order, dry_run=False)
        self.assertTrue(
            PartCrossRef.objects.filter(
                value="RK621296", system=PartCrossRef.System.VENDOR_SKU
            ).exists()
        )

    def test_a_part_already_on_the_shelf_is_matched_rather_than_duplicated(self):
        Part.objects.create(name="Control arm", manufacturer="MOOG", part_number="RK621296")
        report = service.run(self.order, dry_run=False)
        self.assertEqual(Part.objects.filter(part_number="RK621296").count(), 1)
        self.assertEqual(report.parts_matched, 1)

    def test_matching_ignores_the_case_the_number_was_typed_in(self):
        Part.objects.create(name="Control arm", manufacturer="moog", part_number="rk621296")
        report = service.run(self.order, dry_run=False)
        self.assertEqual(report.parts_matched, 1)

    def test_fitment_is_the_vendors_claim_and_says_so(self):
        """FR-PART-4. Nobody watched this part go on a car."""
        service.run(self.order, dry_run=False)
        fitment = PartFitment.objects.filter(part__part_number="RK621296").get()
        self.assertEqual(fitment.confidence, PartFitment.Confidence.VENDOR)
        self.assertEqual(fitment.make, "Suzuki")
        self.assertEqual(fitment.year_from, 2004)

    def test_fitment_points_at_your_own_vehicle_when_you_have_it(self):
        asset = Asset.objects.create(nickname="The Suzuki", make="Suzuki", model="Aerio", year=2004)
        service.run(self.order, dry_run=False)
        self.assertEqual(PartFitment.objects.filter(asset=asset).count(), 4)

    def test_and_is_still_recorded_when_you_do_not(self):
        """A part bought for a car you no longer own is still a part that fits it."""
        service.run(self.order, dry_run=False)
        self.assertEqual(PartFitment.objects.filter(asset__isnull=True).count(), 4)

    def test_a_fitment_you_removed_is_not_brought_back_by_a_second_import(self):
        """Correcting a vendor's claim has to outlast the claim.

        `get_or_create` could not see a soft-deleted row, so re-importing the
        same order made a fresh copy of a fitment the operator had already
        thrown away — the import quietly undoing their work, and looking
        idempotent while it did it.
        """
        service.run(self.order, dry_run=False)
        fitment = PartFitment.objects.filter(part__part_number="RK621296").get()
        fitment.delete()

        service.run(load("179986706"), dry_run=False)

        self.assertFalse(
            PartFitment.objects.filter(part__part_number="RK621296").exists(),
            "the import resurrected a fitment that had been removed",
        )

    def test_a_fitment_marked_as_not_fitting_survives_a_second_import(self):
        """The stronger case: the shop's own finding beats the vendor's claim."""
        service.run(self.order, dry_run=False)
        fitment = PartFitment.objects.filter(part__part_number="RK621296").get()
        fitment.confidence = PartFitment.Confidence.DOES_NOT_FIT
        fitment.save()

        service.run(load("179986706"), dry_run=False)

        rows = PartFitment.objects.filter(part__part_number="RK621296")
        self.assertEqual(rows.count(), 1, "the claim was recorded a second time")
        self.assertEqual(rows.get().confidence, PartFitment.Confidence.DOES_NOT_FIT)

    def test_reading_the_same_order_twice_does_not_make_two_purchases(self):
        """§6.2 — `external_ref` is what makes an import idempotent."""
        service.run(self.order, dry_run=False)
        service.run(load("179986706"), dry_run=False)
        self.assertEqual(Purchase.objects.count(), 1)
        self.assertEqual(Purchase.objects.get().lines.count(), 4)
        self.assertEqual(ExternalRef.objects.filter(source_system="rockauto").count(), 1)

    def test_an_order_already_received_is_left_alone(self):
        """Replacing its lines would throw away the stock lots hanging off them."""
        service.run(self.order, dry_run=False)
        line = Purchase.objects.get().lines.first()
        line.qty_received = 1
        line.save()

        report = service.run(load("179986706"), dry_run=False)
        self.assertTrue(any("received" in warning for warning in report.warnings))
        self.assertEqual(Purchase.objects.get().lines.count(), 4)

    def test_a_kit_component_becomes_a_part_but_never_a_charge(self):
        report = service.run(load("357640871"), dry_run=False)
        purchase = Purchase.objects.get(order_number="357640871")
        self.assertTrue(Part.objects.filter(part_number="6511690").exists())
        self.assertFalse(purchase.lines.filter(part__part_number="6511690").exists())
        self.assertEqual(purchase.lines.count(), len(report.order.charged_lines))

    def test_a_core_charge_on_the_invoice_teaches_the_part_it_has_one(self):
        service.run(load("197262834"), dry_run=False)
        self.assertTrue(Part.objects.get(part_number="18FR2451").has_core)

    def test_the_kit_ends_up_knowing_what_is_inside_it(self):
        """FR-INV-9. The confirmation lists the three components under the kit,
        and until now that grouping was read and thrown away — leaving three
        loose parts and a box with no stated contents, which is the state that
        gets a drier ordered twice."""
        report = service.run(load("357640871"), dry_run=False)
        kit = Part.objects.get(part_number="9642644B")

        self.assertEqual(report.kit_items_recorded, 3)
        self.assertEqual(
            sorted(item.part.part_number for item in kit.kit_items.all()),
            ["3411375", "4696C", "6511690"],
        )
        self.assertTrue(kit.is_kit)

    def test_the_price_column_becomes_the_cost_split(self):
        """The vendor prints `Price EA` against each component even though the
        line is not charged, and those three prices add up to the kit's own —
        so the split is stated on the document and does not need guessing at."""
        service.run(load("357640871"), dry_run=False)
        kit = Part.objects.get(part_number="9642644B")
        shares = {
            item.part.part_number: item.value_minor for item in kit.kit_items.all()
        }

        self.assertEqual(shares["3411375"], 846)
        self.assertEqual(shares["6511690"], 17526)
        self.assertEqual(shares["4696C"], 17507)
        # Which is the kit's own price, hence a true ratio rather than a guess.
        self.assertEqual(sum(shares.values()), 35879)

    def test_a_component_learns_its_own_price_too(self):
        """A component line is never charged, so it produces no purchase line —
        this is the only place that part's price appears anywhere."""
        service.run(load("357640871"), dry_run=False)
        self.assertEqual(
            Part.objects.get(part_number="6511690").known_cost_minor, 17526
        )

    def test_a_price_somebody_already_stated_is_not_overwritten(self):
        Part.objects.create(
            name="A/C Compressor", manufacturer="GPD", part_number="6511690",
            typical_cost_minor=9900,
        )

        service.run(load("357640871"), dry_run=False)

        self.assertEqual(
            Part.objects.get(part_number="6511690").typical_cost_minor, 9900
        )

    def test_a_component_taken_out_of_the_kit_stays_out(self):
        """Same rule as fitment: re-importing the order must not undo somebody
        deciding the vendor's listing was wrong about the box."""
        service.run(load("357640871"), dry_run=False)
        kit = Part.objects.get(part_number="9642644B")
        kit.kit_items.get(part__part_number="4696C").delete()

        service.run(load("357640871"), dry_run=False)

        self.assertEqual(kit.kit_items.count(), 2)

    def test_re_importing_does_not_stack_the_contents_up(self):
        service.run(load("357640871"), dry_run=False)
        service.run(load("357640871"), dry_run=False)
        self.assertEqual(Part.objects.get(part_number="9642644B").kit_items.count(), 3)

    def test_a_dry_run_records_no_contents_either(self):
        report = service.run(load("357640871"), dry_run=True)
        self.assertEqual(report.kit_items_recorded, 3)
        self.assertEqual(PartKitItem.objects.count(), 0)

    def test_the_review_screen_names_the_kit_a_component_landed_in(self):
        report = service.run(load("357640871"), dry_run=True)
        inside = {
            outcome.line.part_number: outcome.inside_kit
            for outcome in report.outcomes
            if outcome.inside_kit is not None
        }
        self.assertEqual(set(inside), {"3411375", "6511690", "4696C"})
        self.assertTrue(all(kit.part_number == "9642644B" for kit in inside.values()))


class ScreenTests(TestCase):
    """The import rehearses before it writes, like every other one here."""

    def setUp(self):
        import shutil
        import tempfile

        from django.test import override_settings

        from homeautoshop.accounts.models import Role, User

        self.tmp = Path(tempfile.mkdtemp())
        self.storage = override_settings(
            MEDIA_ROOT=self.tmp,
            STORAGES={
                "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
                "staticfiles": {
                    "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"
                },
            },
        )
        self.storage.enable()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.addCleanup(self.storage.disable)

        self.user = User.objects.create_user(username="andy", password="x" * 16, role=Role.ADMIN)
        self.client.force_login(self.user)

    def a_file(self, stem: str = "179986706"):
        """The fixture, rendered back into something a form can post.

        The originals cannot be used: they are a shipping document with a real
        name and address on them, and they are not in the repository.
        """
        from unittest import mock

        blob = json.loads((FIXTURES / f"{stem}.capture.json").read_text(encoding="utf-8"))
        from django.core.files.uploadedfile import SimpleUploadedFile

        upload = SimpleUploadedFile("order.pdf", b"%PDF-1.4 stand-in", content_type="application/pdf")
        return upload, mock.patch.object(
            rockauto, "read_pdf", return_value=(blob["text"], blob["words"])
        )

    def test_the_page_offers_it(self):
        from django.urls import reverse

        page = self.client.get(reverse("order_import"))
        self.assertContains(page, "RockAuto")

    def test_a_preview_writes_nothing(self):
        from django.urls import reverse

        upload, patched = self.a_file()
        with patched:
            response = self.client.post(
                reverse("order_import"), {"order": upload, "action": "preview"}, follow=True
            )
        self.assertContains(response, "Nothing has been written yet")
        self.assertEqual(Purchase.objects.count(), 0)

    def test_a_preview_can_be_confirmed_without_choosing_the_file_again(self):
        """The flaw this flow exists to avoid: a browser clears a file input on
        submit, so previewing and then importing used to mean two uploads."""
        from django.urls import reverse

        upload, patched = self.a_file()
        with patched:
            preview = self.client.post(
                reverse("order_import"), {"order": upload, "action": "preview"}
            )
            self.assertEqual(Purchase.objects.count(), 0)
            held = preview.context["held"]

            # No file this time — only what the preview handed back.
            self.client.post(reverse("order_import"), {"held": held, "action": "commit"})

        purchase = Purchase.objects.get()
        self.assertEqual(purchase.order_number, "179986706")
        self.assertEqual(purchase.lines.count(), 4)

    def test_the_document_is_kept_once_however_many_times_it_is_previewed(self):
        """`ingest` deduplicates by SHA-256, which is what makes storing it on
        the preview affordable."""
        from homeautoshop.mediafiles.models import Media
        from django.urls import reverse

        for _ in range(3):
            upload, patched = self.a_file()
            with patched:
                self.client.post(
                    reverse("order_import"), {"order": upload, "action": "preview"}
                )
        self.assertEqual(Media.objects.count(), 1)

    def test_the_confirmation_ends_up_attached_to_the_purchase(self):
        from homeautoshop.mediafiles.models import MediaLink
        from django.urls import reverse

        upload, patched = self.a_file()
        with patched:
            preview = self.client.post(
                reverse("order_import"), {"order": upload, "action": "preview"}
            )
            self.client.post(
                reverse("order_import"),
                {"held": preview.context["held"], "action": "commit"},
            )
        purchase = Purchase.objects.get()
        self.assertTrue(
            MediaLink.objects.filter(
                entity_type="Purchase", entity_id=purchase.pk, role=MediaLink.Role.RECEIPT
            ).exists()
        )

    def test_a_tampered_reference_is_refused(self):
        """It is a filename in a form. Signed, so it cannot be edited into
        importing a document nobody was shown."""
        from django.urls import reverse

        response = self.client.post(
            reverse("order_import"),
            {"held": "not-a-real-token", "action": "commit"},
            follow=True,
        )
        self.assertContains(response, "expired")
        self.assertEqual(Purchase.objects.count(), 0)

    def test_the_review_screen_offers_the_button(self):
        from django.urls import reverse

        upload, patched = self.a_file()
        with patched:
            preview = self.client.post(
                reverse("order_import"), {"order": upload, "action": "preview"}
            )
        self.assertContains(preview, "Read this in")

    def test_money_is_shown_as_money_and_not_as_cents(self):
        """`5379` under a column headed "Each" is not a price anybody reads,
        and this screen exists to be read."""
        from django.urls import reverse

        upload, patched = self.a_file("197262834")
        with patched:
            preview = self.client.post(
                reverse("order_import"), {"order": upload, "action": "preview"}
            )
        page = preview.content.decode()
        self.assertIn("$53.79", page)
        self.assertIn("$35.00", page)
        self.assertNotIn(">5379<", page)

    def test_committing_lands_on_the_purchase_it_made(self):
        from django.urls import reverse

        upload, patched = self.a_file()
        with patched:
            response = self.client.post(
                reverse("order_import"), {"order": upload, "action": "commit"}, follow=True
            )
        purchase = Purchase.objects.get()
        self.assertContains(response, purchase.order_number)
        self.assertEqual(purchase.lines.count(), 4)

    def test_a_file_that_is_not_one_of_these_says_so(self):
        from unittest import mock

        from django.core.files.uploadedfile import SimpleUploadedFile
        from django.urls import reverse

        upload = SimpleUploadedFile("x.pdf", b"%PDF-1.4", content_type="application/pdf")
        with mock.patch.object(rockauto, "read_pdf", return_value=(["Amazon"], [[]])):
            response = self.client.post(
                reverse("order_import"), {"order": upload, "action": "preview"}, follow=True
            )
        self.assertContains(response, "does not look like a RockAuto order")

    def test_nothing_chosen_is_a_message_not_a_crash(self):
        from django.urls import reverse

        response = self.client.post(reverse("order_import"), {}, follow=True)
        self.assertContains(response, "Choose an order confirmation")
