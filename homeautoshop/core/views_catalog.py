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
from homeautoshop.diagnostics import codelistlib
from homeautoshop.diagnostics.models import InstalledCodeList, ParserProfile
from homeautoshop.inspections import templatelib as checklistlib
from homeautoshop.inspections.models import InspectionTemplate
from homeautoshop.maintenance import templatelib
from homeautoshop.maintenance.models import ScheduleTemplate

from . import catalog as catalog_lib
from .imports import NothingToImport, text_from


@login_required
def template_list(request):
    """Everything this shop imports, exports and shares, and where each came from.

    Four kinds together, because they are the same kind of thing: content with
    an author and a source, installed from the same catalog through the same
    validator, and removable without touching what was made from it. Parser
    profiles used to sit on a page of their own under the scan queue,
    reachable only by somebody already looking at scans — and this page's own
    copy said it covered "scan-tool profiles" while listing none. A feature you
    can only find from the screen you already know about is a feature most
    people never find.

    Manufacturer code lists are here for that reason rather than as a fifth
    thing to scroll past. Installed, they were visible only on the catalog
    browse screen, so the shop had no answer to "which makes do we have, and
    at what version" without going back out to the network.
    """
    require(request.user, "settings.manage")
    return render(
        request,
        "core/templates.html",
        {
            "templates": ScheduleTemplate.objects.all().prefetch_related("items"),
            "checklists": InspectionTemplate.objects.all().prefetch_related("points"),
            "profiles": ParserProfile.objects.all(),
            "code_lists": InstalledCodeList.objects.all(),
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
    if isinstance(installed, InstalledCodeList):
        # No "apply it to a vehicle" here: a code list is a dictionary, not a
        # template. It starts answering lookups the moment it is installed,
        # and saying how many definitions arrived is what tells somebody the
        # install did anything at all.
        messages.success(
            request,
            _("Installed %(name)s — %(n)d definitions.")
            % {"name": name, "n": installed.code_count},
        )
    elif isinstance(installed, ParserProfile):
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


def _removed(request, template, kind: str):
    """Soft-delete a template and say what that did and did not touch.

    Applying a template **materializes** it: the schedule items land on the
    vehicle as rows of their own, pointing at service definitions rather than
    at the template. So removing a template changes nothing that is already
    scheduled, and saying so is the difference between a button somebody
    presses and one they hover over and leave alone.

    Soft, like every other delete here, so it is in the trash for thirty days.
    """
    name = template.name
    template.delete()
    messages.success(
        request,
        _("Removed %(name)s. Anything already scheduled from it is untouched.")
        % {"name": name}
        if kind == "schedule"
        else _("Removed %(name)s. Inspections already done with it are untouched.")
        % {"name": name},
    )


@require_POST
@login_required
def template_delete(request, pk):
    """Remove a service schedule (FR-MAINT-11).

    Built-in templates may go too. An operator who does not run diesels should
    not have to scroll past a diesel schedule forever, and the seeders leave a
    deleted template deleted rather than restoring it on the next boot.
    """
    require(request.user, "settings.manage")
    _removed(request, get_object_or_404(ScheduleTemplate, pk=pk), "schedule")
    return redirect(request.POST.get("back") or "template_list")


@require_POST
@login_required
def checklist_delete(request, pk):
    """Remove an inspection checklist (FR-DVI-13).

    An inspection snapshots its points when it starts (FR-DVI-6), so past
    inspections keep rendering exactly as they were performed.
    """
    require(request.user, "settings.manage")
    _removed(request, get_object_or_404(InspectionTemplate, pk=pk), "checklist")
    return redirect(request.POST.get("back") or "template_list")


@require_POST
@login_required
def codelist_import(request):
    """Install a manufacturer code list from a file (§17 R-1, P-1).

    The catalog is how most shops will get one, and it needs an address it can
    reach. P-1 says an instance that reaches nothing must still work — and
    before the lists were published rather than bundled, offline meant *no
    worse*, because Ford's list was in the image. It no longer is. So the way
    in that does not involve the network is built rather than assumed:
    download the file on a machine that has a connection, carry it over.

    Through `codelistlib.load`, which is the same validator the catalog install
    calls. That equivalence is the trust model rather than a tidiness point: a
    catalog file is trusted exactly as far as a file a stranger emailed, and
    that only stays true while both take the same road.
    """
    require(request.user, "settings.manage")
    try:
        text = text_from(request, "codelist")
    except NothingToImport as exc:
        messages.warning(request, str(exc))
        return redirect("template_list")

    try:
        held = codelistlib.load(text, user=request.user)
    except codelistlib.CodeListInvalid as exc:
        messages.error(
            request, _("That code list was refused: %(detail)s") % {"detail": exc}
        )
        return redirect("template_list")

    messages.success(
        request,
        _("Installed the %(name)s code list — %(n)d definitions.")
        % {"name": held.name, "n": held.code_count},
    )
    return redirect("template_list")


@require_POST
@login_required
def codelist_delete(request, pk):
    """Remove an installed manufacturer code list.

    Nothing that has already been read changes. A code recorded on a session
    keeps the wording the scan tool printed and whatever the shop wrote down;
    what goes away is this make's published document, so a lookup falls back
    to the ISO/SAE standard and to the shop's own notes — which is exactly
    where it stood before anybody installed it.
    """
    require(request.user, "settings.manage")
    held = get_object_or_404(InstalledCodeList, pk=pk)
    name = held.name
    held.delete()
    messages.success(
        request,
        _("Removed the %(name)s code list. Readings already recorded are untouched.")
        % {"name": name},
    )
    return redirect(request.POST.get("back") or "template_list")


@require_POST
@login_required
def restore_builtins(request):
    """Put the shipped templates back (FR-MAINT-11, FR-DVI-13).

    The built-ins live in the image, so they are never actually lost — but
    until this existed there was no way to say so. A removed built-in went to
    the trash, and once thirty days had passed it was gone with nothing in the
    catalog to replace it, because the catalog deliberately publishes nothing
    that duplicates what ships.

    Separate from what happens at boot on purpose. Seeding leaves a deleted
    template deleted, because an operator who removed the diesel schedule
    meant it and should not have to remove it again after every restart. This
    is the same person changing their mind, which is a different intent and
    deserves its own deliberate act rather than being inferred.
    """
    require(request.user, "settings.manage")

    from homeautoshop.diagnostics import profiles as profile_seed
    from homeautoshop.inspections import seed as checklist_seed
    from homeautoshop.maintenance import seed as schedule_seed

    def count():
        # Parser profiles are counted here for the same reason they are listed
        # on this page: removing the built-in that reads XTOOL D8 reports is
        # exactly as one-way as removing the diesel schedule, and the button
        # that undoes one should undo the other.
        return (
            ScheduleTemplate.objects.count()
            + InspectionTemplate.objects.count()
            + ParserProfile.objects.count()
        )

    before = count()
    schedule_seed.install(revive=True)
    checklist_seed.install(revive=True)
    profile_seed.seed(revive=True)
    restored = count() - before

    messages.success(
        request,
        _("Put back %(n)d shipped item(s). Nothing you wrote was touched.")
        % {"n": restored}
        if restored
        else _("Everything shipped was already here."),
    )
    return redirect("template_list")
