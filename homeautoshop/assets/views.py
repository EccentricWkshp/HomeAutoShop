"""Asset views (SPEC §7.1, §9.3)."""

from __future__ import annotations

import json
import logging
from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Prefetch
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext as _
from django.utils.translation import ngettext
from django.views.decorators.http import require_GET, require_POST

from homeautoshop.accounts.models import require
from homeautoshop.core.measurements import distance_unit_for
from homeautoshop.mediafiles.models import MediaLink
from homeautoshop.mediafiles.services import ingest
from homeautoshop.work.models import WorkOrder

from . import vin as vinlib
from . import vpic_fields
from .models import (
    Asset,
    AssetSpec,
    Recall,
    SpecGroup,
    AssetKind,
    AssetOwnership,
    AssetServiceInfoLink,
    AssetStatus,
    ServiceInfoProvider,
    UsageReading,
)

log = logging.getLogger(__name__)
from .services import decode_vin, mark_override, record_reading


class AssetForm(forms.ModelForm):
    class Meta:
        model = Asset
        fields = [
            "nickname", "asset_kind", "vehicle_class", "status",
            "vin", "plate", "plate_region", "plate_expires_on",
            "year", "make", "model", "trim", "body_style", "engine",
            "fuel_type", "transmission", "drivetrain", "color_exterior",
            "manufacturer", "model_number", "serial_number",
            "meter", "meter_unit", "acquired_on", "notes",
        ]
        widgets = {
            "notes": forms.Textarea(attrs={"rows": 3}),
            "plate_expires_on": forms.DateInput(attrs={"type": "date"}),
            "acquired_on": forms.DateInput(attrs={"type": "date"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # FR-VEH-1: only a nickname is required. A half-known project car must
        # still be recordable.
        for name, field in self.fields.items():
            field.required = name == "nickname"
            css = "input"
            if isinstance(field.widget, forms.Select):
                css = "select"
            elif isinstance(field.widget, forms.Textarea):
                css = "input textarea"
            field.widget.attrs.setdefault("class", css)

    def clean_vin(self):
        vin = vinlib.normalize(self.cleaned_data.get("vin"))
        if vin:
            check = vinlib.validate(vin)
            if check.errors:
                raise forms.ValidationError(check.errors)
        return vin

    def save(self, commit=True):
        asset = super().save(commit=False)
        # FR-VEH-4: a human correcting a decoded field must survive re-decode.
        if asset.pk and asset.decoded_raw:
            for name in ("year", "make", "model", "trim", "engine", "body_style"):
                if name in self.changed_data:
                    mark_override(asset, name, self.cleaned_data.get(name))
        if commit:
            asset.save()
        return asset


class ReadingForm(forms.Form):
    value = forms.DecimalField(max_digits=12, decimal_places=2, min_value=0)
    read_on = forms.DateField(required=False, widget=forms.DateInput(attrs={"type": "date"}))
    note = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 2}))


@login_required
def asset_list(request):
    kind = request.GET.get("kind", "")
    show_all = request.GET.get("all") == "1"
    qs = Asset.objects.select_related("primary_photo")
    if kind:
        qs = qs.filter(asset_kind=kind)
    if not show_all:
        qs = qs.exclude(status__in=["sold", "parted_out", "totaled"])
    return render(
        request,
        "assets/list.html",
        {
            "assets": qs,
            "kind": kind,
            "show_all": show_all,
            "kinds": AssetKind.choices,
        },
    )


@login_required
def asset_detail(request, pk):
    asset = get_object_or_404(
        Asset.objects.prefetch_related(
            Prefetch("ownerships", queryset=AssetOwnership.objects.select_related("person")),
            Prefetch("service_info_links", queryset=AssetServiceInfoLink.objects.select_related("provider")),
        ),
        pk=pk,
    )
    readings = asset.usage_readings.all()[:20]
    work_orders = asset.work_orders.select_related("asset")[:25]
    photos = MediaLink.for_entity(asset)
    providers = ServiceInfoProvider.objects.filter(is_enabled=True)
    links = {link.provider_id: link for link in asset.service_info_links.all()}
    # Hidden providers are listed separately rather than dropped: a provider
    # that vanished with no way back would be a setting nobody can undo.
    shown = [(p, links.get(p.pk)) for p in providers if not getattr(links.get(p.pk), "is_hidden", False)]
    hidden = [p for p in providers if getattr(links.get(p.pk), "is_hidden", False)]

    story = _group_photos(_timeline(asset))
    recent = story[:RECENT_EVENTS]

    return render(
        request,
        "assets/detail.html",
        {
            "asset": asset,
            # A vehicle with any history is not one anybody means to delete —
            # `sold` is what a car you no longer own is. See `asset_delete`.
            "can_delete": not (
                asset.work_orders.exists()
                or asset.usage_readings.exists()
                or asset.expenses.exists()
                or asset.inspections.exists()
            ),
            # A summary, not the history. What the page is *for* is the meter,
            # the identity and the work — and those were below a scroll of
            # photographs before this was cut down.
            "timeline": recent,
            "history_is_longer": len(story) > RECENT_EVENTS,
            "readings": readings,
            "work_orders": work_orders,
            "photos": photos,
            "reading_form": ReadingForm(),
            "providers": shown,
            "hidden_providers": hidden,
            "vin_check": vinlib.validate(asset.vin) if asset.vin else None,
            # NFR-S-5 — the page carries the mask, and revealing costs a second
            # request. A client-side toggle would put the full VIN in the HTML
            # of every page view, which is masking as decoration: the value is
            # still in the source, the tooltip, and anything that scrapes it.
            "reveal_vin": request.GET.get("vin") == "show",
            "ownership_form": OwnershipForm(),
        },
    )


