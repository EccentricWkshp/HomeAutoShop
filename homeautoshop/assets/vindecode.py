"""
Decoding a VIN from before there was a standard (SPEC FR-VEH-12, §8.1a).

vPIC decodes the 17-character VIN and nothing else, which leaves every vehicle
built before the 1981 model year with an identifier no part of this application
can read. `F26SVAE1234` is an F-250 4WD with a 400 V8 built at Kentucky Truck
in 1978, and all of that is stamped on the door — it just needs the table.

**The tables are data, in `vin_schemes.py`, and this is the matcher.** That
split is the whole design. There were dozens of numbering schemes, they differ
per make, per era and sometimes per model line, and the only thing they have in
common is being fixed-width fields read against lookup tables. Writing a
function per manufacturer would be writing the same function twenty times; the
interesting part is the transcription, and transcription belongs somewhere it
can be read, corrected and checked without touching code.

Three things this deliberately does **not** do:

* **Guess.** A field whose code is not in its table is reported as unknown, not
  approximated. A scheme where nothing resolves is not offered at all.
* **Resolve ambiguity.** Several schemes share a shape — a Ford truck of 1961
  and one of 1970 are both three letters, an engine, a plant and six digits —
  and where more than one reading fits, all of them are returned. Ford's serial
  blocks are themselves ambiguous: the Bronco block starting `A00,001` means
  1967 *and* 1976. Picking one silently would be inventing a fact about
  somebody's truck.
* **Write anything.** This reads a string and returns what it says.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from django.utils.translation import gettext_lazy as _

from .vin_schemes import SCHEMES


@dataclass(frozen=True, slots=True)
class Reading:
    """One field of a VIN, and what the scheme's table says it means."""

    role: str
    label: str
    code: str
    #: Empty when the code is not in the table — which is reported, not filled.
    text: str = ""
    #: Carries a value rather than a code, so no table entry is expected. A
    #: unit number means itself, and GMC's state-designation position is a
    #: dash on nearly every truck ever built.
    free: bool = False

    @property
    def known(self) -> bool:
        return bool(self.text)


@dataclass(frozen=True, slots=True)
class Candidate:
    """One scheme's complete reading of a VIN."""

    scheme: str
    label: str
    make: str
    readings: tuple[Reading, ...]
    #: Every model year this reading could be. More than one is an honest
    #: answer where the scheme cannot narrow it — see `possible_model_years`
    #: for the same problem in the 17-character era.
    years: tuple[int, ...] = ()
    source: str = ""
    notes: str = ""

    @property
    def known(self) -> tuple[Reading, ...]:
        return tuple(reading for reading in self.readings if reading.known)

    @property
    def unknown(self) -> tuple[Reading, ...]:
        return tuple(r for r in self.readings if not r.known and not r.free)

    @property
    def is_complete(self) -> bool:
        return not self.unknown

    @property
    def summary(self) -> str:
        """The one-line reading, for a page that has room for one line."""
        # The year leads the line, so the model-year field does not repeat it.
        parts = [r.text for r in self.known if r.role != "year"]
        if self.years:
            parts.insert(0, self.year_text)
        return " · ".join(parts)

    @property
    def year_text(self) -> str:
        """The years, as a span only where they actually are one.

        Ford's serial blocks overlap in a way that leaves 1976 *or* 1978 open
        with nothing in between, and printing that as "1976–1978" claims a
        third year the reading never offered.
        """
        if not self.years:
            return ""
        if len(self.years) == 1:
            return str(self.years[0])
        contiguous = list(range(self.years[0], self.years[-1] + 1)) == list(self.years)
        if contiguous:
            return f"{self.years[0]}–{self.years[-1]}"
        return str(_(" or ")).join(str(y) for y in self.years)


#: Roles that carry a value rather than a code, so having no table entry is
#: normal rather than a gap. A unit number means itself.
FREE_ROLES = frozenset({"sequence", "unknown"})


def decode(vin: str | None, *, year: int | None = None) -> list[Candidate]:
    """Every reading of `vin` that the tables support, best first.

    `year` narrows rather than decides: it drops schemes whose era does not
    include it and readings whose own years disagree, which is often what turns
    four candidates into one. It is never used to *choose* between readings
    that both fit.
    """
    from .vin import normalize

    vin = normalize(vin)
    if not vin:
        return []

    found = []
    for scheme in SCHEMES:
        if year is not None and not _era_covers(scheme, year):
            continue
        candidate = _read(scheme, vin, year=year)
        if candidate is not None:
            found.append(candidate)

    # Most fields resolved first, then the narrowest year answer, then oldest —
    # a complete reading of a 1978 truck outranks a partial one of anything.
    found.sort(
        key=lambda c: (-len(c.known), len(c.unknown), len(c.years) or 99, c.years or (0,))
    )
    return found


def _era_covers(scheme: dict, year: int) -> bool:
    first, last = scheme["years"]
    return first <= year <= last


