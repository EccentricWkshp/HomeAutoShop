"""
Integration settings and activity (SPEC FR-INT-1/2/3, FR-ADM-3, FR-WL-1).

One screen per the spec's rule: every integration is individually enableable,
testable with a **real** connectivity check rather than a green dot that means
"a key is present", and disabled by default except the free keyless NHTSA
services.
"""

from __future__ import annotations

import logging

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect, render
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST

from .runtime import conf
from homeautoshop.accounts.models import require

from .integrations import sync as lubelogger_sync
from .integrations import wrenchledger as wl
from .models import AuditLog, Setting
from homeautoshop.work.models import ShopTool

log = logging.getLogger(__name__)

DISMISSED_KEY = "product_link.wrenchledger.dismissed"


@login_required
def integrations(request):
    """What is connected, what it did last, and how to test it."""
    require(request.user, "integration.manage")

    return render(
        request,
        "core/integrations.html",
        {
            "offline": conf.OFFLINE_MODE,
            "vin_decode": {
                "enabled": conf.VIN_DECODE_ENABLED,
                "url": settings.VPIC_BASE_URL,
            },
            "lubelogger": {
                "url": conf.LUBELOGGER_URL,
                "mode": conf.LUBELOGGER_MODE,
                "syncs": conf.LUBELOGGER_MODE in lubelogger_sync.SYNC_MODES,
                "every_hours": conf.LUBELOGGER_SYNC_HOURS,
                "last": Setting.get(lubelogger_sync.LAST_SYNC_KEY),
                "result": Setting.get(lubelogger_sync.LAST_RESULT_KEY) or {},
            },
            "wrenchledger": {
                "configured": bool(conf.WRENCHLEDGER_API_KEY),
                "url": settings.WRENCHLEDGER_URL,
                "last": Setting.get(wl.LAST_SYNC_KEY),
                "required_scopes": wl.REQUIRED_SCOPES,
                "consumables_owner": conf.CONSUMABLES_OWNER,
                # The one number that distinguishes "search is broken" from
                # "the copy is incomplete", which look identical from outside.
                "cached": ShopTool.objects.count(),
                "cached_local": ShopTool.objects.filter(checked_at__isnull=True).count(),
            },
            "plate": {
                "enabled": conf.PLATE_LOOKUP_ENABLED,
                "provider": settings.PLATE_LOOKUP_PROVIDER,
                "cap": conf.PLATE_LOOKUP_MONTHLY_CAP,
            },
            # The house placement (INTEGRATION-WRENCHLEDGER.md §10): contextual,
            # static, dismissible, and gone the moment a connection exists.
            "show_wl_placement": (
                conf.SHOW_PRODUCT_LINKS
                and not conf.WRENCHLEDGER_API_KEY
                and not Setting.get(DISMISSED_KEY)
            ),
            "activity": AuditLog.objects.filter(action=AuditLog.Action.OUTBOUND)[:50],
        },
    )


@require_POST
@login_required
def integration_test(request, name):
    """A real call, not a configuration check (FR-INT-1)."""
    require(request.user, "integration.manage")

    if conf.OFFLINE_MODE:
        messages.warning(
            request,
            _("Offline Mode is on, so nothing was contacted."),
        )
        return redirect("integrations")

    if name == "wrenchledger":
        try:
            result = wl.WrenchLedgerClient().check()
        except wl.NotConfigured:
            messages.warning(request, _("No WrenchLedger key is set."))
            return redirect("integrations")

        if result.usable:
            note = _("Connected to a %(tier)s workspace.") % {"tier": result.tier or _("linked")}
            if result.excess_scopes:
                # Over-privileged credentials are the operator's risk, and
                # nobody notices them on their own.
                note = _(
                    "%(note)s That key carries more access than this needs: %(extra)s. "
                    "A narrower key would be safer."
                ) % {"note": note, "extra": ", ".join(result.excess_scopes)}
            messages.success(request, note)
        else:
            messages.error(request, result.message or _("Could not use that connection."))
        return redirect("integrations")

    if name == "lubelogger":
        from .integrations.lubelogger import LubeLoggerClient, NotConfigured

        try:
            diagnosis = LubeLoggerClient().check()
        except NotConfigured:
            messages.warning(request, _("No LubeLogger URL is set."))
            return redirect("integrations")
        if diagnosis.ok:
            messages.success(
                request,
                _("Reached LubeLogger — %(n)d vehicle(s).") % {"n": diagnosis.vehicle_count},
            )
        else:
            messages.error(request, diagnosis.message or _("Could not use that connection."))
        return redirect("integrations")

    messages.error(request, _("There is no integration by that name."))
    return redirect("integrations")


