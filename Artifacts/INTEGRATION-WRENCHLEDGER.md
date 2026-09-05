# HomeAutoShop ↔ WrenchLedger Integration

|  |  |
| --- | --- |
| **Document** | `Artifacts/INTEGRATION-WRENCHLEDGER.md` |
| **Status** | Draft for review |
| **Version** | 0.4.0 |
| **Date** | 2026-08-29 |
| **Parent spec** | [SPEC.md](SPEC.md) §8.7 — this document is the detail behind that summary |
| **Counterpart** | WrenchLedger `docs/API.md` (Phase 6, migrations 0049–0052) and `docs/openapi.yaml` (OpenAPI 3.1), read at `X:\Projects\Software\WrenchLedger`, plus live verification against a Solo workspace on 2026-08-30 (the key used has since been revoked) |
| **v0.4.0 changes** | Sync corrected: **availability now reads `GET /loans?open_only=true` directly** rather than inferring it from tool deltas or the dashboard — a long-dated loan was invisible to both. Kit checkouts confirmed covered · WL-Q10/Q11 resolved · HomeAutoShop owns its own lead window · `reports:read` demoted to optional |
| **v0.3.0 changes** | WL-Q7–Q9 resolved against a live workspace: `/reports/dashboard` collapses the readiness gate to one call but is lead-windowed · **loans do not move `tools.updated_at`** · 120 req/window · `features` map drives the optional surfaces · caching becomes an ingest allow-list |
| **v0.2.0 changes** | WL-Q1–Q6 resolved · new §3.2 three-tier pull loop · §6.6 consumables settled as opt-in duplication · §12 reciprocal proposal held for manual transfer |

---

## 1. Purpose

HomeAutoShop tracks **the machines you work on**. WrenchLedger tracks **the tools you work with**. Both are built by the same developer, and each is a complete product without the other.

This document specifies the optional integration between them: what crosses the boundary, in which direction, over which transport, and — most importantly — what must *not* cross it.

Two things follow from the products' shapes and constrain everything below:

- **WrenchLedger is cloud SaaS.** HomeAutoShop is local-first and LAN-first (SPEC §4, P-1; NG-3).
- **The API is real and already shipped.** Most of what this integration needs exists today (§4). The remaining work is mostly on the HomeAutoShop side.

---

## 2. The boundary

The dividing line is **what you do with a thing**, not what the thing is:

| | HomeAutoShop | WrenchLedger |
| --- | --- | --- |
| Question answered | *What did I do to it, with what parts, at what cost?* | *Do I own it, where is it, what is it worth, who has it?* |
| Subject | Vehicles and serviceable equipment | Tools, kits, shop consumables |
| Core record | Work order | Tool |
| A generator you rebuild the carb on | **Here** | — |
| A generator you only need to know you own | — | **There** |
| A torque wrench | — | **There**, always |

**HomeAutoShop must never grow** storage locations, tool valuation, loan/borrower management, or calibration scheduling. That is SPEC NG-8 and NG-9, and mirroring the data is how a non-goal quietly becomes a feature. §7 of this document sets the caching limits that enforce it.

---

## 3. The transport problem — read this before designing anything

WrenchLedger delivers events by **outbound webhook to a customer HTTPS endpoint**. HomeAutoShop is, by default, **a LAN-only service with no inbound internet exposure** (SPEC NG-3).

A cloud service cannot POST to `https://shop.home.arpa`. This is not a detail to solve later; it determines the whole synchronization design.

### 3.1 Resolution: pull-first, webhooks opt-in

| Mode | When | How |
| --- | --- | --- |
| **Pull (default)** | Any normal LAN deployment | A `wrenchledger.sync` job polls tool status on an interval, plus an on-demand refresh when a work order with tool references is opened. No inbound exposure, nothing to secure, works behind NAT. |
| **Webhook (opt-in)** | The operator already exposes HomeAutoShop deliberately — reverse proxy, Cloudflare Tunnel, Tailscale Funnel | A signed receiving endpoint (§5) gives near-real-time updates. Strictly an upgrade; the pull path stays as the fallback. |

**Recommendation: build pull first and ship it alone.** For a home shop, tool availability changing within minutes rather than seconds has no practical consequence, and pull avoids asking a local-first user to open a hole in their network. The webhook path is worth building only for operators who already exposed the instance for their own reasons.

> This is the single most important finding from reading the WrenchLedger implementation, and it inverts the design sketched in SPEC §8.7.1, which assumed webhooks were the primary direction. They are the *optional* direction.

### 3.2 The pull loop

Verified against the source and a live workspace. The design below is driven by one fact established in §3.3: **loan activity is invisible to a delta poll.** Availability therefore comes from the loans endpoint, not from tools and not from the dashboard.

