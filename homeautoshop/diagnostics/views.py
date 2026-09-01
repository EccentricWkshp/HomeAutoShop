"""Diagnostics views (SPEC §8.3, §9.1 — the vehicle's Diagnostics tab)."""

from __future__ import annotations

import json
import logging

from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.translation import gettext as _
from django.views.decorators.http import require_GET, require_POST

from homeautoshop.accounts.policy import visible_assets, visible_assets_for

from homeautoshop.accounts.models import require
from homeautoshop.assets import service_info
from homeautoshop.assets import vin as vinlib
from homeautoshop.assets.models import Asset
from homeautoshop.work.models import WorkOrder

from . import dtc, engine, profiles as profilelib, services
from .models import (
    CodeDescription,
    CodeStatus,
    DiagnosticCode,
    DiagnosticSession,
    ParseStatus,
    ParserProfile,
    ReviewStatus,
    SessionSource,
)

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# The import queue and the review screen
# --------------------------------------------------------------------------


@login_required
def queue(request):
    """Drafts waiting to be looked at (§8.3a).

    Separate from vehicle history on purpose: a draft has been read by a
    machine and by nobody else, and until somebody confirms it, it is not part
    of what this application claims about the vehicle.
    """
    drafts = (
        visible_assets_for(
            request.user,
            DiagnosticSession.objects.filter(review_status=ReviewStatus.DRAFT),
        )
        .select_related("asset", "parser_profile")
        .order_by("-created_at")
    )
    return render(
        request,
        "diagnostics/queue.html",
        {
            "drafts": drafts,
            "profiles": ParserProfile.objects.all(),
            "assets": visible_assets(request.user),
        },
    )


@login_required
def asset_diagnostics(request, pk):
    """A vehicle's scan history, and what its codes are doing."""
    asset = get_object_or_404(Asset, pk=pk)
    require(request.user, "diagnostic.read", asset)
    sessions = (
        asset.diagnostic_sessions.filter(review_status=ReviewStatus.CONFIRMED)
        .prefetch_related("codes")
        .order_by("-performed_on")
    )
    drafts = asset.diagnostic_sessions.filter(review_status=ReviewStatus.DRAFT).order_by(
        "-created_at"
    )
    open_codes = (
        DiagnosticCode.objects.filter(
            session__asset=asset,
            session__review_status=ReviewStatus.CONFIRMED,
            status__in=[CodeStatus.OPEN, CodeStatus.RECURRING],
        )
        .select_related("session")
        .order_by("-session__performed_on", "code")
    )
    return render(
        request,
        "diagnostics/asset.html",
        {
            "asset": asset,
            "sessions": sessions,
            "drafts": drafts,
            "open_codes": open_codes,
            "manuals": service_info.dtc_links(asset),
        },
    )


@require_POST
@login_required
def session_import(request, pk):
    """Upload a report against a vehicle (FR-INT-4)."""
    asset = get_object_or_404(Asset, pk=pk)
    require(request.user, "asset.edit", asset)

    # Either control: the file picker, or the camera button beside it. A photo
    # of a printout is a report here — see engine.read.
    upload = request.FILES.get("report") or request.FILES.get("report_photo")
    if not upload:
        messages.warning(request, _("Choose a report or a photo to read."))
        return redirect("asset_diagnostics", pk=asset.pk)

    try:
        session = services.session_from_upload(asset, upload, user=request.user)
    except services.VinMismatch as mismatch:
        messages.error(
            request,
            _("That report is for VIN %(found)s, which is not this vehicle.")
            % {"found": vinlib.mask(mismatch.found)},
        )
        return redirect("asset_diagnostics", pk=asset.pk)
    except Exception as exc:  # noqa: BLE001 - surfaced, not swallowed
        log.exception("scan report import failed")
        messages.error(
            request,
            _("That file could not be read as a scan report: %(detail)s") % {"detail": exc},
        )
        return redirect("asset_diagnostics", pk=asset.pk)

    if session.parse_status == ParseStatus.UNMATCHED:
        messages.info(
            request,
            _("No profile recognized that report. Map the values by hand and it still counts."),
        )
        return redirect("session_map", pk=session.pk)

    messages.success(request, _("Read. Check it over before it joins the history."))
    return redirect("session_detail", pk=session.pk)


