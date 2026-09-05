"""Asset views (SPEC §7.1, §9.3)."""

from __future__ import annotations

import json
import logging
import uuid
from urllib.parse import urlencode

from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Prefetch
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy
from django.utils.translation import ngettext
from django.views.decorators.http import require_GET, require_POST

from homeautoshop.accounts.policy import (
    is_helper,
    visible_assets,
    visible_assets_for,
)

from homeautoshop.accounts.models import require
from homeautoshop.core.measurements import distance_unit_for
from homeautoshop.mediafiles.models import Media, MediaLink
from homeautoshop.mediafiles.services import ingest
from homeautoshop.work.models import WorkOrder

from . import board
from . import cards as cardlib
from . import vin as vinlib
from . import vindecode
from . import vpic_fields
from .models import (
    Asset,
    AssetCardPreference,
    AssetLink,
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
from .services import decode_vin, mark_override, read_vin_locally, record_reading


# Which kind of asset each field belongs to. FR-EQP-1 is explicit: equipment
# shows *no* VIN, plate, title or registration field — not a disabled one, not
# one that fails on save, none. A field named here belongs to that kind alone;
# anything not named is shown for both.
FIELD_KINDS = {
    "vehicle_class": AssetKind.VEHICLE,
    "vin": AssetKind.VEHICLE,
    "plate": AssetKind.VEHICLE,
    "plate_region": AssetKind.VEHICLE,
    "plate_expires_on": AssetKind.VEHICLE,
    "make": AssetKind.VEHICLE,
    "model": AssetKind.VEHICLE,
    "trim": AssetKind.VEHICLE,
    "body_style": AssetKind.VEHICLE,
    "transmission": AssetKind.VEHICLE,
    "drivetrain": AssetKind.VEHICLE,
    "color_exterior": AssetKind.VEHICLE,
    "manufacturer": AssetKind.EQUIPMENT,
    "model_number": AssetKind.EQUIPMENT,
    "serial_number": AssetKind.EQUIPMENT,
}

# The form in the order it is read, as (kind, heading, fields). A section with
# a kind belongs to that kind whole; inside a shared section a field can still
# be gated on its own.
#
# **Each field appears exactly once.** Rendered twice it would submit twice,
# and a QueryDict keeps the last value — so the copy nobody could see would be
# the one that won. That is why `year`, `engine` and `fuel_type` sit in one
# shared section instead of being repeated under an equipment heading.
SECTIONS = (
    ("", gettext_lazy("The basics"), ("nickname", "asset_kind", "vehicle_class", "status")),
    (
        AssetKind.VEHICLE,
        gettext_lazy("Registration"),
        ("vin", "plate", "plate_region", "plate_expires_on"),
    ),
    (
        AssetKind.EQUIPMENT,
        gettext_lazy("Identification"),
        ("manufacturer", "model_number", "serial_number"),
    ),
    (
        "",
        gettext_lazy("What it is"),
        ("year", "make", "model", "trim", "body_style", "engine", "fuel_type",
         "transmission", "drivetrain", "color_exterior"),
    ),
    ("", gettext_lazy("Meter"), ("meter", "meter_unit")),
    ("", gettext_lazy("Ownership and notes"), ("acquired_on", "notes")),
)

# Fields worth the whole width rather than half of it.
FULL_WIDTH = ("nickname", "notes")


class AssetForm(forms.ModelForm):
    """The vehicle, plus how this person wants its card to look.

    The card fields are not columns on `Asset` — order, color and pins are per
    *user* (`AssetCardPreference`), because "which of these six is the one I
    mean" is a question two people in the same shop answer differently. They
    are on this form anyway, and not on a screen of their own, because the one
    moment somebody knows what a vehicle should be recognizable by is while
    they are typing in what it is.

    `card_prefs` is the marker that says the card section was on the screen. A
    checkbox group submits nothing at all when every box is cleared, which is
    indistinguishable from a post that never carried the section — and one of
    those means "pin nothing", while the other must leave the card alone.
    """

    card_prefs = forms.CharField(required=False, initial="1", widget=forms.HiddenInput)
    # Labeled rather than left to Django, which would derive "Card color" and
    # "Card pins" from the column names. US English in the `msgid`, because
    # that is the source language, and `locale/en_CA` is what renders the
    # Canadian spelling for a reader who has chosen it (locale/README.md).
    card_color = forms.ChoiceField(
        required=False, choices=cardlib.COLORS, label=gettext_lazy("Color")
    )
    card_pins = forms.MultipleChoiceField(
        required=False,
        choices=[(pin.key, pin.label) for pin in cardlib.PINS],
        label=gettext_lazy("What this card shows"),
    )

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

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = user
        self._load_card_preference()
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

    # -- the card ---------------------------------------------------------

    def _load_card_preference(self) -> None:
        """Fill the card fields in from this person's row, if there is one.

        No row is the ordinary state and is not a gap to be filled: it means
        the card has never been touched, so it takes the defaults, and the
        boxes open showing what the card is actually displaying today.
        """
        self.card_preference = None
        if not (self.user and self.instance.pk):
            self.initial.setdefault("card_pins", list(cardlib.DEFAULT_PINS))
            return
        self.card_preference = AssetCardPreference.objects.filter(
            user=self.user, asset=self.instance
        ).first()
        stored = self.card_preference.pins if self.card_preference else None
        self.initial.setdefault(
            "card_pins", list(cardlib.DEFAULT_PINS) if stored is None else list(stored)
        )
        self.initial.setdefault(
            "card_color", self.card_preference.color if self.card_preference else ""
        )

    def color_options(self):
        chosen = self["card_color"].value() or ""
        return [
            {"value": key, "label": label, "checked": key == chosen}
            for key, label in cardlib.COLORS
        ]

    def pin_options(self):
        """Every pin, with the ones this kind has no use for marked hidden.

        Rendered and hidden rather than omitted, exactly as `sections()` does
        it, so switching between vehicle and equipment is instant and the
        server still decides the state the page opens in.
        """
        value = self["card_pins"].value() or ()
        # A bare string here is one pin, not a set of its letters — which is
        # what `set("photo")` quietly produces.
        chosen = {value} if isinstance(value, str) else set(value)
        kind = self.current_kind()
        return [
            {
                "value": pin.key,
                "label": pin.label,
                "kind": pin.kind,
                "checked": pin.key in chosen,
                "hidden": bool(pin.kind) and pin.kind != kind,
            }
            for pin in cardlib.PINS
        ]

    def clean_card_pins(self):
        return cardlib.valid_pins(self.cleaned_data.get("card_pins"), kind=self.current_kind())

    def save_card_preference(self, asset) -> None:
        """Write this person's card settings, or clear the row if there is nothing to keep."""
        if not (self.user and self.cleaned_data.get("card_prefs")):
            return
        color = self.cleaned_data.get("card_color") or ""
        pins = list(self.cleaned_data.get("card_pins") or [])
        pref = AssetCardPreference.objects.filter(user=self.user, asset=asset).first()
        # A card back at its defaults keeps no row *unless* it has a position:
        # the board order lives in the same row, and dropping it would move the
        # card somebody had placed. Absent means default, which is the state
        # every card starts in.
        default = not color and pins == list(cardlib.DEFAULT_PINS)
        if pref is None:
            if default:
                return
            AssetCardPreference.objects.create(user=self.user, asset=asset, color=color, pins=pins)
            return
        if default and pref.board_order is None:
            pref.delete()
            return
        pref.color = color
        pref.pins = pins
        pref.save(update_fields=["color", "pins", "updated_at"])

    # -- what this kind actually has ------------------------------------

    def current_kind(self) -> str:
        """The kind being filled in — submitted, being edited, or the default.

        `BoundField.value()` because it is the one accessor that answers for a
        bound form, an edit and a blank one alike.
        """
        return self["asset_kind"].value() or AssetKind.VEHICLE

    def sections(self):
        """The form grouped for display, each group knowing whose it is.

        Everything is rendered and what the chosen kind has no use for is
        marked hidden, so changing the kind is instant and costs no round trip.
        The server still decides the initial state, which is what makes the
        screen correct with JavaScript switched off.
        """
        kind = self.current_kind()
        groups = []
        for section_kind, title, names in SECTIONS:
            fields = []
            for name in names:
                if name not in self.fields:
                    continue
                # A field inside a section that is already gated does not carry
                # a gate of its own; one attribute deciding one thing.
                belongs_to = "" if section_kind else FIELD_KINDS.get(name, "")
                fields.append({
                    "field": self[name],
                    "kind": belongs_to,
                    "hidden": bool(belongs_to) and belongs_to != kind,
                    "full": name in FULL_WIDTH,
                })
            groups.append({
                "title": title,
                "kind": section_kind,
                "hidden": bool(section_kind) and section_kind != kind,
                "fields": fields,
            })
        return groups

    # -- validation ------------------------------------------------------

    def clean(self):
        cleaned = super().clean()
        kind = cleaned.get("asset_kind") or AssetKind.VEHICLE
        # What this kind does not have is cleared rather than validated.
        # Turning a vehicle into equipment would otherwise fail on `vin`, and
        # that error points at a field the form is no longer showing — nobody
        # can act on it. `Asset.save()` already does exactly this to
        # `vehicle_class`; this is the same rule applied where it is visible.
        for name, belongs_to in FIELD_KINDS.items():
            if name in self.fields and belongs_to != kind:
                cleaned[name] = None if Asset._meta.get_field(name).null else ""
        return cleaned

    def clean_vin(self):
        if self.current_kind() != AssetKind.VEHICLE:
            # The box is not on the screen for equipment, so an error raised
            # against it would be one nobody could see, let alone fix.
            return ""
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
            kind = asset.asset_kind
            for name in ("year", "make", "model", "trim", "engine", "body_style"):
                # A field cleared because the kind changed is not a correction
                # anybody made, and recording it as one would pin the blank
                # over every future decode.
                if name in self.changed_data and FIELD_KINDS.get(name, kind) == kind:
                    mark_override(asset, name, self.cleaned_data.get(name))
        if commit:
            asset.save()
            self.save_card_preference(asset)
        return asset


class ReadingForm(forms.Form):
    value = forms.DecimalField(max_digits=12, decimal_places=2, min_value=0)
    read_on = forms.DateField(required=False, widget=forms.DateInput(attrs={"type": "date"}))
    note = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 2}))


