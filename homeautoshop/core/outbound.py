"""
Outbound HTTP, with the guardrails from SPEC §12.3 and §12.4 applied in one place.

Every call an integration makes goes through :func:`fetch_json`, so the rules
are enforced rather than remembered:

* **Offline Mode** (NFR-S-2) disables all outbound traffic. Features present
  themselves as intentionally unavailable, not broken.
* **The allowlist is the control, not the address range** — a configured
  integration host is permitted even on a private network (LubeLogger on the
  LAN is the motivating case), while everything else is refused.
* Redirects are never followed across hosts.
* Every call is logged and reviewable by the operator (FR-INT-2).
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from dataclasses import dataclass
from urllib.parse import urlparse

from django.conf import settings
from django.utils.translation import gettext_lazy as _

log = logging.getLogger(__name__)


class OutboundBlocked(Exception):
    """The request was refused before any socket was opened."""


class OutboundFailed(Exception):
    """The request was attempted and did not produce a usable answer.

    Carries the status and the response body where there was one. An API that
    answers 403 with `{"code": "FEATURE_NOT_IN_PLAN"}` is saying something
    completely different from one that answers 403 with a bad key, and the
    operator needs a different sentence for each — so the body is kept rather
    than flattened to "HTTP 403".
    """

    def __init__(self, message, *, status: int = 0, body: dict | None = None) -> None:
        self.status = status
        self.body = body or {}
        super().__init__(message)


@dataclass(slots=True)
class Response:
    status: int
    data: dict | list | None
    elapsed_ms: int


def _host_allowed(host: str) -> bool:
    host = (host or "").lower()
    for entry in settings.OUTBOUND_ALLOWLIST:
        entry = entry.lower().strip()
        if host == entry or host.endswith(f".{entry}"):
            return True
    return False


def fetch_json(
    url: str,
    *,
    timeout: int | None = None,
    headers: dict[str, str] | None = None,
    purpose: str = "",
    user=None,
) -> Response:
    """GET a JSON document, subject to every outbound guardrail."""
    import time

    from .models import AuditLog

    parsed = urlparse(url)
    detail: dict | None = None
    if parsed.scheme not in ("http", "https"):
        raise OutboundBlocked(_("Only http and https are permitted."))

    if settings.OFFLINE_MODE:
        raise OutboundBlocked(
            _("Offline Mode is on, so no outbound requests are made. Enter the details by hand.")
        )

    if not _host_allowed(parsed.hostname or ""):
        raise OutboundBlocked(
            _("%(host)s is not on the outbound allowlist.") % {"host": parsed.hostname}
        )

    request = urllib.request.Request(
        url,
        headers={"Accept": "application/json", "User-Agent": "HomeAutoShop/1.0", **(headers or {})},
        method="GET",
    )

    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, hdrs, newurl):  # noqa: D102
            # Never follow a redirect: a cross-host hop is how an allowlist
            # gets circumvented, and some APIs drop auth headers across one.
            raise OutboundBlocked(_("Redirect refused (%(url)s).") % {"url": newurl})

    opener = urllib.request.build_opener(_NoRedirect)
    started = time.monotonic()
    status, payload, error = 0, None, ""
    try:
        with opener.open(request, timeout=timeout or settings.VIN_DECODE_TIMEOUT) as resp:
            status = resp.status
            payload = json.loads(resp.read().decode("utf-8"))
    except OutboundBlocked:
        raise
    except urllib.error.HTTPError as exc:
        status, error = exc.code, f"HTTP {exc.code}"
        try:
            detail = json.loads(exc.read().decode("utf-8"))
        except Exception:
            detail = None
    except Exception as exc:  # timeouts, DNS, malformed JSON
        error = type(exc).__name__
    finally:
        elapsed = int((time.monotonic() - started) * 1000)
        AuditLog.objects.create(
            entity_type="Outbound",
            action=AuditLog.Action.OUTBOUND,
            user=user if getattr(user, "pk", None) else None,
            summary=f"{parsed.hostname}{parsed.path}"[:255],
            diff={
                "purpose": purpose,
                "status": status,
                "elapsed_ms": elapsed,
                "error": error,
                "host": parsed.hostname,
            },
        )

    if error or payload is None:
        raise OutboundFailed(error or _("Empty response."), status=status, body=detail)
    return Response(status=status, data=payload, elapsed_ms=elapsed)


def post_json(
    url: str,
    payload: dict,
    *,
    timeout: int = 10,
    headers: dict[str, str] | None = None,
    purpose: str = "",
    user=None,
) -> Response:
    """POST a JSON document, under the same guardrails as :func:`fetch_json`.

    Used by outbound webhooks. The allowlist still applies — a webhook target
    is an operator-configured integration host, which is exactly the case §12.3
    permits on a private network while refusing everything else.
    """
    import time

    from .models import AuditLog

    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise OutboundBlocked(_("Only http and https are permitted."))
    if settings.OFFLINE_MODE:
        raise OutboundBlocked(_("Offline Mode is on, so no outbound requests are made."))
    if not _host_allowed(parsed.hostname or ""):
        raise OutboundBlocked(
            _("%(host)s is not on the outbound allowlist.") % {"host": parsed.hostname}
        )

    body = json.dumps(payload, default=str).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "User-Agent": "HomeAutoShop/1.0",
            **(headers or {}),
        },
        method="POST",
    )

    started = time.monotonic()
    status, data, error = 0, None, ""
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            status = resp.status
            raw = resp.read()
            data = json.loads(raw) if raw.strip().startswith(b"{") else None
    except urllib.error.HTTPError as exc:
        status, error = exc.code, f"HTTP {exc.code}"
    except Exception as exc:
        error = type(exc).__name__
    finally:
        elapsed = int((time.monotonic() - started) * 1000)
        AuditLog.objects.create(
            entity_type="Outbound",
            action=AuditLog.Action.OUTBOUND,
            user=user if getattr(user, "pk", None) else None,
            summary=f"POST {parsed.hostname}{parsed.path}"[:255],
            diff={"purpose": purpose, "status": status, "elapsed_ms": elapsed, "error": error},
        )

    if error:
        raise OutboundFailed(error)
    return Response(status=status, data=data, elapsed_ms=elapsed)
