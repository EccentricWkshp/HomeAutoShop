"""Fluid analysis screens (SPEC §7.9a, FR-FLU-*)."""

from __future__ import annotations

from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.translation import gettext as _
from django.utils.translation import ngettext
from django.views.decorators.http import require_POST

from homeautoshop.accounts.models import require
from homeautoshop.assets.models import Asset
from homeautoshop.mediafiles.models import MediaLink
from homeautoshop.mediafiles.services import ingest

from .models import Compartment, FluidSample
from .services import parse_results, samples_for, save_results, series, trends


class SampleForm(forms.ModelForm):
    """The sample, and the panel as one pasted block.

    The paste box is not a shortcut — it is the difference between a feature
    that gets used and one that gets used once. A lab panel is thirty numbers,
    and thirty inputs is a form somebody fills in for the first sample and
    never for the fourth, which is the only one that would have shown a trend.
    """

    results_text = forms.CharField(
        label=_("Results"),
        required=False,
        widget=forms.Textarea(attrs={"rows": 12, "class": "input textarea", "spellcheck": "false"}),
        help_text=_(
            "Paste the lab's table — one line each, like “Iron 24” or "
            "“Viscosity @ 100C 10.9”. Anything that cannot be read is listed "
            "back to you rather than dropped."
        ),
    )

    class Meta:
        model = FluidSample
        fields = [
            "compartment", "position", "sampled_on", "usage_at_sample",
            "fluid_usage", "fluid_changed", "lab", "report_number",
            "fluid_brand", "fluid_grade", "work_order", "lab_comment", "notes",
        ]
        widgets = {
            "sampled_on": forms.DateInput(attrs={"type": "date"}),
            "lab_comment": forms.Textarea(attrs={"rows": 4}),
            "notes": forms.Textarea(attrs={"rows": 2}),
        }

    def __init__(self, *args, asset=None, **kwargs):
        super().__init__(*args, **kwargs)
        if asset is not None:
            self.fields["work_order"].queryset = asset.work_orders.all()
        self.fields["work_order"].empty_label = _("Not against a job")
        for name, field in self.fields.items():
            field.required = name == "compartment"
            css = "select" if isinstance(field.widget, forms.Select) else "input"
            if isinstance(field.widget, forms.Textarea):
                css = "input textarea"
            if isinstance(field.widget, forms.CheckboxInput):
                css = ""
            if css:
                field.widget.attrs.setdefault("class", css)


def _vehicle(request, pk, action="fluid.read"):
    asset = get_object_or_404(Asset, pk=pk)
    require(request.user, action, asset)
    return asset


def _sample(request, pk, action="fluid.read") -> FluidSample:
    sample = get_object_or_404(FluidSample.objects.select_related("asset"), pk=pk)
    require(request.user, action, sample.asset)
    return sample


@login_required
def fluid_list(request, pk):
    """Every sample for one vehicle, and the trend for one compartment."""
    asset = _vehicle(request, pk)
    grouped = series(asset)

    # Which series to trend. Defaults to the one sampled most recently, which
    # is nearly always the one somebody just came here to look at.
    chosen = request.GET.get("compartment") or ""
    position = request.GET.get("position") or ""
    if not chosen and grouped:
        chosen, position = grouped[0][0]

    return render(
        request,
        "fluids/list.html",
        {
            "asset": asset,
            "series": grouped,
            "compartment": chosen,
            "position": position,
            "compartment_label": dict(Compartment.choices).get(chosen, chosen),
            "trends": trends(asset, compartment=chosen, position=position) if chosen else [],
            # Oldest first is what a trend is computed from; newest first is
            # what somebody reads. One query, reversed here rather than
            # ordered twice.
            "samples": list(samples_for(asset, compartment=chosen, position=position))[::-1]
            if chosen
            else [],
        },
    )