@login_required
def asset_list(request):
    kind = request.GET.get("kind", "")
    show_all = request.GET.get("all") == "1"
    qs = visible_assets(request.user, Asset.objects.select_related("primary_photo"))
    if kind:
        qs = qs.filter(asset_kind=kind)
    if not show_all:
        qs = qs.exclude(status__in=["sold", "parted_out", "totaled"])
    return render(
        request,
        "assets/list.html",
        {
            # Cards rather than assets: what each one says is per-vehicle and
            # per-person, and resolving it in the template would mean a query
            # per pin per card. See `board.cards_for`.
            "cards": board.cards_for(request.user, qs),
            "kind": kind,
            "show_all": show_all,
            "kinds": AssetKind.choices,
            # Carried on every reorder form so the redirect lands back on the
            # tab the person was on rather than dropping them to "All".
            "board_scope": board.SCOPE_VEHICLES,
        },
    )


@require_POST
@login_required
def asset_move(request, pk):
    """Move one card up or down its board — the path that needs no script.

    Dragging is the asked-for gesture and `static/board.js` provides it, but it
    cannot be the only one: it is unreachable from a keyboard, it is unreliable
    on a phone held in one oily hand, and it does not exist at all until a
    script has loaded. These two buttons work everywhere and are what the drag
    is an enhancement *of* — the same POST, from the same list, with the
    neighbor worked out on the server either way.
    """
    asset = get_object_or_404(Asset, pk=pk)
    require(request.user, "asset.read", asset)
    scope = request.POST.get("scope") or board.SCOPE_VEHICLES
    everything, visible = board.scope_for(
        request.user,
        scope,
        kind=request.POST.get("kind", ""),
        show_all=request.POST.get("all") == "1",
    )
    board.move(request.user, everything, visible, asset, request.POST.get("direction", "down"))
    return redirect(_board_return(request, scope))


