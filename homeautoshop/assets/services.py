"""
Asset services — VIN decode and meter capture (SPEC §8.1, FR-VEH-3/4/8/9).

The decode rules that matter:

* Invoked only on explicit user action, never on page load or a background job.
* Hard timeout with a fall-through to manual entry — a failed decode never
  blocks creating the vehicle (P-7).
* The **raw response is retained**, so a later mapping improvement can re-derive
  without another call.
* A user-edited field is **never** overwritten by a re-decode (FR-VEH-4).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from django.conf import settings
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from homeautoshop.core.outbound import OutboundBlocked, OutboundFailed, fetch_json

from . import vin as vinlib
from .models import Asset, UsageReading
from homeautoshop.core.runtime import conf

log = logging.getLogger(__name__)

# vPIC field -> Asset field. Kept small and explicit; the raw response is
# stored alongside so nothing is lost by mapping conservatively.
VPIC_FIELD_MAP = {
    "ModelYear": "year",
    "Make": "make",
    "Model": "model",
    "Trim": "trim",
    "BodyClass": "body_style",
    "FuelTypePrimary": "fuel_type",
    "TransmissionStyle": "transmission",
    "DriveType": "drivetrain",
}


@dataclass(slots=True)
class DecodeResult:
    ok: bool
    applied: dict[str, object] = field(default_factory=dict)
    skipped_overridden: list[str] = field(default_factory=list)
    message: str = ""
    thin: bool = False

    @property
    def summary(self) -> str:
        if not self.ok:
            return self.message
        if not self.applied:
            return str(_("Nothing new to fill in — the details already on file were kept."))
        return str(_("Filled in %(n)d field(s) from the VIN.")) % {"n": len(self.applied)}


def _engine_from(payload: dict) -> str:
    displacement = (payload.get("DisplacementL") or "").strip()
    cylinders = (payload.get("EngineCylinders") or "").strip()
    config = (payload.get("EngineConfiguration") or "").strip()
    bits = []
    if displacement:
        try:
            bits.append(f"{float(displacement):.1f}L")
        except ValueError:
            bits.append(f"{displacement}L")
    if config and cylinders:
        bits.append(f"{config[0].upper()}{cylinders}")
    elif cylinders:
        bits.append(f"{cylinders}-cyl")
    return " ".join(bits)


def decode_vin(asset: Asset, *, user=None, force: bool = False, save: bool = True) -> DecodeResult:
    """Decode this asset's VIN via NHTSA vPIC and fill in blanks only.

    `save=False` decodes into the instance and leaves it unsaved, which is
    what the add-a-vehicle form needs: the lookup has to be offerable
    *before* the record exists, or the only way to get decoded details is
    to save a half-empty vehicle first and then remember to come back.
    """
    if asset.asset_kind != "vehicle":
        return DecodeResult(False, message=str(_("Equipment does not have a VIN to decode.")))
    if not conf.VIN_DECODE_ENABLED:
        return DecodeResult(False, message=str(_("VIN decoding is turned off for this instance.")))

    check = vinlib.validate(asset.vin, year=asset.year)
    if check.is_pre_1981 and check.is_well_formed:
        # Offered and failing would be worse than not offered: vPIC only knows
        # the 17-character format, and asking it about eleven characters
        # returns a confident nothing.
        return DecodeResult(
            False,
            message=str(
                _(
                    "A pre-1981 VIN cannot be looked up — the service only knows the "
                    "17-character format. Fill in what you know by hand."
                )
            ),
        )
    if not check.is_well_formed:
        return DecodeResult(
            False,
            message=str(_("That VIN is not well formed, so there is nothing to look up.")),
        )

    # The Extended route, though the name oversells it: measured against
    # three unrelated VINs it returns the same 154 keys as DecodeVinValues and
    # populates exactly three more — NCSAMake, NCSAModel and NCSABodyType,
    # which are crash-database classifications. It costs nothing to ask for
    # them, and they land in `decoded_raw` with everything else.
    #
    # The information a home shop actually wants is already in the ordinary
    # response: 30-40 populated fields, of which this maps 8. The rest is
    # surfaced by `Asset.decoded_details()` rather than thrown away.
    url = f"{settings.VPIC_BASE_URL}/DecodeVinValuesExtended/{asset.vin}?format=json"
    if asset.year:
        url += f"&modelyear={asset.year}"

    try:
        response = fetch_json(
            url, timeout=settings.VIN_DECODE_TIMEOUT, purpose="vin.decode", user=user
        )
    except OutboundBlocked as exc:
        return DecodeResult(False, message=str(exc))
    except OutboundFailed:
        # Degrade, don't block (P-7).
        return DecodeResult(
            False,
            message=str(
                _("Could not reach the VIN service. Enter what you know and try the lookup later.")
            ),
        )

    results = (response.data or {}).get("Results") or []
    if not results:
        return DecodeResult(False, message=str(_("The VIN service returned nothing for that VIN.")))
    payload = results[0]

    asset.decoded_raw = payload
    asset.decode_source = "vpic"
    asset.decoded_at = timezone.now()

    applied: dict[str, object] = {}
    skipped: list[str] = []
    overrides = asset.field_overrides or {}

    for source_key, target in VPIC_FIELD_MAP.items():
        value = (payload.get(source_key) or "").strip()
        if not value:
            continue
        if target in overrides and not force:
            skipped.append(target)
            continue
        current = getattr(asset, target)
        if target == "year":
            try:
                value = int(value)
            except ValueError:
                continue
        if current in ("", None) or force:
            setattr(asset, target, value)
            applied[target] = value

    if engine := _engine_from(payload):
        if "engine" not in overrides and (not asset.engine or force):
            asset.engine = engine
            applied["engine"] = engine

    if not asset.vehicle_class and (body := (payload.get("BodyClass") or "").lower()):
        if "motorcycle" in body:
            asset.vehicle_class = "motorcycle"
        elif "trailer" in body:
            asset.vehicle_class = "trailer"
        elif "truck" in body or "pickup" in body:
            asset.vehicle_class = "truck"
        elif body:
            asset.vehicle_class = "car"

    if save:
        asset.save()

    # vPIC is authoritative for US-market vehicles from roughly 1981 onward.
    # A thin decode on a gray-market or heavily modified vehicle is normal, and
    # must be presented as normal rather than as an error (SPEC §8.1).
    thin = len(applied) <= 1
    result = DecodeResult(True, applied=applied, skipped_overridden=skipped, thin=thin)
    if thin:
        result.message = str(
            _(
                "That VIN decoded thinly. This is normal for imported, pre-1981, or unusual "
                "vehicles — fill in the rest by hand."
            )
        )
    return result


def mark_override(asset: Asset, field_name: str, value) -> None:
    """Record that a human corrected a decoded field, so a re-decode leaves it alone."""
    overrides = dict(asset.field_overrides or {})
    overrides[field_name] = {
        "value": str(value),
        "at": timezone.now().isoformat(),
        "was": str((asset.decoded_raw or {}).get(field_name, "")),
    }
    asset.field_overrides = overrides


def record_reading(
    asset: Asset,
    value,
    *,
    unit: str | None = None,
    read_on=None,
    source: str = UsageReading.Source.MANUAL,
    note: str = "",
    user=None,
) -> UsageReading:
    """Append a meter reading. Never rejects a decrease — flags it (FR-VEH-9)."""
    reading = UsageReading(
        asset=asset,
        meter=asset.meter,
        value=value,
        unit=unit or asset.meter_unit,
        read_on=read_on or timezone.localdate(),
        source=source,
        note=note,
        created_by=user if getattr(user, "pk", None) else None,
    )
    reading.save()
    return reading
