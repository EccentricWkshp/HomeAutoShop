"""
Instance settings and backup, operable from the application (R-9, R-10).

Two screens that exist for the same reason: the instance already knew these
things and had no way to let anyone act on them. It reported that a backup was
overdue while offering no way to take one, and it offered an emergency outbound
kill switch that could only be thrown by editing a file on the host and
restarting a container.

**Restore stays on the command line, deliberately.** Swapping the database out
from underneath a running process is not something a web request should
attempt, and a half-finished one leaves an instance that is neither the old
state nor the new. What the screen does instead is show the exact command with
this instance's real paths already filled in — because the alternative is
somebody reassembling it from the documentation during the one hour they can
least afford to.
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.http import FileResponse, Http404
from django.shortcuts import redirect, render
from django.utils import timezone
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST

from homeautoshop.accounts.models import require

from . import runtime
from .backup import last_backup_age_days
from .models import AuditLog, Job
from .runtime import conf
from .settings_registry import BY_KEY, GROUPS, RESTART, entries_for, settings_currency

log = logging.getLogger(__name__)


def _field(entry, *, stored_secrets: set[str]) -> dict:
    """One row of the form, with its current value already resolved.

    A secret is never sent to the browser — only *where it came from*. There is
    no read path for a credential and this is where that is enforced.

    Three states, not two, and the third is the one that was wrong: a key set in
    the `.env` file has no `credential` row, so asking the credential table
    alone reported "not set" for a key the application was busy authenticating
    with. It also matters which it is, because clearing an environment key here
    cannot work — the file has to be edited.
    """
    from django.conf import settings as django_settings

    source = "none"
    if entry.is_secret:
        if entry.key in stored_secrets:
            source = "stored"
        elif getattr(django_settings, entry.key, ""):
            source = "environment"

    value = "" if entry.is_secret else runtime.current(entry.key)
    currency = ""
    if entry.kind == "money":
        from homeautoshop.core.measurements import Money

        currency = settings_currency()
        # Shown as an amount, because that is now what the box accepts.
        value = Money(int(value or 0), currency).to_decimal()

    return {
        "entry": entry,
        "value": value,
        "currency": currency,
        "source": source,
        "has_secret": source != "none",
        "from_environment": source == "environment",
        "restart": entry.applies == RESTART,
    }


@login_required
def settings_view(request, group: str = "shop"):
    """Everything §17.1 says moves out of the `.env` file."""
    require(request.user, "settings.manage")

    known = {item.key for item in GROUPS}
    if group not in known:
        raise Http404

    if request.method == "POST":
        posted = {}
        for entry in entries_for(group):
            if entry.is_secret:
                # An untouched password field posts blank, which must mean
                # "leave it alone" and not "delete it" — otherwise saving any
                # other field on the page silently unauthenticates every
                # integration in the group.
                raw = request.POST.get(entry.key, "")
                if not raw:
                    continue
                if raw == "\x00clear":
                    posted[entry.key] = ""
                    continue
                posted[entry.key] = raw
            elif entry.kind == "bool":
                posted[entry.key] = entry.key in request.POST
            else:
                posted[entry.key] = request.POST.get(entry.key, "")

        for key in request.POST.getlist("clear"):
            if key in BY_KEY and BY_KEY[key].is_secret:
                posted[key] = ""

        try:
            changed = runtime.save(posted, user=request.user)
        except ValidationError as exc:
            for key, problem in (exc.message_dict if hasattr(exc, "message_dict") else {}).items():
                label = BY_KEY[key].label if key in BY_KEY else key
                messages.error(request, f"{label}: {' '.join(problem)}")
            if not hasattr(exc, "message_dict"):
                messages.error(request, "; ".join(exc.messages))
        else:
            if not changed:
                messages.info(request, _("Nothing was different, so nothing was saved."))
            elif any(BY_KEY[key].applies == RESTART for key in changed):
                messages.success(
                    request,
                    _("Saved. Some of these need a restart before they take effect."),
                )
            else:
                messages.success(request, _("Saved, and in effect now."))
            return redirect("settings", group=group)

    current_group = next(item for item in GROUPS if item.key == group)
    # Read once rather than per field: it is one query either way, and forty
    # fields asking the same question forty times is forty.
    stored_secrets = runtime.configured_credentials()
    return render(
        request,
        "core/settings.html",
        {
            "groups": GROUPS,
            "group": current_group,
            "fields": [
                _field(entry, stored_secrets=stored_secrets) for entry in entries_for(group)
            ],
            "needs_credentials": runtime.unauthenticated_integrations(),
        },
    )


@require_POST
@login_required
def settings_restart(request):
    """Reload the web tier so restart-class settings take effect (§17.2)."""
    require(request.user, "settings.manage")

    if runtime.restart_web():
        messages.success(
            request,
            _("Reloading. Finish this page and the next request will use the new settings."),
        )
    else:
        messages.warning(
            request,
            _(
                "This instance cannot restart itself — gunicorn was started without a "
                "pidfile, or this is the development server. Restart it from the host: "
                "docker compose restart app worker"
            ),
        )
    return redirect(request.META.get("HTTP_REFERER") or "settings")


# ---------------------------------------------------------------------------
# R-10 — backup and export from the UI
# ---------------------------------------------------------------------------


def _directory_size(path: Path) -> int:
    return sum(item.stat().st_size for item in path.rglob("*") if item.is_file())


def held_backups() -> list[dict]:
    """What is actually on disk, read from disk.

    Not from a table of what the instance believes it wrote: the failure this
    screen exists to catch is a backup that was recorded and is not there.
    """
    root = Path(settings.BACKUP_DIR)
    if not root.exists():
        return []

    found: list[dict] = []
    for entry in sorted(root.iterdir(), reverse=True):
        if entry.is_dir():
            database = next(
                (name for name in ("database.dump", "database.sqlite3") if (entry / name).exists()),
                "",
            )
            found.append(
                {
                    "name": entry.name,
                    "kind": "backup",
                    "at": timezone.datetime.fromtimestamp(
                        entry.stat().st_mtime, tz=timezone.get_current_timezone()
                    ),
                    "bytes": _directory_size(entry),
                    "database": database,
                    "media": (entry / "media").exists(),
                    # A backup with no database half is the one that must not
                    # look like the others: it is a media copy, and restoring
                    # from it would produce an empty shop full of photos.
                    "complete": bool(database),
                }
            )
        elif entry.suffix == ".zip" and entry.name.startswith("export-"):
            found.append(
                {
                    "name": entry.name,
                    "kind": "export",
                    "at": timezone.datetime.fromtimestamp(
                        entry.stat().st_mtime, tz=timezone.get_current_timezone()
                    ),
                    "bytes": entry.stat().st_size,
                    "complete": True,
                }
            )
    return found


def restore_command(latest: str | None) -> str:
    """The command to type, with this instance's real paths already in it."""
    name = latest or "<backup-folder>"
    return f"docker compose run --rm app python manage.py restore /data/backups/{name}"


