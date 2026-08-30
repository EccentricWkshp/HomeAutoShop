"""
LubeLogger client and importer (SPEC §8.6, INTEGRATION-LUBELOGGER.md).

Scope discipline: **LubeLogger is optional and additive, never a dependency.**
HomeAutoShop owns the maintenance schedule and every core function outright; an
instance with none configured is not a degraded instance. This module exists to
spare the operator from re-keying years of history, and nothing else may come
to depend on it.

The single most dangerous thing here is the locale hazard. LubeLogger returns
**locale-formatted strings** by default, so a `1.234,56` fuel cost silently
imports as `1.23`. Two defences, because one is not enough:

1. The `culture-invariant` header is always sent, and the connection check
   refuses to proceed if the response does not look invariant.
2. Every number is parsed **strictly**. A comma-decimal raises rather than
   truncating, so a mis-configured instance fails loudly instead of quietly
   producing wrong money.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from urllib.parse import urlencode

from django.conf import settings
from django.utils.translation import gettext_lazy as _

from homeautoshop.core.outbound import OutboundBlocked, OutboundFailed, fetch_json

log = logging.getLogger(__name__)

SOURCE = "lubelogger"

# Endpoint paths are pinned here rather than discovered at run time. LubeLogger
# self-documents at {base}/api, and these were written against its documented
# record types — verify them against the running instance before a real import
# (LL-Q1). A 404 on one type is reported and skipped, never fatal.
ENDPOINTS = {
    "vehicles": "/api/vehicles",
    "odometer": "/api/vehicle/odometerrecords",
    "service": "/api/vehicle/servicerecords",
    "repair": "/api/vehicle/repairrecords",
    "upgrade": "/api/vehicle/upgraderecords",
    "fuel": "/api/vehicle/gasrecords",
    "tax": "/api/vehicle/taxrecords",
    "plan": "/api/vehicle/planrecords",
    "note": "/api/vehicle/notes",
}

# Record types that become completed work orders, and the type they map to.
WORK_ORDER_KINDS = {
    "service": "maintenance",
    "repair": "repair",
    "upgrade": "modification",
}

# LubeLogger tax records cover recurring ownership costs.
TAX_CATEGORY_HINTS = (
    ("registration", "registration"),
    ("plate", "registration"),
    ("tag", "registration"),
    ("inspect", "inspection"),
    ("insur", "insurance"),
)

_COMMA_DECIMAL = re.compile(r"^-?\d{1,3}(\.\d{3})*,\d+$|^-?\d+,\d{1,2}$")


class LocaleFormatError(ValueError):
    """The instance is returning locale-formatted numbers. Refuse to guess."""


class NotConfigured(RuntimeError):
    pass


def parse_number(value, *, field_name: str = "value") -> Decimal:
    """Parse a number strictly, in invariant format only.

    A comma-decimal is refused rather than coerced: importing `1.234,56` as
    `1.23` is the kind of error nobody ever notices, and it corrupts every cost
    report downstream.
    """
    if value in (None, ""):
        return Decimal(0)
    if isinstance(value, (int, float, Decimal)):
        return Decimal(str(value))

    text = str(value).strip().replace("$", "").replace("\xa0", "").replace(" ", "")
    if _COMMA_DECIMAL.match(text):
        raise LocaleFormatError(
            f"{field_name}={value!r} is locale-formatted. Send the culture-invariant "
            "header, or set LUBELOGGER_INVARIANT_API=true on the LubeLogger instance."
        )
    text = text.replace(",", "")  # invariant thousands separators are safe to drop
    try:
        return Decimal(text)
    except InvalidOperation as exc:
        raise LocaleFormatError(f"{field_name}={value!r} is not a number") from exc


def parse_date(value) -> date | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f", "%m/%d/%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(text[: len(fmt) + 6], fmt).date()
        except ValueError:
            continue
    # Ambiguous formats (is 03/04 March or April?) are exactly what the
    # invariant header exists to avoid, so refuse rather than guess.
    raise LocaleFormatError(f"Unrecognized date {value!r}")


def pick(row: dict, *names: str, default=None):
    """LubeLogger's JSON casing varies by endpoint and version."""
    lowered = {k.lower().replace("_", ""): v for k, v in row.items()}
    for name in names:
        key = name.lower().replace("_", "")
        if key in lowered and lowered[key] not in (None, ""):
            return lowered[key]
    return default


