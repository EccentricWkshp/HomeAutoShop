"""
License plate → VIN (SPEC §8.2).

**There is no free, legal, general-purpose plate-to-VIN API.** This needs a
commercial vehicle-data provider, an account, and per-call cost, and coverage
and legal terms vary by jurisdiction. So no provider is bundled or endorsed:
this is an interface with adapters configured by name, base URL and key, and
the operator supplies their own.

The guardrails exist because this is the only integration in the application
that both **spends money** and **sends a plate off-box**, and either one going
unnoticed is a bad day:

1. Off by default; turning it on means entering a key and acknowledging that
   the plate leaves the network.
2. Never automatic. One call per press of a button, never on page load and
   never on a background refresh.
3. The running monthly count is shown *before* the call, not after.
4. An optional monthly cap that hard-stops.
5. Every call recorded with the plate queried and who asked (FR-INT-2).

The result is fed into the §8.1 decode path rather than trusted for vehicle
attributes directly: the provider's own year/make/model is a hint, and the VIN
is the fact.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date

from django.conf import settings
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from homeautoshop.core.models import AuditLog, Setting
from homeautoshop.core.outbound import OutboundBlocked, OutboundFailed, fetch_json

log = logging.getLogger(__name__)

COUNT_KEY = "plate_lookup.month"


class LookupUnavailable(Exception):
    """Refused before any call was made, with a reason worth showing."""


class CapReached(LookupUnavailable):
    """The configured monthly limit. A hard stop, not a warning."""


@dataclass(slots=True)
class PlateResult:
    vin: str = ""
    year: int | None = None
    make: str = ""
    model: str = ""
    confidence: float = 0.0
    raw: dict = field(default_factory=dict)

    @property
    def usable(self) -> bool:
        return bool(self.vin)


class PlateLookupProvider:
    """`lookup(plate, region) -> PlateResult`.

    Adapters differ only in where the fields live in the response, so the
    mapping is data (`RESPONSE_MAP`) rather than a subclass per vendor. A new
    provider is usually a `.env` entry, not a code change.
    """

    #: Where the answer lives, per provider, in dotted paths. Deliberately
    #: sparse: this is a shape nobody can verify without an account, so what is
    #: here is a starting point the operator corrects, not a claim of support.
    RESPONSE_MAP: dict[str, dict[str, str]] = {
        "generic": {
            "vin": "vin",
            "year": "year",
            "make": "make",
            "model": "model",
        },
    }

    def __init__(self, name: str = "", base_url: str = "", api_key: str = "") -> None:
        self.name = name or settings.PLATE_LOOKUP_PROVIDER
        self.base_url = (base_url or settings.PLATE_LOOKUP_URL).rstrip("/")
        self.api_key = api_key or settings.PLATE_LOOKUP_KEY

    def lookup(self, plate: str, region: str, *, user=None) -> PlateResult:
        if not self.base_url:
            raise LookupUnavailable(_("No plate-lookup provider is configured."))

        url = (
            self.base_url.replace("{plate}", plate).replace("{region}", region)
            if "{plate}" in self.base_url
            else f"{self.base_url}?plate={plate}&region={region}"
        )
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        response = fetch_json(
            url, headers=headers, purpose=f"plate lookup {region}/{plate}", timeout=15, user=user
        )
        return self._map(response.data if isinstance(response.data, dict) else {})

    def _map(self, payload: dict) -> PlateResult:
        paths = self.RESPONSE_MAP.get(self.name) or self.RESPONSE_MAP["generic"]
        found = {key: _dig(payload, path) for key, path in paths.items()}
        year = found.get("year")
        return PlateResult(
            vin=str(found.get("vin") or "").strip().upper(),
            year=int(year) if str(year or "").isdigit() else None,
            make=str(found.get("make") or "").strip(),
            model=str(found.get("model") or "").strip(),
            # A provider's own confidence, where it gives one. Absent, the
            # result is treated as unrated rather than as certain.
            confidence=float(payload.get("confidence") or 0),
            raw=payload,
        )


def _dig(payload: dict, path: str):
    current = payload
    for part in path.split("."):
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current


# --------------------------------------------------------------------------
# The guardrails
# --------------------------------------------------------------------------


def _period(today: date | None = None) -> str:
    today = today or timezone.localdate()
    return f"{today.year}-{today.month:02d}"


def usage(*, today: date | None = None) -> int:
    """Calls made this calendar month."""
    counter = Setting.get(COUNT_KEY) or {}
    if not isinstance(counter, dict):
        return 0
    return int(counter.get(_period(today), 0))


def remaining(*, today: date | None = None) -> int | None:
    """Calls left before the cap, or None when no cap is set."""
    cap = settings.PLATE_LOOKUP_MONTHLY_CAP
    if cap <= 0:
        return None
    return max(0, cap - usage(today=today))


def _record(plate: str, region: str, user) -> None:
    counter = Setting.get(COUNT_KEY) or {}
    if not isinstance(counter, dict):
        counter = {}
    period = _period()
    counter[period] = int(counter.get(period, 0)) + 1
    # Only the current and previous month are kept. A running tally forever is
    # a growing record of which plates were looked up and when, which is not
    # something this application should accumulate.
    counter = {k: v for k, v in counter.items() if k >= _previous_period()}
    Setting.put(COUNT_KEY, counter)

    AuditLog.objects.create(
        entity_type="PlateLookup",
        action=AuditLog.Action.OUTBOUND,
        user=user if getattr(user, "pk", None) else None,
        summary=f"{region} {plate}"[:255],
        diff={"provider": settings.PLATE_LOOKUP_PROVIDER, "month_to_date": counter[period]},
    )


def _previous_period() -> str:
    today = timezone.localdate()
    year, month = (today.year, today.month - 1) if today.month > 1 else (today.year - 1, 12)
    return f"{year}-{month:02d}"


def preflight() -> dict:
    """What the confirmation screen has to say before spending anything."""
    return {
        "enabled": settings.PLATE_LOOKUP_ENABLED,
        "provider": settings.PLATE_LOOKUP_PROVIDER,
        "used": usage(),
        "cap": settings.PLATE_LOOKUP_MONTHLY_CAP,
        "remaining": remaining(),
        "cost_estimate": settings.PLATE_LOOKUP_COST_MINOR,
        "currency": settings.CURRENCY_REPORTING,
    }


def lookup(plate: str, region: str, *, user=None, provider=None) -> PlateResult:
    """One lookup, with every guardrail applied in order."""
    plate = (plate or "").strip().upper()
    region = (region or "").strip().upper()

    if not settings.PLATE_LOOKUP_ENABLED:
        raise LookupUnavailable(
            _("Plate lookup is switched off. It costs money per call, so it is off until you turn it on.")
        )
    if settings.OFFLINE_MODE:
        raise LookupUnavailable(_("Offline Mode is on, so the plate is not sent anywhere."))
    if not plate:
        raise LookupUnavailable(_("Enter a plate to look up."))
    if not region:
        raise LookupUnavailable(
            _("Say which state or province issued it — coverage and terms differ by jurisdiction.")
        )

    left = remaining()
    if left is not None and left <= 0:
        raise CapReached(
            _("The monthly limit of %(cap)d lookups is used up. Raise it or wait for next month.")
            % {"cap": settings.PLATE_LOOKUP_MONTHLY_CAP}
        )

    adapter = provider or PlateLookupProvider()
    try:
        result = adapter.lookup(plate, region, user=user)
    except OutboundBlocked as exc:
        raise LookupUnavailable(str(exc)) from exc
    except OutboundFailed as exc:
        # Counted anyway. A provider that answered — even with "no match" — has
        # almost certainly billed for it, and a counter that only tracks
        # successes understates the bill.
        _record(plate, region, user)
        raise LookupUnavailable(
            _("The provider did not answer usefully (%(err)s).") % {"err": exc}
        ) from exc

    _record(plate, region, user)
    return result
