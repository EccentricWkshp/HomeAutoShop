"""
What a parsed scan-tool report contains (SPEC §8.3a).

Plain dataclasses, deliberately free of Django: a parser that imports models is
a parser you cannot run over a corpus of sample files, and the corpus is the
only thing that keeps a profile honest.

Nothing here is authoritative. Every field carries what the tool said, and the
import flow reviews it before anything is written — a scan report is evidence,
not a record.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime


@dataclass(slots=True)
class Tool:
    vendor: str = ""
    model: str = ""
    serial: str = ""


@dataclass(slots=True)
class Vehicle:
    name: str = ""
    year: int | None = None
    vin: str = ""
    # None where the tool reported no reading. The D8 prints "0mile" when it
    # could not read the odometer, and recording that as a genuine zero would
    # reset every mileage-based service interval on the vehicle.
    odometer: int | None = None
    odometer_unit: str = ""
    diagnosis_route: str = ""


@dataclass(slots=True)
class Dtc:
    code: str
    description: str = ""
    module: str = ""
    # Exactly what the tool printed, always. Normalization is lossy and the
    # vocabulary varies by manufacturer, so the original stays for review.
    status_raw: str = ""
    status: str = ""
    # GM reports three separate results rather than one state.
    last_test: str = ""
    this_ignition: str = ""
    since_clear: str = ""


@dataclass(slots=True)
class LiveDatum:
    name: str
    value: str = ""
    maximum: str = ""
    minimum: str = ""
    unit: str = ""
    module: str = ""


@dataclass(slots=True)
class ScanReport:
    tool: Tool = field(default_factory=Tool)
    vehicle: Vehicle = field(default_factory=Vehicle)
    generated_at: datetime | None = None
    modules: list[str] = field(default_factory=list)
    ecu: dict[str, str] = field(default_factory=dict)
    dtcs: list[Dtc] = field(default_factory=list)
    live_data: list[LiveDatum] = field(default_factory=list)
    remark: str = ""
    pages: int = 0
    # Anything the parser could not make sense of, kept so review can show it
    # rather than the import silently dropping a section it did not recognize.
    warnings: list[str] = field(default_factory=list)

    @property
    def modules_with_codes(self) -> list[str]:
        return sorted({d.module for d in self.dtcs if d.module})

    def to_dict(self) -> dict:
        data = asdict(self)
        data["generated_at"] = self.generated_at.isoformat() if self.generated_at else None
        return data


# --------------------------------------------------------------------------
# Bench testers, which print a result rather than a list of codes (SPEC §8.3a)
# --------------------------------------------------------------------------
#
# A scan tool answers *what is wrong*, and :class:`ScanReport` is shaped for
# that: one vehicle, one moment, a list of codes. A bench tester answers *what
# did this measure*, and the shapes are not the same one. A battery tester
# prints a verdict, a handful of readings and its own clock, and it prints that
# **once per test** — a single photograph of the paper coming out of a TOPDON
# BT600 Plus can hold a cranking test and a charging test, taken forty seconds
# apart, each with its own timestamp and its own idea of what `VOLTAGE` means.
#
# Flattening that into the scalar `{field: value}` dictionary the review screen
# already had would have to pick one of the two timestamps and one of the two
# voltages. So a tester report is a *list of results*, and the session keeps it
# as one.


@dataclass(slots=True)
class Value:
    """One value read off a printed report, and how much to believe it.

    Every value carries the same five things because every value on a
    photographed printout is a guess, and the review screen has to be able to
    show which guesses to look at. `raw` is what the reader actually saw, kept
    even when — especially when — the value beside it is empty: a reading that
    failed its own range check is not dropped, it is shown as the characters it
    was read from, with a warning saying why nothing was made of them.

    `corrected` is set by a person, never by a parser. It is what separates
    "the machine read 12.62" from "somebody looked at the paper and typed
    12.62", which is the distinction the whole draft-then-confirm flow exists
    to keep.
    """

    key: str = ""
    label: str = ""
    #: The value as something downstream can use: a number as digits, a verdict
    #: as a stable identifier, a timestamp as ISO 8601. Empty where nothing
    #: trustworthy could be made of `raw`.
    value: str = ""
    unit: str = ""
    #: Exactly what was read, before repair, normalization or validation.
    raw: str = ""
    confidence: float = 0.0
    corrected: bool = False
    #: Whether the tester was **told** this rather than measuring it. A battery
    #: tester is keyed with the capacity printed on the battery's own label
    #: before the test runs, and prints it back on the slip beside what it
    #: measured. Both are numbers with the same unit and they are not the same
    #: kind of fact: 755 CCA is a measurement, 850 CCA is what somebody typed in
    #: — and 755 *against* 850 is the whole result. Shown apart, so a reader
    #: does not take the second for a reading of the battery.
    entered: bool = False
    #: `[x0, top, x1, bottom]` in the coordinates of the page it was read from,
    #: so review can show the crop of the paper this came off. Empty where the
    #: source had no geometry — a re-parse from stored text, say.
    box: list[float] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self, result: int = 0, page: tuple[int, int] | list = ()) -> dict:
        return {
            "key": self.key,
            "label": self.label,
            "value": self.value,
            "unit": self.unit,
            "raw": self.raw,
            "confidence": round(float(self.confidence), 2),
            "corrected": bool(self.corrected),
            "entered": bool(self.entered),
            # The receipt index lives here rather than on the value itself,
            # because a value does not know which result it belongs to and
            # storing the answer twice is one more thing to keep in step.
            #
            # `page` is the size of the picture the box is measured in, and
            # without it the box is unusable: a review screen showing the crop
            # a value came from has pixels and no idea what fraction of the
            # photograph they are.
            "source": {
                "result": result,
                "box": [round(float(n), 1) for n in self.box],
                "page": [int(n) for n in page],
            },
            "warnings": list(self.warnings),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Value":
        source = data.get("source") or {}
        return cls(
            key=str(data.get("key", "")),
            label=str(data.get("label", "")),
            value=str(data.get("value", "")),
            unit=str(data.get("unit", "")),
            raw=str(data.get("raw", "")),
            confidence=float(data.get("confidence") or 0),
            corrected=bool(data.get("corrected")),
            entered=bool(data.get("entered")),
            box=[float(n) for n in (source.get("box") or [])],
            warnings=[str(w) for w in (data.get("warnings") or [])],
        )


@dataclass(slots=True)
class TestResult:
    """One test, off one receipt: a verdict, a time, and what was measured.

    `attributes` and `readings` are both lists of :class:`Value` and the
    difference between them is what you can do with the answer. A reading is a
    measurement — a number and a unit, worth plotting against the last one. An
    attribute is a category the tester was told or worked out: which rating
    standard, which kind of battery. Trending `REGULAR FLOODED` means nothing;
    trending 755 CCA against 850 is the entire point of keeping these.

    Lists rather than dictionaries so the printed order survives. A receipt is
    read top to bottom by whoever is holding it, and a review screen that
    reorders the rows alphabetically makes them check it twice.
    """

    kind: str = ""
    #: Which receipt in the photograph this came off, counting from zero.
    index: int = 0
    verdict: "Value | None" = None
    performed_on: "Value | None" = None
    attributes: list[Value] = field(default_factory=list)
    readings: list[Value] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    #: The bounds of the receipt itself, for the crop shown beside the result.
    box: list[float] = field(default_factory=list)

    def values(self) -> list[Value]:
        head = [v for v in (self.verdict, self.performed_on) if v is not None]
        return head + list(self.attributes) + list(self.readings)

    def reading(self, key: str) -> Value | None:
        for value in self.readings:
            if value.key == key:
                return value
        return None

    def attribute(self, key: str) -> Value | None:
        for value in self.attributes:
            if value.key == key:
                return value
        return None

    @property
    def when(self) -> datetime | None:
        if self.performed_on is None or not self.performed_on.value:
            return None
        try:
            return datetime.fromisoformat(self.performed_on.value)
        except ValueError:
            return None

    @property
    def confidence(self) -> float:
        """The weakest part of it, not the average of the parts.

        A result is only as good as the value somebody would most want to
        query, and averaging a doubtful timestamp against five clean readings
        buries exactly the thing that needed looking at.
        """
        parts = [v.confidence for v in self.values()]
        return min(parts) if parts else 0.0

    def to_dict(self, page: tuple[int, int] | list = ()) -> dict:
        return {
            "kind": self.kind,
            "index": self.index,
            "confidence": round(self.confidence, 2),
            "verdict": self.verdict.to_dict(self.index, page) if self.verdict else None,
            "performed_on": (
                self.performed_on.to_dict(self.index, page) if self.performed_on else None
            ),
            "attributes": [v.to_dict(self.index, page) for v in self.attributes],
            "readings": [v.to_dict(self.index, page) for v in self.readings],
            "warnings": list(self.warnings),
            "box": [round(float(n), 1) for n in self.box],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "TestResult":
        verdict = data.get("verdict")
        when = data.get("performed_on")
        return cls(
            kind=str(data.get("kind", "")),
            index=int(data.get("index") or 0),
            verdict=Value.from_dict(verdict) if verdict else None,
            performed_on=Value.from_dict(when) if when else None,
            attributes=[Value.from_dict(v) for v in (data.get("attributes") or [])],
            readings=[Value.from_dict(v) for v in (data.get("readings") or [])],
            warnings=[str(w) for w in (data.get("warnings") or [])],
            box=[float(n) for n in (data.get("box") or [])],
        )


@dataclass(slots=True)
class TesterReport:
    """Everything one photograph of a tester's paper says."""

    tool: Tool = field(default_factory=Tool)
    results: list[TestResult] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    #: `(width, height)` of the picture every box is measured against. Empty
    #: where the report was read from stored text and has no boxes anyway.
    page: tuple[int, int] | list = ()

    @property
    def performed_on(self) -> datetime | None:
        """The **latest** of the results' own times.

        A photograph is one encounter, and the encounter ended when the last
        test in it did. Taking the first would date the visit by whichever
        receipt happened to be printed at the top of the strip — and on the one
        two-result sample there is, that is the *later* test: the cranking
        result printed above a charging result taken forty-two seconds earlier.
        Print order is not time order, and only one of the two is a fact.
        """
        times = [result.when for result in self.results if result.when]
        return max(times) if times else None

    def to_dict(self) -> dict:
        return {
            "tool": asdict(self.tool),
            "page": [int(n) for n in self.page],
            "test_results": [result.to_dict(self.page) for result in self.results],
            "warnings": list(self.warnings),
        }
