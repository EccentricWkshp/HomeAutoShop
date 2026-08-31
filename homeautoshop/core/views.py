"""Dashboard, search, and health (SPEC §7.10 FR-REP-1, FR-SEARCH-1, FR-ADM-8)."""

from __future__ import annotations

import logging
import pathlib
from datetime import timedelta

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Count, Q
from django.http import FileResponse, Http404, HttpResponse, JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext as _
from django.views.decorators.http import require_http_methods, require_POST

from .runtime import allowlist, conf
from homeautoshop.accounts.models import can
from homeautoshop.assets.models import Asset
from homeautoshop.work.models import WorkOrder, WorkOrderStatus

from homeautoshop.maintenance.services import due_dashboard, project

from .backup import last_backup_age_days
from .models import Job
from .search import search as run_search

log = logging.getLogger(__name__)


@login_required
def lubelogger_import(request):
    """One-time migration of existing history from LubeLogger (SPEC §8.6).

    Nothing here depends on LubeLogger staying around: this copies history in
    once, and the instance owns it afterwards.

    The connection check is not a convenience button, it is a gate. LubeLogger
    returns locale-formatted numbers unless its invariant-culture flag is set,
    and `1.234,56` imported as `1.23` is wrong money that looks plausible for
    months. The command-line importer refuses to run when the check fails, and
    so does this.
    """
    from homeautoshop.accounts.models import require

    require(request.user, "integration.manage")

    from .integrations.importer import run_import
    from .integrations.lubelogger import LubeLoggerClient, NotConfigured

    context = {
        # The URL alone. Requiring a key here hid every control on this page
        # from an operator whose LubeLogger does not ask for one — and plenty
        # do not, because a LAN instance is often left open. The command line
        # imported their history happily while the screen said "not configured
        # yet", which is the worst kind of disagreement.
        #
        # FR-INT-1 wants a *real connectivity check*, and there is one right
        # here. Whether the key is needed is the server's answer to give, not
        # something to infer from a blank setting.
        "configured": bool(conf.LUBELOGGER_URL),
        "instance_url": conf.LUBELOGGER_URL,
        "has_key": bool(conf.LUBELOGGER_API_KEY),
        "mode": conf.LUBELOGGER_MODE,
        "offline_mode": conf.OFFLINE_MODE,
    }

    if request.method == "POST":
        action = request.POST.get("action")
        create_missing = bool(request.POST.get("create_missing"))
        try:
            client = LubeLoggerClient()
            diagnosis = client.check()
            context["diagnosis"] = diagnosis

            if not diagnosis.ok:
                messages.warning(
                    request,
                    _("The connection check failed, so nothing was imported."),
                )
            elif action in ("preview", "commit"):
                report = run_import(
                    dry_run=(action == "preview"),
                    create_missing=create_missing,
                    client=client,
                )
                context["report"] = report
                if action == "preview":
                    messages.info(
                        request,
                        _("Dry run — nothing was written. %(n)d record(s) would be created.")
                        % {"n": report.total_created},
                    )
                else:
                    messages.success(
                        request,
                        _("Imported %(n)d record(s).") % {"n": report.total_created},
                    )
        except NotConfigured as exc:
            messages.warning(request, str(exc))
        except Exception as exc:  # noqa: BLE001 - surfaced, not swallowed
            # An import that half-fails must say so plainly. The alternative is
            # a 500 page and no idea how far it got.
            log.exception("LubeLogger import failed")
            messages.error(
                request,
                _("The import stopped: %(detail)s") % {"detail": exc},
            )

    # Local vehicles, for the manual link below. A source vehicle carrying no
    # usable identifier cannot be matched by any rule, and refusing to guess is
    # only defensible if there is a way to say which one it is.
    context["assets"] = Asset.objects.all().order_by("nickname")
    context["links"] = _lubelogger_links()
    return render(request, "core/lubelogger.html", context)


