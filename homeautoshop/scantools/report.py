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
