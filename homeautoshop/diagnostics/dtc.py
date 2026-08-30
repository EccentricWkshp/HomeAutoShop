"""
Offline trouble-code dictionary (SPEC §8.3c).

Two layers, because a code carries two different kinds of meaning and only one
of them can be looked up honestly.

**Structure** is defined by SAE J2012 and ISO 15031-6 and is true of every code
ever issued: the letter names the system, the second digit says whether the
code is generic or the manufacturer's own, and — for powertrain codes — the
third names the subsystem. That is derivable, so it is derived. A code this
application has never heard of still produces *"Chassis · manufacturer-specific"*
rather than a blank, which is a real answer.

**Wording** is only reliable for the generic set. Those are standardized and
finite, so a table of them is bundled and works with the network unplugged.
Manufacturer-specific codes are not published anywhere free and comprehensive,
and inventing plausible text for `P1516` would be worse than saying nothing:
the operator would act on it. Those come from the operator, once, per make, and
are reused instance-wide (`CodeDescription`).

Every description carries a translation key per §5.6 seed-data rule, so the
catalog can be translated without the codes being re-keyed.
"""

from __future__ import annotations

import re

from django.utils.translation import gettext_lazy as _

#: `B1352-20` is a real code: the suffix is a failure-type byte, not noise.
CODE_RE = re.compile(r"^([PBCU])([0-9A-F])([0-9A-F])([0-9A-F]{2})(?:-([0-9A-F]{2}))?$", re.I)

SYSTEMS = {
    "P": _("Powertrain"),
    "B": _("Body"),
    "C": _("Chassis"),
    "U": _("Network"),
}

#: J2012's third character, for powertrain codes only. The other systems do not
#: subdivide this way, and pretending they do would invent a fact.
POWERTRAIN_SUBSYSTEMS = {
    "0": _("Fuel and air metering"),
    "1": _("Fuel and air metering"),
    "2": _("Fuel and air metering — injector circuit"),
    "3": _("Ignition system or misfire"),
    "4": _("Auxiliary emission controls"),
    "5": _("Vehicle speed, idle control, auxiliary inputs"),
    "6": _("Computer output circuit"),
    "7": _("Transmission"),
    "8": _("Transmission"),
    "9": _("Transmission"),
    "A": _("Hybrid propulsion"),
    "B": _("Hybrid propulsion"),
    "C": _("Hybrid propulsion"),
}

