"""
Oil and fluid analysis (SPEC §7.9a, FR-FLU-*, roadmap R-5).

Three things could make this feature worse than not having it, and most of
what is below defends against one of them.

**Comparing incomparable samples.** Wear metals accumulate while the fluid is
in service, so 24 ppm of iron on 3,000 miles of oil and 24 ppm on 9,000 are
different statements about the same engine. A trend that put both on one line
would read as flat when wear had halved. So the rate is per unit of *fluid*
life, and a sample that never recorded that interval is named rather than
averaged in.

**Rating what cannot be rated.** Viscosity per thousand miles is not a
quantity, and an additive pack *depletes* — expressing zinc as a rate inverts
its meaning. `accumulates` is the flag that decides, and half these tests are
about the rows where it must be false.

**Passing judgment.** Nothing here computes a verdict on somebody's engine.
The lab's comment is stored as written; what the application says is
arithmetic on the operator's own samples.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from django.test import TestCase
from django.urls import reverse

from homeautoshop.accounts.models import Role, User
from homeautoshop.assets.models import Asset
from homeautoshop.fluids import analytes
from homeautoshop.fluids.models import Compartment, FluidResult, FluidSample
from homeautoshop.fluids.services import parse_results, save_results, series, trends

VIN = "1M8GDM9AXKP042788"


class Fixture(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="andy", password="x" * 16, role=Role.ADMIN
        )
        self.client.force_login(self.user)
        self.asset = Asset.objects.create(nickname="Red truck", vin=VIN)

    def sample(self, *, on: date, fluid_usage=None, compartment=Compartment.ENGINE_OIL,
               position="", **results) -> FluidSample:
        row = FluidSample.objects.create(
            asset=self.asset,
            compartment=compartment,
            position=position,
            sampled_on=on,
            fluid_usage=None if fluid_usage is None else Decimal(str(fluid_usage)),
        )
        for analyte, value in results.items():
            FluidResult.objects.create(
                sample=row,
                analyte=analyte,
                value=Decimal(str(value)),
                unit=analytes.BY_SLUG[analyte].unit,
            )
        return row


class ARateNotALevelTests(Fixture):
    """The judgment the whole feature stands on."""

    def test_the_same_reading_over_a_longer_interval_is_a_lower_rate(self):
        self.sample(on=date(2026, 1, 5), fluid_usage=3000, iron=24)
        self.sample(on=date(2026, 6, 5), fluid_usage=9000, iron=24)
        trend = self._iron()
        self.assertEqual(trend.points[0].rate, Decimal("8.00"))
        self.assertEqual(trend.points[1].rate, Decimal("2.67"))
        # Flat on the face of it, and a third of the wear.
        self.assertEqual(trend.change, Decimal("0.33"))

    def test_a_sample_with_no_interval_gets_no_rate(self):
        self.sample(on=date(2026, 1, 5), iron=24)
        self.assertIsNone(self._iron().points[0].rate)

    def test_and_the_trend_says_so_instead_of_comparing_anyway(self):
        """The failure worth avoiding: a confident number built on one guess."""
        self.sample(on=date(2026, 1, 5), fluid_usage=3000, iron=10)
        self.sample(on=date(2026, 6, 5), iron=40)
        trend = self._iron()
        self.assertIsNone(trend.change)
        self.assertEqual(trend.uncomparable, 1)
        self.assertIn("did not record", trend.summary)

    def test_a_clean_sample_reading_zero_is_still_a_rate(self):
        """Truthiness reported the cleanest possible result as unrecorded."""
        self.sample(on=date(2026, 1, 5), fluid_usage=3000, iron=0)
        self.sample(on=date(2026, 6, 5), fluid_usage=3000, iron=6)
        trend = self._iron()
        self.assertEqual(trend.points[0].rate, Decimal("0.00"))
        self.assertEqual(trend.uncomparable, 0)
        self.assertTrue(trend.is_rated)
        # Nothing to multiply from, so no multiple is claimed.
        self.assertIsNone(trend.change)
        self.assertNotIn("did not record", trend.summary)

    def test_a_zero_interval_is_not_divided_by(self):
        self.sample(on=date(2026, 1, 5), fluid_usage=0, iron=10)
        self.assertIsNone(self._iron().points[0].rate)

    def _iron(self):
        found = [t for t in trends(self.asset, compartment=Compartment.ENGINE_OIL)
                 if t.analyte == "iron"]
        return found[0]


class WhatIsAndIsNotARateTests(Fixture):
    """`accumulates` — the flag that keeps three quarters of a panel honest."""

    def test_a_wear_metal_accumulates(self):
        self.assertTrue(analytes.accumulates("iron"))
        self.assertTrue(analytes.accumulates("copper"))

    def test_dirt_getting_past_the_filter_accumulates_too(self):
        self.assertTrue(analytes.accumulates("silicon"))

    def test_an_additive_does_not_accumulate_it_depletes(self):
        """A falling zinc number expressed as a rate would read as improving."""
        self.assertFalse(analytes.accumulates("zinc"))
        self.assertFalse(analytes.accumulates("calcium"))

    def test_a_physical_property_is_a_state_not_a_total(self):
        self.assertFalse(analytes.accumulates("viscosity_100c"))
        self.assertFalse(analytes.accumulates("tbn"))

    def test_a_concentration_is_not_a_total_either(self):
        """An oil is 0.3% water or it is not; it does not climb with mileage."""
        self.assertFalse(analytes.accumulates("water"))
        self.assertFalse(analytes.accumulates("fuel"))

    def test_an_unknown_analyte_is_left_as_the_number_the_lab_printed(self):
        """Safe default: never convert something nobody has checked."""
        self.assertFalse(analytes.accumulates("some_new_element"))

    def test_a_property_is_compared_on_its_raw_value(self):
        self.sample(on=date(2026, 1, 5), fluid_usage=3000, viscosity_100c=10)
        self.sample(on=date(2026, 6, 5), fluid_usage=9000, viscosity_100c=8)
        trend = [t for t in trends(self.asset, compartment=Compartment.ENGINE_OIL)
                 if t.analyte == "viscosity_100c"][0]
        self.assertIsNone(trend.latest.rate)
        # 8/10, not 8/9000 against 10/3000.
        self.assertEqual(trend.change, Decimal("0.80"))


class SeriesTests(Fixture):
    """A differential tells you nothing about an engine."""

    def test_compartments_are_separate_series(self):
        self.sample(on=date(2026, 1, 5), fluid_usage=3000, iron=10)
        self.sample(
            on=date(2026, 2, 5), fluid_usage=30000,
            compartment=Compartment.DIFFERENTIAL, position="rear", iron=400,
        )
        engine = trends(self.asset, compartment=Compartment.ENGINE_OIL)
        self.assertEqual(len(engine[0].points), 1)
        self.assertEqual(engine[0].points[0].value, Decimal("10.0000"))

    def test_two_differentials_are_two_series(self):
        for position in ("front", "rear"):
            self.sample(
                on=date(2026, 1, 5), fluid_usage=30000,
                compartment=Compartment.DIFFERENTIAL, position=position, iron=100,
            )
        keys = [key for key, _rows in series(self.asset)]
        self.assertIn((Compartment.DIFFERENTIAL, "front"), keys)
        self.assertIn((Compartment.DIFFERENTIAL, "rear"), keys)

    def test_points_come_back_oldest_first(self):
        self.sample(on=date(2026, 6, 5), fluid_usage=3000, iron=30)
        self.sample(on=date(2026, 1, 5), fluid_usage=3000, iron=10)
        trend = trends(self.asset, compartment=Compartment.ENGINE_OIL)[0]
        self.assertEqual([p.on for p in trend.points], [date(2026, 1, 5), date(2026, 6, 5)])

    def test_wear_metals_are_shown_before_the_additive_pack(self):
        """The order somebody reads a report in, not alphabetical."""
        self.sample(on=date(2026, 1, 5), fluid_usage=3000, zinc=800, iron=10, water=0.1)
        kinds = [t.kind for t in trends(self.asset, compartment=Compartment.ENGINE_OIL)]
        self.assertEqual(kinds, [analytes.WEAR_METAL, analytes.CONTAMINANT, analytes.ADDITIVE])


class PasteTests(TestCase):
    """A panel is thirty numbers. Thirty boxes is a form nobody fills twice."""

    def test_a_plain_panel(self):
        lines = parse_results("Iron 24\nCopper 6\nAluminum 3\n")
        self.assertEqual([line.analyte for line in lines], ["iron", "copper", "aluminum"])
        self.assertEqual(lines[0].value, Decimal("24"))
        self.assertEqual(lines[0].unit, "ppm")

    def test_element_symbols_are_understood(self):
        self.assertEqual(parse_results("Fe 24")[0].analyte, "iron")
        self.assertEqual(parse_results("Pb: 3")[0].analyte, "lead")

    def test_a_unit_on_the_line_wins_over_the_default(self):
        line = parse_results("Moisture 45 ppm")[0]
        self.assertEqual(line.unit, "ppm")

    def test_a_thousands_separator_is_not_a_decimal_point(self):
        """`1,240` is 1240 ppm of calcium, not 1.24."""
        self.assertEqual(parse_results("Calcium 1,240")[0].value, Decimal("1240"))

    def test_a_below_detection_reading_is_kept_as_its_floor(self):
        self.assertEqual(parse_results("Silver <1")[0].value, Decimal("1"))

    def test_a_viscosity_heading_is_matched_before_a_bare_one_could_be(self):
        line = parse_results("Viscosity @ 100C 10.9")[0]
        self.assertEqual(line.analyte, "viscosity_100c")
        self.assertEqual(line.value, Decimal("10.9"))

    def test_the_labs_own_average_is_kept_beside_the_value(self):
        line = parse_results("Iron 24 ppm (avg 18)")[0]
        self.assertEqual(line.value, Decimal("24"))
        self.assertEqual(line.reference, Decimal("18"))

    def test_an_unknown_element_is_kept_rather_than_discarded(self):
        """A registry that has not heard of it is our shortcoming, not their
        data's."""
        line = parse_results("Unobtainium 7")[0]
        self.assertTrue(line.ok)
        self.assertEqual(line.analyte, "unobtainium")

    def test_a_line_with_no_number_comes_back_as_a_problem(self):
        lines = parse_results("Iron 24\nlooks fine to me\n")
        self.assertTrue(lines[0].ok)
        self.assertFalse(lines[1].ok)
        self.assertIn("number", lines[1].problem)

    def test_a_column_heading_is_not_mistaken_for_a_result(self):
        lines = parse_results("Element  Value\nIron 24")
        self.assertEqual(len(lines), 1)

    def test_the_same_analyte_twice_is_refused_rather_than_overwritten(self):
        """The unique constraint would raise; this says which line caused it."""
        lines = parse_results("Iron 24\nFe 30")
        self.assertTrue(lines[0].ok)
        self.assertFalse(lines[1].ok)
        self.assertIn("already", lines[1].problem)