@login_required
def fluid_sample_detail(request, pk):
    sample = _sample(request, pk)
    results = list(sample.results.all())
    grouped: dict[str, list] = {}
    for result in results:
        grouped.setdefault(result.kind, []).append(result)

    from . import analytes

    return render(
        request,
        "fluids/detail.html",
        {
            "asset": sample.asset,
            "sample": sample,
            # The report itself. The numbers are a transcription of it, and a
            # transcription with no route back to the page it came from cannot
            # be checked — which is the whole reason the lists in this
            # application cite their sources (§8.3c).
            "documents": MediaLink.for_entity(sample).select_related("media"),
            "sections": [
                (analytes.KIND_LABELS[kind], grouped[kind])
                for kind in analytes.KIND_ORDER
                if kind in grouped
            ],
        },
    )


@login_required
def fluid_sample_create(request, pk):
    asset = _vehicle(request, pk, "fluid.edit")
    form = SampleForm(request.POST or None, asset=asset)
    unreadable: list = []

    if request.method == "POST" and form.is_valid():
        sample = form.save(commit=False)
        sample.asset = asset
        sample.save()
        lines = parse_results(form.cleaned_data.get("results_text", ""))
        saved = save_results(sample, lines)
        unreadable = [line for line in lines if not line.ok]
        _report(request, saved, unreadable)
        return redirect("fluid_sample_detail", pk=sample.pk)

    return render(
        request,
        "fluids/form.html",
        {"asset": asset, "form": form, "unreadable": unreadable},
    )


@login_required
def fluid_sample_edit(request, pk):
    sample = _sample(request, pk, "fluid.edit")
    form = SampleForm(request.POST or None, instance=sample, asset=sample.asset)
    unreadable: list = []

    if request.method == "POST" and form.is_valid():
        form.save()
        pasted = form.cleaned_data.get("results_text", "").strip()
        # An empty box means "I am only editing the header", never "delete the
        # panel". Clearing results is a deliberate act, not an oversight.
        if pasted:
            lines = parse_results(pasted)
            saved = save_results(sample, lines)
            unreadable = [line for line in lines if not line.ok]
            _report(request, saved, unreadable)
        return redirect("fluid_sample_detail", pk=sample.pk)

    return render(
        request,
        "fluids/form.html",
        {"asset": sample.asset, "sample": sample, "form": form, "unreadable": unreadable},
    )


@login_required
@require_POST
def fluid_sample_report(request, pk):
    """Attach the lab's own report to the sample (FR-DOC-1, FR-FLU-7).

    On the sample rather than loose on the vehicle, which is where the first
    version of this told people to put it. A PDF filed against the truck is
    findable only by remembering which of eleven documents belongs to the
    March sample; filed against the sample it is one tap from the numbers
    somebody is doubting.

    The same `ingest` every other attachment uses, so a photographed printout
    lands as a photo and a PDF lands as a document without this view deciding
    anything (FR-DOC-10).
    """
    sample = _sample(request, pk, "fluid.edit")
    files = request.FILES.getlist("files")
    if not files:
        messages.warning(request, _("Choose a file first, then Upload."))
        return redirect("fluid_sample_detail", pk=sample.pk)

    created = 0
    for upload in files:
        _media, was_new = ingest(
            upload, user=request.user, entity=sample, role=MediaLink.Role.OTHER
        )
        created += was_new
    messages.success(
        request,
        ngettext("%(n)s file attached.", "%(n)s files attached.", created)
        % {"n": created}
        if created
        else _("That file is already on this sample."),
    )
    return redirect("fluid_sample_detail", pk=sample.pk)


@login_required
@require_POST
def fluid_sample_delete(request, pk):
    sample = _sample(request, pk, "fluid.edit")
    asset_id = sample.asset_id
    sample.delete()
    messages.success(request, _("Sample removed."))
    return redirect("fluid_list", pk=asset_id)


def _report(request, saved: int, unreadable: list) -> None:
    """Say what landed and, more importantly, what did not."""
    if saved:
        messages.success(
            request,
            ngettext("%(n)s result recorded.", "%(n)s results recorded.", saved)
            % {"n": saved},
        )
    for line in unreadable:
        messages.warning(
            request,
            _("Not recorded — %(why)s: %(line)s")
            % {"why": line.problem, "line": line.raw[:120]},
        )
