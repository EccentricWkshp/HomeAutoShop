"""
Offline trouble-code dictionary (SPEC §8.3c).

Four layers, because a code carries several different kinds of meaning and they
are not equally trustworthy. Every answer says which layer it came from, so the
screen can tell a standard apart from a manufacturer's own wording apart from
something somebody in this shop typed.

**Structure** is defined by SAE J2012 and ISO 15031-6 and is true of every code
ever issued: the letter names the system, the second digit says whether the
code is ISO/SAE controlled or the manufacturer's own, and — for powertrain codes — the
third names the subsystem. That is derivable, so it is derived. A code this
application has never heard of still produces *"Chassis · manufacturer-specific"*
rather than a blank, which is a real answer.

**Wording** is standardized only for the ISO/SAE controlled set. Those are finite, so a
table of them is bundled and works with the network unplugged.

Manufacturer-specific codes are not published anywhere free *and*
comprehensive, and inventing plausible text for `P1516` would be worse than
saying nothing, because the operator would act on it. Two honest sources exist
for them and both are used, in this order:

* **What somebody in this shop wrote** (`CodeDescription`), typed once per make
  and reused instance-wide. It outranks a shipped table because the person
  holding the vehicle outranks a document about it.
* **The manufacturer's own published list**, transcribed into
  `codelists/<make>.json` where one exists. Ford publishes three thousand of
  them; typing those in one at a time was never a reasonable ask.

What remains is structure, which is never a guess at the fault.

The ISO/SAE table carries a translation key per the §5.6 seed-data rule. The
transcribed lists deliberately do not — see `codelists/__init__.py`.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from django.utils.translation import gettext_lazy as _

from . import codelists

#: Every list as loaded, including the ones keyed to no make. Filled by
#: :func:`_lists`, which is the cached loader; kept beside it rather than
#: returned from it because the cache key is "all of them" either way.
_EVERY: dict[str, list] = {}

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

#: The ISO/SAE controlled set — what J2012 defines for every vehicle, and what
#: the shop floor calls "generic". Not exhaustive: the full list runs to
#: thousands, most of them cylinder- and bank-numbered variants that
#: :func:`_expand` generates below. What is written out here is the codes a
#: home shop actually meets, phrased the way the standard phrases them.
_ISO_SAE: dict[str, object] = {
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
    # No C-codes here on purpose. `C0035`/`C0040`/`C0045`/`C0050` used to sit
    # in this table as left-front through right-rear wheel speed sensors, which
    # is **GM's** chassis numbering rather than the standard's: the published
    # J2012 C set bundled beside this one puts the wheel speed sensors at
    # `C0031`/`C0034`/`C0037`, reads `C0040` as *Brake Pedal Switch "A"*, and
    # marks `C0050` **ISO/SAE Reserved** — a code the standard reserves cannot
    # also be a definition the standard gives. `C0561` appears in no published
    # ISO/SAE set held here at all, which is not evidence of what it means,
    # only that this table could not say.
    #
    # They mattered more than five rows because of where they were. This table
    # is the *authoritative* layer: `is_authoritative` is true for it and the
    # screen presents it as fact on every vehicle ever built. One manufacturer's
    # chassis codes asserted that way is the precise failure that scoping
    # definitions by make exists to prevent, arrived at from the other end.
    #
    # Removed rather than corrected: the published C set answers all four
    # properly and says whose wording it is, and `C0561` reaches structure,
    # which reports what its number actually tells you — an ISO/SAE-controlled
    # chassis code nobody here has defined — rather than asserting one
    # manufacturer's meaning for it on every vehicle.
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
    table = dict(_ISO_SAE)
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
    #
    # **Three sensors per bank, not four.** The blocks end at P0147 and P0167;
    # what follows is fuel, not oxygen — `P0148` is *Fuel delivery error* and
    # `P0168` is *Fuel temperature too high*, both of which this loop was
    # overwriting with an invented "bank 1 sensor 4". Generating a row is
    # exactly as much of a claim as typing one, and this one was contradicting
    # the published J2012 set bundled beside it while outranking it, because
    # the hand-written table is the layer a caller may present as fact.
    for bank, base in ((1, 130), (2, 150)):
        for sensor in range(1, 4):
            start = base + (sensor - 1) * 6
            table.setdefault(
                f"P{start:04d}",
                _("Oxygen sensor circuit (bank %(b)d sensor %(s)d)") % {"b": bank, "s": sensor},
            )
    return table


#: The revision of the table below. It is written out here the way the standard
#: phrases it rather than transcribed from a file, so it changes with this
#: module — but J2012 is revised and codes are added, and an answer presented
#: to an operator as fact should be able to say which printing it came from.
#: Raise it when a definition here changes or a code is added.
ISO_SAE_VERSION = 1

ISO_SAE = _expand()


def parse(code: str) -> dict | None:
    """Split a code into its J2012 parts, or None if it is not code-shaped."""
    match = CODE_RE.match((code or "").strip().upper())
    if not match:
        return None
    system, second, third, rest, failure = match.groups()
    return {
        "code": f"{system}{second}{third}{rest}".upper(),
        "system": system.upper(),
        "is_iso_sae": second in "02",
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


# --------------------------------------------------------------------------
# Transcribed manufacturer lists
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class CodeList:
    """One published list, as transcribed.

    `scope` is `make` for a manufacturer's own list and `iso_sae` for a
    document that is the standard's list rather than anybody's — the P, B, C
    and U sets. J2012 calls those codes ISO/SAE controlled, and such a list is
    matched to no make at all: it answers ISO/SAE codes for every vehicle and
    manufacturer-controlled codes for none.
    """

    make: str
    source: str
    codes: dict
    scope: str = "make"
    #: Higher wins where two documents cover one make and define one code.
    #: A vehicle's own service manual outranks a third party's summary of the
    #: whole make, and there is no way to work that out from the files.
    precedence: int = 0
    #: The publisher's revision of this document. Every list has one, the
    #: standard's own sets included: J2012 is revised, codes are added, and a
    #: definition presented as fact still came from a particular printing of
    #: it. Bundled lists change with the image rather than through the catalog,
    #: so the number is provenance rather than an update prompt — but "which
    #: revision is this instance answering from" is a question worth being able
    #: to answer, and a transcription that cannot say is not checkable.
    version: int = 1

    @property
    def is_iso_sae(self) -> bool:
        return self.scope == "iso_sae"


@lru_cache(maxsize=1)
def _lists() -> dict[str, list[CodeList]]:
    """Every list this instance can read, keyed by each make that reads it.

    Two sources, and the split is the point. **The standard's own sets ship in
    the image**: they answer for every vehicle ever built, they are finite, and
    an instance that has never reached the network still knows what `P0420`
    means. **A manufacturer's list is installed**, because there are ninety-odd
    makes and a shop owns three; see
    :class:`~homeautoshop.diagnostics.models.InstalledCodeList`.

    A make maps to a **list** of documents, not one. A make can be covered by
    more than one — a summary of the whole badge and, alongside it, one
    vehicle's own service manual — and keying by name alone let the second
    silently replace the first, which is the kind of loss nothing would report.

    Aliases are how one document covers several badges: Ford's is the Ford
    Motor Company Group's, so Lincoln and Mercury read it too. They are the
    same modules with a different badge, and the alternative is three copies of
    one table drifting apart.
    """
    found: dict[str, list[CodeList]] = {}
    _all: list[CodeList] = _EVERY.setdefault("lists", [])
    _all.clear()
    for entry, names in [*_bundled(), *_installed()]:
        _all.append(entry)
        if entry.is_iso_sae:
            # Not keyed to anything. A vehicle never "is" the standard, and
            # keying it to a name would let it answer a manufacturer code.
            continue
        for name in names:
            if str(name).strip():
                found.setdefault(str(name).strip().lower(), []).append(entry)

    for name, entries in found.items():
        # A document that *is* this make's answers before one that merely
        # covers it. Ford's list carries Lincoln and Mercury by alias, which
        # was the whole answer while neither had a document; once Lincoln has
        # 4,892 codes read from Lincoln service manuals, ranking on precedence
        # and source alone hands a Lincoln Ford's list because `Ford` sorts
        # before `Lincoln`. Both still answer — `explain` walks all of them —
        # so the badge's own wording leads and the group's fills the gaps.
        entries.sort(
            key=lambda e: (e.make.strip().lower() != name, -e.precedence, e.source)
        )
    return found


def _bundled() -> list[tuple[CodeList, list[str]]]:
    """The lists that ship in the image: the ISO/SAE sets, and nothing else."""
    out = []
    for path in sorted(Path(codelists.__file__).parent.glob("*.json")):
        if path.name.startswith("_"):
            continue  # `_rejected.json` is a register of what was kept out.
        data = json.loads(path.read_text(encoding="utf-8"))
        out.append((
            CodeList(
                make=str(data.get("make") or ""),
                source=str(data.get("source") or ""),
                codes=data.get("codes") or {},
                scope=str(data.get("scope") or "make"),
                precedence=int(data.get("precedence") or 0),
                version=int(data.get("version") or 1),
            ),
            [data.get("make") or "", *(data.get("aliases") or [])],
        ))
    return out


def _installed() -> list[tuple[CodeList, list[str]]]:
    """The manufacturer lists this shop chose to install.

    Read straight from the rows every time `_lists` is rebuilt, and the cache
    above is dropped whenever one is installed or removed — see
    `DiagnosticsConfig.ready`. A stale answer here would be a code page still
    quoting a list somebody has just deleted.
    """
    from .models import InstalledCodeList

    out = []
    try:
        rows = list(InstalledCodeList.objects.all())
    except Exception:
        # Before `migrate`, and during the first steps of a fresh install,
        # there is no table yet. The standard still answers, which is the
        # whole reason it is bundled rather than installed.
        return out

    for row in rows:
        names = [row.make, *(row.aliases or [])]
        for document in row.documents or []:
            out.append((
                CodeList(
                    make=row.make,
                    source=str(document.get("source") or ""),
                    codes=document.get("codes") or {},
                    scope="make",
                    precedence=int(document.get("precedence") or 0),
                    version=row.version,
                ),
                names,
            ))
    return out


def forget() -> None:
    """Drop the cached view of what is installed.

    Called when a list is installed or removed. Everything else about the
    lookup is files, which do not change while the process runs.
    """
    _lists.cache_clear()


def code_lists_for(make: str) -> list[CodeList]:
    """Every bundled list covering this make, best first."""
    return _lists().get((make or "").strip().lower(), [])


def code_list_for(make: str) -> CodeList | None:
    """The first list covering this make, if one is shipped."""
    covering = code_lists_for(make)
    return covering[0] if covering else None


def makes_with_lists() -> list[str]:
    """The makes a bundled list can answer for, named as their own list names."""
    return sorted({e.make for entries in _lists().values() for e in entries})


def _every_list() -> list[CodeList]:
    """Each bundled list once, in a fixed order so an answer never wobbles.

    The standard's own sets come first. Several documents define one ISO/SAE
    code, and a list that *is* the standard's list is the better authority for
    it than one manufacturer's restatement.
    """
    _lists()  # populates the registry below
    return sorted(
        _EVERY["lists"], key=lambda e: (not e.is_iso_sae, e.make)
    )


# --------------------------------------------------------------------------
# Looking a code up
# --------------------------------------------------------------------------

#: Where a definition came from. The screen says which, because "the standard
#: defines this", "Ford's own list says this" and "somebody here typed this"
#: are three different kinds of claim and only the first is the same on every
#: vehicle in the world.
STANDARD = "standard"
OPERATOR = "operator"
MAKE = "make"
PUBLISHED = "published"
REPORT = "report"
STRUCTURE = "structure"


@dataclass(frozen=True)
class Definition:
    """What is known about one code, and on whose authority."""

    code: str
    text: str
    source: str
    make: str = ""
    citation: str = ""
    #: The revision of the list that answered, where a list did. Zero for a
    #: definition that came from somewhere else — the hand-written ISO/SAE
    #: table, a note typed here, the scan tool, or structure.
    version: int = 0

    def __str__(self) -> str:
        """The code and what it means, in one line.

        Written out because a `Definition` reaches places that render whatever
        they are given — the API's search endpoint among them — and a
        dataclass repr there would be a wall of field names where a person
        expected a sentence.
        """
        said = f"{self.code} — {self.text}" if self.text else self.code
        return f"{said} ({self.make})" if self.make else said

    @property
    def is_authoritative(self) -> bool:
        """True only for the SAE set — the caller may present that as fact."""
        return self.source == STANDARD

    @property
    def is_known(self) -> bool:
        """Whether anything actually said what this fault *is*."""
        return self.source != STRUCTURE


def explain(code: str, *, make: str = "", reported: str = "") -> Definition | None:
    """The best available meaning for a code, and where it came from.

    `reported` is what the scan tool printed against this particular reading,
    where there is one. None is returned when the string is not code-shaped at
    all — different from a code nobody has a definition for, and the caller has
    to be able to tell those apart.

    **The order is the whole point**, and it ranks claims rather than
    convenience:

    1. **The ISO/SAE controlled set.** Such a code means the same thing on every
       vehicle ever built, so nothing local gets to redefine it.
    2. **A note recorded in this shop for this make.** Somebody looked it up
       and wrote it down deliberately, which outranks every table below — the
       same rule that keeps a corrected VIN decode safe from vPIC.
    3. **The manufacturer's own published list.**
    4. **Any published list at all, for an ISO/SAE code.** Such a code means
       the same thing on every vehicle ever built, so a manufacturer's wording
       for one is evidence about the standard rather than about that
       manufacturer — there is no reason a Toyota should be told nothing about
       `P0351` because the list that happens to define it is Ford's. The table
       in this module is a few hundred codes hand-written the way the standard
       phrases them; Ford's list alone carries 862 more.
    5. **What the scan tool printed**, below the manufacturer's own list on
       purpose. A tool is a third party rendering somebody else's definition:
       it truncates, and it sometimes declines outright. A real Ford `B1695`
       reads *"Please See The Vehicle Service Manual."* off one tool and
       *"Autolamp On Circuit Short To Battery"* off Ford's own list. Nothing
       is lost by ranking it here, because the screen still prints what the
       tool read underneath.
    6. **Structure**, which is the floor and is never a guess at the fault.
    """
    parsed = parse(code)
    if parsed is None:
        return None

    canonical = parsed["code"]
    if parsed["is_iso_sae"] and canonical in ISO_SAE:
        return Definition(
            canonical, str(ISO_SAE[canonical]), STANDARD, version=ISO_SAE_VERSION
        )

    if make:
        from .models import CodeDescription

        own = (
            CodeDescription.objects.filter(code=canonical, make__iexact=make)
            .values_list("description", flat=True)
            .first()
        )
        if own:
            return Definition(canonical, own, OPERATOR, make=make)

        for published in code_lists_for(make):
            if canonical in published.codes:
                return Definition(
                    canonical,
                    published.codes[canonical],
                    MAKE,
                    make=published.make,
                    citation=published.source,
                    version=published.version,
                )

    if parsed["is_iso_sae"]:
        for entry in _every_list():
            if canonical in entry.codes:
                return Definition(
                    canonical,
                    entry.codes[canonical],
                    PUBLISHED,
                    make=entry.make,
                    citation=entry.source,
                    version=entry.version,
                )

    if (reported or "").strip():
        return Definition(canonical, reported.strip(), REPORT)

    return Definition(canonical, structural(canonical), STRUCTURE)


#: How many code hits a lookup returns before it stops being a lookup and
#: starts being a listing. Somebody typing "misfire" wants the handful that
#: names it, not four hundred rows to read.
MOST_HITS = 25


def find(query: str, *, limit: int = MOST_HITS) -> list[Definition]:
    """Codes matching `query`, for looking one up without a report in hand.

    Two ways of asking, because there are two questions:

    * **By code.** `P0420`, or the prefix `P042` when somebody is reading a
      cracked screen. This is the common one and it is why the whole thing
      exists — the code page could only be reached by importing a report and
      clicking a reading, so answering "what is P0420" meant running a scan
      first.
    * **By what it means.** `catalyst efficiency`, `evap purge`. A technician
      who knows the symptom and not the number is the other half of the job,
      and the tables already hold the words.

    Searched across the standard's own sets and every installed manufacturer
    list, and each hit says which — an ISO/SAE code means the same thing on
    every vehicle, while `P1345` is one thing to GM and another to Toyota, and
    a result that did not say which would be worse than none.

    Nothing here is vehicle data, so it is not narrowed by who is asking. A
    dictionary is a dictionary.
    """
    query = " ".join((query or "").split())
    if len(query) < 2:
        return []

    wanted = query.upper().replace(" ", "")
    by_code = bool(re.match(r"^[PBCU][0-9A-F]{0,4}$", wanted))
    # Every word, anywhere, rather than the phrase as typed. Nobody types a
    # definition verbatim: `catalyst efficiency` is how a person asks for
    # "Catalyst system efficiency below threshold", and a phrase match answers
    # that with nothing while looking like the code does not exist.
    terms = [t for t in query.casefold().split() if t]

    found: dict[tuple[str, str], Definition] = {}

    def keep(code: str, text: str, source: str, make: str, citation: str, version: int):
        # Keyed by code *and* make: one code answered by four makes is four
        # answers, and collapsing them would silently pick one.
        found.setdefault(
            (code, make.lower()),
            Definition(code, text, source, make=make, citation=citation, version=version),
        )

    def says(text: str) -> bool:
        lowered = text.casefold()
        return all(term in lowered for term in terms)

    for code, text in ISO_SAE.items():
        if code.startswith(wanted) if by_code else says(str(text)):
            keep(code, str(text), STANDARD, "", "", 0)

    for entry in _every_list():
        source = PUBLISHED if entry.is_iso_sae else MAKE
        for code, text in entry.codes.items():
            if code.startswith(wanted) if by_code else says(text):
                keep(code, text, source, entry.make, entry.source, entry.version)
        if len(found) > limit * 4:
            # Enough to rank from. Reading every remaining list to find the
            # 2,000th match is work nobody is waiting for.
            break

    # The standard first, then by code, so an exact code lands at the top and
    # the answer that is true of every vehicle outranks one make's wording.
    return sorted(
        found.values(),
        key=lambda d: (d.source != STANDARD, d.code != wanted, d.code, d.make),
    )[:limit]


def describe(code: str, *, make: str = "", reported: str = "") -> tuple[str, bool]:
    """`(description, is_authoritative)` — the old two-value shape.

    Kept because most callers only need the words and whether to present them
    as fact. Anything that wants to say *where* the words came from should call
    :func:`explain` instead.
    """
    found = explain(code, make=make, reported=reported)
    if found is None:
        return "", False
    return found.text, found.is_authoritative


def structural(code: str) -> str:
    """What the code's shape alone says. Never a guess at the fault."""
    parsed = parse(code)
    if parsed is None:
        return ""
    system = str(SYSTEMS.get(parsed["system"], ""))
    scope = _("ISO/SAE") if parsed["is_iso_sae"] else _("manufacturer-specific")
    if parsed["system"] == "P" and parsed["subsystem"] in POWERTRAIN_SUBSYSTEMS:
        area = str(POWERTRAIN_SUBSYSTEMS[parsed["subsystem"]])
        return f"{system} · {area} · {scope}"
    return f"{system} · {scope}"