@require_POST
@login_required
def asset_reorder(request):
    """Store a whole sequence at once — what a finished drag posts.

    Deliberately not a bespoke JSON endpoint. It is an ordinary form post with
    repeated `ids`, so the enhanced path and a hand-written form say the same
    thing, and a failure here is a redirect to a page that is still correct
    rather than a silent 500 behind a `fetch`.
    """
    scope = request.POST.get("scope") or board.SCOPE_VEHICLES
    everything, visible = board.scope_for(
        request.user,
        scope,
        kind=request.POST.get("kind", ""),
        show_all=request.POST.get("all") == "1",
    )
    ids = []
    for raw in request.POST.getlist("ids"):
        try:
            ids.append(uuid.UUID(raw))
        except (ValueError, AttributeError):
            # A malformed id is one card the request cannot be about. Dropping
            # it leaves that card where it was, which beats refusing the whole
            # rearrangement over one bad value.
            continue
    board.reorder(request.user, everything, visible, ids)
    return redirect(_board_return(request, scope))


def _board_return(request, scope: str) -> str:
    """Back to the screen the rearrangement came from, filter and all."""
    if scope == board.SCOPE_FLEET:
        return reverse("dashboard")
    query = {}
    if request.POST.get("kind"):
        query["kind"] = request.POST["kind"]
    if request.POST.get("all") == "1":
        query["all"] = "1"
    url = reverse("asset_list")
    return f"{url}?{urlencode(query)}" if query else url