#: How much of the story fits on the vehicle's own page before it stops being a
#: summary and becomes the page. Everything else is one link away.
RECENT_EVENTS = 8


def _group_photos(events: list[dict]) -> list[dict]:
    """Collapse a day's photographs into a single entry.

    A photo is one row and a work order is one row, and they are not the same
    size of event. Five shots of the same caliper pushed the meter reading, the
    job they belong to and the whole identity panel below the fold — the vehicle
    page became a photo roll with a service history somewhere underneath it.

    By day rather than by run, because "the thirty-first, four photos" is how
    somebody remembers taking them; and a day with one photograph stays one
    photograph, because a group of one is a worse label than the thing itself.
    """
    photos: dict = {}
    merged = [event for event in events if event["kind"] != "media"]

    for event in events:
        if event["kind"] != "media":
            continue
        when = event["when"]
        day = (timezone.localtime(when) if timezone.is_aware(when) else when).date()
        photos.setdefault(day, []).append(event)

    for group in photos.values():
        if len(group) == 1:
            merged.append(group[0])
            continue
        merged.append(
            {
                # The newest moment in it, so the group sorts where its most
                # recent photograph would have.
                "when": max(event["when"] for event in group),
                "kind": "media_group",
                "title": ngettext("%(n)s photo", "%(n)s photos", len(group))
                % {"n": len(group)},
                "detail": "",
                "url": "",
                "children": sorted(group, key=lambda e: e["when"], reverse=True),
            }
        )
    return sorted(merged, key=lambda event: event["when"], reverse=True)


def _timeline(asset: Asset) -> list[dict]:
    """One story in date order (FR-VEH-10)."""
    events: list[dict] = []
    for wo in asset.work_orders.all()[:50]:
        events.append(
            {
                "when": wo.opened_at,
                "kind": "work_order",
                "title": wo.title,
                "detail": wo.get_status_display(),
                "url": f"/work-orders/{wo.pk}/",
                "number": wo.number,
            }
        )
    for reading in asset.usage_readings.all()[:50]:
        events.append(
            {
                "when": timezone.make_aware(
                    timezone.datetime.combine(reading.read_on, timezone.datetime.min.time())
                )
                if timezone.is_naive(
                    timezone.datetime.combine(reading.read_on, timezone.datetime.min.time())
                )
                else reading.created_at,
                "kind": "reading",
                "title": _("Meter reading"),
                "detail": f"{reading.value:,.0f} {reading.unit}"
                + (f" — {reading.note}" if reading.note else ""),
                "url": "",
                "flag": reading.is_rollback,
            }
        )
    # A part fitted with no job behind it (FR-INV-10). Its work-order siblings
    # already appear through the job that used them; these had no route onto
    # this page at all, so "I put that fuel pump in" was recorded and then
    # visible nowhere the vehicle is read.
    from homeautoshop.parts.models import PartUsage

    for usage in (
        PartUsage.objects.filter(asset=asset, work_order__isnull=True)
        .select_related("part")[:50]
    ):
        events.append(
            {
                "when": timezone.make_aware(
                    timezone.datetime.combine(
                        usage.installed_at, timezone.datetime.min.time()
                    )
                ),
                "kind": "part",
                "title": _("Part fitted"),
                "detail": f"{usage.qty:g} × {usage.part}",
                "url": f"/parts/{usage.part.pk}/",
            }
        )
    for link in MediaLink.for_entity(asset).select_related("media")[:30]:
        events.append(
            {
                "when": link.media.captured_at or link.created_at,
                "kind": "media",
                "title": link.caption or _("Photo"),
                "detail": link.get_role_display(),
                "url": link.media.url_for(),
            }
        )
    return sorted(events, key=lambda e: e["when"], reverse=True)[:60]


# Where a create-form decode waits for the save that follows it.
DECODE_STASH = "vin_decode_stash"

