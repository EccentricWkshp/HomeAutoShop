"""What a vehicle's card on the board is allowed to say, and what colour it is.

The board — the Vehicles screen, and the Fleet panel on the dashboard — is the
one screen somebody looks at to find a vehicle rather than to read one. So the
question it has to answer is not "what is true about this truck" but "which of
these six cards is the truck I mean", and the answer differs per household: one
shop recognises a vehicle by its plate, another by its photo, another by the
fact that it is the one with three codes standing against it.

Hence pins. The card carries the nickname and whatever else has been pinned to
it, chosen per vehicle, because a mower and a truck do not read alike — engine
hours and a serial number identify one, a plate identifies the other, and
"plate" on a generator is a row that can only ever be empty.

**The nickname is not a pin.** It is the card's heading and the link's
accessible name, so a card with it switched off would be an unlabelled link to
somewhere — which is the one arrangement that has to stay impossible (§9.5).
Everything else on the card is the operator's to choose.
"""

from __future__ import annotations

from dataclasses import dataclass

from django.utils.translation import gettext_lazy as _

from .models import AssetKind


@dataclass(frozen=True)
class Pin:
    """One thing a card can be told to show."""

    key: str
    label: str
    #: Blank when both kinds have it. Only VIN and plate are genuinely
    #: vehicle-only — `Asset.clean` refuses them on equipment — so only those
    #: two are gated. A pin that has no value on this particular machine simply
    #: renders nothing, which is the right answer for "engine" on a trailer
    #: without needing a rule about trailers.
    kind: str = ""


PINS: tuple[Pin, ...] = (
    Pin("descriptor", _("Year, make and model")),
    Pin("status", _("Status")),
    Pin("photo", _("Photo")),
    Pin("meter", _("Current meter")),
    Pin("vin", _("VIN"), AssetKind.VEHICLE),
    Pin("plate", _("Plate")),
    Pin("engine", _("Engine")),
    Pin("owner", _("Owner")),
    Pin("driver", _("Primary driver")),
    Pin("schedule", _("What is coming due")),
    Pin("work_orders", _("Open work orders")),
    Pin("codes", _("Open trouble codes")),
)

PINS_BY_KEY = {pin.key: pin for pin in PINS}

#: A card nobody has configured. The first three are what every card showed
#: before pinning existed, so an upgrade changes nothing anybody had; the
#: fourth is the addition — what is coming due is the question the board is
#: most often opened to answer, and it was previously a page away on each
#: vehicle in turn.
DEFAULT_PINS: tuple[str, ...] = ("descriptor", "status", "meter", "schedule")


def valid_pins(keys, *, kind: str = "") -> list[str]:
    """Filter submitted or stored keys down to ones that exist and apply.

    Order follows `PINS`, not the caller's, so the card's layout is stable: two
    vehicles pinned with the same facts read the same way down the column, and
    the order checkboxes happened to be submitted in is not a layout decision
    anybody meant to make.
    """
    wanted = set(keys or ())
    return [
        pin.key
        for pin in PINS
        if pin.key in wanted and (not pin.kind or not kind or pin.kind == kind)
    ]


#: Card colours, as keys — the values live in `app.css`, one custom property
#: per key, so light and dark each get a tint that is legible in that theme.
#:
#: Decoration, never encoding (§9.5). Nothing on the card means anything by
#: being blue; the status pill, the due badges and the code count each carry
#: their own words. Somebody who cannot distinguish these loses a recognition
#: aid and no information.
COLORS: tuple[tuple[str, str], ...] = (
    ("", _("None")),
    ("red", _("Red")),
    ("orange", _("Orange")),
    ("yellow", _("Yellow")),
    ("green", _("Green")),
    ("teal", _("Teal")),
    ("blue", _("Blue")),
    ("violet", _("Violet")),
    ("pink", _("Pink")),
    ("slate", _("Slate")),
)

COLOR_KEYS = frozenset(key for key, _label in COLORS)