@require_POST
@login_required
def integration_sync(request, name):
    """Run a pull now, rather than waiting for the schedule."""
    require(request.user, "integration.manage")

    if name == "lubelogger":
        try:
            summary = lubelogger_sync.run(force=True)
        except lubelogger_sync.SyncSkipped as skipped:
            messages.warning(request, str(skipped))
            return redirect("integrations")
        except Exception as exc:  # noqa: BLE001 - surfaced, not swallowed
            log.exception("lubelogger sync failed")
            messages.error(request, _("Sync failed: %(err)s") % {"err": exc})
            return redirect("integrations")
        messages.success(
            request,
            _("Pulled %(n)d record(s) since %(since)s.")
            % {"n": summary["created"], "since": summary["since"]},
        )
        return redirect("integrations")

    if name == "wrenchledger":
        # A delta poll is only as complete as every run before it. `rebuild`
        # forgets the watermark and reads the workspace from the start, which is
        # the only way to fill a hole an earlier truncated run left behind.
        rebuild = bool(request.POST.get("rebuild"))
        try:
            summary = wl.sync(rebuild=rebuild)
        except Exception as exc:  # noqa: BLE001
            log.exception("wrenchledger sync failed")
            messages.error(request, _("Sync failed: %(err)s") % {"err": exc})
            return redirect("integrations")
        messages.success(
            request,
            _("Read every tool: %(n)d known here now.") % {"n": summary["tools"]}
            if rebuild
            else _("Checked %(n)d tool(s).") % {"n": summary["tools"]},
        )
        return redirect("integrations")

    messages.error(request, _("There is no integration by that name."))
    return redirect("integrations")


@require_POST
@login_required
def dismiss_product_link(request):
    """One dismissal, remembered forever, per instance.

    No re-prompting after an upgrade. A placement that comes back is the
    behavior that makes people distrust the no-telemetry claim, and that claim
    is load-bearing for why anyone runs this at all.
    """
    require(request.user, "integration.manage")
    Setting.put(DISMISSED_KEY, True)
    messages.success(request, _("That will not come back."))
    return redirect("integrations")


# --------------------------------------------------------------------------
# CSV import (SPEC FR-ADM-6)
# --------------------------------------------------------------------------

#: The parsed file lives in the session between the mapping screen and the
#: write, so the operator maps once rather than re-uploading for the dry run
#: and again for the real thing. Capped because sessions are database rows and
#: a 50,000-row spreadsheet has no business being one.
IMPORT_SESSION_KEY = "csv_import"
MAX_PREVIEW_ROWS = 2000


@login_required
def data_import(request):
    """Bring a spreadsheet in, with the columns named by the person who made it."""
    from . import csvimport

    require(request.user, "integration.manage")
    kind = request.GET.get("kind") or request.POST.get("kind") or "vehicles"
    if kind not in csvimport.SCHEMAS:
        kind = "vehicles"

    context = {
        "kind": kind,
        "kinds": [(name, schema["label"]) for name, schema in csvimport.SCHEMAS.items()],
        "schema": csvimport.SCHEMAS[kind],
        "header": [],
        "fields": [],
        "outcome": None,
    }

    if request.method == "GET":
        return render(request, "core/import.html", context)

    if upload := request.FILES.get("file"):
        text = upload.read().decode("utf-8-sig", errors="replace")
        header, rows = csvimport.read(text)
        if not header:
            messages.error(request, _("No column headings were found in that file."))
            return render(request, "core/import.html", context)
        if len(rows) > MAX_PREVIEW_ROWS:
            messages.warning(
                request,
                _("That file has %(n)d rows; the first %(cap)d are being imported. "
                  "Split it and run it again for the rest.")
                % {"n": len(rows), "cap": MAX_PREVIEW_ROWS},
            )
            rows = rows[:MAX_PREVIEW_ROWS]
        request.session[IMPORT_SESSION_KEY] = {"kind": kind, "header": header, "rows": rows}
        context.update(
            header=header,
            fields=_fields(kind, header, csvimport.guess(kind, header)),
            sample=[[row.get(column, "") for column in header] for row in rows[:5]],
            row_count=len(rows),
        )
        return render(request, "core/import.html", context)

    stored = request.session.get(IMPORT_SESSION_KEY) or {}
    if not stored.get("rows"):
        messages.warning(request, _("Choose a file first."))
        return render(request, "core/import.html", context)

    mapping = {
        name: request.POST.get(f"map_{name}", "").strip()
        for name in csvimport.SCHEMAS[stored["kind"]]["fields"]
    }
    mapping = {name: column for name, column in mapping.items() if column}

    # Dry run unless the operator explicitly said write. There is no path
    # through this view that writes without a preview having been shown first.
    dry_run = not request.POST.get("write")
    outcome = csvimport.run(
        stored["kind"], stored["rows"], mapping, dry_run=dry_run, user=request.user
    )

    header = stored["header"]
    context.update(
        kind=stored["kind"],
        schema=csvimport.SCHEMAS[stored["kind"]],
        header=header,
        fields=_fields(stored["kind"], header, mapping),
        sample=[[row.get(column, "") for column in header] for row in stored["rows"][:5]],
        row_count=len(stored["rows"]),
        outcome=outcome,
    )
    if not dry_run:
        request.session.pop(IMPORT_SESSION_KEY, None)
        messages.success(
            request,
            _("Imported %(n)d record(s). %(already)d were already here.")
            % {"n": outcome.created, "already": outcome.already},
        )
    return render(request, "core/import.html", context)


def _fields(kind: str, header: list[str], mapping: dict[str, str]) -> list[dict]:
    """Assemble the mapping selects here rather than in the template.

    Which option is pre-selected is a comparison between two strings, and a
    template language that can only test truthiness ends up either selecting
    nothing or growing a filter to do it.
    """
    from . import csvimport

    schema = csvimport.SCHEMAS[kind]
    return [
        {
            "name": name,
            "required": name in schema["required"],
            "columns": [
                {"name": column, "selected": mapping.get(name) == column} for column in header
            ],
        }
        for name in schema["fields"]
    ]
