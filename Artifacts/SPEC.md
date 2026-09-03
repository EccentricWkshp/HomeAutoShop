# HomeAutoShop — Product & Technical Specification

|  |  |
| --- | --- |
| **Document** | `Artifacts/SPEC.md` |
| **Companion documents** | [README.md](README.md) · [REFERENCE.md](REFERENCE.md) · [SCHEMA-PARSER-PROFILES.md](SCHEMA-PARSER-PROFILES.md) · [SCHEMA-INSPECTION-TEMPLATES.md](SCHEMA-INSPECTION-TEMPLATES.md) · [INTEGRATION-LUBELOGGER.md](INTEGRATION-LUBELOGGER.md) · [INTEGRATION-WRENCHLEDGER.md](INTEGRATION-WRENCHLEDGER.md) |
| **Status** | **Built.** v1 scope is complete. §15.1 records what shipped; §15.2 records what remains. |
| **Version** | 0.7.0 |
| **Date** | 2026-09-03 |
| **Scope decisions** | Docker Compose deployment · all four feature modules in scope · household multi-user with garage PWA · four external integrations |
| **Where status lives** | **§15 is the plan · §15.1 is what was built · §15.2 is what remains.** No other section carries a status of its own. §17 (roadmap), §18 (candidates) and §19 (claims that outran the code) keep the reasoning behind each decision and point at those three for its state. When two sections disagree, §15.1 and §15.2 are the ones that are right. |
| **v0.7.0 changes** | **Status consolidated, and four contradictions removed.** It had accumulated in six places: a phase table, a roadmap where eight of ten rows were struck through, a candidate list still calling three shipped features *not yet in scope*, an open-questions table holding two questions §15.1 had already answered, and a *stated but not built* section that was the only honest inventory of the gaps. Those are now one plan (§15), one done-ledger (§15.1) and one remaining-list (§15.2), with the rationale left where it was written rather than copied forward. Every remaining claim was re-checked against the code while doing it, and three had moved: the translation catalogs are **597 untranslated and 317 fuzzy** after a fresh `makemessages` rather than *about a thousand behind*; **C-3 and C-5 are built**; and **FR-WO-11 — duplicate a work order as a template — is not**. That last one matters more than its size: C-6 cited FR-WO-11 as the reason not to build a checklist system, which is §19's failure mode exactly — a decision resting on a requirement nobody had checked. Release history moved to the appendix, one line each. **And no question in this document is open**: OQ-17 had been answered as WL-Q4 in the WrenchLedger document, so it went on being listed here long after it was settled — the same drift in the other direction. |

---

## 1. Summary

HomeAutoShop is a **self-hosted, local-first shop-management application** for a home/DIY garage working on personally owned vehicles (plus the occasional friend-or-family car). It replaces the shoebox of receipts, the glovebox notebook, and the spreadsheet with a single system of record covering: what vehicles exist, who owns them, what work was done, what parts went in, what it cost, what is due next, and where the paperwork lives.

It is **not** a commercial shop-management system. There is no invoicing, no customer billing, no technician payroll, no parts-markup pricing, no accounting integration. The optimization target is a person standing in a garage with dirty hands and a phone — and that same person three years later trying to remember which brand of caliper they used.

The design bias throughout: **your data lives on your hardware, works with the internet unplugged, and can be exported in full at any time.**

---

## 2. Goals and non-goals

### 2.1 Goals

| ID | Goal |
| --- | --- |
| G-1 | One durable, searchable history per vehicle — from purchase to sale — that survives tool changes and can be handed to a buyer as a PDF. |
| G-2 | Capture work *while it happens*, from a phone in the garage, in under 30 seconds per entry. |
| G-3 | Know what is due, overdue, and coming up across every vehicle without having to think about it. |
| G-4 | Know what a vehicle has actually cost — parts, consumables, outsourced work, and your own time. |
| G-5 | Find a part: what fits, what is on the shelf, what it cost last time, and who sold it. |
| G-8 | See a problem coming before it strands someone — inspect on a schedule, measure the same things each time, and trend them (§7.8). |
| G-6 | Run entirely on hardware you control, with no cloud account required and no vendor able to take it away. |
| G-7 | Full function with the WAN down; external services are accelerators, never dependencies. |

### 2.2 Non-goals

