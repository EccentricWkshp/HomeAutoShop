"""
VIN validation and offline inference (SPEC FR-VEH-2, §5.5, §8.1).

All of this runs locally with no network access. Catching a typo before any
API call is the difference between "that VIN has a bad check digit" and a
mysterious empty decode, and it is what keeps vehicle creation working with
the WAN unplugged (P-7).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from django.utils.translation import gettext_lazy as _

VIN_LENGTH = 17

# I, O and Q are excluded from VINs precisely because they are confusable with
# 1 and 0 — which is why rejecting them catches so many transcription errors.
FORBIDDEN = set("IOQ")
VIN_RE = re.compile(r"^[A-HJ-NPR-Z0-9]{17}$")

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


def validate(vin: str | None, *, reference_year: int = 2026) -> VinCheck:
    """Validate a VIN entirely offline."""
    vin = normalize(vin)
    result = VinCheck(vin=vin)

    if not vin:
        return result

    if len(vin) != VIN_LENGTH:
        result.errors.append(
            str(_("A VIN is 17 characters; this one has %(n)d.")) % {"n": len(vin)}
        )
        # Pre-1981 vehicles legitimately have shorter VINs.
        if len(vin) < VIN_LENGTH:
            result.warnings.append(
                str(_("Vehicles built before 1981 often have shorter VINs. Save it as-is if that is the case."))
            )
        return result

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


def mask(vin: str | None) -> str:
    """Mask a VIN for logs and list views (NFR-S-5)."""
    vin = normalize(vin)
    if len(vin) < 8:
        return "•" * len(vin)
    return f"{vin[:3]}{'•' * 8}{vin[-6:]}"
