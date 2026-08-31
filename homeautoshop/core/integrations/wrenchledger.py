"""
WrenchLedger — tool availability on a work order (SPEC §8.7, INTEGRATION-WRENCHLEDGER.md).

One feature justifies this integration: the **readiness gate**. You plan a brake
job for Saturday; the breaker bar is at a neighbour's and the torque wrench is
due for calibration. Today you find that out on Saturday morning. With this, the
work order says so on Wednesday.

Four decisions from the integration document are load-bearing and are enforced
here rather than remembered:

* **Pull, not webhooks** (FR-WL-5). WrenchLedger delivers events by outbound
  webhook, which cannot reach a LAN instance. Pull is the default and asks the
  operator to open nothing.
* **Availability comes from `/loans?open_only=true`, never from tool deltas.**
  `on_loan` is an `EXISTS` subquery with no trigger behind it, so lending a tool
  does not move `updated_at` and a delta poll would never see it. A tool loaned
  out for a month would have shown as *on hand* — silently, and only for the
  tools people actually borrow.
* **The cache is an allow-list** (§7). The live payload carries purchase price,
  serial numbers and storage locations; NG-8 says none of that belongs here. A
  deny-list would start storing valuation data the first time WrenchLedger adds
  a field, and nobody would have decided to.
* **Entitlement is read, never assumed** (WL-Q5). The Shop-plan gate was an
  arbitrary product decision and may move. A bad key and a plan without API
  access need different sentences and different next steps.

Absent, broken, unsubscribed or switched off, HomeAutoShop is complete and
correct — only less convenient (FR-WL-7).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date
from uuid import UUID

from urllib.parse import quote, urlencode

from django.conf import settings
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from ..runtime import conf
from homeautoshop.core.outbound import OutboundBlocked, OutboundFailed, fetch_json

log = logging.getLogger(__name__)

SOURCE = "wrenchledger"

#: `www` is required. The published OpenAPI notes that clients drop the
#: Authorization header across a cross-host redirect, so calling the apex
#: domain fails in a way that looks exactly like a bad key.
DEFAULT_BASE = "https://www.wrench-ledger.app/api/v1"

#: What a HomeAutoShop key actually needs. `loans:sensitive` is deliberately
#: absent and must never be added: borrower contact details would create a
#: mirroring obligation this application must not have (NG-8).
REQUIRED_SCOPES = ("workspace:read", "tools:read", "loans:read", "maintenance:read")

#: The only fields kept from a Tool. Everything else — `purchase_price`,
#: `current_value`, `serial_number`, `storage_location_id`, `warranty_*`,
#: `notes` — arrives on every call and is dropped at the edge.
TOOL_FIELDS = ("id", "name", "brand", "model", "lifecycle", "condition")


class NotConfigured(RuntimeError):
    """No key. The integration is optional; nothing else depends on it."""


class PlanDoesNotInclude(RuntimeError):
    """The workspace's plan has no API access — a different problem to a bad key."""


@dataclass(slots=True)
class Connection:
    reachable: bool = False
    authenticated: bool = False
    entitled: bool = False
    tier: str = ""
    state: str = ""
    scopes: list[str] = field(default_factory=list)
    features: dict = field(default_factory=dict)
    message: str = ""

    @property
    def usable(self) -> bool:
        return self.reachable and self.authenticated and self.entitled

    @property
    def missing_scopes(self) -> list[str]:
        return [s for s in REQUIRED_SCOPES if s not in self.scopes]

    @property
    def excess_scopes(self) -> list[str]:
        """Scope the operator granted and this application never uses.

        Worth saying out loud: over-privileged credentials are the operator's
        risk, and nobody notices them on their own.
        """
        return sorted(set(self.scopes) - set(REQUIRED_SCOPES))


