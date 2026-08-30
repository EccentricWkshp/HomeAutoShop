"""Inspection views (SPEC §7.8, §9.2 — filled in under a car, on a phone)."""

from __future__ import annotations

from collections import OrderedDict

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST

from homeautoshop.assets.models import Asset
from homeautoshop.mediafiles.models import MediaLink
from homeautoshop.mediafiles.services import ingest

from .models import Area, Inspection, InspectionResult, InspectionTemplate, ResultStatus
from .services import (
    abandon,
    add_check,
    compare,
    complete,
    convert_to_work_order,
    record,
    resume,
    start,
    wear_projection,
)


@login_required
def inspection_list(request):
    inspections = Inspection.objects.select_related("asset", "performed_by")[:100]
    return render(
        request,
        "inspections/list.html",
        {
            "inspections": inspections,
            "templates": InspectionTemplate.objects.filter(is_active=True),
            # A prospect is a car you are looking at, not one you own (FR-DVI-10).
            "assets": Asset.objects.exclude(status__in=["sold", "parted_out", "totaled"]),
        },
    )


@require_POST
@login_required
def inspection_start(request):
    asset = get_object_or_404(Asset, pk=request.POST.get("asset"))
    template = get_object_or_404(InspectionTemplate, pk=request.POST.get("template"))
    inspection = start(asset, template, user=request.user)
    messages.success(
        request,
        _("Started. %(n)d points to walk.") % {"n": inspection.results.count()},
    )
    return redirect("inspection_detail", pk=inspection.pk)


@login_required
def inspection_detail(request, pk):
    inspection = get_object_or_404(
        Inspection.objects.select_related("asset", "performed_by", "work_order"), pk=pk
    )
    results = inspection.results.all()

    # Grouped by area so the walk follows the vehicle, not the database.
    grouped: "OrderedDict[str, list]" = OrderedDict()
    for result in results:
        grouped.setdefault(result.area, []).append(result)

    photos = {
        link.entity_id: link
        for link in MediaLink.objects.filter(
            entity_type="InspectionResult", entity_id__in=[r.pk for r in results]
        ).select_related("media")
    }

    return render(
        request,
        "inspections/detail.html",
        {
            "inspection": inspection,
            "grouped": grouped,
            "photos": photos,
            "summary": inspection.summarize(),
            "attention": list(inspection.needs_attention),
            "changes": compare(inspection) if not inspection.is_draft else [],
            "missing_photos": inspection.missing_required_photos,
            "statuses": ResultStatus.choices,
            "areas": Area.choices,
        },
    )


@require_POST
@login_required
def result_record(request, pk, result_id):
    inspection = get_object_or_404(Inspection, pk=pk)
    result = get_object_or_404(InspectionResult, pk=result_id, inspection=inspection)
    record(
        result,
        value=request.POST.get("value", None),
        status=request.POST.get("status", ""),
        note=(request.POST.get("note") or "").strip(),
    )
    for upload in request.FILES.getlist("files"):
        ingest(upload, user=request.user, entity=result, role=MediaLink.Role.OTHER)
    return redirect("inspection_detail", pk=inspection.pk)


@require_POST
@login_required
def inspection_complete(request, pk):
    inspection = get_object_or_404(Inspection, pk=pk)
    try:
        complete(inspection, force=bool(request.POST.get("force")))
    except ValidationError as exc:
        for message in exc.messages:
            messages.warning(request, message)
    else:
        messages.success(
            request,
            _("Signed off — overall %(status)s.") % {"status": inspection.get_overall_display()},
        )
    return redirect("inspection_detail", pk=inspection.pk)


