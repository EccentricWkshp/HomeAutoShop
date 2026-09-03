"""
What a lab measures, and which of it means anything over time (SPEC §7.9a).

An oil analysis report is thirty numbers in four different kinds, and treating
them alike is the mistake that makes a trend chart worse than no trend chart:

* **Wear metals accumulate.** Iron enters the oil as the engine wears, and
  keeps entering. Twenty-four ppm is a different statement about the engine at
  3,000 miles on the oil than at 9,000, so the only comparable form is a
  *rate* — parts per million per thousand miles of oil life.
* **Contaminants accumulate too**, and for the same reason are rates. Silicon
  is dirt getting past the filter; sodium and potassium are usually coolant.
* **Additives deplete.** Zinc, phosphorus and calcium are put in by the
  blender and are consumed. Expressing them per thousand miles would invert
  the meaning — a falling number would read as an improving rate.
* **Properties are states, not totals.** Viscosity, TBN and flashpoint
  describe the oil as it is now. There is no such thing as viscosity per
  thousand miles.

So each analyte carries `accumulates`, and only the ones that do are ever
normalised. That single flag is the difference between a report somebody can
read and a chart that quietly lies about half its rows.

`unit` here is a **default**, not a constraint. A lab that reports viscosity at
40 °C in cSt and another that reports it in SUS both get stored with the unit
the report used; this is what to assume when a pasted line does not say.

Anything a lab reports that is not on this list is still recordable — the
analyte is a string, not a foreign key, and an unrecognised one is kept with
whatever unit came with it. A registry that refused unknown rows would lose
data to protect a chart.
"""

from __future__ import annotations

from dataclasses import dataclass

from django.utils.translation import gettext_lazy as _

WEAR_METAL = "wear_metal"
CONTAMINANT = "contaminant"
ADDITIVE = "additive"
PROPERTY = "property"

KIND_LABELS = {
    WEAR_METAL: _("Wear metals"),
    CONTAMINANT: _("Contamination"),
    ADDITIVE: _("Additives"),
    PROPERTY: _("Physical properties"),
}


@dataclass(frozen=True, slots=True)
class Analyte:
    slug: str
    label: object
    unit: str
    kind: str
    #: True where the value is a running total that a longer interval makes
    #: larger on its own. Only these are ever expressed as a rate.
    accumulates: bool
    #: Other names the same measurement appears under on a report, matched
    #: when a pasted line is read. Element symbols included, because half the
    #: labs print `Fe` and the other half print `Iron`.
    aliases: tuple[str, ...] = ()