| Tier | Call | Cadence | Provides |
| --- | --- | --- | --- |
| **1 — Availability** *(authoritative)* | `GET /loans?open_only=true` | Every sync | Every currently-open loan: `tool_id`, `tool_name`, `borrower_name`, `due_date`, `loaned_at`, and `from_kit`. This is the answer to *"what is out right now."* |
| **1b — Internal custody** | `GET /assignments` | Every sync, **only if** `features.employee_assignment` | A tool can also be unavailable because it is assigned internally rather than loaned out. Off on Solo, so usually skipped entirely (§4.2). |
| **2 — Tool deltas** | `GET /tools?updated_after=<watermark>` | Every sync | Renames, `lifecycle` (stored / under repair / missing / sold), and `condition`. Cheap and usually an empty page. |
| **3 — Schedules** | `GET /tools/{id}/schedules`, referenced tools only | Slower | Calibration and service due dates, against **HomeAutoShop's own lead window** (§3.4). |
| *(optional)* | `GET /reports/dashboard` | — | A one-call convenience view. Useful, but **not depended on** — see §3.4. |

`open_only=true` maps to `returned_at is null`, so tier 1's result set is inherently small: only what is actually out, never the whole inventory. It carries the due date and borrower name the readiness gate displays, so no second call is needed to render it.

### 3.3 Why availability cannot come from `/tools` or the dashboard

Two independent findings converge on the same conclusion.

**`on_loan` is computed, not stored** (WL-Q8; migration `0007`, unchanged since):

```sql
'on_loan', exists (
  select 1 from public.tool_loans l
  where l.tool_id = t.id and l.returned_at is null
)
```

Loans live in `tool_loans`. No trigger on that table touches `tools`, and no loan path issues an `UPDATE public.tools`. **Lending or returning therefore never moves `tools.updated_at`, and `updated_after` will never surface it.** A full `GET /tools` would compute `on_loan` correctly, but a *delta* poll cannot.

**The dashboard's loan arrays are windowed.** `loans_overdue` and `loans_upcoming` are both bounded by `lead_days`. So a tool loaned out today and due in thirty days appears in **neither** — and does not move `updated_at` either.

> **The failure this prevents.** Had the sync leaned on the dashboard plus `updated_after`, a long-dated loan would have been invisible to both, and the work order would have shown the tool as on hand while it sat at a neighbor's house. Silent, and only for tools people actually borrow — which is exactly the set that matters. Hence tier 1 is a direct, unwindowed read of open loans, and neither of the other two sources is treated as an availability signal.

**Kit checkouts are covered.** A kit checkout generates per-unit rows in `tool_loans` linked by `kit_checkout_item_id`, and `api_list_loans` applies no filter against them — it surfaces them with a `from_kit` flag. So one call to `/loans?open_only=true` captures individual loans and kit checkouts alike, and the UI can say *"out with the brake kit"* rather than just *"out."*

### 3.4 HomeAutoShop owns its own lead window

`/reports/dashboard` returns a genuinely useful aggregate — `schedules_due`, `maintenance_due`, `loans_overdue`, `loans_upcoming`, `consumables_low` — in one request. It is nonetheless **not** the basis of the readiness gate, for a reason that outlives this integration.

Its window comes from `user_settings.reminder_lead_days` (default 7, range 0–60), which is a **per-person WrenchLedger preference** (WL-Q10). Different people there legitimately track and get notified about different things on different horizons. Building HomeAutoShop's planning horizon on that setting would mean:

- a HomeAutoShop behavior that changes when someone edits a preference in another product;
- a horizon that differs between operators for no reason either of them chose;
- and a core feature shaped by a product **most HomeAutoShop users will never subscribe to.**

That last point is decisive and is the same principle already applied to LubeLogger (SPEC OQ-9) and consumables (§6.6): *an optional integration never dictates core behavior.* HomeAutoShop keeps its own lead-window setting, applies it to schedule data fetched per tool (tier 3), and the answer is identical for every operator whether or not WrenchLedger is connected.

The dashboard therefore stays **optional** — a fast path for an at-a-glance widget, clearly labeled as reflecting WrenchLedger's own window — and `reports:read` drops back to a genuinely optional scope.

### 3.5 Watermark rules

`updated_after` compares **strictly greater than** (`t.updated_at > p_updated_after`, migration `0051`) — WL-Q11 answered. Two consequences:

- **Store the watermark as the maximum `updated_at` actually observed in the drained pages**, never the request time. With a strict `>`, a request-time watermark silently skips anything written between query execution and the response landing.
- No millisecond nudging is needed. Exclusive comparison against an observed value progresses cleanly with no re-processing and no gap.

Tier 2 paginates by cursor and **must be drained fully before the watermark advances**; a partially drained page that advances it loses changes permanently. First run has no watermark, making tier 2 a full paginated read — the only expensive sync.

**On-demand refresh** runs tiers 1 and 3 scoped to a single work order's tools when it is opened, so the display is never stale at the moment it matters most.