@login_required
def session_detail(request, pk):
    """The review screen: what was extracted, how sure, and from where.

    Per-field confidence is shown as a word rather than a bar. "Low" is
    actionable; "0.42" invites the reader to decide what 0.42 means, and they
    will decide wrong in whichever direction suits them.
    """
    session = get_object_or_404(
        DiagnosticSession.objects.select_related("asset", "parser_profile", "raw_media"), pk=pk
    )
    require(request.user, "diagnostic.read", session)
    rows = []
    for name, found in sorted((session.extraction or {}).items()):
        if name.startswith("_"):
            continue
        confidence = float(found.get("confidence") or 0)
        rows.append(
            {
                "name": name,
                "label": found.get("label") or name.replace("_", " "),
                "value": found.get("value", ""),
                # A field whose value is a mapping — the module identifiers —
                # is unreadable as raw JSON in a table cell, which is what it
                # looked like. Expanded here so the review screen shows what
                # was read rather than how it was stored.
                "pairs": _pairs(found.get("value", "")),
                "confidence": confidence,
                "band": _band(confidence),
            }
        )

    codes = list(session.codes.all())
    for code in codes:
        described, authoritative = dtc.describe(code.code, make=session.asset.make)
        code.lookup = described
        code.lookup_is_authoritative = authoritative

    return render(
        request,
        "diagnostics/session.html",
        {
            "session": session,
            "rows": rows,
            "codes": codes,
            "match": (session.extraction or {}).get("_match", {}),
            "profiles": ParserProfile.objects.filter(is_active=True),
            "manuals": service_info.dtc_links(session.asset),
            "form": SessionForm(instance=session),
        },
    )


def _pairs(value) -> list[tuple[str, str]]:
    """Read a JSON object out of an extracted value, or return nothing."""
    if not isinstance(value, str) or not value.startswith("{"):
        return []
    try:
        parsed = json.loads(value)
    except ValueError:
        return []
    if not isinstance(parsed, dict):
        return []
    return [(str(k), str(v)) for k, v in parsed.items() if str(v).strip()]


def _band(confidence: float) -> str:
    if confidence >= 0.85:
        return "high"
    if confidence >= 0.6:
        return "medium"
    return "low"


class SessionForm(forms.ModelForm):
    class Meta:
        model = DiagnosticSession
        fields = ["performed_on", "tool", "tool_model", "odometer", "odometer_unit", "notes"]
        widgets = {"notes": forms.Textarea(attrs={"rows": 2})}


@require_POST
@login_required
def session_confirm(request, pk):
    """Admit a draft to vehicle history (FR-INT-4)."""
    session = get_object_or_404(DiagnosticSession, pk=pk)
    require(request.user, "asset.edit", session.asset)

    if session.review_status == ReviewStatus.CONFIRMED:
        messages.info(request, _("That scan is already in the history."))
        return redirect("session_detail", pk=session.pk)

    form = SessionForm(request.POST, instance=session)
    if not form.is_valid():
        messages.error(request, _("Check the corrected values before confirming."))
        return redirect("session_detail", pk=session.pk)
    form.save()

    recurring = services.confirm(session, user=request.user)
    if recurring:
        messages.warning(
            request,
            _("%(n)d code(s) came back after being addressed — the fix did not hold.")
            % {"n": recurring},
        )
    messages.success(request, _("Added to the vehicle's history."))
    return redirect("asset_diagnostics", pk=session.asset_id)


@require_POST
@login_required
def session_discard(request, pk):
    session = get_object_or_404(DiagnosticSession, pk=pk)
    require(request.user, "asset.edit", session.asset)
    asset_id = session.asset_id
    session.delete()
    messages.success(request, _("Draft discarded. It is in the trash for 30 days."))
    return redirect("asset_diagnostics", pk=asset_id)