REGISTRY: tuple[Analyte, ...] = (
    # -- wear metals ------------------------------------------------------
    Analyte("iron", _("Iron"), "ppm", WEAR_METAL, True, ("fe",)),
    Analyte("chromium", _("Chromium"), "ppm", WEAR_METAL, True, ("cr",)),
    Analyte("nickel", _("Nickel"), "ppm", WEAR_METAL, True, ("ni",)),
    Analyte("aluminum", _("Aluminum"), "ppm", WEAR_METAL, True, ("al", "aluminium")),
    Analyte("copper", _("Copper"), "ppm", WEAR_METAL, True, ("cu",)),
    Analyte("lead", _("Lead"), "ppm", WEAR_METAL, True, ("pb",)),
    Analyte("tin", _("Tin"), "ppm", WEAR_METAL, True, ("sn",)),
    Analyte("silver", _("Silver"), "ppm", WEAR_METAL, True, ("ag",)),
    Analyte("titanium", _("Titanium"), "ppm", WEAR_METAL, True, ("ti",)),
    Analyte("vanadium", _("Vanadium"), "ppm", WEAR_METAL, True, ("v",)),
    Analyte("manganese", _("Manganese"), "ppm", WEAR_METAL, True, ("mn",)),
    # -- contamination ----------------------------------------------------
    Analyte("silicon", _("Silicon"), "ppm", CONTAMINANT, True, ("si", "dirt")),
    Analyte("sodium", _("Sodium"), "ppm", CONTAMINANT, True, ("na",)),
    Analyte("potassium", _("Potassium"), "ppm", CONTAMINANT, True, ("k",)),
    # Concentrations, not totals: an oil is 0.3% water or it is not, and the
    # figure does not climb with the interval the way iron does.
    Analyte("water", _("Water"), "%", CONTAMINANT, False, ("h2o",)),
    Analyte("fuel", _("Fuel dilution"), "%", CONTAMINANT, False, ("fuel dilution",)),
    Analyte("glycol", _("Glycol"), "%", CONTAMINANT, False, ("antifreeze", "coolant")),
    Analyte("soot", _("Soot"), "%", CONTAMINANT, False, ()),
    Analyte("insolubles", _("Insolubles"), "%", CONTAMINANT, False, ()),
    # -- additives, which deplete -----------------------------------------
    Analyte("molybdenum", _("Molybdenum"), "ppm", ADDITIVE, False, ("mo",)),
    Analyte("boron", _("Boron"), "ppm", ADDITIVE, False, ("b",)),
    Analyte("magnesium", _("Magnesium"), "ppm", ADDITIVE, False, ("mg",)),
    Analyte("calcium", _("Calcium"), "ppm", ADDITIVE, False, ("ca",)),
    Analyte("barium", _("Barium"), "ppm", ADDITIVE, False, ("ba",)),
    Analyte("phosphorus", _("Phosphorus"), "ppm", ADDITIVE, False, ("p",)),
    Analyte("zinc", _("Zinc"), "ppm", ADDITIVE, False, ("zn",)),
    # -- physical properties ----------------------------------------------
    Analyte("viscosity_100c", _("Viscosity at 100 °C"), "cSt", PROPERTY, False,
            ("viscosity @ 100c", "visc 100", "vis 100c", "sus viscosity @ 210f")),
    Analyte("viscosity_40c", _("Viscosity at 40 °C"), "cSt", PROPERTY, False,
            ("viscosity @ 40c", "visc 40", "vis 40c")),
    Analyte("flashpoint", _("Flashpoint"), "°F", PROPERTY, False, ("flash point",)),
    Analyte("tbn", _("Total base number"), "mg KOH/g", PROPERTY, False,
            ("total base number", "base number")),
    Analyte("tan", _("Total acid number"), "mg KOH/g", PROPERTY, False,
            ("total acid number", "acid number")),
    Analyte("oxidation", _("Oxidation"), "Abs/cm", PROPERTY, False, ()),
    Analyte("nitration", _("Nitration"), "Abs/cm", PROPERTY, False, ()),
    Analyte("ph", _("pH"), "", PROPERTY, False, ()),
    Analyte("moisture", _("Moisture"), "ppm", CONTAMINANT, False, ("karl fischer",)),
)

BY_SLUG: dict[str, Analyte] = {a.slug: a for a in REGISTRY}

#: Every name a pasted line might arrive under.
#:
#: Built from the **slug and the aliases only**, never from `label`. A label is
#: a translated string, and forcing one at import would key this table on
#: whichever language happened to be active — so a French-Canadian instance
#: would match a different set of words than an American one, for reports that
#: are in English either way. Every English name a lab actually prints is
#: therefore written down explicitly above, where it can be seen and added to.
_LOOKUP: dict[str, Analyte] = {}
for _analyte in REGISTRY:
    _LOOKUP[_analyte.slug] = _analyte
    _LOOKUP[_analyte.slug.replace("_", " ")] = _analyte
    for _alias in _analyte.aliases:
        _LOOKUP.setdefault(_alias, _analyte)


def find(name: str) -> Analyte | None:
    """The analyte a report line is naming, or `None` if it is not one we know."""
    key = " ".join(name.strip().lower().replace("_", " ").split())
    if key in _LOOKUP:
        return _LOOKUP[key]
    # `Iron (Fe)` and `Viscosity @ 100°C (cSt)` are both common headings.
    stripped = key.split("(")[0].strip()
    return _LOOKUP.get(stripped)


def label_for(slug: str) -> str:
    """The display name, falling back to whatever the operator recorded."""
    analyte = BY_SLUG.get(slug)
    return str(analyte.label) if analyte else slug.replace("_", " ")


def accumulates(slug: str) -> bool:
    """Unknown analytes do **not** accumulate.

    The safe default: an unknown row is shown as the number the lab printed
    rather than converted into a rate whose meaning nobody has checked.
    """
    analyte = BY_SLUG.get(slug)
    return bool(analyte and analyte.accumulates)


def kind_of(slug: str) -> str:
    analyte = BY_SLUG.get(slug)
    return analyte.kind if analyte else PROPERTY


#: The order sections are shown in — the reason somebody sent the sample first.
KIND_ORDER = (WEAR_METAL, CONTAMINANT, PROPERTY, ADDITIVE)