#: The generic (SAE-defined) set. Not exhaustive — the full J2012 list runs to
#: thousands, most of them cylinder- and bank-numbered variants that
#: :func:`_expand` generates below. What is written out here is the codes a
#: home shop actually meets, phrased the way the standard phrases them.
_GENERIC: dict[str, object] = {
    # --- fuel and air metering -------------------------------------------
    "P0011": _("Camshaft position — timing over-advanced (bank 1)"),
    "P0014": _("Camshaft position — timing over-advanced (bank 1 exhaust)"),
    "P0016": _("Crankshaft / camshaft position correlation (bank 1 sensor A)"),
    "P0017": _("Crankshaft / camshaft position correlation (bank 1 sensor B)"),
    "P0021": _("Camshaft position — timing over-advanced (bank 2)"),
    "P0087": _("Fuel rail / system pressure too low"),
    "P0088": _("Fuel rail / system pressure too high"),
    "P0096": _("Intake air temperature sensor 2 — range or performance"),
    "P0101": _("Mass or volume air flow — range or performance"),
    "P0102": _("Mass or volume air flow — circuit low"),
    "P0103": _("Mass or volume air flow — circuit high"),
    "P0106": _("Manifold absolute pressure — range or performance"),
    "P0107": _("Manifold absolute pressure — circuit low"),
    "P0108": _("Manifold absolute pressure — circuit high"),
    "P0111": _("Intake air temperature — range or performance"),
    "P0112": _("Intake air temperature — circuit low"),
    "P0113": _("Intake air temperature — circuit high"),
    "P0116": _("Engine coolant temperature — range or performance"),
    "P0117": _("Engine coolant temperature — circuit low"),
    "P0118": _("Engine coolant temperature — circuit high"),
    "P0121": _("Throttle position sensor A — range or performance"),
    "P0122": _("Throttle position sensor A — circuit low"),
    "P0123": _("Throttle position sensor A — circuit high"),
    "P0125": _("Coolant temperature insufficient for closed-loop fuel control"),
    "P0128": _("Coolant thermostat below regulating temperature"),
    "P0130": _("Oxygen sensor circuit (bank 1 sensor 1)"),
    "P0131": _("Oxygen sensor circuit low (bank 1 sensor 1)"),
    "P0132": _("Oxygen sensor circuit high (bank 1 sensor 1)"),
    "P0133": _("Oxygen sensor slow response (bank 1 sensor 1)"),
    "P0134": _("Oxygen sensor — no activity detected (bank 1 sensor 1)"),
    "P0135": _("Oxygen sensor heater circuit (bank 1 sensor 1)"),
    "P0136": _("Oxygen sensor circuit (bank 1 sensor 2)"),
    "P0137": _("Oxygen sensor circuit low (bank 1 sensor 2)"),
    "P0138": _("Oxygen sensor circuit high (bank 1 sensor 2)"),
    "P0139": _("Oxygen sensor slow response (bank 1 sensor 2)"),
    "P0140": _("Oxygen sensor — no activity detected (bank 1 sensor 2)"),
    "P0141": _("Oxygen sensor heater circuit (bank 1 sensor 2)"),
    "P0171": _("System too lean (bank 1)"),
    "P0172": _("System too rich (bank 1)"),
    "P0174": _("System too lean (bank 2)"),
    "P0175": _("System too rich (bank 2)"),
    "P0182": _("Fuel temperature sensor A — circuit low"),
    "P0190": _("Fuel rail pressure sensor circuit"),
    # --- misfire ----------------------------------------------------------
    "P0300": _("Random or multiple cylinder misfire detected"),
    "P0315": _("Crankshaft position — variation not learned"),
    "P0316": _("Misfire detected on startup"),
    # --- emission controls ------------------------------------------------
    "P0325": _("Knock sensor 1 circuit (bank 1)"),
    "P0327": _("Knock sensor 1 circuit low (bank 1)"),
    "P0328": _("Knock sensor 1 circuit high (bank 1)"),
    "P0335": _("Crankshaft position sensor A circuit"),
    "P0336": _("Crankshaft position sensor A — range or performance"),
    "P0340": _("Camshaft position sensor A circuit (bank 1)"),
    "P0341": _("Camshaft position sensor A — range or performance (bank 1)"),
    "P0401": _("Exhaust gas recirculation flow insufficient"),
    "P0402": _("Exhaust gas recirculation flow excessive"),
    "P0403": _("Exhaust gas recirculation control circuit"),
    "P0404": _("Exhaust gas recirculation — range or performance"),
    "P0411": _("Secondary air injection — incorrect flow"),
    "P0420": _("Catalyst system efficiency below threshold (bank 1)"),
    "P0430": _("Catalyst system efficiency below threshold (bank 2)"),
    "P0440": _("Evaporative emission system"),
    "P0441": _("Evaporative emission system — incorrect purge flow"),
    "P0442": _("Evaporative emission system — small leak detected"),
    "P0443": _("Evaporative emission purge control valve circuit"),
    "P0446": _("Evaporative emission vent control circuit"),
    "P0449": _("Evaporative emission vent valve circuit"),
    "P0451": _("Evaporative emission pressure sensor — range or performance"),
    "P0455": _("Evaporative emission system — large leak detected"),
    "P0456": _("Evaporative emission system — very small leak detected"),
    "P0457": _("Evaporative emission leak — fuel cap loose or off"),
    # --- speed, idle, auxiliary inputs -----------------------------------
    "P0500": _("Vehicle speed sensor A"),
    "P0501": _("Vehicle speed sensor A — range or performance"),
    "P0505": _("Idle air control system"),
    "P0506": _("Idle air control system — RPM lower than expected"),
    "P0507": _("Idle air control system — RPM higher than expected"),
    "P0521": _("Engine oil pressure sensor — range or performance"),
    "P0562": _("System voltage low"),
    "P0563": _("System voltage high"),
    "P0571": _("Brake switch A circuit"),
    # --- computer output --------------------------------------------------
    "P0601": _("Internal control module — memory checksum error"),
    "P0603": _("Internal control module — keep-alive memory error"),
    "P0605": _("Internal control module — read-only memory error"),
    "P0606": _("Control module processor fault"),
    "P0620": _("Generator control circuit"),
    "P0625": _("Generator field terminal circuit low"),
    "P0645": _("A/C clutch relay control circuit"),
    # --- transmission -----------------------------------------------------
    "P0700": _("Transmission control system — fault indicated"),
    "P0701": _("Transmission control system — range or performance"),
    "P0705": _("Transmission range sensor circuit"),
    "P0711": _("Transmission fluid temperature — range or performance"),
    "P0715": _("Input / turbine speed sensor circuit"),
    "P0720": _("Output speed sensor circuit"),
    "P0730": _("Incorrect gear ratio"),
    "P0740": _("Torque converter clutch circuit"),
    "P0741": _("Torque converter clutch — stuck off or performance"),
    "P0742": _("Torque converter clutch — stuck on"),
    "P0743": _("Torque converter clutch circuit — electrical"),
    "P0748": _("Pressure control solenoid A — electrical"),
    "P0751": _("Shift solenoid A — stuck off or performance"),
    "P0755": _("Shift solenoid B circuit"),
    # --- chassis ----------------------------------------------------------
    "C0035": _("Left front wheel speed sensor circuit"),
    "C0040": _("Right front wheel speed sensor circuit"),
    "C0045": _("Left rear wheel speed sensor circuit"),
    "C0050": _("Right rear wheel speed sensor circuit"),
    "C0561": _("System disabled information stored"),
    # --- body -------------------------------------------------------------
    "B0001": _("Driver airbag deployment control"),
    "B0081": _("Driver seatbelt pretensioner deployment control"),
    # --- network ----------------------------------------------------------
    "U0001": _("High-speed CAN communication bus"),
    "U0100": _("Lost communication with engine control module"),
    "U0101": _("Lost communication with transmission control module"),
    "U0121": _("Lost communication with ABS control module"),
    "U0140": _("Lost communication with body control module"),
    "U0155": _("Lost communication with instrument panel cluster"),
    "U0401": _("Invalid data received from engine control module"),
}


