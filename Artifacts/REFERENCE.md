# HomeAutoShop Reference

|  |  |
| --- | --- |
| **Document** | `Artifacts/REFERENCE.md` |
| **Status** | Draft for review |
| **Version** | 0.1.0 |
| **Date** | 2026-08-29 |
| **Parent spec** | [SPEC.md](SPEC.md) |
| **Contents** | Work order state machine · seed data · glossary |

---

## 1. Work order state machine

```
                    ┌──────────┐
                    │ planned  │◄────────────────┐
                    └────┬─────┘◄──── un-start ──┤
                         │ start                 │ reopen
                    ┌────▼─────────┐             │
             ┌──────┤ in_progress  ├──────┐      │
             │      └────┬─────────┘      │      │
    block    │           │ complete       │ pause│
    ┌────────▼─────────┐ │      ┌─────────▼────┐ │
    │ waiting_on_parts │ │      │  on_hold     │ │
    └────────┬─────────┘ │      └─────────┬────┘ │
             │ unblock   │                │      │
             └──────►────┤◄───────────────┘      │
                         │                       │
                    ┌────▼─────┐            ┌────┴──────┐
                    │ complete │            │ abandoned │
                    └──────────┘            └───────────┘
```

- **Any open state can return to `planned`.** Not in the diagram above and it should have been: starting a job by accident is not a rare event in a home shop, and without this the only way back was to complete the work order and reopen it — pushing a false completion through the record, and firing every service completion attached to it, to undo a mis-tap.
- **`planned` can go straight to `waiting_on_parts`.** Also not in the diagram, and for the same reason as the rule above it: listing the parts a job needs exists so that a shortfall is found while the job is still being planned, and without this edge the only way to record one was to start the job first — a false statement about the shop, written to get around the graph.
- `waiting_on_parts` **requires** a linked purchase or a note explaining the block; this state drives the dashboard's blocked list (FR-REP-1).
- **A work order can be deleted from any state**, which is a separate question from the graph. The graph governs the work; it has nothing to say about a record that should not exist. Deletion is the ordinary soft delete — 30-day trash, restorable (P-5) — and is refused only while other work orders name it as their parent.
- `complete` requires `odometer_out` (FR-WO-9) and triggers `service_completion` for every linked job item (FR-MAINT-5).
- `abandoned` is a first-class outcome. Home shop projects genuinely get abandoned, and recording that honestly is more useful than an eternally open work order.
- Reopening a completed work order is allowed, is audit-logged, and **does not** reverse service completions — a later correction is a new completion record, not a rewrite of history.

---

## 2. Seed data shipped with the application

- Generic schedule templates: gasoline normal, gasoline severe, diesel, EV, and a small-engine/hours-based template.
- Generic SAE J2012 DTC dictionary (P0/P2/P34xx, generic B/C/U ranges).
- Part categories, expense categories, and vendor types.
- Common location layout starter (`Shelf A–D`, `Fluids cabinet`, `Under bench`).
- Service-information providers: LEMON (default, with mirror list), Operation CHARM, ALLDATA DIY (seeded and enabled, per-vehicle subscription status).
- Equipment schedule templates: small engine, generator, mower — hour-based and season-based.
- Inspection templates ([SCHEMA-INSPECTION-TEMPLATES.md](SCHEMA-INSPECTION-TEMPLATES.md)): **pre-purchase inspection**, annual safety/roadworthiness, seasonal (winter prep / storage), post-repair quality check, and a motorcycle-specific template. Class-scoped per `vehicle_class`.
- **Parts-order readers.** RockAuto order confirmations, read by word geometry (`purchasing/importers/rockauto.py`) against a corpus of nine real orders captured as redacted text and geometry — the originals are shipping documents carrying a name, a street address, a phone number and an email, and are never committed.
- Parser profiles for any scan-tool report formats with a sample corpus at build time. **Ships empty until the first sample arrives** (§8.3a) — the manual mapping wizard covers the gap.
- A demo dataset (three vehicles with history) that installs and uninstalls cleanly, for evaluation and for tests.

---

## 3. Glossary

| Term | Meaning |
| --- | --- |
| **Work order** | One unit of shop work on one vehicle: what was wrong, what was done, what it took. |
| **Job item** | One line of work within a work order, independently completable. |
| **Three C's** | Complaint, Cause, Correction — the standard structure for a repair record. |
| **Core / core charge** | A refundable deposit on a rebuildable part, refunded when the old unit is returned. |
| **DTC** | Diagnostic Trouble Code, e.g. `P0301`. |
| **Readiness monitor** | Self-test status an OBD-II system reports; reset by clearing codes, required complete for emissions testing. |
| **Fitment** | The assertion that a specific part fits a specific vehicle. |
| **Stock lot** | A quantity of one part acquired at one price at one time, in one location. |
| **vPIC** | NHTSA's Product Information Catalog and Vehicle Listing — the free VIN decode service. |
| **TSB** | Technical Service Bulletin — a manufacturer advisory, not a recall. |
| **WMI** | World Manufacturer Identifier — the first three VIN characters. |
| **PWA** | Progressive Web App — installable, offline-capable web application. |
| **Parser profile** | Declarative extraction rules for one scan tool's report format — fingerprint, field extractors, DTC table locator. Data, not code ([SCHEMA-PARSER-PROFILES.md](SCHEMA-PARSER-PROFILES.md)). |
| **Pinned link** | A service-information URL resolved once by a human and saved to a vehicle, because these libraries index by catalog string rather than by a computable key (§8.5). |
| **LEMON / Operation CHARM** | Free, no-signup service-manual libraries. CHARM covers through ~2014; LEMON supersedes it at `lemon-manuals.la` with ~10,000 US/Canada vehicles, 1960–2025. |
| **LubeLogger** | Self-hosted vehicle maintenance and fuel tracker with a REST API. Where an operator already runs one, its history is importable (§8.6). |
| **`external_ref`** | Provenance row linking a local record to the external system it was imported from; what makes imports idempotent. |
| **DVI** | Digital Vehicle Inspection - A digital checklist the owner/technician uses to evaluate vehicle condition/status. |
| **Asset** | The core entity: a vehicle or a piece of serviceable equipment. One table, `asset_kind` distinguishing them (§6.2). |
| **Usage reading** | A meter observation — odometer, engine hours, or cycles. Generalizes the odometer so equipment uses the maintenance machinery unchanged. |
| **WrenchLedger** | Cloud SaaS workshop tool inventory by the same developer — what you own, where it lives, who borrowed it. Integrated with (§8.7), never rebuilt (NG-8). |
| **PPI** | Pre-purchase inspection — a DVI run against a `prospect` vehicle you do not own yet (FR-DVI-10). |
| **Component** | An installed part instance with its own life: position, install date and odometer, removal, warranty (`asset_component`). What makes a repeated measurement into a wear rate. |
| **DOT date code** | Four digits on a tire sidewall giving week and year of manufacture. Age condemns a tire independently of tread depth (FR-CMP-6). |
| **`auto_status`** | The inspection status a template's thresholds computed, stored alongside the human's answer so an override is visible rather than lost. |