# Filled in from a decode, when the person adding the vehicle left them blank.
DECODED_FORM_FIELDS = (
    "year", "make", "model", "trim", "body_style", "fuel_type",
    "transmission", "drivetrain", "engine", "vehicle_class",
)


def _lookup_into_form(request):
    """Decode the VIN typed into the add form and hand back a pre-filled one.

    Deliberately not bound: the draft is usually incomplete at this point, and
    answering a lookup with "this field is required" would punish someone for
    doing the useful thing first. Nothing typed is ever overwritten — a decode
    fills blanks, and the person filling the form outranks vPIC.
    """
    # .dict(), not the QueryDict itself. A QueryDict subclasses dict and stores
    # every value as a list; ModelForm merges initial with dict.update(), which
    # takes the C fast path over that raw storage and never calls the overridden
    # __getitem__. Every field then renders as "['Truck']", and a number input
    # silently shows nothing at all rather than [2007].
    data = request.POST.dict()
    probe = Asset(vin=data.get("vin", ""), asset_kind=data.get("asset_kind") or "vehicle")
    result = decode_vin(probe, user=request.user, save=False)

    if result.ok:
        for name in DECODED_FORM_FIELDS:
            value = getattr(probe, name, "")
            if value and not str(data.get(name) or "").strip():
                data[name] = str(value)
        if not str(data.get("nickname") or "").strip() and probe.descriptor:
            # A nickname is the one required field, and "2013 Ford F-150" is a
            # better starting point than an empty box. It stays editable.
            data["nickname"] = probe.descriptor
        # Keep the decode for the save that follows. Without this the probe is
        # discarded with everything vPIC returned on it, and a vehicle added via
        # the lookup ends up with no stored decode at all — so the detail page
        # shows nothing under "What the VIN says" and the lookup has to be run a
        # second time on a record that was created from one.
        request.session[DECODE_STASH] = {
            "vin": probe.vin,
            "raw": probe.decoded_raw,
            "source": probe.decode_source,
        }
        messages.success(request, result.summary)
    else:
        request.session.pop(DECODE_STASH, None)
        messages.warning(request, result.message)

    return AssetForm(initial=data)


def _apply_stashed_decode(request, asset) -> None:
    """Attach the decode from the create form, if it was for this VIN."""
    stash = request.session.pop(DECODE_STASH, None)
    if not stash or not asset.vin:
        return
    if vinlib.normalize(stash.get("vin")) != vinlib.normalize(asset.vin):
        # The VIN was edited after the lookup; the payload describes a different
        # vehicle and attaching it would be worse than having none.
        return
    asset.decoded_raw = stash.get("raw") or {}
    asset.decode_source = stash.get("source") or "vpic"
    asset.decoded_at = timezone.now()
    asset.save(update_fields=["decoded_raw", "decode_source", "decoded_at"])


@login_required
def asset_create(request):
    require(request.user, "asset.create")
    if request.method == "POST":
        if request.POST.get("action") == "lookup":
            return render(
                request,
                "assets/form.html",
                {"form": _lookup_into_form(request), "asset": None},
            )
        form = AssetForm(request.POST)
        if form.is_valid():
            asset = form.save()
            _apply_stashed_decode(request, asset)
            messages.success(request, _("Added %(name)s.") % {"name": asset.nickname})
            return redirect("asset_detail", pk=asset.pk)
    else:
        initial = {"meter_unit": distance_unit_for(request.user.units or "imperial")}
        form = AssetForm(initial=initial)
    return render(request, "assets/form.html", {"form": form, "asset": None})


@login_required
def asset_edit(request, pk):
    asset = get_object_or_404(Asset, pk=pk)
    require(request.user, "asset.edit", asset)
    if request.method == "POST":
        form = AssetForm(request.POST, instance=asset)
        if form.is_valid():
            form.save()
            messages.success(request, _("Saved."))
            return redirect("asset_detail", pk=asset.pk)
    else:
        form = AssetForm(instance=asset)
    return render(request, "assets/form.html", {"form": form, "asset": asset})