def _lubelogger_links() -> dict:
    """Existing source-vehicle links, keyed by their LubeLogger id."""
    from .integrations.lubelogger import SOURCE, instance_url
    from .models import ExternalRef

    found = {}
    for ref in ExternalRef.objects.filter(
        source_system=SOURCE, source_instance_url=instance_url(), external_type="vehicle"
    ):
        asset = Asset.all_objects.filter(pk=ref.entity_id).first()
        if asset is not None:
            found[str(ref.external_id)] = asset
    return found


@require_POST
@login_required
def lubelogger_link(request):
    """Say which local vehicle a LubeLogger vehicle is (SPEC §8.6, FR-INT-12).

    Writes an `external_ref` row and nothing else. That is the whole mechanism:
    the importer already consults `external_ref` before it tries anything
    clever, so one link makes every future run — and every scheduled pull —
    match without guessing again.

    The alternative was to loosen the automatic matcher until it caught these,
    which is how one vehicle's history ends up written into another with no
    clean way back.
    """
    from homeautoshop.accounts.models import require

    from .integrations.lubelogger import SOURCE, instance_url
    from .models import ExternalRef

    require(request.user, "integration.manage")

    external_id = (request.POST.get("external_id") or "").strip()
    asset_id = (request.POST.get("asset") or "").strip()
    if not external_id:
        messages.warning(request, _("Nothing to link."))
        return redirect("lubelogger_import")

    instance = instance_url()

    if not asset_id:
        # Unlinking is as important as linking: a link made in error is
        # otherwise permanent, and the import would keep honoring it.
        removed, _detail = ExternalRef.objects.filter(
            source_system=SOURCE,
            source_instance_url=instance,
            external_type="vehicle",
            external_id=external_id,
        ).delete()
        messages.success(
            request,
            _("Link removed.") if removed else _("That vehicle was not linked."),
        )
        return redirect("lubelogger_import")

    asset = Asset.objects.filter(pk=asset_id).first()
    if asset is None:
        messages.error(request, _("That vehicle is not here any more."))
        return redirect("lubelogger_import")

    clash = (
        ExternalRef.objects.filter(
            source_system=SOURCE, source_instance_url=instance, external_type="vehicle"
        )
        .filter(entity_id=asset.pk)
        .exclude(external_id=external_id)
        .first()
    )
    if clash is not None:
        # Two source vehicles pointing at one local vehicle merges two
        # histories into one record, silently.
        messages.error(
            request,
            _("%(name)s is already linked to another LubeLogger vehicle. Unlink that one first.")
            % {"name": asset.nickname},
        )
        return redirect("lubelogger_import")

    ExternalRef.objects.update_or_create(
        source_system=SOURCE,
        source_instance_url=instance,
        external_type="vehicle",
        external_id=external_id,
        defaults={"entity_type": "Asset", "entity_id": asset.pk},
    )
    messages.success(
        request,
        _("Linked to %(name)s. Run the dry run again to see what it brings in.")
        % {"name": asset.nickname},
    )
    return redirect("lubelogger_import")


@login_required
def dashboard(request):
    """Answers 'what needs attention?' in one screen, ordered by urgency."""
    open_orders = WorkOrder.objects.open().select_related("asset")
    blocked = open_orders.filter(status=WorkOrderStatus.WAITING_ON_PARTS)
    today = timezone.localdate()
    horizon = today + timedelta(days=30)

    alerts = []
    backup_age = last_backup_age_days()
    if backup_age is None:
        alerts.append(
            {
                "level": "warn",
                "text": _("No backup has ever run."),
                "url": "/admin/",
            }
        )
    elif backup_age > conf.BACKUP_WARN_AFTER_DAYS:
        alerts.append(
            {
                "level": "warn",
                "text": _("Last backup was %(n)d days ago.") % {"n": int(backup_age)},
                "url": "/admin/",
            }
        )

    expiring = Asset.objects.fleet().filter(
        plate_expires_on__isnull=False, plate_expires_on__lte=horizon
    )

    due = due_dashboard(limit=10)
    return render(
        request,
        "core/dashboard.html",
        {
            "due": [(item, project(item)) for item in due],
            "overdue_count": sum(1 for i in due if i.status == "overdue"),
            "fleet_count": Asset.objects.fleet().count(),
            "open_orders": open_orders[:12],
            "open_count": open_orders.count(),
            "blocked": blocked,
            "expiring": expiring,
            "alerts": alerts,
            "recent_assets": Asset.objects.fleet().order_by("-updated_at")[:6],
            "failed_jobs": Job.objects.filter(state=Job.State.FAILED).count(),
        },
    )