def _read(scheme: dict, vin: str, *, year: int | None) -> Candidate | None:
    """Read one VIN under one scheme, or decide the scheme does not apply."""
    if not _fits(scheme, vin):
        return None

    readings = _fields(scheme, vin, year=year)

    # A scheme has to read most of the VIN to be reading it at all. Requiring
    # merely one hit was too weak: `ZZZZZZZZZZZ` came back as a Ford truck
    # because Z is St. Louis, with everything else blank. One code landing in
    # one table across eleven characters is a coincidence of length.
    coded = [r for r in readings if not r.free]
    resolved = [r for r in coded if r.known]
    if len(resolved) < 2 or len(resolved) * 2 < len(coded):
        return None

    years = _resolve_years(scheme, vin, readings)
    if years is None:
        # The parts contradict each other — an engine offered only from 1977
        # beside a model-year digit meaning 1975 — so this is not the scheme
        # this VIN was stamped under.
        return None
    if year is not None and years and year not in years:
        return None

    # Now that the year is settled, say what the codes mean *in that year*
    # rather than listing every era they ever meant something in.
    if len(years) == 1:
        readings = _fields(scheme, vin, year=years[0], fallback=readings)
    readings = _explain_sequence(scheme, readings)

    return Candidate(
        scheme=scheme["id"],
        label=str(scheme["label"]),
        make=scheme["make"],
        readings=tuple(readings),
        years=years,
        source=scheme.get("source", ""),
        notes=str(scheme.get("notes", "")),
    )


#: The longest a running production number gets on any of these sheets. The
#: cap matters: without one, an open-ended field swallows any tail at all, and
#: a 17-character VIN came back as a 1949 Chevrolet because two of four
#: positions happened to land in a table and the other thirteen characters
#: were read as a production number.
OPEN_FIELD_MAX = 6


def _fits(scheme: dict, vin: str) -> bool:
    """Whether the VIN is the length this scheme reads.

    A width of zero means "and the rest", which the earliest schemes need: GM
    stamped a running production number of no fixed length, so a 1949 pickup's
    number is as long as the plant had got that year and a fixed total would
    reject most of them. "The rest" is still bounded — see `OPEN_FIELD_MAX`.
    """
    fixed = sum(f["width"] for f in scheme["fields"])
    open_fields = [f for f in scheme["fields"] if f["width"] == 0]
    if open_fields:
        cap = open_fields[0].get("max", OPEN_FIELD_MAX)
        return fixed < len(vin) <= fixed + cap
    return len(vin) == fixed


def _fields(
    scheme: dict, vin: str, *, year: int | None, fallback: list | None = None
) -> list[Reading]:
    """Split the VIN and look each piece up.

    `fallback` carries the first pass's readings when this is the second: a
    narrowed year must not turn a field that resolved into one that did not,
    which would happen wherever a table is thinner than the era it covers.
    """
    tables = scheme.get("tables", {})
    readings: list[Reading] = []
    at = 0
    for index, spec in enumerate(scheme["fields"]):
        code = vin[at : at + spec["width"]] if spec["width"] else vin[at:]
        at += len(code)
        role = spec["role"]
        table = tables.get(spec.get("table", role), {})
        # A model-year table maps a code to years rather than to prose, so it
        # renders itself: "1978", or "1949, 1950 or 1951" where the scheme
        # genuinely cannot narrow it.
        text = (
            _year_text(table.get(code))
            if role == "year"
            else _lookup(table, code, year=year)
        )
        if not text and fallback:
            text = fallback[index].text
        readings.append(
            Reading(
                role=role,
                label=str(spec["label"]),
                code=code,
                text=text,
                free=bool(spec.get("free")) or role in FREE_ROLES,
            )
        )
    return readings


def _resolve_years(scheme: dict, vin: str, readings: list[Reading]) -> tuple[int, ...] | None:
    """Which model years this VIN can be — or `None` where its parts disagree.

    Two sources, and they check each other. The scheme may *state* a year, by a
    model-year position or by which block of the production run the unit number
    falls in. Separately, every code that only meant something for part of the
    era *permits* a span of years. Ford's blocks overlap — `AE1234` is inside
    both the 1976 block and the 1978 one — and the engine letter beside it is
    what settles which, so the intersection is a better answer than either.
    """
    era = set(range(scheme["years"][0], scheme["years"][1] + 1))
    allowed = era & _permitted(scheme, readings)
    stated = _stated_years(scheme, vin, readings)

    if stated:
        agreed = sorted(set(stated) & allowed)
        return tuple(agreed) if agreed else None
    # Nothing states a year, so the answer is whatever the codes leave open.
    # Reported when that is narrower than the era already on the label, and
    # when the scheme covers a single year — Ford's 1980 trucks have neither a
    # year position nor a serial block, and "1980" is still the answer.
    return tuple(sorted(allowed)) if allowed < era or len(allowed) == 1 else ()


def _permitted(scheme: dict, readings: list[Reading]) -> set[int]:
    """Years left open by the codes that only applied to part of the era."""
    tables = scheme.get("tables", {})
    open_years = set(range(scheme["years"][0], scheme["years"][1] + 1))
    for spec, reading in zip(scheme["fields"], readings, strict=True):
        if reading.role == "year" or not reading.known:
            continue
        entry = tables.get(spec.get("table", spec["role"]), {}).get(reading.code)
        span = _span_of(entry, scheme)
        if span is not None:
            open_years &= span
    return open_years