@require_POST
@login_required
def session_reparse(request, pk):
    """Read a stored report again (FR-INT-5).

    A confirmed session is copied to a new draft first. Re-parsing in place
    would rewrite a reading somebody already vouched for, and the whole reason
    the raw text is retained is to make a *comparison* possible.
    """
    session = get_object_or_404(DiagnosticSession, pk=pk)
    require(request.user, "asset.edit", session.asset)

    chosen = None
    if profile_id := request.POST.get("profile"):
        chosen = ParserProfile.objects.filter(pk=profile_id).first()

    target = session
    if session.review_status == ReviewStatus.CONFIRMED:
        target = DiagnosticSession.objects.create(
            asset=session.asset,
            work_order=session.work_order,
            performed_on=session.performed_on,
            source=session.source,
            raw_media=session.raw_media,
            extracted_text=session.extracted_text,
            extracted_words=session.extracted_words,
            notes=session.notes,
            created_by=request.user,
        )

    try:
        services.reparse(target, profile=chosen)
    except services.VinMismatch as mismatch:
        messages.error(
            request,
            _("That profile read VIN %(found)s out of the report, which is not this vehicle.")
            % {"found": vinlib.mask(mismatch.found)},
        )
        return redirect("session_detail", pk=target.pk)

    if target.parse_status == ParseStatus.UNMATCHED:
        messages.warning(request, _("Still no profile matches that report."))
    elif target.pk != session.pk:
        messages.success(request, _("Re-read as a new draft, so the original stays as it was."))
    else:
        messages.success(request, _("Re-read with %(p)s.") % {"p": target.parser_profile.label})
    return redirect("session_detail", pk=target.pk)


# --------------------------------------------------------------------------
# Manual mapping wizard (FR-INT-6)
# --------------------------------------------------------------------------


@login_required
def session_map(request, pk):
    """Make an unrecognized report usable, and optionally learn from it.

    This is what keeps the pipeline useful *before* a profile exists for a
    tool — which matters, because otherwise nobody ever gets far enough to
    write the first one.
    """
    session = get_object_or_404(DiagnosticSession.objects.select_related("asset"), pk=pk)
    require(request.user, "asset.edit", session.asset)

    header, rows = engine.rows_from_csv(session.extracted_text)
    guessed = engine.sniff_columns(header, rows) if header else {}

    if request.method == "POST":
        mapping = {
            role: request.POST.get(f"map_{role}", "").strip()
            for role in ("code", "description", "state", "module")
        }
        mapping = {role: column for role, column in mapping.items() if column}

        if header and mapping.get("code"):
            codes = engine.codes_from_mapping(rows, mapping)
        else:
            codes = _codes_from_text(request.POST.get("codes", ""))

        if not codes:
            messages.error(
                request,
                _("Nothing in that mapping looks like a trouble code. Check the column choice."),
            )
            return redirect("session_map", pk=session.pk)

        services._replace_codes(session, codes, make=session.asset.make)
        session.parse_status = ParseStatus.PARSED
        session.tool = request.POST.get("tool", "")[:60] or session.tool
        session.save(update_fields=["parse_status", "tool"])

        if request.POST.get("save_profile") and header and mapping.get("code"):
            _profile_from_mapping(request, session, header, mapping)

        messages.success(request, _("Mapped. Check it over before it joins the history."))
        return redirect("session_detail", pk=session.pk)

    # The choices are assembled here rather than in the template: which column
    # is pre-selected is a comparison, and a template that can only test for
    # truth ends up either silently selecting nothing or growing a filter.
    roles = [
        {
            "role": role,
            "label": label,
            "columns": [
                {"name": column, "selected": guessed.get(role) == column} for column in header
            ],
        }
        for role, label in (
            ("code", _("Code")),
            ("description", _("Description")),
            ("state", _("State")),
            ("module", _("Module")),
        )
    ]
    return render(
        request,
        "diagnostics/map.html",
        {
            "session": session,
            "header": header,
            "roles": roles,
            "sample": [[row.get(column, "") for column in header] for row in rows[:5]],
            "excerpt": session.extracted_text[:4000],
        },
    )