@login_required
def search_view(request):
    query = request.GET.get("q", "")
    return render(request, "core/search.html", {"results": run_search(query), "q": query})


@login_required
def health(request):
    """Instance health (FR-ADM-8)."""
    if not can(request.user, "settings.manage"):
        raise Http404
    from django.db import connection

    from homeautoshop.mediafiles.models import Media
    from homeautoshop.mediafiles.services import tesseract_status

    media = Media.objects.aggregate(n=Count("id"))
    return render(
        request,
        "core/health.html",
        {
            "vendor": connection.vendor,
            "media_count": media["n"],
            "media_bytes": sum(Media.objects.values_list("bytes", flat=True)),
            "jobs": Job.objects.values("state").annotate(n=Count("id")),
            "ocr": tesseract_status(),
            "ocr_pending": Media.objects.filter(ocr_status=Media.OcrStatus.PENDING).count(),
            "ocr_failed": Media.objects.filter(ocr_status=Media.OcrStatus.FAILED).count(),
            "backup_age": last_backup_age_days(),
            "offline_mode": conf.OFFLINE_MODE,
            # The computed one, not the imported one: the integration
            # addresses it is derived from are editable now, so the list read
            # at import is no longer what an outbound request is checked
            # against — and a health screen showing the wrong one is worse
            # than a health screen showing nothing.
            "allowlist": allowlist(),
        },
    )


def healthz(request):
    return HttpResponse("ok", content_type="text/plain")


def readyz(request):
    from django.db import connection

    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
    except Exception:
        return HttpResponse("db unavailable", status=503, content_type="text/plain")
    return HttpResponse("ready", content_type="text/plain")


# Models a soft-deleted row can be restored from. Deliberately explicit: the
# trash is a safety net for the things people delete by accident, not a
# generic undelete for every table.
TRASHABLE = {
    "asset": ("homeautoshop.assets.models", "Asset"),
    "work_order": ("homeautoshop.work.models", "WorkOrder"),
    "person": ("homeautoshop.people.models", "Person"),
    "inspection": ("homeautoshop.inspections.models", "Inspection"),
}


def _trash_model(kind: str):
    import importlib

    module, name = TRASHABLE[kind]
    return getattr(importlib.import_module(module), name)


@login_required
def trash(request):
    """A 30-day trash with restore (FR-ADM-7)."""
    from homeautoshop.accounts.models import require

    require(request.user, "trash.manage")
    groups = []
    for kind in TRASHABLE:
        rows = list(_trash_model(kind).all_objects.in_trash()[:100])
        if rows:
            groups.append({"kind": kind, "label": kind.replace("_", " ").title(), "rows": rows})
    return render(request, "core/trash.html", {"groups": groups, "retention_days": 30})


@require_POST
@login_required
def trash_restore(request, kind: str, pk):
    from django.http import HttpResponseRedirect
    from homeautoshop.accounts.models import require
    from homeautoshop.core.models import AuditLog

    require(request.user, "trash.manage")
    if request.method != "POST" or kind not in TRASHABLE:
        raise Http404
    obj = _trash_model(kind).all_objects.filter(pk=pk).first()
    if obj is None:
        raise Http404
    obj.restore()
    AuditLog.objects.create(
        entity_type=type(obj).__name__,
        entity_id=obj.pk,
        action=AuditLog.Action.RESTORE,
        user=request.user,
        summary=str(obj)[:255],
    )
    return redirect("trash")


