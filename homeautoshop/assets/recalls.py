"""
NHTSA recall lookup (SPEC §8.4, FR-VEH-*).

Three honest limitations shape this module, and the UI must carry all of them:

1. The free NHTSA API is queried **by year/make/model, not by VIN**. Results are
   campaigns that *may* apply, not confirmed open recalls for this vehicle.
2. **VIN-level completion status is not available.** `owner_status` is therefore
   operator-maintained; the app links out and lets you record what you found.
3. **Coverage is US-only.** Canada and Mexico have separate systems that are not
   wired up. A Canadian operator sees "unavailable for this region" — an empty
   recall list must never be mistaken for a clean vehicle.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from urllib.parse import quote

from django.utils.translation import gettext_lazy as _

from homeautoshop.core.outbound import OutboundBlocked, OutboundFailed, fetch_json
from homeautoshop.core.runtime import conf

from .models import Recall

log = logging.getLogger(__name__)

NHTSA_RECALLS_URL = "https://api.nhtsa.gov/recalls/recallsByVehicle"

# Where a plate is registered decides which authority to ask. Only the US is
# implemented; the rest are named so the gap is visible rather than silent.
SUPPORTED_REGIONS = {"US"}
REGION_AUTHORITIES = {
    "CA": _("Transport Canada"),
    "MX": _("PROFECO"),
}

# Canadian and Mexican plate/region codes we can recognize well enough to say
# "not supported" rather than returning a misleading empty list.
CA_PROVINCES = {"AB", "BC", "MB", "NB", "NL", "NS", "NT", "NU", "ON", "PE", "QC", "SK", "YT"}


@dataclass(slots=True)
class RecallCheck:
    ok: bool = False
    created: int = 0
    total: int = 0
    message: str = ""
    region_unsupported: bool = False
    #: An empty answer that might equally have been a rate limit. Shown as a
    #: warning, never as "no recalls" — see `check` for the measurement.
    inconclusive: bool = False
    campaigns: list = field(default_factory=list)


def region_of(asset) -> str:
    code = (asset.plate_region or "").strip().upper()
    if code in CA_PROVINCES:
        return "CA"
    return "US"


def check(asset, *, user=None) -> RecallCheck:
    """Fetch campaigns for this vehicle's year/make/model."""
    if not conf.RECALLS_ENABLED:
        # Said plainly rather than returning an empty list. On a safety feature
        # "no recalls found" and "nobody looked" must never look the same.
        return RecallCheck(
            message=str(_("Recall checking is switched off in this shop's settings."))
        )
    if asset.asset_kind != "vehicle":
        return RecallCheck(message=str(_("Equipment is not covered by vehicle recalls.")))
    if not (asset.year and asset.make and asset.model):
        return RecallCheck(
            message=str(_("A year, make and model are needed before recalls can be looked up."))
        )

    region = region_of(asset)
    if region not in SUPPORTED_REGIONS:
        authority = REGION_AUTHORITIES.get(region, region)
        return RecallCheck(
            region_unsupported=True,
            message=str(
                _(
                    "Recall data here comes from NHTSA, which covers US-market vehicles. "
                    "This vehicle is registered in a region served by %(authority)s, which is "
                    "not wired up — so an empty list here would not mean the vehicle is clear."
                )
            )
            % {"authority": authority},
        )

    # Encoded, because a model is free text: "F-150" is fine and "Grand Cherokee"
    # is not, and an unencoded space produces a malformed request.
    url = (
        f"{NHTSA_RECALLS_URL}?make={quote(asset.make)}"
        f"&model={quote(asset.model)}&modelYear={quote(str(asset.year))}"
    )
    try:
        payload, empty_with_400 = _fetch(url, user=user)
    except OutboundBlocked as exc:
        return RecallCheck(message=str(exc))
    except OutboundFailed:
        return RecallCheck(
            message=str(_("Could not reach the recall service. Try again later."))
        )

    results = payload.get("results") if isinstance(payload, dict) else None
    if results is None:
        return RecallCheck(message=str(_("The recall service returned nothing usable.")))

    if not results:
        # **This outcome is genuinely ambiguous and is reported as such.**
        #
        # NHTSA answers a vehicle with no campaigns with HTTP 400 and a body
        # that reads `{"Count": 0, "Message": "Results returned successfully",
        # "results": []}`. It answers a *rate-limited* request with exactly the
        # same status and exactly the same body. Measured: a 2020 Subaru
        # Outback returns 400/0 inside a burst and 200 with six campaigns after
        # a pause — same query, same casing.
        #
        # So a 400 cannot be read as "no recalls". On a safety feature the only
        # honest answer is to say which of the two it might be. Treating it as
        # a clean bill of health would be the worst failure this module could
        # have: silent, confident, and wrong in the dangerous direction.
        return RecallCheck(
            ok=not empty_with_400,
            inconclusive=empty_with_400,
            total=Recall.objects.filter(asset=asset).count(),
            message=str(
                _(
                    "NHTSA returned no campaigns — but it answers exactly the same way when "
                    "it is rate-limiting, so this is not a clean bill of health. Try again "
                    "in a minute, especially if you have just checked several vehicles."
                )
                if empty_with_400
                else _(
                    "NHTSA lists no campaigns for this year, make and model. That is not the "
                    "same as the vehicle being clear — coverage is US-market only, and a "
                    "campaign issued tomorrow will not be here until you look again."
                )
            ),
        )

    created = 0
    for row in results:
        campaign = (row.get("NHTSACampaignNumber") or "").strip()
        if not campaign:
            continue
        _obj, was_created = Recall.objects.update_or_create(
            asset=asset,
            campaign_number=campaign,
            defaults={
                "nhtsa_id": str(row.get("NHTSAActionNumber") or "")[:48],
                "reported_on": _parse_date(row.get("ReportReceivedDate")),
                "component": (row.get("Component") or "")[:200],
                "summary": row.get("Summary") or "",
                "consequence": row.get("Consequence") or "",
                "remedy": row.get("Remedy") or "",
                "source": "nhtsa",
            },
        )
        created += was_created

    total = Recall.objects.filter(asset=asset).count()
    return RecallCheck(
        ok=True,
        created=created,
        total=total,
        campaigns=results,
        message=str(
            _(
                "%(n)d campaign(s) found for this year, make and model. These may or may not "
                "apply to your VIN — NHTSA's free data cannot tell us, so mark each one "
                "yourself after checking."
            )
        )
        % {"n": len(results)},
    )


