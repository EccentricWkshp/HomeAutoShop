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
from decimal import Decimal

from django.conf import settings
from django.utils import timezone
from django.utils.translation import gettext as _

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


def build_vehicle_report(asset, *, include_costs: bool = True) -> bytes:
    """Render the vehicle's history as a PDF."""
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.units import inch
    from reportlab.platypus import (
        KeepTogether,
        Paragraph,
        SimpleDocTemplate,
        Spacer,
        Table,
        TableStyle,
    )

    from homeautoshop.core.costs import asset_cost

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
        author=settings.SHOP_NAME,
    )

    def table(rows, widths, *, header=True):
        style = [
            ("FONTSIZE", (0, 0), (-1, -1), 8),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LINEBELOW", (0, 0), (-1, -2), 0.25, colors.HexColor("#DDDDDD")),
            ("TOPPADDING", (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ]
        if header:
            style += [
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("LINEBELOW", (0, 0), (-1, 0), 0.75, colors.HexColor("#888888")),
            ]
        return Table(rows, colWidths=widths, style=TableStyle(style), hAlign="LEFT")

    story: list = []

    # -- identity -------------------------------------------------------
    story.append(Paragraph(asset.nickname, styles["Title"]))
    story.append(Paragraph(asset.descriptor or "", styles["Normal"]))
    identity = [[_("Field"), _("Value")]]
    if asset.vin:
        identity.append([_("VIN"), asset.vin])
    if asset.plate:
        identity.append([_("Plate"), f"{asset.plate} {asset.plate_region}".strip()])
    if asset.engine:
        identity.append([_("Engine"), asset.engine])
    if asset.transmission:
        identity.append([_("Transmission"), asset.transmission])
    current = asset.current_usage
    if current is not None:
        identity.append([_("Meter"), f"{current:,.0f} {asset.meter_unit}"])
    identity.append([_("Status"), asset.get_status_display()])
    story.append(Spacer(1, 8))
    story.append(table(identity, [1.6 * inch, 4.6 * inch]))

    # -- ownership ------------------------------------------------------
    ownerships = list(asset.ownerships.select_related("person"))
    if ownerships:
        story.append(Paragraph(_("Ownership"), styles["H2"]))
        rows = [[_("Person"), _("Role"), _("From"), _("To")]]
        for row in ownerships:
            rows.append([
                row.person.display_name, row.get_role_display(),
                str(row.from_date), str(row.to_date or _("present")),
            ])
        story.append(table(rows, [2.4 * inch, 1.4 * inch, 1.2 * inch, 1.2 * inch]))

    # -- service history ------------------------------------------------
    story.append(Paragraph(_("Service history"), styles["H2"]))
    work_orders = list(
        asset.work_orders.filter(status="complete")
        .order_by("-completed_at", "-opened_at")
        .prefetch_related("part_usages__part", "job_items")
    )
    if work_orders:
        rows = [[_("Date"), _("Meter"), _("Work"), _("Parts")]]
        for wo in work_orders:
            when = wo.completed_at.date() if wo.completed_at else wo.opened_at.date()
            meter = f"{wo.odometer_out:,.0f}" if wo.odometer_out else "—"
            summary = wo.title
            if wo.correction:
                summary += f"\n{wo.correction[:300]}"
            parts = ", ".join(
                f"{u.qty:g}× {u.part.name}" for u in wo.part_usages.all()[:6]
            ) or "—"
            rows.append([
                str(when), meter,
                Paragraph(summary.replace("\n", "<br/>"), styles["Small"]),
                Paragraph(parts, styles["Small"]),
            ])
        story.append(table(rows, [0.85 * inch, 0.75 * inch, 3.0 * inch, 1.8 * inch]))
    else:
        story.append(Paragraph(_("No completed work recorded."), styles["Muted"]))

    # -- inspections ----------------------------------------------------
    inspections = list(asset.inspections.filter(status="complete").order_by("-performed_on")[:12])
    if inspections:
        story.append(Paragraph(_("Inspections"), styles["H2"]))
        rows = [[_("Date"), _("Inspection"), _("Outcome"), _("Flagged")]]
        for inspection in inspections:
            flagged = inspection.results.filter(status__in=["fail", "attention"]).count()
            rows.append([
                str(inspection.performed_on), inspection.template_name,
                inspection.get_overall_display(), str(flagged),
            ])
        story.append(table(rows, [1.0 * inch, 3.0 * inch, 1.4 * inch, 1.0 * inch]))

    # -- open items -----------------------------------------------------
    due = list(asset.service_items.needing_attention().select_related("definition"))
    if due:
        story.append(Paragraph(_("Due or overdue at time of printing"), styles["H2"]))
        rows = [[_("Item"), _("Status"), _("Next due")]]
        for item in due:
            when = item.next_due_on or "—"
            rows.append([item.definition.name, item.get_status_display(), str(when)])
        story.append(table(rows, [3.2 * inch, 1.4 * inch, 1.8 * inch]))

    open_recalls = list(asset.recalls.exclude(owner_status="completed"))
    if open_recalls:
        story.append(Paragraph(_("Recall campaigns not marked complete"), styles["H2"]))
        rows = [[_("Campaign"), _("Component"), _("Status")]]
        for recall in open_recalls:
            rows.append([
                recall.campaign_number, recall.component[:80], recall.get_owner_status_display()
            ])
        story.append(table(rows, [1.4 * inch, 3.4 * inch, 1.6 * inch]))
        story.append(
            Paragraph(
                _(
                    "Campaigns are listed by year, make and model. Whether each applies to this "
                    "VIN must be confirmed with the manufacturer."
                ),
                styles["Muted"],
            )
        )

    # -- specs (never the sensitive ones) -------------------------------
    specs = list(asset.specs.filter(is_sensitive=False).order_by("group", "name"))
    if specs:
        story.append(Paragraph(_("Reference specifications"), styles["H2"]))
        rows = [[_("Group"), _("Item"), _("Value")]]
        for spec in specs:
            rows.append([spec.get_group_display(), spec.name, spec.display_value])
        story.append(table(rows, [1.5 * inch, 2.5 * inch, 2.2 * inch]))

    # -- cost -----------------------------------------------------------
    if include_costs:
        rollup = asset_cost(asset)
        if rollup.lines:
            story.append(Paragraph(_("Cost of ownership recorded here"), styles["H2"]))
            rows = [[_("Category"), _("Amount")]]
            for line in rollup.lines:
                rows.append([line.label, str(line.money)])
            rows.append([_("Total"), str(rollup.total)])
            story.append(table(rows, [3.4 * inch, 2.0 * inch]))

    # -- footer ---------------------------------------------------------
    story.append(Spacer(1, 16))
    story.append(
        Paragraph(
            _(
                "Produced by %(shop)s on %(date)s. This document lists what was recorded; "
                "gaps in it are gaps in the record, not assertions that nothing happened."
            )
            % {"shop": settings.SHOP_NAME, "date": timezone.localdate().isoformat()},
            styles["Muted"],
        )
    )

    document.build(story)
    return buffer.getvalue()
