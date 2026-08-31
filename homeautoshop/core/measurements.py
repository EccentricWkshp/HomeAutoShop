"""
Measurements, money, and locale formatting (SPEC §5.5, §5.6).

The governing rule: **store as entered, plus a canonical column for comparison.**
Round-tripping 87,432 mi must never display 87,431, so the entered value and
unit are both kept and the canonical value exists only for sorting, comparison,
and interval arithmetic.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from django.utils.translation import gettext_lazy as _

# Canonical bases, per SPEC §5.5.
CANONICAL = {
    "distance": "km",
    "volume": "L",
    "mass": "kg",
    "torque": "N·m",
    "pressure": "kPa",
    "temperature": "°C",
    "duration": "hours",
}

# Multiplicative conversions to the canonical base. Temperature is affine and
# handled separately.
_TO_CANONICAL: dict[str, tuple[str, Decimal]] = {
    "km": ("distance", Decimal(1)),
    "mi": ("distance", Decimal("1.609344")),
    "m": ("distance", Decimal("0.001")),
    # Parts are sold by the foot — hose, wire, weatherstrip — so length has to
    # be a dimension parts can be measured in, not only one odometers use.
    "ft": ("distance", Decimal("0.0003048")),
    "in": ("distance", Decimal("0.0000254")),
    "L": ("volume", Decimal(1)),
    "ml": ("volume", Decimal("0.001")),
    "qt": ("volume", Decimal("0.946352946")),
    "gal": ("volume", Decimal("3.785411784")),
    "floz": ("volume", Decimal("0.0295735295625")),
    "kg": ("mass", Decimal(1)),
    "g": ("mass", Decimal("0.001")),
    "lb": ("mass", Decimal("0.45359237")),
    "oz": ("mass", Decimal("0.028349523125")),
    "N·m": ("torque", Decimal(1)),
    "nm": ("torque", Decimal(1)),
    "ft-lb": ("torque", Decimal("1.3558179483314004")),
    "in-lb": ("torque", Decimal("0.1129848290276167")),
    "kPa": ("pressure", Decimal(1)),
    "psi": ("pressure", Decimal("6.894757293168361")),
    "bar": ("pressure", Decimal(100)),
    "hours": ("duration", Decimal(1)),
    "h": ("duration", Decimal(1)),
    "min": ("duration", Decimal(1) / Decimal(60)),
}

DISTANCE_UNITS = ("mi", "km")


class UnknownUnitError(ValueError):
    pass


def dimension_of(unit: str) -> str:
    if unit in ("°C", "°F"):
        return "temperature"
    try:
        return _TO_CANONICAL[unit][0]
    except KeyError as exc:
        raise UnknownUnitError(f"unknown unit {unit!r}") from exc


def to_canonical(value: Decimal | float | int | str, unit: str) -> Decimal:
    """Convert to the canonical base for the unit's dimension."""
    value = Decimal(str(value))
    if unit == "°C":
        return value
    if unit == "°F":
        return (value - 32) * Decimal(5) / Decimal(9)
    try:
        _, factor = _TO_CANONICAL[unit]
    except KeyError as exc:
        raise UnknownUnitError(f"unknown unit {unit!r}") from exc
    return value * factor


def from_canonical(value: Decimal | float | int | str, unit: str) -> Decimal:
    """Inverse of :func:`to_canonical`."""
    value = Decimal(str(value))
    if unit == "°C":
        return value
    if unit == "°F":
        return value * Decimal(9) / Decimal(5) + 32
    try:
        _, factor = _TO_CANONICAL[unit]
    except KeyError as exc:
        raise UnknownUnitError(f"unknown unit {unit!r}") from exc
    return value / factor


def convert(value, from_unit: str, to_unit: str) -> Decimal:
    if dimension_of(from_unit) != dimension_of(to_unit):
        raise UnknownUnitError(f"cannot convert {from_unit!r} to {to_unit!r}")
    return from_canonical(to_canonical(value, from_unit), to_unit)


def distance_unit_for(units_preference: str) -> str:
    """The distance unit an operator expects, given their units preference."""
    return "mi" if (units_preference or "imperial").lower() == "imperial" else "km"


# --------------------------------------------------------------------------
# Money (SPEC §5.5) — integer minor units plus an ISO-4217 code, per
# transaction. Never floating point, and never one instance-wide currency.
# --------------------------------------------------------------------------