### 3.6 Budget

Measured limit is **120 requests per fixed window** (`RateLimit-Limit` / `-Remaining` / `-Reset`, `Retry-After` on `429`). A steady-state sync is two calls — loans and tool deltas — plus a handful of per-tool schedule reads for referenced tools. Comfortably inside the limit, but the headers are read and honored rather than assumed.

> The API shipping `updated_after`, `open_only`, cursor pagination, and rate-limit headers is a strong signal that pull-first (§3.1) is the intended integration shape rather than a workaround for the LAN constraint. Webhooks are the latency optimization on top.

---

## 4. Verified API facts

Read from WrenchLedger `docs/API.md` and `docs/openapi.yaml` at the versions cited in the header. Pin against the operator's running version at implementation time.

### 4.1 Connection

| Aspect | Value |
| --- | --- |
| Base URL | `https://www.wrench-ledger.app/api/v1` |
| **`www` is required** | The OpenAPI file notes clients drop the `Authorization` header across a cross-host redirect. Calling the apex domain will fail authentication in a way that looks like a bad key. **Hard-code the `www` host and never follow redirects.** |
| Auth | `Authorization: Bearer wlk_live_…` — workspace-scoped API key |
| Cookies | Ignored on `/api/v1`; the key is the only credential |
| Workspace binding | Implicit in the key. Never sent as a body field, query param, or header. One key per workspace. |
| Idempotency | `Idempotency-Key: <UUID>` on writes, database-backed |
| Versioning | Major version in the path; additive changes within `/api/v1` |
| Contract | OpenAPI 3.1 — generate the client, do not hand-roll it |
| Tool deep link | `https://www.wrench-ledger.app/tools/{tool_uuid}` — confirmed. This is how HomeAutoShop links out instead of reproducing tool detail (§7). |
| Rate limits | Enforced per connection. The adapter must honor `429` with backoff and treat limiting as a normal condition, not an error state. |

### 4.2 Entitlement — a real gate

`plan_configs.features.api_webhooks` is authoritative:

- **Shop plan: enabled.** Trialing Shop: enabled.
- **Solo plan: disabled**, returning `FEATURE_NOT_IN_PLAN`. An admin may override per workspace.
- After a Shop→Solo downgrade, keys are **suspended, not deleted**, and restore on re-upgrade.

**Verified against a live Solo workspace:** `tier: "solo"`, `state: "complimentary"`, and `features.api_webhooks: true` — the per-workspace override is real and working, so a Solo workspace *can* carry API access today.

`GET /workspace` also returns the full **`features` map** and the key's own `scopes` and `role_ceiling`. That is more useful than it first appears:

| Use | Detail |
| --- | --- |
| **Drive the optional surfaces off `features`, never off configuration** | `projects`, `employee_assignment`, `purchase_requests`, and `operational_reports` are all `false` on Solo. §6.3 (project mirroring) and §6.4 (custody) therefore **cannot work on a Solo workspace at all** — HomeAutoShop reads the map at connection time and hides them rather than offering a toggle that fails. |
| **Reinforces the build order** | The readiness gate (§6.2) depends on none of those flags. It works on the smallest plan; the optional depth in §6.3–6.5 is Shop-only. W1/W2 are the integration. |
| **Detect over-privileged keys** | The response lists the key's scopes, so FR-WL-1's excess-scope warning is a comparison against a known list, not guesswork. |

> The test key used to verify this document carries **all sixteen read scopes including `loans:sensitive`** — precisely the over-privilege case §4.3 warns about. A key for HomeAutoShop needs four.

**Consequence for HomeAutoShop:** the connection test (FR-WL-1) must distinguish *"your key is wrong"* from *"your WrenchLedger plan does not include the API."* Those need completely different messages, and conflating them produces a support conversation that goes nowhere. When the feature is absent, HomeAutoShop says so plainly and stops — no retry, no silent degradation.

> **The gate may move, so do not build around it (WL-Q5).** The Shop-plan restriction was an arbitrary product decision on the WrenchLedger side, and this integration is plausibly the justification to relax it — a Solo user with a home garage is precisely the person who wants tool availability on a work order, and gating it there caps the pairing's reach at the smaller audience.
>
> HomeAutoShop must therefore **never hard-code an assumption that the API implies a Shop plan.** It reads entitlement from the response, reports what it finds, and works identically whichever way the gate is set. If the gate is later opened to Solo, HomeAutoShop needs no change at all.

### 4.3 Scopes — request the minimum

Scopes are additive, unknown scopes are rejected, and **write does not imply read**.

