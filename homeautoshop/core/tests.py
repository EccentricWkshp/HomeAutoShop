"""
Tests for the core rules (SPEC §11.4: every requirement traces to a test).

These cover the invariants that are cheap to break and expensive to discover:
unit round-trips, money arithmetic, concurrency, and the append-only guarantee.
"""

from __future__ import annotations

from decimal import Decimal

from django.core.exceptions import ValidationError
from django.test import TestCase

from homeautoshop.assets.models import Asset, UsageReading
from homeautoshop.core.measurements import (
    Money,
    UnknownUnitError,
    convert,
    from_canonical,
    to_canonical,
)
from homeautoshop.core.models import StaleRevisionError


class UnitConversionTests(TestCase):
    """SPEC §5.5 — store as entered; canonical is for comparison only."""

    def test_round_trip_preserves_the_entered_value(self):
        # The motivating case from the spec: 87,432 mi must never come back
        # as 87,431 because it was normalized through kilometres.
        for value in (Decimal("87432"), Decimal("1"), Decimal("250000.75")):
            canonical = to_canonical(value, "mi")
            self.assertEqual(from_canonical(canonical, "mi"), value)

    def test_known_conversions(self):
        self.assertAlmostEqual(float(convert(1, "mi", "km")), 1.609344, places=6)
        self.assertAlmostEqual(float(convert(1, "gal", "L")), 3.785412, places=5)
        self.assertAlmostEqual(float(convert(100, "ft-lb", "N·m")), 135.5818, places=3)
        self.assertAlmostEqual(float(convert(32, "psi", "kPa")), 220.632, places=2)

    def test_temperature_is_affine_not_multiplicative(self):
        self.assertAlmostEqual(float(convert(212, "°F", "°C")), 100.0, places=6)
        self.assertAlmostEqual(float(convert(0, "°C", "°F")), 32.0, places=6)

    def test_cross_dimension_conversion_is_refused(self):
        with self.assertRaises(UnknownUnitError):
            convert(1, "mi", "kg")


class MoneyTests(TestCase):
    """SPEC §5.5 — integer minor units, per transaction, never floats."""

    def test_minor_units_by_currency(self):
        self.assertEqual(Money.from_decimal("12.34", "USD").amount, 1234)
        self.assertEqual(Money.from_decimal("1200", "JPY").amount, 1200)
        self.assertEqual(Money.from_decimal("1.234", "KWD").amount, 1234)

    def test_no_float_drift(self):
        total = Money(0, "USD")
        for _ in range(10):
            total = total + Money.from_decimal("0.10", "USD")
        self.assertEqual(total.amount, 100)
        self.assertEqual(total.to_decimal(), Decimal("1.00"))

    def test_mixing_currencies_raises_rather_than_guessing(self):
        with self.assertRaises(ValueError):
            Money(100, "USD") + Money(100, "CAD")


class ConcurrencyTests(TestCase):
    """SPEC §5.4 — mutable entities use optimistic concurrency."""

    def setUp(self):
        self.asset = Asset.objects.create(nickname="Red truck")

    def test_revision_increments_on_save(self):
        self.assertEqual(self.asset.revision, 1)
        self.asset.nickname = "Red pickup"
        self.asset.save()
        self.assertEqual(self.asset.revision, 2)

    def test_stale_write_is_refused_with_both_revisions(self):
        stale = Asset.objects.get(pk=self.asset.pk)
        self.asset.nickname = "Updated first"
        self.asset.save()

        stale.nickname = "Updated second"
        with self.assertRaises(StaleRevisionError) as ctx:
            stale.save(expected_revision=1)
        self.assertEqual(ctx.exception.expected, 1)
        self.assertEqual(ctx.exception.actual, 2)
        # The winning write stands; nothing is silently merged.
        self.assertEqual(Asset.objects.get(pk=self.asset.pk).nickname, "Updated first")

    def test_current_revision_write_succeeds(self):
        fresh = Asset.objects.get(pk=self.asset.pk)
        fresh.nickname = "Fine"
        fresh.save(expected_revision=1)
        self.assertEqual(Asset.objects.get(pk=self.asset.pk).nickname, "Fine")


class AppendOnlyTests(TestCase):
    """SPEC §5.4 — append-only entities cannot conflict, by construction."""

    def setUp(self):
        self.asset = Asset.objects.create(nickname="Mower", asset_kind="equipment")

    def test_editing_an_append_only_row_is_refused(self):
        reading = UsageReading.objects.create(asset=self.asset, value=10, unit="hours")
        reading.value = 999
        with self.assertRaises(ValidationError):
            reading.save()

    def test_soft_delete_is_still_allowed(self):
        reading = UsageReading.objects.create(asset=self.asset, value=10, unit="hours")
        reading.delete()
        self.assertIsNotNone(UsageReading.all_objects.get(pk=reading.pk).deleted_at)
        self.assertFalse(UsageReading.objects.filter(pk=reading.pk).exists())


class SoftDeleteTests(TestCase):
    def test_delete_hides_without_destroying(self):
        asset = Asset.objects.create(nickname="Parts car")
        asset.delete()
        self.assertFalse(Asset.objects.filter(pk=asset.pk).exists())
        self.assertTrue(Asset.all_objects.filter(pk=asset.pk).exists())

    def test_restore(self):
        asset = Asset.objects.create(nickname="Parts car")
        asset.delete()
        Asset.all_objects.get(pk=asset.pk).restore()
        self.assertTrue(Asset.objects.filter(pk=asset.pk).exists())

    def test_queryset_delete_keeps_the_orm_return_shape(self):
        """Callers written against Django's `(count, {label: count})` must work."""
        Asset.objects.create(nickname="A")
        Asset.objects.create(nickname="B")
        count, per_model = Asset.objects.filter(nickname__in=["A", "B"]).delete()
        self.assertEqual(count, 2)
        self.assertEqual(per_model["assets.Asset"], 2)
        self.assertEqual(Asset.objects.count(), 0)
        self.assertEqual(Asset.all_objects.count(), 2)


class UuidTests(TestCase):
    def test_primary_keys_are_time_ordered(self):
        # UUIDv7 sorts by creation time, which is what gives index locality.
        first = Asset.objects.create(nickname="A")
        second = Asset.objects.create(nickname="B")
        self.assertLess(str(first.pk), str(second.pk))
        self.assertEqual(first.pk.version, 7)