def _expand() -> dict[str, object]:
    """Generate the numbered families rather than typing four hundred rows.

    `P0301`–`P0312` and the oxygen-sensor grid differ only by a cylinder or a
    bank-and-sensor number, and writing them out by hand is how a typo gets
    into a lookup table nobody checks.
    """
    table = dict(_GENERIC)
    for cylinder in range(1, 13):
        table.setdefault(
            f"P{300 + cylinder:04d}",
            _("Cylinder %(n)d misfire detected") % {"n": cylinder},
        )
        table.setdefault(
            f"P{200 + cylinder:04d}",
            _("Injector circuit — cylinder %(n)d") % {"n": cylinder},
        )
    # J2012 gives each oxygen sensor a block of six consecutive codes, starting
    # at P0130 for bank 1 and P0150 for bank 2. The block base is the plain
    # "circuit" fault; the five after it are low, high, slow, inactive, heater.
    for bank, base in ((1, 130), (2, 150)):
        for sensor in range(1, 5):
            start = base + (sensor - 1) * 6
            table.setdefault(
                f"P{start:04d}",
                _("Oxygen sensor circuit (bank %(b)d sensor %(s)d)") % {"b": bank, "s": sensor},
            )
    return table


GENERIC = _expand()


def parse(code: str) -> dict | None:
    """Split a code into its J2012 parts, or None if it is not code-shaped."""
    match = CODE_RE.match((code or "").strip().upper())
    if not match:
        return None
    system, second, third, rest, failure = match.groups()
    return {
        "code": f"{system}{second}{third}{rest}".upper(),
        "system": system.upper(),
        "is_generic": second in "02",
        "subsystem": third,
        "failure_type": (failure or "").upper(),
    }


def normalize(code: str) -> str:
    """The canonical form: uppercase, failure-type byte dropped.

    Two readings of `B1352` and `B1352-20` are the same fault, and matching a
    recurrence has to see that.
    """
    parsed = parse(code)
    return parsed["code"] if parsed else (code or "").strip().upper()


def describe(code: str, *, make: str = "") -> tuple[str, bool]:
    """Best available wording for a code, and whether it is authoritative.

    Returns `(description, is_authoritative)`. Authoritative means the SAE
    generic table — the caller may present that as fact. Anything else is a
    structural summary or the operator's own note, and the UI says so rather
    than letting a guess look like a lookup.
    """
    parsed = parse(code)
    if parsed is None:
        return "", False

    canonical = parsed["code"]
    if parsed["is_generic"] and canonical in GENERIC:
        return str(GENERIC[canonical]), True

    from .models import CodeDescription

    own = (
        CodeDescription.objects.filter(code=canonical)
        .filter(make__iexact=make)
        .values_list("description", flat=True)
        .first()
        if make
        else None
    )
    if own:
        return own, False

    return structural(canonical), False


def structural(code: str) -> str:
    """What the code's shape alone says. Never a guess at the fault."""
    parsed = parse(code)
    if parsed is None:
        return ""
    system = str(SYSTEMS.get(parsed["system"], ""))
    scope = _("generic") if parsed["is_generic"] else _("manufacturer-specific")
    if parsed["system"] == "P" and parsed["subsystem"] in POWERTRAIN_SUBSYSTEMS:
        area = str(POWERTRAIN_SUBSYSTEMS[parsed["subsystem"]])
        return f"{system} · {area} · {scope}"
    return f"{system} · {scope}"
