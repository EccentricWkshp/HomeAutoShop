"""
Typing money the way it is written (SPEC §5.5, §5.6).

Storage is not the problem and does not change: every amount stays an integer
number of minor units beside its ISO-4217 code, because that is the only
representation that survives arithmetic. The problem was that the *form* made
that the operator's problem too. A receipt says $442.13 and the box wanted
44213, with the label "(minor units)" as the only warning — so the natural
thing to type produced a four-dollar purchase, silently and plausibly.

So the conversion happens at the edge, in one field used by every money input,
rather than in each form. `prepare_value` turns what is stored into what is
shown; `to_python` turns what was typed into what is stored. Nothing in
between ever sees a float.

Parsing goes through Babel, which is already here for formatting. It is worth
the indirection because a French-Canadian instance writes the same amount as
`1 234,56`, and a rule written by hand would either reject it or read it as
one and a bit.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

from django import forms
from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _

from .measurements import Money, format_money, minor_units

#: Decoration rather than digits: currency symbols, spaces, stray letters.
#: The separators are kept, because which one is the decimal point is a
#: question about locale that Babel answers below.
DECORATION = re.compile(r"[^\d.,\-]")


def parse_amount(value, currency: str = "USD") -> int:
    """`$1,234.56` to 123456. Raises ValidationError on anything else."""
    text = DECORATION.sub("", str(value).strip())
    if not text or text in ("-", ".", ","):
        raise ValidationError(_("Enter an amount, like 12.34."), code="invalid")

    number: Decimal | None = None
    try:
        from babel.numbers import parse_decimal
        from django.utils.translation import get_language

        locale = (get_language() or "en_US").replace("-", "_")
        number = parse_decimal(text, locale=locale, strict=False)
    except Exception:
        # Babel missing, or a locale it does not know. A plain decimal still
        # covers the overwhelmingly common `1234.56`.
        try:
            number = Decimal(text.replace(",", ""))
        except InvalidOperation:
            number = None

    if number is None:
        raise ValidationError(_("Enter an amount, like 12.34."), code="invalid")
    return Money.from_decimal(number, currency).amount


class MoneyFormField(forms.Field):
    """An amount in the currency, stored as minor units."""

    def __init__(self, *, currency: str = "USD", **kwargs):
        self.currency = (currency or "USD").upper()
        super().__init__(**kwargs)
        # Text rather than `type=number`: a number input silently submits
        # nothing when its contents do not parse, so pasting `$442.13` off a
        # receipt would clear the field instead of explaining itself.
        # `inputmode` still gets the numeric keypad on a phone.
        self.widget.attrs.setdefault("inputmode", "decimal")
        self.widget.attrs.setdefault("autocomplete", "off")
        self.widget.attrs.setdefault("class", "input")
        self.widget.attrs.setdefault("placeholder", format_money(Money(0, self.currency)))

    def prepare_value(self, value):
        """Stored to shown. Leaves a rejected entry exactly as it was typed."""
        if value is None or value == "" or isinstance(value, bool):
            return value
        if isinstance(value, int):
            return Money(value, self.currency).to_decimal()
        return value

    def to_python(self, value):
        """Shown to stored."""
        if value in self.empty_values:
            return None
        return parse_amount(value, self.currency)


def label_without_minor(label) -> str:
    """`Tax minor` and `Tax (minor units)` are both just `Tax` now."""
    text = str(label or "")
    text = re.sub(r"\s*\(minor units\)\s*$", "", text, flags=re.I)
    text = re.sub(r"\s+minor\s*$", "", text, flags=re.I)
    return text


class MoneyFormMixin:
    """Every `*_minor` field on the form is typed in the currency.

    Swapped in at form level rather than declared field by field, so that the
    next model to grow a money column is right without anyone remembering this
    file exists.
    """

    #: Used when the record does not carry its own — a new one, or a model
    #: where the currency lives on the parent.
    money_currency = "USD"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        instance = getattr(self, "instance", None)
        currency = getattr(instance, "currency", "") or self.money_currency
        for name, field in list(self.fields.items()):
            if not name.endswith("_minor"):
                continue
            self.fields[name] = MoneyFormField(
                currency=currency,
                required=field.required,
                label=label_without_minor(field.label),
                help_text=field.help_text,
                initial=field.initial,
            )


def money_step(currency: str = "USD") -> str:
    """The smallest amount that exists in this currency, for a `step` attr."""
    places = minor_units(currency)
    return "1" if places == 0 else "0." + "0" * (places - 1) + "1"
