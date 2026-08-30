# HomeAutoShop ↔ LubeLogger Integration

|  |  |
| --- | --- |
| **Document** | `Artifacts/INTEGRATION-LUBELOGGER.md` |
| **Status** | L1 implemented; L2/L3 not built |
| **Version** | 0.2.0 |
| **Date** | 2026-08-29 |
| **Parent spec** | [SPEC.md](SPEC.md) |
| **Summarized in** | SPEC.md §8.6 |
| **Implements** | SPEC.md `FR-INT-11`–`FR-INT-16` |
| **Counterpart** | LubeLogger, self-hosted by the operator — REST API documented at `{base}/api` |

---

## 0. Purpose

[LubeLogger](https://lubelogger.com) is a mature self-hosted vehicle maintenance and fuel tracker with a documented REST API. This integration exists for the operator who already runs one and already has years of history in it.

> **Scope discipline (OQ-9). LubeLogger is optional and additive, never a dependency.** HomeAutoShop owns the maintenance schedule (§7.7) and every core function outright; an instance with no LubeLogger configured is not a degraded instance, and no feature, report, or dashboard may be built such that it needs LubeLogger to be correct. The integration exists to spare *this* operator from re-keying history — it is a migration convenience with an optional sync, not a pillar of the architecture. Anything that starts to feel like a pillar is scope creep and should be cut.
>
> **Consequence for cost-per-mile (FR-COST-3):** since fuel is out of scope for good (OQ-3/NG-7) and LubeLogger cannot be a dependency, FR-COST-3 measures **repair and ownership cost per distance, excluding fuel, by design.** That is stated plainly in the report rather than quietly omitted — and it is arguably the more useful number for a repair system anyway.

## 1. Supported modes

| Mode | Direction | Posture |
| --- | --- | --- |
| **A — One-time migration** | LubeLogger → HomeAutoShop, once | Import everything, verify, then retire LubeLogger. Clean, but gives up LubeLogger's fuel/MPG tooling. |
| **B — Ongoing pull sync** *(recommended)* | LubeLogger → HomeAutoShop, scheduled | LubeLogger continues as the operator's fuel/MPG tool; HomeAutoShop owns repairs, parts, inventory, cost, documents, **and the maintenance schedule**, and pulls odometer readings (and fuel as an expense) as a convenience. Nothing in HomeAutoShop depends on the pull succeeding. |
| **C — Odometer push-back** *(MAY, opt-in)* | HomeAutoShop → LubeLogger | Garage-captured odometer readings pushed back so LubeLogger's own projections stay accurate. Append-only on their side, requires an Editor-scoped key, off by default, dry-run first. |
| **D — Bidirectional sync** | **Out of scope** | Two independently mutable systems with no shared identity model. The conflict surface is large, the failure mode is silent data corruption across app boundaries, and the benefit over B is small. |

> **OQ-3 settled: no native fuel logging, ever.** Fuel and MPG are not repair functions and LubeLogger handles them well. A LubeLogger fuel record still maps cleanly onto a `usage_reading` plus a `fuel`-category `expense` when imported, so the odometer series stays dense — but HomeAutoShop never asks the operator to log a fill-up, and never needs to.

## 2. API facts (verified against LubeLogger's documentation)

| Aspect | Detail |
| --- | --- |
| Base | Operator-supplied, e.g. `https://lubelogger.home.arpa` |
| Discovery | The instance self-documents its endpoints at `{base}/api`; the adapter pins exact paths at implementation time against the operator's running version. |
| Auth | `x-api-key: <token>` header (preferred), or `apiKey=` query param, or HTTP Basic. Keys carry **Viewer / Editor / Manager** scopes — **a Viewer key is sufficient for Mode B and is what the setup UI must ask for.** |
| Record types | Vehicles, Service, Repair, Upgrade, Fuel, Odometer, Tax, Plans, Reminders, Supply, Inspections, Equipment, Notes. |
| Filtering | v1.4.8+ GET endpoints accept `Id`, `StartDate`, `EndDate`, `Tags` — used for **incremental** pulls by date window rather than full refetches. |
| Payloads | JSON for writes (preserves numeric types). |
| **Locale hazard** | By default responses are **locale-formatted strings**. The adapter **MUST** send the `culture-invariant` header (or the operator sets `LUBELOGGER_INVARIANT_API=true`) to get type-rich, locale-invariant values. Skipping this silently mis-parses decimals and dates — a `1.234,56` fuel cost imported as `1.23` is the kind of bug that is never noticed. The connectivity test must assert invariant formatting and refuse to import without it. |

## 3. Entity mapping

| LubeLogger | HomeAutoShop | Notes |
| --- | --- | --- |
| Vehicle | `vehicle` | Matched on VIN; falling back to year/make/model with **explicit operator confirmation per vehicle**. Never auto-merged on a fuzzy match. |
| Odometer Record | `usage_reading` (meter `odometer`) | Append-only, deduped by `external_ref`. |
| Fuel Record | `usage_reading` + `expense` (category `fuel`) | Per Mode B above. Volume and unit retained on the expense for later use, though OQ-3 is settled and no MPG feature is planned. |
| Service Record | `work_order` (`type=maintenance`, `status=complete`) + `job_item` | Cost lands as an `expense` on the work order unless itemized parts are present. |
| Repair Record | `work_order` (`type=repair`, complete) | |
| Upgrade Record | `work_order` (`type=modification`, complete) | |
| Inspection | `work_order` (`type=inspection`, complete) | |
| Tax Record | `expense` (`registration` / `inspection` / `insurance`) | |
| Plan Record | `work_order` (`status=planned`) | |
| Reminder | `asset_service_item` | Interval-based reminders map directly; date-only reminders become time-interval items. **Ownership is a setting (OQ-10), defaulting to HomeAutoShop:** it imports LubeLogger reminders once as seed, then owns them, and the setup UI suggests disabling LubeLogger's so the operator is not nagged twice. An operator who prefers LubeLogger's reminders can flip that and HomeAutoShop stops evaluating imported items. |
| Supply Record | `part` + `stock_lot` | Best-effort; supplies are loosely structured in LubeLogger, so these import as drafts for review. |
| Note | `work_order_note` or vehicle note | |
| Attachments on any record | `media` + `media_link` | Fetched and stored locally — **the import must not leave documents behind on the old instance**, or retiring it later loses them. |
| Equipment | *not imported* | Non-plated assets are out of v1 scope; roadmap R-3 and candidate C-2. |

## 4. Sync semantics

- **Idempotent.** Every imported row gets an `external_ref` (§6.2) keyed on source system + instance URL + external type + external id. Re-running an import updates rather than duplicates.
- **Dry run first.** Every import and sync runs in preview mode by default, reporting counts per entity type, unmatched vehicles, and a sample of mapped records, before anything is written.
- **Externally-owned records are marked as such** in the UI, with a link back to the source record in LubeLogger.
- **Local edits win, and are never clobbered.** If a synced record has been edited locally and the source then changes, the pull records a conflict for review — same philosophy as §5.4, applied across an app boundary.
- **Deletions never propagate.** A record deleted at the source is marked orphaned locally and retained. Silent deletion driven by another system is unacceptable in a service history that may be handed to a buyer (G-1).
- **Failure is loud and safe.** An unreachable instance, an expired key, or a version whose API shape no longer matches leaves prior imports untouched and surfaces in admin health (FR-ADM-8).
- **Scheduling.** A `lubelogger.sync` job runs on a configurable interval (default daily), uses date-window filtering for incremental pulls, and is disabled entirely by Offline Mode.

---

## 5. Delivery

Aligns with SPEC §15. Each stage is independently shippable, and the first captures most of the value.

| Stage | Phase | Contents |
| --- | --- | --- |
| **L1 — One-time import** ✅ **built** | Phase 2 | Connection with API key, connectivity test asserting **locale-invariant formatting** (§2), `external_ref` provenance, dry-run preview, then migrate. This is the stage that matters: it back-fills real history so the cost reports built in the same phase are meaningful on day one. |
| **L2 — Scheduled pull sync** | Phase 4 | Incremental date-window pulls, conflict surfacing, orphan handling. Deliberately later — it is ongoing maintenance burden for a fraction of L1's value, and per OQ-9 nothing may depend on it. |
| **L3 — Odometer push-back** | Phase 4, optional | Mode C. Opt-in, dry-run first, Editor-scoped key. |

## 6. Open questions

| ID | Question | Bearing |
| --- | --- | --- |
| LL-Q1 | Which exact endpoint paths does the running instance expose under `{base}/api`? | The adapter pins them at implementation time (§2). Worth capturing once against the operator's actual version rather than rediscovering during a failed import. |
| LL-Q2 | After L1 lands and history is imported, is Mode B actually wanted? | If LubeLogger continues only as a fuel tracker, the ongoing pull may buy nothing beyond a denser odometer series. Deciding *after* the import — with real data in front of you — is the cheaper order. |
| LL-Q3 | Do LubeLogger's Supply Records carry enough structure to import as parts, or should they come in as expenses? | They import as drafts for review either way (§3); this decides whether that review is a formality or real work. |

---

## 7. Implementation notes (L1, built)

| | |
| --- | --- |
| Client | `homeautoshop/core/integrations/lubelogger.py` |
| Importer | `homeautoshop/core/integrations/importer.py` |
| Command | `manage.py import_lubelogger [--check] [--commit] [--create-missing]` |
| Provenance | `core.ExternalRef`, unique on source + instance + type + external id |
| Tests | `homeautoshop/core/tests_lubelogger.py` — 22 tests against a stub client |

**Verified behavior:** dry run writes nothing (including no `ExternalRef` rows),
re-running skips what is already imported, a source edit after import is
reported as a conflict rather than applied, a source deletion is never
propagated, and a missing endpoint is reported and skipped rather than aborting
the run.

**The locale guard is two-layered.** `--check` samples the response and refuses
to proceed on comma-decimals; `parse_number` then refuses strictly during the
import itself. A mis-configured instance fails loudly instead of importing
`1.234,56` as `1.23`.

**Still to verify against a live instance (LL-Q1).** Endpoint paths are pinned
in `ENDPOINTS` and were written against LubeLogger's documented record types,
not observed traffic. Run `--check` and then a dry run against
a live instance before committing; any path that 404s will be reported by
name and can be corrected in that one dictionary.

**Deliberately not imported:** Reminders (HomeAutoShop's maintenance schedule is
Phase 3, and importing them now would create records with nothing to attach to)
and Equipment (non-plated assets, roadmap R-3).