class SavingTests(Fixture):
    def test_a_pasted_panel_lands_as_rows(self):
        sample = self.sample(on=date(2026, 1, 5), fluid_usage=3000)
        saved = save_results(sample, parse_results("Iron 24\nCopper 6\nnonsense\n"))
        self.assertEqual(saved, 2)
        self.assertEqual(sample.results.count(), 2)

    def test_saving_again_replaces_rather_than_duplicates(self):
        """Correcting a typo must not leave both readings on the record."""
        sample = self.sample(on=date(2026, 1, 5), fluid_usage=3000)
        save_results(sample, parse_results("Iron 24"))
        save_results(sample, parse_results("Iron 42"))
        self.assertEqual(sample.results.count(), 1)
        self.assertEqual(sample.results.get().value, Decimal("42"))


class NoVerdictTests(Fixture):
    """§7.9a — the lab judges the engine; this application does arithmetic."""

    def test_the_labs_comment_is_stored_as_written(self):
        sample = FluidSample.objects.create(
            asset=self.asset,
            sampled_on=date(2026, 1, 5),
            lab="Blackstone",
            lab_comment="Iron is up but nothing here worries us. Try 6,000 miles.",
        )
        sample.refresh_from_db()
        self.assertIn("nothing here worries us", sample.lab_comment)

    def test_nothing_on_the_model_computes_pass_or_fail(self):
        """Held down deliberately: a threshold nobody set is a threshold
        nobody can defend, and limits are engine-specific."""
        forbidden = {"status", "verdict", "severity", "is_ok", "passes", "overall"}
        present = {field.name for field in FluidSample._meta.get_fields()}
        present |= {field.name for field in FluidResult._meta.get_fields()}
        self.assertEqual(forbidden & present, set())

    def test_the_summary_talks_about_the_previous_sample_only(self):
        self.sample(on=date(2026, 1, 5), fluid_usage=3000, iron=10)
        self.sample(on=date(2026, 6, 5), fluid_usage=3000, iron=30)
        summary = trends(self.asset, compartment=Compartment.ENGINE_OIL)[0].summary
        self.assertIn("3×", summary)
        for word in ("normal", "high", "fail", "warning", "critical"):
            self.assertNotIn(word, summary.lower())


