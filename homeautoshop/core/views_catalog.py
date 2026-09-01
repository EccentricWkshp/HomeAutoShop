"""Sharing templates: by file, and through the published catalog (R-1).

Two ways in, one validator. A schedule template that arrives as an upload and
one pulled from a repository go through the identical `templatelib` checks —
that is the trust model, and keeping both paths in one module is what stops a
convenience being added to one of them later.
"""

from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST

from homeautoshop.accounts.models import require
from homeautoshop.diagnostics.models import ParserProfile
from homeautoshop.inspections import templatelib as checklistlib
from homeautoshop.inspections.models import InspectionTemplate
from homeautoshop.maintenance import templatelib
from homeautoshop.maintenance.models import ScheduleTemplate

from . import catalog as catalog_lib
from .imports import NothingToImport, text_from


@login_required
def template_list(request):
    """The schedules this shop knows about, and where each came from."""
    require(request.user, "settings.manage")
    return render(
        request,
        "core/templates.html",
        {
            "templates": ScheduleTemplate.objects.all().prefetch_related("items"),
            "checklists": InspectionTemplate.objects.all().prefetch_related("points"),
            "catalog_configured": catalog_lib.is_configured(),
        },
    )


@login_required
def template_export(request, pk):
    """One schedule as a file somebody else can read (§8, FR-MAINT-11)."""
    require(request.user, "settings.manage")
    template = get_object_or_404(ScheduleTemplate, pk=pk)
    body = templatelib.to_yaml(template)
    response = HttpResponse(body, content_type="application/yaml; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="{template.slug}.yaml"'
    return response


@require_POST
@login_required
def template_import(request):
    require(request.user, "settings.manage")
    try:
        text = text_from(request, "template")
    except NothingToImport as exc:
        messages.warning(request, str(exc))
        return redirect("template_list")

    try:
        template = templatelib.load(text)
    except templatelib.TemplateInvalid as exc:
        messages.error(
            request, _("That template was refused: %(detail)s") % {"detail": exc}
        )
        return redirect("template_list")

    messages.success(
        request,
        _("Imported %(name)s. Apply it to a vehicle when you have looked it over.")
        % {"name": template.name},
    )
    return redirect("template_list")


@login_required
def catalog_browse(request):
    """What other people have published (R-1).

    Fetched only because somebody opened this page or pressed refresh. There is
    no background check and nothing on start-up — an instance that never opens
    this screen never talks to the catalog at all.
    """
    require(request.user, "settings.manage")
    context: dict = {
        "configured": catalog_lib.is_configured(),
        "source": catalog_lib.base_url(),
    }
    if context["configured"]:
        try:
            context["catalog"] = catalog_lib.index(
                force=request.GET.get("refresh") == "1", user=request.user
            )
        except catalog_lib.CatalogUnavailable as exc:
            # Never an error page. The catalog is additive by design, so
            # failing to reach it is a sentence on an otherwise working screen.
            context["problem"] = str(exc)
    return render(request, "core/catalog.html", context)


@require_POST
@login_required
def catalog_install(request):
    """Bring one published entry in, through the ordinary import path."""
    require(request.user, "settings.manage")
    kind = request.POST.get("kind") or ""
    path = request.POST.get("path") or ""

    try:
        published = catalog_lib.index(user=request.user)
    except catalog_lib.CatalogUnavailable as exc:
        messages.error(request, str(exc))
        return redirect("catalog_browse")

    # Matched against the published index rather than trusted from the form:
    # otherwise the path is an operator-supplied URL fragment and this becomes
    # a fetch-anything button wearing an install button's clothes.
    entry = next(
        (e for e in published.entries if e.path == path and e.kind == kind), None
    )
    if entry is None:
        raise Http404("no such catalog entry")

    try:
        installed = catalog_lib.install(entry, user=request.user)
    except catalog_lib.CatalogUnavailable as exc:
        messages.error(request, str(exc))
        return redirect("catalog_browse")
    except (templatelib.TemplateInvalid, ValueError) as exc:
        # The same refusal an uploaded file would get, said the same way. A
        # catalog file that fails validation is not a special kind of
        # problem, and treating it as one is how a special path gets added.
        messages.error(
            request, _("That entry was refused: %(detail)s") % {"detail": exc}
        )
        return redirect("catalog_browse")

    name = getattr(installed, "name", "")
    if isinstance(installed, ParserProfile):
        messages.success(request, _("Installed %(name)s.") % {"name": name})
    else:
        messages.success(
            request,
            _("Installed %(name)s. Nothing is scheduled until you apply it to a vehicle.")
            % {"name": name},
        )
    return redirect("catalog_browse")


@login_required
def checklist_export(request, pk):
    """One inspection checklist as a file (FR-DVI-13)."""
    require(request.user, "settings.manage")
    template = get_object_or_404(InspectionTemplate, pk=pk)
    body = checklistlib.to_yaml(template)
    response = HttpResponse(body, content_type="application/yaml; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="{template.slug}.yaml"'
    return response


@require_POST
@login_required
def checklist_import(request):
    require(request.user, "settings.manage")
    try:
        text = text_from(request, "checklist")
    except NothingToImport as exc:
        messages.warning(request, str(exc))
        return redirect("template_list")

    try:
        template = checklistlib.load(text)
    except checklistlib.TemplateInvalid as exc:
        messages.error(
            request, _("That checklist was refused: %(detail)s") % {"detail": exc}
        )
        return redirect("template_list")

    messages.success(
        request, _("Imported %(name)s.") % {"name": template.name}
    )
    return redirect("template_list")