| ID | Non-goal | Rationale |
| --- | --- | --- |
| NG-1 | Customer invoicing, estimates, payments, sales tax | Not a commercial shop. |
| NG-2 | Multi-tenant / SaaS hosting | One household instance per deployment. |
| NG-3 | Public internet exposure as the default posture | LAN-first; remote access is the operator's choice, via their own VPN. |
| NG-4 | Real-time telematics / always-connected OBD dongles | Scan data is imported per session, not streamed. |
| NG-5 | Being a repair-procedure database | It stores and links *your* documents; it does not license OEM service information. |
| NG-6 | Horizontal scale, HA, clustering | Single-node by design (§11). |
| NG-8 | **Tool and toolbox inventory** — what tools you own, what they are worth, which drawer they live in, who borrowed them | Out of scope, permanently (OQ-15). [WrenchLedger](https://wrench-ledger.app) already does this well and represents significant effort that must not be duplicated here. HomeAutoShop integrates with it (§8.7) rather than competing with it. |
| NG-9 | Tool calibration and tool-loan management | Same reason as NG-8. HomeAutoShop may *consume* calibration and loan status from WrenchLedger to warn on a job (§8.7), but never becomes the place that data is entered. |
| NG-7 | Fuel/energy logging — **settled, not deferred** (OQ-3) | Not a repair function, and handled well by LubeLogger. FR-COST-3 is therefore repair-cost-per-distance with fuel excluded by design, and says so in the report. |

---

## 3. Personas and primary scenarios

### 3.1 Personas

- **Operator (primary).** Owns the shop and the instance. Turns wrenches, buys parts, administers users. Phone in the garage, desktop at the bench.
- **Household member.** Logs an odometer reading, uploads a receipt, checks whether the inspection is due. Trusted, but not the administrator.
- **Helper.** A friend wrenching alongside for a weekend. Adds notes and photos to the job at hand. Trusted at the same level as a household member — **there are no granular per-vehicle permissions in this design** (see §12.2).
- **Vehicle owner (non-user).** A person a vehicle belongs to who never signs in — recorded as a `person`, nothing more.

### 3.2 Scenarios the design must serve

1. **Bay capture.** Phone, gloves off for ten seconds: photo of a cracked bushing → attached to the open work order with a note → back to work.
2. **Parts counter.** Needs the exact part number of the alternator installed 18 months ago, and whether the core was ever returned.
3. **Saturday planning.** Which of five vehicles is overdue for anything, and which parts are already on the shelf for it.
4. **Diagnostic session.** Pulls three DTCs with a scan tool, imports them, opens a work order from them, resolves two, leaves one pending.
5. **Sale.** Exports a complete, dated service history PDF with receipts for the buyer.
6. **New-to-me vehicle.** Scans the VIN barcode on the door jamb, gets year/make/model/engine populated, checks open recalls, assigns a maintenance schedule, records purchase price and odometer.
7. **Pre-purchase.** Standing in a stranger's driveway with no signal, walks a car being considered for purchase against the PPI template — tread depths, DOT codes, rust, cold start — photographs the bad news, and leaves with a report that either justifies the price or ends the conversation.
8. **Cold reality.** The NAS dies. New machine, `docker compose up`, restore last night's backup — the shop is back, including every photo.

---

## 4. Principles and hard constraints

| ID | Principle | Consequence in this spec |
| --- | --- | --- |
| P-1 | **Local-first.** The instance is authoritative. | No cloud sync service. External APIs are cached on first success and never sit on the critical path (§8). A master **Offline Mode** switch disables all outbound traffic (§12.4). |
| P-2 | **Offline-tolerant client.** The garage has bad Wi-Fi. | PWA with a service worker: cached reads of recent vehicles and work orders, queued writes with a defined conflict policy (§5.4). |
| P-3 | **Semi-lightweight.** Homelab-sized, not enterprise-sized. | Five containers maximum. Postgres does full-text search (`tsvector`) *and* the job queue (`LISTEN/NOTIFY` + a jobs table). **No Elasticsearch, no Redis, no message broker, no separate cache tier.** |
| P-4 | **Own your data.** | Documented schema, full export to a self-describing ZIP, blobs stored as ordinary files addressable without the app (§13.3). |
| P-5 | **Never lose a capture.** | Photos, notes, odometer readings, and time entries are append-only and always accepted by the server (§5.4). Deletes are soft, with a 30-day trash. |
| P-6 | **Honest about external data.** | Where no free authoritative source exists (TSBs, VIN-level recall status, OEM schedules), the spec says so and specifies manual entry plus an import path (§8). |
| P-7 | **Degrade, do not block.** | A failed VIN decode, an expired API key, or a missing schedule template never prevents creating the record by hand. |

### 4.1 Hard constraints

- **C-1 — TLS is mandatory, not optional.** The PWA needs a *secure context*: service workers, `getUserMedia` (camera / VIN barcode scanning), Web Serial, and Web Bluetooth (ELM327) all refuse to run over plain HTTP on a non-`localhost` origin. LAN deployments therefore ship TLS by default (§5.3).
- **C-2 — Single writer node.** One `app` container owns the database. Scaling out is explicitly unsupported.
- **C-3 — Media dominates storage.** Photos and scanned documents will exceed relational data by two orders of magnitude. Blob storage is separate, and backup treats the two differently (§13).
- **C-4 — Browser support.** Chromium-based browsers are the supported target for OBD-II adapter features (Web Serial and Web Bluetooth are unavailable in Safari and Firefox). Every other feature must work in current Safari, Firefox, and Chromium.

---

## 5. Architecture

### 5.1 Deployment topology

```
                        ┌───────────────────────────────────────────┐
  phone / tablet / PC   │  proxy   (Caddy)                          │
      ──── HTTPS ─────► │  TLS termination, HTTP/2, static assets   │
                        └───────────────┬───────────────────────────┘
                                        │
                        ┌───────────────▼───────────────────────────┐
                        │  app     (REST API + web UI + PWA)        │
                        │  stateless; owns all auth and authz       │
                        └───┬───────────────────────┬───────────────┘
                            │                       │
              ┌─────────────▼──────────┐   ┌────────▼─────────────────┐
              │  db   PostgreSQL 18+   │   │  media-data  (volume)    │
              │  relational + FTS +    │   │  originals + derivatives │
              │  job queue             │   │  read only via `app`     │
              └─────────────▲──────────┘   └────────▲─────────────────┘
                            │                       │
                        ┌───┴───────────────────────┴───────────────┐
                        │  worker   background jobs                 │
                        │  thumbnails, OCR, decode refresh,         │
                        │  recall polling, reminders, backups       │
                        └───────────────────────────────────────────┘
```

Four services, one `docker-compose.yml`, three named volumes (`db-data`, `media-data`, `backup-data`).

| Service | Image | Responsibility | Ports |
| --- | --- | --- | --- |
| `proxy` | `caddy:2-alpine` | TLS (internal CA by default), HSTS, compression, access logs | 443, 80 → 443 |
| `app` | project image | REST API, server-rendered shell, PWA manifest and service worker, authentication, authorization, validation | internal 8080 |
| `worker` | same image, different entrypoint | Async jobs; waits on the `jobs` table via `LISTEN/NOTIFY` | none |
| `db` | `postgres:18-alpine` | System of record, full-text search, job queue | internal 5432 |

**Compose profiles:** `default` (all four) · `slim` (drop `worker`; jobs run in-process in `app` — fine below roughly five vehicles) · `dev` (bind mounts, seed data, mail catcher).

> **Why the filesystem rather than a bundled object store.** The argument for one is that a presigned URL lets a 4 MB photo stream straight to the browser without passing through `app`, which matters on a low-power host over garage Wi-Fi. That URL is not issued. It is a bearer token in a querystring — copied out of an address bar it works for anybody holding it — so `app` serves the bytes itself and reading a photo requires being signed in (§12.3). With that fast path off by default, a store in this file would be a network hop to the same disk, ~180 MB of RSS against NFR-P-6, and a fifth thing to keep patched. It would also break §13.1: a backup copies `MEDIA_ROOT`, and would hold no photos at all.
>
> The `s3` driver is supported and vendor-neutral (§14) for operators who want media on a NAS, on rented storage, or on a host other than the one running the application — with the backup caveat above, which the application states when a backup is taken and again before a restore. `manage.py migrate_storage` moves files either direction without touching the database, so the choice is not one-way.

### 5.2 Component responsibilities

- **`app`** — the only component that serves user requests against `db`. Enforces every authorization decision; the browser is never trusted. Enqueues jobs. Never calls an external API synchronously during a user request *except* when the user explicitly presses a **Look up** button — and then with a hard 5-second timeout and a fall-through to manual entry.
- **`worker`** — idempotent, retriable jobs with exponential backoff and a dead-letter state. Job types: `media.derive` (thumbnails, EXIF handling, PDF page render), `media.ocr`, `vin.refresh`, `recalls.poll`, `reminders.evaluate`, `backup.run`, `export.build`, `search.reindex`.
- **`db`** — forward-only versioned migrations, each in a transaction, applied at `app` startup under an advisory lock so exactly one instance migrates.

### 5.3 Secure context and certificates (constraint C-1)

Three supported TLS strategies, selected by `TLS_MODE`:

| Mode | How it works | Trade-off |
| --- | --- | --- |
| `acme-dns` (**recommended**) | Let's Encrypt via DNS-01 for a real domain whose A record may point at a private IP and need not resolve publicly. | Needs a domain and a DNS credential; publicly trusted, nothing to install per device. |
| `internal` (shipped default) | Caddy's internal CA issues a cert for e.g. `shop.home.arpa`; the operator installs the root CA once per device. | Works with no prerequisites and entirely offline, but per-device trust is the step most people never complete — and it must be repeated whenever a device is replaced. |
| `custom` | Operator supplies cert and key (`mkcert`, existing homelab PKI). | Operator owns renewal. |

`internal` remains the shipped default only because it is the one mode that
needs nothing at all; the installation guide leads with `acme-dns`. Two
consequences of DNS-01 are recorded here because they are the only points where
this design touches the outside world:

- **Renewal needs outbound access** to the CA and the DNS provider roughly every
  60 days, so a permanently air-gapped instance must use `internal` or
  `custom`. Offline Mode (NFR-S-2) governs the application, not the proxy.
- **Issued names are published** to Certificate Transparency logs. The hostname
  becomes public knowledge; nothing about the machine, its address, or its
  contents does. An operator who needs the *name* private wants `internal`.

DNS providers are Go modules compiled into the proxy image, so the set is a
build argument rather than a runtime setting, and an unrecognized provider fails
at startup rather than at first renewal.

The setup wizard must **detect a non-secure context in the browser and explain in plain language** which features are disabled and how to fix it. Silently graying out the camera button is not acceptable.

### 5.4 Offline and synchronization model

The client is an **offline-capable cache with a write queue**, not a full replica. The rules are explicit because this is where local-first designs usually get vague.

**Cached for offline read** (service worker + IndexedDB, refreshed on each successful load):
- All vehicles, their spec sheets, and their open service items.
- Work orders modified in the last 90 days, plus any work order opened on that device.
- Parts and inventory quantities.
- Thumbnails of recently viewed media. Originals are **not** cached.

**Offline write queue.** Mutations are recorded locally with a client-minted **UUIDv7** primary key and replayed in order on reconnect. Because the client mints the ID, a create is naturally idempotent: a replay hits a primary-key conflict that the server treats as success.

**Conflict policy** — determined by entity class, not by guesswork:

| Class | Entities | Policy |
| --- | --- | --- |
| **Append-only** | `usage_reading`, `time_entry`, `media`, `work_order_note`, `stock_transaction`, `diagnostic_code` | **Always accepted.** Conflicts are impossible by construction. This is what makes P-5 true. |
| **Mutable** | `asset`, `work_order`, `job_item`, `part`, `purchase`, `person`, `asset_service_item` | **Optimistic concurrency.** Every mutable row carries `revision` (integer, incremented server-side). A write carrying a stale `revision` is rejected `409 Conflict` with the current server state attached. The client keeps the rejected write as a **pending conflict** and offers a side-by-side merge. Conflicts are never auto-resolved and never silently dropped. |
| **Derived** | rollups, next-due dates, search vectors | Recomputed server-side; never written by a client. |

The client shows a persistent queue indicator — *N changes waiting to sync* — tappable to inspect or discard individual queued writes. A queued write older than 14 days raises a warning.

### 5.5 Identifiers, units, money, and time

| Concern | Rule |
| --- | --- |
| **Primary keys** | `UUIDv7` (RFC 9562) everywhere: time-ordered for index locality, and mintable offline by the client. |
| **Measurements** | Stored **as entered** — `{value numeric, unit text}` — plus a **generated canonical column** (`*_canonical`) for sorting and comparison. Round-tripping 87,432 mi must never display 87,431. Canonical bases: distance `km`, volume `L`, mass `kg`, torque `N·m`, pressure `kPa`, temperature `°C`, duration `hours`. |
| **Money** | Integer **minor units** plus an ISO-4217 code, **per transaction** — never floating point, and never a single instance-wide currency. Any amount that crosses currencies also stores the `fx_rate` and `fx_rate_on` **used at the time**; historical rates are snapshotted, never re-derived. Reports render in a configurable reporting currency. |
| **Time** | `timestamptz`, stored UTC, rendered in the user's timezone (falling back to the instance timezone). **Calendar facts** (purchase date, registration expiry, service date) are stored as `date`, not as a timestamp — this avoids the classic off-by-one when the operator travels. |
| **Display units** | Derived from the active locale, with instance and per-user overrides. Conversion happens at the edge; a preference change never rewrites the database. |
| **Locale** | Per-user, negotiated from `Accept-Language` and overridable, falling back to the instance default. Drives language, number and date formatting, units, and first-day-of-week — **independently of currency** (a US operator may buy parts in EUR). |
| **VIN** | Uppercase. **17 characters for 1981+, and shorter formats accepted before that** — the standard starts with the 1981 model year, and a 1973–79 Ford truck's eleven characters (`F10GLU12345` — make, series, engine, year, plant, unit number) are complete as they are. `I`/`O`/`Q` rejected in the 17-character form, where the standard bans them; permitted in the older ones, which predate the ban. Check digit validated locally per ISO 3779 (position 9) for North American VINs — this works offline and catches most typos before any network call is made. A pre-1981 VIN has no check digit, no model-year position and no decoder, and the UI claims none of the three. |

### 5.6 Localization and internationalization

**Decision (OQ-5): built in from commit one.** Retrofitting i18n is the single most expensive refactor an application of this shape can face — it touches every template, every string, every format call, and every money column — and it is never cheaper later. The cost up front is discipline, not effort.

| Concern | Requirement |
| --- | --- |
| **Strings** | Every user-facing string goes through a message catalog from the first commit. No literal user-facing text in templates or code, ever. CI fails the build on unwrapped user-facing strings. |
| **Formatting** | Numbers, dates, times, and currency are formatted through a CLDR-backed library against the active locale. No hand-rolled date formatting in a template, no manual cents-to-string arithmetic. |
| **Currency** | Per-transaction currency (§5.5). A purchase from a foreign vendor keeps its native currency; rollups convert using the snapshotted rate, so a report of 2023 spending does not silently change when exchange rates move. |
| **Units** | Already locale-driven per §5.5 — imperial/metric is a formatting concern, not a storage concern. |
| **Seed data** | Built-in service definitions, categories, inspection templates, and DTC descriptions carry a stable **translation key**, not just an English name. User-created records are single-language and are never machine-translated. |
| **Layout** | CSS logical properties (`margin-inline-start`, not `margin-left`) throughout, so RTL is a stylesheet concern rather than a rewrite — **and a document that declares its direction**, which this row did not say and the template did not do. `<html>` carried no `dir` for four phases, and a logical property with no declared direction resolves left-to-right, so the discipline was real and bought nothing. Held by `check_rtl` now, which runs as a test: physical properties, directional keywords, four-value shorthands that set the sides apart, inline `style` attributes, and every HTML document declaring a direction. The block axis is deliberately not checked — `top` means the same thing mirrored. See R-8 and §19. |
| **Pluralization** | ICU-style plural rules via the catalog — never `"1 item(s)"`. |
| **Ship set** | v1 targets **North America** (OQ-14): `en-US`, `en-CA`, `fr-CA`, `es-MX` — with `en-US` complete and the others as translator-ready catalogs. Currencies USD, CAD, MXN. Nothing is hard-coded to North America; the ship set is a starting point, not a boundary. |
| **Why these four are not cosmetic** | They exercise genuinely different behavior rather than just swapping words: Canada is metric for distance but commonly imperial for tire pressure and torque; `fr-CA` carries legal weight in Quebec; date order differs across all three countries (`MM/DD/YYYY`, `YYYY-MM-DD`, `DD/MM/YYYY`); and `es-MX` uses different decimal grouping. A locale matrix of these four catches nearly every class of formatting bug. |

> The discipline pays off immediately in a place you would not expect: the LubeLogger locale hazard (§8.6.2). A codebase that already treats formatting as locale-dependent is one that notices `1.234,56` being parsed as `1.23`.

### 5.7 Implementation stack

**Decision (OQ-1): Python.** Not merely because it is the familiar option — it is genuinely the best-suited language for *this* spec, and the reflexive "you should have used Go" instinct is wrong here for a concrete reason.

**Why Python wins on the merits.** The hardest engineering in this document is not throughput; at ≤10 users and ≤50 vehicles, every candidate meets the §11.2 latency targets without trying. The hard parts are **PDF text extraction and layout-aware parsing** (§8.3a, [SCHEMA-PARSER-PROFILES.md](SCHEMA-PARSER-PROFILES.md)), **OCR**, image processing, and report generation. Python owns that ecosystem outright — `pdfplumber`/`PyMuPDF` give word-level coordinates, which is exactly what a label-anchored parser profile needs; `pytesseract` and `Pillow` cover the rest. The parser-profile engine is regex plus layout heuristics plus fixture-driven iteration. That is Python's home ground.

**Why not the obvious alternatives:**

| Candidate | Verdict |
| --- | --- |
| **Go** | The instinctive "lightweight self-hosted" pick, and the wrong one here. Its PDF text-extraction story is genuinely weak — no `pdfplumber` equivalent, the capable full-featured library is commercially licensed, and OCR means cgo-wrapping Tesseract. That is a direct hit on the spec's hardest feature, traded for a memory saving this project does not need. |
| **C# / .NET** | The strongest runner-up, and interesting because LubeLogger itself is .NET. Excellent performance, strong typing across a 100+ requirement domain, capable free PDF libraries. Rejected only on familiarity — a one-person project's real failure mode is abandonment, not latency. |
| **TypeScript / Node** | One language across the stack is a genuine advantage and `sharp` is excellent. Rejected on dependency churn: this spec is explicitly written for something still maintainable in five years. |
| **Rust / Elixir** | Overkill and immature-for-this respectively. Elixir's LiveView would suit this UI beautifully; its PDF and OCR ecosystem does not. |

**Recommended stack.**

| Layer | Choice | Why |
| --- | --- | --- |
| Runtime | **Python 3.12+** | |
| Framework | **Django 6.1** | ORM, migrations, auth, permissions, and a mature i18n framework (§5.6) are all things this spec needs and would otherwise be hand-built. Its admin gives free CRUD over the long tail of ~40 entities — worth months on a one-person project. *(Revised from "Django 5" at implementation: Python 3.14 is the installed runtime and 6.1 is what resolves against it. `uuid.uuid7()` is in the 3.14 standard library, so §5.5's key strategy needs no dependency at all.)* |
| API | **django-ninja** | Pydantic-typed handlers and automatic OpenAPI 3.1 (§10) — FastAPI ergonomics without leaving Django's ORM and migrations. |
| UI | **Server-rendered + HTMX + Alpine.js** | Matches P-3: no build pipeline, no duplicated SPA state. The PWA service worker and IndexedDB queue (§5.4) stay small and hand-written. |
| Jobs | **Postgres-backed queue** (`LISTEN/NOTIFY` + jobs table) | Per P-3 — no Redis, no Celery broker. |
| PDF / OCR | `pdfplumber` + `PyMuPDF`, `pytesseract`, `Pillow` | The deciding factor above. |
| Typing | `mypy --strict` on domain modules, Pydantic at the API edge | The 100+ requirement domain surface is where Python most needs the guardrail. |

> **Honest caveat — one NFR was revised, and the revision now looks pessimistic.** NFR-P-6 was raised from 700 MB to 900 MB (OQ-13) on the estimate that Python `app` and `worker` would land near 200 MB each on top of Postgres and an object store. Measured on a real instance at idle, the four containers come to **338 MB** — `app` 213, `worker` 70, `db` 38, `proxy` 17 — which is inside the figure the raise abandoned. The number is idle and on a small dataset, so it is a floor rather than a promise, and 900 MB stands until there is a measurement under load to lower it against. The `slim` profile (jobs in-process, one Python container) remains the answer on a genuinely constrained host. An operator who opts in to `STORAGE_DRIVER=s3` is running that store themselves, and this budget does not cover it.
>
> **Where this stack strains:** the offline conflict-merge UI (§5.4) is the one genuinely rich-client feature in the spec. HTMX plus a few hundred lines of vanilla JS over IndexedDB handles it, but if that proves painful in Phase 4, the escape hatch is one embedded client-side component — not a rewrite of the UI into an SPA.

---

## 6. Domain model

### 6.1 Entity relationships

```
person ──< asset_ownership >── vehicle ──< usage_reading
                                    │
                                    ├──< asset_service_item >── service_completion
                                    ├──< asset_spec
                                    ├──< recall / tsb_link
                                    ├──< vehicle_service_info_link >── service_info_provider
                                    ├──< diagnostic_session ──< diagnostic_code
                                    │         └──> parser_profile
                                    └──< work_order ──< job_item ──< part_usage >── part
                                            │              │                        │
                                            ├──< time_entry│                        ├──< part_fitment
                                            ├──< expense   └──> service_completion  ├──< part_crossref
                                            ├──< media/document                     └──< stock_lot >── location
                                            └──< work_order_note                              │
                                                                                    stock_transaction
vendor ──< purchase ──< purchase_line >── part

external_ref ──> (any entity)          polymorphic provenance for imported records
```

### 6.2 Entity catalog

Only non-obvious fields are listed. Every entity additionally carries `id uuid pk`, `created_at`, `updated_at`, `created_by`, `deleted_at` (soft delete), and mutable entities carry `revision int`.

#### Naming convention (OQ-15)

Small powered equipment — generators, mowers, pressure washers, tillers, log splitters — is **in scope**: it needs service intervals, repair history, parts, and costs, exactly like a vehicle. So the core entity is **`asset`**, not `vehicle`, carrying an `asset_kind` of `vehicle` or `equipment`.

> **One table, not two.** A mower and a truck share ~90% of their behavior: ownership, service schedules, work orders, parts consumption, documents, costs, photos, inspections, components. Splitting them would duplicate every one of those relationships and every query. Instead, vehicle-specific fields (VIN, plate, title) are nullable and gated by `asset_kind`, and the UI presents two sections over one table. This is free to decide now and expensive to change after implementation.
>
> **Requirement IDs stay `FR-VEH-*`.** They are stable identifiers referenced by tests (§7), and renaming them for tidiness would break traceability for no functional gain. Equipment-specific behavior gets its own `FR-EQP-*` block.
>
> **Prose convention:** the rest of this document says "vehicle" where vehicles are genuinely meant, and "asset" where the statement covers equipment too. Where a rule applies to both, it says so.

#### People, vehicles, and equipment

**`person`** — `display_name`, `given_name`, `family_name`, `emails[]`, `phones[]`, `address`, `notes`, `is_household bool`. A person exists independently of any user account.

**`asset`** (formerly `vehicle`) — `nickname` (what the household actually calls it — *"the red truck"*), `asset_kind` (`vehicle` | `equipment`), `vehicle_class`, `vin`, `vin_status` (`valid` | `unvalidated` | `pre_1981` | `none`), `plate`, `plate_region`, `plate_expires_on`, `year`, `make`, `model`, `trim`, `body_style`, `engine` (`displacement`, `cylinders`, `fuel_type`, `code`), `transmission`, `drivetrain`, `color_exterior`, `color_interior`, `production_date`, `plant`, `status` (`prospect` | `active` | `project` | `stored` | `sold` | `parted_out` | `totaled`), `acquired_on`, `acquired_price`, `acquired_odometer`, `disposed_on`, `disposed_price`, `title_state`, `insurance_policy`, `notes`, `primary_photo_id`, `decode_source`, `decoded_at`, `field_overrides jsonb`.

> **`vehicle_class` (OQ-8): anything that wears a license plate is in scope** — `car`, `truck`, `motorcycle`, `trailer`, `rv`, `bus`, `other_plated`. This is broader than it first appears and has real consequences: a trailer has a VIN and a plate but no engine, odometer, or emissions system; a motorcycle has a VIN, an odometer, and chain maintenance that no car template covers. So `vehicle_class` gates which spec groups, schedule templates, inspection templates, and form fields appear, and **nothing may assume an engine, an odometer, or four wheels.** vPIC decodes motorcycles and trailers, so the VIN path holds. Non-plated equipment and stand-alone engines are roadmap (§17).
>
> **Equipment fields** (`asset_kind = equipment`): `manufacturer`, `model_number`, `serial_number`, `engine_make`, `engine_model`, `engine_serial`, `fuel_type`, `rated_output` (watts, cutting width, PSI — free-form value + unit), `purchased_from`. VIN, plate, title, and registration are **null and hidden** for equipment; `vehicle_class` is likewise null. Nothing about equipment goes through vPIC, plate lookup, or recalls.
>
> **`prospect` status** supports pre-purchase inspection (FR-DVI-10): a car you are looking at, not one you own. Prospects are excluded from every fleet count, cost rollup, and due dashboard until promoted, and a prospect you walk away from is kept as a record of *why*.
>
> **`field_overrides` matters.** When a VIN decode says the trim is `SE` and the operator knows it is an `SE-R`, the override is recorded per field so a later re-decode never clobbers a human correction. Any UI showing a decoded field shows its provenance.

**`asset_ownership`** (formerly `asset_ownership`) — `asset_id`, `person_id`, `role` (`owner` | `co_owner` | `primary_driver`), `from_date`, `to_date`. Ownership is history, not a foreign key on the vehicle: cars change hands, and the service record must remain correct about who owned it when.

**`usage_reading`** (formerly `usage_reading`) — `asset_id`, `meter` (`odometer` | `engine_hours` | `cycles`), `value`, `unit`, `value_canonical`, `read_on date`, `source` (`manual` | `work_order` | `obd` | `document` | `import`), `note`. **Append-only.** Readings are validated as non-decreasing against the asset's history; a decrease is *allowed but flagged* (cluster swaps, meter replacements, and rollbacks are all real) and requires a note.

> Generalizing the meter is what lets a generator use the maintenance machinery unchanged — FR-MAINT-3 already supports hour-based intervals, and an hour meter is just a different meter. An asset may have **no meter at all** (a trailer, a log splitter with no hour meter), in which case its service intervals are purely time-based and the UI never asks for a reading.

#### Work

**`work_order`** — the central record. `asset_id`, `number` (human-friendly sequential, e.g. `WO-2026-0043`), `title`, `type` (`maintenance` | `repair` | `diagnosis` | `modification` | `inspection` | `project`), `status` (see Appendix A), `priority`, `complaint` (what's wrong, in the reporter's words), `cause`, `correction`, `opened_at`, `started_at`, `completed_at`, `odometer_in`, `odometer_out`, `requested_by_person_id`, `parent_work_order_id`, `tags[]`, `is_safety_critical bool`, `budget_minor`/`budget_currency` (nullable — `NULL` is no budget and 0 is a budget of nothing; §7.6b).

> **Why `complaint` / `cause` / `correction`:** the classic three-C's discipline. Six months later, "brakes noisy" is useless without "inner pad worn to backing, caliper slide pins seized" and "replaced pads, rotors, rebuilt caliper." Free-form notes do not reliably capture this; three labeled fields do.
>
> **Why `parent_work_order_id`:** an engine swap is not one afternoon. A project work order collects child work orders (teardown, machine work, reassembly) while remaining a single cost and time rollup.

**`job_item`** — a line of work within a work order. `work_order_id`, `sequence`, `title`, `description`, `status`, `service_item_id` (nullable link to the maintenance item this satisfies), `assigned_to_user_id`. One garage session is usually "oil change + brake pads + chase that rattle"; each is a `job_item` so that the oil change can close out its maintenance interval independently.

**`work_order_note`** — `work_order_id`, `body`, `author_user_id`, `noted_at`. **Append-only**, timestamped, never edited — the running log of the job.

**`time_entry`** — `work_order_id`, `job_item_id`, `user_id`, `started_at`, `ended_at`, `duration_minutes`, `note`, `category` (`diagnosis` | `wrenching` | `parts_run` | `cleanup` | `research`). **Append-only.** Supports both a running timer and after-the-fact entry. `hourly_rate_cents` is an optional instance setting used only for opportunity-cost reporting; it never produces an invoice (NG-1).

#### Parts, inventory, purchasing

**`part`** — canonical definition. `name`, `category`, `manufacturer`, `part_number`, `part_type` (`oem` | `oe_supplier` | `aftermarket` | `remanufactured` | `used`), `unit` (`each` | `L` | `kg` | `ft`), `is_consumable`, `has_core bool`, `core_value_cents`, `hazmat_class`, `notes`, `spec jsonb` (viscosity, thread pitch, dimensions — schema-free by design).

**`part_crossref`** — `part_id`, `system` (`oem` | `interchange` | `vendor_sku` | `upc`), `value`. The same water pump has five numbers; all five must find it.

**`part_fitment`** — `part_id`, `asset_id` **or** (`make`, `model`, `year_from`, `year_to`, `engine_code`), `position` (`front_left` | `rear` | ...), `notes`, `confidence` (`confirmed_installed` | `stated_by_vendor` | `unverified`). `confirmed_installed` is set automatically the first time the part is consumed on that vehicle — **the shop's own history becomes its fitment database**, which is the only fitment data that is ever fully trustworthy.

**`location`** — `name` (`Shelf B3`, `Red cabinet, drawer 2`), `parent_location_id`, `qr_code`. Printable QR labels; scanning one opens its contents.

**`stock_lot`** — `part_id`, `location_id`, `qty_on_hand`, `unit_cost_cents`, `purchase_line_id`, `acquired_on`, `expires_on` (brake fluid, RTV, and epoxy do expire). Lot-level costing so consumption values correctly when the same part was bought twice at different prices (FIFO by `acquired_on`).

**`stock_transaction`** — `stock_lot_id`, `delta`, `reason` (`receive` | `consume` | `adjust` | `return` | `scrap` | `found`), `work_order_id`, `note`. **Append-only ledger**; `qty_on_hand` is a derived projection, and any discrepancy is auditable.

**`part_usage`** — `job_item_id`, `part_id`, `qty`, `unit_cost_cents`, `source` (`from_stock` | `purchased_for_job` | `reused` | `warranty`), `stock_lot_id`, `core_returned bool`, `core_returned_on`, `warranty_months`, `warranty_miles`, `installed_at`. Warranty fields let the system answer *"is this alternator still under its 24-month warranty?"* at the moment it fails again.

**`vendor`** — `name`, `type` (`online` | `local_store` | `dealer` | `salvage` | `machine_shop` | `individual`), `url`, `account_number`, `phone`, `notes`, `return_window_days`.

**`purchase`** — `vendor_id`, `ordered_on`, `received_on`, `order_number`, `status` (`cart` | `ordered` | `partial` | `received` | `returned` | `canceled`), `subtotal_cents`, `tax_cents`, `shipping_cents`, `discount_cents`, `total_cents`, `payment_method`, `work_order_id` (optional — bought for a specific job), `return_by` (derived from vendor's return window).

**`purchase_line`** — `purchase_id`, `part_id`, `description_as_ordered`, `qty_ordered`, `qty_received`, `unit_price_cents`, `core_charge_cents`, `line_total_cents`. Receiving a line creates a `stock_lot` and a `receive` transaction.

**`expense`** — non-part costs. `asset_id`, `work_order_id`, `category` (`shop_supplies` | `outsourced_labor` | `machine_work` | `towing` | `disposal` | `tooling` | `registration` | `inspection` | `insurance` | `other`), `amount_cents`, `incurred_on`, `vendor_id`, `description`. Tooling is deliberately trackable, always exported, but **excluded from per-vehicle cost unless `COST_INCLUDE_TOOLING` is set** — a torque wrench is not a cost of the Civic (OQ-4).

#### Maintenance

**`service_definition`** — a reusable item: `name` ("Engine oil and filter"), `category`, `default_interval_km`, `default_interval_months`, `default_interval_hours`, `severity` (`routine` | `safety` | `emissions`), `instructions`, `default_parts[]`.

**`schedule_template`** — a named set of `service_definition`s with intervals: `name` ("Generic gasoline — severe service"), `source` (`builtin` | `user` | `imported`), `applies_to` (make/model/year hints), `notes`.

**`asset_service_item`** — the live instance on a vehicle. `asset_id`, `service_definition_id`, `interval_km`, `interval_months`, `interval_hours`, `last_done_on`, `last_done_odometer`, `next_due_on`, `next_due_odometer`, `status` (`ok` | `due_soon` | `overdue` | `snoozed` | `disabled`), `snooze_until`, `notes`. Per-vehicle intervals are editable — the operator's severe-service judgment beats the template.

**`service_completion`** — `asset_service_item_id`, `job_item_id`, `completed_on`, `odometer`. Closing a job item that is linked to a service item writes this row and rolls the interval forward. **This is the join that makes the maintenance module self-maintaining:** doing the work in a work order *is* the act of resetting the schedule; there is no second place to remember to update.

**Due calculation.** `due_soon` and `overdue` are computed from whichever of time, distance, or engine hours arrives first. Distance projection uses the vehicle's observed average daily distance over its trailing six months of odometer readings (falling back to an instance default when there is insufficient history), so *"due in about 3 weeks"* is shown alongside *"in 900 mi."*

#### Documents, media, specs

**`media`** — `kind` (`photo` | `document` | `scan_export` | `audio_note`), `mime`, `bytes`, `sha256`, `storage_key`, `original_filename`, `captured_at` (from EXIF where present), `width`, `height`, `page_count`, `ocr_text`, `ocr_status`, `thumb_key`, `preview_key`. **Append-only.** `sha256` deduplicates the same receipt photographed twice.

**`media_link`** — polymorphic attachment: `media_id`, `entity_type`, `entity_id`, `role` (`primary_photo` | `before` | `after` | `receipt` | `manual` | `title` | `registration` | `insurance` | `inspection` | `diagram` | `other`), `caption`, `sort_order`. One document, many attachment points — a purchase receipt is legitimately attached to both the purchase and the work order.

**`asset_spec`** — reference values: `asset_id`, `group` (`fluids` | `torque` | `tires` | `electrical` | `alignment` | `capacities` | `filters`), `name`, `value`, `unit`, `condition` (e.g. *"cold, curb weight"*), `source` (`manual` | `oem_doc` | `measured` | `decoded`), `source_media_id`, `notes`. Free-form key/value on purpose: no fixed schema can cover a 1968 Mustang and a 2024 EV.

> Specs are the quiet high-value feature. *"What's the rear diff fill capacity?"* and *"what's the axle nut torque?"* are looked up far more often than any report is run, and the answer is usually in a manual PDF that takes three minutes to find.

#### Inspections (DVI) and installed components

**`inspection_template`** — `name` ("Pre-purchase inspection", "Annual safety"), `translation_key`, `description`, `vehicle_classes[]` (which classes it applies to), `source` (`builtin` | `user` | `imported`), `version int`, `is_active`.

**`inspection_point`** — one thing you look at. `template_id`, `area` (`road_test` | `under_hood` | `under_vehicle` | `brakes` | `tires_wheels` | `suspension_steering` | `lighting_electrical` | `body_glass` | `interior` | `fluids` | `exhaust_emissions`), `sequence`, `name`, `translation_key`, `guidance` (what "good" looks like), `result_type` (`status` | `measurement` | `both`), `measurement_unit`, `positions[]` (e.g. `LF, RF, LR, RR`, or tire tread's `outer, center, inner`), `thresholds jsonb`, `photo_required` (`never` | `on_attention` | `always`), `is_safety_critical`, `is_optional`.

**`inspection`** — `asset_id`, `template_id`, `template_version` **and a full snapshot of the template's points**, `work_order_id` (nullable), `performed_by_user_id`, `performed_on`, `odometer`, `status` (`draft` | `complete` | `abandoned`), `summary jsonb` (counts by status), `notes`, `overall` (`pass` | `attention` | `fail`).

> **The template is snapshotted onto the inspection.** Editing a template must never silently rewrite the meaning of a two-year-old inspection. Same principle already applied to parser profiles and audit history.

**`inspection_result`** — `inspection_id`, `inspection_point_id`, `position`, `status` (`pass` | `attention` | `fail` | `not_applicable` | `not_inspected`), `measured_value`, `unit`, `value_canonical`, `auto_status` (what the thresholds computed), `status_overridden bool`, `note`, `recommended_action`, `converted_to_job_item_id`, `converted_to_service_item_id`.

> `auto_status` alongside `status` is deliberate: thresholds propose, **the human disposes**, and the disagreement is recorded. A 4/32" tread reading auto-flags `attention`; the operator who knows the car is being sold next month may downgrade it, and six months later the record shows both what the rule said and what they decided.

**`asset_component`** — an installed part instance with a life of its own: `asset_id`, `part_id` (nullable — a component may predate any part record), `component_type` (`tire` | `battery` | `brake_pad` | `brake_rotor` | `belt` | `filter` | `wiper` | `bulb` | `other`), `position`, `installed_on`, `installed_odometer`, `installed_by_job_item_id`, `removed_on`, `removed_odometer`, `removal_reason` (`worn` | `failed` | `upgraded` | `rotated` | `seasonal`), `serial_or_dot_code`, `warranty_months`, `warranty_distance`, `expected_life_distance`, `notes`.

> This is what turns DVI measurements into something useful. A tread depth reading is a number; a tread depth reading **against a component installed 31,000 miles ago** is a wear rate, and a wear rate is a due date (§7.8, FR-DVI-11). It also answers the questions a home shop actually asks — *"how old is this battery?"*, *"how many miles are on these tires and have they ever been rotated?"*, *"is this alternator still under warranty?"*

**`fluid_sample`** — one lab report on one compartment: `asset_id`, `compartment` (`engine_oil` | `transmission` | `differential` | `transfer_case` | `hydraulic` | `coolant` | `brake_fluid` | `fuel` | `gear_oil` | `other`), `position`, `sampled_on`, `usage_at_sample`, **`fluid_usage`** (how far the fluid itself had run), `fluid_changed bool`, `lab`, `report_number`, `fluid_brand`, `fluid_grade`, `work_order_id`, `lab_comment`, `notes`.

**`fluid_result`** — one line off that report: `fluid_sample_id`, `analyte` (a registry slug, or whatever the lab called it), `value`, `unit`, `reference` (the lab's own average, where the report prints one), `flagged bool`. Unique per analyte per sample.

> **Why `fluid_usage` is the field that matters.** The same reasoning as `asset_component` above, applied to a fluid: 24 ppm of iron is a number, and 24 ppm **over 3,000 miles of oil** is a rate. A sample without it can be read and cannot be compared, so it is recorded, shown, and named in a stated shortfall rather than averaged into a trend (§7.9a, FR-FLU-5).
>
> **Why results are rows.** Engine oil, gear oil, coolant and brake fluid have different panels, and a lab may revise its own. A column per element is forty nullable columns and a migration every time somebody's lab adds one; a row per result costs a join and accepts anything.
>
> **Where a sample is reachable from.** Its own vehicle's button row, that vehicle's timeline, and — where one was named — the work order it was taken for. The last of those is not decoration: the form offers a work order, so a work order that could not display one would be recording a link nothing can follow.

#### Diagnostics, recalls, bulletins

**`diagnostic_session`** — `asset_id`, `work_order_id`, `performed_on`, `tool` (free text), `tool_model`, `odometer`, `source` (`pdf_report` | `file_import` | `elm327` | `manual` | `photo`), `raw_media_id`, `extracted_text`, `extracted_words jsonb`, `parser_profile_id`, `parser_version`, `parse_status` (`pending` | `parsed` | `unmatched` | `failed`), `extraction jsonb` (per-field value + confidence + source page/offset), `test_results jsonb`, `review_status` (`draft` | `confirmed`), `readiness_monitors jsonb`, `notes`.

> **`extraction` and `test_results` are two different things and the difference is the point.** `extraction` is what the machine read and is never edited — it is the only way to answer *what did the tool actually say?* a year later, and that question stops being answerable the moment a correction lands on top of it. `test_results` is the operator-correctable copy, and every value in it records whether a person put it there.
>
> `test_results` exists because a **bench tester's answer is not a list of codes**. A battery tester prints a verdict, a clock and a handful of readings, once per test — and one photograph of a strip of thermal paper can hold two of them with two timestamps and two different things called `VOLTAGE`. A flat `{field: value}` dictionary has room for one of each. Each entry holds `kind`, `verdict`, `performed_on`, `attributes` and `readings`, and every value carries `raw` (the characters it was read from), `confidence`, `source` (which receipt, and the box on the photograph), `corrected` and `warnings`.

> A session is **only visible in vehicle history once `review_status = confirmed`** (§8.3a). Drafts live in an import queue. `extracted_text` is retained so a later profile improvement can re-parse without the original upload — see `parser_profile` below.

**`parser_profile`** — declarative extraction rules for one tool's report format. `name` ("XTOOL D8 — DTC report"), `tool_vendor`, `tool_model`, `version int`, `media_type` (`pdf` | `csv` | `json` | `text`), `fingerprint jsonb` (PDF `Producer`/`Creator` metadata match plus first-page text regexes, with a match score threshold), `field_extractors jsonb` (named regex/label-anchored extractors with capture groups, type coercion, and per-field confidence weighting), `table_extractor jsonb` (DTC table locator, column roles, row filters), `source` (`builtin` | `user` | `imported`), `is_active`. Exportable and importable as YAML; see Appendix D. **Adding support for a new scan tool is authoring a profile, never a code change.**

**`diagnostic_code`** — `diagnostic_session_id`, `code` (`P0301`), `description`, `system` (`P`|`B`|`C`|`U`), `is_generic bool`, `state` (`stored` | `pending` | `permanent` | `history`), `freeze_frame jsonb`, `status` (`open` | `addressed` | `recurring` | `ignored`), `resolved_by_job_item_id`. **Append-only** for the reading; `status` is the one mutable field.

**`recall`** — `asset_id`, `campaign_number`, `nhtsa_id`, `reported_on`, `component`, `summary`, `consequence`, `remedy`, `source` (`nhtsa` | `manual`), `owner_status` (`open` | `scheduled` | `completed` | `not_applicable`), `completed_on`, `notes`. `owner_status` is **always operator-maintained** — see §8.4 for why it cannot be automatic.

**`bulletin`** (TSB) — `asset_id` or model scope, `number`, `issued_on`, `title`, `component`, `summary`, `source` (`manual` | `imported`), `media_id`, `relevance` (`applies` | `maybe` | `not_applicable`).

#### Integrations and provenance

**`external_ref`** — provenance for anything imported from another system. `source_system` (`lubelogger` | `csv_import` | …), `source_instance_url`, `external_type`, `external_id`, `entity_type`, `entity_id`, `first_imported_at`, `last_seen_at`, `source_hash`, `state` (`linked` | `orphaned` | `conflicted`). Unique on (`source_system`, `source_instance_url`, `external_type`, `external_id`). This one small table is what makes imports idempotent, drift detectable, and unlinking possible (§8.6.4).

**`service_info_provider`** — `name`, `base_urls[]` (ordered mirror list with failover), `url_template` (token substitution: `{make}`, `{year}`, `{model}`), `deep_link_depth` (how far the template is trustworthy — `make_year` for CHARM/LEMON), `access` (`free` | `paid` | `account_required`), `notes`, `is_enabled`, `sort_order`.

**`vehicle_service_info_link`** — `asset_id`, `service_info_provider_id`, `url` (the **resolved, pinned** URL), `label`, `pinned_by_user_id`, `pinned_at`, `last_verified_at`. See §8.5 for why pinning rather than generating is the correct design.

#### System

**`user`** — `person_id`, `username`, `email`, `password_hash` (Argon2id), `role` (`admin` | `member`), `totp_secret`, `is_active`, `last_login_at`, `preferences jsonb`.

**`api_token`** — `user_id`, `name`, `token_hash`, `scopes[]`, `last_used_at`, `expires_at`. For scripts and the operator's own automations.

**`audit_log`** — `entity_type`, `entity_id`, `action`, `user_id`, `at`, `diff jsonb`, `source` (`web` | `api` | `sync` | `system`). Not a compliance artifact — a *"who changed the odometer to 300,000 and why"* artifact.

**`job`** — `type`, `payload jsonb`, `run_after`, `attempts`, `state`, `last_error`, `locked_by`, `locked_at`.

**`setting`** — instance configuration surfaced in the UI (units, currency, timezone, integration toggles, retention).

---

## 7. Functional requirements

Requirements are `MUST` unless marked *(SHOULD)* or *(MAY)*. IDs are stable and referenced by tests.

### 7.1 Vehicles — `FR-VEH`

| ID | Requirement |
| --- | --- |
| FR-VEH-1 | Create a vehicle from a VIN, from a plate, or entirely by hand. **No field except a nickname is required** — a half-known project car must still be recordable. |
| FR-VEH-2 | Validate a VIN locally (length, character set, ISO 3779 check digit) before any network call, showing errors inline. **Length is a rule about the year, not about VINs**: seventeen characters is required from the 1981 model year, and a shorter VIN is accepted and recorded as `pre_1981` rather than refused — a 1973–79 Ford truck's eleven characters are complete, and rejecting them rejected the exact population a home garage keeps. Where the model year is known and 1981 or later a short VIN is still an error, because there it is a typo; where the year is unknown the panel says which reading it took and that the year is what settles it. The reading fills in the vehicle on request, under §8.1a's rules. Nothing is claimed about a pre-1981 VIN that cannot be: no check digit, no model-year inference, no vPIC decode, and no VIN recall search — each is withheld rather than shown failing. |
| FR-VEH-3 | Decode a VIN via the configured provider on explicit user action, populate empty fields, and mark each populated field with its provenance and decode timestamp. |
| FR-VEH-4 | Never overwrite a user-edited field on re-decode; record the divergence in `field_overrides` and offer a review UI showing decoded-vs-yours. |
| FR-VEH-5 | Scan a VIN barcode (Code 39 on the door jamb) or QR with the device camera, decoding on-device. |
| FR-VEH-6 | Record vehicle status transitions (`active` → `sold`, etc.) with date and price; **sold and parted-out vehicles are retained in full, never deleted**, and are hidden from default lists. |
| FR-VEH-7 | Maintain ownership history with multiple people and roles over time. |
| FR-VEH-8 | Record odometer readings from any screen, with the most recent reading shown everywhere the vehicle appears. |
| FR-VEH-9 | Flag a decreasing odometer reading and require a note, without blocking it. |
| FR-VEH-10 | Show a single vehicle timeline merging work orders, purchases, odometer readings, documents, diagnostics, and recalls in date order. The vehicle's own page carries a **summary** of it — the most recent handful, with each day's photographs folded into one expandable entry — because a photograph is one row and there are far more photographs than anything else, and the meter, the identity panel and the open work were sitting below a scroll of them. The full story, ungrouped, has its own page. |
| FR-VEH-11 | *(SHOULD)* Warn when `plate_expires_on` or an inspection due date is within 30 days. |
| FR-VEH-12 | **Read a pre-1981 VIN locally, against the manufacturer's own tables.** vPIC decodes the 17-character format and nothing else (§8.1), so before 1981 the number stamped on the door was one nothing here could interpret — and those are exactly the vehicles a home garage keeps. The tables are published: an eleven-character `F26SVAE1234` is an F-250 4WD with a 400 CID V8 built at Kentucky Truck in 1978, and every part of that is a lookup. Transcribed schemes live in data, not code (§8.1a), each naming the sheet it came from. Three rules govern the reading. **Nothing is guessed** — a code absent from its table is reported as unknown, and a scheme matching fewer than half the coded positions is a coincidence of length rather than a reading. **Ambiguity is reported, not resolved** — several schemes share a shape, Ford's own serial blocks overlap so that one number is both a 1976 and a 1978, and where two readings fit both are shown with the model year named as what separates them. **The parts check each other** — a code offered only from 1977 against a serial block that is only 1973 is a contradiction, and a contradiction means the wrong table rather than a surprising vehicle. Where a make stamped no engine code at all but the sheets name the engine its trucks carried that year — GM's, throughout — the reading says so, marked as coming from the year rather than from the plate, since a code on the door and a fact about every truck built that year are both true and are not the same claim. |
| FR-VEH-13 | **Documents and Links on the vehicle.** Documents are attachments that are not photographs, listed separately because the file decides which it is (FR-DOC-10) — every attachment previously landed in the Photos grid, so a PDF of the title showed as a blank thumbnail nobody could read. Which section a file goes to is read off the file, not off the form it arrived by. Links are any web page about *this* vehicle: the forum thread that solved the fault, the diagram that took an hour to find, the listing it was bought from — knowledge that otherwise lives in one person's bookmarks. Distinct from the pinned service-manual links (§8.5), which are one per configured provider and answer a different question. Only `http`/`https` are accepted, because a stored URL is rendered into an `href`. **Nothing is ever fetched** — a link is stored and displayed — so no allowlist and no offline-mode question arises. |

### 7.1a Equipment — `FR-EQP`

Small powered equipment is in scope (OQ-15). It reuses the asset, work order, parts, cost, schedule, document, and inspection machinery wholesale; these requirements cover only what is genuinely different.

| ID | Requirement |
| --- | --- |
| FR-EQP-1 | Create an `asset` with `asset_kind = equipment`, capturing manufacturer, model and serial numbers, engine details, and purchase information — **with no VIN, plate, title, or registration fields shown**. |
| FR-EQP-2 | Track an hour meter, a cycle counter, or **no meter at all**, with service intervals falling back to time-only where no meter exists. |
| FR-EQP-3 | Never route equipment through VIN decode, plate lookup, or recall checking; those integrations are hidden rather than shown-and-failing. |
| FR-EQP-4 | Ship equipment schedule templates (small engine, generator, mower) and equipment inspection templates, scoped so vehicle templates never appear for a mower. |
| FR-EQP-5 | Include equipment in cost rollups, the due dashboard, search, and the vehicle timeline equivalents, listed separately from vehicles rather than intermixed. |

> **Boundary with WrenchLedger (§8.7), stated because it is genuinely ambiguous.** WrenchLedger already tracks meters, service logs, warranties, and lifecycle states for the things in a workshop — so a generator could plausibly live in either system. The dividing line is **what you do with it**, not what it is:
>
> - **HomeAutoShop** — you *perform repairs on it*: work orders, parts, diagnostics, a repair history. A generator whose carburetor you rebuild belongs here.
> - **WrenchLedger** — you *own and use it*: where it lives, what it is worth, who borrowed it, insurance documentation. A generator you only need to know you own belongs there.
>
> An asset that genuinely belongs in both can be linked across them (§8.7). What must **never** happen is HomeAutoShop growing storage locations, loan tracking, or valuation — that is NG-8, and it is where duplication would start.

### 7.2 People — `FR-OWN`

| ID | Requirement |
| --- | --- |
| FR-OWN-1 | Create people independently of user accounts; a person may own vehicles without ever signing in. |
| FR-OWN-2 | Link a `user` to a `person` so authored records attribute to a human, not a login. |
| FR-OWN-3 | Show, for any person, their vehicles (current and former) and total spend across them. |
| FR-OWN-4 | Merge duplicate people, repointing all references, with the merge recorded in the audit log. |

### 7.3 Work orders — `FR-WO`

| ID | Requirement |
| --- | --- |
| FR-WO-1 | Create a work order in two taps from a vehicle, with `opened_at` and the last known odometer prefilled. |
| FR-WO-2 | Support the lifecycle in [REFERENCE.md §1](REFERENCE.md), including `waiting_on_parts` with a link to the blocking purchase. |
| FR-WO-3 | Hold many `job_item`s, each independently completable. |
| FR-WO-4 | Capture complaint / cause / correction as distinct fields. |
| FR-WO-5 | Accept notes, photos, and time entries offline and sync them without conflict (§5.4). |
| FR-WO-6 | Attach parts from inventory (decrementing stock) or as purchased-for-job, in one flow. |
| FR-WO-7 | Link a job item to a `asset_service_item` so completing it rolls the maintenance interval forward automatically. |
| FR-WO-8 | Support parent/child work orders for multi-session projects, with costs and time rolling up to the parent. **The roll-up was stated here from the first draft and did not exist**: `parent_work_order_id` was written, read by the picker, and by nothing else — a project's page showed neither its children nor their spend, so the one record the field was added for was the one the screen could not display. Found by building R-6 on top of it, which is the expensive way: a burn-down over a project's own line items would have reported an engine swap as barely started. `WorkOrder.tree_ids()` walks it now, `project_cost()` sums it, and the project's page lists what is under it. |
| FR-WO-9 | Prompt for `odometer_out` at completion. |
| FR-WO-10 | Show a live cost rollup (parts + expenses + optional labor value) as the job progresses. |
| FR-WO-11 | Duplicate a work order as a template ("annual service") including job items and expected parts, without copying notes, photos, or costs. **Not built** — no view, no service, no template flag, and the three FR-WO-11 citations in the code sit on the *planned parts* feature instead, so a grep reads as though it exists. §18's C-6 declined to build a checklist system partly because this one was already there. §15.2. |
| FR-WO-12 | **Job items are editable, re-orderable and removable.** They were write-once: a typo stayed a typo, and the checkbox is a toggle, so `doing` and `skipped` existed in the model and were unreachable from any screen — **Skipped** most of all, which is what distinguishes work considered and declined from work still waiting, and only one of those belongs on next week's list. Order is set with **up and down buttons, not dragging**: dragging cannot exist without a script, needs a second mechanism built beside it to be reachable from a keyboard, and is unpleasant on the phone the list is read on. Moving renumbers the whole list rather than swapping two values, so items that share a sequence still reorder. An item **parts were used on refuses removal** — a soft delete does not take the usages with it, so the item would vanish from the screen while its cost stayed in the job total; skipping it is the answer. Its tool references go with it; a part requirement is a claim about the job and moves up to the work order. |
| FR-WO-13 | *(SHOULD)* Offer a running timer, resilient to the tab closing (start time is server-recorded). |

### 7.4 Parts and inventory — `FR-PART`, `FR-INV`

| ID | Requirement |
| --- | --- |
| FR-PART-1 | Search parts by name, manufacturer, any cross-reference number, or UPC — one search box, all identifiers. |
| FR-PART-2 | Record unlimited cross-references per part. |
| FR-PART-3 | Record fitment against a specific vehicle or a make/model/year range, and **auto-record `confirmed_installed` fitment when a part is consumed on a vehicle**. |
| FR-PART-4 | Answer "what fits this vehicle?" from the fitment table, ranked with confirmed-installed first. A part with a `does_not_fit` fitment for that vehicle is **excluded, not demoted** — being offered a part you have already held against the car and rejected is how it gets ordered twice. |
| FR-INV-9 | **A kit holds the stock while its box is closed.** A kit is a part with other parts recorded inside it, so it stays buyable, stockable and searchable by every mechanism that already exists. Each component's page shows what is sitting inside a kit on the shelf — findable, explicitly **not counted as stock**, because a boxed drier is not on a shelf — which is what stops it being ordered a second time. **Opening** a kit is one ledger event with two signs: the box leaves stock, the contents arrive as their own lots at their share of the kit's landed cost, divided in proportion to what the parts are worth and exact to the cent. **Prices, never weights** — every part may carry a usual price, a kit row may override it for that box, and the proportions fall out of the money rather than being a percentage somebody works out with a calculator; the screen shows each row's price and the share it comes to. Quantity counts: six one-dollar O-rings weigh six dollars. Where any component's price is unknown the split is **even and says so**, rather than quietly landing that part at a cost of nothing. Each released lot remembers the kit lot it came from, so an opening is reversible **while none of the contents have moved** — a drier fitted to a car cannot go back in a box, and the refusal says so. Where an imported order lists a kit's components (§8.3a), the contents and their weights are taken from the document rather than guessed at: the vendor prints a `Price EA` against each component even though the line is not charged, and those prices are the split stated by the only party who knows it. The weights are shown on screen as the percentage each works out to — a bare `1` against a `70` is not a split anybody can read. |
| FR-PART-6 | Fitments are editable and removable, and one of the things a fitment can say is **`does_not_fit` — tried it**. A vendor's claim is re-recorded every time its order is imported, so deleting a disproved one is undone by the next import; recording the failure is the shop's own knowledge and outranks the claim. An import never overwrites or resurrects a fitment that has been edited or removed. |
| FR-PART-5 | Show, for any part, complete purchase history with vendor and price trend. |
| FR-PART-7 | **A part chooser is a search with a shortlist, never the catalog.** Every chooser rendered `Part.objects.all()[:500]` into a `<select>` — a control that gets worse the more the application is used, since a part is never removed when it is used up (and should not be: what a fuel pump cost in 2015 is the point), and past five hundred it stopped listing parts at all without saying so. Nothing is hidden by the replacement: typing searches every part by every identifier (FR-PART-1). What changes is the resting state, which is a shortlist assembled from relevance — **the parts that fit this vehicle, the parts on the shelf, and the consumables** — capped short. Fitment outranks stock deliberately: **planning is the act of finding the gap between what is on hand and what has to be bought**, so a part that fits and is not in stock is the most useful row on the screen, and every row prints what is on the shelf including *none on hand* so the gap is visible where the choice is made. A `does_not_fit` part is excluded from the shortlist for the reason FR-PART-4 gives. A search that matches nothing offers the new-part form with the typed name already in it, which is the planning case for a part nobody has bought yet. With no script the chooser is a text box whose name the server resolves — an unambiguous name is a part, an ambiguous one says so rather than guessing, and on an order line no match is *not cataloged* rather than an error. The chosen row carries the part's own step and convertible units to the quantity box beside it (FR-INV-13), which is what a `<select>` of every part had been carrying on each `<option>`. |
| FR-INV-1 | Track quantity on hand per part per location via the append-only transaction ledger. |
| FR-INV-2 | Support hierarchical locations with printable QR labels; scanning a label opens that location's contents. |
| FR-INV-3 | Scan a part's UPC to find or create it. |
| FR-INV-4 | Support a minimum quantity per part and surface a restock list. |
| FR-INV-5 | Value inventory using FIFO by lot; consumption uses the oldest lot's actual cost. |
| FR-INV-6 | Flag expiring consumables (brake fluid, sealants) ahead of `expires_on`. |
| FR-INV-7 | Support a guided cycle count that writes `adjust` transactions with a reason, never silently overwriting quantities. |
| FR-INV-8 | The parts list shows a kit's contents **beneath the kit**, not beside it. Flat, a kit and the parts in it are peers and the components read as zero on hand, which is the reading that gets one ordered while it is already in the box. While searching they stay at the top level and name their kit instead — a search for "condenser" that files the condenser under a kit named nothing like it has answered the wrong question. |
| FR-INV-11 | **A stock lot is correctable.** Its location, unit cost and dates are editable after the fact — stock could be added but never fixed, and a lot recorded without a cost is not a cosmetic gap: everything drawn from it costs nothing, so the job is cheaper than it was and the shelf is worth less than it is. **Quantity is not one of the editable fields**, because it is a projection of the ledger (FR-INV-1) and a box that overwrote it would be exactly the silent correction the ledger exists to prevent — counting it is the route, and the count records a reason. A lot may be **removed only while nothing has been drawn from it**; once something has, the draw is what a job cost. A lot received against a purchase, or opened out of a kit, refuses removal by name and points at un-receiving or closing the kit instead. |
| FR-INV-14 | **The parts screen pages, and says how many there are.** It stopped at two hundred rows while browsing and at twenty-five while searching, both silently — so a shop with four hundred parts looked exactly like a shop with two hundred, and the only way to find out otherwise was to go looking for something that should have been there. A cap with no page numbers under it is not a limit, it is a claim about the catalog that happens to be false. The count is of parts rather than of visible rows, because kit contents fold into their kit (FR-INV-8) and "how many do I have" means parts. The chooser keeps its own tighter limit (FR-PART-7): twenty-five is already more than anybody scrolls in a picker, and the two screens want different things.
| FR-INV-12 | The parts list answers **price, fitment, last purchase and location** on the row, each labeled. Every one of these previously required opening the part, and a row of unlabeled figures is four facts identified by shape. Unknown facts are omitted rather than printed as dashes. The row's cost is a **fixed number of queries whatever the list's length** — the figures are prefetched, not read off properties that each issue their own query. |
| FR-ADM-8 | **The small records are correctable and removable**, not create-only: vendors, storage locations, cross-references, expenses, time entries and order lines. Each was reachable from a screen once and never again, so a name typed in a hurry or an amount off by a decimal place was permanent short of the Django admin. Every removal carries a refusal rule of the same shape — **a record that explains money already spent or stock already moved does not disappear from underneath it**: a vendor a purchase names, a location still holding stock, an order line already received, a part still on the shelf, a vehicle with any history (which is marked *sold*, keeping everything it cost), a person a vehicle still names. Time entries are append-only and stay uneditable — but append-only never meant unremovable, and a timer left running overnight is eleven hours nobody worked. |
| FR-ADM-9 | **Grant a helper access to named vehicles** from the user page: one row per vehicle, read or write, added and removed in place (§12.2a). Grants stay visible when the role is not `helper` — a role changed away leaves its rows behind, and a permission nobody can see is a permission nobody audits — with the screen saying plainly that they are dormant. A helper cannot reach the page that grants them, and the request gate refuses any screen not opened to helpers before the view runs. |
| FR-INV-13 | **A part is measured in whatever it is sold in, and a quantity may be entered in any unit of the same kind.** Four units were hard-coded — each, litres, kilograms, feet — which is a guess about somebody else's catalog: R-134a is sold in cylinders **by the pound** and dispensed **by the ounce or the half-kilogram**, and none of those were sayable. Mass, volume, length and count are all available, built from the conversion table rather than listed beside it so a unit that exists is a unit the arithmetic knows. Stock is always **held in the part's own unit** — one number per part, so the shelf total never depends on which box somebody typed into — and the conversion is quantised to the ledger's three decimal places at the edge, so what lands is what was shown. A counted part offers no unit picker: there is no factor between a gasket and a litre. |
| FR-SPEC-5 | **A spec may be a range.** Plenty are — a refrigerant charge is 0.50–0.55 kg, a cold tire pressure is 32–35 psi, a valve lash is a window — and typed into one text box as `0.50-0.55` a range reads correctly and compares to nothing, so nothing can ever check a measurement against it. `value_max` is optional and blank means a single figure. Formatting is one property, because a range assembled by hand at each call site is one call site away from printing the bottom of it and dropping the top. |
| FR-INV-10 | **A part can be recorded as used with no work order behind it.** Most of what a home garage has fitted was never a job in here — "I installed that fuel pump, I bought it in June" is a complete statement — and the alternatives were inventing a work order, which puts a fiction in a vehicle's history, or leaving the part on the shelf where it inflates what the shop believes it owns. It is the same FIFO draw at the same lot cost; a vehicle and a date are recorded when offered and required never. Naming the vehicle still records the fitment (FR-PART-3), because the part went on the car whether or not a job says so. |

### 7.5 Purchasing and vendors — `FR-PUR`

| ID | Requirement |
| --- | --- |
| FR-PUR-1 | Record a purchase with lines, tax, shipping, discounts, and core charges, and attach the receipt image. **A supplier's order confirmation may be read in instead of typed**: the PDF becomes the purchase, its lines, the parts, their fitment, and — where a line is listed as a kit component — the kit it belongs inside along with its share of the kit's cost (FR-INV-9), after a preview the operator confirms. |
| FR-PUR-2 | Support partial receiving; receiving creates stock lots at the actual landed cost. |
| FR-PUR-3 | Allocate tax and shipping proportionally across lines so landed cost is real *(SHOULD)*. |
| FR-PUR-4 | **Track core charges and surface uncollected ones — the money a home shop most often loses.** Cores have their own screen under **Parts**, and that placement is the requirement rather than an implementation detail: they were filed under Shelf, where the operator found the button twice by accident and could not find it a third time. Stumbling onto something twice is a stronger signal than never finding it — the screen was reachable, so where it was filed was the whole problem. A core is a deposit on a part somebody fitted; the bin it came out of has nothing to do with whether the old one went back to the counter. The screen shows the **returned as well as the owed**, because the question is *what happened to this core* and a list of debts alone cannot answer it — which also makes a core marked returned by a slip findable enough to undo. It totals what is outstanding, skipping parts whose core charge was never recorded rather than counting them as zero, since a total that reads unknown as nothing understates exactly what it exists to report. Marking takes **several at once**: cores come back in an armful, and ticking boxes and pressing one button is the difference between recording the trip and not bothering. Every "core owed" pill in the application links to it, so the way to settle a core is wherever the core is mentioned. |
| FR-PUR-5 | Surface a return-window warning based on the vendor's window and the receive date. |
| FR-PUR-6 | Link a purchase to a work order and show it as the blocker on a `waiting_on_parts` job. |
| FR-PUR-7 | Record returns and refunds as first-class events that correct both stock and cost. |
| FR-PUR-8 | **Receiving is undoable.** A receipt taken back writes the opposite movement into the ledger rather than erasing the original (FR-INV-1), under its own `unreceive` reason — a correction is not a cycle count. It is refused when the stock has since been used, because then the parts really did leave and only the paperwork is in doubt; a return or a scrap is the honest record. The purchase's status follows back down, which it previously could not do. |
| FR-PUR-9 | A purchase is deletable to the 30-day trash (FR-ADM-7) **only while nothing on it is received**, because a stock lot's landed cost points at it. The refusal names what to do first. |

### 7.6 Costs, receipts, time — `FR-COST`, `FR-TIME`

| ID | Requirement |
| --- | --- |
| FR-COST-1 | Roll up cost per work order: parts consumed (at lot cost) + expenses + optional labor value, itemized. |
| FR-COST-2 | Roll up lifetime cost per vehicle, split by category, including acquisition and excluding disposal proceeds; show net position after a sale. **A category that is really a stack of jobs says which.** `Parts — $1,240.00` is a true figure and a useless one: the question anybody has about it is *on what*, so the parts line carries its own breakdown by job, largest first — the screen is asking where the money went and the answer belongs at the top. Parts fitted with no work order behind them (FR-INV-10) get their own row rather than being dropped, and a long history is **summarized rather than cut**, because a breakdown that stops short without saying so does not add up to the total it sits under. The breakdown lives on the rollup, so the costs screen and the sale document cannot disagree about it, and it goes when the report is printed without costs. |
| FR-COST-3 | Compute cost per distance over any period from odometer history, stating the interval used. |
| FR-COST-4 | Attach a receipt to any purchase, expense, or work order, and OCR it in the background so receipts are text-searchable. |
| FR-COST-5 | Report spend by month, by category, by vendor, and by vehicle, with CSV export. |
| FR-COST-6 | Exclude tooling from per-vehicle cost by default, with an explicit toggle to include it. |
| FR-COST-7 | **Forecast the next twelve months of spend** from the schedule and from what this shop has paid for the same work before (§7.6a). Recurring services are counted as often as they recur at the asset's observed usage rate. A service with no cost history is counted and named but never priced at zero, and the total is presented as a floor with the shortfall stated. Exports to CSV with one row per expected occurrence, unpriced rows carrying an empty amount rather than a zero. |
| FR-COST-8 | **Track a budget against a work order and its children** (§7.6b). A budget is optional and belongs to any work order, not only a project; where one is set, the screen shows what has been fitted, what is on the shelf against the job, and what is still on order, and measures what is left against the sum of the three. An overrun is reported as a figure and drawn past the budget marker, never clamped. **Time valued at the labour rate is excluded** — a household budget is cash, and charging a notional rate for the operator's own Saturdays would report an overrun nobody's bank account saw. |
| FR-TIME-1 | Log time against a work order or job item, by timer or manual entry, attributed to a user. **Editable, and it used to be append-only.** The abstract argument was sound and wrong for this shop: picking the wrong category is the commonest mistake anybody makes here, and delete-and-retype is not a stronger record — it is the same one with a gap where the old row was. Nobody is billed from these numbers and no auditor reads them (NG-1), so immutability cost something every day and bought nothing. Readings, notes and media stay append-only, where the argument holds: those are captures from a moment, and a corrected odometer reading really is a different observation. |
| FR-TIME-2 | Report time per vehicle, per work order, and per category. |
| FR-TIME-3 | Value time at an instance-wide optional rate, clearly labeled as an estimate and **never** rendered as a bill. |

### 7.6a Cost forecasting — `FR-COST-7` (R-7)

`spend_by_month` looks backwards. This is the same shape looking forwards, and it exists because the two halves were already modeled and never joined: the schedule knows *when* each service comes round (§7.7, `project()`), and `service_completion` names the work order that paid for the last one, which already knows its own total.

Every service due in the next twelve months, at the price this shop has paid for it before, bucketed by the month it is expected to land in. Three judgments make the difference between a forecast and a number:

- **An unknown is not priced at zero.** A service this shop has never performed has no price. It is counted, named, and left *out* of the total, and the figure is labeled a floor with its shortfall stated beside it — "3 of these have never been done here". Treating unknown as free would understate exactly the thing the report exists to warn about, which is the same mistake the cores total already refuses to make (FR-PUR-4). A total that is quietly too low is worse than no total, because it gets believed.
- **A recurring service recurs.** An oil change on a daily driver lands three times in a year and once on a truck that leaves the yard twice a month. The recurrence comes from the item's own interval against the asset's **observed** usage rate, so the two vehicles forecast differently — and a projection that counted each schedule item once would be roughly half of what actually happens, wrong in the same direction for everybody.
- **Shared cost is split, not counted twice.** One Saturday's work order that closed an oil change, an air filter and a tire rotation is not three bills, so its total divides by the number of services it completed. That under-attributes the expensive one and over-attributes the cheap ones; it is the best answer available without asking somebody to itemize their weekend, and it is right in aggregate, which is what a forecast is. The **median** of past occurrences is taken rather than the mean, so one brake job where the caliper also failed does not reprice every future brake job.

Two smaller rules follow from the same reasoning. A work order that recorded no cost prices nothing rather than pricing zero — it is far likelier to be a job whose parts were never entered than a service that is genuinely free. Something already overdue is spend that has not happened yet, so it lands in the month ahead rather than being dropped for having a date in the past, which would forecast nothing for the vehicle most likely to need money spent on it.

Snoozed items and retired vehicles are excluded: snoozing says *not now*, and a forecast that argues with the operator is one they stop reading. The CSV export carries one row per expected occurrence with an **empty** amount where there is no price — never a zero, because a spreadsheet sums a zero and cannot sum a blank.

### 7.6b Project budget burn-down — `FR-COST-8` (R-6)

Home builds overrun; watching it happen is the point. The roadmap row predicted a column and some arithmetic, and the arithmetic turned out to be the easy half.

**The first thing built was not the budget.** `work_order_cost` answers for one work order, and a project's parts, expenses and hours are on its *children* — the teardown, the machine work, the reassembly. FR-WO-8 had promised that roll-up since the first draft and nothing implemented it, so the burn-down's first version reported an engine swap as barely started. That is the failure mode worth naming: a budget screen built on an incomplete total does not merely omit something, it says **on budget** about a project that is not, which is worse than showing nothing at all. `tree_ids()` walks the tree; `project_cost()` sums it; the project's own page now lists what is under it, which it never did.

**One number cannot answer the question, so three are shown.** *"What has this cost me"* and *"what is on the vehicle"* have different answers, and a single figure has to be wrong about one of them:

- **Fitted** — parts consumed at the lot cost actually drawn, plus expenses. This is the burn-down line, and it uses the same basis as `spend_by_month` (FR-COST-5), so the two reports cannot disagree about the same money.
- **On the shelf** — bought against this job, arrived, not yet fitted. Traced through `stock_lot.purchase_line`, which is the only thing that remembers why a box is on the shelf.
- **On order** — the un-received share of the job's open purchases, valued by scaling the order total so that tax and shipping ride along. Those overheads are apportioned at receiving, so pricing an undelivered line at its bare unit price would understate it by the shipping already on the invoice.

Collapsing the last two into "fitted" double-counts them the moment the parts go on. Dropping them lets a project read as comfortably under budget on the day before a pallet arrives. So `remaining` is measured against all three, and only the first draws the line.

**Your own time is not budgeted.** `LABOR_RATE_MINOR` values a Saturday for reporting (FR-TIME-3) and a household budget is cash. Charging a notional rate against it would report an overrun that never left anybody's bank account — and on a project, which is where the hours are, it would dominate everything else on the bar. The hours are shown beside the figures and never move it.

Three smaller rules follow. **An overrun is drawn, not clamped**: the bar scales to whichever is larger, the budget or what has been committed, so the bands run past the budget marker instead of pinning at the end and hiding that it happened. **A quiet month still gets a row** — a burn-down is about pace, and a series assembled only from the months that have rows in them draws a slope that is wrong. And **`NULL` is not zero**: a work order with no budget gets no card, because an empty burn-down is a screen telling somebody they have spent 0% of nothing, while a budget of zero is a real statement by somebody who means this job to cost nothing and wants to hear the moment it does not.

The column is `budget_minor` rather than the row's `budget_cents`, matching every other money column in the schema (§5.5), and it is offered on **any** work order rather than only on `type = project`. A brake job somebody has decided is worth $400 is the same question, and a field that refuses to be filled in is a worse answer than one nobody uses.

### 7.7 Maintenance and reminders — `FR-MAINT`

| ID | Requirement |
| --- | --- |
| FR-MAINT-1 | Ship built-in schedule templates (generic gasoline/diesel/EV, normal and severe service) as seed data. |
| FR-MAINT-2 | Assign a template to a vehicle, materializing editable per-vehicle service items. |
| FR-MAINT-3 | Support intervals by distance, time, engine hours, or any combination, with first-to-arrive semantics. |
| FR-MAINT-4 | Compute `next_due` and project a due *date* from observed distance-per-day, stating the basis. |
| FR-MAINT-5 | Roll intervals forward automatically on completion of a linked job item (`service_completion`). |
| FR-MAINT-6 | Back-fill history: record a past service (with date and odometer) that sets the baseline without inventing a work order. |
| FR-MAINT-7 | Provide a cross-vehicle due/overdue dashboard as the app's default landing view. |
| FR-MAINT-8 | Snooze or disable a service item with a reason. |
| FR-MAINT-9 | Import and export schedule templates as YAML/JSON so they can be shared between instances — **built, see FR-MAINT-11**. Worth recording that this sat as an unimplemented *(SHOULD)* long enough for OQ-2 to cite it as the reason a shared repository was unnecessary. |
| FR-MAINT-11 | **Schedule templates are portable, and installable from a shared catalog** (§8.1b). A template exports to YAML carrying its own service definitions — a file assuming the receiving shop already had "Engine oil and filter" under that exact name would import as an empty schedule on half the instances that tried it — matched on import by `translation_key` before name, so a shop running in another language does not collect a duplicate every time. Import refuses rather than coerces: unknown fields, unknown interval units, an item with no interval at all, and a name already in use. A catalog entry is installed through that same validator and no other. **Templates can also be removed** — including built-in ones, since an operator who runs no diesels should not scroll past a diesel schedule forever — from both the templates list and the catalog, because those are the two places somebody notices they do not want one and a control present in only one of them is a control they hunt for (the lesson of FR-PUR-4). Removal is soft, so it is recoverable from the trash; already-applied schedules are untouched because applying materializes them onto the vehicle; and **the seeders leave a deleted built-in deleted** rather than restoring it on the next boot — which is not only a matter of not arguing with the operator, since `slug` is uniquely constrained regardless of `deleted_at` and re-creating one would fail on the constraint. A template picker names each option's source, author and size, because two templates covering the same ground are otherwise a coin toss. **Removal has two ways home**, because they cover different lengths of time: for thirty days it is in the trash like anything else soft-deleted, and after that the shipped set is still in the image, so **Restore shipped templates** puts back any built-in that was removed and leaves everything the operator wrote alone. Without the second, removing a built-in was a one-way door — the catalog deliberately publishes nothing that duplicates the shipped set, so it was never going to be the way back. |
| FR-MAINT-12 | **A scheduled item can be taken off a vehicle, and applying a template can replace rather than only add.** FR-MAINT-8's snooze-or-disable was the only way to say no to a scheduled item, and a disabled item stays in the list — so a vehicle moved from one template to another carried both schedules forever and the list only ever grew. Removal draws its line at history: an item **never completed** is a plan, and a plan belongs to whoever owns the vehicle; an item with completions is a record, and removing it would take the record with it, so that one is refused with a message naming *Ignore* as the honest way to retire it. Removal is soft, and re-applying any template that names the item **revives that same row** with its last-done date and its completions rather than starting a second one beside it — without which removal would be a trap, the apply reporting items added and the item not appearing. Applying a template offers **Replace**, off by default, which takes off whatever the incoming template does not include under the same rule and reports how many stayed and why. Ignored items are folded away on the schedule screen rather than listed among the live ones, since an ignored item is a decision already made. |
| FR-MAINT-10 | Deliver reminders by email (SMTP), web push, or webhook; **all notification channels are opt-in and off by default** *(SHOULD)*. |

### 7.8 Inspections (DVI) and components — `FR-DVI`, `FR-CMP`

A Digital Vehicle Inspection is a structured, repeatable walk of a vehicle producing a dated, photographed, measured record. For a commercial shop it is a sales document. **For a home shop its value is different and better: it is the only feature that tells you what is going to need attention *before* it strands someone**, and — because measurements repeat over time against a known installed component — it converts observation into prediction.

It also covers a scenario nothing else in this spec does: **inspecting a vehicle you do not own yet.** A pre-purchase inspection is the highest-leverage thirty minutes in the whole hobby, and it happens in a stranger's driveway with a phone and no signal.

| ID | Requirement |
| --- | --- |
| FR-DVI-1 | Define inspection templates as an ordered set of points grouped by area, scoped to applicable vehicle classes. |
| FR-DVI-2 | Support three point types: pass/attention/fail status, a numeric measurement, or both. |
| FR-DVI-3 | Support **positional** points — a tire tread point yields 4 wheels × 3 positions, brake pads 4 corners × inner/outer — without defining twelve separate points by hand. |
| FR-DVI-4 | Compute status automatically from configured thresholds, record it as `auto_status`, and allow the inspector to override with the override retained. |
| FR-DVI-5 | Attach photos per result, and require a photo where the template says so (`on_attention` is the useful default for safety-critical points). |
| FR-DVI-6 | **Snapshot the template onto the inspection**, so later template edits never rewrite historical inspections. |
| FR-DVI-7 | Run an entire inspection **fully offline**, including photos, and sync on reconnect (§5.4). This is not optional — inspections happen under a car, in a driveway, on bad signal. |
| FR-DVI-8 | Convert any `attention` or `fail` result into a job item on a new or existing work order in one action, carrying the note and photos across. |
| FR-DVI-9 | Produce a shareable inspection report (PDF) with per-area status, measurements, and photos. |
| FR-DVI-10 | Run an inspection against a **prospective** vehicle (status `prospect`) that is not yet owned, and either promote it to the fleet on purchase or retain it as a record of a car that was walked away from. |
| FR-DVI-11 | Chart any measurement point over time for a vehicle, and where it maps to a `asset_component`, project a wear rate and a **predicted replacement point** that feeds the due dashboard (§7.7). |
| FR-DVI-12 | Compare an inspection against the previous one for the same vehicle, highlighting what changed. |
| FR-DVI-13 | Ship built-in templates ([REFERENCE.md §2](REFERENCE.md)) and support user templates, with YAML import/export ([SCHEMA-INSPECTION-TEMPLATES.md](SCHEMA-INSPECTION-TEMPLATES.md)), and install published checklists from the catalog (§8.1b). The format is the one that document specifies, validated against it on import. **Thresholds get the strictest checking**: they decide `auto_status`, so an unknown comparison, a bound that is not a number, a backwards `between`, a rule grading to a status only a person may choose, or thresholds on a point that records no measurement are each refused with the point named — a rule that reads wrong is a confident wrong answer about a brake pad, and it would otherwise be found on a driveway. A retired area (`fluids`) is kept and reported rather than refused, since inspections recorded under it still render. |
| FR-DVI-14 | Show an inspection's history on the vehicle timeline (FR-VEH-10) alongside work orders and diagnostics. |
| FR-CMP-1 | Track installed components with position, install date and odometer, and link to the job item that installed them. |
| FR-CMP-2 | Create a component automatically when a part with a component-bearing category is consumed on a work order, prompting only for position. |
| FR-CMP-3 | Record removal with a reason, retaining the full component history for the vehicle. |
| FR-CMP-4 | Support **rotation** — recording a position change without treating it as removal and reinstallation. |
| FR-CMP-5 | Show component age in both time and distance, and surface warranty status while it is live. |
| FR-CMP-6 | Record DOT date codes for tires and surface age independently of wear — **a tire with full tread and a ten-year-old date code is a failed tire**, and nothing else in the system would catch that. |

### 7.9 Documents, photos, specs — `FR-DOC`, `FR-SPEC`

| ID | Requirement |
| --- | --- |
| FR-DOC-1 | Attach images and PDFs to vehicles, work orders, job items, parts, purchases, expenses, and people. |
| FR-DOC-2 | Capture straight from the device camera into the current context, with multi-shot capture. |
| FR-DOC-3 | Generate thumbnails and previews asynchronously; the UI is never blocked on processing. |
| FR-DOC-4 | Preserve originals byte-for-byte; derivatives are always regenerable. |
| FR-DOC-5 | OCR documents in the background and include the text in global search. **Every image is rotated by its own EXIF orientation first**, which is not a refinement: a phone held sideways writes pixels in sensor order, and Tesseract returns nothing at all from a page rotated ninety degrees. A photograph of a receipt was being filed `done` with empty text, which looks exactly like a photograph with no text in it. Photographs are also flattened for uneven lighting before reading — the same preparation the scan-report path uses, because one picture must not read two different ways depending on which door it came in by (§8.3a (a2)). |
| FR-DOC-6 | Deduplicate by SHA-256 and offer to link the existing media instead of storing a second copy. |
| FR-DOC-7 | Tag photos as `before`/`after` on a job item and display them paired. |
| FR-DOC-8 | Serve media only to authenticated users, via short-lived presigned URLs; **the bucket is never public**. |
| FR-DOC-9 | Strip GPS EXIF on upload by default, with a per-instance setting to retain it. |
| FR-DOC-10 | A photograph enlarges over the record it belongs to, keeping that record's scroll position; a document opens in its own tab, where the browser's own viewer can page and search it. Which one a file gets is decided by the file, not by the screen. Both are links to the file first: with scripting blocked every attachment still opens. |
| FR-DOC-11 | An attachment can be taken off the record it is on. **Detaching, not deleting** — a link is not ownership, and one receipt legitimately hangs off both a purchase and a work order (§6.2), so removing it from one leaves the other intact. The file itself goes only with its last link, because past that point no screen can reach it. Both halves are soft, so both are recoverable from the trash. |
| FR-SPEC-1 | Record arbitrary grouped key/value specs per vehicle with units, conditions, and a source. |
| FR-SPEC-2 | Link a spec to the page of the document it came from *(SHOULD)*. |
| FR-SPEC-3 | Copy a full spec sheet from one vehicle to another (same model, second car). |
| FR-SPEC-4 | Surface the vehicle's specs in a persistent quick-reference panel on its work orders — **the lookup happens mid-job, not at a desk**. |

### 7.9a Oil and fluid analysis — `FR-FLU` (R-5)

| ID | Requirement |
| --- | --- |
| FR-FLU-1 | Record a lab sample against a vehicle or a piece of equipment: where it was drawn from, when, the meter reading, **how far the fluid itself had run**, the lab, the report number, the fluid and grade, and optionally the job it belongs to. |
| FR-FLU-2 | Record the panel as rows — analyte, value, unit, and the lab's own reference figure where the report prints one. Anything a lab reports is storable, including something this application has never heard of. |
| FR-FLU-3 | **Enter a panel by pasting it.** One analyte per line, tolerant of `Fe 24`, `Iron: 24 ppm` and `Viscosity @ 100C 10.9`. A line that cannot be read is **listed back with the reason**, never dropped. |
| FR-FLU-4 | Trend each analyte across the samples from the **same compartment on the same asset**, oldest first. A differential and an engine are separate series; so are a front and a rear differential. |
| FR-FLU-5 | Express an accumulating analyte as a rate per 1,000 units of fluid life, and **never** express one that does not accumulate as a rate. A sample with no fluid interval recorded is shown, excluded from the rate, and counted in a stated shortfall. |
| FR-FLU-6 | Store the lab's comment verbatim and attribute it. **No pass, fail, or severity is ever computed** — see below. |
| FR-FLU-7 | Attach the lab's own report **to the sample**, by camera or by file (FR-DOC-1/2). The numbers on the screen are a transcription of that document, and a transcription with no route back to its source cannot be checked — the same reason every code list in this application cites the page it was read from. |

**A wear metal is a rate, not a level, and that is the whole design.** Iron enters the oil while the oil is in service and keeps entering: 24 ppm on 3,000 miles and 24 ppm on 9,000 are different statements about the same engine, and a chart that put both on one line would read as flat where wear had in fact fallen by two thirds. So the sample carries `fluid_usage` — how far the *fluid* had gone — alongside the odometer reading, and it is the number nearly every home sample forgets. It is the same judgment `asset_component` makes about a tread depth (§6.2): a measurement is a number, and a measurement against an interval is a rate.

**Three quarters of a panel must never be turned into a rate.** Wear metals and contamination accumulate. Additives *deplete* — zinc and calcium are put in by the blender and consumed — so a falling number expressed as a rate would read as an improving one. Viscosity, TBN and flashpoint are states of the fluid as it is now, and there is no such thing as viscosity per thousand miles. Water and fuel dilution are concentrations rather than totals. Each analyte therefore carries an `accumulates` flag, and an analyte this application does not recognise is assumed **not** to accumulate: the safe default is showing the number the lab printed rather than converting it by a rule nobody has checked.

**No verdict is ever computed, and that is a decision rather than an omission.** Limits are engine-specific, lab-specific, and genuinely contested; an application printing PASS over somebody's engine would be asserting a threshold nobody set and no one could defend. What is stored is the lab's own comment, verbatim and attributed, and what the application says about a number is arithmetic on the operator's own history — *"three times the previous sample"* — which is a fact about their data rather than a judgment about their machine. This is NG-1 applied to a second surface: the same reason a time entry is never rendered as a bill.

Two consequences of shape. Results are **rows, not columns**, because engine oil, gear oil, coolant and brake fluid have different panels and a column per element would be forty nullable columns plus a migration every time a lab revised its own. And the panel is **pasted rather than typed**: thirty text boxes is a form somebody fills in for the first sample and never for the fourth, which is the only one that would have shown a trend. Unreadable lines come back on the screen with the reason, on the same principle the scan-report transcription follows (§8.3a) — refuse visibly rather than guess quietly.

**The report is filed on the sample, not on the vehicle** (FR-FLU-7). Loose on the vehicle it is findable only by remembering which of eleven documents belongs to the March sample, and the question it answers — *is this figure typed right?* — is asked while looking at the figure.

**Not built:** reading that PDF automatically. It is the same problem the parser profiles solve for scan reports (§8.3a) and would need a profile per lab; the paste box covers it without claiming a capability that does not exist.

### 7.10 Search, dashboard, reporting — `FR-SEARCH`, `FR-REP`

| ID | Requirement |
| --- | --- |
| FR-SEARCH-1 | Provide one global search across vehicles, work orders, parts, part numbers, people, vendors, notes, and OCR'd document text, returning grouped results. |
| FR-SEARCH-2 | Return results in under 300 ms at the scale targets in §11. |
| FR-SEARCH-3 | Support field filters (`vehicle:`, `status:`, `tag:`, `year:`) in the query string *(SHOULD)*. |
| FR-REP-1 | Landing dashboard: overdue and due-soon services, open work orders, jobs waiting on parts, expiring registrations, outstanding cores, low stock. |
| FR-REP-2 | Per-vehicle report: full service history, cost summary, open items — exportable as PDF (**the sale document**, G-1) and CSV, both drawn from the same `report_sections` the preview renders, so the three cannot disagree about what the report contains. The CSV writes each section as its own block with its own header row: a vehicle report is six differently shaped tables, and flattening them gives a file whose header lies about most of its rows. **The report is shown before it is produced.** The button used to start a download, which is the wrong shape for this document in particular: it is the one handed to a buyer, it is the one place sensitive specs are deliberately withheld (C-5), and how complete it looks is the whole question — all of which is worth answering while there is still time to fill a gap in, rather than after a file has landed in a downloads folder. The page says how many sections have anything in them and states plainly that a sensitive spec was left out, since somebody who wanted it in there should find out here rather than from the buyer. Page and PDF are drawn from **one description of the content**, so a preview cannot promise a section the document omits. |
| FR-REP-3 | Shop reports: spend over time, inventory value, time invested, vendor spend. |
| FR-REP-4 | Every report exports to CSV; **no report is a dead end**. Stated since v1 and **not yet true**: the shop reports and the vehicle report export, and the per-vehicle costs screen, the due list, the wear chart and the shelf do not. Named here as an open gap rather than left as an assumed capability — see §19. |

### 7.11 Integrations — `FR-INT`

| ID | Requirement |
| --- | --- |
| FR-INT-1 | Every integration is individually enableable, testable from the UI with a real connectivity check, and disabled by default except the free, keyless NHTSA services. |
| FR-INT-2 | Show an integration activity log — endpoint, timestamp, requesting user, outcome, and (where metered) running call count — reviewable by the operator. |
| FR-INT-3 | Honor **Offline Mode** globally: no integration makes an outbound call, and the UI presents affected features as intentionally unavailable rather than broken. |
| FR-INT-4 | Import a scan-tool PDF report, extract a draft diagnostic session, and require operator confirmation before it enters vehicle history. |
| FR-INT-5 | Retain the raw report and its extracted text permanently, and support **re-parsing** historical reports after a parser profile is added or improved. |
| FR-INT-6 | Fall back to a manual mapping wizard when no parser profile matches, and offer to save that mapping as a new profile. |
| FR-INT-7 | Import, export, and version parser profiles as YAML, with a test fixture corpus verifying each against known-good expected output. **A published profile carries an author and may be proven against captured reports** (§8.1b). This matters more for profiles than for anything else shared: a schedule states its intervals in prose a reader can judge, while a profile is regexes over a scan report whose correctness is not knowable by looking — two files both named `XTOOL D8`, one written by somebody holding the tool and one guessed, are otherwise indistinguishable. Naming reports is not a claim: the build runs the profile over each and refuses to publish one that cannot read them, and requires **at least two**, since a profile overfitted to a single capture passes a single-report check by construction. Publishing unproven stays allowed and is shown as such, because scan reports carry VINs and not every contributor can share one. **Profiles are listed, imported and removed beside schedule templates and inspection checklists** (§8.1b), not on a page of their own: they are the third of the same thing — YAML carrying an author and a source, installed from the same catalog through the same validator — and the page that lists the other two already told the reader it covered scan-tool profiles while listing none. The old address redirects rather than 404s, and the scan queue's button now lands there. **A profile can be removed**, not only switched off, which was the same gap schedule templates had: the catalog could install one and nothing could take it away. Removal is soft and leaves what was already read alone — a session points at its profile with `SET_NULL` and keeps `parser_version` in a column of its own, so it goes on saying which version read it — and **Restore shipped templates** puts back a removed built-in, without which removing the profile that reads XTOOL D8 reports would be a one-way door. |
| FR-INT-8 | Pin a resolved service-information URL per vehicle per provider, reachable in one tap from the vehicle and from any of its work orders. |
| FR-INT-9 | Ship LEMON, Operation CHARM, and ALLDATA DIY as seeded providers, with LEMON as the default and mirror-domain failover for it. |
| FR-INT-10 | Never crawl, scrape, mirror, or background-fetch a service-information provider; links are rendered for a human to click. |
| FR-INT-11 | Connect to a LubeLogger instance with a base URL and API key, verify reachability, scope, version, and **locale-invariant response formatting** before permitting any import. |
| FR-INT-12 | Run every LubeLogger import as a **dry run first**, reporting per-entity counts, unmatched vehicles, and sample mappings before writing anything. |
| FR-INT-13 | Make LubeLogger imports idempotent via `external_ref`, support scheduled incremental pulls by date window, and never duplicate on re-run. |
| FR-INT-14 | Never overwrite a locally edited record with source data, and never propagate a source deletion; surface both as reviewable conditions. |
| FR-INT-15 | Fetch and store attachments from imported LubeLogger records locally, so retiring the source instance loses nothing. |
| FR-INT-16 | *(MAY)* Push garage-captured odometer readings back to LubeLogger, opt-in, dry-run first, requiring an Editor-scoped key. |

### 7.12 Administration — `FR-ADM`

| ID | Requirement |
| --- | --- |
| FR-ADM-1 | First-run wizard: create the admin, set units/currency/timezone, name the shop, explain TLS trust, and offer seed data. |
| FR-ADM-2 | Manage users (invite, deactivate, reset password, promote, grant vehicles to a helper); **deactivating a user never deletes their authored records**. Deactivation is the answer for somebody who worked here — their name belongs on what they did, and taking the key away must not rewrite it. It was for a while the *only* answer, which was wrong about the other case: an account created by mistake or while trying the application out has no history to protect, and refusing to remove those left an instance filling with fragments whose only cure was to wipe it and start again. So an account may be **deleted exactly when nothing in the shop carries its name** — the same shape FR-ADM-8 uses for vendors and locations, and a rule with near-zero risk because there is by construction nothing to lose. The refusal names what is holding the account ("3 work orders, 2 time entries") rather than saying it has history, because the first is a list of things to deal with and the second is a dead end. You cannot delete yourself, and the last administrator is kept. |
| FR-ADM-3 | Configure integrations with connectivity tests and clear per-integration enable/disable. |
| FR-ADM-4 | Configure and run backups, and show the last-successful-backup age prominently, with a warning past 7 days. |
| FR-ADM-5 | Trigger a full export and download it. |
| FR-ADM-6 | Import from CSV with column mapping for vehicles, parts, and service history — **so the spreadsheet this replaces can actually come along**. |
| FR-ADM-7 | Show a 30-day trash with restore for soft-deleted records. | Schedule templates and inspection checklists are trashable too — they were soft-deleted and listed nowhere, which made a removal permanent and invisible at once.
| FR-ADM-8 | Show instance health: version, DB size, media size and count, job queue depth, failed jobs, last backup. **This number is used twice.** §7.4 carries a second FR-ADM-8 (*the small records are correctable and removable*) and an FR-ADM-9 beside it, both added later and both now cited under those numbers in the code. Renumbering either would make eight comments point at the wrong row, so the collision is recorded rather than repaired: cite §7.4 or §7.12 alongside the number. |

---

## 8. External integrations

Every integration is an **adapter behind an interface**, disabled by default, individually toggleable, and non-blocking. Every outbound call is logged with timestamp, endpoint, and outcome, and is visible to the operator at Settings → Integrations → Activity. Concrete endpoint paths and response shapes must be re-verified at implementation time and pinned; the adapter boundary exists precisely because these services change.

### 8.1 VIN decode — NHTSA vPIC (free, no API key)

- **Service:** NHTSA Product Information Catalog and Vehicle Listing (vPIC), `vpic.nhtsa.dot.gov/api`, `DecodeVinValues` returning a flat field set. Free, no key, US-market vehicles.
- **Behavior:** invoked only on explicit user action. 5-second timeout, one retry, then fall back to manual entry with a clear message. The **raw response is stored** on the vehicle alongside the mapped fields, so a later mapping improvement can re-derive without another call.
- **Coverage honesty:** vPIC is authoritative for US-market vehicles from roughly 1981 onward. Gray-market, pre-1981, and heavily modified vehicles will decode poorly or not at all. The UI must present a thin decode as normal, not as an error.
- **Offline:** WMI (chars 1–3) and model-year (char 10) can be interpreted locally to pre-fill a plausible year and manufacturer with low confidence, and the check digit validated, with **zero** network access.
**Optional offline dataset (OQ-6: yes, as an opt-in download).** NHTSA publishes the vPIC dataset. The application does **not** bundle it — that would inflate the image for every user to serve a minority — but Settings → Integrations offers **Download offline VIN dataset**, an admin-triggered `vin.dataset.sync` job that fetches, converts, and loads it into local tables.

| Aspect | Behavior |
| --- | --- |
| Trigger | Explicit admin action, never automatic, never on first run. |
| Cost disclosure | The UI states the download size and resulting disk usage **before** starting, and shows progress with a cancel. |
| Precedence | When the local dataset is present it is used **first**; the network is consulted only for VINs it cannot resolve, and not at all in Offline Mode. |
| Freshness | The dataset's vintage is displayed wherever a decode sourced from it appears; refresh is a manual re-run. |
| Reversibility | Removable from the same screen, reclaiming the space, falling back to the online path. |
| Caveat | NHTSA's distribution format is not directly loadable and requires a conversion step; that converter is the actual work here, and it must fail loudly rather than half-loading a dataset. |

### 8.1a Pre-1981 VIN decode — local tables, no service

vPIC stops at the 17-character VIN, which means it stops at the 1981 model year. Before that every manufacturer numbered vehicles its own way, so there is no service to ask and no standard to apply — but there are published tables, and reading one is a lookup rather than a request.

- **Source:** manufacturer chassis-identification sheets, currently LMC Truck's, kept under `Artifacts/VIN Decoding/`. Each transcribed scheme names the file it came from, so a disputed entry is checked against the page rather than argued about.
- **Data, not code.** `vin_schemes.py` holds the schemes — fixed-width fields plus lookup tables — and `vindecode.py` is one generic matcher. The schemes differ per make, per era and sometimes per model line; a function per manufacturer would be the same function twenty times, and the interesting part is the transcription, which belongs somewhere it can be corrected by somebody holding the door plate.
- **The year is often not a position.** Ford's trucks of 1961–79 carry no model-year field at all: the year is which block of the production run the consecutive unit number falls in. Any rule built around character positions was never going to read one.
- **Transcription is guarded by test.** A mistyped table does not raise — it answers wrongly and confidently. Every scheme carries an example that is checked to decode on every run. Where the published example does not decode under its own scheme (several do not; they are layout illustrations, not real VINs) the discrepancy is recorded beside the entry.
- **Coverage is stated, and so are the gaps.** Ford trucks, Bronco and vans 1948–80; Chevrolet and GMC trucks and vans 1947–80; Dodge trucks and vans 1971–80 — 32 schemes across four makes. Deliberately absent, each for a reason recorded beside it: GMC 1951–55 1st series, where the year position exists only from 1954 and the plant codes only from 1952, so the same characters mean different things at three lengths within one sheet; the production-number charts several sheets offer, which would narrow years the codes leave open — they are legible, and absent because a year read off them depends on the plant, the drive and the tonnage at once and the data format has no way yet to say so; and everything from 1981, which is vPIC's job and which it does with more detail than these sheets carry.
- **A second sheet checks the first.** Alongside each VIN sheet LMC publishes an `*_Engine_ID.pdf` listing every engine code against the years and models it was fitted to — the only independent check these tables have. Reading the two against each other found the 1953–56 engine table claiming the 239 V8 was a 1955 only when it ran from 1954, which made a genuine 1954 truck decode as a contradiction and be refused. Where a scheme has been checked this way it names both documents. Two rules govern a disagreement, and they differ because the costs do: **on which years a code was offered, take the union** — a year too broad costs one more check, a year too narrow refuses a vehicle that exists — and **on a detail the sheets flatly contradict, a carburettor or a horsepower, print neither**, since none of it decodes anything and a disputed number is worth less than a blank.
- **On GM's numbers the second sheet is not only a check.** GM stamped no engine code. What the number carries is a *flag* — a leading `V` on a Chevrolet, an `8` before the plant on a GMC — and the absence of it is the six, which is why those are separate schemes of a different length. So the plate says six-or-eight and the year, and `CA_Engine_ID.pdf` says which six and which eight: a 1957 GMC with the flag had the 347, and a 1958 Chevrolet without it had the 235. The displacement is therefore **derived from the year rather than read from a position**, which changes what may be done with it. It is labeled on screen as the engine standard that year rather than as a code off the plate; it never narrows the year it was derived from, since that would be the decoder agreeing with itself; and it is excluded from how completely a scheme is judged to have read the number, because a scheme that inferred a fact must not outrank one that read a character. The same sheet's second page does the equivalent for the chassis: a Chevrolet's series code plus its era gives the class and wheelbase — a 3600 is a 125.25″ truck through the 1955 1st series and a 123.25″ one after — while the bed type on that page stays unread, since it lives in the fourth digit of the model number and the VIN carries only the first three. Reading the two GM sheets together also produced the second contradiction: the VIN sheet marks a leading V as a V8 across 1953–55 1st series and the engine sheet lists no Chevrolet truck V8 before the 1955 2nd series, so by the rules above the era stays as the VIN sheet gives it and the displacement is left blank.
- **Three sheets are photographs, not text**, and were read by OCR. Where a table did not survive — the Dodge van's model years came through as `NODRADBIR@XOP`, the GM van's engine table broke into fragments — it is taken from the sibling sheet that prints the same codes legibly, and the substitution is recorded in the data rather than smoothed over. Where a doubt could not be resolved that way it is left standing: the Ford van sheet prints `E18` where the next column prints `E16` for the same van, and 6/8 is exactly what this scan confuses, so it is transcribed as printed and the discrepancy noted.
- **It fills the vehicle in**, on an explicit press and never on page load, holding the same line §8.1 holds for vPIC: blanks only, never over a field the operator corrected (FR-VEH-4), and the provenance recorded as `vin-tables` rather than `vpic`, because a field filled from a table transcribed off a scan and one filled from NHTSA are not the same claim. Two further rules are its own. **It refuses where the reading is ambiguous** — writing one of two honest readings would turn a question the screen is asking into a fact the record asserts — and the refusal names the model year as what usually separates them. **It writes only what the sheet says**: a year when the reading settled on one and not when it offered a range; a model only from a scheme that declares which position names one, since Ford stamps `F-250 4WD` where GM stamps `1/2 ton` and a tonnage in the model column is worse than a blank — and only the designation, never a description of it, because a position that names the model is also the position a reading is tempted to enrich, and `3600, 3/4 ton pickup, 125.25 in wheelbase` is a sentence about a model rather than one; and an engine only where the reading resolved to a single one. Ford's `H` is a 390 through 1976 and a 351M after it, and with the year still open the reading honestly offers both — but `390 CID V8 / 351M CID V8` in an engine column is not an engine, it is the question still being asked, and a vehicle record is no place to ask it.

### 8.1b The shared template catalog — published files, no service (R-1)

Somebody has already worked out the severe-service schedule for a 7.3 Power Stroke and written the parser profile for an Autel scanner. Retyping either from a forum post is exactly the work this application exists to remove, and it is the one thing an instance genuinely cannot produce for itself.

**The format came first, because the reason R-1 was deferred turned out to be false.** OQ-2 held that "import/export files only in v1" already covered the ninety-percent case. That was true of parser profiles, which had `to_yaml`/`from_yaml` and a screen. It was not true of **schedule templates**, which had no import or export of any kind — and those are the artifact a shared repository is mostly about. So `maintenance/templatelib.py` is the file format §8 promised, and the catalog is a way of delivering files it already accepts. A template exported from any instance is a catalog entry.

**The network dependency is made to obey P-1 rather than argued with.**

- Nothing is fetched unless somebody presses a button — no background check, no update poll, nothing on start-up. The same line FR-INT-10 holds for service-information providers.
- Every request goes through `core/outbound._get`, so Offline Mode refuses it and the allowlist governs it exactly as for every other outbound call. Files were briefly published wrapped as `{"body": "…"}` so the then-only guarded fetcher could read them — which put an implementation detail of this codebase in front of every contributor, who would have had to JSON-escape a YAML file by hand. The guards were extracted instead: **one guarded GET, two decoders**, so the file a contributor commits is the file an instance reads. The rule worth keeping was one enforcement point, not one body shape.
- Nothing depends on it. Bundled templates ship in the image, the catalog is additive, an instance that never reaches it is not degraded, and one that reached it yesterday keeps what it installed.
- **`CATALOG_URL` defaults to this project's own catalog**, because expecting somebody to stand up a repository before they can install a schedule is expecting them not to bother. A default *address* is not a default *request*: the address is contacted only when somebody presses Browse, which is asserted by a test that the dashboard and the templates screen touch it not at all. Its host is declared in the privacy sweep's third-party list, so pointing the application at a real service out of the box stays a deliberate, reviewed act rather than something that drifted in. A catalog that cannot be reached is a sentence on a working screen, never an error page.

**The trust model has two layers, and only one is in the code.**

The outer layer is editorial: the default catalog is a folder in this project's own repository, so an entry becomes published by being reviewed and merged. That is the layer that addresses the question actually worth worrying about — whether a schedule's intervals are sensible — which no amount of code can check. `catalog/README.md` states what a reviewer looks for. **The index is generated, never hand-edited** (`manage.py build_catalog`): an index somebody had to remember to update is one that silently publishes nothing when they forget, and names and descriptions read out of the files themselves cannot disagree with what they point at. The command validates every file with the same validator an instance will use, and the test suite runs it with `--check`, so a broken template or a stale index fails the suite. That check is worth more than the index-writing: it moves the first failure from somebody's garage to the pull request, while the author is still looking.

The inner layer exists because `CATALOG_URL` is a **setting**. An operator may point it at their club's repository or their own fork, and on that day the editorial layer is somebody else's process or nobody's. So the code is written as though the catalog were a postman rather than an authority. The guards cost nothing, they do not weaken with a reviewed source, and they are all that stands where the review does not reach:

| Rule | What it stops |
| --- | --- |
| **A downloaded file goes through the identical validator an uploaded one does.** No privileged import path exists. | The catalog being trusted into doing anything a stranger's emailed file could not. |
| **An index entry names a path, never a URL.** An absolute address, a scheme, a host or `..` is refused, and the resolved URL is asserted to sit under the configured base. | The repository choosing which host this instance talks to — which would leave the allowlist rubber-stamping somebody else's decision rather than the operator's. |
| **The install form is matched against the published index**, not trusted from the POST body. | An install button that is really a fetch-anything button. |
| **Nothing installed is applied.** Installing adds a template to the shop's list; putting it on a vehicle stays a separate act. | A stranger's file deciding when somebody's brakes get checked. |
| **Where it came from is recorded** as `source=imported`. | A downloaded schedule that later proves wrong being indistinguishable from a built-in one. |

Signatures are deliberately absent. A signature proves a file came from whoever holds a key, which is not the question; the question is whether the intervals suit this truck, and no signature answers it. The honest protections are the review, the validator, the reading somebody does before installing, and the fact that a template does nothing until it is applied.

An entry of a `kind` this version does not know is **skipped and counted**, not refused, so an older instance browsing a newer catalog sees what it can use and is told how many entries it could not — rather than silently showing a shorter list than exists.

### 8.2 License plate → VIN (paid, pluggable, off by default)

- **Reality:** there is **no free, legal, general-purpose plate-to-VIN API**. This capability requires a commercial vehicle-data provider, an account, and per-call cost. Coverage and legal terms vary by jurisdiction, and some providers restrict use cases.
- **Design:** a `PlateLookupProvider` interface (`lookup(plate, region) → {vin?, year?, make?, model?, confidence, raw}`) with provider adapters configured by name, base URL, and API key. **No provider is bundled or endorsed**; the operator supplies credentials.
- **Guardrails, because this one spends money and sends data off-box:**
  1. Off by default; enabling requires entering a key and acknowledging that plate data leaves the network.
  2. Never automatic — only a user-pressed **Look up by plate**, one call per press, never on page load, never on a background refresh.
  3. A per-call confirmation showing the running monthly call count and the configured cost-per-call estimate.
  4. Optional monthly call cap that hard-stops at the limit.
  5. Every call recorded in the integration activity log with the plate queried and the requesting user.
- **Result handling:** a returned VIN is fed into the §8.1 decode path rather than trusted for vehicle attributes directly. Provider results are marked `decode_source = plate_provider` with confidence.

> **Asked and answered: can NHTSA's plate lookup be used instead?** The recalls
> site at `nhtsa.gov/recalls` does accept a state and plate and return a VIN, so
> it looks like the free option this section says does not exist. It is not one.
>
> - `api.nhtsa.gov` — the documented public API, which serves recalls,
>   complaints, ratings and vPIC — exposes **no plate endpoint**. Every
>   candidate path answers `{"message": "Missing Authentication Token"}`, which
>   is AWS API Gateway's response for a route that is not there.
> - The plate box is a **website feature**, and the site sits behind bot
>   protection that returns 403 to any non-interactive client — verified, with
>   and without a browser user agent.
>
> Using it would mean defeating that protection, from every self-hosted
> instance, against a government site. FR-INT-10 already refuses to crawl or
> background-fetch a service-information provider; the same reasoning settles
> this. The premise stands: there is no free, legal, general-purpose
> plate-to-VIN API.
>
> Nothing needs redesigning if that changes. `PlateLookupProvider` is an
> interface with adapters configured by name, base URL and key — a published
> NHTSA endpoint would be one more adapter and a default, not a rewrite.

### 8.3 OBD-II / scan tool import

Three paths, because the container cannot reach the operator's hardware and because real scan tools emit reports for humans, not APIs.

#### (a) PDF report import — primary path (XTOOL D8 and similar)

Consumer and prosumer scan tools (XTOOL, Autel, Launch, Topdon) export a **PDF report** as their sharing format. There is no API and no structured export; the PDF *is* the interchange format. This path is therefore first-class, not a fallback.

**Pipeline.**

```
upload PDF ─► media (raw, retained forever)
              │
              ▼
        diagnostics.parse job
              │
              ├─ text-layer extraction  (generated PDFs — expected for the D8)
              └─ OCR fallback           (image-only PDFs; reuses the §7.9 OCR worker)
              │
              ▼
        profile detection (fingerprint match)
              │
              ├─ matched   ─► field + table extraction ─► draft session
              └─ unmatched ─► manual mapping wizard    ─► draft session (+ optional new profile)
              │
              ▼
        OPERATOR REVIEW  (extracted values beside the rendered page)
              │
              ▼
        commit ─► diagnostic_session + diagnostic_code rows
```

**Parser profiles.** A profile is declarative data, not code — a `parser_profile` row (see §6.2) carrying a fingerprint, field extractors, and a DTC table extractor. Profiles are versioned, exportable as YAML, and shippable as seed data. Adding support for a new tool is authoring a profile and a test fixture, never a code change. [SCHEMA-PARSER-PROFILES.md](SCHEMA-PARSER-PROFILES.md) gives the profile schema and the XTOOL D8 skeleton.

| Requirement | Rationale |
| --- | --- |
| The raw PDF is stored permanently as `media`, and the extracted plain text is stored on the session. | A profile improvement six months from now can **re-parse every historical report** without the operator re-uploading anything. Same principle as retaining the raw vPIC response (§8.1). |
| Extraction **never auto-commits**. A draft session goes to an operator review screen showing each extracted field beside the source page, with per-field confidence. | Regex-and-OCR extraction is fallible, and a misread VIN or odometer silently poisons the vehicle record and every cost-per-mile figure derived from it. |
| An unmatched PDF still produces a usable session via the manual mapping wizard. | The scaffold is useful **before** the D8 profile exists. The operator can paste values in, and optionally save the mapping as a new profile — learn-from-example. |
| Every session records `parser_profile_id` and `parser_version`. | Makes re-parse targeting and regression triage possible. |

**XTOOL D8 status: built** *(was: scaffolded, blocked on a sample report)*. Nine real reports arrived, and the profile is written against them — as a built-in parser rather than declarative rules, for the reasons in §15.1. The pipeline, review UI, re-parse and fixture harness are all in place. The paragraph below is kept as written because its reasoning held: the profile could not have been written from guesswork — page layout, field labels, and the DTC table shape are unknown until a real file exists. Drop reports into `Artifacts/samples/scan-reports/` (see the README there); each sample becomes a golden test fixture with an expected-output JSON alongside it, and profile development is test-driven from that corpus. Two or three reports covering *no codes found*, *several stored codes*, and *codes plus freeze frame* are enough to write a solid profile; a single sample tends to overfit.

#### (a2) Photographed printouts — bench testers

Plenty of shop equipment prints paper and nothing else: a battery tester, a charging-system tester, a compression tester. There is no PDF to export and no app. What the operator has ten seconds later is a phone photo of a curled thermal receipt, which §7.9 has always said is read by OCR — and which, until this was built, was read by OCR into nothing at all.

**Three things had to be true before a parser was worth writing.**

| Requirement | Rationale |
| --- | --- |
| Every image is rotated by its own EXIF orientation before OCR. | A phone held sideways writes pixels in sensor order and records the rotation as metadata. All five sample photographs are orientation 6. Tesseract returns *no* text from a page rotated ninety degrees — not poor text — and the media pipeline recorded that as a successful OCR with empty output, so the one format that most needed reading was the one that could not be read, and nothing said why. |
| Uneven lighting is removed per-region, not per-image. | Global autocontrast has one curve for the whole frame and a strip of paper photographed on a bench is bright at one end and shadowed at the other, so the curve is wrong at both. Subtracting a heavily blurred copy of the picture from itself gives every part of the page its own black and white points. Measured over the five samples against the values a person can read off the paper: **73 of 86 without it, 84 with**, and the seven that appear only with it are the whole second half of one photograph. |
| OCR returns **words with positions and confidence**, not a string. | The geometry is the only thing joining a label to its value: the receipt prints `HEALTH:` at the left margin and `79%` at the right. It was being recognized and thrown away, which is why an image was the one format where a built-in parser had nothing to stand on. Kept on the session as `extracted_words`, so a parser written next year can re-read a photograph uploaded today by its columns. |

**Re-parse re-reads the pixels.** For every other format the stored extraction *is* the report — a PDF's word geometry is lossless and extracting it again gives the same words — but a photograph's words are whatever OCR made of it on the day. An improvement to the image pipeline is worth nothing to the reports already uploaded unless re-reading means re-reading the picture, so a photo session with its original still in storage runs OCR again and keeps the better reading.

**TOPDON BT600 Plus status: built**, as a built-in parser rather than declarative rules, for reasons that are worth stating because they are not the D8's:

* One photograph can hold **more than one report**, each with its own clock. Print order is not time order — on the one two-result sample, the cranking test is printed *above* a charging test taken forty-two seconds earlier — so the session is dated by the latest result, not the first.
* The receipt **draws its own values**. Two bar graphs and a voltage trace sit between the labels, and the trace's axis ticks read back as `24U 12U 0U`: three numbers with a voltage unit beside them that are not measurements of anything. Nothing here reads a number off a graph.
* **Repair is for numbers, and only where nothing cleaner is available.** `O`→`0` and `S`→`5` apply to a field the label says is numeric, never to a verdict — `GOOD` has a `G` in it and `G` is one of the characters that comes back as a `6`. Where a clean run of digits and a repairable one both exist the clean one wins: OCR reads `850CCA(CCA)` as `B850CCA(CCA)` and repairing the `B` gives 8850, nine times the battery's capacity.
* **A value that cannot be read is shown, not dropped and not guessed.** Out of its own range, or unreadable, leaves the value empty with the characters it was read from beside it and a warning saying which.

**What the tester measured and what it was told are shown apart.** Before a battery test the operator keys in the capacity, rating standard and chemistry printed on the battery's own label, and the slip prints them back beside the measurements. `MEASURED: 755CCA` against `RATED: 850CCA` is the entire result, and only the first is a measurement — so a value the tester was given is marked as such and grouped separately, or a reader takes a number somebody typed for something the instrument found. Those values are also shown read-only where they are not numbers: a battery chemistry is a word from the tester's vocabulary, and the correction path accepts readings and the clock only. **A box whose contents are discarded on submit is worse than no box**, which is how the rule got written.

**What a tool's reports can contain is declared, not inferred.** A `reports` list on the profile names which of `codes`, `live_data`, `readiness` and `test_results` a report from that tool can hold. A battery tester's says `[test_results]`, so its session page has no trouble-code section and its line in a vehicle's scan history says `GOOD BATTERY` rather than `0 codes` — which was not a result, it was the absence of a thing that tool cannot produce. Inferring it from an empty list would have been wrong in the other direction just as often: a scan tool that found no codes is the best outcome available and needs saying out loud. **Empty means undeclared**, because every profile in the catalog predates the field, and reading that as "reports nothing" would hide what those profiles do read.

**A correction reaches the session.** `extraction` is never edited, and the review screen used to show only that — so a reader who retyped a misread clock, saved it, and came back found the misreading still presented as the reading. The correction had been kept; the page never mentioned it. The evidence table now strikes a superseded value and names what replaced it, and a corrected clock re-dates the session by the latest test on the strip.

**What it does not do, recorded rather than glossed.** One sample prints its timestamp across the tear-off perforation and the hour's second digit is physically overprinted. A person reading the paper makes it `18:37:59`; Tesseract makes it `19:37:59` at word confidence 89, against 95 for the date printed beside it. There is no signal in the OCR output to act on, and the parser does not invent one — see §19 for what was tried. What makes it survivable is the rest of the design rather than a rule: the session stays a draft, the field is editable, and the crop of the paper the value came off is shown beside it.

#### (b) Structured file import

Where a tool can emit CSV, JSON, or plain text, import it directly via a **column-mapping wizard** that saves a mapping per tool. Cheaper and more reliable than PDF parsing whenever it is available — so the UI offers it first if the tool supports it.

#### (c) Direct ELM327 adapter (Chromium only)

**Web Serial** (USB) or **Web Bluetooth** (BLE) — the *browser* talks to the adapter and posts results to the server. This is deliberate: the app runs in a container with no access to host USB or Bluetooth, and the phone in the garage is the device actually near the car. Requires a secure context (C-1) and a Chromium browser (C-4); the UI must state the requirement rather than failing mysteriously. Read-only operation: read DTCs, freeze frame, readiness, VIN (mode 09), and live PIDs for a snapshot. **Clearing codes is supported but requires a confirmation that names the vehicle and warns that readiness monitors will reset** — clearing before an emissions test is a genuine, expensive mistake.

**Code dictionary.** A bundled offline dictionary of generic SAE J2012 codes (P0/P2/P34xx, plus generic B/C/U ranges) supplies descriptions with no network access. Manufacturer-specific codes (P1xxx and friends) are **not** bundled — no free comprehensive source exists. They are stored with the operator's own description, and those descriptions are reused instance-wide for the same make.

**Workflow value:** a code can be promoted directly into a work order (code → complaint), and closing that work order marks the code `addressed`. A code recurring after being addressed is flagged `recurring` — the fix did not hold, which is exactly what you want to know a year later.

### 8.4 Recalls, bulletins, and OEM schedules

**Recalls — NHTSA (free).** The free NHTSA recalls API is queried **by year/make/model**, not by VIN. The practical consequences must be honored in the UI:

1. Results are campaigns that *may* apply to the vehicle, not confirmed open recalls for that specific VIN.
2. **VIN-level completion status is not available from a free API.** `owner_status` is therefore operator-maintained: the app links out to the manufacturer's or NHTSA's VIN lookup and lets the operator record what it said, with a date.
3. A `recalls.poll` job refreshes per active vehicle at most weekly, is disabled in Offline Mode, and surfaces new campaigns as dashboard items.

Presenting a scraped guess as "your recall status" would be worse than useless on a safety matter — the spec chooses an honest, slightly manual flow instead.

> **Measured quirk, and it bites exactly where it matters.** NHTSA answers a
> vehicle that has **no campaigns** with `HTTP 400` and a body reading
> `{"Count": 0, "Message": "Results returned successfully", "results": []}`. It
> answers a **rate-limited** request with the same status and the same body.
> Measured on a 2020 Subaru Outback: 400 with nothing inside a burst of
> requests, 200 with six campaigns after a pause — same query, same casing.
>
> Two consequences, both implemented:
>
> 1. Reading only the status turns "this vehicle is clear" into "the recall
>    service is down". That was the original bug, and it made recalls appear
>    broken on any vehicle with nothing against it.
> 2. Reading only the body turns a rate limit into a clean bill of health,
>    which is the same bug pointed the dangerous way. So an empty `400` is
>    **retried once**, and if it stays empty it is reported as *inconclusive* —
>    never as "no recalls". §8.4's rule that an empty list must not be mistaken
>    for a clear vehicle turns out to have a sharper edge than it first
>    appeared.

**Coverage is US-only, and that matters now (OQ-14).** NHTSA covers US-market vehicles. Canada's recall database (Transport Canada) and Mexico's (PROFECO) are separate systems with separate interfaces, and neither is wired up here. The provider interface accommodates them, but v1 ships the NHTSA adapter alone; a Canadian or Mexican operator sees recalls as unavailable-for-region rather than as a silent absence — an empty recall list must never be mistaken for a clean vehicle.

**Bulletins (TSBs).** No free bulk source exists. Manual entry plus a document attachment plus a CSV import path; the adapter interface is defined so a commercial source can be added later without a schema change.

**Maintenance schedules.** No free OEM schedule API exists either. Three supported sources, in order of practicality: (1) bundled generic templates; (2) manual entry from the owner's manual — with the manual attached as media; (3) YAML/JSON template import/export for sharing between instances (`maintenance/templatelib.py`), and (4) the shared catalog built on it (§8.1b). Source (3) was promised here and unimplemented for schedule templates until R-1 went looking for it.

### 8.5 Service information libraries (link-out)

NG-5 stands: the application is **not** a repair-procedure database. It does not host, mirror, scrape, or crawl service information. What it does is put the right *link* one tap away from the work order you already have open, so the lookup that currently means "find the laptop, remember the site, re-navigate the make/year/model tree" becomes a single tap on the vehicle.

**Model.** A `service_info_provider` is configuration (name, base URLs, URL template, auth note, enabled flag), and a `vehicle_service_info_link` pins a **resolved** URL to a specific vehicle. Providers ship as seed data; the operator may add their own.

**Why pinning matters — and it is the central design decision here.** These libraries index vehicles by a catalog string, not by a computable key. CHARM's structure is:

```
https://charm.li/{Make}/{Year}/{Model Trim Body Engine}/
https://charm.li/Honda/2000/Accord%20DX%20Sedan%20L4-2254cc%202.3L%20SOHC%20MFI/
```

That trailing segment — model, trim, body style, and full engine spec fused into one URL-encoded string — **cannot be derived from a VIN decode.** Generating it would produce dead links. So the flow is **resolve once, pin forever**:

1. The app deep-links as far as the pattern is deterministic — `/{Make}/{Year}/` — which lands the operator on the short list of variants for that vehicle.
2. The operator picks the right variant and pins it with **Save this link** (paste the URL, or a bookmarklet/share-target on mobile).
3. Every future visit to that vehicle is one tap. Pinned links are per-vehicle data, so they survive, export, and back up with everything else.

**Seeded providers.**

| Provider | URL | Access | Notes |
| --- | --- | --- | --- |
| **LEMON Manuals** | `lemon-manuals.la` (mirrors: `lemon-manuals.org.ua`, and a third domain) | Free, no sign-up | The **default**. Successor to Operation CHARM and a superset of it — roughly 10,000 US/Canada-market vehicles, 1960–2025. Provider config must support a **mirror list with failover**, since the project deliberately runs across several jurisdictions and any one domain may be unreachable. |
| **Operation CHARM** | `charm.li` | Free, no sign-up | Predecessor, coverage through ~2014, still up and stable. Retained as a fallback and because its URL structure is known-good. |
| **ALLDATA DIY** | `alldatadiy.com` | **Paid**, per-vehicle annual subscription | Link-out to the site's entry point only. Deep-linking to a specific procedure requires an authenticated session, so the app does **not** attempt to construct procedure URLs; the pin holds whatever URL the operator saves after logging in themselves. No credentials are ever stored by HomeAutoShop. |

> **Correction to the source request:** the LEMON domain is `lemon-manuals.**la**` (Laos), not `.ls`. Verified against charm.li's own successor notice and the site itself.

**Rules.**

- Links open in a new tab with `rel="noopener noreferrer"` and a referrer policy that leaks no internal URL.
- Providers are subject to **Offline Mode** (NFR-S-2): when it is on, pinned links render as disabled with their URL shown as copyable text, rather than appearing broken.
- No crawler, no bulk download, no mirroring — the app makes **zero** background requests to these sites. It only ever renders a link a human clicks. (The operator remains free to attach documents they obtained themselves as ordinary media under §7.9.)
- A provider's reachability is never assumed; a pinned link that 404s is the operator's to re-pin, and the UI offers **Re-resolve** rather than silently failing.
- **Providers are show/hide-able globally and per vehicle (OQ-11).** This matters specifically because ALLDATA DIY subscriptions are **per vehicle**: the operator subscribes for the truck and the wife's car but not the project car. A `vehicle_service_info_link` therefore carries `subscription_status` (`subscribed` | `not_subscribed` | `unknown`) and `subscription_expires_on`, so ALLDATA appears on the two vehicles it is paid for and stays out of the way on the rest — and an expiring subscription surfaces alongside registration renewals on the dashboard, since it is the same kind of fact.

### 8.6 LubeLogger (existing self-hosted instance)

> **Detailed in [INTEGRATION-LUBELOGGER.md](INTEGRATION-LUBELOGGER.md)** — modes, verified API facts, the full entity mapping, and sync semantics. This section is the summary the rest of this document relies on.

[LubeLogger](https://lubelogger.com) is a mature self-hosted vehicle maintenance and fuel tracker with a documented REST API. Where an operator already runs one, it already holds real history — which is what makes importing it worth doing.

> **Scope discipline (OQ-9). LubeLogger is optional and additive, never a dependency.** HomeAutoShop owns the maintenance schedule (§7.7) and every core function outright. An instance with no LubeLogger configured is not a degraded instance, and no feature, report, or dashboard may be built such that it needs LubeLogger to be correct. Anything that starts to feel load-bearing is scope creep and gets cut.

**Posture.** Four modes are specified; the recommendation is a **one-time import** of existing history (Phase 2), with **scheduled pull sync** as an optional later addition (Phase 4) and bidirectional sync explicitly out of scope. Every import is idempotent via `external_ref`, dry-run by default, never clobbers a local edit, and never propagates a deletion.

**Consequence for cost-per-mile (FR-COST-3):** since fuel is permanently out of scope (OQ-3/NG-7) and LubeLogger cannot be a dependency, FR-COST-3 measures **repair and ownership cost per distance, excluding fuel, by design** — stated plainly in the report rather than quietly omitted, and arguably the more useful number for a repair system.

**One hazard worth carrying in the base spec:** LubeLogger returns **locale-formatted strings** by default. The adapter must send the `culture-invariant` header, and the connection test must refuse to import without it. Skipping this silently mis-parses decimals and dates — a `1.234,56` fuel cost imported as `1.23` is a bug nobody ever notices.

### 8.7 WrenchLedger (tool inventory)

> **Detailed in a separate document: [INTEGRATION-WRENCHLEDGER.md](INTEGRATION-WRENCHLEDGER.md).** It is a full integration contract against another product's shipped API, and keeping it here would bury the base spec under material that changes on someone else's release schedule. This section is the summary the rest of this document relies on.

[WrenchLedger](https://wrench-ledger.app) is a cloud-hosted SaaS workshop tool inventory by the same developer — what you own, where it lives, what it is worth, who borrowed it — with nested locations, kits, lending, service schedules, meters, and warranty records.

Per NG-8/NG-9, HomeAutoShop builds none of that. **WrenchLedger tracks the tools you work with; HomeAutoShop tracks the machines you work on** (§7.1a states the rule for the ambiguous middle, such as a generator).

**What the integration delivers.** One feature justifies it: the **readiness gate**. You plan a brake job for Saturday; the breaker bar is on loan to a neighbor and the torque wrench is due for calibration. Today you find out on Saturday morning. With the integration, the work order says so on Wednesday — surfaced beside `waiting_on_parts` on the planning dashboard (FR-REP-1), as a warning and never a block.

**Requirements on the HomeAutoShop side.**

| ID | Requirement |
| --- | --- |
| FR-WL-1 | Configure a connection with an API key and verify it, **distinguishing a bad credential from a WrenchLedger plan that does not include API access** — those need different messages and different next steps. |
| FR-WL-2 | Reference tools from a `job_item` by id — a lightweight `tool_ref`, never a copy of the tool record. |
| FR-WL-3 | Show live tool availability on a work order: on hand, on loan (to whom, due when), under repair, missing, or calibration due. |
| FR-WL-4 | Warn before starting or planning a job whose tools are unavailable, and surface it on the planning dashboard. |
| FR-WL-5 | **Synchronize by polling.** WrenchLedger delivers events by outbound webhook, which cannot reach a LAN-only instance — so pull is the default and the signed webhook receiver is an opt-in upgrade for operators who already expose their instance. |
| FR-WL-6 | *(MAY, opt-in)* Mirror work orders as WrenchLedger projects, record tool custody for the duration of a job, and post meter increments back on completion. |
| FR-WL-7 | Degrade completely cleanly. With the integration absent, broken, unsubscribed, or disabled, HomeAutoShop is a **complete and correct** application — only less convenient. |
| FR-WL-8 | Cache only id, display name, availability, due dates, and a checked-at timestamp. Storage locations, valuations, serial numbers, photos, and **borrower contact details are never mirrored** — the scope granting contact details is deliberately never requested. |
| FR-WL-11 | The cached tools have a screen of their own, searching WrenchLedger and the local shadow together — previously that search existed only inside a job item, so "do I own a vacuum pump?" had no answer anywhere. A tool **named by hand** on a job is marked as such, because nothing knows where it is and a blank availability column otherwise reads as a fault; it can be forgotten, taking its job-item references with it. A tool that came from WrenchLedger cannot be deleted here — the next sync would bring it back — and the refusal says where to remove it instead. |
| FR-WL-9 | Keep HomeAutoShop's own consumable tracking complete and standalone, and let the operator choose which system owns shop consumables when a connection exists — **basic shop functionality is never conditional on a paid third-party subscription**, and the setting exists to prevent double-counted cost. |
| FR-WL-10 | Present the house placement per the rules in the integration document — **static, bundled, zero network calls, dismissible, disclosed, and suppressed once connected**. NFR-S-1 admits no exception, including for first-party promotion. |

> **The sharpest tension in this spec.** WrenchLedger is cloud SaaS; HomeAutoShop is local-first (P-1). The resolution is that the integration is strictly additive: off by default, disabled by Offline Mode, host explicitly allowlisted (§12.3), and **nothing — no report, no dashboard, no work order state — may become incorrect in its absence.**

---

## 9. User experience

### 9.1 Information architecture

```
Dashboard        due & overdue · open work orders · waiting on parts · alerts
Vehicles         list → detail: Overview · History · Schedule · Specs · Components ·
                               Inspections · Parts · Documents · Costs · Recalls ·
                               Diagnostics · Manuals · Fluid analysis
Work Orders      board (by status) · list · detail (budget burn-down where one is set)
Parts            catalog · inventory by location · restock · outstanding cores
Purchases        orders · vendors · returns
People           owners & contacts
Equipment        generators · mowers · small engines — same machinery, no VIN or plate
Inspections      templates · run an inspection · history · measurement trends
Reports          vehicle report · inspection report · spend · time · inventory value · warranties
Settings         users · units · integrations · backup · export/import · health
```

### 9.2 The garage-first rule


**A form must not cost you your place.** Post-redirect-get is the foundation and stays: it works with no script, survives a dropped connection, and gives the back button an honest meaning. Its one cost is felt constantly — ticking the fourth job item on a long work order reloads the page and lands the reader at the top, hunting for the row they were on, which on a phone held in one hand is the whole interaction. Two layers answer it. **With no script**, a form inside a marked region carries the region's name and any same-page redirect gains it as a fragment, so the browser lands where the form was. **With script**, the post is made in the background and only that region is replaced, so nothing navigates at all — and the region is taken from the *same full page the server already returns*, so the enhanced and unenhanced paths render identical HTML and cannot drift apart. Anything that genuinely leaves the page is detected and followed as an ordinary navigation.
The phone/tablet UI is the primary interface for capture; the desktop is the primary interface for planning and reporting. Concretely:


**Columns are placed, not balanced.** A two-column page puts the work in the main column and the reference beside it, and where that came out lopsided the fix is to *move a named card*, never to let the layout reflow by height: a page whose shape depends on how much data it happens to hold is a page nobody can learn. Long reference blocks that are read once and rarely again — a full VIN decode is forty rows — are folded into a native `<details>` rather than moved, which keeps them where they belong and costs the column nothing.
- **Touch targets ≥ 48 px**, generous spacing, no hover-dependent affordances. The user may be wearing nitrile gloves with brake dust on them.
- **A dark, high-contrast theme by default on mobile.** Garages are dim, and a white screen at night is blinding.
- **One-thumb reach:** primary actions sit in the lower third of the screen.
- **Camera is one tap from any work order**, with multi-shot capture and no mandatory metadata — a photo with no caption is a complete, valid record.
- **Voice-to-text on every note field** (browser speech API where available) — dictating "left front bearing has play, ordered a Timken" beats typing it with one clean finger.
- **The sync indicator is always visible** and honest about queued writes (§5.4).
- **Quick-add sheet** reachable from anywhere: odometer, photo, note, time entry, part used.

### 9.3 Key screens

- **Dashboard.** Answers "what needs attention?" in one screen. Ordered by urgency: overdue safety items, overdue routine, work orders blocked on parts, expiring registrations, then due-soon.
- **Vehicle detail.** Header: nickname, photo, year/make/model, current odometer, status, next due. Tabs as above. The **timeline** (FR-VEH-10) is the default tab — the vehicle's whole story in date order.
- **Work order detail — the workbench.** Three-C fields at top; job items as a checklist; parts, time, notes, and photos as append-only streams below; the vehicle's spec quick-reference in a collapsible side panel (FR-SPEC-4), with **pinned service-manual links sitting right beneath it** (FR-INT-8) — the two lookups that interrupt a job most often, both one tap away; live cost rollup in the header.
- **Inventory by location.** Mirrors the physical shop. Scan a bin's QR → see its contents; scan a part's UPC → find it or create it.
- **Vehicle report.** Print/PDF-oriented layout: identity block, ownership, complete dated service history with parts and odometer, cost summary, attached receipts index.

### 9.4 PWA behavior

Installable (manifest, icons, standalone display), service worker precaching the shell and runtime-caching API reads per §5.4, offline landing that shows cached content rather than an error page, background sync on reconnect, and web push for reminders where the browser supports it and the operator has opted in.

### 9.5 Accessibility

WCAG 2.1 AA as the target: 4.5:1 contrast, full keyboard operability, visible focus, correct labels and roles, no color-only status encoding (the due/overdue states carry icon and text, not just red/green), and respect for `prefers-reduced-motion`.

---

## 10. API

- **Style:** REST/JSON at `/api/v1`, plural nouns, cursor pagination (`?cursor=&limit=`), consistent envelopes, `application/problem+json` errors (RFC 9457) with a stable `type` per error class.
- **Auth:** session cookie (`HttpOnly`, `Secure`, `SameSite=Lax`) for browsers; `Authorization: Bearer <token>` for API tokens. CSRF double-submit token on cookie-authenticated unsafe methods.
- **Concurrency:** mutable resources return `revision`; unsafe requests must send `If-Match: <revision>` or a `revision` field, and a mismatch yields `409` with the current representation (§5.4).
- **Idempotency:** creates accept a client-supplied `id` (UUIDv7); replaying a create returns `200` with the existing resource rather than `409`.
- **Bulk sync:** `POST /api/v1/sync/batch` accepts an ordered array of queued mutations and returns per-item results, so a reconnecting client makes one round trip rather than fifty.
- **Discovery:** OpenAPI 3.1 document at `/api/v1/openapi.json`, browsable docs at `/api/docs`.

Representative endpoints:

```
GET    /vehicles                     ?status=&owner=&q=&cursor=
POST   /vehicles
GET    /vehicles/{id}
PATCH  /vehicles/{id}                If-Match: revision
GET    /vehicles/{id}/timeline
GET    /vehicles/{id}/service-items
POST   /vehicles/{id}/odometer-readings
GET    /vehicles/{id}/report.pdf

POST   /vin/validate                 local only, no network
POST   /vin/decode                   explicit; provider call
POST   /plate/lookup                 explicit; paid provider; confirmation required

GET    /work-orders                  ?status=&vehicle=&assignee=
POST   /work-orders
POST   /work-orders/{id}/notes       append-only
POST   /work-orders/{id}/time-entries
POST   /work-orders/{id}/parts
POST   /work-orders/{id}/complete    body: odometer_out

GET    /parts?q=                     name | number | crossref | UPC
GET    /parts/{id}/fitment
GET    /inventory?location=&low=true
POST   /inventory/transactions       append-only ledger

GET    /purchases                    POST /purchases/{id}/receive
GET    /purchases/cores/outstanding

POST   /media                        presigned upload → confirm
POST   /media/{id}/links

POST   /diagnostics/sessions         PDF report, file import, or ELM327 payload
GET    /diagnostics/sessions?review=draft
POST   /diagnostics/sessions/{id}/confirm    commit a reviewed extraction
POST   /diagnostics/sessions/{id}/reparse    re-run against a newer profile
GET    /diagnostics/codes?status=open
GET    /parser-profiles                POST /parser-profiles/import   (YAML)

GET    /vehicles/{id}/service-info-links
POST   /vehicles/{id}/service-info-links     pin a resolved URL
GET    /service-info-providers

POST   /integrations/lubelogger/test         reachability, scope, invariant check
POST   /integrations/lubelogger/import       ?dry_run=true (default)
GET    /integrations/lubelogger/status       last sync, counts, conflicts
GET    /integrations/activity                outbound call log (FR-INT-2)

GET    /inspection-templates            POST /inspection-templates/import  (YAML)
POST   /vehicles/{id}/inspections       start from a template (snapshots it)
PATCH  /inspections/{id}/results/{rid}  offline-queued, positional
POST   /inspections/{id}/complete
POST   /inspections/{id}/convert        attention/fail results -> job items
GET    /inspections/{id}/report.pdf
GET    /vehicles/{id}/measurements?point=  trend series for charting

GET    /vehicles/{id}/components?active=true
POST   /vehicles/{id}/components        install
POST   /components/{id}/rotate          position change, not removal
POST   /components/{id}/remove          with reason

GET    /reports/warranties?active=true
GET    /reports/spend?from=&to=&group_by=
GET    /search?q=

POST   /admin/backup/run             GET /admin/health
POST   /admin/export                 → job → download
```

---

## 11. Non-functional requirements

### 11.1 Scale targets (design point, not a limit)

| Dimension | Target |
| --- | --- |
| Users | ≤ 10, ≤ 3 concurrent |
| Vehicles | ≤ 50 |
| Work orders | ≤ 10,000 |
| Parts / stock lots | ≤ 20,000 / ≤ 50,000 |
| Media objects | ≤ 100,000, ≤ 250 GB |
| Relational data | < 2 GB |

### 11.2 Performance

| ID | Requirement |
| --- | --- |
| NFR-P-1 | p95 API read < 200 ms at scale targets, measured on a 4-core / 8 GB host with the DB on SSD. |
| NFR-P-2 | Global search p95 < 300 ms. |
| NFR-P-3 | First contentful paint < 1.5 s on LAN; interactive < 2.5 s on a mid-range phone. |
| NFR-P-4 | Photo upload returns as soon as the object is stored; derivatives are async (FR-DOC-3). |
| NFR-P-5 | Cold `docker compose up` to serving < 30 s, migrations included. |
| NFR-P-6 | Idle footprint < 900 MB RSS across all four containers, or < 600 MB on the `slim` profile. **Raised from 700 MB (OQ-13)** — the Python choice (§5.7) makes the original figure unreachable, and an NFR nobody can meet is worse than an honest one. Dropping the object store (§5.1) gave back ~180 MB of it; the figure is not lowered again, because the headroom is what the `s3` driver and a busier worker will spend. |

### 11.3 Reliability and operability

| ID | Requirement |
| --- | --- |
| NFR-R-1 | Every container defines a healthcheck; `app` exposes `/healthz` (liveness) and `/readyz` (DB + storage reachable). |
| NFR-R-2 | Jobs are idempotent and retried with backoff; permanent failures land in a dead-letter state visible in admin. |
| NFR-R-3 | Structured JSON logs with a request ID; no secrets or full VINs at info level. |
| NFR-R-4 | `/metrics` in Prometheus format *(SHOULD)*, off by default. |
| NFR-R-5 | Schema migrations are forward-only, transactional, and tested against the previous release's data. |
| NFR-R-6 | The app starts and serves read-only with a clear banner if blob storage is unreachable — **a storage outage must not hide the service history**. Moot on the default `filesystem` driver, where media shares its fate with the application; it is the `s3` driver, where the store is a separate machine that can be down on its own, that this defends. |

### 11.4 Maintainability

Automated tests covering: domain rules (interval math, FIFO costing, VIN check digit, unit conversion round-trips), API contract, sync/conflict behavior, and a migration test. Seed and demo datasets ship with the repo. Every requirement ID in §7 is traceable to at least one test.

---

## 12. Security and privacy

### 12.1 Threat model

In scope: a curious housemate, a compromised device on the home LAN, an unencrypted backup on a lost drive, accidental exposure of the port to the internet, and a malicious file upload. Out of scope: a targeted attacker with physical access to the host, and nation-state adversaries. The instance is assumed to sit on a home LAN behind NAT.

### 12.2 Authentication and authorization

- Argon2id password hashing with sensible parameters; a minimum length rather than composition rules.
- Optional TOTP second factor per user.
- Login rate limiting with progressive delay and lockout; generic failure messages.
- Sessions: server-side, `HttpOnly`/`Secure`/`SameSite=Lax`, sliding 30-day expiry, revocable per-device from settings.
- API tokens: shown once, stored hashed, scoped, expirable.
- **Two roles ship in v1 — `admin` and `member`** — but the **seams for a third are built now** (OQ-7). `admin` manages users, integrations, backups, and settings; `member` does everything else.
- **Three roles: `admin`, `member`, and `helper`** (§12.2a). The helper scaffolding this section originally described — `can(user, action, resource)` plus an `asset_access` table with implied-allow for `member` — was built in v1 and its central claim, that adding the role later would be "populating a table and adding policy rules, not auditing every view", **turned out to be false**. It held for admin-versus-member, where the decision is a property of the screen. It did not hold for per-vehicle, where the decision is a property of the object: only 19 of 225 view functions ever named a resource. The correction, and what replaced the assumption, is §12.2a.

### 12.2a The `helper` role — per-vehicle access (R-2)

A helper is somebody you let work on one vehicle: a friend borrowing the lift, a son-in-law doing his own brakes in your garage, a neighbor you are teaching. They see that vehicle and everything about it, they record what they did, and they see nothing else.

**What a helper gets.** The vehicles named in `asset_access` and everything scoped to them — history, work orders, job items, time, notes, photos, inspections, diagnostics, the maintenance schedule. The parts catalog is **readable**, because a helper has to be able to say which filter they fitted, and not writable, because that is the shop's parts list rather than theirs. `level` on the grant distinguishes read from write on the vehicle itself.

**What a helper never gets, and why each is deliberate.** Costs and vehicle reports — a grant is permission to work on a truck, not to see what it has cost its owner. Inventory, lots, locations and kits — the shelf is the shop's. Purchasing, suppliers and receipts. The address book. Shop reports and CSV exports. Users, settings, integrations, backups, trash. Sensitive specs are withheld even on their own vehicle: the key code, the radio code and where the wheel-lock key lives are exactly what `is_sensitive` already marks and what a vehicle report already excludes (C-5), so somebody let into the garage is not thereby let into the glovebox.

**Search is treated as a back door and closed as one.** A helper barred from a vehicle's page who could still find its work orders by typing its name has not been barred from anything, so the narrowing happens on the querysets inside `search()`, and the groups a helper has no business in — people, documents — are not searched at all.

#### Why this is a request gate and not more call sites

§12.2 promised that routing every decision through `can(user, action, resource)` would make this role "populating a table and adding policy rules, not auditing every view in the codebase". **That promise did not hold, and the shape of the failure is worth recording**: of 225 view functions, 48 called `require()` and only 19 of those named a resource — every one of them in `assets/views.py`. The apps where a helper actually works had none at all: `work` 31 views and no checks, `parts` 34 and none, `diagnostics` 22 and none, `maintenance` 9 and none.

The scaffold was real for admin-versus-member, where the decision is a property of the *screen*, and absent for per-vehicle, where it is a property of the *object*. Nothing was written down wrongly; the seam simply depended on 225 separate acts of remembering, and a boundary maintained that way is not a boundary but a hope with good documentation.

So the outer fence is enforced **once, on every request**, against an allow-list of URL names (`accounts/middleware.py`, `accounts/policy.py`). A helper reaching a screen not on that list is refused before the view runs. The consequences are the point:

- **A new screen is closed to helpers on the day it is written.** Opening one is a deliberate line in `policy.py`, not an omission nobody notices.
- **The failure direction is safe.** Forgetting to classify a route denies a helper access they should have had, which is a complaint. The old arrangement failed the other way.
- **`can()` is still the policy layer.** The gate decides *which screens*; `require(user, action, object)` inside allow-listed views decides *which vehicle*, walking the relation to the asset so that a view holding a job item does not need to know that its permission really belongs to the work order's vehicle. Both are needed: every vehicle screen is on the allow-list, so without the object check an id in the URL would be enough.

**The inner half was stated here before it was true.** The paragraph above has said since it was written that allow-listed views call `require(user, action, object)` — and `maintenance` was one of the apps measured at "9 views and no checks" two paragraphs earlier, and stayed that way. Every schedule screen is on the allow-list, so until this was found a helper granted read on one vehicle could POST an interval change, a back-dated service, a snooze or a component onto **any** vehicle in the shop: the gate checked that they may reach *a* schedule, and nothing checked *whose*. The seven writes in `maintenance/views.py` now go through one `_vehicle()` helper that resolves the asset and requires against it, which is also the shape that makes the omission visible — a write that skips it does not look like the others. This is the same failure as the one this section was written about, one layer in, and it is recorded in §19 rather than quietly fixed, because it was a claim this document made about itself.

A companion test asserts that **every named route is either opened or written down as closed**, so adding a screen forces the decision that was never forced before. That test is not what protects the data — the gate already refuses anything unlisted — it is what stops the list drifting quietly out of view, which is exactly what happened the first time.

`visible_assets()` and `visible_assets_for()` are the single place a listing is narrowed, so *what can a helper see* has one answer rather than one per page.

### 12.3 Application security

Parameterized queries only; output encoding by default; strict CSP without `unsafe-inline`; upload validation by content sniffing rather than extension, with a size cap and rejection of active content; media served from a distinct origin or path with `Content-Disposition: attachment` and `X-Content-Type-Options: nosniff`; secrets from environment or file, never in the database in plaintext, never in logs.

**SSRF policy, stated precisely** — because one integration is deliberately on the LAN. Outbound requests are permitted only to an **allowlist derived from explicitly enabled, operator-configured integrations**. A configured integration host is allowed even when it resolves to a private range (a self-hosted LubeLogger on the LAN is the motivating case, §8.6); the allowlist is the control, not the address range. Everything else — user-supplied URLs, values pulled from imported data, redirect targets — is blocked from private and link-local ranges, and redirects are not followed across hosts. Pinned service-information URLs (§8.5) are rendered as links for the browser to follow and are **never fetched server-side**, so they need no allowlist entry.

### 12.4 Privacy and data sovereignty

| ID | Requirement |
| --- | --- |
| NFR-S-1 | **No telemetry, no analytics, no phone-home.** Ever. Including update checks unless explicitly enabled. |
| NFR-S-2 | A single **Offline Mode** switch disables every outbound network call; the UI shows integration features as intentionally unavailable, not broken. |
| NFR-S-3 | Every outbound call is logged and reviewable by the operator, with endpoint, timestamp, user, and outcome. |
| NFR-S-4 | GPS EXIF stripped from images by default (FR-DOC-9) — a photo of a car in a driveway geotags a home address. |
| NFR-S-5 | VINs and plates are masked in logs and in the UI where not needed; full values require an explicit reveal. |
| NFR-S-6 | Backups may be encrypted with an operator-supplied passphrase (age/GPG), with a plain warning that a lost passphrase is unrecoverable. |
| NFR-S-7 | Credentials for external systems (LubeLogger API key, plate provider key) are stored encrypted at rest with a key from the environment, are write-only in the UI (never re-displayed), and are redacted from logs, exports, and error reports. |
| NFR-S-9 | The WrenchLedger house placement (§8.7.4) is static, bundled, dismissible, and makes **no network call of any kind** — no beacon, no pixel, no third-party ad SDK. NFR-S-1 admits no exception, including for first-party promotion. |
| NFR-S-8 | Service-information links are opened by the browser with `rel="noopener noreferrer"` and a referrer policy that discloses no internal URL. The operator is told, once, that clicking a link tells that site which vehicle they are researching. |

---

## 13. Backup, restore, and portability

### 13.1 Backup

A nightly `backup.run` job produces a timestamped set: `pg_dump` (custom format, compressed), a media manifest with checksums, and either a full or incremental media copy into `backup-data`. Retention is GFS-style (configurable, default: 7 daily, 4 weekly, 6 monthly). Every backup is verified — the dump is test-restorable and a sample of media checksums is validated — and the result recorded. **The dashboard shows backup age and warns loudly past 7 days** (FR-ADM-4): a backup system nobody looks at is not a backup system.

The media half of a backup is a copy of `MEDIA_ROOT`: everything under the default `filesystem` driver, and nothing under `s3` — a backup cannot reach into an object store it was only ever given an API client for. A gap like that is otherwise discovered at a restore, which is the one moment it cannot be closed, so it is stated three times: a warning in the log when the backup is taken, `"media": "external"` in the manifest, and a warning from `manage.py restore` before it does anything. An operator on `s3` backs that store up themselves.

Optional post-backup sync to an operator-configured target (rsync/rclone to a NAS, external drive, or off-site) is supported as a shell hook, not a bundled cloud client.

### 13.2 Restore

A single documented command restores database and media into an empty instance. The restore path is exercised in CI against the prior release's backup format. Documentation includes the full disaster-recovery runbook (scenario 7, §3.2), and an operator-facing **restore drill** reminder every 6 months *(SHOULD)*.

### 13.3 Portability (P-4)

`POST /admin/export` produces a ZIP containing: every table as newline-delimited JSON, all media in a `media/` tree named by `id` and original extension, a `manifest.json` with schema version and checksums, and a `README.md` describing the layout in prose. **The export must be usable without this application** — that is the point. A matching import path accepts the same ZIP, allowing instance migration and serving as an end-to-end test of the format.

Additionally: per-vehicle PDF/CSV export (FR-REP-2), CSV export on every report (FR-REP-4), and CSV import with column mapping (FR-ADM-6).

---

## 14. Configuration reference

Environment variables, with `_FILE` variants supported for secrets.

**Most of this table is now edited in the application, not in a file.** R-9 is
built: a value stored through the settings screen wins over the environment,
which in turn wins over the default. What remains environment-only is what
cannot live in the database — values read before the database is reachable, and
values a wrong answer would lock an operator out of the settings screen with.
§17.1 lists which is which.

| Variable | Default | Notes |
| --- | --- | --- |
| `SHOP_NAME` | `Home Shop` | Branding in UI and reports |
| `BASE_URL` | — | Required; must be `https://` for full function (C-1) |
| `TLS_MODE` | `internal` | `internal` \| `custom` \| `acme-dns` |
| `DATABASE_URL` | compose-provided | Postgres DSN |
| `STORAGE_DRIVER` | `filesystem` | `filesystem` \| `s3`. `filesystem` puts media under `MEDIA_ROOT`, where the backup finds it; `s3` is an object store the operator supplies. `manage.py migrate_storage` moves files between them without touching the database |
| `STORAGE_*` | unset | Endpoint, bucket, credentials. Read only when `STORAGE_DRIVER=s3`, and none of them has a working default — there is no bundled object store to point at |
| `STORAGE_PUBLIC_ENDPOINT` | unset | Only for an operator who has genuinely published their object store on an address a browser can reach. Blank — the default — means files are served by the application, which needs no second hostname, no second certificate and no exposed port, and makes reading a photo require a login |
| `UNITS` | `imperial` | `imperial` \| `metric`; per-user override |
| `CURRENCY` | `USD` | ISO 4217 |
| `TZ` | `UTC` | Instance display timezone |
| `OFFLINE_MODE` | `false` | Master outbound kill switch (NFR-S-2) |
| `VIN_DECODE_ENABLED` | `true` | NHTSA vPIC |
| `RECALLS_ENABLED` | `true` | NHTSA recalls |
| `PLATE_PROVIDER` | unset | Adapter name; blank disables |
| `PLATE_API_KEY` | unset | Operator-supplied |
| `PLATE_MONTHLY_CAP` | `0` | `0` = no cap |
| `OCR_ENABLED` | `true` | Local OCR in `worker`; also the fallback for image-only scan-tool PDFs. Off leaves media `pending`, not `failed`, so switching it back on has a backlog to work through (`media.ocr_sweep`) |
| `TESSERACT_LANGS` | `eng` | **Build argument**, passed by `docker-compose.yml` to both the image and `OCR_LANGUAGES`. One variable for both halves: a pack installed and never asked for is image weight, and a language asked for and never installed is a failure on a background job |
| `OCR_LANGUAGES` | `eng` | Set from `TESSERACT_LANGS`. Narrowed at run time to what the image actually has — Tesseract fails the whole call for one missing language, so a fourth added without a rebuild would otherwise take the other three down |
| `OCR_PDF_MAX_PAGES` | `20` | How far into an image-only PDF to read. A receipt is one page; a service manual is hundreds |
| `SCAN_IMPORT_ENABLED` | `true` | Scan-tool report import pipeline (§8.3) |
| `SERVICE_INFO_ENABLED` | `true` | Service-information link-out providers (§8.5) |
| `LOCALE_DEFAULT` | `en-US` | Instance default; users negotiate their own (§5.6) |
| `CURRENCY_REPORTING` | `USD` | Rollup/reporting currency; transactions keep their own |
| `COST_INCLUDE_TOOLING` | `false` | OQ-4 — include `tooling` expenses in per-vehicle cost |
| `REMINDERS_OWNER` | `homeautoshop` | OQ-10 — `homeautoshop` \| `lubelogger` |
| `VIN_OFFLINE_DATASET` | `false` | Set by the admin-triggered dataset download (§8.1) |
| `DVI_ENABLED` | `true` | Inspections module (§7.8) |
| `EQUIPMENT_ENABLED` | `true` | Equipment assets — generators, mowers (§7.1a) |
| `WRENCHLEDGER_URL` | unset | Tool inventory integration (§8.7); blank disables |
| `WRENCHLEDGER_API_KEY` | unset | Scoped key; read-mostly is sufficient |
| `WRENCHLEDGER_SYNC_HOURS` | `6` | How often the availability pull runs |
| `WRENCHLEDGER_WEBHOOK_SECRET` | *(not implemented)* | The signed inbound receiver is the opt-in upgrade for an exposed instance; pull is the default and is what ships (FR-WL-5) |
| `WRENCHLEDGER_PUSH_USAGE` | *(not implemented)* | Opt-in usage post-back (FR-WL-6). Gated on a WrenchLedger feature absent from the smaller plan |
| `CONSUMABLES_OWNER` | `homeautoshop` | `homeautoshop` \| `split` \| `wrenchledger` (FR-WL-9) |
| `SHOW_PRODUCT_LINKS` | `true` | House placement (§8.7.4); `false` removes it entirely |
| `SERVICE_INFO_DEFAULT` | `lemon` | `lemon` \| `charm` \| `alldata` \| custom provider name |
| `LUBELOGGER_URL` | unset | e.g. `https://lubelogger.home.arpa`; blank disables the integration |
| `LUBELOGGER_API_KEY` | unset | Sent as `x-api-key`; Viewer scope suffices for pull-only |
| `LUBELOGGER_MODE` | `pull` | `off` \| `import_once` \| `pull` \| `pull_push_odometer` |
| `LUBELOGGER_SYNC_HOURS` | `12` | Incremental pull interval. An interval rather than a cron expression: the worker asks what is due on each pass (§15.1), so there is no crontab to parse and no second scheduler to supervise |
| `SMTP_*` | unset | Reminder email; unset disables |
| ~~`PUSH_VAPID_*`~~ | *(not an environment setting)* | The VAPID pair is generated on first use and stored as a `setting`. It is this instance's identity to the push services and nothing more — losing it means devices re-subscribe, not a breach — so putting it in the environment would be ceremony without a benefit |
| `PLATE_LOOKUP_ENABLED` | `false` | §8.2. Off deliberately: every call costs money and sends a plate off-box |
| `PLATE_LOOKUP_PROVIDER` | `generic` | Selects the response mapping; no provider is bundled or endorsed |
| `PLATE_LOOKUP_URL` | unset | The provider's endpoint; `{plate}` and `{region}` are substituted where present |
| `PLATE_LOOKUP_KEY` | unset | Sent as a bearer token |
| `PLATE_LOOKUP_MONTHLY_CAP` | `0` | `0` means no cap. A cap is the difference between a mistake costing a dollar and a mistake costing a month's budget |
| `PLATE_LOOKUP_COST_MINOR` | `0` | The operator's own per-call estimate, shown before each lookup. Nothing reads a price list; providers do not publish one |
| ~~`BACKUP_CRON`~~ | *(not an environment setting)* | The worker enqueues `backup.run` daily on its own schedule (§15.1), so the interval is code rather than a crontab. R-9 moves it to the UI, where a schedule belongs |
| `BACKUP_INTERVAL_HOURS` | `24` | How often the worker enqueues `backup.run`. Editable in the UI (R-10) |
| `BACKUP_RETENTION` | `7d4w6m` | GFS |
| `BACKUP_PASSPHRASE` | unset | Enables encryption |
| `LABOR_RATE_MINOR` | `0` | Time valuation; `0` hides it. Named for minor units rather than cents, since not every currency has hundredths (§5.5) |
| `MAX_UPLOAD_MB` | `50` | Per file |
| `CREDENTIAL_KEY` | derived from `SECRET_KEY` | R-9 — encrypts stored integration credentials. Stays in the environment by definition: a key kept in the database it protects is not a key. Rotating it invalidates every stored credential at once, which is the intended emergency behavior. |
| `GUNICORN_PIDFILE` | `/tmp/gunicorn.pid` | Written by `gunicorn --pid`. Without it the pending-restart banner names the command instead of offering a button that would quietly do nothing (§17.2) |
| `LOG_LEVEL` | `info` | |

---

## 15. Delivery phases — the plan

This section is the plan as committed, kept unchanged so that what was
predicted can be read against what happened. **§15.1 is what was built and
§15.2 is what remains** — neither is recorded here.

Each phase is independently useful — **the shop must be able to start using it at the end of Phase 1**, not at the end of Phase 4.

### Phase 1 — Spine (MVP)
Vehicles (manual + VIN decode + local VIN validation), people and ownership, odometer readings, work orders with job items / notes / photos, media pipeline, auth and users, global search, dashboard, backup and restore, full export. Plus **service-information link pinning** (§8.5) — a table, a link, and a button, costing almost nothing and delivering a daily-use win from week one — and the **localization scaffolding** (§5.6): message catalogs, CLDR formatting, per-transaction currency, and the automated check for unwrapped strings. The latter is not a feature and ships no visible value; it is here because it is the one thing in this document that genuinely cannot be added later without touching every file. **Usable outcome:** the notebook and the shoebox are retired.

### Phase 2 — Money and parts
Parts catalog with cross-references and fitment, inventory with locations and QR labels, purchases and vendors, core tracking, expenses, receipt OCR, cost rollups and reports, time tracking. Plus the **`external_ref` table and a one-time LubeLogger import** (§8.6) — dry-run, then migrate. **Scheduled pull sync is deliberately deferred to Phase 4**: the one-time import captures nearly all the value at a fraction of the ongoing maintenance burden, and per OQ-9 nothing may depend on the sync existing. **Usable outcome:** "what did this car cost me" and "what's on the shelf" are answerable — and the answer includes the history already sitting in LubeLogger, rather than starting from an empty database.

> The *import* lands in Phase 2 deliberately: it is what makes the cost reports built in this same phase meaningful on day one — arriving with two years of real service records beats arriving with none, and it back-fills the odometer series that FR-COST-3 is computed against. The *sync* is a different proposition with a different risk profile, so it waits.

### Phase 3 — Ahead of the work
Schedule templates and per-vehicle service items, interval math and projections, service completion linkage, due dashboard, reminders and notifications, recalls integration, vehicle specs, per-vehicle PDF report. Plus **equipment assets** (§7.1a) — an `asset_kind` column, the meter generalization, and equipment schedule templates, which is nearly all reuse — and the **DVI module** (§7.8) — templates, positional measurements, thresholds, offline inspection, results-to-work-order, inspection report, and `asset_component` tracking with wear-rate projection. **Usable outcome:** the app tells you what to do next instead of only recording what you did.

> DVI belongs in this phase and nowhere else: its measurements feed the same due-projection machinery this phase builds, and `asset_component` is what turns a repeated measurement into a wear rate. Built earlier it would be a checklist with nothing to predict against; built later the projection logic would be written twice.

### Phase 4 — Garage hardware and polish
Scan-tool import pipeline — **PDF report ingestion, parser-profile engine, extraction review UI, re-parse, manual mapping wizard** (§8.3a) — plus structured file import, ELM327 direct read, DTC dictionary and code→work-order workflow, plate lookup adapter, scheduled LubeLogger pull sync, the **WrenchLedger integration** (§8.7, subject to OQ-16), offline write queue and conflict merge UI, CSV import, PWA install and push, accessibility pass. **Usable outcome:** the phone in the garage is a first-class tool.

> **The XTOOL D8 profile was not in this phase's committed scope** — the *engine* was, and the profile was blocked on a sample report (§8.3a). Nine arrived during the phase and it was written. The bet held either way: the manual mapping wizard makes any tool's export importable without a profile, and profiles are additive on top of it.

> Ordering rationale: the offline write queue lands in Phase 4 rather than Phase 1 because it is the single most expensive correctness surface in the design, and its value depends on there being enough daily capture to lose. Phases 1–3 must still work on a flaky connection — they simply fail the write and say so plainly, rather than queueing it.

---

## 15.1 What is built

Phases 1–4 are implemented. This section is the honest record and the **single
ledger of everything that shipped**: what was delivered against each phase, what
shipped after the phases closed, and — more usefully — **where the
implementation decided something different from the rest of this document, and
why**. A status section that only says "done" stops being worth reading.

The roadmap (§17) and the candidate list (§18) keep the reasoning behind each
item and no longer carry its state; every item either appears in the table below
or in §15.2.

| Phase | State |
| --- | --- |
| 1 — Spine | Built |
| 2 — Money and parts | Built |
| 3 — Ahead of the work | Built |
| 4 — Garage hardware and polish | Built |

### Phase 4, item by item

| Committed scope | What shipped |
| --- | --- |
| PDF report ingestion | `homeautoshop/diagnostics/` — upload becomes a draft `diagnostic_session`, with the raw media and the extracted word geometry retained permanently. |
| Parser-profile engine | Built, with one deviation recorded below. |
| Extraction review UI | Each field's value, provenance and confidence beside the original. Nothing enters vehicle history without a person confirming it (FR-INT-4). |
| Re-parse | From the stored words or text, needing neither the original upload nor object storage. Re-reading a *confirmed* session produces a new draft rather than rewriting a reading somebody vouched for. |
| Manual mapping wizard | Column mapping for tabular exports, free-text entry otherwise, and **learn-from-example**: the mapping saves as a profile that reads the next report of the same shape by itself (FR-INT-6). |
| Structured file import | CSV, JSON and text, with the column guess made from **cell content rather than header names** — a column whose values are trouble codes is the code column whatever it is called, which is what makes the guess right for a tool nobody has aliased. |
| ELM327 direct read | Web Serial, driven by the browser. Modes 03/07/0A, DTC decoding from the raw bytes, and clearing behind a confirmation that names the vehicle and the readiness-monitor consequence. |
| DTC dictionary | **Five layers, each named on screen.** J2012's own words are used throughout — a code is *ISO/SAE controlled* or *manufacturer controlled*; "generic" is the shop-floor term and appears nowhere in the standard. The SAE generic set is bundled and authoritative. Below it, a note somebody typed for that make, then a manufacturer's own published list. **Those are published rather than bundled** — `catalog/codes/<make>.json`, one file per make, **56 makes and 151,232 definitions** — for the reason parser profiles are: there are ninety-odd makes and a shop owns two or three, so bundling them all would put eighteen thousand definitions in every image so that each operator could use a few hundred. A shop installs the makes on its own ramp; an installed list is an `InstalledCodeList` row that `dtc` reads beside the bundled standard, and a make with nothing installed falls back to the standard and the shop's own notes — exactly where it stood before any list existed. Most of that is read from factory service manuals rather than crawled: a published library distributes its whole corpus as an index and a sorted string table, so `read_manual_library` walks the files directly — no request budget, no sampling, nobody's server involved — and `catalog_dtc_harvest` folds a make's harvest into its bundle *above* whatever was already there rather than in place of it. That ordering is the point: the harvest reads the manuals of the vehicles it sampled, so it goes very deep on those and can miss ranges a broad compilation happens to list, and substituting would have read as an upgrade while losing 1,902 Ford codes, 368 Audi and 309 Daewoo. Publishing is a separate command from harvesting because running a harvest is routine and publishing somebody else's compilation is not; `--publication` is required so attribution is never something a command invents. Lincoln and Mercury read Ford's list for as long as that was the only one covering them and now have 4,892 and 3,048 codes of their own, with Ford's 5,633 behind them for what they do not carry — a badge's own document outranks one that reaches it by alias, and both still answer. Sibling brands are *not* merged: Chevrolet and Buick share 427 codes and word only 227 of them alike, so each keeps its own document and an alias is used only where two are word for word the same (Peterbilt reads Kenworth's). **Below that, the standard's own P/B/C/U sets answer an ISO/SAE code for any make at all**, taking that coverage from the 144 codes written out by hand to 3,512; such a list is matched to no make, so it can never answer a manufacturer-controlled code. A make may be covered by more than one document — Suzuki has a summary of the badge and a 2004 Aerio's own service manual, sharing 11 codes of 155 — so **one file is one manufacturer, holding every document that covers it**, with `precedence` deciding which answers first and the citation naming the document, so "Suzuki says" is never the whole answer. Grouping by make rather than by document is what makes the catalog entry something a person can choose: you install *Suzuki*, not two documents you are then expected to rank. **Every list carries a `version`**, raised only when the content actually changes, so the browse screen can say that what is installed is behind what is published — and a newer one is **offered, never applied**, because a definition somebody is reading today should not change under them because a catalog was edited this morning. **A published list may not claim to be the standard**: `codelistlib` refuses a document scoped `iso_sae` outright rather than downgrading it, since an ISO/SAE definition is presented as fact and a stranger's wording arriving with that authority is the failure scoping by make exists to prevent. It is the same validator for every route in — catalog install, `manage.py install_code_list`, and the upload form — because a file is trusted exactly as far as the road it came down, and there is only one road. **P-1 keeps a way in with no network**: the lists were bundled, so being offline used to mean no worse; now `install_code_list` reads the catalog folder off disk and the import form takes a file carried in on a stick. Documents examined and kept out are recorded in `codelists/_rejected.json` with the evidence, because the overlap check compares against what is *already held* and so depends on build order — rebuilt from empty in alphabetical order, `Citroen` is read before `Ford` and nothing catches it. `build_dtc_list` measures every new list against the ones held and **refuses one that is word for word another's** — which is how a "Duramax" manual (91% Ford), a "Citroen" page (91% Ford, including *Dual Alternator Upper Fault*) and both free multi-make databases on GitHub were kept out, while genuine corporate-family overlap up to 84% passes with a note. **A transcription repairs bytes and never wording.** An extractor that drops an encoding puts characters in the table the publisher never wrote: TroubleCodes' Daihatsu list reads *Advance`A1``A2`retard*, which is EUC-JP for the ideographic comma, and Volvo's `P1637` and `P1638` are the same sentence with a bullet where the other has a dash. Those are decidable and get decided; typographic dashes and quotes become the ASCII a technician can actually type, since an en dash reads identically and searches differently; `Pass Key®`, `4x4` and degrees survive, being notation somebody wrote on purpose; and damage that cannot be read back to a character — a lone `A3` with no lead byte — is dropped rather than guessed, because inventing a comma is still inventing. 2,389 definitions across 43 lists were repaired, and the tables are asserted clean on every run rather than cleaned once. **And a row that looks like a definition is not one.** A chart marks every unassigned number `ISO/SAE Reserved` and every assigned one `Manufacturer Controlled DTC`; a manual writes prose about a code in a bullet that opens with it — `B0188 is for the right sunload sensor.` — and a seven-column VAG table puts the enable conditions two columns from the meaning, so `condition` in a header match defined 195 Audi codes by the thresholds at which they set. Those were 7% of one 151,128-definition harvest, and refusing them recovered 564 more: a reserved row read early *claimed* the code and hid the real definition sitting in a later manual. What is not refused is a definition naming another code — `C1293` means `C1291 or C1292 set in a previous ignition cycle` — and a code that a rule removed is looked for again by `read_manual_codes`, which searches a library for named codes at a hundredth of a harvest's cost and reports the ones nothing defines, because that is the answer often enough to matter. What remains is answered **structurally** from J2012's own shape — system, subsystem, generic-or-not — and is never a guess at the fault. A shop's own note outranks a shipped table, for the same reason a corrected VIN decode outranks vPIC — but not the standard, since a generic code means the same thing on every vehicle ever built. **Below all of those sits the wording the scan tool itself printed**, which is a third party rendering somebody else's definition: it truncates, and it sometimes declines outright — one tool answers a Ford `B1695` with *"Please See The Vehicle Service Manual."* where Ford's own list says *"Autolamp On Circuit Failure"*. Ranked there rather than dropped: what the tool read is still printed underneath, because the reading is evidence and editing it on the reader's behalf is the opposite of the point. Every code links to a page carrying the definition, who says so, everywhere the code has turned up in this shop, and the box to write down what nobody has said yet — reachable from any reading, not only from a draft. **And reachable without one**: the global search box and `GET /api/v1/codes` both look a code up by number, by prefix for one half-read off a cracked screen, or by words from its meaning, because a dictionary that could only be entered through an imported report answered "what is P0420" by asking somebody to run a scan first. Every list carries a `version` — **the standard's own sets included**, since J2012 is revised and codes are added, and a definition presented as fact still came from a particular printing of it; a bundled list changes with the image rather than through the catalog, so there the number is provenance rather than an update prompt, and an answer that came from no list at all reports none. |
| Code → work order | A code becomes a complaint in the car's words; completing the job item marks the code addressed; the same code on a later scan is flagged `recurring`. |
| Plate lookup adapter | A provider interface with every §8.2 guardrail: off by default, a per-press confirmation showing the running monthly count, a hard monthly cap, and a failed call still counted. |
| Scheduled LubeLogger pull | Incremental by date window with a deliberate overlap, never creating a vehicle unattended, and the watermark advanced only after a clean run. |
| WrenchLedger | The readiness gate. Pull-only, availability from `/loans?open_only=true`, an allow-listed field cache, and entitlement read rather than inferred. |
| Offline write queue | An IndexedDB queue with client-minted UUIDv7 keys, `POST /api/v1/sync/batch` returning per-item results, and a conflict list that is neither auto-merged nor dropped. |
| Conflict merge UI | Side-by-side, at whole-version granularity — see below. |
| CSV import | Vehicles, parts and service history, mapped by the person who made the file, dry-run first, idempotent on re-run. |
| PWA install and push | Manifest, icons, a service worker served from the site root, an offline page, Background Sync, and Web Push. |
| Accessibility pass | Every control given an accessible name, `prefers-reduced-motion` and `forced-colors` honored, and a `check_accessibility` command that fails the build — the same shape as the i18n gate, because a pass with no gate behind it decays one hurried form at a time. |

### Where the implementation disagreed with this document

**A parser profile is data — with an escape hatch, and the escape hatch is
load-bearing.** §8.3a says adding a tool is authoring a profile, never a code
change. That holds for formats whose labels and values survive text extraction:
the shipped `Generic code list` profile is pure YAML and reads a DTC table with
no code involved at all. The XTOOL D8 defeats it — its section boundaries are
**colors**, its labels extract *after* their values, and a cell's first line can
render above its own row. A profile language able to express that is a
programming language with worse tooling. So `parser_profile.engine` may name a
built-in parser instead of carrying rules, and the session still records which
profile and which version read it, so re-parse and regression triage work
identically either way.
[SCHEMA-PARSER-PROFILES.md](SCHEMA-PARSER-PROFILES.md) already predicted this
and declined to freeze the contract until a second tool's reports arrive. That
judgment was right, and it has now been tested — see below.

**The second tool arrived, and fourteen more with it.** 169 reports and exports
published on the public web were gathered into the corpus (`fetch_scan_samples`,
`capture_scan_samples`), and **seven working profiles came out of them**:
Ross-Tech VCDS, Autel MaxiSys, THINKCAR, TOPDON, Carly, BlueDriver and Car
Scanner ELM OBD2. Most read *text*, several of those from a PDF — so §8.3a's
finding that word geometry is necessary turns out to be a fact about the D8
rather than about scan reports. Word geometry is what a format costs you, not
what a parser deserves.

**The largest single defect was in how a PDF was read at all.** `_read_pdf`
joined each page's words in *extraction order* — the order they were written
into the content stream, which is not the order they appear on the page — and
produced one meaningless line per page. That made three formats unparseable, and
it let a label reach across a flattened page for a value that was not its own:
an Autel report printing `VIN: --` yielded a VIN, the first seventeen characters
of a repair-order number further down. A wrong VIN is worse than no VIN. Reading
the printed lines instead fixed the misreading and unlocked the three formats,
with **zero change to anything that already worked**.

Six things the contract could not express, each added because a format demanded
it and each recorded in SCHEMA-PARSER-PROFILES.md §1a: a row that spans lines,
a column that falls back between capture groups, a column *joined* from a cell
wrapped over two lines, `map` as a closed vocabulary rather than a set of
shortcuts, a section heading that attributes a code to its module, and a
*generic* profile that no longer outranks a specific one on score alone. That
last was not cosmetic — the bundled `Generic code list` scored 1.0 on a VCDS
Auto-Scan where the VCDS profile scored 0.85, and read nothing out of reports
holding 61, 14 and 0 faults.

**A code now arrives with the module it came from** — 388 of the 398 the catalog
profiles read, where none did. **And the data stream is read, not only the code
table.** `DiagnosticSession.live_data` and the Reading / Value / Min / Max panel
on the session screen shipped in Phase 4 and only the D8's built-in parser could
fill them, so a THINKCAR data-stream report holding 159 readings and no fault
codes at all imported as an entirely empty session. A `live_data_extractor` is
now a profile key like any other.

That is deliberately **not** a datalog reader, and the boundary is NG-4's. A
`LiveDatum` is a reading with a value and a range — what a tool prints during a
session. A logger CSV is a time series, and collapsing ten thousand rows of RPM
to a minimum and a maximum throws away the only thing such a file is for. The
corpus therefore keeps what a profile here could read and names what it drops,
with the reason, in `not-captured.json`.

What is still not read is a report whose columns
wrap *around* the code: THINKCAR's older generator prints the module name and
the status above and below their own row, so a description arrives in fragments
that only column geometry could reassemble. That is the D8's wall, and the
answer is the same — no profile is published for it, and the profile for
THINKCAR's newer generator fingerprints so as not to claim it. The same rule
settled TOPDON, whose two tablets wrap differently: claiming both read one fault
in eleven out of the second, which is the worst answer a fingerprint can give.

**The profiles are published, not bundled** (§8.1b). The image still ships two —
the D8 and the generic text reader — and the rest live in the catalog, because
there are hundreds of scan tools and an operator owns one. A built-in list of
eighty formats is eighty claims this project cannot check, presented to somebody
who needs exactly one of them.

**Redaction needed a second rule, and it needed it urgently.** NFR-S-5's rule
keyed off the ISO 3779 check digit, which is exact for the North American
vehicles the original corpus came from and silently wrong everywhere else:
position 9 is a check digit only where a regulator requires one, so every
European VIN in a public report would have been committed unredacted. A VIN is
now also anything VIN-shaped that follows a VIN label, and a stand-in preserves
a filler where it found one, so the corpus keeps the case §5.5's validation
exists to tolerate.

**Web Push is the one thing that cannot be local-first.** §9.4 asks for web
push; P-1 and NFR-S-1 say your data lives on your hardware and nothing phones
home. Web Push does not deliver to a browser — it delivers to *the browser
vendor's* push service (Google, Mozilla, Apple), which then wakes the device.
No design choice changes that, so it is stated rather than buried. What is
enforced instead: off until a person answers a browser permission prompt on
that device; disabled by Offline Mode; the endpoint host allow-listed, so the
operator can see and refuse the service; and **the payload names nothing** —
*"something is due"*, with the detail behind the tap, because the notification
renders on a lock screen in front of whoever is standing there and passes
through a third party on the way.

**Recurring work is enqueued by the worker, not by cron.** Nothing scheduled the
jobs this design assumed would run on a timer — the backup handler existed and
was never called. The options were a cron entry inside the container (invisible
to the application), a beat process (another container, against P-3), or the
worker asking on each pass what is due. The third means *"is the nightly backup
actually running"* is a row in the same table as everything else, answerable
from the health screen. §5.1's process list is unchanged.

**The conflict merge is version-level, not field-level.** §5.4 requires a
side-by-side merge that never auto-resolves and never silently drops. It ships
as a choice between two whole versions rather than a field-by-field editor: the
conflicts this application actually produces are one person's odometer reading
against another's, or a status change against a status change — small, whole
facts where *mine or theirs* is the real question. The escape hatch §5.7
reserved, one embedded client-side component, was not needed.

**A failed plate lookup is still counted.** §8.2 says every call is recorded.
The implementation also counts calls that *errored*, because a provider that
answered at all has almost certainly billed for it, and a counter tracking only
successes understates the bill — which is the single thing this feature's
guardrails exist to prevent.

### Settled by building it

| ID | Now settled |
| --- | --- |
| OQ-18 | **v1 ships US-only recall coverage, stated rather than silently absent.** Transport Canada and PROFECO each need their own adapter and their own data model; neither is wired up, and the UI says unavailable-for-region rather than showing an empty list. Unchanged from §8.4 — recorded here because "decide during Phase 4" is no longer a live answer. |
| OQ-19 | **Not taken up.** The DVI template engine is class-scoped already, so equipment inspection is a matter of authoring templates rather than building anything. No pre-season equipment template ships; the capability is there the moment somebody writes one. |
| WL-Q5 | The gate is read from `features.api_webhooks` on every connection check and never inferred from the plan tier — so a Solo workspace carrying the override works today, and a relaxed gate needs no change here. |

### Built after Phase 4 closed

Everything that shipped outside the four phases, including the eight roadmap
items and the three candidate features that were built. The **Why** column names
the section that still holds the reasoning.

| Requirement | What shipped | Why |
| --- | --- | --- |
| FR-VEH-5 | VIN barcode scanning off the door jamb, decoded **on the device** by the browser's own `BarcodeDetector`. No frame leaves the phone, so it works with the WAN unplugged — the only version of this consistent with P-1. Code 39, QR, Data Matrix and PDF417, because jamb labels use all four. The payload is searched for something VIN-shaped rather than trusted whole: real labels wrap the VIN in Code 39 start/stop asterisks, prefix it, or append a checksum. | §7.1 |
| FR-INV-2 | Printable QR labels for storage locations, and scanning one opens that location's contents — its child locations included, since an empty cabinet is not the answer when the parts are in its drawers. | §7.4 |
| FR-INV-3 | Scan a part's barcode to find it, **or create it with the barcode already recorded** as a UPC cross-reference. Without that last step a scan-and-miss teaches the shop nothing and the same box is a dead end again next month. | §7.4 |
| C-4 | Vehicle tags, from the same machinery. | §18 |
| R-8 | RTL verified rather than assumed. `dir` on `<html>`, `lang` that reflects the language actually being served, twelve physical declarations in `app.css` and two inline styles made logical, and `check_rtl` as the gate that keeps it that way. | §17 |
| FR-WO-8, FR-COST-8 (R-6) | The project cost roll-up that had been claimed since the first draft, and the budget burn-down built on it. §7.6b. | §17 |
| FR-FLU-1–6 (R-5) | Oil and fluid analysis: samples, pasted panels, and trends expressed as a rate per unit of *fluid* life wherever a rate means anything. §7.9a. | §17 |
| R-1 | The shared template catalog — schedule templates, inspection checklists, parser profiles and per-make DTC lists, published as reviewed files in `catalog/` rather than served by anything. The file formats came first, because two of the four artifacts had no portable format at all. §8.1b. | §17 |
| R-2 | The `helper` role, as a request gate over an allow-list of URL names rather than the policy-layer fill-in the roadmap predicted — see §12.2a for the measurement that changed the approach. | §17 |
| R-7 | Maintenance cost forecasting: the next twelve months of spend projected from due service items and historical part costs. §7.6a. | §17 |
| R-9 | Instance settings in the UI, on a typed registry with **database → environment → default** precedence, credentials in a separate encrypted table excluded from both backup paths and from the export, and a non-dismissible pending-restart banner for the settings Django resolves at startup. §17.1, §17.2. | §17 |
| R-10 | Backup operable from the UI: back up now, the held backups with timestamp and size, download, and the portable export on the same screen. **Restore stays on the command line**, with the screen printing the exact command against this instance's real paths. | §17 |
| C-3 | The warranty report — parts still under warranty, sorted by expiry — turning `part_usage.warranty_*`, collected since Phase 2 and surfaced nowhere, into money recoverable. `core/costs.py`, on the shop reports screen. | §18 |
| C-5 | `asset_spec.is_sensitive`, defaulted on for the security-adjacent seed groups, and honored by the vehicle report, the shared export and every screen a non-owner can reach. Key codes, radio codes and wheel-lock locations are exactly what P-4's portability goal would otherwise have handed to a buyer. | §18 |

**One label format for everything.** A label carries `{BASE_URL}/s/{uuid}/`, and
`/s/` resolves the id against locations, vehicles and parts. Primary keys are
UUIDv7 and unique across the database, so one route answers for any of them —
one label design, one scanner, and a bin label and a windshield tag that behave
identically.

**The `location.qr_code` column in §6.2 is deliberately not implemented.** A
second identifier is a second thing to generate, keep unique, and keep in step
with the row it names, and it buys nothing: the primary key is already unique
and already permanent. The one thing a separate code *would* buy — a label that
survives its row being deleted and recreated — is not wanted. That label should
stop working.

**A LubeLogger vehicle can be paired by hand.** The matcher was reading a `vin`
field LubeLogger does not have. It has one identifier column: `vehicleIdentifier`
names the *kind* — it comes back as the literal string "License Plate" — and the
value lives in `licensePlate` whatever kind was chosen. On the instance this was
found against, the operator had put full VINs there, so both vehicles reported
as unmatchable. Values are now classified by shape rather than by the column
they arrived in. What no rule can match — a source vehicle carrying neither a
VIN nor a plate — is paired on the import screen, which writes one `external_ref`
row and is honored by every later run, the scheduled pull included. Refusing to
guess is only defensible if there is a way to say which vehicle is which.

### Not built, and deliberately

- **The WrenchLedger webhook receiver** (integration document §5). Pull is the
  default precisely because a LAN instance cannot receive a webhook; the signed
  receiver is an upgrade for operators who already expose their instance.
- **Project mirroring, tool custody, and meter post-back** (§6.3–6.5 there). All
  three are gated on WrenchLedger features absent from the smaller plan, and all
  three are marked optional. The readiness gate is the integration.
- **The offline VIN dataset** (OQ-6). Still the right design; still an
  admin-triggered download nobody has needed.
- **`SCAN_IMPORT_ENABLED` and `EQUIPMENT_ENABLED`** (§14). Both are documented
  and neither is read by anything. They are therefore **not** on the settings
  screen: a switch that does nothing is worse than an absent one, because it is
  a promise the instance cannot keep. `OCR_ENABLED`, `RECALLS_ENABLED` and
  `SERVICE_INFO_ENABLED` were in the same state and now gate something.

---

## 15.2 What remains

Everything outstanding, in one place: **seven items deferred on purpose, six
claims the code does not answer, and no open questions.** Nothing here has to be
discovered by reading another section — §17, §18 and §19 hold the reasoning and
point back here for the state. Re-verified against the code on 2026-09-03.

### Deferred on purpose

Decided, not forgotten. Each has a reason that is still good.

| Item | Where | Why it is not built |
| --- | --- | --- |
| **R-4** — read-only share link for one report | §17 | A share link is only a share link if somebody outside the LAN can open it, which means a permanent inbound hole in the router for an occasional convenience. The existing answer is a PDF sent by whatever channel the operator already trusts. Worth building if this ever runs somewhere already reachable. |
| **R-3 remnant** — stand-alone engines and project drivetrains | §17, OQ-15 | Equipment is in scope and shipped; what is left is the narrower case of an asset with no meter and no identity of its own. Nothing in the schema precludes it. |
| **OQ-6** — the offline VIN dataset | §8.1 | Still the right design, still an admin-triggered download nobody has needed. Network VIN decode plus local validation has covered every case so far. |
| **C-6** — reusable procedure checklists | §18 | *Recommendation stands, its stated basis does not.* C-6 declined to build a third checklist system because FR-WO-11 and the DVI template engine already covered it — **but FR-WO-11 is itself not built** (see below), so only one of the two exists. Extending the DVI engine remains the right move; the reasoning needs redoing before anyone acts on it. |
| **WrenchLedger webhook receiver, project mirroring, tool custody, meter post-back** | §8.7, integration doc §5, §6.3–6.5 | Pull is the default precisely because a LAN instance cannot receive a webhook; the other three are gated on WrenchLedger features absent from the smaller plan. All are marked optional there. The readiness gate is the integration. |
| **Canadian and Mexican recall sources** | §8.4, OQ-18 | Each needs its own adapter and its own data model. v1 ships US-only coverage and the UI says unavailable-for-region rather than showing an empty list. |
| **Tool and toolbox inventory** | §18 C-2, NG-8/NG-9 | Rejected permanently, not deferred. WrenchLedger does it, and this integrates rather than rebuilds. |

### Claimed in this document, absent from the code

The live half of §19. Each row is a capability this specification describes in
the present tense that nothing implements — recorded here rather than left to be
rediscovered, because a claim nobody has built on is a claim nobody has checked.

| Claimed | Where | What is actually there |
| --- | --- | --- |
| **Duplicate a work order as a template** ("annual service"), with job items and expected parts | FR-WO-11 | Nothing. No view, no service, no template flag on `work_order`. The three FR-WO-11 citations in the code all sit on the *planned parts* feature, which is a different requirement that was never given a number — so the requirement reads as implemented to anyone grepping for it. **§18's C-6 rests on this row**, which is the §19 pattern exactly. |
| Every report exports to CSV; no report is a dead end | FR-REP-4 | Two exports: the shop reports screen and the per-asset report. The per-vehicle costs screen, the due list, the wear chart and the shelf do not. |
| Fitment data is publishable to the catalog | R-1, §8.1b | No portable format exists for it. Schedule templates, inspection checklists, parser profiles and DTC lists all publish; fitment is the one artifact the catalog is partly about that cannot. |
| The ship set is `en-US`, `en-CA`, `fr-CA`, `es-MX`, with the three non-source catalogs **complete** | §5.6, DEVELOPMENT.md | A fresh extraction puts `fr_CA` and `es_MX` at **597 untranslated and 317 fuzzy** of 2,926 — better than the ~1,000 behind recorded at v0.6.5, and still a picker offering four languages where a fifth of the newest screens fall through to English. `en_CA` carries 1,853 untranslated, which is mostly harmless (it falls through to the US source, which is usually right) and hides the handful where it is not. Only `makemessages` can find this: `check_translations` proves each string is *wrapped*, a property of the source, and is silent on whether any catalog answers for it. |
| A PDF's words are read as **the lines they were printed as** | §8.3a | True between lines and not within one. `lines_from_words` sorts a row left to right **only for measured geometry — in practice OCR** — so a Toyota report's module fault count, printed to the right of the module name and one point higher, still comes out in front of it as `2 EOBD/OBD II`. The fix is one `sort` already written and deliberately not applied: two catalog profiles' section patterns were authored against the current output and lose their headings without it, costing nine modules' worth of code attribution across two real reports. Correcting it means new profile versions, which is its own change. |
| The build is gated: CI fails on unwrapped strings, on a stale catalog index, and exercises restore against the prior release's backup format | §5.6, §8.1b, §13.2 | **There is no CI.** `.github/` does not exist. All four gates are real and each runs as a test — `check_translations`, `check_accessibility`, `check_rtl`, `build_catalog --check` — but what enforces them is somebody remembering `manage.py test`. The restore round-trip is not covered at all: a sample backup sits in `Artifacts/samples/backups/` and no test loads it. |

### Questions still open

**None in this document.** OQ-1 through OQ-19 are all answered — §16 holds each decision with its
consequence — and **OQ-17 was the last of them**, closed in the integration
document rather than here, which is why §16.2 went on listing it: **WL-Q4 answers
no reciprocal pairing at this time**, with the proposal preserved verbatim in
[INTEGRATION-WRENCHLEDGER.md](INTEGRATION-WRENCHLEDGER.md) §13 for manual transfer
to the WrenchLedger backlog when it is wanted. WL-Q1–Q12 are likewise all
answered against source and a live workspace.

The one live set is **LL-Q1–Q3** in
[INTEGRATION-LUBELOGGER.md](INTEGRATION-LUBELOGGER.md), and none blocks anything:
the exact endpoint paths of a running instance, whether the ongoing pull is still
wanted now that the import has landed, and whether Supply Records import as parts
or as expenses. Each carries a stated default and imports as a draft for review
either way.

---

## 16. Resolved decisions

All twelve open questions from v0.2.0 are answered. Recorded here with their consequences, because a decision without its rationale becomes an open question again in six months.

| ID | Question | Decision | Consequence |
| --- | --- | --- | --- |
| OQ-1 | Implementation language and framework | **Python 3.12 + Django 5 + django-ninja + HTMX** (§5.7) | Chosen on the merits, not on familiarity: PDF/OCR parsing is the hardest work in this spec and Python owns that ecosystem. Cost: NFR-P-6 must rise to 900 MB or the `slim` profile is used. |
| OQ-2 | Community repository for templates and fitment | **Built** — §8.1b | Deferred in v1 on the grounds that import/export already covered it; schedule templates turned out to have neither. P-1 is kept by making the catalog user-pressed, allowlisted, Offline-Mode-aware and entirely optional, and the trust model by giving downloaded files no privilege an uploaded one lacks. |
| OQ-3 | Native fuel/energy logging | **No, permanently** | Not a repair function; LubeLogger handles it. NG-7 is now settled rather than deferred. FR-COST-3 is explicitly repair-cost-per-distance, fuel excluded, and says so in the report. |
| OQ-4 | Tooling in per-vehicle cost | **Tracked, exportable, excluded by default, toggleable** | `expense.category = tooling` is first-class and always exported; `COST_INCLUDE_TOOLING` (default off) governs rollups. FR-COST-6 unchanged. |
| OQ-5 | Multi-currency / localization | **Full localization from commit one** (§5.6) | The largest single consequence in this round: per-transaction currency with snapshotted FX rates, message catalogs, CLDR formatting, translation keys on all seed data, logical CSS properties. Ships English (US) plus translator-ready catalogs. |
| OQ-6 | Offline VIN dataset | **Yes, as an opt-in download** (§8.1) | Not bundled — an admin-triggered job that fetches, converts, and loads it locally, with size disclosed up front and removable afterwards. Local dataset takes precedence over the network when present. |
| OQ-7 | Narrower role for non-household helpers | **Scaffold now, ship later** (§12.2, §17 R-2) | v1 behavior is unchanged (two roles), but authorization goes through a single `can(user, action, resource)` policy layer and a `vehicle_access` table exists with implied-allow semantics. Adding `helper` later is policy rules, not an audit of every view. |
| OQ-8 | Which vehicle types are in scope | **Anything with a license plate** | `vehicle_class` (car, truck, motorcycle, trailer, RV, bus, other plated) gates specs, schedules, inspection templates, and form fields. **Nothing may assume an engine, an odometer, or four wheels.** Non-plated equipment and stand-alone engines are roadmap (R-3). |
| OQ-9 | LubeLogger retire or keep | **Keep, but never a dependency** (§8.6) | HomeAutoShop owns the maintenance schedule and every core function. An instance with no LubeLogger is not degraded. The integration is a migration convenience with an optional sync — anything that starts to feel load-bearing is scope creep and gets cut. |
| OQ-10 | Who owns reminders | **User's choice; HomeAutoShop by default** | Imports LubeLogger reminders once as seed, then owns them, and suggests disabling theirs. Flipping the setting stops HomeAutoShop evaluating imported items. |
| OQ-11 | ALLDATA DIY subscription | **Seeded and enabled, with per-vehicle visibility** | Subscriptions are per vehicle, so `vehicle_service_info_link` carries `subscription_status` and `subscription_expires_on`. ALLDATA shows on the vehicles it is paid for; expiry surfaces beside registration renewals. |
| OQ-12 | Export scope for profiles and pinned links | **Profiles exportable; pinned links backup-only** | Parser profiles are shareable and belong in a portable bundle. Pinned links reveal which vehicles the operator owns, so they travel only in the full encrypted backup. |

### 16.1 Second round

| ID | Question | Decision | Consequence |
| --- | --- | --- | --- |
| OQ-13 | Memory budget under the Python stack | **Raise NFR-P-6 to 900 MB** | Settled in §11.2. The `slim` profile remains documented at < 600 MB for constrained hosts, but the five-container default is the honest baseline. |
| OQ-14 | Which locales | **North America: `en-US`, `en-CA`, `fr-CA`, `es-MX`**, with the destination genuinely open | §5.6 ship set. These four are not cosmetic — they exercise metric/imperial mixing, three different date orders, different decimal grouping, and a legally weighted second language. **Also exposed a real gap:** NHTSA recall data is US-only; Canada and Mexico have separate systems, unwired in v1 and now stated rather than silently absent (§8.4). |
| OQ-15 | Tool and equipment scope | **Split: serviceable equipment in, tool inventory permanently out** | The largest change this round. Generators, mowers, and small engines are first-class assets (§7.1a, NG-8/NG-9); toolbox inventory, storage locations, loans, and valuation belong to WrenchLedger and are integrated with (§8.7), never rebuilt. Drove the `asset`/`asset_kind` generalization and the `usage_reading` meter generalization in §6.2. |
| OQ-15a | C-1 installed component tracking | **In scope** | Was already modeled and required (`asset_component`, FR-CMP-1–6) because FR-DVI-11 depends on it. Now confirmed rather than provisional. |

### 16.2 Questions this round opened

**All of them are answered.** OQ-17 was the last, and it closed in the
integration document as WL-Q4 rather than here — which is how it stayed on this
list after it had stopped being a question. §15.2 records that nothing is open.

| ID | Question | Bearing |
| --- | --- | --- |
| OQ-16 | ~~Does WrenchLedger's API cover what this needs?~~ **Answered by reading the implementation.** It does — REST v1 with OpenAPI 3.1, scoped keys, idempotent writes, signed outbound webhooks, and Projects/assignments/meters already shipped. The real constraints turned out to be different: the **Shop-plan gate**, and the fact that **cloud webhooks cannot reach a LAN instance**. Remaining questions moved to the integration document (WL-Q1–Q6). |
| ~~OQ-17~~ | ~~Should the pairing be reciprocal — a HomeAutoShop mention inside WrenchLedger?~~ **Answered as WL-Q4: no WrenchLedger changes at this time.** | Answered in the integration document rather than here, which is why this row outlived it. The proposal is preserved verbatim in [INTEGRATION-WRENCHLEDGER.md](INTEGRATION-WRENCHLEDGER.md) §13 for manual transfer to the WrenchLedger backlog when it is wanted — greenfield rather than a conflict, since WrenchLedger's roadmap has no vehicle-side concept at all. |
| ~~OQ-18~~ | ~~Do Canadian or Mexican recall sources get wired up, or does v1 ship US-only with an honest gap?~~ **Answered: US-only, stated rather than silently absent.** | Each source needs its own adapter and its own data model, so neither is wired up and the UI says unavailable-for-region rather than showing an empty list. Recorded in §15.1 and carried in §15.2 as deferred. |
| ~~OQ-19~~ | ~~Should equipment support the DVI module, or is inspection vehicle-only in v1?~~ **Answered: not taken up, and nothing needs building.** | The DVI template engine is class-scoped already (FR-EQP-4), so equipment inspection is a matter of authoring a template. No pre-season equipment template ships; the capability is there the moment somebody writes one. §15.1. |

---

## 17. Roadmap (post-v1) — what each item turned out to be

Explicitly out of v1 scope when written, retained so the decisions are not re-litigated and so the schema does not accidentally preclude them.

**This section holds the reasoning, not the status.** Eight of the ten shipped and are listed in §15.1; R-3 was superseded by OQ-15; **R-4 alone is still deferred** and is carried in §15.2 with the rest of what remains. The struck-out rows are kept rather than deleted because several of them predicted the work wrongly, and *how* they were wrong is the part worth having: R-1's stated reason for deferring turned out to rest on a capability that did not exist, R-6 could not be built until a requirement claimed since the first draft was actually implemented, and R-8's premise was almost right and worth nothing. A roadmap item is a load test for the requirements underneath it (§19).

| ID | Item | From | Notes |
| --- | --- | --- | --- |
| ~~R-1~~ | **Community template repository** — shared schedule templates and parser profiles — *built* | OQ-2 | The stated reason for deferring did not survive contact: "import/export already covers the ninety-percent case" was true of parser profiles and **false of schedule templates, which had no import or export at all** — the artifact the repository is mostly about. So the file format came first and the catalog delivers files it already accepts. The trust model and the network dependency were treated as a specification rather than an objection: see §8.1b. Hosted as `catalog/` in this repository, so entries are reviewed by pull request. Fitment data and inspection checklists are not yet publishable — neither has a portable format, which is now a visible gap rather than an assumed capability. |
| ~~R-2~~ | **`helper` role with per-vehicle access** — *built* | OQ-7 | Not what this row predicted. The scaffolding was real but load-bearing in the wrong place, so the work was a request gate over an allow-list of URL names rather than "populating it and adding a UI" — see §12.2a for the measurement that changed the approach. |
| R-3 | ~~Non-plated assets~~ — **superseded (OQ-15)** | OQ-8 | Generators, mowers, and small engines are now **in scope** as `asset_kind = equipment` (§7.1a). What remains on the roadmap is the narrower case of stand-alone engines and project drivetrains with no meter and no identity of their own. |
| R-4 | **Read-only share link for a single report** | §3.1 | A tokenized, expiring, revocable URL for one vehicle report or PPI — for a buyer or a family member, without an account. Distinct from user accounts and genuinely useful, and **staying here** for a reason that is about deployment rather than about design: this runs on a LAN, and a share link is only a share link if somebody outside the LAN can open it. That means an inbound hole in the router — a permanent one, for an occasional convenience — and the honest trade is that the exposure lasts far longer than the need. The existing answer is a PDF, which the operator sends by whatever channel they already trust. Worth building if this ever runs somewhere already reachable; not worth opening a port for. |
| ~~R-5~~ | **Oil and fluid analysis results** — *built* | new | Structured storage of lab reports trended over time, and the row's own spelling of *analyzis* survived four revisions of this table, which is its own small argument for reading what you wrote. The design turned on one thing the row did not mention: **a wear metal is a rate, not a level**, so the sample carries how far the *fluid* had run and not only where the odometer stood. Without that, two samples cannot be compared at all — and three quarters of a panel must never be turned into a rate, because additives deplete and viscosity is a state. See §7.9a. |
| ~~R-6~~ | **Project budget burn-down** — *built* | new | Not the one-column job this row predicted, for a reason the row could not have known: **FR-WO-8's cost roll-up to the parent had never been implemented**, so a burn-down over `work_order_cost` would have reported an engine swap as barely started. Building the tree roll-up came first. The second surprise was that one number cannot answer the question — money fitted, money on the shelf and money on order are three different answers to *"how much has this cost me"* and the bar shows all three. See §7.6b. |
| ~~R-7~~ | **Maintenance cost forecasting** — *built* | new | Projecting the next 12 months of spend from due service items and historical part costs. It was nearly free as predicted, because both halves were already modeled: `project()` knows when each service lands and `ServiceCompletion` names the work order that paid for the last one. See §7.6a for the two judgments that were not free. |
| ~~R-8~~ | **RTL locale testing** — *built* | §5.6 | The premise was nearly right and worth nothing. The stylesheet really was logical — twelve physical declarations out of a few hundred, one of them a four-value `padding` shorthand that no grep for `-left` would ever have found — but **`<html>` carried no `dir` attribute at all**, and a logical property with no declared direction resolves left-to-right, so every `margin-inline-start` in the file was doing exactly what `margin-left` would have done. No amount of reading the stylesheet could have shown that; rendering a page in an RTL locale showed it in one line. The same render found `lang="en"` on a page served in French. Now held by `check_rtl`, which runs as a test. See §19 and §5.6. |
| ~~R-9~~ | **Instance settings in the UI** — *built in v0.6.1* | new | The `setting` entity already exists and already promises this — *"instance configuration surfaced in the UI, overriding environment defaults"* — but holds only `last_backup_at`. Today, renaming the shop, changing the reminder cooldown, or throwing the Offline Mode kill switch means a text editor and a container restart, which puts routine choices behind a deployment step and puts an emergency control (NFR-S-2) out of reach of the person who needs it. Precedence becomes **database → environment → default**, so an instance nobody has touched behaves exactly as it does now. See §17.1 for what moves and what cannot. |
| ~~R-10~~ | **Backup operable from the UI** — *built in v0.6.1* | new | The health page reports how long ago the last backup ran, and the reminder digest raises a warning when it goes stale — and neither offers any way to act. Telling someone their backup is overdue while making them go and find a shell is worse than saying nothing at all. The machinery already exists: `backup.run` is a registered job handler and `manage.py backup` ships, so this is a screen over finished work. Scope: **Back up now**, enqueued and showing progress · the held backups with timestamp, size and contents · download · the portable export (P-4) on the same screen · retention and schedule, which arrive with R-9. **Restore stays on the command line** — swapping the database underneath a running process is not something a web request should attempt — but the screen shows the exact command with this instance's real paths filled in, rather than leaving an operator to reassemble it from the docs during the one hour they can least afford it. |

### 17.1 R-9 — what moves, and what does not *(shipped; this is how it behaves)*

> **Built in v0.6.1.** The registry is `homeautoshop/core/settings_registry.py`;
> the accessor and the credential store are `homeautoshop/core/runtime.py`. Two
> things came out differently from the design below and are worth recording:
>
> * **`conf.X`, not `settings.X`.** §17.2 called for "a lazily-read accessor",
>   and the shape it took is an attribute lens over `django.conf.settings` — one
>   token different at each of the ninety-odd call sites, evaluated when asked,
>   falling through to the environment when no row exists. Values are cached for
>   one second per process, which is a dozen fewer queries per page render and
>   well inside the time it takes to walk to the workshop after throwing the
>   Offline Mode switch.
> * **The outbound allowlist had to become derived.** It is assembled at import
>   from the configured integration addresses — and those addresses are now
>   editable. Left alone, a LubeLogger saved on the settings screen would have
>   been configured, enabled, and refused: the worst of the three states,
>   because everything says it should work. It is computed per call instead.

Three groups, because treating them alike is how a settings page becomes a
lockout or a leak.

**Stays in the environment.** `SECRET_KEY`, `CREDENTIAL_KEY`, `DATABASE_URL`,
`ALLOWED_HOSTS`, `BASE_URL`, `DEBUG`, `TLS_MODE`, `STORAGE_*`, `STATIC_HASHED`,
`LOG_LEVEL`.
These are read before the database is reachable, or a wrong value locks the
operator out of the very screen that would fix it. Configuration that can
prevent the instance from booting does not belong inside the instance.

**Moves to the UI.** Shop name and branding · locale, units, currency and
timezone · Offline Mode and the per-integration enable flags · default
service-information provider · DVI and equipment toggles · tooling-cost
inclusion and labour rate · reminder enablement and cooldown · house-placement
links · backup schedule, retention and staleness warning · upload ceiling and
GPS stripping · plate-lookup monthly cap · LubeLogger and WrenchLedger modes.
Roughly two thirds of §14, and the two thirds a household actually adjusts.

**Moves too — credentials.** SMTP password, LubeLogger, WrenchLedger and
plate-provider API keys are entered in the UI like anything else. They are
**not** `setting` rows: they live in a separate `credential` table, encrypted
at rest with a key held in the environment, and they are stripped from every
artifact the instance produces.

That last part needs to be precise, because the database backup is a *physical*
one — a whole-database `pg_dump`, or a copy of the SQLite file — so nothing is
excluded merely by not asking for it:

| Artifact | How the exclusion is actually achieved |
| --- | --- |
| Portable export (P-4) | The `credential` table is skipped outright. The precedent exists: `REDACTED_FIELDS` already strips `user.password` and `api_token.token_hash` from the export. |
| Postgres backup | `pg_dump --exclude-table-data=core_credential` — the schema is kept, the rows are not. |
| SQLite backup | The file copy is taken as now, then the **copy** is opened and its `core_credential` rows deleted. Never the live file. |
| Media archive | Nothing to do — a credential is never written to disk outside the database. |

Encryption at rest is kept **as well as** exclusion, not instead of it. They
defend different things: exclusion protects the artifact that gets carried to a
NAS or a laptop; encryption protects the live database file, which is what an
attacker reads if they get as far as the volume.

One consequence must be stated in the UI rather than discovered later: **a
restored instance has no integration credentials.** Restore is already a
deliberate, guarded operation, so the screen following one lists every
integration that is configured but unauthenticated, each with a link to re-enter
its key. An integration in that state is *disabled and says so* — it does not
retry silently and fill the log with authentication failures.

All of which is testable as a single assertion over the produced artifact: *no
backup and no export contains credential material.*

Two further constraints apply to everything that moves:

- **Values need a typed, validated, translatable registry.** A free-text
  `setting` row with no schema is a way to brick an instance by typo, and the
  labels and help text are user-facing strings subject to §5.6 like any other.
- **Every change writes to `audit_log`** — who, when, old value, new value, with
  credential values recorded as changed rather than quoted. Offline Mode is the
  case that most needs an answer to *who turned this off*.

### 17.2 R-9 — making a change take effect *(shipped; this is how it behaves)*

> **Built in v0.6.1**, with one correction. The overlay for restart-class
> settings does **not** run in `AppConfig.ready()`, which is the obvious place
> and the wrong one: `ready()` runs for every management command, including the
> `migrate` that creates the table it would be reading, and Django warns about
> database access there for exactly that reason. It runs on the first request
> (`ConfigMiddleware`, placed before `LocaleMiddleware` so a stored language is
> not one request late) and at worker start. Exactly three settings are
> restart-class — `LANGUAGE_CODE`, `TIME_ZONE`, `MAX_UPLOAD_MB` — and a test
> asserts that set, because a setting marked `restart` that did not need to be
> makes people restart for nothing.

Settings are read at import time today, so a value edited in the UI changes
nothing until the process restarts. Most entries can become immediate by moving
their consumers to a lazily-read accessor, and should. A few cannot: anything
Django resolves once at startup — storage backends, the locale set, middleware,
database configuration — is a restart no matter how the value is stored.

So the registry declares `applies: immediate | restart` per entry, and **the UI
never leaves someone guessing which they are dealing with**:

- An immediate setting simply takes effect, and says so on save.
- A restart setting is marked at the field, *before* it is edited rather than
  after — "takes effect when the instance restarts".
- Changing one raises a banner on every admin page listing what is waiting.
  It does not fade, and it is not dismissible; only an actual restart clears it.

The mechanism is a `config_generation` counter, bumped on every restart-class
write and recorded by each process at boot. A process whose recorded value is
behind the stored one is running stale configuration, and knows it:

| Process | How it picks up the change |
| --- | --- |
| Web (`gunicorn`) | An **Apply and restart** button in the banner sends `SIGHUP` to the gunicorn master via a pidfile. Workers reload gracefully — in-flight requests finish, new workers import the new settings. No dropped requests, no downtime. |
| Worker | It compares `config_generation` on each poll and exits cleanly when it differs; Compose's `restart: unless-stopped` brings it back within seconds. No cross-container signalling to build. |
| `runserver` (dev) | The autoreloader watches files, not the database, so nothing happens by itself. The banner falls back to naming the exact command. |

This adds one deployment requirement — `--pid` on the gunicorn command line —
and nothing else.

If the restart never happens, the banner stays. The failure mode is a visible
nag rather than an instance quietly running configuration that disagrees with
what its own settings page displays.

---

## 18. Candidate features — the answer to "what else is missing?"

Four were built outright, one was split — serviceable equipment accepted and shipped, tool inventory rejected permanently — and **C-6 is the only one still outstanding**, carried in §15.2. The rows are kept here with their reasoning, which is the part worth having: three times now the best answer to a candidate feature has been a boundary rather than a build.

| ID | State |
| --- | --- |
| C-1 Installed component tracking | Accepted into scope (OQ-15) and shipped in Phase 3 |
| C-2 Shop tooling | **Split** — serviceable equipment shipped, tool inventory rejected permanently |
| C-3 Warranty dashboard | Built — §15.1 |
| C-4 Vehicle QR tag | Built — §15.1 |
| C-5 Sensitive spec handling | Built — §15.1 |
| C-6 Reusable procedure checklists | **Outstanding, and its stated reasoning is wrong** — §15.2 |

### C-1 — Installed component tracking · **ACCEPTED — now in scope**

Modeled as `asset_component` (§6.2) and required by FR-CMP-1–6, because FR-DVI-11 depends on it. A tread-depth reading is a number; a tread-depth reading against a component installed 31,000 miles ago is a **wear rate**, and a wear rate is a due date. It also answers the questions a home shop asks out loud — *how old is this battery*, *how many miles on these tires and were they ever rotated*, *is this alternator still under warranty*. FR-CMP-6 (tire DOT date codes) catches a genuine safety case nothing else in the spec would: full tread, ten years old, structurally finished.

**Cost:** one entity, one tab, automatic creation from part usage. Small, because it rides on parts and inspections that already exist.

### C-2 — Shop tooling · **SPLIT: serviceable equipment accepted, tool inventory permanently rejected**

The original candidate bundled two things that turned out to be entirely different propositions (OQ-15):

**Accepted — serviceable equipment (§7.1a, `FR-EQP-1–5`).** Generators, mowers, pressure washers, and small engines need service intervals, repair history, parts, and cost tracking. That is the same machinery vehicles already use, which is why it cost an `asset_kind` column and a meter generalization rather than a module. Drove the `asset` and `usage_reading` renames in §6.2 — cheap now, expensive after implementation.

**Rejected, permanently — tool and toolbox inventory (NG-8, NG-9).** Which drawer holds which socket, what the collection is worth, who borrowed the breaker bar, when the torque wrench was calibrated. [WrenchLedger](https://wrench-ledger.app) already does all of it — nested locations five deep, kits, lending with due dates and reminders, insurance-ready reports — and rebuilding it here would duplicate significant existing effort to produce something worse. HomeAutoShop **integrates** instead (§8.7): it references tools, shows availability, and warns before a job when a needed tool is loaned out or out for calibration, while never becoming the place tool data is entered.

> The general principle worth extracting: *the correct response to an adjacent product that already solves a problem well is a boundary and an integration, not a reimplementation.* The same reasoning already retired fuel logging to LubeLogger (OQ-3) and repair procedures to LEMON/ALLDATA (NG-5). Three times now, the best feature decision has been not to build something.

### C-3 — Warranty dashboard · **BUILT**

`part_usage` already carries `warranty_months` and `warranty_distance`, and `asset_component` extends it. Nothing currently *surfaces* it. One report — parts still under warranty, sorted by expiry — turns data already being collected into money recovered. Add to FR-REP-1.

### C-4 — Vehicle QR tag · **BUILT**

QR label printing already exists for storage bins (FR-INV-2). The same machinery on a windshield or door-jamb tag means scanning a car opens its record — useful with five vehicles in a driveway, and very useful for logging an odometer reading in three seconds.

### C-5 — Sensitive spec handling · **BUILT** — it was trivial, and it was a real gap

Key codes, radio codes, alarm PINs, and wheel-lock key locations are exactly what `asset_spec` is for, and exactly what must **not** land in a shared export, a service-history PDF handed to a buyer, or an unencrypted backup. Adding `is_sensitive` to `asset_spec`, defaulting the security-adjacent seed groups to sensitive, and excluding them from reports and shareable bundles closes a hole that P-4's portability goal would otherwise open.

### C-6 — Reusable procedure checklists · **still outstanding**

A repeatable job (brake fluid flush, valve adjustment) as a template that materializes into job items.

The recommendation was **do not build a third checklist system — extend one of the two that already exist**, naming FR-WO-11 (duplicate a work order as a template) and the DVI template engine (§7.8). The recommendation survives; its arithmetic does not. **FR-WO-11 was never built** — no view, no service, no template flag — so there is one existing checklist system, not two, and this row talked itself out of a feature by counting one that did not exist. Extending the DVI engine is still the right move, and §15.2 carries both halves: the checklist and the missing FR-WO-11 underneath it.

> This is §19's failure mode with a candidate feature instead of a design decision, and it is the fourth instance: a requirement nobody doubts is a requirement nobody checks, and whatever leans on it inherits the mistake.

---

---

**What the redaction guard is about.** The check that no valid VIN appears in the tree reads what git would publish, not what happens to be on disk. The two are not the same: research notes gathered for a later phase — a manifest of third-party scan reports found on the public web, some with the VIN in the source URL, so that redacting breaks the link the entry exists for — sat gitignored and untracked and failed a test whose subject is the contents of the repository. Only *ignored* files are skipped, never merely untracked ones: a new untracked file is one `git add` from being published, and catching it then is the whole reason the check runs before a commit rather than after. Where git cannot answer, nothing is skipped.

## 19. Stated but not built

This document has described several capabilities in the present tense that were never implemented. Each was found by trying to use it, one at a time, and in two cases the missing thing had already been cited as a reason not to build something else. That is the expensive failure mode: a requirement nobody doubts is a requirement nobody checks, and a decision resting on it inherits the mistake.

Found and closed:

| Claimed | Where | What was actually there | Closed by |
| --- | --- | --- | --- |
| Schedule templates import/export as YAML | §8, FR-MAINT-9 *(SHOULD)* | Nothing. Parser profiles had it; schedule templates had neither direction. **OQ-2 cited this as the reason a shared repository was unnecessary.** | FR-MAINT-11, §8.1b |
| Every authorization decision goes through `can(user, action, resource)` | §12.2 | True for admin-versus-member. Of 225 view functions, 48 called `require()` and **19 named a resource**, all in one app. §12.2 cited this as the reason a `helper` role would need no audit. | §12.2a |
| Inspection templates import/export as YAML | FR-DVI-13, with a full schema document | Nothing. The contract was written, reviewed and never implemented. | FR-DVI-13, §8.1b |
| The per-vehicle report exports as PDF **and CSV** | FR-REP-2 | PDF only. | FR-REP-2 |
| Allow-listed views call `require(user, action, object)` to decide *which* vehicle | §12.2a | Not in `maintenance`: all seven writes on the schedule screen took the asset id from the URL and checked nobody against it, so a helper with a read grant on one vehicle could write to any. The section that recorded "`maintenance` 9 views and no checks" as the problem then described the fix in the present tense. | §12.2a, FR-MAINT-12 |
| CSS logical properties throughout, so **RTL is a stylesheet concern rather than a rewrite** | §5.6, R-8 | The stylesheet was all but twelve declarations logical and it did not matter: `<html>` had no `dir` attribute, so every `-inline-start` in the file resolved left-to-right anyway. Also `lang="en"` on every page whatever the language — `django.template.context_processors.i18n` was never installed, and the template's `|default:'en'` covered for the empty variable. Found by rendering one page under an RTL locale, which is the check the row itself had deferred. | `check_rtl`, `core/tests_rtl.py`, §5.6 |
| Costs and time **roll up to the parent** on a project work order | FR-WO-8 | Nothing rolled up. `parent_work_order_id` was written and read by the parent picker and by nothing else: a project's page listed neither its children nor their spend. Found by building R-6 on it — the first burn-down reported an engine swap as barely started. | FR-WO-8, §7.6b |
| A photographed scan report is read by OCR | §7.9, §8.3a, v0.6.2 | True, and it read nothing. Every phone photo carries an EXIF orientation and Pillow hands back sensor order, so a receipt photographed on a bench went to Tesseract rotated ninety degrees and came back **empty** — not poor, empty. The media pipeline then filed it `ocr_status = done` with an empty `ocr_text`, which is indistinguishable from a picture with no text in it, so nothing ever said the feature was not working. Found by trying to parse the first photograph anybody had actually taken. | `upright()`, `flatten_lighting()`, §8.3a (a2) |
| The corpus is redacted captures, and nothing identifying reaches the repository | NFR-S-5, `capture.py` | True for PDFs and switched off for photographs. Every rule that protects a labelled value asks what is printed *beside* it, and `_same_line` compared word **tops**: on a real OCR capture `SN:` and its serial are 24 pixels apart, because the colon runs a full cap height and the serial is digits, against a tolerance of 21. So no label preceded the serial, so a real tester serial was written into this repository and committed. Found by grepping the new captures for the string — which is worth doing every time, and which then found the same serial in the *test* written to prove the redaction works. A value that belongs nowhere belongs nowhere, including in the test about removing it. | `_same_line` compares middles; `RedactingABenchTesterTests` |
| Re-parsing a confirmed report **leaves the original alone**, so two readings can be compared | §8.3a, FR-INT-5 | True, and it stopped there. Nothing recorded that the copy was the *same report*, so confirming it filed the same test in the vehicle's history twice — and removal was offered on drafts alone, which made the duplicate permanent. The same photograph uploaded again did it too: media is deduplicated by SHA-256, so it was demonstrably the same report and demonstrably a second scan. Found by somebody re-reading a battery slip and looking at the vehicle's page. | `supersedes`, `services.confirm`, "Remove from the history" |
| A tool's profile **declares what its reports contain**, and the screens ask it rather than guessing | §8.3a, `reports` | True in the code at both ends and false in the row between them. `seed()` runs on every boot and skips a profile it has already installed — the rule that keeps an operator's changes — so the declaration reached fresh installs and reached nobody who was already running: their row was written before the column existed and re-seeding stepped over it. Versioning, the documented answer to a bundled profile changing, cannot serve either, because a bumped version is a new row and the history points at the old one. Found by somebody adding the field, seeing the screens ignore it, and asking whether it was read anywhere. | Migration `0012`, and `seed()` now fills a bundled field the row has **never** held a value for |
| A scan session soft-deletes into a **30-day trash** | `session_discard`, since drafts became discardable | The message said so and nothing listed sessions in the trash, so the delete was permanent and invisible at once — the pair the three entries above `parser_profile` in `TRASHABLE` were each added to fix, now the fourth. | `TRASHABLE["diagnostic_session"]` |
| Equipment is created **with no VIN, plate, title, or registration fields shown** | FR-EQP-1 | One flat form, showing both kinds' fields to both kinds. Adding a mower offered a VIN, a licence plate, a registration expiry and a vehicle class — and `Asset.clean()` refuses a VIN on equipment outright, so the screen invited an entry the record would then reject. Found the ordinary way: somebody went to add a mower. | FR-EQP-1 unchanged — the form now matches it |

**Still open: see §15.2.** The live half of this list is kept there with the
rest of what remains, so that there is one place to look rather than two. Six
claims stand open, one of them found while consolidating this document —
FR-WO-11's work-order template, which §18's C-6 had cited as a reason not to
build something else.

**The lesson is about how the gaps were found, not about the gaps.** Three were discovered by a person trying to use the feature, months apart. One was found in ten minutes by grepping this document for rows claiming a portable format and checking each against the code — which is what produced the "still open" list above as well. One was found while adding a delete button to the screen that was missing the check, which is the ordinary way: you look at the lock when you are already at the door.

One more arrived by a route that is worth naming because it looked like nothing: **a test that failed only in company**. `TroubleCodesAreSearchableTests` passed alone and failed in a full run, on and off, and an intermittent failure reads as flakiness rather than as a bug. It was a bug. `dtc.find` searched the *rendered* wording of the ISO/SAE table — lazy strings, so whichever language was last activated — and `LocaleMiddleware` activates one per request and never deactivates, so any case following one that rendered a French page searched a French table for English words. A French instance would have had it permanently, and nobody would have connected the two. The fix searches the source wording and the reader's own, which is better behaviour anyway: a scan tool prints English whatever the shop speaks. **An intermittent failure is a bug that has told you its symptom and withheld its ordering.**

Two arrived by a fourth route: **running the thing against real input for the first time**. Both photograph findings above were invisible to every test in the suite, because every test in the suite supplied text. The temptation at that point is to write the fixture by hand — the photographs cannot be committed, OCR needs Tesseract installed, and typing out what the receipt says looks like a reasonable way to get unstuck. It is not, and the corpus now refuses a capture that does not record OCR as its reader. A hand-written capture is a record of what somebody *imagined* OCR does, and every hard case in this format is a case where OCR does something surprising: `850CCA(CCA)` read as `BSOCCA(CCA)`, a value landing a line above its own label, a section banner torn in half across two lines. A transcription would have passed on all of them and shipped a parser that failed on all of them.

The same session produced a **dead end worth recording**, because it will look attractive again. The one value the parser gets wrong is a digit printed over by a tear-off perforation, and word confidence does not catch it — Tesseract reports 89 for the damaged token. Tesseract will also give *per-character* confidence, at no extra cost and returning the same words in the same order (`-c lstm_choice_mode=2` with hOCR output), and on a damaged glyph the character stream does show a run of low-confidence marks that never reach the decoded word. That looked like exactly the signal. It is not: on one page it marks 21 of 41 words, including three that were read perfectly, because what it is actually detecting is the texture of photographed thermal paper. **A warning that fires on half of what it shows teaches the reader to stop looking**, which is worse than not warning — so the failure is recorded in a test instead, and the mitigation is that the value is a draft with the paper shown beside it.

The last two arrived by a third route worth naming, because it is the one that scales. Both were found by **building the next thing on top of the claim**: R-8 was verification of §5.6's layout promise and R-6 needed FR-WO-8's roll-up, and each collapsed within an hour of being leaned on. A roadmap item is a load test for the requirements underneath it, and a claim nobody has yet built on is a claim nobody has yet checked — which is a reasonable way to decide which of the remaining ones to be suspicious of.

A specification is a set of claims about a program. Anything in it that is mechanically checkable should be mechanically checked, and where a claim is load-bearing for a *decision* — as FR-MAINT-9 was for OQ-2, and §12.2 was for R-2 — the cost of it being wrong is not one missing feature but a design conclusion drawn from a false premise. New capability claims should arrive with the test that proves them, and existing ones are worth sweeping again whenever a decision is about to lean on one.


## Appendix — Release history

One line per release. What each one decided is in the body of the document, at
the section named; §15.1 and §15.2 are always the current state.

| Version | What it changed |
| --- | --- |
| 0.7.0 | Status consolidated into §15 (plan), §15.1 (built) and §15.2 (remaining); four contradictions between the six places it had been kept removed; every open claim re-verified against the code, which found FR-WO-11 unbuilt underneath §18's C-6. |
| 0.6.7 | The first tool whose report is a photograph — a TOPDON BT600 Plus receipt, five phone photos, and a built-in parser. Found that OCR read *nothing* from a rotated capture and filed it `done`; that subtracting a blurred copy of a photo from itself took the corpus from 73 legible values to 84; and that NFR-S-5's redaction was switched off on photographs, committing a real tester serial. §8.3a, §19. |
| 0.6.6 | The second scan tool and thirteen more: 169 public reports redacted into the corpus, **seven parser profiles published rather than bundled**, and the discovery that `_read_pdf` had been joining words in extraction order — which made three formats unparseable and let an Autel report printing `VIN: --` yield a VIN. §8.3a, §8.1b. |
| 0.6.5 | **No object store in the stack.** Media lives under `MEDIA_ROOT`, which is what §13.1 backs up; MinIO held it where no backup reached it. §5.1, §13.1. |
| 0.6.4 | Follow-ups from using the parts-order import: the preview now leads into the import rather than asking for the same file twice, and the document is stored on the way past, deduplicated by SHA-256. §8.3a. |
| 0.6.3 | Supplier order confirmations read into the catalog — a RockAuto PDF becomes a purchase, its lines, and the parts in it, with fitment recorded as *stated by vendor*. FR-PUR-1, FR-PART-2/3/4. |
| 0.6.2 | Fixes from first real use: uploaded files served by the application rather than by presigned URL against a hostname that resolves only inside Compose. §5.1, §12.3. |
| 0.6.1 | **R-9 and R-10 built** — instance settings in the UI on a typed registry, credentials in a separate encrypted table, and backup operable from a screen. §17.1, §17.2. |
| 0.6.0 | **Phase 4 built.** §15.1 opened, recording what shipped and where the implementation disagreed with this document. |
| 0.5.3 | R-9 credentials resolved: a separate encrypted table stripped from the export and both backup paths, and a restored instance stating which integrations need re-authenticating. §17.1. |
| 0.5.2 | R-9 added — instance configuration moves out of `.env` and into the UI. §14, §17. |
| 0.5.0 | Split into a document set; the base spec becomes requirements only. |
| 0.4.1 | WrenchLedger contract split out and written against the shipped API. **Key finding: webhooks cannot reach a LAN-only instance, so sync is pull-first.** FR-WL-5. |
| 0.4.0 | OQ-13/14/15 resolved · **serviceable equipment in scope**, driving the `asset`/`usage_reading` generalizations · **tool inventory permanently out** · §8.7 WrenchLedger · locales scoped to North America, exposing a US-only recall gap · C-1 accepted. |
| 0.3.0 | All 12 open questions resolved · **stack decided: Python/Django** · **localization from commit one** · the DVI and component-tracking module · vehicle scope widened to anything plated. |
| 0.2.0 | §8.3 rewritten around scan-tool PDF ingestion · §8.5 service-information link-out · §8.6 LubeLogger · new entities `parser_profile`, `external_ref`, `service_info_provider`, `vehicle_service_info_link`. |

---

## Appendix — Companion documents

The appendices that were previously inline now live beside this document, so the base spec stays focused on requirements while material that changes on its own schedule can move independently.

| Document | Contents |
| --- | --- |
| [REFERENCE.md](REFERENCE.md) | Work order state machine · seed data shipped with the application · glossary |
| [SCHEMA-PARSER-PROFILES.md](SCHEMA-PARSER-PROFILES.md) | Parser profile YAML contract and the XTOOL D8 scaffold — implements §8.3a |
| [SCHEMA-INSPECTION-TEMPLATES.md](SCHEMA-INSPECTION-TEMPLATES.md) | Inspection template YAML contract and threshold evaluation — implements §7.8 |
| [INTEGRATION-LUBELOGGER.md](INTEGRATION-LUBELOGGER.md) | Modes, API facts, entity mapping, sync semantics — detail behind §8.6 |
| [INTEGRATION-WRENCHLEDGER.md](INTEGRATION-WRENCHLEDGER.md) | Boundary, transport, verified API facts, caching limits, house placement — detail behind §8.7 |
| [samples/scan-reports/](samples/scan-reports/README.md) | Scan-tool report corpus and fixture conventions |

See [README.md](README.md) for the full document set and reading order.
