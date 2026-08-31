"""Purchases, vendors, and expenses (SPEC §7.5, §7.6)."""

from __future__ import annotations

from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST

from homeautoshop.core.measurements import Money
from homeautoshop.core.moneyform import MoneyFormMixin, parse_amount
from homeautoshop.mediafiles.models import MediaLink
from homeautoshop.mediafiles.services import ingest
from homeautoshop.parts.models import Location, Part

from .models import Expense, Purchase, PurchaseLine, PurchaseStatus, Vendor


class VendorForm(forms.ModelForm):
    class Meta:
        model = Vendor
        fields = ["name", "type", "url", "account_number", "phone", "return_window_days", "notes"]
        widgets = {"notes": forms.Textarea(attrs={"rows": 2})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name, field in self.fields.items():
            field.required = name == "name"
            css = "select" if isinstance(field.widget, forms.Select) else "input"
            field.widget.attrs.setdefault("class", css)


class PurchaseForm(MoneyFormMixin, forms.ModelForm):
    class Meta:
        model = Purchase
        fields = [
            "vendor", "order_number", "status", "ordered_on",
            "tax_minor", "shipping_minor", "discount_minor",
            "payment_method", "work_order", "notes",
        ]
        widgets = {"ordered_on": forms.DateInput(attrs={"type": "date"}), "notes": forms.Textarea(attrs={"rows": 2})}
        labels = {
            "tax_minor": _("Tax"),
            "shipping_minor": _("Shipping"),
            "discount_minor": _("Discount"),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name, field in self.fields.items():
            field.required = name == "vendor"
            css = "select" if isinstance(field.widget, forms.Select) else "input"
            if isinstance(field.widget, forms.Textarea):
                css = "input textarea"
            field.widget.attrs.setdefault("class", css)


class ExpenseForm(MoneyFormMixin, forms.ModelForm):
    class Meta:
        model = Expense
        fields = ["category", "amount_minor", "incurred_on", "vendor", "description"]
        widgets = {"incurred_on": forms.DateInput(attrs={"type": "date"})}
        labels = {"amount_minor": _("Amount")}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name, field in self.fields.items():
            field.required = name in ("category", "amount_minor")
            css = "select" if isinstance(field.widget, forms.Select) else "input"
            field.widget.attrs.setdefault("class", css)


@login_required
def purchase_list(request):
    status = request.GET.get("status", "")
    qs = Purchase.objects.select_related("vendor").prefetch_related("lines")
    if status:
        qs = qs.filter(status=status)
    closing = [p for p in Purchase.objects.select_related("vendor") if p.return_window_closing]
    return render(
        request,
        "purchasing/list.html",
        {"purchases": qs[:100], "status": status, "statuses": PurchaseStatus.choices, "closing": closing},
    )


@login_required
def purchase_detail(request, pk):
    purchase = get_object_or_404(
        Purchase.objects.select_related("vendor").prefetch_related("lines__part"), pk=pk
    )
    return render(
        request,
        "purchasing/detail.html",
        {
            "purchase": purchase,
            "lines": purchase.lines.all(),
            "locations": Location.objects.all(),
            "parts": Part.objects.all()[:500],
            "receipts": MediaLink.for_entity(purchase),
            # Shows the shape an amount takes here, without asserting a value.
            "zero_amount": Money(0, purchase.currency),
        },
    )


@login_required
def purchase_create(request):
    if request.method == "POST":
        form = PurchaseForm(request.POST)
        if form.is_valid():
            purchase = form.save()
            return redirect("purchase_detail", pk=purchase.pk)
    else:
        form = PurchaseForm()
    return render(request, "purchasing/form.html", {"form": form, "purchase": None})


@require_POST
@login_required
def purchase_line_add(request, pk):
    purchase = get_object_or_404(Purchase, pk=pk)
    # Written out by hand in the template rather than as a Django form, so the
    # conversion has to be asked for here. It is the same `parse_amount` the
    # form field uses, so `$12.40` off a receipt means the same thing on this
    # screen as on every other one.
    try:
        unit_price = parse_amount(request.POST.get("unit_price_minor") or 0, purchase.currency)
        core_charge = parse_amount(request.POST.get("core_charge_minor") or 0, purchase.currency)
    except ValidationError as exc:
        messages.error(request, exc.messages[0])
        return redirect("purchase_detail", pk=purchase.pk)

    PurchaseLine.objects.create(
        purchase=purchase,
        part_id=request.POST.get("part") or None,
        description_as_ordered=(request.POST.get("description") or "").strip(),
        qty_ordered=request.POST.get("qty_ordered") or 1,
        unit_price_minor=unit_price,
        core_charge_minor=core_charge,
    )
    return redirect("purchase_detail", pk=purchase.pk)


@require_POST
@login_required
def purchase_line_receive(request, pk, line_id):
    """Receiving is the transition that makes the cost reports true."""
    purchase = get_object_or_404(Purchase, pk=pk)
    line = get_object_or_404(PurchaseLine, pk=line_id, purchase=purchase)
    try:
        line.receive(
            request.POST.get("qty") or None,
            location=Location.objects.filter(pk=request.POST.get("location") or None).first(),
            user=request.user,
        )
    except ValidationError as exc:
        messages.error(request, " ".join(exc.messages))
    else:
        messages.success(request, _("Received into stock at landed cost."))
    return redirect("purchase_detail", pk=purchase.pk)


@require_POST
@login_required
def purchase_line_unreceive(request, pk, line_id):
    """Take back a receipt recorded by mistake (FR-PUR-2).

    Marked received is a one-way door without this, and it is an easy button to
    hit: the line already knows the quantity, so receiving is a single tap on a
    screen where every line has one.
    """
    purchase = get_object_or_404(Purchase, pk=pk)
    line = get_object_or_404(PurchaseLine, pk=line_id, purchase=purchase)
    try:
        taken = line.unreceive(request.POST.get("qty") or None, user=request.user)
    except ValidationError as exc:
        messages.error(request, " ".join(exc.messages))
    else:
        messages.success(
            request,
            _("Took %(n)s back out of stock. The ledger records the correction.")
            % {"n": taken},
        )
    return redirect("purchase_detail", pk=purchase.pk)


@require_POST
@login_required
def purchase_delete(request, pk):
    """Remove a purchase, once it is not holding anything up.

    Refused while any of it is received, and the refusal is the useful part.
    A purchase is where a stock lot's cost came from; deleting one out from
    under stock that is on the shelf leaves parts whose landed cost points at
    nothing, and every cost rollup that reads through them quietly wrong. The
    message says what to do instead, and un-receiving is one button away.
    """
    purchase = get_object_or_404(Purchase, pk=pk)
    received = [line for line in purchase.lines.all() if line.qty_received > 0]
    if received:
        messages.error(
            request,
            _(
                "This purchase has %(n)s line(s) already received. Take those back out "
                "of stock first — deleting it now would leave the stock they created "
                "with no record of what it cost."
            )
            % {"n": len(received)},
        )
        return redirect("purchase_detail", pk=purchase.pk)

    purchase.delete()
    messages.success(
        request,
        _("Deleted. It is in the trash for 30 days if that was a mistake."),
    )
    return redirect("purchase_list")


@require_POST
@login_required
def purchase_receipt_upload(request, pk):
    purchase = get_object_or_404(Purchase, pk=pk)
    files = request.FILES.getlist("files")
    for upload in files:
        ingest(upload, user=request.user, entity=purchase, role=MediaLink.Role.RECEIPT)
    if files:
        messages.success(request, _("Receipt attached; it will be text-searchable shortly."))
    return redirect("purchase_detail", pk=purchase.pk)


@login_required
def vendor_list(request):
    if request.method == "POST":
        pass
    return render(request, "purchasing/vendors.html", {"vendors": Vendor.objects.all(), "form": VendorForm()})


@require_POST
@login_required
def vendor_create(request):
    form = VendorForm(request.POST)
    if form.is_valid():
        form.save()
        messages.success(request, _("Vendor added."))
    else:
        messages.error(request, _("A vendor needs a name."))
    return redirect("vendor_list")


@require_POST
@login_required
def expense_add(request, pk):
    from homeautoshop.work.models import WorkOrder

    work_order = get_object_or_404(WorkOrder, pk=pk)
    form = ExpenseForm(request.POST)
    if form.is_valid():
        expense = form.save(commit=False)
        expense.work_order = work_order
        expense.save()
        messages.success(request, _("Expense recorded."))
    else:
        messages.error(request, _("Check the amount and category."))
    return redirect("work_order_detail", pk=work_order.pk)
