"""Maintenance views (SPEC §7.7, §9.3)."""

from __future__ import annotations

from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.db.models import Count
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST

from homeautoshop.accounts.models import require
from homeautoshop.accounts.policy import visible_assets, visible_assets_for

from homeautoshop.assets.models import Asset

from .models import (
    AssetComponent,
    AssetServiceItem,
    ScheduleTemplate,
    ServiceDefinition,
    ServiceStatus,
)
from .services import (
    apply_template,
    complete,
    due_dashboard,
    project,
    prune_to_template,
    recalculate,
)


def _vehicle(request, pk, action="maintenance.edit"):
    """The vehicle being worked on, once this person is allowed to work on it.

    Every write on this screen goes through here. The URL gate in
    `accounts/middleware.py` decides whether a helper may reach the schedule
    at all; it cannot decide *whose* schedule, because a URL name says nothing
    about which vehicle the id in it belongs to. That is this check, and these
    views did not have it: a helper granted read on one vehicle could POST an
    interval, a back-dated service or a snooze onto any vehicle in the shop.
    """
    asset = get_object_or_404(Asset, pk=pk)
    require(request.user, action, asset)
    return asset


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
    rows = due_dashboard(user=request.user)
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
    require(request.user, "maintenance.read", asset)
    # `times_done` answers "may this be removed" for the whole list in one
    # query — see `AssetServiceItem.is_removable`.
    items = list(
        asset.service_items.select_related("definition").annotate(times_done=Count("completions"))
    )
    for item in items:
        recalculate(item)
    # Prefetched: the picker counts each template's items so two similarly
    # named ones can be told apart, and that is a query per option otherwise.
    templates = [
        t
        for t in ScheduleTemplate.objects.filter(is_active=True).prefetch_related("items")
        if t.applies_to(asset)
    ]
    return render(
        request,
        "maintenance/schedule.html",
        {
            "asset": asset,
            # Tracked and ignored are two different lists, not one list with a
            # status column. An ignored item is a decision already made;
            # leaving it among the live ones means the answer to "what does
            # this vehicle need" gets longer every time somebody ignores
            # something, which is the opposite of what ignoring it was for.
            "rows": [(item, project(item)) for item in items if item.status != ServiceStatus.DISABLED],
            "ignored": [item for item in items if item.status == ServiceStatus.DISABLED],
            "templates": templates,
            "form": ServiceItemForm(),
            "components": asset.components.filter(removed_on__isnull=True),
            "component_form": ComponentForm(),
        },
    )


@require_POST
@login_required
def apply_schedule_template(request, pk):
    """Apply a template, optionally in place of what is already there.

    Two different intentions share this button. *Add* is the original one and
    stays the default: layer a template on top, keeping everything else.
    *Replace* is switching this vehicle from one schedule to another, which
    until now left the old schedule's items on screen with nothing to do about
    them but ignore each one and watch it stay.
    """
    asset = _vehicle(request, pk)
    template = get_object_or_404(ScheduleTemplate, pk=request.POST.get("template"))
    items = apply_template(asset, template)
    messages.success(
        request,
        _("Added %(n)d item(s) from %(name)s. Every interval is yours to edit.")
        % {"n": len(items), "name": template.name},
    )
    if request.POST.get("replace"):
        removed, kept = prune_to_template(asset, template)
        if kept:
            # Said plainly rather than left to be noticed. Somebody who asked
            # for a replacement and got a partial one is owed the reason.
            messages.info(
                request,
                _(
                    "Removed %(removed)d item(s) the template does not include. "
                    "%(kept)d stayed because they have been done before — those "
                    "are history, so ignore them instead of removing them."
                )
                % {"removed": removed, "kept": kept},
            )
        elif removed:
            messages.info(
                request,
                _("Removed %(n)d item(s) the template does not include.")
                % {"n": removed},
            )
    return redirect("asset_schedule", pk=asset.pk)


@require_POST
@login_required
def service_item_add(request, pk):
    asset = _vehicle(request, pk)
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
    asset = _vehicle(request, pk)
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
    asset = _vehicle(request, pk)
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
    asset = _vehicle(request, pk)
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
def service_item_remove(request, pk, item_id):
    """Take an item off this vehicle's schedule for good.

    The gap this closes: **ignore was the only way to say no.** An item put on
    by the wrong template, or one that stopped applying when the vehicle was
    re-powered, could be switched off but never taken away, so the list only
    ever grew. Nothing in the app could remove one.

    The rule is the one `is_removable` states — an item that has never been
    completed is a plan, and a plan is the operator's to change; an item with
    completions is a record, and ignoring it is the honest way to retire that.
    Refusing loudly here rather than deleting quietly is the whole point: the
    message names the alternative instead of leaving somebody to find it.

    This is a soft delete like everything else, and re-applying any template
    that names the item, or adding it back by hand, revives this same row with
    its history intact rather than starting a second one.
    """
    asset = _vehicle(request, pk)
    item = get_object_or_404(AssetServiceItem, pk=item_id, asset=asset)
    name = item.definition.name

    if not item.is_removable:
        messages.error(
            request,
            _(
                "%(name)s has been done before, so removing it would take the "
                "record with it. Ignore it instead — it stops being tracked "
                "and the history stays."
            )
            % {"name": name},
        )
        return redirect("asset_schedule", pk=asset.pk)

    item.delete()
    messages.success(
        request,
        _("Removed %(name)s from this schedule.") % {"name": name},
    )
    return redirect("asset_schedule", pk=asset.pk)


@require_POST
@login_required
def component_add(request, pk):
    asset = _vehicle(request, pk, "component.edit")
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
    asset = _vehicle(request, pk, "component.edit")
    component = get_object_or_404(AssetComponent, pk=component_id, asset=asset)
    component.removed_on = timezone.localdate()
    component.removed_usage = asset.current_usage
    component.removal_reason = request.POST.get("reason") or AssetComponent.RemovalReason.WORN
    component.save()
    messages.success(request, _("Removed. Its history stays on the vehicle."))
    return redirect("asset_schedule", pk=asset.pk)
