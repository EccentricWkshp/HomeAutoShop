"""Parts and inventory views (SPEC §7.4, §9.3)."""

from __future__ import annotations

from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.db.models import Prefetch, Q, Sum
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST

from homeautoshop.core.costs import inventory_value

from .models import Location, Part, PartCrossRef, PartUsage, StockLot, StockTransaction
from .services import consume, cycle_count, expiring_lots, find, outstanding_cores, restock_list


class PartForm(forms.ModelForm):
    class Meta:
        model = Part
        fields = [
            "name", "category", "manufacturer", "part_number", "part_type",
            "unit", "is_consumable", "has_core", "core_value_minor",
            "min_quantity", "notes",
        ]
        widgets = {"notes": forms.Textarea(attrs={"rows": 2})}
        labels = {"core_value_minor": _("Core charge (minor units)")}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name, field in self.fields.items():
            field.required = name == "name"
            if not isinstance(field.widget, forms.CheckboxInput):
                css = "select" if isinstance(field.widget, forms.Select) else "input"
                if isinstance(field.widget, forms.Textarea):
                    css = "input textarea"
                field.widget.attrs.setdefault("class", css)


class LotForm(forms.ModelForm):
    quantity = forms.DecimalField(max_digits=12, decimal_places=3, min_value=0)

    class Meta:
        model = StockLot
        fields = ["location", "unit_cost_minor", "acquired_on", "expires_on"]
        widgets = {
            "acquired_on": forms.DateInput(attrs={"type": "date"}),
            "expires_on": forms.DateInput(attrs={"type": "date"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name, field in self.fields.items():
            field.required = name == "quantity"
            css = "select" if isinstance(field.widget, forms.Select) else "input"
            field.widget.attrs.setdefault("class", css)


@login_required
def part_list(request):
    query = request.GET.get("q", "")
    parts = find(query) if query else list(Part.objects.all()[:200])
    return render(
        request,
        "parts/list.html",
        {"parts": parts, "q": query, "low": restock_list()},
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
    return render(
        request,
        "parts/detail.html",
        {
            "part": part,
            "lots": part.stock_lots.all(),
            "usages": part.usages.select_related("work_order", "work_order__asset")[:25],
            "purchase_lines": part.purchase_lines.select_related("purchase", "purchase__vendor")[:25],
            "lot_form": LotForm(),
        },
    )


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
        form = PartForm()
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
            form.cleaned_data["quantity"],
            StockTransaction.Reason.FOUND,
            note=str(_("Added by hand")),
            user=request.user,
        )
        messages.success(request, _("Stock added."))
    else:
        messages.error(request, _("Check the quantity and try again."))
    return redirect("part_detail", pk=part.pk)


@require_POST
@login_required
def lot_count(request, pk, lot_id):
    part = get_object_or_404(Part, pk=pk)
    lot = get_object_or_404(StockLot, pk=lot_id, part=part)
    note = (request.POST.get("note") or "").strip()
    try:
        entry = cycle_count(lot, request.POST.get("counted") or 0, note=note, user=request.user)
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
            "cores": [] if focus else outstanding_cores(),
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


@require_POST
@login_required
def core_returned(request, usage_id):
    from django.utils import timezone

    usage = get_object_or_404(PartUsage, pk=usage_id)
    usage.core_returned = True
    usage.core_returned_on = timezone.localdate()
    usage.save()
    messages.success(request, _("Core marked returned."))
    return redirect(request.POST.get("next") or "inventory")


@require_POST
@login_required
def location_create(request):
    name = (request.POST.get("name") or "").strip()
    if name:
        Location.objects.create(name=name, parent_id=request.POST.get("parent") or None)
        messages.success(request, _("Location added."))
    return redirect("inventory")


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