def _codes_from_text(blob: str) -> list[dict]:
    """Take codes out of whatever the operator pasted.

    Deliberately forgiving about separators and deliberately strict about what
    counts as a code: anything J2012 does not recognize is dropped rather than
    stored as a code nobody can look up.
    """
    found = []
    for chunk in (blob or "").replace(",", "\n").splitlines():
        parts = chunk.strip().split(None, 1)
        if not parts:
            continue
        if not dtc.parse(parts[0]):
            continue
        found.append(
            {
                "code": dtc.normalize(parts[0]),
                "description": parts[1].strip() if len(parts) > 1 else "",
            }
        )
    return found


def _profile_from_mapping(request, session, header: list[str], mapping: dict) -> None:
    """Learn-from-example: save the operator's mapping as a reusable profile."""
    name = request.POST.get("profile_name", "").strip() or _("%(tool)s import") % {
        "tool": session.tool or _("Untitled tool")
    }
    if ParserProfile.all_objects.filter(name=name, version=1).exists():
        messages.info(
            request, _("A profile called %(name)s already exists, so none was saved.") % {"name": name}
        )
        return
    ParserProfile.objects.create(
        name=name[:120],
        tool_vendor=(session.tool or "")[:60],
        media_type="csv",
        source="user",
        fingerprint={
            "threshold": 0.6,
            # The header row is the fingerprint. It is what actually
            # distinguishes one tool's export from another's, and it is the one
            # signal the operator has already confirmed by mapping it.
            "signals": [
                {"kind": "doc_text", "pattern": _escape(column), "weight": 1.0 / len(header)}
                for column in header
                if column.strip()
            ],
        },
        table_extractor={
            "row_pattern": r"([PBCU][0-9A-F]{4}(?:-[0-9A-F]{2})?)[\s,:.-]+(.*)",
            "columns": [
                {"role": "code", "group": 1, "validate": "dtc_format"},
                {"role": "description", "group": 2},
            ],
        },
        notes=str(
            _("Learned from a mapping on %(when)s. Columns: %(cols)s")
            % {"when": session.created_at.date().isoformat(), "cols": ", ".join(mapping.values())}
        ),
        created_by=request.user,
    )
    messages.success(request, _("Saved as a profile, so the next one of these reads itself."))


def _escape(value: str) -> str:
    import re as _re

    return _re.escape(value.strip())


# --------------------------------------------------------------------------
# Codes
# --------------------------------------------------------------------------


@require_POST
@login_required
def code_promote(request, pk):
    """Code → work order (§8.3c). The reason for reading codes at all."""
    code = get_object_or_404(DiagnosticCode.objects.select_related("session__asset"), pk=pk)
    require(request.user, "asset.edit", code.session.asset)

    if code.session.review_status != ReviewStatus.CONFIRMED:
        messages.error(request, _("Confirm the scan first — a draft is not history yet."))
        return redirect("session_detail", pk=code.session_id)

    existing = None
    if wanted := request.POST.get("work_order"):
        existing = WorkOrder.objects.filter(pk=wanted, asset=code.session.asset).first()

    work_order = services.promote_to_work_order(code, user=request.user, work_order=existing)
    messages.success(
        request,
        _("%(code)s is now on %(number)s.") % {"code": code.code, "number": work_order.number},
    )
    return redirect("work_order_detail", pk=work_order.pk)


@require_POST
@login_required
def code_status(request, pk):
    """Record the operator's verdict on a code — the one mutable field."""
    code = get_object_or_404(DiagnosticCode.objects.select_related("session__asset"), pk=pk)
    require(request.user, "asset.edit", code.session.asset)

    wanted = request.POST.get("status", "")
    if wanted not in CodeStatus.values:
        messages.error(request, _("That is not a status a code can have."))
        return redirect("asset_diagnostics", pk=code.session.asset_id)

    code.status = wanted
    code.save(update_fields=["status"])
    messages.success(
        request,
        _("%(code)s marked %(status)s.")
        % {"code": code.code, "status": code.get_status_display()},
    )
    return redirect("asset_diagnostics", pk=code.session.asset_id)


