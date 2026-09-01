"""
The per-vehicle report (SPEC FR-REP-2, G-1).

This is **the sale document**: a complete, dated service history you can hand to
a buyer. Goal G-1 says the record must survive tool changes and be handable to
someone else, and a PDF is how that promise is kept in practice.

Two rules govern what goes in it:

* **Sensitive specs never appear.** Key codes, radio codes and alarm PINs are
  exactly what `is_sensitive` marks, and a document you hand to a stranger is
  exactly where they must not be (§18 C-5).
* **Nothing is inferred.** Every line is something that was recorded. A gap in
  the history shows as a gap, not as an assumption.
"""

from __future__ import annotations

import io
from dataclasses import dataclass, field
from django.utils import timezone
from django.utils.translation import gettext as _
from .runtime import conf

PAGE_MARGIN = 42


def _styles():
    from reportlab.lib.enums import TA_LEFT
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet

    sheet = getSampleStyleSheet()
    sheet.add(ParagraphStyle(name="Small", parent=sheet["Normal"], fontSize=8, leading=10))
    sheet.add(
        ParagraphStyle(
            name="Muted", parent=sheet["Normal"], fontSize=8, leading=10, textColor="#666666"
        )
    )
    sheet.add(
        ParagraphStyle(
            name="H2", parent=sheet["Heading2"], fontSize=12, spaceBefore=14, spaceAfter=6,
            alignment=TA_LEFT,
        )
    )
    return sheet


@dataclass(slots=True)
class Section:
    """One block of the vehicle report, before anything decides how to draw it.

    The report used to be assembled straight into ReportLab objects, which was
    fine while a PDF was the only thing that existed. It stopped being fine the
    moment the same content had to appear on a screen first: a second renderer
    reading the same database is a second set of decisions about what to
    include, and the two drift the first time either is touched. The preview
    would then show a report nobody could download.

    So the content is decided once, here, and drawn twice. The same reasoning
    `liveform.js` uses for regions — one template, two paths — applied to a
    document instead of a page.
    """

    title: str
    columns: list[str]
    rows: list[list[str]]
    #: Column widths in inches, for the PDF. The HTML preview ignores them and
    #: lets the browser lay the table out, which is what a browser is for.
    widths: list[float] = field(default_factory=list)
    #: Which columns hold prose and need wrapping rather than truncation.
    wrap: tuple[int, ...] = ()
    note: str = ""

    @property
    def is_empty(self) -> bool:
        return not self.rows


def report_sections(asset, *, include_costs: bool = True) -> list[Section]:
    """Everything the vehicle report says, as data (FR-REP-2, G-1).

    Sensitive specs are excluded here rather than at the point of drawing, so
    a key code cannot reach either output by way of a renderer that forgot
    (C-5). Empty sections are dropped by the caller, not filtered here, so a
    preview can still say *why* a section is missing where that matters.
    """
    from homeautoshop.core.costs import asset_cost

    sections: list[Section] = []

    identity = []
    if asset.vin:
        identity.append([str(_("VIN")), asset.vin])
    if asset.plate:
        identity.append([str(_("Plate")), f"{asset.plate} {asset.plate_region}".strip()])
    if asset.engine:
        identity.append([str(_("Engine")), asset.engine])
    if asset.transmission:
        identity.append([str(_("Transmission")), asset.transmission])
    current = asset.current_usage
    if current is not None:
        identity.append([str(_("Meter")), f"{current:,.0f} {asset.meter_unit}"])
    identity.append([str(_("Status")), str(asset.get_status_display())])
    sections.append(
        Section(
            title=str(_("Identity")),
            columns=[str(_("Field")), str(_("Value"))],
            rows=identity,
            widths=[1.6, 4.6],
        )
    )

    ownerships = list(asset.ownerships.select_related("person"))
    sections.append(
        Section(
            title=str(_("Ownership")),
            columns=[str(_("Person")), str(_("Role")), str(_("From")), str(_("To"))],
            rows=[
                [
                    row.person.display_name,
                    str(row.get_role_display()),
                    str(row.from_date),
                    str(row.to_date or _("present")),
                ]
                for row in ownerships
            ],
            widths=[2.4, 1.4, 1.2, 1.2],
        )
    )

    work_orders = list(
        asset.work_orders.filter(status="complete")
        .order_by("-completed_at", "-opened_at")
        .prefetch_related("part_usages__part", "job_items")
    )
    history = []
    for wo in work_orders:
        when = wo.completed_at.date() if wo.completed_at else wo.opened_at.date()
        summary = wo.title
        if wo.correction:
            summary += f"\n{wo.correction[:300]}"
        parts = ", ".join(
            f"{u.qty:g}× {u.part.name}" for u in wo.part_usages.all()[:6]
        ) or "—"
        history.append([
            str(when),
            f"{wo.odometer_out:,.0f}" if wo.odometer_out else "—",
            summary,
            parts,
        ])
    sections.append(
        Section(
            title=str(_("Service history")),
            columns=[str(_("Date")), str(_("Meter")), str(_("Work")), str(_("Parts"))],
            rows=history,
            widths=[0.85, 0.75, 3.0, 1.8],
            wrap=(2, 3),
            note="" if history else str(_("No completed work recorded.")),
        )
    )

    inspections = list(
        asset.inspections.filter(status="complete").order_by("-performed_on")[:12]
    )
    sections.append(
        Section(
            title=str(_("Inspections")),
            columns=[
                str(_("Date")), str(_("Inspection")), str(_("Outcome")), str(_("Flagged"))
            ],
            rows=[
                [
                    str(i.performed_on),
                    i.template_name,
                    str(i.get_overall_display()),
                    str(i.results.filter(status__in=["fail", "attention"]).count()),
                ]
                for i in inspections
            ],
            widths=[1.0, 3.0, 1.4, 1.0],
        )
    )

    due = list(asset.service_items.needing_attention().select_related("definition"))
    sections.append(
        Section(
            title=str(_("Due or overdue at time of printing")),
            columns=[str(_("Item")), str(_("Status")), str(_("Next due"))],
            rows=[
                [item.definition.name, str(item.get_status_display()), str(item.next_due_on or "—")]
                for item in due
            ],
            widths=[3.2, 1.4, 1.8],
        )
    )

    open_recalls = list(asset.recalls.exclude(owner_status="completed"))
    sections.append(
        Section(
            title=str(_("Recall campaigns not marked complete")),
            columns=[str(_("Campaign")), str(_("Component")), str(_("Status"))],
            rows=[
                [r.campaign_number, r.component[:80], str(r.get_owner_status_display())]
                for r in open_recalls
            ],
            widths=[1.4, 3.4, 1.6],
            note=str(
                _(
                    "Campaigns are listed by year, make and model. Whether each applies to "
                    "this VIN must be confirmed with the manufacturer."
                )
            )
            if open_recalls
            else "",
        )
    )

    # Never the sensitive ones — decided here so that neither renderer can be
    # the one that forgets (C-5).
    specs = list(asset.specs.filter(is_sensitive=False).order_by("group", "name"))
    sections.append(
        Section(
            title=str(_("Reference specifications")),
            columns=[str(_("Group")), str(_("Item")), str(_("Value"))],
            rows=[
                [str(s.get_group_display()), s.name, s.display_value] for s in specs
            ],
            widths=[1.5, 2.5, 2.2],
        )
    )

    if include_costs:
        rollup = asset_cost(asset)
        sections.append(
            Section(
                title=str(_("Cost of ownership recorded here")),
                columns=[str(_("Category")), str(_("Amount"))],
                rows=(
                    [[line.label, str(line.money)] for line in rollup.lines]
                    + [[str(_("Total")), str(rollup.total)]]
                    if rollup.lines
                    else []
                ),
                widths=[3.4, 2.0],
            )
        )

    return sections