@login_required
def asset_detail(request, pk):
    asset = get_object_or_404(
        Asset.objects.prefetch_related(
            Prefetch("ownerships", queryset=AssetOwnership.objects.select_related("person")),
            Prefetch("service_info_links", queryset=AssetServiceInfoLink.objects.select_related("provider")),
        ),
        pk=pk,
    )
    require(request.user, "asset.read", asset)
    readings = asset.usage_readings.all()[:20]
    work_orders = asset.work_orders.select_related("asset")[:25]
    # Split by what the file *is*, not by which form uploaded it. Every
    # attachment used to land in the Photos grid, so a PDF of the title
    # appeared there as a blank thumbnail with no way to read it — the file
    # decides which section it belongs to (FR-DOC-10).
    attachments = list(MediaLink.for_entity(asset))
    photos = [a for a in attachments if a.media.kind == Media.Kind.PHOTO]
    documents = [a for a in attachments if a.media.kind != Media.Kind.PHOTO]
    providers = ServiceInfoProvider.objects.filter(is_enabled=True)
    links = {link.provider_id: link for link in asset.service_info_links.all()}
    # Hidden providers are listed separately rather than dropped: a provider
    # that vanished with no way back would be a setting nobody can undo.
    shown = [(p, links.get(p.pk)) for p in providers if not getattr(links.get(p.pk), "is_hidden", False)]
    hidden = [p for p in providers if getattr(links.get(p.pk), "is_hidden", False)]

    story = _group_media(_timeline(asset))
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
            "documents": documents,
            "asset_links": asset.links.all(),
            "reading_form": ReadingForm(),
            "providers": shown,
            "hidden_providers": hidden,
            "vin_check": vinlib.validate(asset.vin, year=asset.year) if asset.vin else None,
            # What the VIN says, read against the manufacturer's own tables —
            # the only route to this for a vehicle vPIC will not decode.
            # Named apart from `readings` above, which is the odometer.
            "vin_readings": vindecode.decode(asset.vin, year=asset.year),
            "vin_year": asset.year,
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


