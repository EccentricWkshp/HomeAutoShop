"""
VIN validation and offline inference (SPEC FR-VEH-2, §5.5, §8.1).

All of this runs locally with no network access. Catching a typo before any
API call is the difference between "that VIN has a bad check digit" and a
mysterious empty decode, and it is what keeps vehicle creation working with
the WAN unplugged (P-7).

**Seventeen characters is a rule about 1981, not about VINs.** The
standardized 17-character VIN arrives with the 1981 model year; before that
every manufacturer numbered vehicles its own way and the results are shorter
and shaped differently — a 1973–79 Ford truck carries eleven characters
(`F10GLU12345`: make, series, engine, year, plant, then a five-digit unit
number), GM of the same era carries thirteen, and older vehicles carry less
still. Rejecting those was rejecting the exact population this application
exists for, and it did so while printing a warning that said to save it
anyway — advice the validator itself made impossible to follow.

So a short VIN is accepted and marked as what it is. Nothing about it can be
checked: there is no check digit to compute, no position that means the model
year, and no decoder to ask. The one thing that *can* be checked is whether
the vehicle is old enough for the short form to be plausible, and where the
year is known and 1981 or later, a short VIN is still an error — that is a
typo, and it is the case the length rule was ever any good at catching.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from django.utils.translation import gettext_lazy as _

VIN_LENGTH = 17

#: The model year from which a 17-character VIN is required.
MODERN_VIN_YEAR = 1981

#: Below this, it is a half-typed VIN rather than an old one. Pre-1981 formats
#: run from about five characters upward, so this refuses almost nothing real
#: while still catching a field somebody tabbed out of early.
SHORT_VIN_MIN = 5

# I, O and Q are excluded from VINs precisely because they are confusable with
# 1 and 0 — which is why rejecting them catches so many transcription errors.
#
# The exclusion is part of the 1981 standard, so it has no authority over what
# came before, and this is not hypothetical: in Ford's own 1973–79 truck scheme
# `I` is the assembly-plant code for Highland Park and the 1973 serial block
# starts at `Q00,001`. Rejecting those in a short VIN would reject VINs Ford
# stamped.
FORBIDDEN = set("IOQ")
VIN_RE = re.compile(r"^[A-HJ-NPR-Z0-9]{17}$")
SHORT_VIN_RE = re.compile(r"^[A-Z0-9]+$")

_TRANSLITERATION = {
    **{str(d): d for d in range(10)},
    "A": 1, "B": 2, "C": 3, "D": 4, "E": 5, "F": 6, "G": 7, "H": 8,
    "J": 1, "K": 2, "L": 3, "M": 4, "N": 5, "P": 7, "R": 9,
    "S": 2, "T": 3, "U": 4, "V": 5, "W": 6, "X": 7, "Y": 8, "Z": 9,
}
_WEIGHTS = [8, 7, 6, 5, 4, 3, 2, 10, 0, 9, 8, 7, 6, 5, 4, 3, 2]

# Position 10 encodes model year on a 30-year cycle. Which cycle a VIN belongs
# to is genuinely ambiguous without more context, so callers get both.
_YEAR_CODES = "ABCDEFGHJKLMNPRSTVWXY123456789"


@dataclass(slots=True)
class VinCheck:
    vin: str
    is_well_formed: bool = False
    check_digit_valid: bool | None = None  # None when not applicable
    #: Shorter than 17, so read under whatever scheme its maker used rather
    #: than under the standard. Nothing derived below applies to it.
    is_pre_1981: bool = False
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    wmi: str = ""
    possible_years: list[int] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """Well formed, and the check digit either passes or does not apply."""
        return self.is_well_formed and self.check_digit_valid is not False

    @property
    def status(self) -> str:
        """Maps onto Asset.vin_status."""
        if not self.vin:
            return "none"
        if not self.is_well_formed:
            return "unvalidated"
        if self.is_pre_1981:
            return "pre_1981"
        return "valid" if self.check_digit_valid else "unvalidated"


def normalize(vin: str | None) -> str:
    return (vin or "").strip().upper().replace(" ", "").replace("-", "")


def check_digit(vin: str) -> str:
    """Compute the ISO 3779 check digit for position 9."""
    total = sum(_TRANSLITERATION[c] * w for c, w in zip(vin, _WEIGHTS, strict=True))
    remainder = total % 11
    return "X" if remainder == 10 else str(remainder)


def possible_model_years(code: str, *, reference_year: int = 2026) -> list[int]:
    """Model years a position-10 code could mean, newest first.

    The code repeats every 30 years, so a 'K' is 2019 or 1989. Returning both
    and letting the operator choose beats silently guessing wrong on a project
    car, which is exactly the population where the older answer is right.
    """
    code = code.upper()
    if code not in _YEAR_CODES:
        return []
    index = _YEAR_CODES.index(code)
    years = []
    # 1980 anchors the modern 17-character VIN era.
    base = 1980
    while base + index <= reference_year + 1:
        years.append(base + index)
        base += 30
    return sorted(years, reverse=True)


def validate(
    vin: str | None, *, reference_year: int = 2026, year: int | None = None
) -> VinCheck:
    """Validate a VIN entirely offline.

    `year` is the vehicle's model year where it is known, and it is the only
    thing that makes a length rule meaningful. Without it a short VIN is read
    as a pre-1981 one; with it, 1981 or later means the VIN really is supposed
    to be seventeen characters and a short one is a typo worth refusing.
    """
    vin = normalize(vin)
    result = VinCheck(vin=vin)

    if not vin:
        return result

    if len(vin) > VIN_LENGTH:
        result.errors.append(
            str(_("No VIN is longer than 17 characters; this one has %(n)d."))
            % {"n": len(vin)}
        )
        return result

    if len(vin) < VIN_LENGTH:
        return _validate_pre_1981(result, year=year)

    if bad := sorted(FORBIDDEN & set(vin)):
        result.errors.append(
            str(_("A VIN never contains %(chars)s — those are usually a mistyped 1 or 0."))
            % {"chars": ", ".join(bad)}
        )
        return result

    if not VIN_RE.match(vin):
        result.errors.append(str(_("A VIN uses only letters and digits.")))
        return result

    result.is_well_formed = True
    result.wmi = vin[:3]
    result.possible_years = possible_model_years(vin[9], reference_year=reference_year)

    expected = check_digit(vin)
    result.check_digit_valid = vin[8] == expected
    if not result.check_digit_valid:
        # A warning, not an error: the check digit is a North American
        # convention, and plenty of legitimate imports fail it.
        result.warnings.append(
            str(
                _(
                    "Check digit does not match (expected %(expected)s at position 9). "
                    "Usually a typo — but imported and gray-market vehicles can fail this legitimately."
                )
            )
            % {"expected": expected}
        )
    return result


def _validate_pre_1981(result: VinCheck, *, year: int | None) -> VinCheck:
    """A VIN from before there was a standard, checked as far as one can be.

    Which is barely at all, and saying so is the point. There is no check
    digit, no position that means the model year, and no decoder that knows
    the scheme — so this accepts the characters, records what it is, and does
    not pretend to have verified anything.
    """
    result.is_pre_1981 = True
    length = len(result.vin)

    if year and year >= MODERN_VIN_YEAR:
        result.errors.append(
            str(
                _(
                    "A %(year)s vehicle has a 17-character VIN; this one has %(n)d. "
                    "The shorter formats end with the 1980 model year."
                )
            )
            % {"year": year, "n": length}
        )
        return result

    if length < SHORT_VIN_MIN:
        result.errors.append(
            str(_("%(n)d characters is too short to be a VIN, even a pre-1981 one."))
            % {"n": length}
        )
        return result

    if not SHORT_VIN_RE.match(result.vin):
        result.errors.append(str(_("A VIN uses only letters and digits.")))
        return result

    result.is_well_formed = True
    result.warnings.append(
        str(
            _(
                "Read as a pre-1981 VIN of %(n)d characters. There was no standard "
                "before 1981 — a 1973–79 Ford truck's eleven are complete as they "
                "are — so nothing here can check it or look it up."
            )
        )
        % {"n": length}
    )
    if not year:
        result.warnings.append(
            str(
                _(
                    "If this is a 1981 or later vehicle, its VIN has 17 characters. "
                    "Filling in the model year is what tells these two apart."
                )
            )
        )
    return result


def mask(vin: str | None) -> str:
    """Mask a VIN for logs and list views (NFR-S-5).

    Keeps the length, so the masked form of an eleven-character VIN still
    reads as eleven characters. The head and tail were fixed at three and six,
    which on anything shorter than seventeen *overlapped* — an eleven-character
    VIN came back longer than it started and revealed nine of its characters
    twice over. Short VINs give up less of their tail for the same reason they
    have less to give.
    """
    vin = normalize(vin)
    if len(vin) < 8:
        return "•" * len(vin)
    head, tail = 3, 6 if len(vin) >= VIN_LENGTH else 4
    hidden = len(vin) - head - tail
    if hidden < 1:
        return f"{vin[0]}{'•' * (len(vin) - 1)}"
    return f"{vin[:head]}{'•' * hidden}{vin[-tail:]}"
