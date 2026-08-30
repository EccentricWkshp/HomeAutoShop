"""Work order views — the workbench (SPEC §7.3, §9.3)."""

from __future__ import annotations

import logging

from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST

from homeautoshop.assets.models import Asset
from homeautoshop.assets.services import record_reading
from homeautoshop.mediafiles.models import MediaLink
from homeautoshop.core.costs import work_order_cost
from homeautoshop.mediafiles.services import ingest
from homeautoshop.parts.models import Part
from homeautoshop.parts.services import consume
from homeautoshop.purchasing.views import ExpenseForm

from . import readiness
from .models import (
    JobItem,
    JobItemTool,
    ShopTool,
    TimeEntry,
    WorkOrder,
    WorkOrderNote,
    WorkOrderStatus,
    WorkOrderType,
)

log = logging.getLogger(__name__)


class WorkOrderForm(forms.ModelForm):
    class Meta:
        model = WorkOrder
        fields = [
            "asset", "title", "type", "priority", "complaint", "cause",
            "correction", "odometer_in", "requested_by", "parent",
            "is_safety_critical",
        ]
        widgets = {
            "complaint": forms.Textarea(attrs={"rows": 3}),
            "cause": forms.Textarea(attrs={"rows": 3}),
            "correction": forms.Textarea(attrs={"rows": 3}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["asset"].queryset = Asset.objects.exclude(status="sold")
        self.fields["parent"].queryset = WorkOrder.objects.filter(type=WorkOrderType.PROJECT)
        for name, field in self.fields.items():
            if name not in ("asset", "title"):
                field.required = False
            css = "select" if isinstance(field.widget, forms.Select) else "input"
            if isinstance(field.widget, forms.Textarea):
                css = "input textarea"
            if isinstance(field.widget, forms.CheckboxInput):
                css = ""
            if css:
                field.widget.attrs.setdefault("class", css)


class JobItemForm(forms.ModelForm):
    class Meta:
        model = JobItem
        fields = ["title", "description"]
        widgets = {"description": forms.Textarea(attrs={"rows": 2})}


@login_required
def work_order_list(request):
    status = request.GET.get("status", "open")
    qs = WorkOrder.objects.select_related("asset")
    if status == "open":
        qs = qs.open()
    elif status and status != "all":
        qs = qs.filter(status=status)
    board: dict[str, list] = {}
    for wo in qs[:200]:
        board.setdefault(wo.status, []).append(wo)
    return render(
        request,
        "work/list.html",
        {
            "work_orders": qs[:200],
            "board": board,
            "status": status,
            "statuses": WorkOrderStatus.choices,
        },
    )


@login_required
def work_order_detail(request, pk):
    wo = get_object_or_404(
        WorkOrder.objects.select_related("asset", "requested_by").prefetch_related(
            "job_items", "notes"
        ),
        pk=pk,
    )
    done, total = wo.job_item_progress
    return render(
        request,
        "work/detail.html",
        {
            "wo": wo,
            "asset": wo.asset,
            "job_items": wo.job_items.all(),
            "notes": wo.notes.select_related("author")[:100],
            "photos": MediaLink.for_entity(wo).select_related("media"),
            "job_item_form": JobItemForm(),
            "progress": {"done": done, "total": total},
            "next_statuses": [
                (s, dict(WorkOrderStatus.choices)[s]) for s in _allowed_next(wo)
            ],
            "service_links": wo.asset.service_info_links.select_related("provider"),
            "pinned_specs": wo.asset.specs.filter(is_pinned=True, is_sensitive=False)[:12],
            "part_usages": wo.part_usages.select_related("part", "stock_lot"),
            "expenses": wo.expenses.select_related("vendor"),
            "time_entries": wo.time_entries.select_related("user"),
            "rollup": work_order_cost(wo),
            "expense_form": ExpenseForm(),
            "parts": Part.objects.all()[:500],
            # Empty whenever WrenchLedger is absent, off, or unreachable, so
            # the page renders exactly as it did before the integration existed.
            "tool_warnings": readiness.for_work_order(wo),
            "tools_enabled": readiness.enabled(),
            "job_item_tools": JobItemTool.objects.filter(job_item__work_order=wo)
            .select_related("tool", "job_item"),
        },
    )


def _allowed_next(wo: WorkOrder) -> tuple[str, ...]:
    from .models import TRANSITIONS

    return TRANSITIONS.get(wo.status, ())


@login_required
def work_order_create(request):
    initial = {}
    if asset_id := request.GET.get("asset"):
        asset = Asset.objects.filter(pk=asset_id).first()
        if asset:
            initial["asset"] = asset
            latest = asset.latest_reading()
            if latest:
                initial["odometer_in"] = latest.value

    if request.method == "POST":
        form = WorkOrderForm(request.POST)
        if form.is_valid():
            wo = form.save()
            messages.success(request, _("Opened %(number)s.") % {"number": wo.number})
            return redirect("work_order_detail", pk=wo.pk)
    else:
        form = WorkOrderForm(initial=initial)
    return render(request, "work/form.html", {"form": form, "wo": None})


@login_required
def work_order_edit(request, pk):
    wo = get_object_or_404(WorkOrder, pk=pk)
    if request.method == "POST":
        form = WorkOrderForm(request.POST, instance=wo)
        if form.is_valid():
            form.save()
            messages.success(request, _("Saved."))
            return redirect("work_order_detail", pk=wo.pk)
    else:
        form = WorkOrderForm(instance=wo)
    return render(request, "work/form.html", {"form": form, "wo": wo})


@require_POST
@login_required
def work_order_transition(request, pk):
    wo = get_object_or_404(WorkOrder, pk=pk)
    target = request.POST.get("status", "")
    odometer_out = request.POST.get("odometer_out") or None
    if reason := (request.POST.get("blocked_reason") or "").strip():
        wo.blocked_reason = reason
    try:
        wo.transition_to(target, user=request.user, odometer_out=odometer_out)
    except ValidationError as exc:
        for msg in exc.messages:
            messages.error(request, msg)
    else:
        # Completing captures the meter reading into the asset's history too,
        # so the odometer series stays dense without double entry.
        if target == WorkOrderStatus.COMPLETE and wo.odometer_out and wo.asset.has_meter:
            record_reading(
                wo.asset,
                wo.odometer_out,
                source="work_order",
                note=_("Recorded on completion of %(n)s") % {"n": wo.number},
                user=request.user,
            )
        messages.success(request, _("Moved to %(s)s.") % {"s": wo.get_status_display()})
    return redirect("work_order_detail", pk=wo.pk)


@require_POST
@login_required
def note_create(request, pk):
    wo = get_object_or_404(WorkOrder, pk=pk)
    body = (request.POST.get("body") or "").strip()
    if body:
        WorkOrderNote.objects.create(work_order=wo, body=body, author=request.user)
    return redirect("work_order_detail", pk=wo.pk)


@require_POST
@login_required
def job_item_create(request, pk):
    wo = get_object_or_404(WorkOrder, pk=pk)
    form = JobItemForm(request.POST)
    if form.is_valid():
        item = form.save(commit=False)
        item.work_order = wo
        item.sequence = wo.job_items.count()
        item.save()
    return redirect("work_order_detail", pk=wo.pk)


@require_POST
@login_required
def job_item_toggle(request, pk, item_id):
    wo = get_object_or_404(WorkOrder, pk=pk)
    item = get_object_or_404(JobItem, pk=item_id, work_order=wo)
    item.status = JobItem.Status.TODO if item.is_done else JobItem.Status.DONE
    item.save()
    return redirect("work_order_detail", pk=wo.pk)


@require_POST
@login_required
def work_order_photo(request, pk):
    wo = get_object_or_404(WorkOrder, pk=pk)
    role = request.POST.get("role") or MediaLink.Role.OTHER
    files = request.FILES.getlist("files")
    for upload in files:
        ingest(upload, user=request.user, entity=wo, role=role)
    if files:
        messages.success(request, _("Added %(n)d photo(s).") % {"n": len(files)})
    return redirect("work_order_detail", pk=wo.pk)


@require_POST
@login_required
def part_use(request, pk):
    """Draw a part from stock onto this job (FR-WO-6).

    One flow whether it comes off the shelf or was bought for the job: a
    shortfall is recorded as purchased-for-job rather than refused, because the
    part is installed either way and the record should say so.
    """
    wo = get_object_or_404(WorkOrder, pk=pk)
    part = get_object_or_404(Part, pk=request.POST.get("part"))
    try:
        result = consume(
            part,
            request.POST.get("qty") or 1,
            work_order=wo,
            job_item=JobItem.objects.filter(pk=request.POST.get("job_item") or None).first(),
            user=request.user,
            allow_short=True,
        )
    except ValidationError as exc:
        messages.error(request, " ".join(exc.messages))
    else:
        if result.shortfall:
            messages.warning(
                request,
                _("Recorded. %(n)s came from stock; the rest is marked bought for this job.")
                % {"n": result.usages[0].qty if result.usages else 0},
            )
        else:
            messages.success(request, _("Part recorded and drawn from stock."))
    return redirect("work_order_detail", pk=wo.pk)


@require_POST
@login_required
def time_add(request, pk):
    wo = get_object_or_404(WorkOrder, pk=pk)
    try:
        minutes = int(float(request.POST.get("hours") or 0) * 60)
    except ValueError:
        minutes = 0
    if minutes > 0:
        TimeEntry.objects.create(
            work_order=wo,
            user=request.user,
            minutes=minutes,
            category=request.POST.get("category") or TimeEntry.Category.WRENCHING,
            note=(request.POST.get("note") or "").strip()[:200],
        )
        messages.success(request, _("Time recorded."))
    return redirect("work_order_detail", pk=wo.pk)


# --------------------------------------------------------------------------
# Tool references (SPEC §8.7, FR-WL-2/3)
# --------------------------------------------------------------------------


@require_POST
@login_required
def job_item_tool_add(request, pk, item_id):
    """Point a job item at a tool. Stores the id, never a copy of the record."""
    wo = get_object_or_404(WorkOrder, pk=pk)
    item = get_object_or_404(JobItem, pk=item_id, work_order=wo)

    tool_id = (request.POST.get("tool_id") or "").strip()
    if not tool_id:
        messages.warning(request, _("Choose a tool first."))
        return redirect("work_order_detail", pk=wo.pk)

    tool, _created = ShopTool.objects.get_or_create(
        tool_id=tool_id[:64],
        defaults={"name": (request.POST.get("tool_name") or "").strip()[:160]},
    )
    JobItemTool.objects.get_or_create(job_item=item, tool=tool)

    # Refresh this one tool on the spot rather than waiting for the next
    # scheduled pull. The moment somebody adds a tool to a job is the moment
    # they want to know whether it is here.
    if readiness.enabled():
        try:
            from homeautoshop.core.integrations.wrenchledger import sync

            sync(tool_ids=[tool.tool_id])
        except Exception as exc:  # noqa: BLE001 - the reference stands regardless
            messages.info(
                request,
                _("Recorded, but availability could not be checked just now (%(err)s).")
                % {"err": exc},
            )
            return redirect("work_order_detail", pk=wo.pk)

    messages.success(request, _("Tool added to %(item)s.") % {"item": item.title})
    return redirect("work_order_detail", pk=wo.pk)


@require_POST
@login_required
def job_item_tool_remove(request, pk, reference_id):
    wo = get_object_or_404(WorkOrder, pk=pk)
    reference = get_object_or_404(JobItemTool, pk=reference_id, job_item__work_order=wo)
    reference.delete()
    messages.success(request, _("Tool reference removed."))
    return redirect("work_order_detail", pk=wo.pk)


@login_required
def tool_search(request):
    """Search-as-you-type against WrenchLedger, for the picker.

    Falls back to what is already cached when the API is unreachable, so adding
    a tool you have used before keeps working with the WAN down (G-7).
    """
    query = (request.GET.get("q") or "").strip()
    if not query:
        return JsonResponse({"tools": []})

    if readiness.enabled():
        try:
            from homeautoshop.core.integrations.wrenchledger import (
                WrenchLedgerClient,
                keep_tool_fields,
            )

            rows = [keep_tool_fields(row) for row in WrenchLedgerClient().search_tools(query)]
            return JsonResponse(
                {
                    "tools": [
                        {"id": str(row.get("id")), "name": str(row.get("name") or "")}
                        for row in rows
                        if row.get("id")
                    ],
                    "source": "wrenchledger",
                }
            )
        except Exception as exc:  # noqa: BLE001 - the cache is the fallback
            log.info("tool search fell back to the cache: %s", exc)

    cached = ShopTool.objects.filter(name__icontains=query)[:20]
    return JsonResponse(
        {
            "tools": [{"id": t.tool_id, "name": t.name} for t in cached],
            "source": "cache",
        }
    )