def _media_title(link) -> str:
    """What to call one attachment in the story.

    A photograph is worth calling "Photo": its file name is `IMG_4032.jpg` and
    says nothing. A document's file name is the only thing that distinguishes
    it from the three beside it, so that is what gets shown — until somebody
    gives it a name of their own, which always wins.
    """
    if link.caption:
        return link.caption
    # `kind`, the same test the Photos and Documents cards split on, so a row
    # in the story and the card it opens never disagree about what it is.
    if link.media.kind != Media.Kind.PHOTO and link.media.original_filename:
        return link.media.original_filename
    return link.media.get_kind_display()


def _group_label(kind: str, n: int) -> str:
    """The plural for a day's worth of one kind of attachment."""
    if kind == Media.Kind.DOCUMENT:
        return ngettext("%(n)s document", "%(n)s documents", n) % {"n": n}
    if kind == Media.Kind.SCAN_EXPORT:
        return ngettext("%(n)s scan report", "%(n)s scan reports", n) % {"n": n}
    if kind == Media.Kind.AUDIO_NOTE:
        return ngettext("%(n)s audio note", "%(n)s audio notes", n) % {"n": n}
    return ngettext("%(n)s photo", "%(n)s photos", n) % {"n": n}


def _group_media(events: list[dict]) -> list[dict]:
    """Collapse a day's attachments into a single entry, one group per kind.

    A photo is one row and a work order is one row, and they are not the same
    size of event. Five shots of the same caliper pushed the meter reading, the
    job they belong to and the whole identity panel below the fold — the vehicle
    page became a photo roll with a service history somewhere underneath it.

    By day rather than by run, because "the thirty-first, four photos" is how
    somebody remembers taking them; and a day with one photograph stays one
    photograph, because a group of one is a worse label than the thing itself.

    **By kind as well as by day**, because the group carries a noun. Four PDFs
    uploaded together were counted as "4 photos" — a row that named the wrong
    thing and, opened, led to files the Photos card had never held.
    """
    attachments: dict = {}
    merged = [event for event in events if event["kind"] != "media"]

    for event in events:
        if event["kind"] != "media":
            continue
        when = event["when"]
        day = (timezone.localtime(when) if timezone.is_aware(when) else when).date()
        attachments.setdefault((day, event.get("media_kind", "")), []).append(event)

    for (_day, kind), group in attachments.items():
        if len(group) == 1:
            merged.append(group[0])
            continue
        merged.append(
            {
                # The newest moment in it, so the group sorts where its most
                # recent photograph would have.
                "when": max(event["when"] for event in group),
                "kind": "media_group",
                "media_kind": kind,
                "title": _group_label(kind, len(group)),
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
    # A lab sample is a dated observation about this vehicle, and the timeline
    # is where the vehicle's story is read. Left off it, a sample was a thing
    # you had to already know to go and look for — the same defect as the
    # orphan part usage above.
    for sample in asset.fluid_samples.all()[:30]:
        events.append(
            {
                "when": timezone.make_aware(
                    timezone.datetime.combine(
                        sample.sampled_on, timezone.datetime.min.time()
                    )
                ),
                "kind": "fluid",
                "title": _("Fluid sample"),
                "detail": sample.where
                + (f" — {sample.lab}" if sample.lab else ""),
                "url": f"/fluids/{sample.pk}/",
            }
        )
    for link in MediaLink.for_entity(asset).select_related("media")[:30]:
        events.append(
            {
                "when": link.media.captured_at or link.created_at,
                "kind": "media",
                # A document is not a photograph. Every attachment used to be
                # titled "Photo", so four PDFs of a mower's manual read as
                # four photographs on a page whose Photos card was empty.
                "title": _media_title(link),
                "media_kind": link.media.kind,
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

    # `.dict()` above keeps one value per key, which is right for every field
    # on this form but the checkbox group: it would arrive as the single last
    # box that happened to be ticked, and the rest would come back cleared.
    data["card_pins"] = request.POST.getlist("card_pins")
    return AssetForm(initial=data, user=request.user)


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
        form = AssetForm(request.POST, user=request.user)
        if form.is_valid():
            asset = form.save()
            _apply_stashed_decode(request, asset)
            messages.success(request, _("Added %(name)s.") % {"name": asset.nickname})
            return redirect("asset_detail", pk=asset.pk)
    else:
        initial = {"meter_unit": distance_unit_for(request.user.units or "imperial")}
        # Arriving from the Equipment tab opens the equipment form. Otherwise
        # the one screen that knows which kind you meant throws it away, and
        # the first thing you do on a fresh form is change it back.
        kind = request.GET.get("kind", "")
        if kind in AssetKind.values:
            initial["asset_kind"] = kind
        if kind == AssetKind.EQUIPMENT:
            initial["meter"] = "engine_hours"
            initial["meter_unit"] = "hours"
        form = AssetForm(initial=initial, user=request.user)
    return render(request, "assets/form.html", {"form": form, "asset": None})


@login_required
def asset_edit(request, pk):
    asset = get_object_or_404(Asset, pk=pk)
    require(request.user, "asset.edit", asset)
    if request.method == "POST":
        form = AssetForm(request.POST, instance=asset, user=request.user)
        if form.is_valid():
            form.save()
            messages.success(request, _("Saved."))
            return redirect("asset_detail", pk=asset.pk)
    else:
        form = AssetForm(instance=asset, user=request.user)
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
    """Live, offline VIN validation for the form (FR-VEH-2).

    The model year comes along because it is what decides whether a short VIN
    is a pre-1981 format or a typo. Without it this panel would say "read as a
    pre-1981 VIN" about a half-typed one on a 2016 car and then the save would
    refuse it — feedback that disagrees with the form it is attached to.
    """
    try:
        year = int(request.GET.get("year") or 0) or None
    except ValueError:
        year = None
    check = vinlib.validate(request.GET.get("vin", ""), year=year)
    return render(
        request,
        "assets/_vin_feedback.html",
        {
            "check": check,
            # Only for the era the sheets cover; a 17-character VIN is vPIC's
            # job and asking both would print two different answers.
            "vin_readings": (
                vindecode.decode(check.vin, year=year) if check.is_pre_1981 else []
            ),
            "vin_year": year,
        },
    )


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
def vin_read(request, pk):
    """Fill in what a pre-1981 VIN says, from the local tables (FR-VEH-12).

    A separate action from `vin_decode` rather than a fallback inside it. They
    answer from different places — one asks NHTSA, one reads a table in this
    repository — and a button that silently changed which would make the
    provenance on the resulting fields a coincidence of what was reachable.
    """
    asset = get_object_or_404(Asset, pk=pk)
    require(request.user, "asset.edit", asset)

    result = read_vin_locally(asset, user=request.user)
    if not result.ok:
        messages.warning(request, result.message)
    else:
        messages.success(request, result.summary)
        if result.skipped_overridden:
            messages.info(
                request,
                _("Left your edits alone: %(fields)s.")
                % {"fields": ", ".join(result.skipped_overridden)},
            )
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
    # §12.2a — this was the one attachment route with no resource check on it,
    # so a helper granted read on one vehicle could put photographs on any of
    # them. `document_upload`, three lines of code away, always had it.
    require(request.user, "asset.edit", asset)
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
    else:
        # Silence here read as a page that did nothing for no reason. The two
        # controls are a sequence and this is what says so when only the
        # second one was pressed.
        messages.warning(request, _("Choose a photo first, then Upload."))
    return redirect("asset_detail", pk=asset.pk)


@require_POST
@login_required
def link_add(request, pk):
    """Keep a page that is about this vehicle (FR-VEH-13).

    Only `http` and `https` are accepted. A URL field is rendered straight
    into an `href`, and `javascript:` in an href is script execution — so the
    scheme is checked here rather than trusted from a form that a determined
    POST never goes through.
    """
    asset = get_object_or_404(Asset, pk=pk)
    require(request.user, "asset.edit", asset)
    url = (request.POST.get("url") or "").strip()

    if not url.lower().startswith(("http://", "https://")):
        messages.error(request, _("Paste a full web address starting with http."))
        return redirect("asset_detail", pk=asset.pk)

    AssetLink.objects.create(
        asset=asset,
        url=url[:500],
        label=(request.POST.get("label") or "").strip()[:120],
        notes=(request.POST.get("notes") or "").strip()[:300],
    )
    messages.success(request, _("Saved the link."))
    return redirect("asset_detail", pk=asset.pk)


@require_POST
@login_required
def link_delete(request, pk, link_id):
    asset = get_object_or_404(Asset, pk=pk)
    require(request.user, "asset.edit", asset)
    link = get_object_or_404(AssetLink, pk=link_id, asset=asset)
    link.delete()
    messages.success(request, _("Removed the link."))
    return redirect("asset_detail", pk=asset.pk)


@require_POST
@login_required
def document_upload(request, pk):
    """Attach a manual, a title, a receipt (FR-DOC-1).

    The same `ingest` the photo form uses: which kind a file becomes is read
    off the file, so a photo uploaded here still lands under Photos and a PDF
    dropped on the photo form still lands here. The two forms exist for where
    somebody looks, not to classify anything.
    """
    asset = get_object_or_404(Asset, pk=pk)
    require(request.user, "asset.edit", asset)
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
            _("Added %(n)d document(s).") % {"n": created}
            if created
            else _("Those documents were already on file."),
        )
    else:
        messages.warning(request, _("Choose a file first, then Upload."))
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
        fields = [
            "group", "name", "value", "value_max", "unit", "condition",
            "source", "is_sensitive", "is_pinned",
        ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name, field in self.fields.items():
            field.required = name in ("group", "name", "value")
            if name == "value":
                # Reads as the pair it is once there is a second box beside it.
                field.label = _("Value, or the low end")
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
    require(request.user, "asset.read", asset)
    return render(
        request,
        "assets/timeline.html",
        {"asset": asset, "timeline": _timeline(asset)},
    )


@login_required
def asset_specs(request, pk):
    """The lookup that interrupts a job most often (FR-SPEC-1..4)."""
    asset = get_object_or_404(Asset, pk=pk)
    require(request.user, "asset.read", asset)
    specs = asset.specs.select_related("source_media")
    # A helper is somebody let into the garage, not into the glovebox. The
    # key code, the radio code and where the wheel-lock key lives are exactly
    # what `is_sensitive` already marks and exactly what a vehicle report
    # already withholds (C-5) — so the same line is held here rather than a
    # new one invented for it.
    if is_helper(request.user):
        specs = specs.filter(is_sensitive=False)
    grouped: dict = {}
    for spec in specs:
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
            "others": visible_assets(request.user).exclude(pk=asset.pk),
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
    # §12.2a. `spec_edit` immediately above has always had this; this one was
    # the odd route out, and the two sit four lines apart.
    require(request.user, "asset.edit", asset)
    get_object_or_404(AssetSpec, pk=spec_id, asset=asset).delete()
    return redirect("asset_specs", pk=asset.pk)


@require_POST
@login_required
def spec_pin(request, pk, spec_id):
    """Put a spec on the work-order quick-reference panel, or take it off.

    This was reachable only through the edit form: open a form, find one
    checkbox among six fields, save. The row already *shows* the state as a
    pill, so the one thing you could not do was change it where you were
    reading it — and which specs are worth having in front of you mid-job is
    exactly the sort of thing you change often and by eye.

    The wanted state is posted rather than toggled. A toggle acts on what the
    server holds now; a stale page then does the opposite of what its button
    said, which for a two-state control is every bit as wrong as failing.
    """
    asset = get_object_or_404(Asset, pk=pk)
    require(request.user, "asset.edit", asset)
    spec = get_object_or_404(AssetSpec, pk=spec_id, asset=asset)

    spec.is_pinned = request.POST.get("pinned") == "1"
    spec.save(update_fields=["is_pinned"])

    if not spec.is_pinned:
        messages.success(request, _("%(name)s is no longer pinned.") % {"name": spec.name})
    elif spec.is_sensitive:
        # A sensitive spec is kept off work orders and reports by design, so
        # pinning one changes nothing there. Said here, rather than left to be
        # discovered on a work order the spec never appears on.
        messages.warning(
            request,
            _("%(name)s is pinned, but it is marked sensitive — those are kept off work orders.")
            % {"name": spec.name},
        )
    else:
        messages.success(
            request,
            _("%(name)s will show on this vehicle's work orders.") % {"name": spec.name},
        )
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
    require(request.user, "asset.read", asset)
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
    """What the sale document will say, before it is produced (FR-REP-2, G-1).

    The button used to start a download. That is the wrong shape for this
    particular document: it is the thing you hand to a buyer, it is the one
    place sensitive specs are deliberately withheld, and how complete it looks
    is the whole question — all of which somebody wants to check *before* a
    file lands in their downloads folder, not after opening it from there.

    Rendered from `report_sections`, the same description the PDF is drawn
    from, so the preview cannot promise a section the document omits.
    """
    from homeautoshop.core.reports import report_footer, report_sections

    asset = get_object_or_404(Asset, pk=pk)
    require(request.user, "asset.read", asset)
    include_costs = request.GET.get("costs") != "0"
    sections = report_sections(asset, include_costs=include_costs)
    return render(
        request,
        "assets/report.html",
        {
            "asset": asset,
            "sections": sections,
            "footer": report_footer(),
            "include_costs": include_costs,
            # Counted rather than trusted: a report that would be mostly empty
            # is worth knowing about while there is still time to fill it in.
            "filled": sum(1 for section in sections if not section.is_empty),
            "hidden_specs": asset.specs.filter(is_sensitive=True).count(),
        },
    )


@login_required
def asset_report_csv(request, pk):
    """The same report as rows (FR-REP-2, FR-REP-4).

    FR-REP-2 says this document is exportable as PDF *and CSV*, and only the
    PDF existed. Built from `report_sections` like the other two, so the three
    outputs cannot disagree about what the report contains — which was the
    reason for pulling the content out of the renderer in the first place.

    Each section is written as its own block with a blank line between, rather
    than forced into one flat table. A vehicle report is six differently
    shaped tables, and flattening them would produce a file with a header row
    that lies about most of its contents.
    """
    import csv

    from django.http import HttpResponse

    from homeautoshop.core.reports import report_footer, report_sections

    asset = get_object_or_404(Asset, pk=pk)
    require(request.user, "asset.read", asset)
    include_costs = request.GET.get("costs") != "0"

    response = HttpResponse(content_type="text/csv")
    slug = "".join(c if c.isalnum() else "-" for c in asset.nickname).strip("-").lower()
    response["Content-Disposition"] = (
        f'attachment; filename="{slug or "vehicle"}-history.csv"'
    )
    writer = csv.writer(response)
    writer.writerow([asset.nickname, asset.descriptor])

    for section in report_sections(asset, include_costs=include_costs):
        if section.is_empty and not section.note:
            continue
        writer.writerow([])
        writer.writerow([section.title])
        if section.is_empty:
            writer.writerow([section.note])
            continue
        writer.writerow(section.columns)
        for row in section.rows:
            writer.writerow(row)
        if section.note:
            writer.writerow([section.note])

    writer.writerow([])
    writer.writerow([report_footer()])
    return response


@login_required
def asset_report_pdf(request, pk):
    """The file itself, once somebody has seen what is in it."""
    from django.http import HttpResponse

    from homeautoshop.core.reports import build_vehicle_report

    asset = get_object_or_404(Asset, pk=pk)
    require(request.user, "asset.read", asset)
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