@require_POST
@login_required
def inspection_add_check(request, pk):
    """Add a one-off check the template did not anticipate (FR-DVI-3)."""
    inspection = get_object_or_404(Inspection, pk=pk)
    try:
        result = add_check(
            inspection,
            name=request.POST.get("name", ""),
            area=request.POST.get("area", Area.UNDER_HOOD),
            unit=request.POST.get("unit", ""),
            guidance=request.POST.get("guidance", ""),
            is_safety_critical=bool(request.POST.get("is_safety_critical")),
        )
    except ValidationError as exc:
        for message in exc.messages:
            messages.warning(request, message)
    else:
        messages.success(request, _("Added %(name)s.") % {"name": result.name})
    return redirect("inspection_detail", pk=inspection.pk)


@require_POST
@login_required
def result_remove(request, pk, result_id):
    """Remove a check that was added by hand.

    Template points stay put — marking one "not applicable" is the answer
    there, and silently dropping a point from a checklist is how an inspection
    stops meaning anything.
    """
    inspection = get_object_or_404(Inspection, pk=pk)
    result = get_object_or_404(InspectionResult, pk=result_id, inspection=inspection)
    if not result.is_ad_hoc:
        messages.warning(request, _("That check came from the template. Mark it not applicable instead."))
    else:
        result.delete(hard=True)
        messages.success(request, _("Removed."))
    return redirect("inspection_detail", pk=inspection.pk)


@require_POST
@login_required
def inspection_abandon(request, pk):
    """Stop working on an inspection but keep the record (FR-DVI-6)."""
    inspection = get_object_or_404(Inspection, pk=pk)
    try:
        abandon(inspection)
    except ValidationError as exc:
        for message in exc.messages:
            messages.warning(request, message)
    else:
        messages.success(request, _("Abandoned. It is still here if you want to pick it up."))
    return redirect("inspection_detail", pk=inspection.pk)


@require_POST
@login_required
def inspection_resume(request, pk):
    inspection = get_object_or_404(Inspection, pk=pk)
    try:
        resume(inspection)
    except ValidationError as exc:
        for message in exc.messages:
            messages.warning(request, message)
    return redirect("inspection_detail", pk=inspection.pk)


@require_POST
@login_required
def inspection_delete(request, pk):
    """Discard an inspection entirely — recoverable from Trash for 30 days."""
    from homeautoshop.accounts.models import require

    # Not "trash.manage": that is admin-only and gates *restoring*. Discarding
    # your own mistaken inspection is ordinary work, and it is recoverable.
    require(request.user, "inspection.delete")
    inspection = get_object_or_404(Inspection, pk=pk)
    name = inspection.template_name
    inspection.delete()
    messages.success(
        request,
        _("Deleted %(name)s. It is in the trash for 30 days if you want it back.")
        % {"name": name},
    )
    return redirect("inspection_list")


@require_POST
@login_required
def inspection_convert(request, pk):
    inspection = get_object_or_404(Inspection, pk=pk)
    work_order, items = convert_to_work_order(inspection, user=request.user)
    if not items:
        messages.info(request, _("Nothing flagged to turn into work."))
        return redirect("inspection_detail", pk=inspection.pk)
    messages.success(
        request, _("Created %(n)d job item(s).") % {"n": len(items)}
    )
    return redirect("work_order_detail", pk=work_order.pk)


@login_required
def wear_chart(request, pk):
    """Measurement trends for one asset (FR-DVI-11)."""
    asset = get_object_or_404(Asset, pk=pk)
    seen: list[tuple[str, str, str]] = []
    for result in InspectionResult.objects.filter(
        inspection__asset=asset,
        inspection__status=Inspection.Status.COMPLETE,
        measured_value__isnull=False,
    ):
        key = (result.name, result.position, result.unit)
        if key not in seen:
            seen.append(key)

    trends = [
        wear_projection(asset, name, position, unit=unit) for name, position, unit in seen
    ]
    # Anything actually wearing is worth looking at first.
    trends.sort(key=lambda w: (not w.is_projectable, w.point_name, w.position))
    return render(request, "inspections/wear.html", {"asset": asset, "trends": trends})
