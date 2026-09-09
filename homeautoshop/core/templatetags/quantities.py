"""`{{ usage.qty|quantity }}` — a stored quantity, written as somebody typed it.

The first template tag library in the project, and it exists for one reason:
every quantity column is a `Decimal` stored to three places, and a `Decimal`
rendered bare prints its coefficient — `1.000`, `12.000`. The Python sites that
built strings from these went through `format_quantity`; the templates that
printed the column directly did not, so a part's "used on" list still read
`1.000` after the fix that was supposed to have removed it.
"""

from django import template

from homeautoshop.core.measurements import format_quantity

register = template.Library()


@register.filter
def quantity(value) -> str:
    """`1` and `1.5`, never `1.000` — see `format_quantity` for the arithmetic."""
    if value is None or value == "":
        return ""
    return format_quantity(value)