def _span_of(entry, scheme: dict) -> set[int] | None:
    """Every year an entry allows, or `None` where it allows all of them."""
    if isinstance(entry, dict):
        options = [entry]
    elif isinstance(entry, (list, tuple)):
        options = list(entry)
    else:
        return None

    years: set[int] = set()
    for option in options:
        span = option.get("years")
        if not span:
            # One unconstrained reading means the code says nothing about when.
            return None
        years |= set(range(span[0], span[1] + 1))
    return years


def _stated_years(scheme: dict, vin: str, readings: list[Reading]) -> tuple[int, ...]:
    """The year a scheme names outright, from whichever position carries it.

    Some schemes have a model-year code. Ford's trucks of 1961–79 have none at
    all: the year is which block of the production run the unit number falls
    in, which is why a rule built around character positions was never going to
    read one.
    """
    for reading in readings:
        if reading.role == "year" and reading.known:
            years = scheme["tables"]["year"][reading.code]
            return (years,) if isinstance(years, int) else tuple(years)

    blocks = scheme.get("serial_blocks")
    sequence = next((r.code for r in readings if r.role == "sequence"), "")
    if not blocks or not sequence:
        return ()
    # Lexicographic across the whole field, because these blocks run through
    # the letters as well as the digits — `AE0,001` to `CK9,999` is one 1978.
    return tuple(
        sorted({b["year"] for b in blocks if b["from"] <= sequence <= b["to"]})
    )


def _explain_sequence(scheme: dict, readings: list[Reading]) -> list[Reading]:
    """Say what the unit number told us, where it told us anything.

    Reported as: a 1979 truck showing "DH6036 — not in the table" against its
    consecutive unit number. Two things were wrong with that. The number is a
    number: it carries a value rather than a code, so there is no table for it
    to be missing from. And on a Ford of these years it is the field that
    *determines* the model year — the one part of the VIN that answered the
    question — so reporting it as a gap inverted its meaning exactly.
    """
    blocks = scheme.get("serial_blocks")
    if not blocks:
        return readings
    return [
        (
            reading
            if reading.role != "sequence"
            else Reading(
                role=reading.role,
                label=reading.label,
                code=reading.code,
                text=_block_text(blocks, reading.code),
                free=reading.free,
            )
        )
        for reading in readings
    ]


def _block_text(blocks: list[dict], sequence: str) -> str:
    """Which production block a unit number falls in, named so it can be checked.

    Both are printed where the blocks overlap, because Ford's do: the same
    number is inside the 1976 block and the 1978 one, and showing only the year
    that survived the other checks would hide why anything needed checking.
    """
    hits = [b for b in blocks if b["from"] <= sequence <= b["to"]]
    if not hits:
        return ""
    return ", ".join(
        str(
            _("in the %(year)d block, %(from)s–%(to)s")
            % {"year": b["year"], "from": b["from"], "to": b["to"]}
        )
        for b in hits
    )


def _lookup(table: dict, code: str, *, year: int | None) -> str:
    """What a table says about a code, with year-dependent entries filtered.

    A code can mean two things in one scheme — Ford's `H` is a 390 through 1976
    and a 351M from 1977 — so an entry may carry the years it applies to, and
    several entries may share a code. Knowing the year picks one; not knowing
    it reports both, because a truck is not two engines and saying so is the
    reader's cue that something else has to settle it.
    """
    entry = table.get(code)
    if entry is None:
        return ""
    # Tested for the container rather than for `str`, because a plain reading is
    # a lazy translation proxy and `isinstance(proxy, str)` is False — which
    # would send every single-reading entry down the multi-reading path.
    if isinstance(entry, dict):
        options = [entry]
    elif isinstance(entry, (list, tuple)):
        options = list(entry)
    else:
        return str(entry)

    texts = []
    for option in options:
        span = option.get("years")
        if year is not None and span and not (span[0] <= year <= span[1]):
            continue
        # The span is only worth printing while the year is still open. Once
        # it is settled, "400 CID V8 (1977–1979)" on a truck already labelled
        # 1978 is noise about years this vehicle is not.
        texts.append(
            str(option["text"])
            if not span or year is not None
            else str(
                _("%(text)s (%(from)d–%(to)d)")
                % {"text": option["text"], "from": span[0], "to": span[1]}
            )
        )
    return " / ".join(texts)


def _year_text(entry) -> str:
    """A model-year code rendered, one year or the several it cannot separate."""
    if entry is None:
        return ""
    if isinstance(entry, int):
        return str(entry)
    return ", ".join(str(y) for y in entry)


def describe(vin: str | None, *, year: int | None = None) -> Candidate | None:
    """The single best reading, or nothing. For callers that want one answer.

    Only ever returns a candidate that stands alone: where two schemes read the
    VIN equally well, there is no best one and the caller is told nothing
    rather than told a guess.
    """
    found = decode(vin, year=year)
    if not found:
        return None
    if len(found) > 1:
        best, second = found[0], found[1]
        if len(best.known) == len(second.known):
            return None
    return found[0]