class WrenchLedgerClient:
    def __init__(self, base_url: str | None = None, api_key: str | None = None) -> None:
        self.base_url = (base_url or settings.WRENCHLEDGER_URL or DEFAULT_BASE).rstrip("/")
        self.api_key = api_key or conf.WRENCHLEDGER_API_KEY
        if not self.api_key:
            raise NotConfigured(
                "WRENCHLEDGER_API_KEY is not set. The integration is optional; "
                "nothing in HomeAutoShop depends on it."
            )

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}", "Accept": "application/json"}

    def get(self, path: str, **params) -> dict | list:
        # `urlencode`, not string concatenation. A WrenchLedger watermark is an
        # ISO timestamp ending `+00:00`, and an unencoded `+` arrives at the
        # server as a space — which it rejects as a validation error, so every
        # sync after the first one failed with HTTP 400. The same hazard applies
        # to any search text a person types.
        query = urlencode({k: v for k, v in params.items() if v not in (None, "")})
        url = f"{self.base_url}{path}" + (f"?{query}" if query else "")
        response = fetch_json(
            url, headers=self.headers, purpose=f"wrenchledger{path}", timeout=20
        )
        return response.data

    # -- connection ------------------------------------------------------

    def check(self) -> Connection:
        """FR-WL-1 — verify the key, and say which of the two ways it failed."""
        result = Connection()
        try:
            data = self.get("/workspace")
        except OutboundBlocked as exc:
            result.message = str(exc)
            return result
        except OutboundFailed as exc:
            result.reachable = exc.status > 0
            code = str((exc.body or {}).get("code", "")).upper()
            if code == "FEATURE_NOT_IN_PLAN" or exc.status == 402:
                # A working key on a plan without API access. Retrying will
                # never help, and telling the operator to check their key sends
                # them somewhere with nothing to find.
                result.authenticated = True
                result.message = str(
                    _(
                        "The key works, but this WrenchLedger workspace's plan does not "
                        "include API access. Nothing here needs fixing — the plan does."
                    )
                )
            elif exc.status in (401, 403):
                result.message = str(
                    _("WrenchLedger rejected that key. Check it was copied whole, and not expired.")
                )
            else:
                result.message = str(
                    _("Could not reach %(url)s (%(err)s).")
                ) % {"url": self.base_url, "err": exc}
            return result

        if not isinstance(data, dict):
            result.message = str(_("WrenchLedger answered with something unexpected."))
            return result

        # Every response is enveloped as `{"data": …, "request_id": …}`, so the
        # workspace itself is one level down. Reading the envelope instead —
        # which this did — leaves tier, state, scopes and features all empty,
        # and empty `features` means the entitlement check falls back to its
        # default and quietly passes. A check that cannot fail is not a check.
        workspace = data.get("data") if isinstance(data.get("data"), dict) else data

        result.reachable = True
        result.authenticated = True
        result.tier = str(workspace.get("tier", ""))
        result.state = str(workspace.get("state", ""))
        # Scopes belong to the key, not the workspace, so they arrive under the
        # api_client that presented it. Both places are read because the shape
        # is the vendor's to change.
        client_info = workspace.get("api_client")
        scopes = workspace.get("scopes")
        if not scopes and isinstance(client_info, dict):
            scopes = client_info.get("scopes")
        result.scopes = [str(s) for s in scopes or []]
        result.features = workspace.get("features") or {}
        # Read from the response, never inferred from the tier. The gate is a
        # per-workspace override as often as it is a plan rule.
        result.entitled = bool(result.features.get("api_webhooks", True))
        if not result.entitled:
            result.message = str(
                _("This workspace's plan does not include API access.")
            )
        elif result.scopes and result.missing_scopes:
            # Only when the scopes were actually reported. An API that does not
            # list them is not an API that granted none, and warning about
            # "missing" scopes it never mentioned would be noise.
            result.message = str(
                _("That key is missing %(scopes)s, so availability cannot be read.")
            ) % {"scopes": ", ".join(result.missing_scopes)}
        return result

    # -- the pull loop ---------------------------------------------------

    def open_loans(self) -> list[dict]:
        """Tier 1, and the authoritative answer to *what is out right now*."""
        data = self.get("/loans", open_only="true")
        return _rows(data)

    def tools_changed_since(self, watermark: str | None) -> tuple[list[dict], str | None]:
        """Tier 2, drained fully.

        The watermark is the **maximum `updated_at` actually seen**, never the
        request time: `updated_after` compares strictly greater-than, so a
        request-time watermark silently skips anything written between the
        query running and the response landing. A page left undrained must not
        advance it either, or those changes are lost for good.

        That last sentence used to be true only of the explicit page bound. The
        far more likely way to leave a drain unfinished was to *not recognise*
        the cursor: this read `next_cursor` at the top level and nothing else,
        while `_rows` already accepted four different places for the rows. An
        envelope that nests its cursor under `meta` therefore looked exactly
        like a finished drain — one page read, the watermark advanced to that
        page's newest row, and every tool with an older stamp skipped for good
        because the next request asks for strictly *newer* than that.

        So two changes. The cursor is looked for as tolerantly as the rows are,
        and **a full page with no cursor does not advance the watermark** — it
        is the signature of a continuation token we failed to find, and staying
        put costs one repeated page while guessing costs the whole tail.
        """
        rows: list[dict] = []
        cursor = None
        seen = watermark
        pages = 0
        for _page in range(40):  # a bound, not a limit anyone should hit
            data = self.get(
                "/tools", updated_after=watermark, cursor=cursor, limit=PAGE_SIZE
            )
            page = _rows(data)
            rows.extend(page)
            pages += 1
            for row in page:
                stamp = str(row.get("updated_at") or "")
                if stamp and (seen is None or stamp > seen):
                    seen = stamp
            cursor, where = _find_cursor(data)
            if cursor and pages == 1:
                # Once per drain, naming the key. The failure this replaced was
                # invisible precisely because a truncated drain looks exactly
                # like a short one, so where the token lives is worth a line in
                # the log the day the envelope changes again.
                log.info("wrenchledger pages /tools by %r", where)
            if not cursor:
                if len(page) >= PAGE_SIZE:
                    log.warning(
                        "wrenchledger returned a full page of %s tools with no cursor "
                        "this client recognises; leaving the watermark alone",
                        len(page),
                    )
                    return rows, watermark
                return rows, seen
        # Bailed out mid-drain: report what was read but leave the watermark
        # where it was, so the next run starts from the same place.
        log.warning("wrenchledger tool delta did not drain in 40 pages")
        return rows, watermark

    def all_tools(self) -> list[dict]:
        """Every tool in the workspace, by the documented drain.

        The search below rests on a `q` parameter this integration has never
        confirmed exists; this rests on `updated_after` and cursor paging, both
        of which are documented and both of which the sync already depends on.
        It is what makes the local cache complete, and a complete local cache is
        what makes "do I own a vacuum pump?" answerable without asking anybody.
        """
        rows, _seen = self.tools_changed_since(None)
        return rows

    def schedules_for(self, tool_id: str) -> list[dict]:
        """Tier 3 — calibration and service dates, per referenced tool only."""
        # Quoted, because an id is not guaranteed to be URL-safe just because
        # it usually is: `/tools/Vacuum pump/schedules` went out unencoded and
        # failed at the socket rather than at the server.
        return _rows(self.get(f"/tools/{quote(str(tool_id), safe='')}/schedules"))

    # There is no `search_tools` here any more, and its absence is the point.
    #
    # It sent `GET /tools?q=…`, a parameter this integration never verified and
    # which **the API rejects**: 137 such calls in one instance's audit log,
    # every one of them HTTP 400, not one success. So the remote half of tool
    # search never worked — while the failure was swallowed and reported as
    # "WrenchLedger did not answer", which is both alarming and untrue. It
    # answered immediately and said the request was malformed.
    #
    # Searching is now purely local, over the cache the drain above fills. That
    # is the documented path, it is the one that works, and it is faster and
    # available offline besides. The cache being *complete* is what makes it
    # sufficient — which is why the drain, not the search, was the real bug.