@require_POST
@login_required
def code_describe(request, pk):
    """Name a manufacturer-specific code once, for every vehicle of that make.

    Scoped by make because `P1345` is a different fault to GM and to Toyota,
    and a dictionary that pretends otherwise is worse than an empty one.
    """
    code = get_object_or_404(DiagnosticCode.objects.select_related("session__asset"), pk=pk)
    require(request.user, "asset.edit", code.session.asset)

    text = request.POST.get("description", "").strip()[:255]
    if not text:
        messages.warning(request, _("Nothing to record."))
        return redirect("session_detail", pk=code.session_id)

    make = code.session.asset.make or ""
    CodeDescription.objects.update_or_create(
        make=make, code=code.code, defaults={"description": text}
    )
    DiagnosticCode.objects.filter(
        code=code.code, session__asset__make__iexact=make, description=""
    ).update(description=text)
    code.description = text
    code.save(update_fields=["description"])
    messages.success(
        request,
        _("Recorded. Every %(make)s in the shop will show that for %(code)s.")
        % {"make": make or _("vehicle"), "code": code.code},
    )
    return redirect("session_detail", pk=code.session_id)


# --------------------------------------------------------------------------
# ELM327 (§8.3c) — the browser talks to the adapter, not the server
# --------------------------------------------------------------------------


@login_required
def elm327(request, pk):
    """The live-read screen.

    The container has no USB and no Bluetooth, and the phone in the garage is
    the device actually near the car — so the *browser* drives the adapter and
    posts what it read. That also makes the requirements concrete rather than
    mysterious: a secure context and a Chromium browser, both stated on the
    page rather than silently graying out a button.
    """
    from django.middleware.csrf import get_token

    asset = get_object_or_404(Asset, pk=pk)
    return render(
        request,
        "diagnostics/elm327.html",
        {
            "asset": asset,
            # Handed to the script as JSON rather than interpolated into it, so
            # the strings stay in the message catalog (§5.6) and the script
            # itself is a static file that caches.
            "config": {
                "captureUrl": reverse("elm_capture", args=[asset.pk]),
                "csrf": get_token(request),
                "baudRate": 38400,
            },
            "strings": {
                "ready": _("Ready. Plug the adapter in and press Read codes."),
                "insecure": _(
                    "This page is not on HTTPS, so the browser will not open a serial port. "
                    "See the install guide for turning on TLS."
                ),
                "noSerial": _(
                    "This browser has no Web Serial. Chrome or Edge on desktop or Android "
                    "will do it; Safari and Firefox will not."
                ),
                "notConnected": _("No adapter was chosen."),
                "connected": _("Connected. Reading…"),
                "stored": _("stored"),
                "pending": _("pending"),
                "permanent": _("permanent"),
                "noCodes": _("Read complete — no codes found."),
                "done": _("Read. Keep it and check it over."),
                "readFailed": _("The adapter stopped answering."),
                "saveFailed": _("Could not save that read."),
                "cleared": _("Codes cleared. Readiness monitors are now unset."),
                "clearWarning": _(
                    "Clear all codes on %(vehicle)s?\n\n"
                    "This also resets the readiness monitors, and a car with unset monitors "
                    "fails an emissions test until it has been driven enough to set them again."
                )
                % {"vehicle": asset.nickname},
            },
        },
    )


@require_POST
@login_required
def elm_capture(request, pk):
    """Record what the browser read off the adapter.

    Lands in the same draft queue as a PDF. A $12 dongle and a bad connector
    misread codes at least as often as a PDF parser does.
    """
    asset = get_object_or_404(Asset, pk=pk)
    require(request.user, "asset.edit", asset)

    try:
        payload = json.loads(request.body.decode("utf-8") or "{}")
    except ValueError:
        return JsonResponse({"error": "malformed"}, status=400)

    rows = [
        {"code": entry.get("code", ""), "state": entry.get("state", "stored")}
        for entry in payload.get("codes") or []
        if isinstance(entry, dict)
    ]
    session = services.session_from_codes(
        asset,
        rows,
        user=request.user,
        source=SessionSource.ELM327,
        tool=str(payload.get("adapter", "ELM327"))[:60],
        odometer=payload.get("odometer"),
    )
    session.readiness_monitors = payload.get("readiness") or {}
    session.live_data = payload.get("live_data") or []
    session.save(update_fields=["readiness_monitors", "live_data"])

    return JsonResponse(
        {
            "session": str(session.pk),
            "url": f"/diagnostics/sessions/{session.pk}/",
            "codes": session.codes.count(),
        }
    )