@login_required
def reports(request):
    """Shop-level reporting (FR-REP-3/4). Every table exports to CSV."""
    from .costs import active_warranties, inventory_value, spend_by_month

    return render(
        request,
        "core/reports.html",
        {
            "spend": spend_by_month(),
            "inventory_value": inventory_value(),
            "warranties": active_warranties()[:50],
            "assets": Asset.objects.fleet(),
        },
    )


@login_required
def asset_costs(request, pk):
    """What has this vehicle actually cost (FR-COST-2/3)."""
    from django.shortcuts import get_object_or_404

    from .costs import asset_cost, cost_per_distance

    asset = get_object_or_404(Asset, pk=pk)
    return render(
        request,
        "core/asset_costs.html",
        {"asset": asset, "rollup": asset_cost(asset), "per_distance": cost_per_distance(asset)},
    )


@login_required
def export_csv(request, kind: str):
    """FR-REP-4 — no report is a dead end."""
    import csv

    from django.http import HttpResponse

    from .costs import active_warranties, spend_by_month

    response = HttpResponse(content_type="text/csv")
    response["Content-Disposition"] = f'attachment; filename="{kind}.csv"'
    writer = csv.writer(response)

    if kind == "spend":
        writer.writerow(["month", "amount_minor", "currency"])
        for row in spend_by_month():
            writer.writerow([row["month"], row["amount_minor"], row["money"].currency])
    elif kind == "warranties":
        writer.writerow(["part", "vehicle", "installed", "expires", "work_order"])
        for usage in active_warranties():
            writer.writerow([
                str(usage.part), usage.work_order.asset.nickname,
                usage.installed_at, usage.warranty_expires_on, usage.work_order.number,
            ])
    elif kind == "assets":
        from .costs import asset_cost

        writer.writerow(["nickname", "descriptor", "status", "total_minor", "currency"])
        for asset in Asset.objects.all():
            rollup = asset_cost(asset)
            writer.writerow([
                asset.nickname, asset.descriptor, asset.status,
                rollup.total_minor, rollup.currency,
            ])
    else:
        raise Http404
    return response


@login_required
def reminders(request):
    """Manage delivery channels (FR-MAINT-10)."""
    from django.conf import settings as conf

    from homeautoshop.accounts.models import require

    from .models import NotificationChannel
    from .notifications import collect

    require(request.user, "settings.manage")
    return render(
        request,
        "core/reminders.html",
        {
            "channels": NotificationChannel.objects.all(),
            # Web push is not in this list: a browser subscribes itself, and
            # offering it as a "kind" with a target field to type would be a
            # form nobody can fill in.
            "kinds": [
                (value, label)
                for value, label in NotificationChannel.Kind.choices
                if value != NotificationChannel.Kind.WEBPUSH
            ],
            "digest": collect(),
            "enabled": conf.REMINDERS_ENABLED,
            "smtp_configured": bool(conf.EMAIL_HOST),
            "cooldown_days": conf.REMINDER_COOLDOWN_DAYS,
            "push_strings": {
                "ready": _("This device can be notified. Nothing is sent until you say so."),
                "subscribed": _("This device will be notified when something is due."),
                "unsupported": _(
                    "This browser cannot do notifications, or the page is not on HTTPS."
                ),
                "unavailable": _("Push is not available on this instance."),
                "blocked": _(
                    "Notifications are blocked for this site. Allow them in the browser's "
                    "site settings — asking again from here will not work."
                ),
                "declined": _("Not enabled. You can come back to this."),
                "failed": _("The browser would not subscribe. Try again."),
            },
        },
    )


@require_POST
@login_required
def reminder_channel_add(request):
    from homeautoshop.accounts.models import require

    from .models import NotificationChannel

    require(request.user, "settings.manage")
    target = (request.POST.get("target") or "").strip()
    if target:
        NotificationChannel.objects.create(
            name=(request.POST.get("name") or target)[:80],
            kind=request.POST.get("kind") or NotificationChannel.Kind.EMAIL,
            target=target,
            is_enabled=bool(request.POST.get("is_enabled")),
            include_routine=bool(request.POST.get("include_routine")),
        )
    return redirect("reminders")