@login_required
def backups(request):
    """R-10 — take a backup, see what is held, download it, export."""
    require(request.user, "settings.manage")

    running = Job.objects.filter(
        type__in=("backup.run", "export.build"), state__in=(Job.State.PENDING, Job.State.RUNNING)
    ).first()
    held = held_backups()
    newest = next((item["name"] for item in held if item["kind"] == "backup"), None)

    root = Path(settings.BACKUP_DIR)
    try:
        free_bytes = shutil.disk_usage(root if root.exists() else root.parent).free
    except OSError:
        free_bytes = None

    return render(
        request,
        "core/backups.html",
        {
            "held": held,
            "running": running,
            "backup_age": last_backup_age_days(),
            "warn_after": conf.BACKUP_WARN_AFTER_DAYS,
            "interval_hours": conf.BACKUP_INTERVAL_HOURS,
            "retention": {
                "daily": conf.BACKUP_RETENTION_DAILY,
                "weekly": conf.BACKUP_RETENTION_WEEKLY,
                "monthly": conf.BACKUP_RETENTION_MONTHLY,
            },
            "directory": str(root),
            "free_bytes": free_bytes,
            "restore_command": restore_command(newest),
            "recent": AuditLog.objects.filter(
                action__in=(AuditLog.Action.BACKUP, AuditLog.Action.EXPORT)
            )[:10],
        },
    )