| Scope | Needed? | Why |
| --- | --- | --- |
| `workspace:read` | **Yes** | Connection test; returns tier, state, locale, currency, time zone |
| `tools:read` | **Yes** | Tool references and availability |
| `loans:read` | **Yes — load-bearing** | The authoritative availability source (§3.3). Returns `borrower_name`, `due_date`, and `from_kit`; `borrower_email`/`borrower_contact` come back `null` at this scope, which is exactly what we want. |
| `maintenance:read` | **Yes** | Calibration and service schedules, for the readiness gate |
| `lists:read` | Optional | Category names for nicer pickers |
| `projects:read` / `projects:write` | Optional (§6.3) | Work-order ↔ project mirroring |
| `assignments:read` | **Yes, where the feature exists** | Internal custody is a second way a tool is unavailable (§3.2, tier 1b). Gated by `features.employee_assignment`, so absent on Solo. |
| `assignments:write` | Optional (§6.4) | Recording custody for the duration of a job |
| `maintenance:write` | Optional (§6.5) | Meter/usage post-back |
| **`loans:sensitive`** | **Never** | Borrower contact details. HomeAutoShop has no use for them and requesting the scope would create a mirroring obligation it must not have (NG-8). *"The breaker bar is on loan, due Friday"* is all a work order needs. |
| `reports:read` | **Optional** | `/reports/dashboard` aggregates due data in one call, but its window is another product's per-user setting, so it is a convenience rather than a dependency (§3.4). Omit it and nothing is lost. |
| `files:*`, `consumption:*`, `purchase_requests:*`, `audit:read` | **No** | Outside the boundary (§2) |

> The setup UI should show the operator the exact minimal scope list to create the key with, and the connection test should **warn if the key carries more scope than needed** — over-privileged credentials are the operator's risk, and they will not notice on their own.

---

## 5. Webhook contract (opt-in path only)

Verified from `docs/API.md` §11.

```http
POST /api/v1/integrations/wrenchledger/webhook HTTP/1.1
Content-Type: application/json
User-Agent: WrenchLedger-Webhooks/1.0
WrenchLedger-Event: tool.lifecycle_changed
WrenchLedger-Event-Id: evt_01K...
WrenchLedger-Delivery-Id: del_01K...
WrenchLedger-Timestamp: 1784928903
WrenchLedger-Signature: v1=4d5b...
```

Signature is `hex(HMAC-SHA-256(signing_secret, "<unix_timestamp>.<exact_raw_body>"))`.

**Receiver requirements** — all mandatory:

1. Read the **raw body before JSON parsing**; re-serialization changes the signature.
2. Reject timestamps outside a **five-minute** tolerance.
3. Constant-time signature comparison.
4. Deduplicate on `WrenchLedger-Event-Id` / `WrenchLedger-Delivery-Id`.
5. Return `2xx` **only after** durably accepting the event — enqueue, then acknowledge.
6. Respond within the timeouts: 3 s connect, 10 s complete. Do no real work in the handler.

Delivery is **at-least-once with no ordering guarantee**.

> ### 5.1 The rule that follows from "no ordering guarantee"
>
> **Treat every event as a cache-invalidation hint, never as a state delta.**
>
> On `tool.lifecycle_changed`, HomeAutoShop marks its cached row stale and re-reads the tool from the API. It does not apply the payload's values. Out-of-order delivery is explicitly permitted, so applying deltas would eventually write an older state over a newer one — a corruption that is silent, intermittent, and nearly impossible to reproduce.
>
> The cost of re-reading is one API call. The cost of getting this wrong is a work order that says a tool is available when it is not.

### 5.2 Events worth subscribing to