@dataclass(slots=True)
class Diagnosis:
    reachable: bool = False
    authenticated: bool = False
    invariant: bool = False
    vehicle_count: int = 0
    message: str = ""
    detail: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.reachable and self.authenticated and self.invariant


def instance_url() -> str:
    """The configured instance, normalized.

    `external_ref.source_instance_url` scopes a pairing to one LubeLogger — ids
    mean different things on a different instance — so the code that *writes* a
    link and the code that *reads* it have to agree on this string exactly. One
    function, so they cannot drift.
    """
    return (settings.LUBELOGGER_URL or "").rstrip("/")


class LubeLoggerClient:
    def __init__(self, base_url: str | None = None, api_key: str | None = None) -> None:
        self.base_url = (base_url or instance_url()).rstrip("/")
        self.api_key = api_key or settings.LUBELOGGER_API_KEY
        if not self.base_url:
            raise NotConfigured(
                "LUBELOGGER_URL is not set. The integration is optional; nothing "
                "else in HomeAutoShop depends on it."
            )

    @property
    def headers(self) -> dict[str, str]:
        return {
            "x-api-key": self.api_key,
            # Without this, numbers and dates come back locale-formatted.
            "culture-invariant": "true",
            "Accept": "application/json",
        }

    def get(self, path: str, **params) -> list[dict]:
        # `urlencode`, not string concatenation. A WrenchLedger watermark is an
        # ISO timestamp ending `+00:00`, and an unencoded `+` arrives at the
        # server as a space — which it rejects as a validation error, so every
        # sync after the first one failed with HTTP 400. The same hazard applies
        # to any search text a person types.
        query = urlencode({k: v for k, v in params.items() if v not in (None, "")})
        url = f"{self.base_url}{path}" + (f"?{query}" if query else "")
        response = fetch_json(url, headers=self.headers, purpose=f"lubelogger{path}", timeout=20)
        data = response.data
        if isinstance(data, dict):
            data = data.get("data") or data.get("records") or []
        return data if isinstance(data, list) else []

    def vehicles(self) -> list[dict]:
        return self.get(ENDPOINTS["vehicles"])

    def records(self, kind: str, vehicle_id) -> list[dict]:
        path = ENDPOINTS.get(kind)
        if not path:
            return []
        return self.get(path, vehicleId=vehicle_id)

    def push_odometer(self, vehicle_id, *, value, on, note: str = "") -> None:
        """Write one odometer reading back (FR-INT-16, MAY, opt-in).

        The only write this integration ever makes, and it needs an
        Editor-scoped key — a read-only key returns 401 here while every other
        call keeps working, so the failure is per-record and logged rather than
        being mistaken for the instance being down.
        """
        from homeautoshop.core.outbound import post_json

        post_json(
            f"{self.base_url}{ENDPOINTS['odometer']}/add",
            {
                "vehicleId": vehicle_id,
                "date": on.isoformat() if hasattr(on, "isoformat") else str(on),
                "odometer": str(value),
                "notes": note,
            },
            headers=self.headers,
            purpose="lubelogger odometer push",
            timeout=20,
        )

    def check(self) -> Diagnosis:
        """Verify reachability, credentials, and invariant formatting.

        The invariant check is not optional politeness — importing without it
        produces wrong money, so a failure here stops the import.
        """
        result = Diagnosis()
        try:
            vehicles = self.vehicles()
        except OutboundBlocked as exc:
            result.message = str(exc)
            return result
        except OutboundFailed as exc:
            result.message = str(
                _("Could not reach %(url)s (%(err)s).")
            ) % {"url": self.base_url, "err": exc}
            return result

        result.reachable = True
        result.authenticated = True  # a 401/403 would have raised above
        result.vehicle_count = len(vehicles)

        offenders = [
            f"{k}={v!r}"
            for row in vehicles[:5]
            for k, v in row.items()
            if isinstance(v, str) and _COMMA_DECIMAL.match(v.strip())
        ]
        if offenders:
            result.invariant = False
            result.message = str(
                _(
                    "This instance is returning locale-formatted numbers (%(sample)s). "
                    "Importing would silently corrupt costs, so it is refused. Set "
                    "LUBELOGGER_INVARIANT_API=true on the LubeLogger side."
                )
            ) % {"sample": ", ".join(offenders[:3])}
            return result

        result.invariant = True
        result.message = str(_("Reachable, authenticated, and returning invariant values."))
        return result
