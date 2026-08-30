"""
Presenting what a VIN decode already told us (SPEC §8.1).

A vPIC decode returns 154 fields, of which 30-40 are populated for a typical
vehicle. `services.VPIC_FIELD_MAP` maps eight of them onto columns, because
mapping conservatively is right — inventing a column per vPIC field would be a
schema that tracks someone else's API. The rest was never lost: it is kept
verbatim in `asset.decoded_raw`. It just had nowhere to appear.

This module is that somewhere. It is presentation only: a curated, ordered,
translated view over the stored payload, with no schema and no second source of
truth. A field vPIC stops returning simply stops appearing.

Fields already shown elsewhere on the page — year, make, model, trim, body
style, fuel, transmission, drivetrain, engine — are deliberately absent, so
this reads as *additional* detail rather than a second copy of the header.
"""

from __future__ import annotations

from django.utils.translation import gettext_lazy as _

# vPIC says "Not Applicable" where a variable does not apply to the vehicle,
# and returns bare zeros for numeric fields it has no value for. Neither is
# information, and a spec sheet padded with them is harder to read than a
# short one.
EMPTY_VALUES = {"", "0", "not applicable", "not available", "none", "no"}

GROUPS: list[tuple[object, list[tuple[str, object]]]] = [
    (
        _("Engine"),
        [
            ("EngineHP", _("Horsepower")),
            ("EngineKW", _("Power (kW)")),
            ("EngineCylinders", _("Cylinders")),
            ("EngineConfiguration", _("Configuration")),
            ("DisplacementCC", _("Displacement (cc)")),
            ("EngineManufacturer", _("Engine made by")),
            ("EngineModel", _("Engine model")),
            ("FuelInjectionType", _("Fuel injection")),
            ("Turbo", _("Turbocharged")),
            ("ValveTrainDesign", _("Valvetrain")),
            ("CoolingType", _("Cooling")),
            ("OtherEngineInfo", _("Other engine notes")),
            ("ElectrificationLevel", _("Electrification")),
            ("BatteryType", _("Battery type")),
            ("ChargerLevel", _("Charger level")),
        ],
    ),
    (
        _("Body and dimensions"),
        [
            ("VehicleType", _("Vehicle type")),
            ("Series", _("Series")),
            ("Series2", _("Series detail")),
            ("BodyCabType", _("Cab type")),
            ("BedType", _("Bed type")),
            ("BedLengthIN", _("Bed length (in)")),
            ("Doors", _("Doors")),
            ("Seats", _("Seats")),
            ("SeatRows", _("Seat rows")),
            ("GVWR", _("Gross weight rating")),
            ("CurbWeightLB", _("Curb weight (lb)")),
            ("WheelBaseShort", _("Wheelbase (in)")),
            ("Wheels", _("Wheels")),
            ("WheelSizeFront", _("Front wheel size")),
            ("WheelSizeRear", _("Rear wheel size")),
            ("TrackWidth", _("Track width")),
        ],
    ),
    (
        _("Safety equipment"),
        [
            ("BrakeSystemType", _("Brake system")),
            ("BrakeSystemDesc", _("Brake detail")),
            ("ABS", _("Anti-lock brakes")),
            ("ESC", _("Stability control")),
            ("TractionControl", _("Traction control")),
            ("TPMS", _("Tire pressure monitoring")),
            ("AirBagLocFront", _("Front airbags")),
            ("AirBagLocSide", _("Side airbags")),
            ("AirBagLocCurtain", _("Curtain airbags")),
            ("AirBagLocKnee", _("Knee airbags")),
            ("SeatBeltsAll", _("Seat belt type")),
            ("BlindSpotMon", _("Blind spot monitoring")),
            ("ForwardCollisionWarning", _("Forward collision warning")),
            ("LaneDepartureWarning", _("Lane departure warning")),
            ("BackupCamera", _("Reversing camera")),
            ("ParkAssist", _("Park assist")),
            ("AdaptiveCruiseControl", _("Adaptive cruise")),
            ("DaytimeRunningLight", _("Daytime running lights")),
            ("KeylessIgnition", _("Keyless ignition")),
        ],
    ),
    (
        _("Where it was built"),
        [
            ("Manufacturer", _("Manufacturer")),
            ("PlantCompanyName", _("Plant")),
            ("PlantCity", _("Plant city")),
            ("PlantState", _("Plant state")),
            ("PlantCountry", _("Plant country")),
            ("NCSAMake", _("NCSA make")),
            ("NCSAModel", _("NCSA model")),
            ("NCSABodyType", _("NCSA body type")),
        ],
    ),
]


def is_meaningful(value: object) -> bool:
    return str(value or "").strip().lower() not in EMPTY_VALUES


def details(payload: dict | None) -> list[tuple[object, list[tuple[object, str]]]]:
    """Group the populated fields of a stored decode for display.

    Returns only groups that have something in them, so a sparsely decoded
    vehicle shows two short sections rather than four headings over nothing.
    """
    payload = payload or {}
    grouped = []
    for label, fields in GROUPS:
        rows = [
            (key, field_label, str(payload.get(key)).strip())
            for key, field_label in fields
            if is_meaningful(payload.get(key))
        ]
        if rows:
            grouped.append((label, rows))
    return grouped


# --------------------------------------------------------------------------
# Promoting a decoded value into a spec
# --------------------------------------------------------------------------
# Which spec group a decoded field belongs in, and the unit it carries.
#
# Only fields worth having on a reference sheet appear here. vPIC is a
# registration database: it holds no torque values, no fluid capacities and no
# cold tire pressures, so the groups a person reaches for mid-job stay empty
# either way. What it does have is a handful of facts worth not looking up
# twice, and this is the map for those.
#
# Anything shown but absent from this map still promotes, into `other`. A
# missing entry should mean "unclassified", never "refused".
SPEC_TARGETS: dict[str, tuple[str, str]] = {
    # key: (spec group, unit)
    "EngineHP": ("electrical", "hp"),
    "EngineKW": ("electrical", "kW"),
    "EngineCylinders": ("other", ""),
    "DisplacementCC": ("other", "cc"),
    "EngineModel": ("filters", ""),
    "EngineManufacturer": ("filters", ""),
    "FuelInjectionType": ("other", ""),
    "Turbo": ("other", ""),
    "ValveTrainDesign": ("torque", ""),
    "CoolingType": ("fluids", ""),
    "ElectrificationLevel": ("electrical", ""),
    "BatteryType": ("electrical", ""),
    "ChargerLevel": ("electrical", ""),
    "GVWR": ("other", ""),
    "CurbWeightLB": ("other", "lb"),
    "WheelBaseShort": ("alignment", "in"),
    "TrackWidth": ("alignment", "in"),
    "Wheels": ("tires", ""),
    "WheelSizeFront": ("tires", "in"),
    "WheelSizeRear": ("tires", "in"),
    "BedLengthIN": ("other", "in"),
    "Doors": ("other", ""),
    "Seats": ("other", ""),
    "SeatRows": ("other", ""),
    "BrakeSystemType": ("other", ""),
    "BrakeSystemDesc": ("other", ""),
    "TPMS": ("tires", ""),
    "ABS": ("other", ""),
    "ESC": ("other", ""),
    "TractionControl": ("other", ""),
}

# Label -> vPIC key, so a promote request can name what the reader saw.
LABELS_BY_KEY: dict[str, object] = {
    key: label for _group, fields in GROUPS for key, label in fields
}


def target_for(key: str) -> tuple[str, str]:
    """The spec group and unit for a decoded field. Unknown means `other`."""
    return SPEC_TARGETS.get(key, ("other", ""))
