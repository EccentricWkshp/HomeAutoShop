"""Parts and inventory views (SPEC §7.4, §9.3)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from uuid import UUID

from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db.models import Count, Prefetch, Q, Sum
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template.defaultfilters import floatformat
from django.urls import reverse
from django.utils.translation import gettext as _, ngettext
from django.views.decorators.http import require_POST

from homeautoshop.accounts.policy import visible_assets, visible_assets_for

from homeautoshop.assets.models import Asset
from homeautoshop.core.costs import inventory_value
from homeautoshop.core.moneyform import MoneyFormMixin, parse_amount
from homeautoshop.purchasing.models import PurchaseLine

from .models import (
    Category, Location, Part, PartCrossRef, PartFitment, PartKitItem,
    PartUsage, StockLot, StockTransaction,
)
from .services import (
    KINDS, candidates, categories, categories_for, close_kit, consume,
    core_value_owed, cycle_count, expiring_lots, find, kit_weights, matching,
    open_kit, outstanding_cores, restock_list, resolve_part, returned_cores,
    split_kit_cost,
)


class PartForm(MoneyFormMixin, forms.ModelForm):
    """Everything about a part, including the several categories it is in.

    Two controls for one concept, and both work with no script at all, which
    is why it is not a token box. **Checkboxes** for what the shop already
    files under — that is the reuse half, and on a phone a wrapped row of
    boxes is easier than a multi-select, which is the control this would
    otherwise have been. **A text box** for anything new, comma-separated,
    because a fixed list cannot express the forty-first category and refusing
    to let somebody invent one is how the field stops being used.
    """

    #: Free text beside the boxes. `Electrical/Lighting` written in here comes
    #: out as two, for the same reason `categories_for` splits on a slash.
    new_categories = forms.CharField(
        required=False,
        label=_("Add a category"),
        help_text=_("Anything not listed above. Separate several with commas."),
    )

    class Meta:
        model = Part
        fields = [
            "name", "categories", "new_categories", "manufacturer",
            "part_number", "part_type", "unit", "typical_cost_minor",
            "is_consumable", "has_core", "core_value_minor", "min_quantity",
            "notes",
        ]
        widgets = {
            "notes": forms.Textarea(attrs={"rows": 2}),
            "categories": forms.CheckboxSelectMultiple,
        }
        labels = {
            "core_value_minor": _("Core charge"),
            "typical_cost_minor": _("Usual price"),
            "is_consumable": _("Consumable"),
        }
        help_texts = {
            "typical_cost_minor": _(
                "What one costs. Optional, and used to divide a kit's price "
                "across what is inside it."
            ),
            "categories": _(
                "How the parts list groups this. A headlight is electrical "
                "and lighting — tick both."
            ),
            "is_consumable": _(
                "Gets used up rather than installed — oil, cleaner, rags. "
                "Consumables are offered in every part picker and are never "
                "ranked as something planned for one vehicle."
            ),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Only categories with something in them. An empty one is a filter
        # that finds nothing, and the box beside this is how a new one starts.
        self.fields["categories"].queryset = Category.objects.filter(
            parts__isnull=False
        ).distinct()
        for name, field in self.fields.items():
            field.required = name == "name"
            if isinstance(field.widget, (forms.CheckboxInput, forms.CheckboxSelectMultiple)):
                continue
            css = "select" if isinstance(field.widget, forms.Select) else "input"
            if isinstance(field.widget, forms.Textarea):
                css = "input textarea"
            field.widget.attrs.setdefault("class", css)

    def save(self, commit=True):
        """The typed-in categories join the ticked ones.

        `ModelForm` writes an m2m in `save_m2m`, which sets the relation to
        exactly what the field cleaned — so anything added here has to be
        applied after that, or saving would drop it.
        """
        part = super().save(commit=commit)
        if commit:
            self._file_it(part)
        else:
            original = self.save_m2m

            def save_m2m():
                original()
                self._file_it(part)

            self.save_m2m = save_m2m
        return part

    def _file_it(self, part) -> None:
        added = categories_for(self.cleaned_data.get("new_categories", ""))
        if added:
            part.categories.add(*added)


class LotForm(MoneyFormMixin, forms.ModelForm):
    quantity = forms.DecimalField(max_digits=12, decimal_places=3, min_value=0)

    class Meta:
        model = StockLot
        fields = ["location", "unit_cost_minor", "acquired_on", "expires_on"]
        widgets = {
            "acquired_on": forms.DateInput(attrs={"type": "date"}),
            "expires_on": forms.DateInput(attrs={"type": "date"}),
        }

    def __init__(self, *args, part=None, **kwargs):
        super().__init__(*args, **kwargs)
        for name, field in self.fields.items():
            field.required = name == "quantity"
            css = "select" if isinstance(field.widget, forms.Select) else "input"
            field.widget.attrs.setdefault("class", css)
        # Gaskets arrive in whole ones; coolant does not. Validation is
        # unchanged — three decimal places either way — this is the spinner.
        self.fields["quantity"].widget.attrs["step"] = (
            part.qty_step if part is not None else "0.001"
        )


class LotEditForm(MoneyFormMixin, forms.ModelForm):
    """Correcting a lot that was recorded with something missing.

    Everything a lot knows *except* how many there are. Quantity is a projection
    of the ledger (FR-INV-1) and stays one — a box here that overwrote it would
    be the exact silent correction the ledger exists to prevent, so counting is
    the cycle-count form and this is everything else.
    """

    class Meta:
        model = StockLot
        fields = ["location", "unit_cost_minor", "acquired_on", "expires_on"]
        widgets = {
            "acquired_on": forms.DateInput(attrs={"type": "date"}),
            "expires_on": forms.DateInput(attrs={"type": "date"}),
        }
        labels = {"unit_cost_minor": _("Unit cost")}
        help_texts = {
            "unit_cost_minor": _(
                "What one of these cost. Leaving it blank makes everything drawn "
                "from this lot cost nothing."
            ),
            "acquired_on": _("Consumption draws the oldest lot first, by this date."),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name, field in self.fields.items():
            field.required = False
            css = "select" if isinstance(field.widget, forms.Select) else "input"
            field.widget.attrs.setdefault("class", css)


class FitmentForm(forms.ModelForm):
    """What a part fits, and how much the claim is worth.

    A fitment names a vehicle — either one of yours or a description of a class
    of them — and never a part beyond the one it hangs off. `part` is therefore
    not a field here: it comes from the URL, and offering it would let somebody
    file a fitment against the part they are not looking at.
    """

    class Meta:
        model = PartFitment
        fields = [
            "asset", "make", "model", "year_from", "year_to",
            "engine_code", "position", "confidence", "notes",
        ]
        widgets = {"notes": forms.Textarea(attrs={"rows": 2})}
        labels = {
            "asset": _("One of your vehicles"),
            "make": _("or make"),
            "year_from": _("Years from"),
            "year_to": _("to"),
            "engine_code": _("Engine"),
        }
        help_texts = {
            "asset": _("Pick a vehicle, or leave blank and describe one below."),
            "position": _("Front, rear, left — when the part is side-specific."),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["asset"].required = False
        self.fields["asset"].empty_label = _("— not one of my vehicles —")
        for name, field in self.fields.items():
            field.required = name == "confidence"
            css = "select" if isinstance(field.widget, forms.Select) else "input"
            if isinstance(field.widget, forms.Textarea):
                css = "input textarea"
            field.widget.attrs.setdefault("class", css)

    def clean(self):
        data = super().clean()
        # A fitment that names no vehicle fits everything, which is the one
        # thing it must never be taken to mean.
        if not data.get("asset") and not (data.get("make") or data.get("model")):
            raise ValidationError(
                _("Say which vehicle: pick one of yours, or give at least a make or model.")
            )
        first, last = data.get("year_from"), data.get("year_to")
        if first and last and first > last:
            raise ValidationError(_("The first year is after the last one."))
        return data


def _fitment_page(request, part, fitment, form):
    return render(
        request,
        "parts/fitment_form.html",
        {"part": part, "fitment": fitment, "form": form},
    )


@login_required
def fitment_add(request, pk):
    part = get_object_or_404(Part, pk=pk)
    form = FitmentForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        fitment = form.save(commit=False)
        fitment.part = part
        fitment.save()
        messages.success(
            request, _("Recorded: %(what)s.") % {"what": fitment.vehicle}
        )
        return redirect("part_detail", pk=part.pk)
    return _fitment_page(request, part, None, form)


@login_required
def fitment_edit(request, pk, fitment_id):
    part = get_object_or_404(Part, pk=pk)
    fitment = get_object_or_404(PartFitment, pk=fitment_id, part=part)
    form = FitmentForm(request.POST or None, instance=fitment)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, _("Saved."))
        return redirect("part_detail", pk=part.pk)
    return _fitment_page(request, part, fitment, form)


@require_POST
@login_required
def fitment_delete(request, pk, fitment_id):
    """Remove a fitment outright.

    Worth knowing before reaching for it: if the claim came from a vendor's
    order and turned out to be wrong, editing it to **Does not fit** is the
    better answer. A deleted one says nothing, and nothing is what the vendor's
    claim will overwrite the next time somebody looks the part up. A recorded
    failure is the shop's own knowledge and it stays.
    """
    part = get_object_or_404(Part, pk=pk)
    fitment = get_object_or_404(PartFitment, pk=fitment_id, part=part)
    vehicle = fitment.vehicle
    fitment.delete()
    messages.success(request, _("Removed %(what)s.") % {"what": vehicle})
    return redirect("part_detail", pk=part.pk)


@require_POST
@login_required
def kit_item_add(request, pk):
    """Record a part as being inside this kit (FR-INV-9)."""
    kit = get_object_or_404(Part, pk=pk)
    inside, problem = resolve_part(request.POST)
    if inside is None:
        messages.error(request, problem)
        return redirect("part_detail", pk=kit.pk)
    item = PartKitItem(
        kit=kit,
        part=inside,
        quantity=request.POST.get("quantity") or 1,
    )
    typed = (request.POST.get("value") or "").strip()
    try:
        # Blank is not zero here: it means "whatever the part costs", which is
        # the answer most of the time and the reason the box may be left alone.
        if typed:
            item.value_minor = parse_amount(typed, item.value_currency or "USD")
        item.full_clean(exclude=["created_by"])
    except ValidationError as exc:
        detail = getattr(exc, "message_dict", None)
        messages.error(
            request,
            " ".join(m for msgs in detail.values() for m in msgs)
            if detail
            else " ".join(exc.messages),
        )
    else:
        item.save()
        messages.success(request, _("Added %(part)s to this kit.") % {"part": item.part})
    return redirect("part_detail", pk=kit.pk)


@require_POST
@login_required
def kit_item_remove(request, pk, item_id):
    kit = get_object_or_404(Part, pk=pk)
    item = get_object_or_404(PartKitItem, pk=item_id, kit=kit)
    part = item.part
    item.delete()
    messages.success(request, _("Removed %(part)s from this kit.") % {"part": part})
    return redirect("part_detail", pk=kit.pk)


@require_POST
@login_required
def lot_open_kit(request, pk, lot_id):
    """Open a boxed kit into the parts inside it."""
    part = get_object_or_404(Part, pk=pk)
    lot = get_object_or_404(StockLot, pk=lot_id, part=part)
    try:
        released = open_kit(lot, request.POST.get("quantity") or 1, user=request.user)
    except ValidationError as exc:
        messages.error(request, " ".join(exc.messages))
    else:
        messages.success(
            request,
            _("Opened. %(n)s part(s) are now on the shelf at their share of the kit's cost.")
            % {"n": len(released)},
        )
    return redirect("part_detail", pk=part.pk)


@require_POST
@login_required
def lot_close_kit(request, pk, lot_id):
    """Undo an opening, while everything that came out is still untouched."""
    part = get_object_or_404(Part, pk=pk)
    lot = get_object_or_404(StockLot, pk=lot_id, part=part)
    try:
        kits = close_kit(lot, user=request.user)
    except ValidationError as exc:
        messages.error(request, " ".join(exc.messages))
    else:
        messages.success(request, _("Back in the box: %(n)s kit(s).") % {"n": kits})
    return redirect("part_detail", pk=part.pk)


@login_required
def part_list(request):
    """The catalog, with kit contents shown as contents.

    Flat, a compressor kit and the three parts inside it are four peers, and
    nothing on the screen says the last three are in the first one's box. So a
    kit carries its contents beneath it and they are not repeated at the top
    level — the shape of the shelf, rather than of the table.

    **Except while narrowed** — searched, filtered to a category, or split by
    kind — where they stay flat and carry a label naming the kit instead.
    Somebody searching for "condenser" wants the condenser; filing it under a
    kit whose name does not match the search would hide the only row they asked
    for, and a category filter has exactly the same problem.
    """
    query = request.GET.get("q", "")
    category = request.GET.get("category", "").strip()
    kind = request.GET.get("kind", "")
    # An unrecognized `kind` is everything rather than nothing. A URL somebody
    # edited or a bookmark from a renamed value should show the catalog, not an
    # empty screen that looks like a shop with no parts in it.
    consumable = KINDS.get(kind)
    if consumable is None:
        kind = ""

    # Everything the search box and the category picker allow, *before* the
    # parts/consumables split — so the split can say how many rows each side
    # holds under the filters already applied. "Consumables (0)" is the answer
    # to a question, where an unlabeled tab that turns out to be empty is a
    # dead end somebody has to walk into to discover.
    narrowed = matching(query, category=category)
    # `.order_by()` first, and it is load-bearing: the default ordering is
    # `["name"]`, and Django adds ordering columns to the GROUP BY — so without
    # this the tally is one row per part rather than one row per kind.
    tally = {
        row["is_consumable"]: row["n"]
        for row in narrowed.order_by()
        .values("is_consumable")
        .annotate(n=Count("pk", distinct=True))
    }
    counts = {
        "part": tally.get(False, 0),
        "consumable": tally.get(True, 0),
        "all": tally.get(False, 0) + tally.get(True, 0),
    }

    # Both halves of this used to stop silently. Browsing took the first two
    # hundred rows of the table and said nothing about the rest; searching took
    # `find`'s default twenty-five, so a search matching forty parts reported
    # thirty-nine and a half of them as not existing. A cap with no page
    # numbers under it is not a limit, it is a lie about the catalog.
    matched = matching(query, category=category, consumable=consumable)
    page = Paginator(matched, PAGE_SIZE).get_page(request.GET.get("page"))
    found = list(page.object_list)

    # Everything each row shows, in four queries rather than six per part. The
    # list previously answered "how many on hand?" with an aggregate per part
    # and then asked again for `is_low`; adding price, fitment and purchase
    # history on top of that would have been a page of five hundred queries.
    parts = list(
        Part.objects.filter(pk__in=[part.pk for part in found]).prefetch_related(
            "categories",
            Prefetch(
                "fitments", queryset=PartFitment.objects.select_related("asset")
            ),
            Prefetch(
                "stock_lots", queryset=StockLot.objects.select_related("location")
            ),
            Prefetch(
                "purchase_lines",
                queryset=PurchaseLine.objects.select_related(
                    "purchase", "purchase__vendor"
                ),
            ),
        )
    )

    inside: dict = {}
    within: dict = {}
    for item in PartKitItem.objects.select_related("kit", "part"):
        inside.setdefault(item.kit_id, []).append(item)
        within.setdefault(item.part_id, []).append(item.kit)

    shown = {part.pk for part in parts}
    asked_for = bool(query or category or kind)
    rows = []
    for part in parts:
        part.kit_contents = inside.get(part.pk, [])
        part.boxed_in = within.get(part.pk, [])
        _summarize(part)
        # Nested under its kit rather than listed twice — but only when that kit
        # is actually on this page to nest under, and only while nothing has
        # been asked for. Under a filter the rows that match are the answer,
        # and folding one of them under a kit that matched for its own reasons
        # hides it behind a heading nobody was looking at.
        if part.boxed_in and not asked_for:
            if any(kit.pk in shown for kit in part.boxed_in):
                continue
        rows.append(part)

    return render(
        request,
        "parts/list.html",
        {
            "parts": rows,
            "q": query,
            "category": category,
            "categories": categories(),
            "kind": kind,
            "counts": counts,
            "filtered": asked_for,
            "low": restock_list(),
            "page": page,
            "cores_owed": PartUsage.objects.filter(
                part__has_core=True, core_returned=False
            ).count(),
        },
    )


#: How many parts a page of the catalog holds. Large enough that a small
#: shop never sees a second page, and bounded so a large one still renders.
PAGE_SIZE = 100


#: How many vehicles a row names before it stops naming them. Three fits the
#: line; the rest become a count, which is still an answer.
FITS_SHOWN = 3


def _summarize(part) -> None:
    """Attach what a list row shows, all of it from data already fetched.

    Reading a `@property` here would be the wrong instinct: `on_hand` and
    `known_cost` each issue their own query, and on two hundred rows that is the
    difference between a page and a stall. Everything below walks the prefetched
    lists instead.
    """
    lots = list(part.stock_lots.all())
    part.shelf_qty = sum((lot.qty_on_hand for lot in lots), Decimal(0))
    part.is_short = (
        part.min_quantity is not None and part.shelf_qty < part.min_quantity
    )
    part.where = sorted(
        {lot.location.path for lot in lots if lot.qty_on_hand > 0 and lot.location}
    )

    fitments = [
        fitment
        for fitment in part.fitments.all()
        if fitment.confidence != PartFitment.Confidence.DOES_NOT_FIT
    ]
    part.fits_named = [fitment.vehicle for fitment in fitments[:FITS_SHOWN]]
    part.fits_more = max(len(fitments) - FITS_SHOWN, 0)

    lines = [line for line in part.purchase_lines.all() if line.purchase_id]
    part.last_bought = (
        max(lines, key=lambda line: (line.purchase.ordered_on or date.min, line.created_at))
        if lines
        else None
    )


@login_required
def part_detail(request, pk):
    part = get_object_or_404(
        Part.objects.prefetch_related(
            "cross_refs",
            "fitments",
            Prefetch("stock_lots", queryset=StockLot.objects.select_related("location")),
        ),
        pk=pk,
    )
    # Each row shows the price it is carrying and the percentage that works out
    # to — through the same allocator the cents go through, so the percentages
    # on the screen add up to 100 for the same reason the money adds up to the
    # kit's price. `priced` is what lets the card say *why* the split is even
    # when it is, rather than leaving somebody to deduce it from four 25s.
    kit_items = list(part.kit_items.select_related("part"))
    weights, priced = kit_weights(kit_items)
    for item, percent in zip(kit_items, split_kit_cost(100, weights)):
        item.share_percent = percent
    return render(
        request,
        "parts/detail.html",
        {
            "part": part,
            "lots": part.stock_lots.all(),
            "kit_items": kit_items,
            "kit_split_is_priced": priced,
            # The answer to "do I already have one of these?" when the one you
            # have is inside a box with three other things.
            "in_kits": part.available_in_kits(),
            "usages": part.usages.select_related(
                "work_order", "work_order__asset", "asset"
            )[:25],
            "purchase_lines": part.purchase_lines.select_related("purchase", "purchase__vendor")[:25],
            "lot_form": LotForm(part=part),
            # For "I fitted this, there was no job" — the vehicle is usually the
            # one thing that is remembered.
            "vehicles": visible_assets(request.user),
        },
    )


@login_required
def part_search(request):
    """What a part chooser offers, as JSON (FR-PART-1).

    The chooser used to be a `<select>` holding `Part.objects.all()[:500]` —
    every part ever bought, in one flat list, on four different screens. Parts
    are never removed from the catalog when they are used up, so that list
    only ever grows: a second vehicle or a year of jobs turns choosing a part
    into scrolling past everything that was ever chosen before, and at five
    hundred it silently stopped listing them at all.

    The list is gone. This answers a search box instead, so length stops being
    the reader's problem — and with nothing typed it answers with the
    shortlist, which is what makes the resting state short.
    """
    asset = Asset.objects.filter(pk=_uuid(request.GET.get("asset"))).first()
    choices = candidates(
        request.GET.get("q", ""),
        asset=asset,
        exclude=(request.GET.get("exclude") or "").split(","),
    )
    return JsonResponse({"results": [part_row(choice) for choice in choices]})


def part_row(choice) -> dict:
    """One chooser row: what it is, what is on the shelf, and how it is measured.

    The last two matter because the quantity box beside the chooser cannot know
    in the markup whether it is about gaskets or about kilograms of refrigerant.
    The chosen part carries its own step and its own convertible units, exactly
    as the `<option>` attributes used to.
    """
    part = choice.part
    return {
        "id": str(part.pk),
        "name": str(part),
        "detail": _detail(choice),
        "fits": choice.fits,
        "step": part.qty_step,
        "unit": part.unit,
        "units": list(part.compatible_units),
    }


def _detail(choice) -> str:
    """The line under the name, which is the whole point of ranking out loud.

    "none on hand" is the useful half during planning: a part that fits and is
    not on the shelf is the thing that has to be bought, and saying so here
    means the gap is visible before the requirement is even saved.
    """
    part = choice.part
    if choice.on_hand > 0:
        stock = _("%(qty)s %(unit)s on hand") % {
            "qty": floatformat(choice.on_hand, "-3"),
            "unit": part.unit_label,
        }
    else:
        stock = _("none on hand")
    return f"{stock} · {_('fits this vehicle')}" if choice.fits else stock


def _uuid(value):
    """A primary key from a query string, or nothing — never an exception."""
    try:
        return UUID(str(value))
    except (ValueError, AttributeError, TypeError):
        return None


@login_required
def part_create(request):
    # `?upc=` arrives from a scan that found nothing. Carrying it through means
    # the barcode is recorded on the new part without anybody retyping it —
    # which is the whole reason the scan-and-miss path is worth having.
    scanned = (request.GET.get("upc") or request.POST.get("scanned_upc") or "").strip()[:80]

    if request.method == "POST":
        form = PartForm(request.POST)
        if form.is_valid():
            part = form.save()
            if scanned:
                PartCrossRef.objects.get_or_create(
                    part=part, system=PartCrossRef.System.UPC, value=scanned
                )
            messages.success(request, _("Added %(name)s.") % {"name": part.name})
            return redirect("part_detail", pk=part.pk)
    else:
        # `?name=` arrives from a chooser that found nothing — the planning case,
        # where the part being named is one nobody has bought yet. Carrying the
        # typed name through means the search that failed becomes the first
        # field of the form that fixes it, instead of being retyped.
        form = PartForm(initial={"name": (request.GET.get("name") or "").strip()[:200]})
    return render(
        request, "parts/form.html", {"form": form, "part": None, "scanned_upc": scanned}
    )


@login_required
def part_edit(request, pk):
    part = get_object_or_404(Part, pk=pk)
    if request.method == "POST":
        form = PartForm(request.POST, instance=part)
        if form.is_valid():
            form.save()
            messages.success(request, _("Saved."))
            return redirect("part_detail", pk=part.pk)
    else:
        form = PartForm(instance=part)
    return render(request, "parts/form.html", {"form": form, "part": part})


@require_POST
@login_required
def crossref_add(request, pk):
    part = get_object_or_404(Part, pk=pk)
    value = (request.POST.get("value") or "").strip()
    if value:
        PartCrossRef.objects.get_or_create(
            part=part, system=request.POST.get("system") or "interchange", value=value
        )
    return redirect("part_detail", pk=part.pk)


@require_POST
@login_required
def lot_add(request, pk):
    """Put stock on the shelf without a purchase record — the common case for
    parts that were already in the garage when the shop started."""
    part = get_object_or_404(Part, pk=pk)
    form = LotForm(request.POST)
    if form.is_valid():
        lot = form.save(commit=False)
        lot.part = part
        lot.qty_on_hand = 0
        lot.save()
        StockTransaction.record(
            lot,
            part.quantity_in_stock_units(
                form.cleaned_data["quantity"], request.POST.get("quantity_unit")
            ),
            StockTransaction.Reason.FOUND,
            note=str(_("Added by hand")),
            user=request.user,
        )
        messages.success(request, _("Stock added."))
    else:
        messages.error(request, _("Check the quantity and try again."))
    return redirect("part_detail", pk=part.pk)


@login_required
def lot_edit(request, pk, lot_id):
    """Fix what a lot was recorded with (FR-INV-11).

    Stock could be added but never corrected, so a lot entered without a cost
    or a location stayed that way — and a lot with no cost is not a cosmetic
    problem: everything drawn from it costs nothing, so the job it goes on is
    cheaper than it was and the shelf is worth less than it is.
    """
    part = get_object_or_404(Part, pk=pk)
    lot = get_object_or_404(StockLot, pk=lot_id, part=part)
    form = LotEditForm(request.POST or None, instance=lot)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, _("Saved."))
        return redirect("part_detail", pk=part.pk)
    return render(
        request, "parts/lot_form.html", {"part": part, "lot": lot, "form": form}
    )


@require_POST
@login_required
def lot_delete(request, pk, lot_id):
    """Remove a lot that should not have been recorded at all.

    Only while it is untouched. Once anything has been drawn from a lot, the
    draw is what a job cost and what a vehicle's history says it cost, and
    deleting the lot underneath it rewrites both — so the three ways a lot can
    be entangled are each refused by name, each naming the tool that does fit:

    * **drawn from** — count it instead, which records the discrepancy rather
      than hiding it;
    * **received against a purchase** — un-receive the line, which puts the
      order back where it was;
    * **opened out of a kit** — put the kit back together, which is the same
      event in reverse.
    """
    part = get_object_or_404(Part, pk=pk)
    lot = get_object_or_404(StockLot, pk=lot_id, part=part)

    if lot.usages.exists() or lot.transactions.filter(delta__lt=0).exists():
        messages.error(
            request,
            _(
                "Something has already come out of this lot, so removing it "
                "would change what a job cost. Count it to zero instead."
            ),
        )
    elif lot.purchase_line_id is not None:
        messages.error(
            request,
            _(
                "This arrived against a purchase. Un-receive it on the order, "
                "so the order goes back to expecting it."
            ),
        )
    elif lot.from_kit_lot_id is not None:
        messages.error(
            request,
            _("This came out of a kit. Put the kit back together instead."),
        )
    else:
        lot.delete()
        messages.success(request, _("Lot removed."))
    return redirect("part_detail", pk=part.pk)


@require_POST
@login_required
def part_use(request, pk):
    """Take a part off the shelf without a job to hang it on (FR-INV-10).

    Every other way out of stock wanted a work order first, and a home shop is
    full of parts whose story is "I fitted that, I bought it in June and I do
    not remember the rest". The alternatives were inventing a work order — which
    puts a fiction in the vehicle's history — or leaving the part on the shelf
    for ever, where it silently inflates what the shop thinks it owns.

    Everything except the quantity is optional, and the quantity defaults to
    one. A vehicle and a date are recorded when offered, because they are
    usually the parts somebody does remember; nothing is required to know them.
    """
    part = get_object_or_404(Part, pk=pk)
    asset = None
    if request.POST.get("asset"):
        from homeautoshop.assets.models import Asset

        asset = Asset.objects.filter(pk=request.POST["asset"]).first()

    # Entered in whatever is written on the container, held in the part's own
    # unit. Half a kilogram of R-134a out of a cylinder stocked by the pound is
    # 1.102 lb, and nobody should be doing that on paper.
    quantity = part.quantity_in_stock_units(
        request.POST.get("qty") or 1, request.POST.get("qty_unit")
    )
    try:
        result = consume(
            part,
            quantity,
            asset=asset,
            installed_at=request.POST.get("installed_at") or None,
            note=(request.POST.get("note") or "").strip(),
            user=request.user,
        )
    except ValidationError as exc:
        messages.error(request, " ".join(exc.messages))
    else:
        taken = sum(usage.qty for usage in result.usages)
        messages.success(
            request,
            _("Recorded: %(n)s used.") % {"n": taken}
            if asset is None
            else _("Recorded: %(n)s used on %(vehicle)s.")
            % {"n": taken, "vehicle": asset},
        )
    return redirect("part_detail", pk=part.pk)


@require_POST
@login_required
def lot_count(request, pk, lot_id):
    part = get_object_or_404(Part, pk=pk)
    lot = get_object_or_404(StockLot, pk=lot_id, part=part)
    note = (request.POST.get("note") or "").strip()
    try:
        counted = part.quantity_in_stock_units(
            request.POST.get("counted") or 0, request.POST.get("counted_unit")
        )
        entry = cycle_count(lot, counted, note=note, user=request.user)
    except ValidationError as exc:
        messages.error(request, " ".join(exc.messages))
    else:
        messages.success(
            request, _("Counted.") if entry is None else _("Adjustment recorded.")
        )
    return redirect("part_detail", pk=part.pk)


@login_required
def inventory(request):
    """Mirrors the physical shop (FR-INV-2).

    `?location=` is what a scanned bin label lands on. Without it this is the
    whole shelf, which is the right default for a desk and useless with a label
    in your hand — the promise of the label is that it opens *that* bin.
    """
    locations = (
        Location.objects.prefetch_related(
            Prefetch(
                "stock_lots",
                queryset=StockLot.objects.filter(qty_on_hand__gt=0).select_related("part"),
            )
        )
        .annotate(units=Sum("stock_lots__qty_on_hand"))
        .order_by("name")
    )

    focus = None
    if wanted := request.GET.get("location"):
        focus = Location.objects.filter(pk=wanted).first()
        if focus is None:
            messages.warning(
                request, _("That label points at a location that is no longer here.")
            )
        else:
            # Children too: scanning a cabinet should show what is in its
            # drawers, not an empty cabinet.
            locations = locations.filter(pk__in=_with_children(focus))

    unfiled = StockLot.objects.filter(location__isnull=True, qty_on_hand__gt=0).select_related("part")
    return render(
        request,
        "parts/inventory.html",
        {
            "locations": locations,
            "focus": focus,
            # The shop-wide panels are noise when the question is "what is in
            # this bin", and they are the point of the page otherwise.
            "unfiled": [] if focus else unfiled,
            "low": [] if focus else restock_list(),
            "expiring": [] if focus else expiring_lots(),
            "value": None if focus else inventory_value(),
        },
    )


def _with_children(location) -> list:
    """A location and everything nested inside it.

    Walked in Python rather than with a recursive CTE: the tree is a garage's
    shelves, so it is a handful of rows deep, and a `WITH RECURSIVE` here would
    be the more impressive way to be no faster.
    """
    found = [location.pk]
    frontier = [location.pk]
    while frontier:
        children = list(
            Location.objects.filter(parent_id__in=frontier).values_list("pk", flat=True)
        )
        children = [pk for pk in children if pk not in found]
        found.extend(children)
        frontier = children
    return found


@login_required
def core_list(request):
    """Every core, owed and returned, in one place (FR-PUR-4).

    Reported as: *"I know I saw a button to mark the core returned but I can't
    find it again."* It was on the **Shelf** screen, in a panel among the
    restock list and the expiring fluids — and the reporter found it twice by
    accident, which is a stronger signal than never finding it at all. A core
    is a fact about a part somebody fitted, not about a bin it was stored in,
    and it was filed under storage.

    So it lives under Parts, it shows the returned ones as well as the owed —
    the question is *what happened to this core*, and a list of debts alone
    cannot answer it — and every "core owed" pill in the application links
    here, so the button is wherever the problem is mentioned.
    """
    owed = outstanding_cores()
    return render(
        request,
        "parts/cores.html",
        {
            "owed": owed,
            "returned": returned_cores(),
            "owed_value": core_value_owed(owed),
        },
    )


@require_POST
@login_required
def core_update(request):
    """Mark cores returned, or put one back to owed.

    Takes a list rather than one id: cores come back to the counter in an
    armful, and ticking six boxes and pressing one button is the difference
    between recording that and not bothering. The undo is the same endpoint
    because a core marked returned by a slip needs the same journey back.
    """
    from django.utils import timezone

    usages = list(
        PartUsage.objects.filter(pk__in=request.POST.getlist("usage"), part__has_core=True)
    )
    if not usages:
        messages.error(request, _("Choose a core first."))
        return redirect(request.POST.get("next") or "core_list")

    returning = request.POST.get("state", "returned") == "returned"
    for usage in usages:
        usage.core_returned = returning
        usage.core_returned_on = timezone.localdate() if returning else None
        usage.save()

    messages.success(
        request,
        ngettext(
            "%(n)d core marked returned.", "%(n)d cores marked returned.", len(usages)
        )
        % {"n": len(usages)}
        if returning
        else ngettext(
            "%(n)d core is owed again.", "%(n)d cores are owed again.", len(usages)
        )
        % {"n": len(usages)},
    )
    return redirect(request.POST.get("next") or "core_list")


@require_POST
@login_required
def location_create(request):
    name = (request.POST.get("name") or "").strip()
    if name:
        Location.objects.create(name=name, parent_id=request.POST.get("parent") or None)
        messages.success(request, _("Location added."))
    return redirect("inventory")


class LocationForm(forms.ModelForm):
    class Meta:
        model = Location
        fields = ["name", "parent", "notes"]
        widgets = {"notes": forms.Textarea(attrs={"rows": 2})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["parent"].required = False
        self.fields["parent"].empty_label = _("— top level —")
        if self.instance.pk:
            # A location cannot be moved inside itself or inside anything it
            # contains. `clean()` catches it, but a dropdown that offers the
            # impossible is a worse way to find that out than one that does not.
            self.fields["parent"].queryset = Location.objects.exclude(
                pk__in=_descendants(self.instance) | {self.instance.pk}
            )
        for name, field in self.fields.items():
            field.required = name == "name"
            css = "select" if isinstance(field.widget, forms.Select) else "input"
            if isinstance(field.widget, forms.Textarea):
                css = "input textarea"
            field.widget.attrs.setdefault("class", css)


def _descendants(location) -> set:
    """Every location underneath this one, however deep."""
    found: set = set()
    frontier = [location.pk]
    while frontier:
        children = list(
            Location.objects.filter(parent_id__in=frontier)
            .exclude(pk__in=found)
            .values_list("pk", flat=True)
        )
        if not children:
            break
        found.update(children)
        frontier = children
    return found


@login_required
def location_edit(request, pk):
    """Rename a shelf, or move a bin into a different cabinet (FR-INV-2).

    Locations could be created and never touched, so a typo was permanent and a
    cabinet could not be reorganized — on a shelf whose labels are *printed and
    stuck to the bins*, which is exactly where a wrong name is most expensive.
    The label keeps working either way: it carries the primary key, not the name.
    """
    location = get_object_or_404(Location, pk=pk)
    form = LocationForm(request.POST or None, instance=location)
    if request.method == "POST" and form.is_valid():
        try:
            form.instance.full_clean()
        except ValidationError as exc:
            messages.error(request, " ".join(m for ms in exc.message_dict.values() for m in ms))
        else:
            form.save()
            messages.success(request, _("Saved."))
            return redirect("inventory")
    return render(
        request, "parts/location_form.html", {"location": location, "form": form}
    )


@require_POST
@login_required
def location_delete(request, pk):
    """Remove a place nothing is kept in.

    Refused while it holds stock or contains another location. `StockLot.location`
    is `SET_NULL`, so a soft delete would not orphan a lot outright — it would
    do something worse and quieter: leave every lot in it pointing at a place no
    list shows, which reads as *unfiled* on one screen and as a real shelf on
    another. Empty the bin first, and the question answers itself.
    """
    location = get_object_or_404(Location, pk=pk)
    children = Location.objects.filter(parent=location).count()
    held = location.stock_lots.count()

    if children:
        messages.error(
            request,
            _("%(name)s contains %(n)s other location(s). Move or remove those first.")
            % {"name": location.name, "n": children},
        )
    elif held:
        messages.error(
            request,
            _("%(name)s still holds %(n)s stock lot(s). Move them somewhere else first.")
            % {"name": location.name, "n": held},
        )
    else:
        name = location.name
        location.delete()
        messages.success(request, _("Removed %(name)s.") % {"name": name})
    return redirect("inventory")


@require_POST
@login_required
def crossref_remove(request, pk, ref_id):
    """Take a number off a part.

    A cross-reference is a claim that this number finds this part, and a wrong
    one is worse than a missing one: it makes a scan or a search land on the
    wrong shelf, confidently.
    """
    part = get_object_or_404(Part, pk=pk)
    ref = get_object_or_404(PartCrossRef, pk=ref_id, part=part)
    value = ref.value
    ref.delete()
    messages.success(request, _("Removed %(value)s.") % {"value": value})
    return redirect("part_detail", pk=part.pk)


@require_POST
@login_required
def part_delete(request, pk):
    """Remove a part that should not be in the catalog.

    Refused while any of it is on the shelf, and while a kit lists it as one of
    its contents. Stock is the harder rule of the two: a part with a quantity is
    a thing in the building, and making it disappear from every screen does not
    make it disappear from the drawer — the shelf would simply stop matching the
    room. Count it out or use it, and then it goes.

    Its history does not go. Usages, purchase lines and fitments name it and
    keep naming it; this is a soft delete like everything else, and the trash
    holds it for thirty days (P-5).
    """
    part = get_object_or_404(Part, pk=pk)
    in_kits = PartKitItem.objects.filter(part=part).select_related("kit")

    if part.on_hand > 0:
        messages.error(
            request,
            _(
                "There are still %(n)s of these on the shelf. Count them out or "
                "use them first — removing the record does not empty the drawer."
            )
            % {"n": part.on_hand},
        )
    elif in_kits.exists():
        messages.error(
            request,
            _("%(kits)s list this as one of their contents. Remove it from those first.")
            % {"kits": ", ".join(str(item.kit) for item in in_kits[:3])},
        )
    else:
        name = str(part)
        part.delete()
        messages.success(request, _("Removed %(name)s.") % {"name": name})
        return redirect("part_list")
    return redirect("part_detail", pk=part.pk)


@login_required
def part_by_code(request):
    """Find or create a part from a scanned barcode (SPEC FR-INV-3).

    Three outcomes, and the third is the one that matters. A code that is
    already on file opens that part; a code that matches nothing offers to
    create one *with the barcode already recorded as a cross-reference*, so the
    next scan of the same box finds it. Without that last step, scanning a new
    part teaches the shop nothing and the operator scans it again next month to
    the same empty result.
    """
    code = (request.GET.get("code") or "").strip()
    if not code:
        messages.warning(request, _("Nothing was scanned."))
        return redirect("part_list")

    matches = list(
        Part.objects.filter(
            Q(cross_refs__value__iexact=code) | Q(part_number__iexact=code)
        ).distinct()[:2]
    )

    if len(matches) == 1:
        return redirect("part_detail", pk=matches[0].pk)
    if len(matches) > 1:
        # Two parts carrying one barcode is a data problem, not a lookup to
        # guess at. The search screen shows both.
        messages.info(
            request, _("More than one part carries that code.")
        )
        return redirect(f"{reverse('part_list')}?q={code}")

    messages.info(
        request,
        _("Nothing on file for %(code)s yet. Adding it here records the barcode too.")
        % {"code": code},
    )
    return redirect(f"{reverse('part_create')}?upc={code}")