def report_footer() -> str:
    """The sentence both outputs end on, so both make the same disclaimer."""
    return str(
        _(
            "Produced by %(shop)s on %(date)s. This document lists what was recorded; "
            "gaps in it are gaps in the record, not assertions that nothing happened."
        )
        % {"shop": conf.SHOP_NAME, "date": timezone.localdate().isoformat()}
    )


def build_vehicle_report(asset, *, include_costs: bool = True) -> bytes:
    """Draw the sections as a PDF. What goes in them is `report_sections`."""
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.units import inch
    from reportlab.platypus import (
        Paragraph,
        SimpleDocTemplate,
        Spacer,
        Table,
        TableStyle,
    )

    styles = _styles()
    buffer = io.BytesIO()
    document = SimpleDocTemplate(
        buffer,
        pagesize=letter,
        leftMargin=PAGE_MARGIN,
        rightMargin=PAGE_MARGIN,
        topMargin=PAGE_MARGIN,
        bottomMargin=PAGE_MARGIN,
        title=f"{asset.nickname} — {_('Service history')}",
        author=conf.SHOP_NAME,
    )

    def table(rows, widths):
        return Table(
            rows,
            colWidths=widths,
            style=TableStyle([
                ("FONTSIZE", (0, 0), (-1, -1), 8),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LINEBELOW", (0, 0), (-1, -2), 0.25, colors.HexColor("#DDDDDD")),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("LINEBELOW", (0, 0), (-1, 0), 0.75, colors.HexColor("#888888")),
            ]),
            hAlign="LEFT",
        )

    story: list = [
        Paragraph(asset.nickname, styles["Title"]),
        Paragraph(asset.descriptor or "", styles["Normal"]),
        Spacer(1, 8),
    ]

    for index, section in enumerate(report_sections(asset, include_costs=include_costs)):
        if section.is_empty and not section.note:
            continue
        # The identity block leads and needs no heading over it: the vehicle's
        # name is already the title of the page.
        if index:
            story.append(Paragraph(section.title, styles["H2"]))
        if section.is_empty:
            story.append(Paragraph(section.note, styles["Muted"]))
            continue
        rows = [list(section.columns)]
        for row in section.rows:
            rows.append([
                Paragraph(str(cell).replace(chr(10), "<br/>"), styles["Small"])
                if column in section.wrap
                else str(cell)
                for column, cell in enumerate(row)
            ])
        story.append(table(rows, [w * inch for w in section.widths]))
        if section.note:
            story.append(Paragraph(section.note, styles["Muted"]))

    story.append(Spacer(1, 16))
    story.append(Paragraph(report_footer(), styles["Muted"]))

    document.build(story)
    return buffer.getvalue()