# Currencies whose minor unit is not 1/100. Enough to be correct for the
# common cases rather than a complete ISO-4217 table.
_MINOR_UNITS = {"JPY": 0, "KRW": 0, "CLP": 0, "ISK": 0, "VND": 0, "BHD": 3, "KWD": 3, "OMR": 3, "TND": 3}


def minor_units(currency: str) -> int:
    return _MINOR_UNITS.get((currency or "USD").upper(), 2)


@dataclass(frozen=True, slots=True)
class Money:
    """An amount in minor units, with its currency."""

    amount: int
    currency: str = "USD"

    @classmethod
    def from_decimal(cls, value: Decimal | float | int | str, currency: str = "USD") -> "Money":
        exp = Decimal(10) ** -minor_units(currency)
        quantized = Decimal(str(value)).quantize(exp, rounding=ROUND_HALF_UP)
        return cls(int(quantized.scaleb(minor_units(currency))), currency.upper())

    def to_decimal(self) -> Decimal:
        return Decimal(self.amount).scaleb(-minor_units(self.currency))

    def __add__(self, other: "Money") -> "Money":
        if other.currency != self.currency:
            raise ValueError(
                "refusing to add %s to %s: convert with a snapshotted rate first"
                % (other.currency, self.currency)
            )
        return Money(self.amount + other.amount, self.currency)

    def __str__(self) -> str:
        return format_money(self)


def format_money(money: Money, locale: str | None = None) -> str:
    """Format via CLDR. No hand-rolled currency arithmetic (SPEC §5.6)."""
    from django.utils.translation import get_language

    locale = (locale or get_language() or "en_US").replace("-", "_")
    try:
        from babel.numbers import format_currency

        return format_currency(money.to_decimal(), money.currency, locale=locale)
    except Exception:
        # Never let a formatting failure break a page.
        return f"{money.to_decimal():.2f} {money.currency}"


def format_measurement(value, unit: str, locale: str | None = None) -> str:
    from django.utils.translation import get_language

    locale = (locale or get_language() or "en_US").replace("-", "_")
    try:
        from babel.numbers import format_decimal

        rendered = format_decimal(Decimal(str(value)), locale=locale)
    except Exception:
        rendered = str(value)
    return f"{rendered} {unit}"


UNIT_LABELS = {
    "mi": _("miles"),
    "km": _("kilometres"),
    "hours": _("hours"),
    "cycles": _("cycles"),
    "each": _("each"),
    "L": _("litres"),
    "ml": _("millilitres"),
    "qt": _("quarts"),
    "gal": _("gallons"),
    "floz": _("fluid ounces"),
    "kg": _("kilograms"),
    "g": _("grams"),
    "lb": _("pounds"),
    "oz": _("ounces"),
    "m": _("metres"),
    "ft": _("feet"),
    "in": _("inches"),
}

#: What a part can be measured in, grouped by what it can be converted within.
#: A vendor's units are the vendor's business — R-134a is sold by the pound in
#: cylinders and dispensed by the ounce or the half-kilogram — so the catalogue
#: has to hold both and the arithmetic has to join them up.
PART_UNITS: dict[str, tuple[str, ...]] = {
    "count": ("each",),
    "mass": ("lb", "oz", "kg", "g"),
    "volume": ("qt", "gal", "floz", "L", "ml"),
    "length": ("ft", "in", "m"),
}

#: `dimension_of` calls length "distance", because that is what the canonical
#: table calls it. Parts do not measure distance; they measure length.
_PART_DIMENSION = {"distance": "length"}


def part_dimension(unit: str) -> str:
    """Which group of units this one can be converted within.

    `count` for `each`, which converts to nothing — a thing is a thing, and
    there is no factor between a gasket and a litre.
    """
    if unit == "each" or not unit:
        return "count"
    try:
        found = dimension_of(unit)
    except UnknownUnitError:
        return "count"
    return _PART_DIMENSION.get(found, found)


def compatible_units(unit: str) -> tuple[str, ...]:
    """Every unit a quantity of this part may be entered in."""
    return PART_UNITS.get(part_dimension(unit), ("each",))


def unit_label(unit: str) -> str:
    return UNIT_LABELS.get(unit, unit)
