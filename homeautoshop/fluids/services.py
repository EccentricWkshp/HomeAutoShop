"""
Reading a report in, and reading a series back out (SPEC §7.9a).

Two jobs, and the first one exists because of the second. A panel is thirty
numbers; thirty text boxes is a form nobody fills in twice, and a feature
whose value only appears at the fourth sample cannot afford to be tedious at
the first. So results are **pasted** — the lab's own table, dragged out of the
PDF or the email, one analyte per line — and what could not be read is handed
back rather than dropped.

That last part is the rule the parser is built around. A line the parser does
not understand is shown to the operator with the reason, because the failure
mode of a silent importer is a report that looks complete and is missing the
one row somebody sent the sample to see. It is the same judgment the scan
report transcription makes about a byte it cannot place: refuse it visibly
rather than guess it quietly.

The trends are the point of the table. Everything in `Trend` is arithmetic on
the operator's own samples — a rate, a ratio against the previous one, a count
of what could not be compared. Nothing here decides whether a number is *bad*;
see the model docstring for why not.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation

from django.utils.translation import gettext_lazy as _

from . import analytes
from .models import FluidResult, FluidSample

#: A pasted line: a name, then a number, with whatever the lab put between
#: them. The unit and the lab's own reference figure are optional trailing
#: parts, because half the labs print `Iron 24 ppm (avg 18)` and half print
#: `Iron  24`.
LINE = re.compile(
    r"""^\s*
        (?P<name>[A-Za-z][A-Za-z0-9 @°/.'()+-]*?)   # Iron, Viscosity @ 100C
        \s*[:\t ]\s*
        (?P<value>[<>]?\s*-?\d[\d,]*(?:\.\d+)?)     # 24, 1,240, <1, 10.9
        \s*
        (?P<unit>%|ppm|cSt|cst|SUS|mg\s*KOH/g|Abs/cm|°?[CF])?
        \s*
        (?:\(\s*(?:avg|average|ref|reference|limit)?\s*[:=]?\s*
           (?P<reference>-?\d[\d,]*(?:\.\d+)?)\s*\))?
        \s*(?P<flag>\*|!)?
        \s*$""",
    re.X,
)

#: Headings and footers that turn up in a pasted block and are not results.
NOISE = re.compile(
    r"^\s*(?:element|analyte|unit|units|sample|report|page|date|universal|"
    r"averages?|results?|test|value)s?\b.*$",
    re.I,
)


@dataclass(slots=True)
class ParsedLine:
    """One line of the paste, understood or not."""

    raw: str
    analyte: str = ""
    value: Decimal | None = None
    unit: str = ""
    reference: Decimal | None = None
    flagged: bool = False
    problem: str = ""

    @property
    def ok(self) -> bool:
        return not self.problem

    @property
    def label(self) -> str:
        return analytes.label_for(self.analyte) if self.analyte else self.raw


def _decimal(text: str) -> Decimal | None:
    """`1,240`, `10.9`, `<1` — the last of which a lab means as a detection floor."""
    cleaned = text.replace(",", "").replace(" ", "").lstrip("<>")
    try:
        return Decimal(cleaned)
    except InvalidOperation:
        return None


def parse_results(text: str) -> list[ParsedLine]:
    """Read a pasted panel. Never raises; every line comes back with a verdict.

    An unrecognised *name* is still kept as a result under its own slug — the
    number is the operator's data and a registry that has not heard of an
    element is this application's shortcoming, not theirs. What comes back as
    a problem is a line with no number in it at all, which is the only case
    where there is nothing to store.
    """
    out: list[ParsedLine] = []
    seen: set[str] = set()

    for raw in text.splitlines():
        line = raw.strip()
        if not line or NOISE.match(line):
            continue

        match = LINE.match(line)
        if not match:
            out.append(ParsedLine(raw=line, problem=str(_("no number on this line"))))
            continue

        value = _decimal(match.group("value"))
        if value is None:
            out.append(ParsedLine(raw=line, problem=str(_("could not read the number"))))
            continue

        name = match.group("name").strip()
        known = analytes.find(name)
        slug = known.slug if known else _slugify(name)
        if not slug:
            out.append(ParsedLine(raw=line, problem=str(_("no analyte named on this line"))))
            continue
        if slug in seen:
            out.append(
                ParsedLine(raw=line, problem=str(_("this analyte is already on the report")))
            )
            continue
        seen.add(slug)

        unit = (match.group("unit") or "").strip()
        if not unit and known:
            unit = known.unit

        out.append(
            ParsedLine(
                raw=line,
                analyte=slug,
                value=value,
                unit=unit,
                reference=_decimal(match.group("reference") or "") if match.group("reference") else None,
                flagged=bool(match.group("flag")),
            )
        )
    return out


def _slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.strip().lower()).strip("_")[:48]


def save_results(sample: FluidSample, lines: list[ParsedLine], *, replace: bool = True) -> int:
    """Write the readable lines onto the sample. Returns how many landed."""
    good = [line for line in lines if line.ok]
    if replace:
        # Hard, not soft. A re-paste is a *correction* — somebody fixing a
        # mistyped iron figure — and the superseded figure is not history worth
        # keeping: the trash lists samples, not the rows inside one, so a soft
        # delete here would accumulate results nothing can reach and nobody can
        # restore. The sample itself is what the 30-day trash holds.
        sample.results.all().hard_delete()
    FluidResult.objects.bulk_create(
        FluidResult(
            sample=sample,
            analyte=line.analyte,
            value=line.value,
            unit=line.unit,
            reference=line.reference,
            flagged=line.flagged,
        )
        for line in good
    )
    return len(good)


# ---------------------------------------------------------------------------
# Trends
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class Point:
    on: date
    usage: Decimal | None
    fluid_usage: Decimal | None
    value: Decimal
    unit: str
    fluid_changed: bool = False
    #: Whether this analyte is a running total. Carried on the point rather
    #: than asked of the trend, so `rate` is the single place that decides and
    #: a template cannot forget to ask the second question.
    accumulates: bool = False

    @property
    def rate(self) -> Decimal | None:
        """Per 1,000 units of fluid life.

        `None` in the two cases where a rate would be a lie: an analyte that
        does not accumulate, and a sample that never said how far its fluid
        had run.
        """
        if not self.accumulates:
            return None
        if self.fluid_usage is None or self.fluid_usage <= 0:
            return None
        return (self.value * 1000 / self.fluid_usage).quantize(Decimal("0.01"))


@dataclass(slots=True)
class Trend:
    """One analyte, in one compartment, over every sample there is."""

    analyte: str
    unit: str
    points: list[Point] = field(default_factory=list)

    @property
    def label(self) -> str:
        return analytes.label_for(self.analyte)

    @property
    def kind(self) -> str:
        return analytes.kind_of(self.analyte)

    @property
    def accumulates(self) -> bool:
        return analytes.accumulates(self.analyte)

    @property
    def latest(self) -> Point | None:
        return self.points[-1] if self.points else None

    @property
    def previous(self) -> Point | None:
        return self.points[-2] if len(self.points) > 1 else None

    @property
    def uncomparable(self) -> int:
        """Samples with no fluid interval recorded, so no rate."""
        if not self.accumulates:
            return 0
        return sum(1 for point in self.points if point.rate is None)

    @property
    def is_rated(self) -> bool:
        """True when the two most recent samples can actually be compared.

        `is not None`, not truthiness: a clean sample really can read 0 ppm,
        and a rate of zero is a rate. Asking whether it was *truthy* reported
        the cleanest possible result as an unrecorded one.
        """
        if not self.accumulates or not self.latest or not self.previous:
            return False
        return self.latest.rate is not None and self.previous.rate is not None

    @property
    def change(self) -> Decimal | None:
        """How the latest compares with the one before it, as a multiple.

        Rates where the analyte accumulates, raw values where it does not —
        which is the whole reason `accumulates` exists. `None` when there is
        nothing to compare against, or when the previous figure was zero and a
        multiple would be meaningless rather than infinite.
        """
        if not self.latest or not self.previous:
            return None
        if self.accumulates:
            if not self.is_rated:
                return None
            new, old = self.latest.rate, self.previous.rate
        else:
            new, old = self.latest.value, self.previous.value
        if not old:
            return None
        return (Decimal(new) / Decimal(old)).quantize(Decimal("0.01"))

    @staticmethod
    def _plain(value: Decimal) -> str:
        """`3.00` reads as `3`, and `100` does not become `1E+2`."""
        return format(value.normalize(), "f")

    @property
    def summary(self) -> str:
        """A sentence about the operator's own numbers, and nothing more.

        Deliberately never says whether a value is acceptable: that is the
        lab's judgment and it is stored verbatim on the sample.
        """
        if not self.points:
            return ""
        if len(self.points) == 1:
            return str(_("One sample so far — send another to see a trend."))

        if self.accumulates and not self.is_rated:
            return str(
                _("Not comparable: %(n)s of these samples did not record how far the fluid had run.")
            ) % {"n": self.uncomparable}

        change = self.change
        if change is None:
            return str(_("No comparable previous figure."))
        if change > 1:
            return str(_("%(x)s× the previous sample.")) % {"x": self._plain(change)}
        if change < 1:
            return str(_("Down to %(x)s× the previous sample.")) % {"x": self._plain(change)}
        return str(_("Unchanged from the previous sample."))


def samples_for(asset, *, compartment: str = "", position: str = ""):
    query = FluidSample.objects.filter(asset=asset).prefetch_related("results")
    if compartment:
        query = query.filter(compartment=compartment)
        # Position only narrows within a compartment; on its own it would mean
        # "the front of anything", which is not a series.
        if position:
            query = query.filter(position=position)
    return query.order_by("sampled_on", "created_at")


def series(asset) -> list[tuple[tuple[str, str], list[FluidSample]]]:
    """The asset's samples grouped into comparable series, newest series first."""
    grouped: dict[tuple[str, str], list[FluidSample]] = {}
    for sample in samples_for(asset):
        grouped.setdefault(sample.series_key, []).append(sample)
    return sorted(grouped.items(), key=lambda pair: pair[1][-1].sampled_on, reverse=True)


def trends(asset, *, compartment: str, position: str = "") -> list[Trend]:
    """Every analyte in one compartment, oldest sample first.

    Ordered so the reason somebody sent the sample comes first: wear metals,
    then contamination, then the oil's own condition, then the additives.
    """
    collected: dict[str, Trend] = {}
    for sample in samples_for(asset, compartment=compartment, position=position):
        for result in sample.results.all():
            trend = collected.get(result.analyte)
            if trend is None:
                trend = collected[result.analyte] = Trend(result.analyte, result.unit)
            if not trend.unit:
                trend.unit = result.unit
            trend.points.append(
                Point(
                    on=sample.sampled_on,
                    usage=sample.usage_at_sample,
                    fluid_usage=sample.fluid_usage,
                    value=Decimal(result.value),
                    unit=result.unit,
                    fluid_changed=sample.fluid_changed,
                    accumulates=analytes.accumulates(result.analyte),
                )
            )

    order = {kind: index for index, kind in enumerate(analytes.KIND_ORDER)}
    return sorted(
        collected.values(),
        key=lambda trend: (order.get(trend.kind, 99), trend.label),
    )