@require_POST
@login_required
def reminder_channel_action(request, channel_id):
    """Toggle, delete, or send a test digest.

    The test send is the whole reason this page exists rather than leaving
    channels to the admin: SMTP configuration is never right first time, and
    finding out at 2am when the first real digest fails is no use.
    """
    from django.shortcuts import get_object_or_404
    from homeautoshop.accounts.models import require

    from .models import NotificationChannel
    from .notifications import Alert, Digest, deliver

    require(request.user, "settings.manage")
    channel = get_object_or_404(NotificationChannel, pk=channel_id)
    action = request.POST.get("action")

    if action == "delete":
        channel.delete()
        messages.success(request, _("Channel removed."))
    elif action == "toggle":
        channel.is_enabled = not channel.is_enabled
        channel.save()
        messages.success(
            request, _("Enabled.") if channel.is_enabled else _("Disabled.")
        )
    elif action == "test":
        sent = deliver(
            channel,
            Digest(alerts=[
                Alert(
                    dedupe_key=f"test:{timezone.now().timestamp()}",
                    severity="info",
                    title=_("Test message from %(shop)s") % {"shop": conf.SHOP_NAME},
                    detail=_("If you are reading this, delivery works."),
                )
            ]),
        )
        if sent:
            messages.success(request, _("Test sent."))
        else:
            messages.error(
                request,
                _("Test failed: %(err)s") % {"err": channel.last_error or _("unknown")},
            )
    return redirect("reminders")


# --------------------------------------------------------------------------
# PWA (SPEC §9.4)
# --------------------------------------------------------------------------


# GET and HEAD. Django's `require_GET` refuses HEAD, which turns an uptime
# probe or a `curl -I` into a 405 on a file that plainly exists.
@require_http_methods(["GET", "HEAD"])
def service_worker(request):
    """Serve the worker from the site root, not from /static/.

    A service worker's default scope is the directory it is served from, so one
    at `/static/sw.js` would control `/static/` and nothing else — it would
    register cleanly, report as active, and never intercept a single page. The
    alternatives are a `Service-Worker-Allowed` header WhiteNoise does not set,
    or this: six lines that put the file where its scope needs to be.
    """
    from django.contrib.staticfiles import finders

    path = finders.find("sw.js")
    if not path:
        raise Http404("sw.js")
    body = pathlib.Path(path).read_text(encoding="utf-8")
    response = HttpResponse(body, content_type="text/javascript; charset=utf-8")
    response["Service-Worker-Allowed"] = "/"
    # Never cached: a stale worker keeps serving a stale app, and the usual
    # symptom is an update that "did not deploy".
    response["Cache-Control"] = "no-cache"
    return response


@login_required
def sync_queue(request):
    """Inspect what is waiting on this device (SPEC §5.4).

    Server-side this page knows nothing — the queue lives in the browser's
    IndexedDB, which is the only place it can live if it is to survive the tab
    closing and the phone going into a pocket. All this view supplies is the
    shell and the wording.
    """
    return render(
        request,
        "core/sync_queue.html",
        {
            "strings": {
                "ops": {
                    "reading.create": _("Meter reading"),
                    "note.create": _("Note on a work order"),
                    "job_item.status": _("Job item status"),
                    "work_order.status": _("Work order status"),
                },
                "today": _("today"),
                "daysAgo": _("%(n)s days ago"),
                "nothingQueued": _("Nothing waiting. Everything captured here has been sent."),
                "noConflicts": _("None."),
                "changedElsewhere": _("Somebody changed this record while you were offline."),
                "refused": _("The server would not accept this."),
                "keepMine": _("Use mine"),
                "keepTheirs": _("Keep theirs"),
                "discard": _("Discard"),
                "confirmDiscard": _("Throw this away? It has not been sent anywhere."),
                "confirmOverwrite": _(
                    "This replaces the version on the server with yours. Their change is lost."
                ),
            }
        },
    )


