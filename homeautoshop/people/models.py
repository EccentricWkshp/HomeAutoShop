"""People — owners and contacts (SPEC §7.2).

A person exists independently of any user account: most vehicle owners in a
home shop never sign in.
"""

from __future__ import annotations

from django.db import models
from django.utils.translation import gettext_lazy as _

from homeautoshop.core.models import RevisionedModel


class Person(RevisionedModel):
    display_name = models.CharField(max_length=120)
    given_name = models.CharField(max_length=60, blank=True)
    family_name = models.CharField(max_length=60, blank=True)
    email = models.EmailField(blank=True)
    phone = models.CharField(max_length=40, blank=True)
    address = models.TextField(blank=True)
    notes = models.TextField(blank=True)
    is_household = models.BooleanField(
        default=True, help_text=_("Lives here, as opposed to a friend whose car you work on.")
    )

    class Meta:
        ordering = ["display_name"]
        verbose_name_plural = "people"

    def __str__(self) -> str:
        return self.display_name

    def save(self, *args, **kwargs):
        if not self.display_name:
            self.display_name = " ".join(p for p in (self.given_name, self.family_name) if p) or "Unnamed"
        return super().save(*args, **kwargs)

    def current_assets(self):
        from homeautoshop.assets.models import Asset

        return Asset.objects.filter(
            ownerships__person=self, ownerships__to_date__isnull=True
        ).distinct()

    def former_assets(self):
        """Assets this person once owned and no longer does.

        The exclusion is written as an explicit subquery rather than a second
        `exclude()` on the relation: Django does not guarantee that conditions
        in a multi-valued `exclude()` refer to the same related row, which
        silently returned nothing here.
        """
        from homeautoshop.assets.models import Asset, AssetOwnership

        still_owned = AssetOwnership.objects.filter(person=self, to_date__isnull=True).values("asset_id")
        return (
            Asset.objects.filter(ownerships__person=self, ownerships__to_date__isnull=False)
            .exclude(pk__in=still_owned)
            .distinct()
        )