class HelperAccessTests(TestCase):
    """§12.2a — the object-level half, which is the one that gets forgotten.

    A sample is a fact about a vehicle, so it travels with the vehicle grant
    rather than being held back with the money screens. Reading one needs a
    grant; recording one needs a grant that can write.
    """

    def setUp(self):
        from homeautoshop.accounts.models import AssetAccess, can

        self.can = can
        self.helper = User.objects.create_user(
            username="sam", password="x" * 16, role=Role.HELPER
        )
        self.mine = Asset.objects.create(nickname="Aero")
        self.theirs = Asset.objects.create(nickname="Barn find")
        AssetAccess.objects.create(user=self.helper, asset=self.mine, level="read")
        self.sample = FluidSample.objects.create(asset=self.mine, sampled_on=date(2026, 1, 5))
        self.client.force_login(self.helper)

    def test_a_read_grant_opens_the_samples_on_that_vehicle(self):
        self.assertTrue(self.can(self.helper, "fluid.read", self.sample))
        self.assertEqual(
            self.client.get(reverse("fluid_list", args=[self.mine.pk])).status_code, 200
        )

    def test_it_does_not_open_a_vehicle_they_were_not_given(self):
        self.assertEqual(
            self.client.get(reverse("fluid_list", args=[self.theirs.pk])).status_code, 403
        )

    def test_a_read_grant_does_not_let_them_record_one(self):
        self.assertFalse(self.can(self.helper, "fluid.edit", self.sample))
        self.assertEqual(
            self.client.get(reverse("fluid_sample_create", args=[self.mine.pk])).status_code,
            403,
        )