# --------------------------------------------------------------------------
# Profiles (FR-INT-7)
# --------------------------------------------------------------------------


@login_required
def profile_list(request):
    """Kept as an address, not as a page.

    Parser profiles used to have a screen of their own here, reachable only
    from the scan queue — which meant the one page listing everything a shop
    imports, exports and shares listed two of the three kinds, while its own
    copy already said it covered "scan-tool profiles". A profile is the same
    sort of thing as a schedule and a checklist: YAML, an author, a source,
    installed from the same catalog by the same validator. It belongs beside
    them, so it is now listed there.

    This redirect stays because the URL was linked and bookmarkable, and a
    dead link is a worse answer than a hop.
    """
    require(request.user, "integration.manage")
    return redirect(reverse("template_list") + "#profiles")


@require_GET
@login_required
def profile_export(request, pk):
    profile = get_object_or_404(ParserProfile, pk=pk)
    body = profilelib.to_yaml(profile)
    response = HttpResponse(body, content_type="application/yaml; charset=utf-8")
    slug = profile.name.lower().replace(" ", "-").replace("/", "-")
    response["Content-Disposition"] = f'attachment; filename="{slug}-v{profile.version}.yaml"'
    return response


@require_POST
@login_required
def profile_import(request):
    require(request.user, "integration.manage")
    from homeautoshop.core.imports import NothingToImport, text_from

    try:
        text = text_from(request, "profile")
    except NothingToImport as exc:
        messages.warning(request, str(exc))
        return redirect("template_list")

    try:
        profile = profilelib.from_yaml(text)
    except profilelib.ProfileInvalid as exc:
        messages.error(request, _("That profile was refused: %(detail)s") % {"detail": exc})
        return redirect("template_list")

    if ParserProfile.all_objects.filter(name=profile.name, version=profile.version).exists():
        messages.error(
            request,
            _("%(name)s v%(v)d is already here. Bump the version to import a revision.")
            % {"name": profile.name, "v": profile.version},
        )
        return redirect("template_list")

    profile.created_by = request.user
    profile.save()
    messages.success(request, _("Imported %(name)s.") % {"name": profile.label})
    return redirect("template_list")


@require_POST
@login_required
def profile_delete(request, pk):
    """Remove a parser profile (FR-INT-7).

    Switching one off was the only way to say no to it, which is the same gap
    a scheduled item had: a profile that is wrong, superseded or simply for a
    tool nobody here owns stayed in the list forever, and the catalog could
    install one that nothing could take away.

    Nothing that was already read is disturbed. `DiagnosticSession` points at
    its profile with `SET_NULL` and keeps `parser_version` in a column of its
    own, so a session goes on saying which version read it after the profile
    is gone. Soft, like every other delete here, so it is in the trash for
    thirty days — and a shipped profile can also be put back at any time from
    **Restore shipped templates**, which matters because removing the built-in
    that reads XTOOL D8 reports would otherwise be a one-way door.
    """
    require(request.user, "integration.manage")
    profile = get_object_or_404(ParserProfile, pk=pk)
    name = profile.label
    profile.delete()
    messages.success(
        request,
        _("Removed %(name)s. Scans already read with it keep their results.")
        % {"name": name},
    )
    return redirect(request.POST.get("back") or "template_list")


@require_POST
@login_required
def profile_toggle(request, pk):
    require(request.user, "integration.manage")
    profile = get_object_or_404(ParserProfile, pk=pk)
    profile.is_active = not profile.is_active
    profile.save(update_fields=["is_active"])
    messages.success(
        request,
        _("%(name)s is now %(state)s.")
        % {
            "name": profile.label,
            "state": _("in use") if profile.is_active else _("switched off"),
        },
    )
    return redirect("template_list")