@require_POST
@login_required
def backup_now(request):
    """Enqueue a backup rather than run it in the request.

    A `pg_dump` of a shop with years of photos is minutes of work; doing it
    inline would hold a worker, hit the proxy's timeout, and leave the operator
    looking at a dead page with no idea whether it finished.
    """
    require(request.user, "settings.manage")

    kind = "export.build" if request.POST.get("what") == "export" else "backup.run"
    existing = Job.objects.filter(
        type=kind, state__in=(Job.State.PENDING, Job.State.RUNNING)
    ).first()
    if existing is not None:
        messages.info(request, _("One is already running. This page will show it when it lands."))
        return redirect("backups")

    Job.objects.create(type=kind)
    messages.success(
        request,
        _("Started. It runs in the background — this page shows it when it finishes.")
        if kind == "backup.run"
        else _("Building the export. It appears below when it is ready."),
    )
    return redirect("backups")


@login_required
def backup_download(request, name: str):
    """Hand over one held artifact.

    Only ever a single file: a backup folder is streamed as its database half,
    which is the part that is small and the part a person actually wants off
    the box. The media tree is gigabytes and is copied, not downloaded.
    """
    require(request.user, "settings.manage")

    root = Path(settings.BACKUP_DIR).resolve()
    target = (root / name).resolve()
    # `..` in a URL segment is the classic way out of a directory. Checked by
    # resolved prefix rather than by string matching on the name, and answered
    # with a flat 404 — an attempt to walk out of the backup directory gets
    # nothing back to learn from.
    if not target.is_relative_to(root):
        raise Http404
    if not target.exists():
        # Pruning runs on a schedule, so an open page can easily be offering a
        # backup that has since aged out. Saying so beats a 404.
        messages.warning(request, _("%(name)s is not here any more.") % {"name": name})
        return redirect("backups")

    if target.is_dir():
        database = next(
            (target / n for n in ("database.dump", "database.sqlite3") if (target / n).exists()),
            None,
        )
        if database is None:
            messages.warning(request, _("That backup has no database file in it."))
            return redirect("backups")
        target = database

    AuditLog.objects.create(
        entity_type="Backup",
        action=AuditLog.Action.EXPORT,
        user=request.user,
        summary=_("Downloaded %(name)s") % {"name": name},
    )
    return FileResponse(open(target, "rb"), as_attachment=True, filename=target.name)


@require_POST
@login_required
def backup_delete(request, name: str):
    require(request.user, "settings.manage")

    root = Path(settings.BACKUP_DIR).resolve()
    target = (root / name).resolve()
    if not target.is_relative_to(root) or target == root:
        raise Http404
    if not target.exists():
        messages.info(request, _("%(name)s was already gone.") % {"name": name})
        return redirect("backups")

    # Not `ignore_errors=True`. That is what hid a backup whose database file
    # was still open — deleted, said so, and was still there on the next page
    # load. A delete that cannot happen has to say it did not happen.
    errors: list[str] = []
    if target.is_dir():
        shutil.rmtree(target, onexc=lambda _fn, _path, exc: errors.append(str(exc)))
    else:
        try:
            target.unlink()
        except OSError as exc:
            errors.append(str(exc))

    if errors or target.exists():
        log.error("could not delete backup %s: %s", name, "; ".join(errors))
        messages.error(
            request,
            _("%(name)s could not be deleted: %(why)s")
            % {"name": name, "why": errors[0] if errors else _("something still has it open")},
        )
    else:
        messages.success(request, _("Deleted %(name)s.") % {"name": name})
    return redirect("backups")
