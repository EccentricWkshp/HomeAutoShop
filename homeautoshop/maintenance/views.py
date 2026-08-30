"""Maintenance views (SPEC §7.7, §9.3)."""

from __future__ import annotations

from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST

from homeautoshop.assets.models import Asset

from .models import (
    AssetComponent,
    AssetServiceItem,
    ScheduleTemplate,
    ServiceDefinition,
    ServiceStatus,
)
from .services import apply_template, complete, due_dashboard, project, recalculate


class ServiceItemForm(forms.ModelForm):
    class Meta:
        model = AssetServiceItem
        fields = [
            "definition", "interval_distance", "interval_unit",
            "interval_months", "interval_hours", "notes",
        ]
        widgets = {"notes": forms.Textarea(attrs={"rows": 2})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name, field in self.fields.items():
            field.required = name == "definition"
            css = "select" if isinstance(field.widget, forms.Select) else "input"
            field.widget.attrs.setdefault("class", css)


class ComponentForm(forms.ModelForm):
    class Meta:
        model = AssetComponent
        fields = [
            "component_type", "label", "position", "installed_on", "installed_usage",
            "serial_or_dot_code", "warranty_months", "expected_life_distance", "notes",
        ]
        widgets = {
            "installed_on": forms.DateInput(attrs={"type": "date"}),
            "notes": forms.Textarea(attrs={"rows": 2}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name, field in self.fields.items():
            field.required = name == "component_type"
            css = "select" if isinstance(field.widget, forms.Select) else "input"
            field.widget.attrs.setdefault("class", css)


@login_required
def due_list(request):
    """The landing view for 'what needs attention' (FR-MAINT-7)."""
    rows = due_dashboard()
    return render(
        request,
        "maintenance/due.html",
        {
            "items": [(item, project(item)) for item in rows],
            "overdue": sum(1 for i in rows if i.status == ServiceStatus.OVERDUE),
            "due_soon": sum(1 for i in rows if i.status == ServiceStatus.DUE_SOON),
        },
    )


@login_required
def asset_schedule(request, pk):
    asset = get_object_or_404(Asset, pk=pk)
    items = asset.service_items.select_related("definition").all()
    for item in items:
        recalculate(item)
    templates = [t for t in ScheduleTemplate.objects.filter(is_active=True) if t.applies_to(asset)]
    return render(
        request,
        "maintenance/schedule.html",
        {
            "asset": asset,
            "rows": [(item, project(item)) for item in items],
            "templates": templates,
            "form": ServiceItemForm(),
            "components": asset.components.filter(removed_on__isnull=True),
            "component_form": ComponentForm(),
        },
    )


@require_POST
@login_required
def apply_schedule_template(request, pk):
    asset = get_object_or_404(Asset, pk=pk)
    template = get_object_or_404(ScheduleTemplate, pk=request.POST.get("template"))
    items = apply_template(asset, template)
    messages.success(
        request,
        _("Added %(n)d item(s) from %(name)s. Every interval is yours to edit.")
        % {"n": len(items), "name": template.name},
    )
    return redirect("asset_schedule", pk=asset.pk)


@require_POST
@login_required
def service_item_add(request, pk):
    asset = get_object_or_404(Asset, pk=pk)
    form = ServiceItemForm(request.POST)
    if form.is_valid():
        item = form.save(commit=False)
        item.asset = asset
        try:
            item.full_clean(exclude=["created_by"])
        except ValidationError as exc:
            for message in exc.messages:
                messages.error(request, message)
        else:
            item.save()
            recalculate(item)
            messages.success(request, _("Added to the schedule."))
    else:
        messages.error(request, _("Pick a service item and give it an interval."))
    return redirect("asset_schedule", pk=asset.pk)


@require_POST
@login_required
def service_item_update(request, pk, item_id):
    asset = get_object_or_404(Asset, pk=pk)
    item = get_object_or_404(AssetServiceItem, pk=item_id, asset=asset)
    for field in ("interval_distance", "interval_months", "interval_hours"):
        raw = request.POST.get(field)
        setattr(item, field, int(raw) if raw and raw.isdigit() else None)
    item.save()
    recalculate(item)
    messages.success(request, _("Interval updated."))
    return redirect("asset_schedule", pk=asset.pk)


@require_POST
@login_required
def service_item_complete(request, pk, item_id):
    """Back-fill history without inventing a work order (FR-MAINT-6)."""
    asset = get_object_or_404(Asset, pk=pk)
    item = get_object_or_404(AssetServiceItem, pk=item_id, asset=asset)
    on = request.POST.get("completed_on") or None
    usage = request.POST.get("usage") or None
    complete(
        item,
        on=timezone.datetime.strptime(on, "%Y-%m-%d").date() if on else None,
        usage=usage,
        note=(request.POST.get("note") or "").strip(),
        backfill=True,
    )
    messages.success(request, _("Recorded. The interval has rolled forward."))
    return redirect("asset_schedule", pk=asset.pk)


@require_POST
@login_required
def service_item_snooze(request, pk, item_id):
    asset = get_object_or_404(Asset, pk=pk)
    item = get_object_or_404(AssetServiceItem, pk=item_id, asset=asset)
    action = request.POST.get("action", "snooze")

    if action == "disable":
        item.status = ServiceStatus.DISABLED
        item.save()
        messages.success(request, _("No longer tracked on this vehicle."))
    elif action == "enable":
        item.status = ServiceStatus.OK
        item.snooze_until = None
        item.save()
        recalculate(item)
        messages.success(request, _("Tracking again."))
    else:
        days = int(request.POST.get("days") or 30)
        item.snooze_until = timezone.localdate() + timezone.timedelta(days=days)
        item.snooze_reason = (request.POST.get("reason") or "").strip()[:200]
        item.save()
        recalculate(item)
        messages.success(request, _("Snoozed for %(n)d days.") % {"n": days})
    return redirect("asset_schedule", pk=asset.pk)


@require_POST
@login_required
def component_add(request, pk):
    asset = get_object_or_404(Asset, pk=pk)
    form = ComponentForm(request.POST)
    if form.is_valid():
        component = form.save(commit=False)
        component.asset = asset
        if component.installed_usage is None:
            component.installed_usage = asset.current_usage
        component.save()
        messages.success(request, _("Component recorded."))
    else:
        messages.error(request, _("Check the component details."))
    return redirect("asset_schedule", pk=asset.pk)


@require_POST
@login_required
def component_remove(request, pk, component_id):
    asset = get_object_or_404(Asset, pk=pk)
    component = get_object_or_404(AssetComponent, pk=component_id, asset=asset)
    component.removed_on = timezone.localdate()
    component.removed_usage = asset.current_usage
    component.removal_reason = request.POST.get("reason") or AssetComponent.RemovalReason.WORN
    component.save()
    messages.success(request, _("Removed. Its history stays on the vehicle."))
    return redirect("asset_schedule", pk=asset.pk)
