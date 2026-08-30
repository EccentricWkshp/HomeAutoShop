"""
Money on models (SPEC §5.5).

Every monetary column is a pair — integer **minor units** plus an ISO-4217 code,
**per transaction**. Never a float, and never one instance-wide currency: a part
bought from a German vendor keeps its EUR price, and a rollup converts using the
rate snapshotted at the time rather than today's.

Declaring the pair by hand on each model would be repetitive and easy to get
subtly wrong, so `MoneyField.add_to` builds both columns and the accessor.
"""

from __future__ import annotations

from decimal import Decimal

from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _

from .measurements import Money, minor_units


def money_columns(name: str, *, verbose_name=None, null: bool = False, default: int | None = 0):
    """Return the (amount, currency) field pair for `name`.

    Usage:

        subtotal_minor, subtotal_currency = money_columns("subtotal")
    """
    amount = models.BigIntegerField(
        verbose_name=verbose_name or name.replace("_", " "),
        null=null,
        blank=null,
        default=None if null else default,
        help_text=_("Minor units (e.g. cents). Never a float."),
    )
    currency = models.CharField(max_length=3, default="USD", blank=True)
    return amount, currency


class MoneyAccessor:
    """A `Money` view over a `<name>_minor` / `<name>_currency` column pair."""

    def __init__(self, name: str) -> None:
        self.amount_attr = f"{name}_minor"
        self.currency_attr = f"{name}_currency"

    def __get__(self, obj, objtype=None):
        if obj is None:
            return self
        amount = getattr(obj, self.amount_attr)
        if amount is None:
            return None
        return Money(amount, getattr(obj, self.currency_attr) or default_currency())

    def __set__(self, obj, value) -> None:
        if value is None:
            setattr(obj, self.amount_attr, None)
            return
        if isinstance(value, Money):
            setattr(obj, self.amount_attr, value.amount)
            setattr(obj, self.currency_attr, value.currency)
            return
        # A bare number is interpreted in the row's currency, which is what a
        # form field submits.
        currency = getattr(obj, self.currency_attr) or default_currency()
        setattr(obj, self.amount_attr, Money.from_decimal(value, currency).amount)
        setattr(obj, self.currency_attr, currency)


def money(name: str) -> MoneyAccessor:
    return MoneyAccessor(name)


def default_currency() -> str:
    return getattr(settings, "CURRENCY_REPORTING", "USD")


def to_minor(value, currency: str | None = None) -> int:
    return Money.from_decimal(value, currency or default_currency()).amount


def to_decimal(amount: int | None, currency: str | None = None) -> Decimal:
    if amount is None:
        return Decimal(0)
    return Decimal(amount).scaleb(-minor_units(currency or default_currency()))


def total(*amounts: Money | None) -> Money:
    """Sum amounts that share a currency.

    Refuses to add across currencies rather than guessing a rate — a rollup
    that silently converts is a rollup nobody can audit.
    """
    present = [a for a in amounts if a is not None]
    if not present:
        return Money(0, default_currency())
    result = present[0]
    for item in present[1:]:
        result = result + item
    return result