@require_POST
@login_required
def asset_delete(request, pk):
    """Remove a vehicle that should not be here.

    Refused the moment it has any history — work orders, readings, expenses,
    inspections. That refusal is not caution about the delete, which is soft and
    restorable for thirty days (P-5); it is that **a vehicle with history is
    almost never one somebody wants gone.** A car that was sold is `sold`, and
    stays in the record with everything it cost and everything that was done to
    it — which is the whole point of having kept it. Deleting it throws away the
    answer to "what did that Civic actually cost me", permanently, to tidy a
    list that has a status filter on it already.

    What this is for is the other case: a vehicle added twice, or added to the
    wrong instance, which has nothing attached because nothing has happened to
    it yet.
    """
    asset = get_object_or_404(Asset, pk=pk)
    require(request.user, "asset.edit", asset)

    attached = {
        _("work orders"): asset.work_orders.count(),
        _("meter readings"): asset.usage_readings.count(),
        _("expenses"): asset.expenses.count(),
        _("inspections"): asset.inspections.count(),
    }
    held = {label: n for label, n in attached.items() if n}
    if held:
        messages.error(
            request,
            _(
                "%(name)s has %(what)s. A vehicle you no longer own is marked "
                "sold — it keeps its history, which is the reason to have it."
            )
            % {
                "name": asset.nickname or str(asset),
                "what": ", ".join(
                    _("%(n)s %(label)s") % {"n": n, "label": label}
                    for label, n in held.items()
                ),
            },
        )
        return redirect("asset_detail", pk=asset.pk)

    name = asset.nickname or str(asset)
    asset.delete()
    messages.success(request, _("Removed %(name)s.") % {"name": name})
    return redirect("asset_list")


@require_GET
@login_required
def vin_validate(request):
    """Live, offline VIN validation for the form (FR-VEH-2)."""
    check = vinlib.validate(request.GET.get("vin", ""))
    return render(request, "assets/_vin_feedback.html", {"check": check})


@require_POST
@login_required
def vin_decode(request, pk):
    """Explicit user action only — never on page load (SPEC §8.1)."""
    asset = get_object_or_404(Asset, pk=pk)
    require(request.user, "asset.edit", asset)
    result = decode_vin(asset, user=request.user)
    if result.ok:
        messages.success(request, result.summary)
        if result.thin:
            messages.info(request, result.message)
        if result.skipped_overridden:
            messages.info(
                request,
                _("Left your edits alone: %(fields)s.")
                % {"fields": ", ".join(result.skipped_overridden)},
            )
    else:
        messages.warning(request, result.message)
    return redirect("asset_detail", pk=asset.pk)


@require_POST
@login_required
def reading_create(request, pk):
    asset = get_object_or_404(Asset, pk=pk)
    form = ReadingForm(request.POST)
    if form.is_valid():
        try:
            reading = record_reading(
                asset,
                form.cleaned_data["value"],
                read_on=form.cleaned_data.get("read_on"),
                note=form.cleaned_data.get("note", ""),
                user=request.user,
            )
        except Exception as exc:  # decreasing reading without a note
            messages.error(request, str(exc))
        else:
            if reading.is_rollback:
                messages.warning(
                    request,
                    _("Recorded, and flagged as lower than the previous reading."),
                )
            else:
                messages.success(request, _("Reading recorded."))
    else:
        messages.error(request, _("Check the reading and try again."))
    return redirect("asset_detail", pk=asset.pk)


@require_POST
@login_required
def photo_upload(request, pk):
    asset = get_object_or_404(Asset, pk=pk)
    files = request.FILES.getlist("files")
    created = 0
    for upload in files:
        _media, was_new = ingest(
            upload, user=request.user, entity=asset, role=MediaLink.Role.OTHER
        )
        created += was_new
    if files:
        messages.success(
            request,
            _("Added %(n)d photo(s).") % {"n": created}
            if created
            else _("Those photos were already on file."),
        )
    return redirect("asset_detail", pk=asset.pk)


@require_POST
@login_required
def service_info_pin(request, pk, provider_id):
    """Resolve once, pin forever (SPEC §8.5)."""
    asset = get_object_or_404(Asset, pk=pk)
    provider = get_object_or_404(ServiceInfoProvider, pk=provider_id)
    url = (request.POST.get("url") or "").strip()
    if not url.startswith("http"):
        messages.error(request, _("Paste the full address of the manual page."))
        return redirect("asset_detail", pk=asset.pk)
    AssetServiceInfoLink.objects.update_or_create(
        asset=asset,
        provider=provider,
        defaults={
            "url": url,
            "label": (request.POST.get("label") or "").strip(),
            "subscription_status": request.POST.get("subscription_status") or "unknown",
            "last_verified_at": timezone.now(),
        },
    )
    messages.success(request, _("Pinned %(provider)s for this vehicle.") % {"provider": provider.name})
    return redirect("asset_detail", pk=asset.pk)


@require_POST
@login_required
def service_info_unpin(request, pk, provider_id):
    asset = get_object_or_404(Asset, pk=pk)
    AssetServiceInfoLink.objects.filter(asset=asset, provider_id=provider_id).delete()
    return redirect("asset_detail", pk=asset.pk)