#: Our page size rather than the server's default, so "a full page" is a fact
#: this client can recognise — see `tools_changed_since`.
PAGE_SIZE = 100

#: Where a continuation token is found, in the order it is looked for. As
#: tolerant as `_rows` is about the rows themselves, and for the same reason:
#: guessing wrong here does not fail loudly, it silently truncates.
CURSOR_KEYS = ("next_cursor", "nextCursor", "cursor", "next", "next_page")
#: Envelopes that put paging beside the rows rather than at the top level.
CURSOR_ENVELOPES = ("meta", "pagination", "page", "paging", "links")


def _rows(data) -> list[dict]:
    if isinstance(data, list):
        return [row for row in data if isinstance(row, dict)]
    if isinstance(data, dict):
        for key in ("data", "items", "results", "tools", "loans", "schedules"):
            found = data.get(key)
            if isinstance(found, list):
                return [row for row in found if isinstance(row, dict)]
    return []


def _find_cursor(data) -> tuple[str | None, str]:
    """The continuation token and where it was found.

    The location is returned, not just the value, because it is the thing worth
    knowing when this stops working: an envelope that moves is invisible from
    the outside — the drain simply reads one page and stops — and this is what
    turns that into a line somebody can read.
    """
    if not isinstance(data, dict):
        return None, ""
    scopes = [("", data)] + [(name, data.get(name)) for name in CURSOR_ENVELOPES]
    for envelope, scope in scopes:
        if not isinstance(scope, dict):
            continue
        for key in CURSOR_KEYS:
            value = scope.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip(), f"{envelope}.{key}" if envelope else key
    return None, ""


