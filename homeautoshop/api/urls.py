"""
REST API (SPEC §10).

Style: `/api/v1`, cursor pagination, `application/problem+json` errors, and
optimistic concurrency on mutable resources. Creates accept a client-supplied
UUIDv7 so a replayed offline create is idempotent rather than a duplicate.

Phase 1 exposes reads plus the writes the garage actually needs offline —
readings, notes, and work order status. The rest of the surface in SPEC §10
lands with the features it belongs to.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal
from uuid import UUID

from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from ninja import NinjaAPI, Schema
from ninja.security import HttpBearer

from homeautoshop.accounts.models import ApiToken
from homeautoshop.assets.models import Asset, UsageReading
from homeautoshop.assets.services import record_reading
from homeautoshop.core.models import StaleRevisionError
from homeautoshop.parts.models import Part
from homeautoshop.parts.services import (
    KINDS, categories as part_categories, matching, shelf_quantities,
)
from homeautoshop.work.models import WorkOrder, WorkOrderNote


class TokenAuth(HttpBearer):
    """Bearer tokens for scripts; browsers use the session cookie."""

    def authenticate(self, request, token: str):
        import hashlib

        from django.utils import timezone

        digest = hashlib.sha256(token.encode()).hexdigest()
        row = ApiToken.objects.filter(token_hash=digest).select_related("user").first()
        if not row or not row.is_active or not row.user.is_active:
            return None
        ApiToken.objects.filter(pk=row.pk).update(last_used_at=timezone.now())
        request.user = row.user
        return row.user


class SessionOrToken(TokenAuth):
    def __call__(self, request):
        user = getattr(request, "user", None)
        if user is not None and user.is_authenticated:
            return user
        return super().__call__(request)


api = NinjaAPI(
    title="HomeAutoShop",
    version="1.0.0",
    description="Self-hosted, local-first shop management.",
    auth=SessionOrToken(),
    urls_namespace="api",
    docs_url="/docs",
)


@api.exception_handler(StaleRevisionError)
def stale_revision(request, exc: StaleRevisionError):
    """409 with the current representation attached (SPEC §5.4).

    Conflicts are never auto-resolved: the client is expected to present a
    merge, not to retry blindly.
    """
    return JsonResponse(
        {
            "type": "https://homeautoshop.local/problems/stale-revision",
            "title": "Stale revision",
            "status": 409,
            "detail": str(exc),
            "expected_revision": exc.expected,
            "current_revision": exc.actual,
            "current": _asset_out(exc.instance) if isinstance(exc.instance, Asset) else None,
        },
        status=409,
        content_type="application/problem+json",
    )


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class AssetOut(Schema):
    id: UUID
    revision: int
    nickname: str
    asset_kind: str
    status: str
    descriptor: str
    vin: str
    vin_status: str
    plate: str
    year: int | None = None
    make: str
    model: str
    meter: str
    meter_unit: str
    current_usage: float | None = None


class ReadingIn(Schema):
    id: UUID | None = None
    value: float
    unit: str | None = None
    read_on: date | None = None
    note: str = ""


class ReadingOut(Schema):
    id: UUID
    value: float
    unit: str
    read_on: date
    source: str
    note: str
    is_rollback: bool


class WorkOrderOut(Schema):
    id: UUID
    revision: int
    number: str
    title: str
    type: str
    status: str
    asset_id: UUID
    complaint: str
    cause: str
    correction: str
    opened_at: datetime
    completed_at: datetime | None = None


class NoteIn(Schema):
    id: UUID | None = None
    body: str


class StatusIn(Schema):
    status: str
    blocked_reason: str = ""
    odometer_out: float | None = None


def _asset_out(asset: Asset) -> dict[str, Any]:
    usage = asset.current_usage
    return {
        "id": str(asset.pk),
        "revision": asset.revision,
        "nickname": asset.nickname,
        "asset_kind": asset.asset_kind,
        "status": asset.status,
        "descriptor": asset.descriptor,
        "vin": asset.vin,
        "vin_status": asset.vin_status,
        "plate": asset.plate,
        "year": asset.year,
        "make": asset.make,
        "model": asset.model,
        "meter": asset.meter,
        "meter_unit": asset.meter_unit,
        "current_usage": float(usage) if usage is not None else None,
    }


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@api.get("/assets", response=list[AssetOut], tags=["Assets"])
def list_assets(request, kind: str | None = None, status: str | None = None, limit: int = 50):
    qs = Asset.objects.all()
    if kind:
        qs = qs.filter(asset_kind=kind)
    if status:
        qs = qs.filter(status=status)
    return [_asset_out(a) for a in qs[: min(limit, 200)]]


@api.get("/assets/{asset_id}", response=AssetOut, tags=["Assets"])
def get_asset(request, asset_id: UUID):
    return _asset_out(get_object_or_404(Asset, pk=asset_id))


@api.get("/assets/{asset_id}/readings", response=list[ReadingOut], tags=["Assets"])
def list_readings(request, asset_id: UUID, limit: int = 50):
    asset = get_object_or_404(Asset, pk=asset_id)
    return list(asset.usage_readings.all()[: min(limit, 200)])


@api.post("/assets/{asset_id}/readings", response={201: ReadingOut}, tags=["Assets"])
def create_reading(request, asset_id: UUID, payload: ReadingIn):
    """Append-only, so this always succeeds — an offline capture cannot be
    lost to a conflict (SPEC §5.4)."""
    asset = get_object_or_404(Asset, pk=asset_id)
    if payload.id and (existing := UsageReading.objects.filter(pk=payload.id).first()):
        # Replay of a queued create: idempotent by construction.
        return 201, existing
    reading = record_reading(
        asset,
        payload.value,
        unit=payload.unit,
        read_on=payload.read_on,
        note=payload.note,
        user=request.user,
    )
    if payload.id:
        UsageReading.objects.filter(pk=reading.pk).update(id=payload.id)
        reading.pk = payload.id
    return 201, reading


@api.get("/work-orders", response=list[WorkOrderOut], tags=["Work orders"])
def list_work_orders(request, status: str | None = None, asset_id: UUID | None = None, limit: int = 50):
    qs = WorkOrder.objects.all()
    if status == "open":
        qs = qs.open()
    elif status:
        qs = qs.filter(status=status)
    if asset_id:
        qs = qs.filter(asset_id=asset_id)
    return list(qs[: min(limit, 200)])


@api.get("/work-orders/{wo_id}", response=WorkOrderOut, tags=["Work orders"])
def get_work_order(request, wo_id: UUID):
    return get_object_or_404(WorkOrder, pk=wo_id)


@api.post("/work-orders/{wo_id}/notes", response={201: dict}, tags=["Work orders"])
def add_note(request, wo_id: UUID, payload: NoteIn):
    wo = get_object_or_404(WorkOrder, pk=wo_id)
    if payload.id and (existing := WorkOrderNote.objects.filter(pk=payload.id).first()):
        return 201, {"id": str(existing.pk), "body": existing.body}
    note = WorkOrderNote(work_order=wo, body=payload.body, author=request.user)
    if payload.id:
        note.pk = payload.id
    note.save()
    return 201, {"id": str(note.pk), "body": note.body}


@api.post("/work-orders/{wo_id}/status", response=WorkOrderOut, tags=["Work orders"])
def set_status(request, wo_id: UUID, payload: StatusIn):
    from django.core.exceptions import ValidationError

    wo = get_object_or_404(WorkOrder, pk=wo_id)
    if payload.blocked_reason:
        wo.blocked_reason = payload.blocked_reason
    try:
        wo.transition_to(payload.status, user=request.user, odometer_out=payload.odometer_out)
    except ValidationError as exc:
        return api.create_response(
            request,
            {
                "type": "https://homeautoshop.local/problems/illegal-transition",
                "title": "Illegal transition",
                "status": 422,
                "detail": " ".join(exc.messages),
            },
            status=422,
        )
    return wo


# ---------------------------------------------------------------------------
# Parts (SPEC §7.4, FR-PART-8/9)
# ---------------------------------------------------------------------------
#
# Reads only, which is the same line every other resource here is drawn at: the
# module docstring says the write surface lands with the feature it belongs to,
# and creating a part means cross-references, fitment and stock lots rather
# than one POST.
#
# The filters are deliberately the *same three* the parts screen offers, going
# through the *same* `matching()`. A second implementation of "what counts as a
# consumable" is how the API and the screen come to disagree while both look
# right in isolation.


class PartOut(Schema):
    id: UUID
    revision: int
    name: str
    categories: list[str]
    is_consumable: bool
    manufacturer: str
    part_number: str
    part_type: str
    unit: str
    has_core: bool
    on_hand: float
    min_quantity: float | None = None
    is_low: bool
    notes: str


def _part_out(part: Part, on_hand: Decimal) -> dict[str, Any]:
    """One part, with its shelf quantity handed in rather than looked up.

    `Part.on_hand` and `Part.is_low` are properties that each issue a query, so
    reading them here would make a fifty-row response a hundred and one
    queries. The caller sums every row's lots in one.
    """
    return {
        "id": str(part.pk),
        "revision": part.revision,
        "name": part.name,
        "categories": [c.name for c in part.categories.all()],
        "is_consumable": part.is_consumable,
        "manufacturer": part.manufacturer,
        "part_number": part.part_number,
        "part_type": part.part_type,
        "unit": part.unit,
        "has_core": part.has_core,
        "on_hand": float(on_hand),
        "min_quantity": float(part.min_quantity) if part.min_quantity is not None else None,
        "is_low": part.min_quantity is not None and on_hand < part.min_quantity,
        "notes": part.notes,
    }


@api.get("/parts/categories", response=list[str], tags=["Parts"])
def list_part_categories(request):
    """Every category in use, so a client can offer the same picker the form
    does — which is the half that stops `category` sprouting spellings."""
    return part_categories()


@api.get("/parts", response=list[PartOut], tags=["Parts"])
def list_parts(
    request,
    q: str = "",
    category: str = "",
    kind: Literal["part", "consumable"] | None = None,
    limit: int = 50,
):
    """The catalog, narrowed the way the parts screen narrows it.

    `kind` is a literal rather than a free string, and that is the one place
    this deliberately differs from the screen. There, an unrecognized `kind`
    shows the catalog: a URL is typed by hand, and an empty parts page is
    indistinguishable from a shop that owns nothing. Here it is a 422, because
    a client sending `kind=banana` has a bug, and answering it with the whole
    catalog hides the bug in data that looks fine.
    """
    rows = list(
        matching(q, category=category, consumable=KINDS.get(kind or ""))
        .prefetch_related("categories")[: min(limit, 200)]
    )
    on_hand = shelf_quantities([part.pk for part in rows])
    return [_part_out(part, on_hand.get(part.pk, Decimal(0))) for part in rows]


@api.get("/parts/{part_id}", response=PartOut, tags=["Parts"])
def get_part(request, part_id: UUID):
    part = get_object_or_404(Part.objects.prefetch_related("categories"), pk=part_id)
    return _part_out(part, shelf_quantities([part.pk]).get(part.pk, Decimal(0)))


@api.get("/search", tags=["Search"])
def search_api(request, q: str):
    from homeautoshop.core.search import search as run_search

    results = run_search(q)
    return {
        "query": results.query,
        "total": results.total,
        "groups": [
            {"label": g.label, "kind": g.kind, "results": [str(r) for r in g.results]}
            for g in results.groups
        ],
    }


# ---------------------------------------------------------------------------
# Trouble codes (SPEC §8.3c)
# ---------------------------------------------------------------------------


class DefinitionOut(Schema):
    """One answer about one code, with who said it.

    `source` is the load-bearing field and the reason this is not just a
    string. `standard` means J2012 defines it identically for every vehicle
    ever built and a caller may present it as fact; `make` means it is one
    manufacturer's own wording for its own code; `structure` means nobody has
    said, and what comes back is the shape of the code rather than a guess at
    the fault. A client that renders all three the same way is making a claim
    this application refuses to make.
    """

    code: str
    text: str
    source: str
    make: str = ""
    citation: str = ""
    version: int = 0
    is_authoritative: bool = False


def _out(found) -> dict:
    return {
        "code": found.code,
        "text": found.text,
        "source": found.source,
        "make": found.make,
        "citation": found.citation,
        "version": found.version,
        "is_authoritative": found.is_authoritative,
    }


@api.get("/codes", response=list[DefinitionOut], tags=["Trouble codes"])
def code_search(request, q: str, limit: int = 25):
    """Look a code up by number or by what it means.

    The point of having it: reading a code off a scan tool and wanting to know
    what it is should not require importing a report first. `q` takes either
    `P0420`, the prefix `P042`, or words from the definition —
    `catalyst efficiency` — and every word has to appear somewhere, because
    nobody types a definition verbatim.

    Not narrowed by who is asking. A dictionary is not vehicle data.
    """
    from homeautoshop.diagnostics import dtc

    return [_out(found) for found in dtc.find(q, limit=max(1, min(limit, 100)))]


@api.get("/codes/{code}", response=DefinitionOut, tags=["Trouble codes"])
def code_detail(request, code: str, make: str = ""):
    """The best available meaning for one code, and where it came from.

    `make` matters and is worth passing whenever it is known: `P1345` is one
    thing to GM and another to Toyota, and without it a manufacturer-controlled
    code can only be answered from its structure. With it, the answer walks the
    same ranking the screens use — a note recorded in this shop, then that
    maker's own published list, then the standard, then structure.
    """
    from homeautoshop.diagnostics import dtc

    found = dtc.explain(code, make=make)
    if found is None:
        # Not "no definition" — not code-shaped at all. The caller has to be
        # able to tell a typo from a code nobody has written down.
        return api.create_response(
            request,
            {
                "type": "about:blank",
                "title": "Not a trouble code",
                "status": 404,
                "detail": f"{code!r} is not shaped like a trouble code.",
            },
            status=404,
        )
    return _out(found)


# ---------------------------------------------------------------------------
# Offline write queue (SPEC §5.4, §10)
# ---------------------------------------------------------------------------


class BatchItem(Schema):
    """One queued mutation, replayed in the order it was captured.

    `op` names an operation rather than carrying a method and a URL, so the
    server decides what a client is allowed to replay. A queue that could
    replay arbitrary requests would be a way to reach every endpoint from a
    file in a browser's storage.
    """

    client_id: UUID
    op: str
    payload: dict = {}


class BatchIn(Schema):
    items: list[BatchItem]


@api.post("/sync/batch", tags=["Sync"])
def sync_batch(request, payload: BatchIn):
    """Replay a queue in one round trip (SPEC §5.4, §10).

    Per-item results rather than all-or-nothing: a reconnecting phone with
    fifty captures and one conflict should land forty-nine of them and be told
    precisely which one needs a person. A batch that failed as a unit would
    make the whole queue hostage to its worst item.

    Nothing here is transactional across items on purpose, for the same reason.
    """
    results = []
    for item in payload.items[:200]:
        try:
            results.append({"client_id": str(item.client_id), **_apply_batch(request, item)})
        except StaleRevisionError as conflict:
            # The one case the client must not retry blindly. It keeps the
            # write as a pending conflict and offers a side-by-side merge.
            results.append(
                {
                    "client_id": str(item.client_id),
                    "status": 409,
                    "expected_revision": conflict.expected,
                    "current_revision": conflict.actual,
                    "current": _asset_out(conflict.instance)
                    if isinstance(conflict.instance, Asset)
                    else None,
                }
            )
        except Exception as exc:  # noqa: BLE001 - reported per item, never fatal
            results.append(
                {"client_id": str(item.client_id), "status": 422, "detail": str(exc)}
            )
    return {"results": results}


def _apply_batch(request, item: BatchItem) -> dict:
    from django.core.exceptions import ValidationError

    from homeautoshop.work.models import JobItem

    data = item.payload or {}

    if item.op == "reading.create":
        asset = get_object_or_404(Asset, pk=data["asset_id"])
        # The client minted the id, so a replay hits the primary key and is
        # accepted rather than duplicating (SPEC §5.4).
        if existing := UsageReading.objects.filter(pk=item.client_id).first():
            return {"status": 200, "id": str(existing.pk)}
        reading = record_reading(
            asset,
            data["value"],
            unit=data.get("unit"),
            read_on=data.get("read_on"),
            note=data.get("note", ""),
            user=request.user,
        )
        UsageReading.objects.filter(pk=reading.pk).update(id=item.client_id)
        return {"status": 201, "id": str(item.client_id)}

    if item.op == "note.create":
        wo = get_object_or_404(WorkOrder, pk=data["work_order_id"])
        if existing := WorkOrderNote.objects.filter(pk=item.client_id).first():
            return {"status": 200, "id": str(existing.pk)}
        note = WorkOrderNote(work_order=wo, body=data.get("body", ""), author=request.user)
        note.pk = item.client_id
        note.save()
        return {"status": 201, "id": str(note.pk)}

    if item.op == "job_item.status":
        job_item = get_object_or_404(JobItem, pk=data["job_item_id"])
        job_item.status = data.get("status", JobItem.Status.DONE)
        job_item.save(expected_revision=data.get("revision"))
        return {"status": 200, "id": str(job_item.pk), "revision": job_item.revision}

    if item.op == "work_order.status":
        wo = get_object_or_404(WorkOrder, pk=data["work_order_id"])
        if (expected := data.get("revision")) is not None and expected != wo.revision:
            raise StaleRevisionError(wo, expected, wo.revision)
        if reason := data.get("blocked_reason"):
            wo.blocked_reason = reason
        try:
            wo.transition_to(
                data["status"], user=request.user, odometer_out=data.get("odometer_out")
            )
        except ValidationError as exc:
            return {"status": 422, "detail": " ".join(exc.messages)}
        return {"status": 200, "id": str(wo.pk), "revision": wo.revision}

    return {"status": 400, "detail": f"unknown operation {item.op}"}