@login_required
def push_subscribe(request):
    """Register this browser for reminder notifications (§9.4).

    Written by the browser after the operator has answered a permission prompt
    they saw — there is no path here that enables notifications on somebody's
    behalf. The endpoint is checked against the known push services before it
    is stored, because a subscription is written by script and the allowlist is
    what stops this becoming a general-purpose outbound POST.
    """
    import json as _json

    from homeautoshop.accounts.models import require

    from . import webpush
    from .models import NotificationChannel

    require(request.user, "settings.manage")

    if request.method == "GET":
        return JsonResponse(
            {"available": webpush.available(), "key": webpush.public_key() if webpush.available() else ""}
        )

    if request.method != "POST":
        raise Http404

    try:
        payload = _json.loads(request.body.decode("utf-8") or "{}")
    except ValueError:
        return JsonResponse({"error": "malformed"}, status=400)

    subscription = payload.get("subscription") or {}
    endpoint = str(subscription.get("endpoint", ""))
    if not webpush.endpoint_allowed(endpoint):
        return JsonResponse({"error": "endpoint not allowed"}, status=400)

    channel, created = NotificationChannel.objects.get_or_create(
        kind=NotificationChannel.Kind.WEBPUSH,
        target=endpoint[:500],
        defaults={
            "name": str(payload.get("label") or _("This browser"))[:80],
            "user": request.user,
            # On by default *here* only. The operator has already answered a
            # browser permission prompt to get this far, so a second opt-in
            # would be asking the same question twice.
            "is_enabled": True,
        },
    )
    channel.subscription = subscription
    channel.is_enabled = True
    channel.save(update_fields=["subscription", "is_enabled", "updated_at"])
    return JsonResponse({"ok": True, "created": created, "channel": str(channel.pk)})


# --------------------------------------------------------------------------
# QR labels and scanning (SPEC FR-INV-2, FR-VEH-5, C-4)
# --------------------------------------------------------------------------


@require_http_methods(["GET", "HEAD"])
@login_required
def scan_target(request, pk):
    """Resolve a scanned label to whatever it names.

    One route for every kind of label. Primary keys are UUIDv7 and unique
    across the database, so a bin, a vehicle and a part can all carry the same
    label format and the reader does not have to know which it is holding.

    Ordered cheapest-first, and it stops at the first hit — a UUID collision
    across three tables is not a case worth writing code for.
    """
    from homeautoshop.assets.models import Asset
    from homeautoshop.parts.models import Location, Part

    for model, route in (
        (Location, "inventory"),
        (Asset, "asset_detail"),
        (Part, "part_detail"),
    ):
        found = model.objects.filter(pk=pk).first()
        if found is None:
            continue
        if model is Location:
            # Locations have no detail page of their own; the inventory screen
            # is the thing that shows what is in one.
            return redirect(f"{reverse('inventory')}?location={found.pk}")
        return redirect(route, pk=found.pk)

    messages.warning(
        request,
        _("That label points at something that is no longer here. It may have been deleted."),
    )
    return redirect("dashboard")


@login_required
def labels(request):
    """A printable sheet of QR labels.

    Deliberately one page rather than a per-label download: labels are printed
    a sheet at a time, onto whatever stock is in the drawer, and cut. Anything
    fancier means guessing at label stock this application cannot see.
    """
    from homeautoshop.assets.models import Asset
    from homeautoshop.parts.models import Location

    from .labels import label_for

    kind = request.GET.get("kind", "locations")
    wanted = request.GET.getlist("id")

    if kind == "vehicles":
        rows = Asset.objects.all()
        heading = _("Vehicle tags")
        note = _(
            "Stick one inside the door jamb or on the windshield. Scanning it opens "
            "the vehicle, which is the fastest way to log an odometer reading."
        )
    else:
        kind = "locations"
        rows = Location.objects.all()
        heading = _("Bin labels")
        note = _("Scanning one opens what is stored in it.")

    if wanted:
        rows = rows.filter(pk__in=wanted)

    return render(
        request,
        "core/labels.html",
        {
            "kind": kind,
            "heading": heading,
            "note": note,
            "labels": [label_for(row) for row in rows[:200]],
            "location_count": Location.objects.count(),
            "vehicle_count": Asset.objects.count(),
        },
    )