| Event | Use |
| --- | --- |
| `tool.lifecycle_changed` | active / stored / under repair / missing / sold — the core availability signal |
| `tool.condition_changed` | Condition degraded; worth surfacing before a precision job |
| `tool.updated` | Name or attributes changed; refresh the cached display name |
| `tool.deleted` | Mark the reference orphaned (never delete the job item's reference) |
| `loan.created` / `loan.returned` | Out with a borrower / back on the shelf |
| `loan.due_soon` / `loan.overdue` | It is not coming back before Saturday |
| `schedule.due` | Calibration or service due — drives the readiness gate (§6.2) |
| `schedule.completed` | Calibration done; clear the warning |

Not subscribed: everything project-, assignment-, consumable-, kit-, purchase-request-, and file-related unless the corresponding optional surface (§6.3–6.5) is enabled.

---

## 6. Integration surfaces

### 6.1 Tool references on job items — the foundation

A `job_item` may carry zero or more `tool_ref`s: `{ wrenchledger_tool_id, cached_name, cached_status, checked_at }`. That is the entire local record. Everything else is fetched or shown as a link.

Selection is a search-as-you-type picker backed by `GET /tools`, and the reference stores the id — not the name — because names change and `revision` exists on the Tool object precisely because WrenchLedger expects edits.

### 6.2 Readiness gate — the feature that justifies the integration

Before starting or planning a job, HomeAutoShop answers: **can I actually do this today?**

```
Work order: Front brakes — Silverado                    ⚠ 2 tool issues
  Job items
    ✓ Replace pads and rotors
        🔧 3/8" torque wrench        ⚠ calibration due 2026-08-14
        🔧 Breaker bar               ⚠ on loan to Dave, due 2026-09-02
        🔧 Caliper piston tool       ✓ available
```

This surfaces in three places: the work order itself, the planning dashboard beside `waiting_on_parts` (SPEC FR-REP-1), and a pre-start check when a job moves to `in_progress`.

**It is a warning, never a block.** The operator may know the wrench is fine, or may be borrowing one. A hard block on data from an optional external system would be indefensible.

### 6.3 Work order ↔ WrenchLedger project *(optional)*

WrenchLedger has first-class **Projects** with open/closed status, and assignments and consumption can be attributed to them. A HomeAutoShop work order maps naturally onto one.

| HomeAutoShop | WrenchLedger | Notes |
| --- | --- | --- |
| `work_order` created | `POST /projects` | Name from the work order number and title |
| `work_order` completed / abandoned | `POST /projects/{id}/close` | |
| `external_ref` row | project id | Reuses SPEC §6.2's provenance table — no new mechanism |

Off by default. It is genuinely useful for anyone using WrenchLedger's utilization and downtime reports, and pure overhead for anyone who is not.

### 6.4 Tool custody during a job *(optional)*

`POST /assignments` on job start and `POST /assignments/{id}/release` on completion records internal custody in WrenchLedger for the duration of the work — so a second person searching for the 3/8" torque wrench sees where it went.

Realistically valuable only in a household with multiple people wrenching. Off by default; requires `assignments:write`.

### 6.5 Usage and meter post-back *(optional)*

The Tool object carries `meter_kind` (`hours`, `cycles`, `miles`, `kilometers`) and `meter_value`. A completed work order can post a meter increment, so WrenchLedger's service scheduling reflects work actually done.

Torque wrenches are the real case — calibration intervals are properly driven by cycles, and nobody counts them by hand.

**Rules:** opt-in, dry-run preview first, `Idempotency-Key` on every write (a retried post must not double-count), and never on a work order that was reopened and re-completed.

### 6.6 Consumables — deliberate, bounded duplication

**Both products track consumables**, and the overlap is real: WrenchLedger has consumables with stock levels, low-stock alerts, and job consumption; HomeAutoShop has parts and consumables in inventory with expiry tracking (SPEC FR-INV-6).

**Decision (WL-Q3): keep both, and let the operator choose.** Most HomeAutoShop users will never have a WrenchLedger account, and consumable tracking is basic shop functionality that cannot be conditional on a paid third-party subscription. So HomeAutoShop keeps its own, complete, standalone consumable tracking — and when a connection exists, the operator decides whether it stays that way.

| Setting | Behavior |
| --- | --- |
| `CONSUMABLES_OWNER = homeautoshop` *(default)* | HomeAutoShop tracks all consumables. WrenchLedger's are ignored entirely. Correct for anyone not using WrenchLedger, and for anyone who simply prefers one place. |
| `CONSUMABLES_OWNER = split` | HomeAutoShop tracks consumables that go **into a vehicle** — oil, coolant, brake fluid, filters, RTV, threadlocker. WrenchLedger tracks what the **shop** uses — abrasives, rags, gloves, cutting fluid, welding gas, blades. |
| `CONSUMABLES_OWNER = wrenchledger` | HomeAutoShop stops prompting for shop-supply consumption and records those costs as a single `shop_supplies` expense. Not recommended: vehicle fluids genuinely belong on the work order that consumed them. |

**The setting exists to prevent one specific failure: double-counting.** Cost accuracy is a stated goal (SPEC G-4), and a quart of oil recorded in both systems inflates the vehicle's lifetime cost in one and the shop's in the other, with no error message anywhere. The setting is presented at connection time with that consequence spelled out, not buried in a settings page.

The `split` line is defensible but not crisp — **brake cleaner sits squarely on it**. That is acceptable: the operator picks a side once per ambiguous item and stays consistent. What is *not* acceptable is the system silently allowing both, so HomeAutoShop warns when a consumable name closely matches one on the WrenchLedger side under `split`, and otherwise stays out of the way.

> Note this is duplication of *function*, not of *data*: no consumable record is ever mirrored across the boundary. §7's caching limits are unchanged, and NG-8 still holds — HomeAutoShop is not becoming a tool inventory, it simply already had a parts shelf.

## 7. Caching limits — what may be stored locally

Enforcing NG-8 is a matter of what the cache is *allowed* to contain.

| Field | Cached? |
| --- | --- |
| Tool id | **Yes** — the reference |
| Tool name, brand, model | **Yes** — display only, refreshed on change |
| Availability status, loan due date, calibration due date | **Yes** — the readiness gate needs them offline |
| `checked_at` | **Yes** — staleness must be visible |
| Storage location | **No** — this is WrenchLedger's core value; link out |
| Purchase price, declared value, warranty | **No** — valuation is out of scope |
| Serial numbers | **No** |
| Photos, documents, receipts | **No** — link out |
| **Borrower contact details** | **No** — and the scope granting them is never requested (§4.3) |

> **Filter on ingest with an allow-list, not a deny-list.** The live `Tool` payload is richer than the published schema and includes `purchase_price`, `current_value`, `purchase_location`, `serial_number`, `storage_location_id`, `warranty_*`, `url`, and free-text `notes` — every one of them outside the boundary. HomeAutoShop receives them on every call whether it wants them or not, so the mapping layer must **name the fields it keeps** and drop the rest at the edge. A deny-list would silently start storing valuation data the first time WrenchLedger adds a field, and NG-8 would erode without anyone deciding to erode it.

Every cached row shows its age, and anything older than the configured staleness window renders as *"last checked 3 days ago"* rather than as current fact.

---

## 8. Failure modes

| Condition | Behavior |
| --- | --- |
| No connection configured | Tool references unavailable; nothing else changes. The house placement (§10) may appear. |
| Offline Mode on (SPEC NFR-S-2) | Integration fully disabled; cached values shown with age; no calls attempted. |
| WrenchLedger unreachable | Cached values with staleness; sync retries with backoff; admin health shows last-success age (SPEC FR-ADM-8). |
| Key revoked / expired | Clear actionable error naming the key; no retry storm; integration marked needs-attention. |
| Plan downgraded to Solo | `FEATURE_NOT_IN_PLAN` reported as a **plan** problem, not a credential problem (§4.2). |
| Rate limited (`429`) | Honor backoff; not surfaced as an error unless sustained. |
| Tool deleted upstream | Reference marked orphaned, **retained** on the job item. History does not get rewritten by another system — same rule as SPEC §8.6.4. |
| Webhook signature invalid | Reject, log, do not process. Repeated failures raise a configuration warning. |

**The governing rule:** with the integration absent, broken, unsubscribed, or disabled, HomeAutoShop is a complete and correct application. Nothing in it may become *wrong* — only less convenient.

---

## 9. Security

- The API key is stored encrypted at rest, write-only in the UI, redacted from logs, exports, and error reports (SPEC NFR-S-7).
- `www.wrench-ledger.app` is added to the outbound allowlist only while the integration is enabled (SPEC §12.3).
- Redirects are never followed — required for correctness (§4.1) as well as safety.
- The webhook secret is stored the same way as the API key; the endpoint is unauthenticated by design and secured **only** by signature verification, so §5's requirements are not optional.
- The receiving endpoint is rate-limited and size-capped independently of the rest of the API.
- Minimum-scope keys (§4.3), with over-privilege warned about at setup.

---

## 10. House placement

WrenchLedger is a commercial product by the same developer, and a contextual mention inside HomeAutoShop is legitimate. HomeAutoShop's credibility, however, rests on SPEC NFR-S-1 — *no telemetry, no analytics, no phone-home, ever* — and a careless placement would break the promise the product is built on.

| Rule | Detail |
| --- | --- |
| **Static and bundled** | Ships in the image as ordinary markup. **Zero network calls** — no remote fetch, no impression beacon, no tracking pixel, no third-party ad SDK, ever. |
| **Attribution happens on the other side** | The link carries a static `?ref=homeautoshop` parameter. WrenchLedger learns of the click *when the visitor arrives on its own property* — first-party data it already collects. HomeAutoShop reports nothing, to anyone, at any time. This costs nothing in attribution quality and is the entire trick. |
| **Contextual, not ambient** | Only where it is genuinely useful: the empty state of tool references on a work order, and the integrations settings page. Not the dashboard, not a banner, not a modal, never on a phone in a garage bay. |
| **Permanently dismissible** | One dismissal, remembered forever, per instance. No re-prompting after upgrades. |
| **Disclosed** | States plainly that it is from the same developer. Undisclosed, it reads as adware; disclosed, it reads as a recommendation — which is what it is. |
| **Honest about the model** | States that WrenchLedger is a paid cloud service. It does **not** name a plan tier — the gate is arbitrary and may move (§4.2), and a placement that hard-codes it becomes wrong the day it changes. A local-first audience will otherwise assume self-hosted and free, and feel misled. |
| **Suppressed when connected** | Once a connection is configured, it disappears. Advertising to an existing customer is noise. |
| **Removable** | `SHOW_PRODUCT_LINKS=false` removes it entirely. |

> **Why the restraint serves the product rather than limiting it.** A self-hosted, local-first audience is the most ad-hostile there is. If the codebase is open they will delete an intrusive placement — or, worse, doubt the no-telemetry claim, which is load-bearing for why anyone runs this at all. A quiet, honest mention appearing exactly when someone is looking for tool tracking will convert better than a banner and costs nothing that matters.

---

## 11. Resolved questions

| ID | Question | Resolution |
| --- | --- | --- |
| WL-Q1 | Does `GET /tools` support filtering by a set of ids? | **No** — confirmed against `openapi.yaml`. It does not matter: `updated_after` is documented as the efficient change-poll, so the sync is a delta loop rather than N fetches (§3.2). The one real constraint found is that **schedules are per-tool with no workspace-wide list**, mitigated by polling only referenced tools. |
| WL-Q2 | Canonical per-tool deep link | `https://www.wrench-ledger.app/tools/{tool_uuid}` (§4.1). |
| WL-Q3 | Where does the consumables boundary fall? | **Both keep their own; the operator chooses** via `CONSUMABLES_OWNER` (§6.6). Consumable tracking is basic shop functionality and cannot be conditional on a paid third-party subscription. The setting exists specifically to prevent double-counted cost. |
| WL-Q4 | Should the pairing be reciprocal? | **No WrenchLedger changes at this time.** The proposal is preserved verbatim in §13 for manual transfer to the WrenchLedger backlog when it is wanted. |
| WL-Q5 | Does the Shop-plan gate make this worth building? | The gate was an **arbitrary** product decision and may be relaxed — this integration is plausibly the justification. HomeAutoShop therefore **never assumes a plan tier**, reads entitlement from the response, and requires no change whichever way the gate is set (§4.2). |
| WL-Q6 | Should HomeAutoShop consume `consumable.low_stock`? | **No.** WrenchLedger already handles those notifications; duplicating them here buys nothing and is the first step toward mirroring its inventory. |

### 11.1 Second round — resolved against a live workspace

| ID | Question | Resolution |
| --- | --- | --- |
| WL-Q7 | Does `/reports/dashboard` carry usable due data? | **Yes — one call returns `schedules_due`, `maintenance_due`, `loans_overdue`, `loans_upcoming`, `consumables_low`, and `lead_days`.** But every array is windowed by a per-user setting, so it is a convenience, not the gate (§3.4). `reports:read` is **optional**. |
| WL-Q8 | Does a loan bump the tool's `updated_at`? | **No** — `on_loan` is an `EXISTS` subquery against `tool_loans`, with no trigger and no `UPDATE public.tools` on any loan path. This reshaped the sync: **availability comes from a direct, unwindowed `GET /loans?open_only=true`**, not from tool deltas and not from the dashboard (§3.3). Kit checkouts generate `tool_loans` rows, so that one call is complete. |
| WL-Q9 | Actual rate limits | **120 requests per fixed window**, with `RateLimit-Limit` / `-Remaining` / `-Reset` headers and `Retry-After` on `429`. Generous relative to a two-call sync; headers are still honored rather than assumed. |

### 11.2 Third round — resolved

| ID | Question | Resolution |
| --- | --- | --- |
| WL-Q10 | Whose `reminder_lead_days` does the dashboard resolve to? | **Moot, by design.** They can legitimately differ per person, so HomeAutoShop **keeps its own lead window** and never inherits one from another product — most of its users will not have a WrenchLedger subscription at all. The dashboard is demoted to an optional convenience (§3.4). Same principle as SPEC OQ-9 and §6.6: an optional integration never dictates core behavior. |
| WL-Q11 | Is `updated_after` inclusive or exclusive? | **Strictly exclusive** — `t.updated_at > p_updated_after` (migration `0051`). So the watermark is the **maximum `updated_at` observed in the drained pages**, never the request time; a request-time watermark would silently skip anything written between query execution and response (§3.5). No millisecond nudging needed. |

### 11.3 Fourth round — found by using it, then settled from the audit log

Reported as: *most tools cannot be found by name, and the few that can look
arbitrary.* Three assumptions in the adapter had never met the API — every test
covering them mocks the client itself — and each one was wrong.

They were answered by reading one instance's outbound audit log, which records
host, path, purpose and status for every call and survives restarts. **206
WrenchLedger calls; the numbers below are from that table**, not from reasoning
about what the API probably does.

| ID | Question | Resolution |
| --- | --- | --- |
| WL-Q12 | Where does `GET /tools` put its continuation token? | **Not at the top level under `next_cursor`, which was the only place the adapter looked.** The log settles it: **19 `/loans` calls and 19 pre-rebuild `/tools` calls** — one page per sync run, nineteen runs. A drain that had merely hit the 20-page bound would show twenty calls in *one* run. The cursor is now looked for as tolerantly as the rows already were (`next_cursor` / `nextCursor` / `cursor` / `next` / `next_page`, top level or under `meta` / `pagination` / `page` / `paging` / `links`), and the drain logs which key it found, once per run, so the day the envelope moves again is a line somebody can read rather than a silent truncation. |
| WL-Q13 | What happens when a drain stops early? | **It loses the tail permanently and says nothing.** `updated_after` is strictly exclusive (WL-Q11), so advancing the watermark to the newest row of an unfinished page skips every older tool for good. Measured: a **642-tool workspace had 140–160 tools cached** — about a quarter — after nineteen runs each ratcheting the watermark forward one page. A **full page with no recognized cursor now leaves the watermark alone**: that shape is the signature of a token we failed to find, and repeating one page is cheap where guessing is not. The drain also sends its own `limit`, so "a full page" is a fact this client can recognize rather than the server's default. |
| WL-Q14 | Does `GET /tools` support a `q` search parameter? | **No, and it does not ignore it — it refuses.** **137 `/tools` calls, every one HTTP 400, not a single success.** The remote half of tool search therefore never worked, on any query, since the day it was written; the failure was caught and reported to the operator as *"WrenchLedger did not answer"*, which was both alarming and false. The API answered at once and said the request was malformed. **The remote search is gone.** Searching is local, over the cache the drain fills — the documented path, the one that works, and instant and offline-capable besides. A complete cache is what makes it sufficient, which is why the drain was always the real bug. |
| WL-Q15 | May a `ShopTool` id be sent to WrenchLedger unchecked? | **No.** A tool named on a job that the picker could not match is recorded under the typed text (FR-WL-7 — the integration is never load-bearing), so `Vacuum pump` became a tool id. Tier 3 then asked for `/tools/Vacuum pump/schedules` on every sync — **3 calls, status 0**, failing at the socket on the unencoded space. Ids are checked for UUID shape before any per-tool call, because asking about something the other system has never heard of is not a fetch that failed but a question that should not have been posed. Path segments are percent-encoded regardless. |

**How these compounded.** The search was broken, so the picker could not find a
tool by name; the operator typed the name anyway and it was stored as an id;
that invented id then generated a failing call on every sync thereafter. One
root cause — the truncated drain — reached the screen as three unrelated-looking
faults.

**Consequence for an instance already running.** Fixing the drain does not fill
the hole a truncated drain left; the watermark has moved past it. **Read every
tool again** on the integrations screen clears the watermark and reads the
workspace from the start — 642 tools on the instance above, against the 140–160
it had. That screen and the tools screen both now state how many tools are
cached, because a search over four rows and a search over four hundred fail
identically from the outside.

---

## 12. Held proposal — reciprocal pairing (no action)

Recorded here at the operator's direction (WL-Q4) for **manual transfer to the WrenchLedger backlog**. Nothing in this section is a HomeAutoShop requirement, and nothing here is being built.

> ### Proposal: vehicle-side awareness in WrenchLedger
>
> **Context.** WrenchLedger's roadmap currently contains no vehicle or automotive concept. Its audience — people with a workshop, tools worth cataloging, and things they maintain — overlaps heavily with HomeAutoShop's, and the two products already share a developer and a design sensibility.
>
> **The asymmetry today.** HomeAutoShop tells its users that WrenchLedger exists (§10). WrenchLedger tells its users nothing. Discovery therefore only flows from the smaller, newer product to the established one, which is backwards from where the traffic actually is.
>
> **Minimum viable version.** A contextual mention wherever a WrenchLedger user is doing something vehicle-shaped — a tool categorized as automotive, a project named after a car, a maintenance schedule on something with an odometer meter kind. Same restraint as §10: static, contextual, dismissible, disclosed, and suppressed once the user is known to have HomeAutoShop.
>
> **A more interesting version.** WrenchLedger already has `meter_kind` values of `miles` and `kilometers`, which means it can already represent a vehicle as an asset. A user tracking a car there today is using the wrong tool and does not know a better one exists. That is the highest-intent moment there will ever be for a referral.
>
> **What it is not.** Not a merged product, not shared accounts, not data sync between hosted and self-hosted instances. A link and a sentence.

---

## 13. Delivery

Aligns with SPEC §15 Phase 4. Each stage is independently shippable.

| Stage | Contents |
| --- | --- |
| **W1 — Connect and reference** | Settings connection with key entry, `/workspace` connectivity test distinguishing credential from plan failures, generated OpenAPI client, tool picker, `tool_ref` on job items, link-out. |
| **W2 — Readiness (the payoff)** | Pull sync job, cached availability with staleness, the readiness gate on work orders and planning dashboard, degradation behavior. |
| **W3 — Optional depth** | Project mirroring (§6.3), custody (§6.4), meter post-back (§6.5) — each independently toggleable, all off by default. |
| **W4 — Webhooks** | Signed receiver with full §5 verification, invalidate-and-re-read handling, delivery log. **Only for operators who already expose the instance.** |

House placement (§10) is not a stage; it ships with W1 and disappears once W1 is used.
