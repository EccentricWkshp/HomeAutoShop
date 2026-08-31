"""Purchases, vendors, and expenses (SPEC §7.5, §7.6)."""

from __future__ import annotations

from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST

from homeautoshop.core.measurements import Money
from homeautoshop.core.moneyform import MoneyFormMixin, parse_amount
from homeautoshop.mediafiles.models import MediaLink
from homeautoshop.mediafiles.services import ingest
from homeautoshop.parts.models import Location
from homeautoshop.parts.services import resolve_part

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


@login_required
def purchase_edit(request, pk):
    """Correct the order itself (FR-PUR-1).

    Lines could be added and received and the *order* could not be touched, so
    a missing order number, a tax figure left at zero, or the wrong vendor was
    permanent — and shipping and tax are not decoration: they are what makes a
    landed cost landed, and every lot received against this order is priced
    from them.
    """
    purchase = get_object_or_404(Purchase, pk=pk)
    form = PurchaseForm(request.POST or None, instance=purchase)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, _("Saved."))
        return redirect("purchase_detail", pk=purchase.pk)
    return render(
        request, "purchasing/form.html", {"form": form, "purchase": purchase}
    )


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

    # A line with no part is ordinary — "not cataloged" is a real answer, and
    # the description carries what was bought — so a name that resolves to
    # nothing is not an error here the way it is on a work order.
    part, _problem = resolve_part(request.POST)

    PurchaseLine.objects.create(
        purchase=purchase,
        part=part,
        description_as_ordered=(request.POST.get("description") or "").strip(),
        qty_ordered=request.POST.get("qty_ordered") or 1,
        unit_price_minor=unit_price,
        core_charge_minor=core_charge,
    )
    return redirect("purchase_detail", pk=purchase.pk)


class PurchaseLineForm(MoneyFormMixin, forms.ModelForm):
    """A line after it was typed (FR-PUR-1).

    Everything except how many have arrived. `qty_received` is written by
    receiving and unwound by un-receiving, both of which move stock; a box here
    would change the number without moving anything, so the shelf and the order
    would disagree and neither would be wrong on its own terms.
    """

    class Meta:
        model = PurchaseLine
        fields = [
            "part", "description_as_ordered", "qty_ordered",
            "unit_price_minor", "core_charge_minor",
        ]
        labels = {
            "description_as_ordered": _("Description"),
            "qty_ordered": _("Quantity ordered"),
            "unit_price_minor": _("Unit price"),
            "core_charge_minor": _("Core charge"),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["part"].required = False
        self.fields["part"].empty_label = _("Not cataloged")
        for name, field in self.fields.items():
            field.required = False
            css = "select" if isinstance(field.widget, forms.Select) else "input"
            field.widget.attrs.setdefault("class", css)


@login_required
def purchase_line_edit(request, pk, line_id):
    """Correct a line that was typed wrong.

    Refused once any of it has been received: the receipt created stock at this
    price, and changing the price underneath it would leave lots on the shelf
    costed at a number the order no longer states. Un-receive first — that path
    already exists and already puts the stock back.
    """
    purchase = get_object_or_404(Purchase, pk=pk)
    line = get_object_or_404(PurchaseLine, pk=line_id, purchase=purchase)

    if line.qty_received:
        messages.error(
            request,
            _(
                "Some of this line has been received, and the stock it made "
                "carries this price. Un-receive it first, then correct it."
            ),
        )
        return redirect("purchase_detail", pk=purchase.pk)

    form = PurchaseLineForm(request.POST or None, instance=line)
    if request.method == "POST" and form.is_valid():
        form.save()
        purchase.recompute_status()
        messages.success(request, _("Saved."))
        return redirect("purchase_detail", pk=purchase.pk)
    return render(
        request,
        "purchasing/line_form.html",
        {"purchase": purchase, "line": line, "form": form},
    )


@require_POST
@login_required
def purchase_line_delete(request, pk, line_id):
    """Remove a line that should not be on the order."""
    purchase = get_object_or_404(Purchase, pk=pk)
    line = get_object_or_404(PurchaseLine, pk=line_id, purchase=purchase)

    if line.qty_received:
        messages.error(
            request,
            _(
                "Some of this line is on the shelf. Un-receive it first, so the "
                "stock goes back before the line that explains it does."
            ),
        )
    else:
        line.delete()
        purchase.recompute_status()
        messages.success(request, _("Line removed."))
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


@login_required
def vendor_edit(request, pk):
    """Correct a vendor after it was added (FR-PUR-2).

    Vendors could be created and never touched again, so a name typed in a
    hurry — or one the order importer minted from a document — stayed that way
    on every purchase that named it.
    """
    vendor = get_object_or_404(Vendor, pk=pk)
    form = VendorForm(request.POST or None, instance=vendor)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, _("Saved."))
        return redirect("vendor_list")
    return render(
        request, "purchasing/vendor_form.html", {"vendor": vendor, "form": form}
    )


@require_POST
@login_required
def vendor_delete(request, pk):
    """Remove a vendor nothing has been bought from.

    Refused once a purchase names it. `Purchase.vendor` is `PROTECT` for a
    reason — an order without a supplier is not a record of anything — and a
    soft delete would slip past that check while leaving the purchase pointing
    at a vendor no list will show. A vendor you have stopped using is not a
    vendor you never used.
    """
    vendor = get_object_or_404(Vendor, pk=pk)
    if vendor.purchases.exists():
        messages.error(
            request,
            _(
                "%(name)s is named on %(n)s purchase(s), so it stays. Nothing is "
                "lost by leaving a vendor you no longer use."
            )
            % {"name": vendor.name, "n": vendor.purchases.count()},
        )
    else:
        name = vendor.name
        vendor.delete()
        messages.success(request, _("Removed %(name)s.") % {"name": name})
    return redirect("vendor_list")


@login_required
def expense_edit(request, pk):
    """Correct an expense (FR-COST-1).

    An amount typed wrong lands in a vehicle's cost per mile and in every
    rollup that reads it, and there was no way to change it.
    """
    expense = get_object_or_404(Expense, pk=pk)
    form = ExpenseForm(request.POST or None, instance=expense)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, _("Saved."))
        return redirect(_expense_home(expense))
    return render(
        request, "purchasing/expense_form.html", {"expense": expense, "form": form}
    )


@require_POST
@login_required
def expense_delete(request, pk):
    expense = get_object_or_404(Expense, pk=pk)
    home = _expense_home(expense)
    expense.delete()
    messages.success(request, _("Expense removed."))
    return redirect(home)


def _expense_home(expense):
    """Back where it was entered — a job, a vehicle, or the expense list."""
    if expense.work_order_id:
        return reverse("work_order_detail", args=[expense.work_order_id])
    if expense.asset_id:
        return reverse("asset_costs", args=[expense.asset_id])
    return reverse("purchase_list")


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