class OnTheScreenTests(Fixture):
    def test_the_list_shows_a_vehicles_samples(self):
        sample = self.sample(on=date(2026, 1, 5), fluid_usage=3000, iron=24)
        page = self.client.get(reverse("fluid_list", args=[self.asset.pk])).content.decode()
        self.assertIn(reverse("fluid_sample_detail", args=[sample.pk]), page)
        self.assertIn("Iron", page)

    def test_two_series_get_a_switcher_that_actually_addresses_them(self):
        """Django templates fail silently on a bad lookup, so a compartment
        picker built on a tuple key is exactly where a broken link hides."""
        self.sample(on=date(2026, 1, 5), fluid_usage=3000, iron=10)
        self.sample(
            on=date(2026, 2, 5), fluid_usage=30000,
            compartment=Compartment.DIFFERENTIAL, position="rear", iron=400,
        )
        page = self.client.get(reverse("fluid_list", args=[self.asset.pk])).content.decode()
        self.assertIn("?compartment=engine_oil&amp;position=", page)
        self.assertIn("?compartment=differential&amp;position=rear", page)

    def test_the_switcher_shows_the_series_it_was_asked_for(self):
        self.sample(on=date(2026, 1, 5), fluid_usage=3000, iron=10)
        self.sample(
            on=date(2026, 2, 5), fluid_usage=30000,
            compartment=Compartment.DIFFERENTIAL, position="rear", iron=400,
        )
        page = self.client.get(
            reverse("fluid_list", args=[self.asset.pk]),
            {"compartment": Compartment.ENGINE_OIL, "position": ""},
        ).content.decode()
        # 10 ppm over 3,000 is 3.33 per thousand; the differential's 400 over
        # 30,000 is 13.33, and mixing them would be the whole bug.
        self.assertIn("3.33", page)
        self.assertNotIn("13.33", page)

    def test_a_sample_with_no_interval_is_flagged_on_its_own_page(self):
        sample = self.sample(on=date(2026, 1, 5), iron=24)
        page = self.client.get(
            reverse("fluid_sample_detail", args=[sample.pk])
        ).content.decode()
        self.assertIn("cannot be compared", page)

    def test_recording_a_sample_from_the_form(self):
        response = self.client.post(
            reverse("fluid_sample_create", args=[self.asset.pk]),
            {
                "compartment": Compartment.ENGINE_OIL,
                "sampled_on": "2026-01-05",
                "fluid_usage": "4200",
                "lab": "Blackstone",
                "results_text": "Iron 24\nCopper 6\nViscosity @ 100C 10.9\n",
            },
        )
        self.assertEqual(response.status_code, 302)
        sample = FluidSample.objects.get(asset=self.asset)
        self.assertEqual(sample.results.count(), 3)
        self.assertEqual(sample.fluid_usage, Decimal("4200.00"))

    def test_an_unreadable_line_is_reported_back_rather_than_swallowed(self):
        response = self.client.post(
            reverse("fluid_sample_create", args=[self.asset.pk]),
            {
                "compartment": Compartment.ENGINE_OIL,
                "sampled_on": "2026-01-05",
                "results_text": "Iron 24\nthe oil smelled like fuel\n",
            },
            follow=True,
        )
        text = response.content.decode()
        self.assertIn("the oil smelled like fuel", text)

    def test_editing_without_touching_the_paste_box_keeps_the_results(self):
        """An empty box means 'I am fixing the date', never 'delete the panel'."""
        sample = self.sample(on=date(2026, 1, 5), fluid_usage=3000, iron=24)
        self.client.post(
            reverse("fluid_sample_edit", args=[sample.pk]),
            {
                "compartment": Compartment.ENGINE_OIL,
                "sampled_on": "2026-01-06",
                "fluid_usage": "3000",
                "results_text": "",
            },
        )
        sample.refresh_from_db()
        self.assertEqual(sample.sampled_on, date(2026, 1, 6))
        self.assertEqual(sample.results.count(), 1)

    def test_a_deleted_sample_is_recoverable_from_the_trash(self):
        """The button promises 30 days; the trash has to know the model."""
        sample = self.sample(on=date(2026, 1, 5), fluid_usage=3000, iron=24)
        self.client.post(reverse("fluid_sample_delete", args=[sample.pk]))
        self.assertFalse(FluidSample.objects.filter(pk=sample.pk).exists())
        page = self.client.get(reverse("trash")).content.decode()
        self.assertIn(str(sample.pk), page)

    def test_the_vehicle_page_links_to_it(self):
        page = self.client.get(reverse("asset_detail", args=[self.asset.pk])).content.decode()
        self.assertIn(reverse("fluid_list", args=[self.asset.pk]), page)


