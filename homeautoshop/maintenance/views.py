"""Maintenance views (SPEC §7.7, §9.3)."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.db.models import Count
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy
from django.views.decorators.http import require_POST

from homeautoshop.accounts.models import require
from homeautoshop.accounts.policy import visible_assets, visible_assets_for

from homeautoshop.assets.models import Asset
from homeautoshop.core.described import DescribedFields
from homeautoshop.core.runtime import conf

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
    refresh_asset,
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


class ServiceItemForm(DescribedFields, forms.ModelForm):
    #: A job this shop does that the shared list has never heard of — greasing
    #: a fifth wheel, the boat lift's cables, whatever this particular shed
    #: needs. Not a model field: it *makes* one.
    #:
    #: It exists because there was no route to a new service short of the
    #: Django admin or authoring a whole schedule template to add a single
    #: line, and the picker beside it silently defined what this shop was
    #: allowed to track.
    new_definition = forms.CharField(
        required=False, max_length=120, label=gettext_lazy("…or name a new one")
    )

    descriptions = {
        "definition": gettext_lazy(
            "Which job this is — oil change, brake fluid, timing belt. The "
            "list is shared, so the same job means the same thing on every vehicle."
        ),
        "new_definition": gettext_lazy(
            "A job the list does not have yet. It joins the shared list under "
            "this name, so the next vehicle that needs it picks it from above."
        ),
        "interval_distance": gettext_lazy(
            "How far between services. Leave it empty for a job that is only "
            "ever due by time."
        ),
        "interval_unit": gettext_lazy("The unit that distance is in: mi or km."),
        "interval_months": gettext_lazy(
            "How many months between services. Brake fluid ages whether or not "
            "the vehicle moves, and whichever comes first wins."
        ),
        "interval_hours": gettext_lazy(
            "How many running hours between services, for anything counted by "
            "the hour rather than the mile."
        ),
        "last_done_on": gettext_lazy(
            "When it was last done, if you know. An interval runs from the "
            "last service, so with this blank the schedule can only start the "
            "clock today — and a vehicle new to the shop is then told "
            "everything is fine for a year."
        ),
        "last_done_usage": gettext_lazy(
            "What the meter read when it was last done. The distance half of "
            "the same answer: an oil change at 96,000 miles is due again at "
            "101,000, not five thousand from wherever the truck is now."
        ),
        "notes": gettext_lazy(
            "Anything this vehicle does differently — the filter it takes, "
            "why the interval is shorter than the book says."
        ),
    }

    class Meta:
        model = AssetServiceItem
        fields = [
            "definition", "interval_distance", "interval_unit",
            "interval_months", "interval_hours",
            "last_done_on", "last_done_usage", "notes",
        ]
        # The column names read as storage; these read as the question being
        # asked. "Last done usage" is not what anybody calls an odometer.
        labels = {
            "last_done_on": gettext_lazy("Last done"),
            "last_done_usage": gettext_lazy("Meter when last done"),
        }
        widgets = {
            "notes": forms.Textarea(attrs={"rows": 2}),
            "last_done_on": forms.DateInput(attrs={"type": "date"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name, field in self.fields.items():
            field.required = False
            css = "select" if isinstance(field.widget, forms.Select) else "input"
            field.widget.attrs.setdefault("class", css)

    def clean(self):
        """One service, from the list or newly named, and an interval on it."""
        cleaned = super().clean()
        named = (cleaned.get("new_definition") or "").strip()
        chosen = cleaned.get("definition")

        if named and chosen:
            raise ValidationError(
                _("Pick one from the list or name a new one, not both.")
            )
        if not named and not chosen:
            raise ValidationError(_("Pick a service from the list, or name a new one."))

        # The model says this too. It is repeated here for the sake of
        # ordering: naming a new service writes a shop-wide row, and doing
        # that for a form about to be rejected for carrying no interval would
        # leave the picker one entry longer every time somebody got it wrong.
        if not any(
            cleaned.get(name)
            for name in ("interval_distance", "interval_months", "interval_hours")
        ):
            raise ValidationError(
                _("Give this item at least one interval — distance, time, or hours.")
            )

        if named:
            cleaned["definition"] = self._definition_named(named)
        return cleaned

    @staticmethod
    def _definition_named(name: str) -> ServiceDefinition:
        """That name's definition, reused before a second one is made.

        The list is shop-wide on purpose — the same job means the same thing on
        every vehicle — so `Cabin filter` and `cabin filter` as two rows would
        be two entries in every picker and one job's history split in half.

        `translation_key` stays empty, which is what tells the shipped items
        apart from this one: that key is how a name we ship gets translated,
        and these are the operator's own words in their own language.
        """
        found = ServiceDefinition.objects.filter(name__iexact=name).first()
        return found or ServiceDefinition.objects.create(name=name)

    def clean_last_done_on(self):
        """A service done in the future is a typo, and an expensive one.

        It is the one field here where a mistyped year is silent: the interval
        runs from it, so `2027` instead of `2026` pushes the next service out a
        year and the schedule reports the item as fine the whole time.
        """
        on = self.cleaned_data.get("last_done_on")
        if on and on > timezone.localdate():
            raise ValidationError(_("That date has not happened yet."))
        return on


class ComponentForm(DescribedFields, forms.ModelForm):
    descriptions = {
        "component_type": gettext_lazy(
            "What was fitted: a battery, a tire, a timing belt. It is what "
            "lets the shop ask how old the battery is."
        ),
        "label": gettext_lazy(
            "How you refer to this one — “front left”, “house battery”, "
            "“the spare”."
        ),
        "installed_on": gettext_lazy(
            "When it went on. Age is counted from here, so a warranty and an "
            "expected life both hang off it."
        ),
        "installed_usage": gettext_lazy(
            "What the meter read when it was fitted, so wear can be counted in "
            "miles or hours rather than only in months."
        ),
        "warranty_months": gettext_lazy(
            "How long it is covered for. You are told while it still is."
        ),
        "expected_life_distance": gettext_lazy(
            "How far it should last — a tire's treadwear, a belt's interval. "
            "It is what turns a fitted part into something that comes due."
        ),
        "notes": gettext_lazy(
            "Brand, size, part number, where it was bought. Whatever you would "
            "want in front of you when it needs replacing."
        ),
    }

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
            # The projection is what the list was sorted on, so it comes with
            # the row rather than being worked out a second time.
            "items": [(item, item.projection) for item in rows],
            # The button says how long, so nobody has to press it to find out.
            "snooze_days": conf.SNOOZE_DAYS,
            "overdue": sum(1 for i in rows if i.status == ServiceStatus.OVERDUE),
            "due_soon": sum(1 for i in rows if i.status == ServiceStatus.DUE_SOON),
        },
    )


#: What a row-level confirmation may say, by key.
#:
#: A fixed set, looked up here rather than printed out of the querystring. The
#: URL carries the key and never the words: a message taken off a URL and
#: rendered onto the page is a message anybody with a link can write.
ROW_SAID = {
    "interval": gettext_lazy("Interval saved."),
    "done": gettext_lazy("Recorded."),
}


def _after_row_action(request, asset, item, said: str):
    """Where a row action goes when it is done.

    Back to the Due list when that is where it was pressed. Due is the page
    somebody opens to decide what to do with a Saturday, and it offered no way
    to *do* any of it — every row led to the vehicle's schedule, and the action
    taken there left you on that vehicle with the rest of the list somewhere
    else. On Due the confirmation is the row leaving: done, snoozed and ignored
    all mean "no longer needs attention", which is the list's one rule, so
    nothing needs pinning to a row that is not there any more.
    """
    if request.POST.get("from") == "due":
        return redirect("due_list")
    return _back_to_row(asset, item, said)


def _back_to_row(asset, item, said: str):
    """Back to the schedule, with the answer pinned to the row that asked.

    A confirmation at the top of the page is one nobody sees. `liveform.js`
    swaps this card in place, so the page never moves and the banner it writes
    lands above a fold somebody is not looking at — pressing Set on the
    eleventh item looked exactly like pressing nothing at all, and Done cleared
    the date box and left no trace of where it had gone.

    A querystring rather than a fragment because it has to survive that swap:
    the path is unchanged, so the response is spliced into the region, and
    anything that was only in the URL bar would not be in it. The banner still
    fires as well — it is what a screen reader is listening to, and what the
    no-script path lands on.
    """
    from urllib.parse import urlencode

    query = urlencode({"saved": str(item.pk), "said": said})
    return redirect(f"{reverse('asset_schedule', args=[asset.pk])}?{query}")


@login_required
def asset_schedule(request, pk):
    asset = get_object_or_404(Asset, pk=pk)
    require(request.user, "maintenance.read", asset)
    # Recalculated *before* the list is fetched for display, because the order
    # it is displayed in rests on `next_due_on`. Fetching soonest-first and
    # then recalculating each row left the list in the order of the old dates
    # — and on a vehicle's first visit every date was null, so the page came
    # out alphabetical and called itself soonest-first.
    refresh_asset(asset)
    # `times_done` answers "may this be removed" for the whole list in one
    # query — see `AssetServiceItem.is_removable`.
    items = list(
        asset.service_items.select_related("definition")
        .annotate(times_done=Count("completions"))
        .arranged()
    )

    # Carried on the item rather than as a third element of each row, so the
    # template's `{% for item, projection in rows %}` stays what it is.
    #
    # Split by which control was used, because the row has two of them in two
    # different columns and a tick beside both says less than a tick beside
    # one — the point is *where* the write landed, not merely that it did.
    key = request.GET.get("said", "")
    said = ROW_SAID.get(key)
    saved = request.GET.get("saved", "")
    for item in items:
        hit = said if said and str(item.pk) == saved else ""
        item.saved_interval = hit if key == "interval" else ""
        item.saved_done = hit if key == "done" else ""
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
            # "Last done on the first of January" is a service that happened,
            # and the schedule's own Done button records exactly that as a
            # completion. Entered here it set the interval's anchor and nothing
            # else, so the same fact reached the vehicle's history by one route
            # and not the other. A completion needs a date; a meter reading
            # alone anchors the interval and is left at that, because a
            # completion dated today for work done at an unknown time would be
            # a record of something that did not happen then.
            if item.last_done_on:
                complete(
                    item,
                    on=item.last_done_on,
                    usage=item.last_done_usage,
                    backfill=True,
                )
            else:
                recalculate(item)
            messages.success(request, _("Added to the schedule."))
    else:
        problems = list(form.non_field_errors()) or [
            message for errors in form.errors.values() for message in errors
        ]
        for message in problems or [_("Pick a service item and give it an interval.")]:
            messages.error(request, message)
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
    return _back_to_row(asset, item, "interval")


@require_POST
@login_required
def service_item_move(request, pk, item_id):
    """Move a scheduled item up or down the vehicle's list.

    Buttons rather than dragging, for the reasons `job_item_move` gives: they
    exist before any script loads, they work from a keyboard, and they work on
    a phone held in one oily hand. The neighbor is worked out here, against the
    list as the page shows it — tracked items only, in their current order — so
    the row that moves is the row that was pressed beside, ignored items
    included in nobody's count.

    The first move writes every row's position. Until then `sort_order` is
    zero across the board and the list reads soonest-first; materializing the
    order somebody was looking at is what makes their one swap land where they
    expect rather than somewhere a fresh sort put it.
    """
    asset = _vehicle(request, pk)
    item = get_object_or_404(AssetServiceItem, pk=item_id, asset=asset)

    # Brought up to date first, exactly as the page is before it is drawn, so
    # the neighbor is found in the order somebody was looking at. Read off
    # stale dates the list could be alphabetical while the screen was
    # soonest-first, and the swap landed beside the wrong row.
    refresh_asset(asset)
    items = list(asset.service_items.live().arranged())
    here = next((i for i, row in enumerate(items) if row.pk == item.pk), None)
    if here is not None:
        there = here - 1 if request.POST.get("direction") == "up" else here + 1
        if 0 <= there < len(items):
            items[here], items[there] = items[there], items[here]
            for position, row in enumerate(items):
                if row.sort_order != position:
                    row.sort_order = position
                    row.save(update_fields=["sort_order"])
    return redirect("asset_schedule", pk=asset.pk)


@require_POST
@login_required
def service_item_complete(request, pk, item_id):
    """Back-fill history without inventing a work order (FR-MAINT-6)."""
    asset = _vehicle(request, pk)
    item = get_object_or_404(AssetServiceItem, pk=item_id, asset=asset)
    on = request.POST.get("completed_on") or None
    # Parsed here rather than handed to the column as text: a stray character
    # in the box would otherwise surface as a database error on a form whose
    # whole job is to be quick.
    raw = (request.POST.get("usage") or "").strip()
    try:
        usage = Decimal(raw) if raw else None
    except InvalidOperation:
        messages.error(request, _("That meter reading is not a number."))
        return redirect("asset_schedule", pk=asset.pk)
    complete(
        item,
        on=timezone.datetime.strptime(on, "%Y-%m-%d").date() if on else None,
        usage=usage,
        note=(request.POST.get("note") or "").strip(),
        backfill=True,
    )
    messages.success(request, _("Recorded. The interval has rolled forward."))
    return _after_row_action(request, asset, item, "done")


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
        # "Not now", for as long as the shop says not-now lasts. It was a
        # literal 30 here, which is a policy hidden in a view; `SNOOZE_DAYS`
        # is the same number, kept where the other maintenance thresholds are.
        days = int(request.POST.get("days") or conf.SNOOZE_DAYS)
        item.snooze_until = timezone.localdate() + timezone.timedelta(days=days)
        item.snooze_reason = (request.POST.get("reason") or "").strip()[:200]
        item.save()
        recalculate(item)
        messages.success(request, _("Snoozed for %(n)d days.") % {"n": days})
    if request.POST.get("from") == "due":
        return redirect("due_list")
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