def _cursor(data) -> str | None:
    """The continuation token, wherever this API happens to keep it."""
    return _find_cursor(data)[0]


def is_wrenchledger_id(value) -> bool:
    """Whether this id could have come from WrenchLedger at all.

    Tool ids there are UUIDs (WL-Q2 — the deep link is `/tools/{tool_uuid}`).
    A `ShopTool` row keyed by anything else was invented here, by somebody
    naming a tool on a job that the picker could not match, and the other
    system has never heard of it.
    """
    try:
        UUID(str(value))
    except (ValueError, AttributeError, TypeError):
        return False
    return True


def matches(row: dict, query: str) -> bool:
    """Whether a tool row actually answers what was typed.

    The same fields the local search looks at, so a tool cannot be findable in
    one half of the merged result and not the other.
    """
    needle = (query or "").strip().casefold()
    if not needle:
        return False
    return any(
        needle in str(row.get(field) or "").casefold()
        for field in ("name", "brand", "model", "id", "tool_id", "category")
    )


def keep_tool_fields(row: dict) -> dict:
    """Allow-list the payload (§7). Named fields in, everything else dropped."""
    return {name: row.get(name) for name in TOOL_FIELDS if row.get(name) is not None}


# --------------------------------------------------------------------------
# Sync
# --------------------------------------------------------------------------

WATERMARK_KEY = "wrenchledger.tools_watermark"
LAST_SYNC_KEY = "wrenchledger.last_sync"