class ItHasToBeFindableTests(Fixture):
    """A feature nobody can reach is a feature nobody has.

    The first version put one small link inside the Inspections card, after
    "Measurement trends" — a footnote to a different feature, on a card about
    something else. Everything a vehicle *has* is a button in its own row, and
    a sample is a fact about a vehicle, so that is where it goes. The rest of
    this class holds down the other routes a record has to appear on once it
    exists: a link recorded and never displayed is a link quietly lost.
    """

    def test_the_vehicle_page_offers_it_where_its_other_sections_are(self):
        page = self.client.get(reverse("asset_detail", args=[self.asset.pk])).content.decode()
        row = page[page.index("Diagnostics") : page.index("Diagnostics") + 400]
        self.assertIn(reverse("fluid_list", args=[self.asset.pk]), row)

    def test_a_sample_appears_in_the_vehicles_own_story(self):
        """The timeline is where the vehicle is read. Left off it, a sample
        was a thing you had to already know to go and look for."""
        sample = self.sample(on=date(2026, 1, 5), fluid_usage=3000, iron=24)
        page = self.client.get(
            reverse("asset_timeline", args=[self.asset.pk])
        ).content.decode()

        self.assertIn("Fluid sample", page)
        self.assertIn(reverse("fluid_sample_detail", args=[sample.pk]), page)

    def test_the_timeline_says_it_shows_them(self):
        """The page describes its own contents, so the sentence has to keep up."""
        page = self.client.get(
            reverse("asset_timeline", args=[self.asset.pk])
        ).content.decode()
        self.assertIn("fluid samples", page)

    def test_a_sample_taken_for_a_job_shows_on_that_job(self):
        """The form offers a work order, so the work order has to show what
        chose it — otherwise the field records something nothing can display."""
        from homeautoshop.work.models import WorkOrder

        job = WorkOrder.objects.create(asset=self.asset, title="Diff service")
        sample = self.sample(
            on=date(2026, 1, 5), fluid_usage=30000,
            compartment=Compartment.DIFFERENTIAL, position="rear", iron=400,
        )
        sample.work_order = job
        sample.save()

        page = self.client.get(reverse("work_order_detail", args=[job.pk])).content.decode()
        self.assertIn(reverse("fluid_sample_detail", args=[sample.pk]), page)

    def test_a_job_with_no_samples_grows_no_empty_card(self):
        from homeautoshop.work.models import WorkOrder

        job = WorkOrder.objects.create(asset=self.asset, title="Oil change")
        page = self.client.get(reverse("work_order_detail", args=[job.pk])).content.decode()
        self.assertNotIn('id="fluid-samples"', page)