class OwnershipForm(forms.ModelForm):
    class Meta:
        model = AssetOwnership
        fields = ["person", "role", "from_date"]
        widgets = {"from_date": forms.DateInput(attrs={"type": "date"})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["from_date"].required = False
        for field in self.fields.values():
            css = "select" if isinstance(field.widget, forms.Select) else "input"
            field.widget.attrs.setdefault("class", css)


@require_POST
@login_required
def ownership_add(request, pk):
    """FR-OWN-1/2 — ownership is dated history, not a foreign key."""
    asset = get_object_or_404(Asset, pk=pk)
    form = OwnershipForm(request.POST)
    if not form.is_valid():
        messages.error(request, _("Pick a person and a role."))
        return redirect("asset_detail", pk=asset.pk)

    ownership = form.save(commit=False)
    ownership.asset = asset
    if not ownership.from_date:
        ownership.from_date = timezone.localdate()

    # One current owner at a time: taking ownership ends the previous run
    # rather than silently creating two open-ended owners.
    if ownership.role == AssetOwnership.Role.OWNER:
        asset.ownerships.filter(role=AssetOwnership.Role.OWNER, to_date__isnull=True).update(
            to_date=ownership.from_date
        )
    ownership.save()
    messages.success(
        request, _("%(name)s is now on record.") % {"name": ownership.person.display_name}
    )
    return redirect("asset_detail", pk=asset.pk)


@require_POST
@login_required
def ownership_end(request, pk, ownership_id):
    asset = get_object_or_404(Asset, pk=pk)
    ownership = get_object_or_404(AssetOwnership, pk=ownership_id, asset=asset)
    ownership.to_date = timezone.localdate()
    ownership.save()
    messages.success(request, _("Ownership ended. The history stays on the record."))
    return redirect("asset_detail", pk=asset.pk)


class SpecForm(forms.ModelForm):
    class Meta:
        model = AssetSpec
        fields = ["group", "name", "value", "unit", "condition", "source", "is_sensitive", "is_pinned"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name, field in self.fields.items():
            field.required = name in ("group", "name", "value")
            if not isinstance(field.widget, forms.CheckboxInput):
                css = "select" if isinstance(field.widget, forms.Select) else "input"
                field.widget.attrs.setdefault("class", css)


@login_required
def asset_timeline(request, pk):
    """The whole story, ungrouped (FR-VEH-10).

    The vehicle's own page carries a summary of this, and the summary is the
    right thing there: somebody opening a vehicle wants the meter, the identity
    and the open work, not a scroll of photographs. But the history is the
    reason the records exist, so it gets a page where nothing competes with it —
    and here every photograph is its own row again, because on a page about the
    history, "four photos" is the answer to a question nobody asked.
    """
    asset = get_object_or_404(Asset, pk=pk)
    return render(
        request,
        "assets/timeline.html",
        {"asset": asset, "timeline": _timeline(asset)},
    )


@login_required
def asset_specs(request, pk):
    """The lookup that interrupts a job most often (FR-SPEC-1..4)."""
    asset = get_object_or_404(Asset, pk=pk)
    grouped: dict = {}
    for spec in asset.specs.select_related("source_media"):
        grouped.setdefault(spec.get_group_display(), []).append(spec)

    # `?edit=<id>` swaps one row for a filled-in form. A round trip rather than
    # a script, which keeps the page working the way the rest of the app does
    # and means the form is the same one that validates the write.
    editing = None
    edit_form = None
    requested = request.GET.get("edit")
    if requested:
        editing = asset.specs.filter(pk=requested).first()
        if editing is not None:
            edit_form = SpecForm(instance=editing)

    return render(
        request,
        "assets/specs.html",
        {
            "asset": asset,
            "grouped": grouped,
            "form": SpecForm(),
            "editing": editing,
            "edit_form": edit_form,
            "others": Asset.objects.exclude(pk=asset.pk),
        },
    )


@require_POST
@login_required
def spec_edit(request, pk, spec_id):
    """Correct a spec that is already on file.

    Distinct from the add form, which upserts by name and therefore cannot
    rename anything. Here the name, group and condition are all editable, so a
    change can collide with a different spec — refused with an explanation
    rather than surfacing the unique constraint as a 500.

    `is_sensitive` *can* be cleared here, unlike on the add path. The checkbox
    arrives pre-filled with the current value, so unticking it is a deliberate
    act on a visible state rather than an omission that happens to look like one.
    """
    asset = get_object_or_404(Asset, pk=pk)
    require(request.user, "asset.edit", asset)
    spec = get_object_or_404(AssetSpec, pk=spec_id, asset=asset)

    form = SpecForm(request.POST, instance=spec)
    if not form.is_valid():
        messages.error(request, _("A spec needs a group, a name and a value."))
        return redirect(f"{reverse('asset_specs', args=[asset.pk])}?edit={spec.pk}")

    candidate = form.save(commit=False)
    clash = (
        AssetSpec.objects.filter(
            asset=asset,
            group=candidate.group,
            name=candidate.name,
            condition=candidate.condition,
        )
        .exclude(pk=spec.pk)
        .first()
    )
    if clash is not None:
        messages.error(
            request,
            _("%(name)s is already on the sheet under that group. Edit that one instead.")
            % {"name": clash.name},
        )
        return redirect(f"{reverse('asset_specs', args=[asset.pk])}?edit={spec.pk}")

    candidate.save()
    messages.success(request, _("Updated %(name)s.") % {"name": candidate.name})
    return redirect("asset_specs", pk=asset.pk)


def _upsert_spec(
    asset,
    *,
    group,
    name,
    value,
    unit="",
    condition="",
    source="manual",
    is_sensitive=False,
    is_pinned=False,
):
    """Create a spec, or correct the one already under that name.

    Returns `(spec, created)`. Three callers share this: the add form, promoting
    a decoded field, and importing a scan report. They must agree, because the
    unique constraint is on (asset, group, name, condition) and disagreeing
    would mean one of them raising IntegrityError.
    """
    existing = AssetSpec.objects.filter(
        asset=asset, group=group, name=name, condition=condition
    ).first()
    if existing is not None:
        existing.value = value
        existing.unit = unit
        existing.source = source
        existing.is_pinned = is_pinned
        # Only ever added by an update, never removed: the model defaults
        # security-adjacent groups to sensitive on create, and letting a later
        # correction strip that would put a door code in the next report.
        existing.is_sensitive = existing.is_sensitive or is_sensitive
        existing.save()
        return existing, False

    spec = AssetSpec(
        asset=asset,
        group=group,
        name=name,
        value=value,
        unit=unit,
        condition=condition,
        source=source,
        is_sensitive=is_sensitive,
        is_pinned=is_pinned,
    )
    spec.save()
    return spec, True


@require_POST
@login_required
def spec_from_decode(request, pk):
    """Put one decoded field onto the spec sheet (FR-SPEC-1, SPEC §8.1).

    Per row, and never automatic. vPIC is a registration database — it has no
    torque values or fluid capacities at all, and it is frequently wrong about
    trim — so which of its answers is worth relying on mid-job is a judgment
    the person makes, not one the decode earns by existing.
    """
    asset = get_object_or_404(Asset, pk=pk)
    require(request.user, "asset.edit", asset)

    key = request.POST.get("key", "")
    value = str((asset.decoded_raw or {}).get(key) or "").strip()
    if not vpic_fields.is_meaningful(value):
        messages.warning(request, _("There is nothing recorded for that field."))
        return redirect("asset_detail", pk=asset.pk)

    group, unit = vpic_fields.target_for(key)
    spec, created = _upsert_spec(
        asset,
        group=group,
        name=str(vpic_fields.LABELS_BY_KEY.get(key, key)),
        value=value[:200],
        unit=unit,
        source="decoded",
    )
    messages.success(
        request,
        (_("Added %(name)s to the spec sheet.") if created else _("Updated %(name)s."))
        % {"name": spec.name},
    )
    return redirect("asset_detail", pk=asset.pk)


@require_POST
@login_required
def spec_add(request, pk):
    asset = get_object_or_404(Asset, pk=pk)
    form = SpecForm(request.POST)
    if form.is_valid():
        spec = form.save(commit=False)
        spec.asset = asset

        # Entering a name that is already on file means correcting it, not
        # creating a second one — there is no edit screen for a spec, so
        # refusing would leave deleting and retyping as the only way to fix a
        # digit. The unique constraint made that an IntegrityError and a 500.
        _existing, created = _upsert_spec(
            asset,
            group=spec.group,
            name=spec.name,
            value=spec.value,
            unit=spec.unit,
            condition=spec.condition,
            source=spec.source,
            is_sensitive=spec.is_sensitive,
            is_pinned=spec.is_pinned,
        )
        if not created:
            messages.success(request, _("Updated %(name)s.") % {"name": _existing.name})
            return redirect("asset_specs", pk=asset.pk)

        spec = _existing
        if spec.is_sensitive:
            messages.success(
                request,
                _("Saved, and marked sensitive — it stays out of reports and shared exports."),
            )
        else:
            messages.success(request, _("Saved."))
    else:
        messages.error(request, _("A spec needs a group, a name and a value."))
    return redirect("asset_specs", pk=asset.pk)


# What a scan report calls a thing, and what it is worth calling on a spec
# sheet. These are the values you go digging for when matching a reflash or a
# replacement module, and they are read straight off the vehicle rather than
# inferred — which is what makes them worth trusting in a way a VIN decode is
# not.
ECU_SPEC_NAMES = {
    "VIN": None,  # already on the record; importing it as a spec is noise
    "Software part number": "Software part number",
    "Calibration part number": "Calibration part number",
    "Base model part number": "Base model part number",
    "End model part number": "End model part number",
}


def _ecu_rows(report) -> list[tuple[str, str]]:
    """The identifiers worth keeping, in the order the tool printed them."""
    rows = []
    for key, value in (report.ecu or {}).items():
        name = ECU_SPEC_NAMES.get(key, key)
        if name and str(value).strip():
            rows.append((name, str(value).strip()))
    return rows


@require_POST
@login_required
def spec_from_scan(request, pk):
    """Read ECU identifiers off a scan-tool report (SPEC §8.3a, FR-SPEC-1).

    Two steps on purpose. The first parses and shows what was found; the second
    writes it. A scan report names a vehicle, and attaching one car's module
    identifiers to another is the kind of mistake that is invisible afterwards —
    so the VIN is checked, and what will be written is shown before it is.
    """
    from homeautoshop import scantools

    asset = get_object_or_404(Asset, pk=pk)
    require(request.user, "asset.edit", asset)

    if request.POST.get("confirm"):
        rows = json.loads(request.POST.get("payload") or "[]")
        written = 0
        for name, value in rows:
            _upsert_spec(
                asset,
                group="electrical",
                name=str(name)[:120],
                value=str(value)[:200],
                source="scan_tool",
            )
            written += 1
        messages.success(
            request,
            _("Recorded %(n)d identifier(s) from the scan report.") % {"n": written},
        )
        return redirect("asset_specs", pk=asset.pk)

    upload = request.FILES.get("report")
    if not upload:
        messages.warning(request, _("Choose a scan-tool report to read."))
        return redirect("asset_specs", pk=asset.pk)

    try:
        report = scantools.parse(upload)
    except Exception as exc:  # noqa: BLE001 - surfaced, not swallowed
        log.exception("scan report parse failed")
        messages.error(
            request,
            _("That file could not be read as a scan report: %(detail)s") % {"detail": exc},
        )
        return redirect("asset_specs", pk=asset.pk)

    found = report.vehicle.vin
    if found and asset.vin and vinlib.normalize(found) != vinlib.normalize(asset.vin):
        # Refused rather than warned. Module identifiers from another vehicle
        # look exactly like this one's afterwards, and nothing later would
        # reveal the mistake.
        messages.error(
            request,
            _("That report is for VIN %(found)s, which is not this vehicle.")
            % {"found": vinlib.mask(found)},
        )
        return redirect("asset_specs", pk=asset.pk)

    rows = _ecu_rows(report)
    if not rows:
        messages.info(
            request,
            _("No module identifiers in that report — not every scan records them."),
        )
        return redirect("asset_specs", pk=asset.pk)

    return render(
        request,
        "assets/specs_from_scan.html",
        {
            "asset": asset,
            "report": report,
            "rows": rows,
            "payload": json.dumps(rows),
            "vin_unverified": not asset.vin or not found,
        },
    )


@require_POST
@login_required
def spec_delete(request, pk, spec_id):
    asset = get_object_or_404(Asset, pk=pk)
    get_object_or_404(AssetSpec, pk=spec_id, asset=asset).delete()
    return redirect("asset_specs", pk=asset.pk)


@require_POST
@login_required
def spec_copy(request, pk):
    """Copy a spec sheet from another vehicle — the second car of the same model."""
    asset = get_object_or_404(Asset, pk=pk)
    source = get_object_or_404(Asset, pk=request.POST.get("source"))
    copied = 0
    for spec in source.specs.all():
        _obj, created = AssetSpec.objects.get_or_create(
            asset=asset,
            group=spec.group,
            name=spec.name,
            condition=spec.condition,
            defaults={
                "value": spec.value,
                "unit": spec.unit,
                "source": spec.source,
                "is_sensitive": spec.is_sensitive,
                "is_pinned": spec.is_pinned,
                "notes": spec.notes,
            },
        )
        copied += created
    messages.success(
        request,
        _("Copied %(n)d spec(s) from %(name)s.") % {"n": copied, "name": source.nickname},
    )
    return redirect("asset_specs", pk=asset.pk)


@login_required
def asset_recalls(request, pk):
    asset = get_object_or_404(Asset, pk=pk)
    from . import recalls as recall_service

    return render(
        request,
        "assets/recalls.html",
        {
            "asset": asset,
            "recalls": asset.recalls.all(),
            "statuses": Recall.OwnerStatus.choices,
            "vin_lookup_url": recall_service.vin_lookup_url(asset),
            "region_supported": recall_service.region_of(asset) in recall_service.SUPPORTED_REGIONS,
        },
    )


@require_POST
@login_required
def recall_check(request, pk):
    from . import recalls as recall_service

    asset = get_object_or_404(Asset, pk=pk)
    result = recall_service.check(asset, user=request.user)
    if result.ok:
        messages.success(request, result.message)
    else:
        messages.warning(request, result.message)
    return redirect("asset_recalls", pk=asset.pk)


@require_POST
@login_required
def recall_status(request, pk, recall_id):
    """Operator-maintained: NHTSA's free data cannot tell us (SPEC §8.4)."""
    asset = get_object_or_404(Asset, pk=pk)
    recall = get_object_or_404(Recall, pk=recall_id, asset=asset)
    recall.owner_status = request.POST.get("owner_status") or Recall.OwnerStatus.OPEN
    recall.notes = (request.POST.get("notes") or "").strip()
    if recall.owner_status == Recall.OwnerStatus.COMPLETED and not recall.completed_on:
        recall.completed_on = timezone.localdate()
    recall.save()
    messages.success(request, _("Recorded."))
    return redirect("asset_recalls", pk=asset.pk)


@login_required
def asset_report(request, pk):
    """The sale document (FR-REP-2, G-1)."""
    from django.http import HttpResponse

    from homeautoshop.core.reports import build_vehicle_report

    asset = get_object_or_404(Asset, pk=pk)
    pdf = build_vehicle_report(asset, include_costs=request.GET.get("costs") != "0")
    response = HttpResponse(pdf, content_type="application/pdf")
    slug = "".join(c if c.isalnum() else "-" for c in asset.nickname).strip("-").lower()
    disposition = "inline" if request.GET.get("inline") else "attachment"
    response["Content-Disposition"] = f'{disposition}; filename="{slug or "vehicle"}-history.pdf"'
    return response


# --------------------------------------------------------------------------
# Plate lookup (SPEC §8.2)
# --------------------------------------------------------------------------


@login_required
def plate_lookup(request):
    """Look a plate up, with the cost shown before it is spent.

    Two steps, always. The first shows the running monthly count and the
    operator's own cost estimate and asks; the second spends. A one-click
    lookup on a metered API is how a stray double-click becomes a line on a
    bill nobody can account for.
    """
    from . import plate as platelib

    require(request.user, "asset.edit")
    preflight = platelib.preflight()

    if request.method != "POST":
        return render(
            request,
            "assets/plate.html",
            {"preflight": preflight, "plate": "", "region": "", "result": None},
        )

    number = (request.POST.get("plate") or "").strip().upper()
    region = (request.POST.get("region") or "").strip().upper()

    if not request.POST.get("confirm"):
        # The confirmation step. Nothing has been spent yet and the page says so.
        return render(
            request,
            "assets/plate.html",
            {
                "preflight": preflight,
                "plate": number,
                "region": region,
                "confirming": True,
                "result": None,
            },
        )

    try:
        result = platelib.lookup(number, region, user=request.user)
    except platelib.LookupUnavailable as exc:
        messages.error(request, str(exc))
        return render(
            request,
            "assets/plate.html",
            {"preflight": platelib.preflight(), "plate": number, "region": region, "result": None},
        )

    if not result.usable:
        messages.warning(
            request,
            _("The provider had no VIN for %(plate)s. The call still counted.")
            % {"plate": number},
        )
    return render(
        request,
        "assets/plate.html",
        {
            "preflight": platelib.preflight(),
            "plate": number,
            "region": region,
            "result": result,
            # The provider's own year/make/model is a hint. The VIN goes
            # through the §8.1 decode path, which is the thing that is
            # authoritative about a vehicle.
            "vin_check": vinlib.validate(result.vin) if result.vin else None,
        },
    )


@require_POST
@login_required
def service_info_visibility(request, pk, provider_id):
    """Hide or restore a provider for one vehicle (SPEC OQ-11).

    Per vehicle, not globally: CHARM has no entry for a 2025 Crosstrek and
    never will, while it is the best source there is for a 2007 F-150. A
    provider that will never have a link is a box that will never be filled,
    and a permanent empty box on a page you use daily is worse than no box.

    A row is written even with no URL, because "hidden here" is a fact about
    this vehicle and this provider — which is what this table already exists to
    record, in the same way `subscription_status` records that ALLDATA is paid
    for on two of the four cars.
    """
    asset = get_object_or_404(Asset, pk=pk)
    require(request.user, "asset.edit", asset)
    provider = get_object_or_404(ServiceInfoProvider, pk=provider_id)

    hide = bool(request.POST.get("hide"))
    link, _created = AssetServiceInfoLink.objects.get_or_create(
        asset=asset, provider=provider, defaults={"url": ""}
    )
    link.is_hidden = hide
    link.save(update_fields=["is_hidden", "updated_at"])

    # A row that is neither pinned nor hidden carries nothing, so it goes.
    if not hide and not link.url:
        link.delete(hard=True)

    messages.success(
        request,
        _("%(name)s is hidden for %(vehicle)s.") % {"name": provider.name, "vehicle": asset.nickname}
        if hide
        else _("%(name)s is back.") % {"name": provider.name},
    )
    return redirect("asset_detail", pk=asset.pk)