def _fetch(url: str, *, user=None) -> tuple[dict, bool]:
    """Ask NHTSA, once, then once more if the answer was an empty 400.

    The retry is what turns some rate limits back into real answers: the same
    query that returns 400 inside a burst returns 200 with results a moment
    later. It cannot resolve every case — two 400s still mean "no campaigns, or
    still limited" — so the caller is told which kind of empty it got.
    """
    import time

    attempted_retry = False
    while True:
        try:
            response = fetch_json(url, timeout=10, purpose="recalls.check", user=user)
        except OutboundFailed as exc:
            body = exc.body if isinstance(exc.body, dict) else None
            if body is None or "results" not in body:
                raise
            if body.get("results") and not attempted_retry:
                return body, False
            if attempted_retry:
                return body, True
            attempted_retry = True
            # Short enough that nobody notices, long enough to clear a burst.
            time.sleep(1.5)
            continue
        return response.data or {}, False


def _parse_date(value):
    if not value:
        return None
    for fmt in ("%d/%m/%Y", "%m/%d/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(str(value)[:10], fmt).date()
        except ValueError:
            continue
    return None


def vin_lookup_url(asset) -> str:
    """Where the operator goes to check completion for *this* VIN.

    We cannot answer this ourselves, so we send them somewhere that can and let
    them record the answer (FR-VEH-*, §8.4).
    """
    # NHTSA's VIN search takes the 17-character form and nothing else, so a
    # pre-1981 VIN goes to the plain page rather than to a link that lands on
    # an error — and campaigns that old are mostly not in there anyway.
    from .vin import VIN_LENGTH

    if len(asset.vin) != VIN_LENGTH:
        return "https://www.nhtsa.gov/recalls"
    return f"https://www.nhtsa.gov/recalls?vin={asset.vin}"