def sync(*, client=None, tool_ids: list[str] | None = None, rebuild: bool = False) -> dict:
    """Refresh the availability cache.

    `tool_ids` scopes the schedule reads to one work order's tools, for the
    on-demand refresh when a job is opened — so what is shown is never stale at
    the moment it matters most.

    `rebuild` throws the watermark away and reads the workspace from the start.
    A delta poll is only ever as complete as every run before it, so a run that
    truncated once leaves a hole no later delta will fill: the watermark has
    already moved past it. Fixing the code that truncated does not fix the
    cache, and there has to be a way to say *start again*.
    """
    from homeautoshop.core.models import Setting
    from homeautoshop.work.models import ShopTool

    if conf.OFFLINE_MODE:
        raise OutboundBlocked(_("Offline Mode is on, so nothing is fetched."))

    client = client or WrenchLedgerClient()
    now = timezone.now()
    touched = 0

    # Tier 2 first: a rename should land before the loan that references it, so
    # the gate never renders a tool as "(unknown)" with a due date beside it.
    watermark = None if rebuild else Setting.get(WATERMARK_KEY)
    changed, moved = client.tools_changed_since(watermark)
    for row in changed:
        kept = keep_tool_fields(row)
        if not kept.get("id"):
            continue
        ShopTool.objects.update_or_create(
            tool_id=str(kept["id"]),
            defaults={
                "name": str(kept.get("name") or "")[:160],
                "brand": str(kept.get("brand") or "")[:80],
                "model": str(kept.get("model") or "")[:80],
                "lifecycle": str(kept.get("lifecycle") or "")[:32],
                "checked_at": now,
            },
        )
        touched += 1
    if moved and moved != watermark:
        Setting.put(WATERMARK_KEY, moved)

    # Tier 1: rewrite loan state wholesale. A loan that has been *returned*
    # simply is not in this response, so clearing first is the only way a
    # returned tool ever becomes available again.
    ShopTool.objects.update(on_loan_to="", loan_due_on=None, from_kit="")
    for loan in client.open_loans():
        tool_id = str(loan.get("tool_id") or "")
        if not tool_id:
            continue
        ShopTool.objects.update_or_create(
            tool_id=tool_id,
            defaults={
                "name": str(loan.get("tool_name") or "")[:160],
                # `borrower_name` only. The scope that would return contact
                # details is never requested (§4.3).
                "on_loan_to": str(loan.get("borrower_name") or _("someone"))[:80],
                "loan_due_on": _as_date(loan.get("due_date")),
                "from_kit": str(loan.get("from_kit") or "")[:80],
                "checked_at": now,
            },
        )
        touched += 1

    # Tier 3: only tools a job actually references. There is no workspace-wide
    # schedule list, so this is per tool and deliberately the narrowest call.
    #
    # And only tools WrenchLedger has heard of. A tool named on a job that the
    # picker could not match is recorded under the typed text — `Vacuum pump`
    # became a tool id — and this then spent every sync asking WrenchLedger for
    # `/tools/Vacuum pump/schedules`, which cannot succeed and never did. Asking
    # about something the other system has no idea exists is not a fetch that
    # failed, it is a question that should not have been posed.
    wanted = tool_ids if tool_ids is not None else list(
        ShopTool.objects.filter(references__isnull=False)
        .distinct()
        .values_list("tool_id", flat=True)
    )
    wanted = [tool_id for tool_id in wanted if is_wrenchledger_id(tool_id)]
    for tool_id in wanted:
        try:
            schedules = client.schedules_for(tool_id)
        except (OutboundFailed, OutboundBlocked) as exc:
            log.info("no schedules for %s: %s", tool_id, exc)
            continue
        due = [
            _as_date(row.get("next_due_on") or row.get("due_date"))
            for row in schedules
            if (row.get("kind") or "").lower() in ("calibration", "service", "")
        ]
        due = [d for d in due if d]
        ShopTool.objects.filter(tool_id=tool_id).update(
            calibration_due_on=min(due) if due else None, checked_at=now
        )

    Setting.put(LAST_SYNC_KEY, now.isoformat())
    return {"at": now.isoformat(), "tools": touched}


def _as_date(value) -> date | None:
    """A calendar date out of either a date or a timestamp.

    WrenchLedger returns both shapes depending on the field, and a due date is
    a calendar fact (SPEC §5.5) — so the time part is dropped rather than
    carried around to cause an off-by-one for an operator who travels.
    """
    if not value:
        return None
    from django.utils.dateparse import parse_date, parse_datetime

    text = str(value)
    if found := parse_date(text[:10]):
        return found
    stamp = parse_datetime(text)
    return stamp.date() if stamp else None
