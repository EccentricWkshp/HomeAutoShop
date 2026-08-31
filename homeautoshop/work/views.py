"""Work order views — the workbench (SPEC §7.3, §9.3)."""

from __future__ import annotations

import logging
from decimal import Decimal, InvalidOperation

from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q
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
from homeautoshop.purchasing.models import Vendor
from homeautoshop.purchasing.views import ExpenseForm

from . import parts_readiness, readiness
from .models import (
    REQUIREMENTS,
    JobItem,
    JobItemTool,
    PartRequirement,
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

        # A parent is a *project* — that is what the field is for, and what its
        # help text promises. The bug was not the filter, it was that a shop
        # with no projects got an empty dropdown and no explanation, which
        # reads as broken. The empty case now says what to do; see the label
        # below and the template.
        parents = WorkOrder.objects.filter(type=WorkOrderType.PROJECT)
        if self.instance.pk:
            # A work order cannot be its own parent, and neither can anything
            # already underneath it — either one makes a cycle that the
            # timeline walks forever.
            parents = parents.exclude(pk__in=self.instance.descendant_ids() | {self.instance.pk})
        self.fields["parent"].queryset = parents
        self.fields["parent"].empty_label = _("Not part of a project")
        if not parents.exists():
            self.fields["parent"].help_text = _(
                "Nothing here yet — set a work order's type to Project and it "
                "becomes available as a parent."
            )
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
    parts_needed = parts_readiness.for_work_order(wo)
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
            # The bare codes too: a template asking whether a move is legal
            # should not have to stringify a list of pairs to find out.
            "allowed_statuses": list(_allowed_next(wo)),
            # Which target needs which field, handed to the browser so the form
            # can say so before it is submitted rather than after. The check
            # itself stays in `transition_to`; this is only the sign on it.
            "status_requirements": {
                status: REQUIREMENTS[status]
                for status in _allowed_next(wo)
                if status in REQUIREMENTS
                # Completing a work order on something with no meter needs no
                # reading, so marking the field required would be a lie.
                and not (status == WorkOrderStatus.COMPLETE and not wo.asset.has_meter)
            },
            "service_links": wo.asset.service_info_links.select_related("provider"),
            "pinned_specs": wo.asset.specs.filter(is_pinned=True, is_sensitive=False)[:12],
            "part_usages": wo.part_usages.select_related("part", "stock_lot"),
            # What it still needs, against what it can actually have.
            "parts_needed": parts_needed,
            # Prefilled rather than left to be typed, because a block that
            # names the missing parts is the whole content of the note the
            # status already requires.
            "parts_blocked_reason": _shortfall_sentence(parts_needed),
            "vendors": Vendor.objects.order_by("name")[:100],
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



def _shortfall_sentence(parts_needed) -> str:
    """What is missing, as a sentence somebody would have written themselves."""
    shortfalls = parts_needed.shortfalls
    if not shortfalls:
        return ""
    return str(
        _("Waiting on %(parts)s.")
        % {"parts": ", ".join(f"{_trim(line.short)} × {line.part}" for line in shortfalls)}
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
        _warn_about_parts(request, wo, target)
    return redirect("work_order_detail", pk=wo.pk)


def _warn_about_parts(request, wo, target: str) -> None:
    """Starting work short of a part is allowed, and worth saying out loud.

    A warning rather than a refusal, for the same reason the tool check is
    (SPEC §8.7): the box may be in the truck, the store may be ten minutes
    away, and half the job may not need it. What is not defensible is letting
    somebody start and find out with the wheel already off.
    """
    if target != WorkOrderStatus.IN_PROGRESS:
        return
    shortfalls = parts_readiness.for_work_order(wo).shortfalls
    if not shortfalls:
        return
    messages.warning(
        request,
        _("Started, but short of %(parts)s. Nothing is stopping you.")
        % {"parts": ", ".join(str(line.part) for line in shortfalls[:4])},
    )


@require_POST
@login_required
def work_order_delete(request, pk):
    """Remove a work order, whatever state it is in.

    Deliberately not gated on status. The status graph governs *the work* —
    whether a job can be completed before it is started — and it has nothing
    useful to say about a record that should not exist at all. The ones most
    worth deleting are the half-finished ones somebody made while learning the
    application, and requiring those to be walked to `complete` first would
    mean firing every service completion attached to them on the way past.

    This is the ordinary soft delete (P-5): it goes to the 30-day trash and can
    be restored from there, so a mis-click costs nothing.
    """
    from homeautoshop.core.models import AuditLog

    wo = get_object_or_404(WorkOrder, pk=pk)
    children = wo.children.count()
    if children:
        # Deleting the parent would leave its children pointing at a row in the
        # trash — visible nowhere, restorable by nobody looking for them.
        messages.error(
            request,
            _(
                "%(n)s work order(s) sit under this one. Move or delete those first, "
                "so nothing is left pointing at a record that is gone."
            )
            % {"n": children},
        )
        return redirect("work_order_detail", pk=wo.pk)

    number, asset = wo.number, wo.asset
    wo.delete()
    AuditLog.objects.create(
        entity_type="WorkOrder",
        entity_id=wo.pk,
        action=AuditLog.Action.DELETE,
        user=request.user,
        summary=f"{number} — {wo.title}"[:255],
    )
    messages.success(
        request,
        _("%(n)s was deleted. It is in the trash for 30 days if you want it back.")
        % {"n": number},
    )
    return redirect("asset_detail", pk=asset.pk)


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
def part_require(request, pk):
    """Say this job is going to need a part (FR-WO-11).

    Deliberately not the same action as using one. `part_use` draws stock and
    is a fact about the past; this is a claim on the future, and the whole
    reason it exists is to be answerable *before* the wheel is off.
    """
    wo = get_object_or_404(WorkOrder, pk=pk)
    part = Part.objects.filter(pk=request.POST.get("part") or None).first()
    if part is None:
        messages.error(request, _("Choose a part first."))
        return redirect("work_order_detail", pk=wo.pk)

    job_item = JobItem.objects.filter(
        pk=request.POST.get("job_item") or None, work_order=wo
    ).first()

    try:
        qty = Decimal(str(request.POST.get("qty") or 1))
    except (InvalidOperation, TypeError):
        messages.error(request, _("Check the quantity and try again."))
        return redirect("work_order_detail", pk=wo.pk)

    # The same part named twice for the same line of work is one requirement
    # for more of it, not two rows saying the same thing.
    existing = PartRequirement.objects.filter(
        work_order=wo, part=part, job_item=job_item
    ).first()
    requirement = existing or PartRequirement(
        work_order=wo,
        part=part,
        job_item=job_item,
        qty=Decimal(0),
        origin=PartRequirement.origin_for(wo),
        created_by=request.user if getattr(request.user, "pk", None) else None,
    )
    requirement.qty = Decimal(str(requirement.qty)) + qty
    requirement.note = (request.POST.get("note") or requirement.note or "").strip()[:200]

    try:
        requirement.full_clean(exclude=["created_by"])
    except ValidationError as exc:
        messages.error(request, " ".join(exc.messages) or _("Check the quantity and try again."))
        return redirect("work_order_detail", pk=wo.pk)
    requirement.save()

    line = next(
        (
            candidate
            for candidate in parts_readiness.for_work_order(wo).lines
            if candidate.part.pk == part.pk
        ),
        None,
    )
    if line is not None and not line.is_ready:
        messages.warning(
            request,
            _("Added. %(n)s short — nothing on the shelf is free for this job.")
            % {"n": _trim(line.short)},
        )
    else:
        messages.success(request, _("Added to what this job needs."))
    return redirect("work_order_detail", pk=wo.pk)


@require_POST
@login_required
def part_unrequire(request, pk, requirement_id):
    """Decide the job does not need it after all."""
    wo = get_object_or_404(WorkOrder, pk=pk)
    requirement = get_object_or_404(PartRequirement, pk=requirement_id, work_order=wo)
    requirement.delete()
    messages.success(request, _("Removed from what this job needs."))
    return redirect("work_order_detail", pk=wo.pk)


def _trim(value) -> str:
    """`2` rather than `2.000`, which is how a person writes a count."""
    quantised = Decimal(str(value)).normalize()
    return f"{quantised:f}"


@require_POST
@login_required
def part_order_shortfall(request, pk):
    """Draft an order for everything this job is missing (FR-WO-2).

    Created as a **cart**, not an order: this application has not asked
    anybody for anything, and saying it had would clear the shortfall on the
    strength of a list. It also lands with no prices, because nobody knows
    them yet — which is what the purchase screen is for, and where this
    redirects to.

    A vendor is required because `Purchase` requires one, and that is right:
    an order with nobody to send it to is a shopping list, and the shop
    already has one of those.
    """
    from homeautoshop.purchasing.models import Purchase, PurchaseLine, PurchaseStatus, Vendor

    wo = get_object_or_404(WorkOrder, pk=pk)
    shortfalls = parts_readiness.for_work_order(wo).shortfalls
    if not shortfalls:
        messages.info(request, _("Nothing is missing, so there is nothing to order."))
        return redirect("work_order_detail", pk=wo.pk)

    vendor = Vendor.objects.filter(pk=request.POST.get("vendor") or None).first()
    if vendor is None:
        messages.error(request, _("Choose who to order from."))
        return redirect("work_order_detail", pk=wo.pk)

    with transaction.atomic():
        purchase = Purchase.objects.create(
            vendor=vendor,
            work_order=wo,
            status=PurchaseStatus.CART,
            created_by=request.user if getattr(request.user, "pk", None) else None,
        )
        PurchaseLine.objects.bulk_create(
            [
                PurchaseLine(
                    purchase=purchase,
                    part=line.part,
                    description_as_ordered=str(line.part)[:200],
                    qty_ordered=line.short,
                )
                for line in shortfalls
            ]
        )

    messages.success(
        request,
        _("Drafted %(n)d line(s) for %(vendor)s. Add prices and mark it ordered.")
        % {"n": len(shortfalls), "vendor": vendor.name},
    )
    return redirect("purchase_detail", pk=purchase.pk)


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


@login_required
def tool_search(request):
    """Find a tool by name instead of by remembering its id (FR-WL-3).

    An endpoint for this already existed and nothing ever called it: the form
    asked for a tool id and a name typed from memory, and its `list=` pointed
    at a datalist that was never rendered. Autocompletion promised in the
    markup, wired to nothing, with a working search sitting one URL away.

    This replaces that one rather than sitting beside it, and changes one thing
    about how it answers. The old one returned **either** WrenchLedger's
    results or the local cache — so with WrenchLedger reachable, a tool named
    here and not there was invisible. Both are searched, and merged.

    Two sources, in this order:

    * **Tools already referenced here**, matched locally. Instant, and it works
      with WrenchLedger unreachable or Offline Mode on — the tools somebody
      reaches for repeatedly are exactly the ones already cached.
    * **WrenchLedger's own search**, for everything else. Failure is not an
      error: the local results still stand, and the caller is told the remote
      half did not answer rather than being shown a silently short list.
    """
    query = (request.GET.get("q") or "").strip()
    if len(query) < 2:
        return JsonResponse({"results": [], "remote": False})

    found: dict[str, dict] = {}
    for tool in ShopTool.objects.filter(
        Q(name__icontains=query) | Q(tool_id__icontains=query) | Q(brand__icontains=query)
    )[:10]:
        found[tool.tool_id] = {
            "id": tool.tool_id,
            "name": tool.name or tool.tool_id,
            "detail": " ".join(x for x in (tool.brand, tool.model) if x),
            "known": True,
        }

    remote_ok = False
    if readiness.enabled():
        try:
            from homeautoshop.core.integrations.wrenchledger import WrenchLedgerClient

            for row in WrenchLedgerClient().search_tools(query)[:15]:
                tool_id = str(row.get("id") or row.get("tool_id") or "").strip()
                if not tool_id or tool_id in found:
                    continue
                found[tool_id] = {
                    "id": tool_id,
                    "name": str(row.get("name") or tool_id),
                    "detail": " ".join(
                        str(x) for x in (row.get("brand"), row.get("model")) if x
                    ),
                    "known": False,
                }
            remote_ok = True
        except Exception as exc:  # noqa: BLE001 - the local half still stands
            log.info("tool search could not reach WrenchLedger: %s", exc)

    return JsonResponse({"results": list(found.values())[:20], "remote": remote_ok})


@require_POST
@login_required
def job_item_tool_add(request, pk, item_id):
    """Point a job item at a tool. Stores the id, never a copy of the record."""
    wo = get_object_or_404(WorkOrder, pk=pk)
    item = get_object_or_404(JobItem, pk=item_id, work_order=wo)

    tool_id = (request.POST.get("tool_id") or "").strip()
    typed = (request.POST.get("tool_query") or "").strip()

    if not tool_id and typed:
        # No script, or a name typed and never picked from the list. Resolve it
        # against what is already known; an exact single match is unambiguous
        # and anything else is a question rather than a guess.
        matches = list(
            ShopTool.objects.filter(Q(name__iexact=typed) | Q(tool_id__iexact=typed))[:2]
        )
        if len(matches) == 1:
            tool_id = matches[0].tool_id
        elif len(matches) > 1:
            messages.warning(
                request, _("More than one tool is called that. Pick one from the list.")
            )
            return redirect("work_order_detail", pk=wo.pk)
        else:
            # Nothing known by that name. Recorded anyway under the typed text:
            # WrenchLedger is optional and never load-bearing (FR-WL-7), so a
            # tool named by hand has to keep working.
            tool_id = typed[:64]

    if not tool_id:
        messages.warning(request, _("Choose a tool first."))
        return redirect("work_order_detail", pk=wo.pk)

    tool, _created = ShopTool.objects.get_or_create(
        tool_id=tool_id[:64],
        defaults={"name": (request.POST.get("tool_name") or typed or "").strip()[:160]},
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
