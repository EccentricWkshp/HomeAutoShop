"""
Reading configuration that can change while the instance is running (R-9, §17.2).

`config/settings.py` still reads the environment, and still holds every default.
What this module adds is the layer above it:

    database  →  environment  →  default

An instance nobody has touched behaves exactly as it did before, because with
no `setting` row the lookup falls straight through to `django.conf.settings`.
That is the whole reason the registry keys are Django settings names: there is
one copy of the defaults, not two that drift.

**Why an accessor rather than patching `django.conf.settings`.** Django caches
settings attributes aggressively and by design, and a module-level
`settings.OFFLINE_MODE` is read once at import in plenty of code. §17.2 calls
for "moving their consumers to a lazily-read accessor", and `conf.OFFLINE_MODE`
is that: one token different at the call site, evaluated at the moment it is
asked.

**Freshness.** Values are cached per process, and `ConfigMiddleware` drops
that cache at the start of every request — so a page render asks the table
once instead of a dozen times, and never serves a value that was true a moment
ago. Outside a request, in the worker and in management commands, the cache
ages out after `CACHE_SECONDS` instead; nothing there is watching a screen.
Writes clear it in the writing process at once, so the page that saved a
change never shows the old value back.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import os
import signal
import time
from typing import Any
from urllib.parse import urlparse

from django.conf import settings as django_settings
from django.core.exceptions import ValidationError
from django.dispatch import receiver
# Public Django surface, not a test-only import: it fires whenever a settings
# value moves at runtime, which is exactly when a cached one stops being true.
from django.test.signals import setting_changed
from django.utils.translation import gettext_lazy as _

from .settings_registry import BY_KEY, RESTART_KEYS, SECRET_KEYS, coerce

log = logging.getLogger(__name__)

#: How long a process may serve a cached settings value.
CACHE_SECONDS = 1.0

#: Bumped on every restart-class write; recorded by each process at boot.
GENERATION_KEY = "config_generation"

_cache: dict[str, Any] = {}
_cache_at: float = 0.0

#: The generation this process booted with. `None` until `record_generation()`
#: runs, which is deliberate: a process that never recorded one — a management
#: command, a test — is not "stale", it is simply not long-running.
_booted_generation: int | None = None


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


def _overlay() -> dict[str, Any]:
    global _cache, _cache_at
    now = time.monotonic()
    if _cache_at and (now - _cache_at) < CACHE_SECONDS:
        return _cache

    from django.db import DatabaseError

    from .models import Setting

    try:
        rows = dict(Setting.objects.values_list("key", "value"))
    except DatabaseError:
        # Before the first migration, or with the database briefly away. The
        # environment is a complete configuration on its own, so this degrades
        # to exactly the behaviour of the release before this one.
        return _cache

    _cache = {key: payload.get("v") for key, payload in rows.items() if isinstance(payload, dict)}
    _cache_at = now
    return _cache


def invalidate() -> None:
    global _cache_at
    _cache_at = 0.0


@receiver(setting_changed)
def _drop_cache_when_settings_move(**kwargs) -> None:
    """Follow `override_settings`, which is otherwise silently ignored here.

    The overlay is the database half of `current()`, and it is cached for a
    second. That is the right trade in production and a trap under test: a
    case that says `override_settings(SHOP_NAME=...)` and then reads
    `conf.SHOP_NAME` gets whatever the previous case left warm, because the
    rollback that discarded the row cannot reach a dictionary in memory.

    Django's own caches subscribe to this signal for the same reason.
    """
    invalidate()


def current(key: str) -> Any:
    """The effective value: database, else environment, else default."""
    if key in SECRET_KEYS:
        stored = credential_get(key)
        if stored is not None:
            return stored
        return getattr(django_settings, key, "")

    overlay = _overlay()
    if key in overlay and key in BY_KEY:
        return overlay[key]
    return getattr(django_settings, key)


class _Conf:
    """`conf.OFFLINE_MODE` — the same read as `settings.OFFLINE_MODE`, live.

    Deliberately not a dict lookup: the call sites read better as attributes,
    and a typo raises `AttributeError` from `django.conf.settings` exactly as
    it did before, rather than silently returning `None`.
    """

    def __getattr__(self, name: str) -> Any:
        return current(name)

    def __dir__(self):
        return sorted(BY_KEY)


conf = _Conf()


def allowlist() -> list[str]:
    """Hosts an outbound request may reach at all (§12.3).

    Computed rather than read, because the integration addresses it is derived
    from are now editable. `config/settings.py` appends the LubeLogger and
    WrenchLedger hosts at import time; with those URLs moving into the database
    a saved address would otherwise be blocked by an allowlist built before it
    existed — the integration would be configured, enabled, and refused.
    """
    hosts = list(django_settings.OUTBOUND_ALLOWLIST)
    for key in ("LUBELOGGER_URL", "WRENCHLEDGER_URL", "PLATE_LOOKUP_URL", "VPIC_BASE_URL"):
        value = current(key) if key in BY_KEY else getattr(django_settings, key, "")
        if not value:
            continue
        if host := urlparse(str(value)).hostname:
            hosts.append(host)

    # Normalized, because this list is now half derived and half hand-written.
    # `settings.py` already appends the configured integration hosts, so those
    # arrive twice; and a hand-written entry is easily typed as `nhtsa.gov/
    # recalls` or `https://nhtsa.gov`, neither of which can ever match a
    # hostname. Both are silent failures — a duplicated entry looks careless on
    # the health screen, and an entry with a path looks allowed and is not.
    seen: list[str] = []
    for entry in hosts:
        bare = str(entry).strip().lower()
        bare = bare.split("://", 1)[-1].split("/", 1)[0].split(":", 1)[0]
        if bare and bare not in seen:
            seen.append(bare)
    return seen


# ---------------------------------------------------------------------------
# Credentials (§17.1)
# ---------------------------------------------------------------------------


def _fernet():
    """Encryption for stored integration keys.

    `CREDENTIAL_KEY` stays in the environment by definition — a key kept in the
    database it protects is not a key. It falls back to a value derived from
    `SECRET_KEY` so an existing instance keeps working without a new variable;
    that is weaker (one secret protecting two things) and the install guide
    says so.
    """
    from cryptography.fernet import Fernet

    raw = getattr(django_settings, "CREDENTIAL_KEY", "") or django_settings.SECRET_KEY
    digest = hashlib.sha256(f"homeautoshop.credentials.v1:{raw}".encode()).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def credential_get(key: str) -> str | None:
    """The stored value, or None when nothing has been entered here."""
    from django.db import DatabaseError

    from .models import Credential

    try:
        blob = Credential.objects.filter(key=key).values_list("ciphertext", flat=True).first()
    except DatabaseError:
        return None
    if not blob:
        return None
    try:
        return _fernet().decrypt(blob.encode()).decode()
    except Exception:
        # Almost always `CREDENTIAL_KEY` having been rotated, which invalidates
        # every stored credential at once — the intended emergency behaviour.
        # Treated as "not configured", which is the state the UI can explain.
        log.warning("stored credential %s could not be decrypted", key)
        return None


#: Written beside a credential, and deliberately *not* secret. See below.
CONFIGURED_PREFIX = "credential.configured."


def credential_set(key: str, value: str) -> None:
    """Store a credential, and separately record **that** one exists.

    The marker is the piece that makes §17.1's promise deliverable. Credentials
    are excluded from every backup, so after a restore the `credential` table is
    empty — and since the key *is* the on-switch for these integrations, an
    empty table is indistinguishable from an instance that never configured
    them. There would be nothing to list.

    The marker is an ordinary `setting` row carrying a boolean, so it rides
    along in the backup exactly as the rest of the configuration does, and
    carries no secret material to leak. What comes back is: this shop had
    WrenchLedger set up, and it does not any more.
    """
    from .models import Credential, Setting

    if not value:
        Credential.objects.filter(key=key).delete()
        Setting.objects.filter(key=f"{CONFIGURED_PREFIX}{key}").delete()
        return
    Credential.objects.update_or_create(
        key=key, defaults={"ciphertext": _fernet().encrypt(value.encode()).decode()}
    )
    Setting.put(f"{CONFIGURED_PREFIX}{key}", True)


def configured_credentials() -> set[str]:
    from django.db import DatabaseError

    from .models import Credential

    try:
        return set(Credential.objects.values_list("key", flat=True))
    except DatabaseError:
        return set()


#: What to call each one on the screen that asks for it back.
CREDENTIAL_NAMES = {
    "WRENCHLEDGER_API_KEY": "WrenchLedger",
    "LUBELOGGER_API_KEY": "LubeLogger",
    "PLATE_LOOKUP_KEY": _("Plate lookup"),
    "EMAIL_HOST_PASSWORD": _("Outgoing email"),
}


def unauthenticated_integrations() -> list[dict]:
    """Integrations this shop had set up and no longer has a key for (§17.1).

    A restored instance lands here for every one of them, because credentials
    are stripped from backups and exports on purpose. Saying so is the whole
    point: the alternative is an integration retrying forever and filling the
    log with authentication failures nobody connects to last night's restore.

    Answered from the markers rather than from configuration, because for most
    of these the key *is* the on-switch — with the table empty there is nothing
    else left to infer from.
    """
    from django.db import DatabaseError

    from .models import Setting

    stored = configured_credentials()
    try:
        marked = {
            row.removeprefix(CONFIGURED_PREFIX)
            for row in Setting.objects.filter(
                key__startswith=CONFIGURED_PREFIX
            ).values_list("key", flat=True)
        }
    except DatabaseError:
        return []

    needing = [
        {"key": key, "name": CREDENTIAL_NAMES.get(key, key)}
        for key in sorted(marked - stored)
    ]

    # One case the markers cannot cover: a key that only ever came from the
    # environment, on an instance restored onto a host without that `.env`.
    if current("PLATE_LOOKUP_ENABLED") and not current("PLATE_LOOKUP_KEY"):
        if not any(item["key"] == "PLATE_LOOKUP_KEY" for item in needing):
            needing.append(
                {"key": "PLATE_LOOKUP_KEY", "name": CREDENTIAL_NAMES["PLATE_LOOKUP_KEY"]}
            )

    return needing


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------


def save(values: dict[str, Any], *, user=None, source: str = "web") -> list[str]:
    """Validate and store a batch of settings. Returns the keys that changed.

    Batched rather than one at a time because a settings form posts a whole
    group, and half-applying it on the fourth field's validation error would
    leave the instance in a state nobody chose.
    """
    from .models import AuditLog, Setting

    cleaned: dict[str, Any] = {}
    errors: dict[str, str] = {}

    for key, raw in values.items():
        entry = BY_KEY.get(key)
        if entry is None:
            continue
        try:
            cleaned[key] = coerce(entry, raw)
        except ValidationError as exc:
            errors[key] = " ".join(exc.messages)

    if errors:
        raise ValidationError(errors)

    changed: list[str] = []
    for key, value in cleaned.items():
        before = current(key)
        if before == value:
            continue
        changed.append(key)
        if key in SECRET_KEYS:
            credential_set(key, str(value))
        else:
            Setting.put(key, value)

        AuditLog.objects.create(
            entity_type="Setting",
            action=AuditLog.Action.UPDATE,
            user=user if getattr(user, "pk", None) else None,
            source=source,
            summary=key,
            # §17.1: a credential is recorded as *changed*, never quoted. The
            # audit log answers "who turned this off", not "what was the key".
            diff=(
                {"secret": True, "was_set": bool(before), "now_set": bool(value)}
                if key in SECRET_KEYS
                else {"from": before, "to": value}
            ),
        )

    if changed:
        invalidate()
        if any(key in RESTART_KEYS for key in changed):
            bump_generation()
    return changed


# ---------------------------------------------------------------------------
# Making a change take effect (§17.2)
# ---------------------------------------------------------------------------


def stored_generation() -> int:
    from django.db import DatabaseError

    from .models import Setting

    try:
        return int(Setting.get(GENERATION_KEY, 0) or 0)
    except (DatabaseError, TypeError, ValueError):
        return 0


def bump_generation() -> int:
    from .models import Setting

    value = stored_generation() + 1
    Setting.put(GENERATION_KEY, value)
    invalidate()
    return value


def record_generation() -> None:
    """Remember, at boot, which configuration this process is running."""
    global _booted_generation
    _booted_generation = stored_generation()


def booted_generation() -> int | None:
    return _booted_generation


def is_stale() -> bool:
    """True when this process is running configuration that has since changed."""
    if _booted_generation is None:
        return False
    return stored_generation() != _booted_generation


def pending_restart_keys() -> list[str]:
    """Which restart-class settings were edited since this process booted.

    Read from the audit log rather than tracked separately: it already records
    every change with a timestamp, and a second bookkeeping table for the same
    facts is a second thing to get out of step.
    """
    if not is_stale():
        return []
    from .models import AuditLog, Setting

    marker = Setting.objects.filter(key=GENERATION_KEY).values_list("updated_at", flat=True).first()
    if marker is None:
        return []
    recent = (
        AuditLog.objects.filter(entity_type="Setting", action=AuditLog.Action.UPDATE)
        .order_by("-at")
        .values_list("summary", flat=True)[:50]
    )
    return sorted({key for key in recent if key in RESTART_KEYS})


def restart_web() -> bool:
    """Ask gunicorn to reload its workers (§17.2).

    `SIGHUP` to the master retires the workers gracefully — in-flight requests
    finish, new workers import fresh settings — so this is a reload, not an
    outage. It needs `--pid` on the command line; without it there is nothing
    to signal and the UI says to restart the container instead, which is the
    honest answer rather than a button that silently does nothing.
    """
    pidfile = getattr(django_settings, "GUNICORN_PIDFILE", "")
    if not pidfile or not os.path.exists(pidfile):
        return False
    try:
        with open(pidfile) as handle:
            pid = int(handle.read().strip())
        os.kill(pid, signal.SIGHUP)
    except (OSError, ValueError) as exc:
        log.warning("could not signal gunicorn: %s", exc)
        return False
    log.info("sent SIGHUP to gunicorn master %s", pid)
    return True


_overlay_applied = False


def ensure_overlay() -> None:
    """Apply the restart-class settings once per process, on first use.

    Not in `AppConfig.ready()`, which is the obvious place and the wrong one:
    `ready()` runs for every management command including the `migrate` that
    creates the table it would read, and Django warns about database access
    there for precisely that reason. First request and worker start are both
    after the database exists, and both are still before anything reads these
    values — which is what makes them restart-class rather than broken.
    """
    global _overlay_applied
    if _overlay_applied:
        return
    _overlay_applied = True
    try:
        apply_restart_overlay()
    except Exception:  # pragma: no cover - never fail a request over this
        # The environment is a complete configuration on its own, so the
        # instance is correct without the overlay, just not yet reconfigured.
        log.exception("could not apply stored settings")


def apply_restart_overlay() -> None:
    """Push restart-class values into Django's own settings.

    These are the entries Django resolves for itself — the locale set, the
    upload ceiling, the timezone — and reading them through `conf` at the call
    site is not possible because the call site is inside Django.
    """
    overlay = _overlay()
    for key in RESTART_KEYS:
        if key not in overlay:
            continue
        value = overlay[key]
        setattr(django_settings, key, value)
        if key == "MAX_UPLOAD_MB":
            django_settings.DATA_UPLOAD_MAX_MEMORY_SIZE = int(value) * 1024 * 1024
    record_generation()
