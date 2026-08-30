"""
Unique constraints must agree with soft deletion (SPEC §5.4).

A soft delete leaves the row where it is. A plain unique constraint therefore
keeps enforcing uniqueness against records the application has already stopped
showing anyone — so deleting a thing and adding the same thing back fails, at
the database, as a 500 on a sequence any user would expect to work.

Seven models shipped with this. It stayed hidden because nothing in the test
suite deleted a uniquely-constrained row and then recreated it, which is
exactly the kind of gap a per-model test does not close and an invariant does.
"""

from __future__ import annotations

from django.apps import apps
from django.db import IntegrityError
from django.db.models import UniqueConstraint
from django.test import TestCase

from homeautoshop.assets.models import Asset, AssetSpec
from homeautoshop.core.models import BaseModel


class ConstraintInvariantTests(TestCase):
    def test_every_unique_constraint_on_a_soft_deleted_model_ignores_the_dead(self):
        offenders = []
        for model in apps.get_models():
            if not issubclass(model, BaseModel):
                continue
            for constraint in model._meta.constraints:
                if isinstance(constraint, UniqueConstraint) and constraint.condition is None:
                    offenders.append(f"{model._meta.label}.{constraint.name}")
        self.assertEqual(
            offenders,
            [],
            "These enforce uniqueness against soft-deleted rows, so deleting a "
            "record and adding it back raises IntegrityError. Add "
            "condition=models.Q(deleted_at__isnull=True).",
        )


class SpecLifecycleTests(TestCase):
    """The case that was reported: adding a spec that already exists."""

    def setUp(self):
        self.asset = Asset.objects.create(nickname="Red truck")

    def _spec(self, **kwargs):
        return AssetSpec.objects.create(
            asset=self.asset, group="access", name="Door Code", value="1234", **kwargs
        )

    def test_a_deleted_spec_does_not_block_adding_it_again(self):
        first = self._spec()
        first.delete()

        second = self._spec()  # would have raised IntegrityError
        self.assertNotEqual(first.pk, second.pk)
        self.assertEqual(AssetSpec.objects.filter(asset=self.asset).count(), 1)

    def test_two_live_specs_with_the_same_key_are_still_refused(self):
        """The constraint has to keep doing its job for records that are here."""
        self._spec()
        with self.assertRaises(IntegrityError):
            self._spec()

    def test_the_same_name_under_a_different_condition_is_a_different_spec(self):
        self._spec()
        cold = AssetSpec.objects.create(
            asset=self.asset, group="access", name="Door Code", value="5678", condition="cold"
        )
        self.assertEqual(AssetSpec.objects.filter(name="Door Code").count(), 2)
        self.assertEqual(cold.value, "5678")
