"""A stored quantity reads as the number somebody typed (§5.6)."""

from __future__ import annotations

from decimal import Decimal

from django.template import Context, Template
from django.test import TestCase
from django.urls import reverse

from homeautoshop.accounts.models import User
from homeautoshop.assets.models import Asset
from homeautoshop.parts.models import Part, PartUsage


class TheQuantityFilterTests(TestCase):
    def render(self, value) -> str:
        return Template("{% load quantities %}{{ v|quantity }}").render(Context({"v": value}))

    def test_a_whole_number_loses_its_decimals(self):
        self.assertEqual(self.render(Decimal("1.000")), "1")

    def test_a_fraction_keeps_only_what_it_needs(self):
        self.assertEqual(self.render(Decimal("2.500")), "2.5")

    def test_ten_is_not_written_in_scientific_notation(self):
        """`normalize()` alone turns 10.000 into 1E+1."""
        self.assertEqual(self.render(Decimal("10.000")), "10")

    def test_nothing_renders_as_nothing(self):
        self.assertEqual(self.render(None), "")


class ThePagesThatPrintQuantitiesTests(TestCase):
    """The Python sites went through `format_quantity`; these templates printed
    the column bare, so `1.000` survived the fix that was meant to remove it."""

    def setUp(self):
        self.user = User.objects.create_user("andy", password="correct-horse-battery")
        self.client.force_login(self.user)
        self.asset = Asset.objects.create(nickname="Truck")
        self.part = Part.objects.create(name="Caliper")
        PartUsage.objects.create(part=self.part, qty=1, asset=self.asset, unit_cost_minor=1500)

    def test_the_part_page(self):
        page = self.client.get(reverse("part_detail", args=[self.part.pk])).content.decode()
        self.assertNotIn("1.000", page)