class TheReportItselfTests(Fixture):
    """The numbers are a transcription. A transcription needs its source.

    Filed against the sample rather than loose on the vehicle: a PDF attached
    to the truck is findable only by remembering which of eleven documents
    belongs to the March sample, and the question it answers — *is this figure
    typed right?* — is asked while looking at the figure.
    """

    def _upload(self, sample, name="blackstone.pdf", body=b"%PDF-1.4 not really",
                content_type="application/pdf"):
        from django.core.files.uploadedfile import SimpleUploadedFile

        return self.client.post(
            reverse("fluid_sample_report", args=[sample.pk]),
            {"files": SimpleUploadedFile(name, body, content_type=content_type)},
        )

    def test_the_report_attaches_to_the_sample(self):
        from homeautoshop.mediafiles.models import MediaLink

        sample = self.sample(on=date(2026, 1, 5), fluid_usage=3000, iron=24)
        self._upload(sample)

        links = MediaLink.for_entity(sample)
        self.assertEqual(links.count(), 1)
        self.assertEqual(links.get().media.original_filename, "blackstone.pdf")

    def test_and_shows_on_the_page_with_a_way_to_open_it(self):
        sample = self.sample(on=date(2026, 1, 5), fluid_usage=3000, iron=24)
        self._upload(sample)

        page = self.client.get(
            reverse("fluid_sample_detail", args=[sample.pk])
        ).content.decode()
        self.assertIn("blackstone.pdf", page)

    def test_a_photographed_printout_is_just_as_good(self):
        """A garage photographs the sheet; a lab emails a PDF. Which kind a
        file becomes is read off the file, not off which box was used."""
        from homeautoshop.mediafiles.models import Media, MediaLink

        sample = self.sample(on=date(2026, 1, 5), fluid_usage=3000, iron=24)
        self._upload(
            sample,
            name="report.jpg",
            body=b"\xff\xd8\xff not really a jpeg",
            content_type="image/jpeg",
        )

        link = MediaLink.for_entity(sample).get()
        self.assertEqual(link.media.kind, Media.Kind.PHOTO)

    def test_a_sample_with_no_report_says_what_is_missing(self):
        sample = self.sample(on=date(2026, 1, 5), fluid_usage=3000, iron=24)
        page = self.client.get(
            reverse("fluid_sample_detail", args=[sample.pk])
        ).content.decode()
        self.assertIn("cannot be checked against anything without it", page)

    def test_uploading_nothing_says_so_rather_than_claiming_success(self):
        sample = self.sample(on=date(2026, 1, 5), fluid_usage=3000, iron=24)
        response = self.client.post(
            reverse("fluid_sample_report", args=[sample.pk]), {}, follow=True
        )
        self.assertIn("Choose a file first", response.content.decode())

    def test_the_report_can_be_taken_off_again(self):
        """Detaching, not deleting (FR-DOC-11)."""
        from homeautoshop.mediafiles.models import MediaLink

        sample = self.sample(on=date(2026, 1, 5), fluid_usage=3000, iron=24)
        self._upload(sample)
        link = MediaLink.for_entity(sample).get()

        self.client.post(reverse("media_unlink", args=[link.pk]))
        self.assertEqual(MediaLink.for_entity(sample).count(), 0)

    def test_a_read_only_helper_cannot_attach_one(self):
        from homeautoshop.accounts.models import AssetAccess

        helper = User.objects.create_user(
            username="sam", password="x" * 16, role=Role.HELPER
        )
        AssetAccess.objects.create(user=helper, asset=self.asset, level="read")
        sample = self.sample(on=date(2026, 1, 5), fluid_usage=3000, iron=24)

        self.client.force_login(helper)
        self.assertEqual(self._upload(sample).status_code, 403)
