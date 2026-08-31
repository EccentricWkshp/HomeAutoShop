# HomeAutoShop

Self-hosted, local-first shop management for a home garage working on personally
owned vehicles and equipment.

It replaces the shoebox of receipts, the glovebox notebook, and the spreadsheet
with one searchable record of what you own, what you did to it, and what needs
doing next. Your data lives on your hardware, works with the internet unplugged,
and can be exported in full at any time.

**Not** a commercial shop system: no invoicing, no customer billing, no
technician payroll. The optimization target is a person standing in a garage
with dirty hands and a phone — and that same person three years later trying to
remember which brand of caliper they used.

## Status — Phases 1–4 complete

| Capability | State |
| --- | --- |
| Vehicles and equipment, with VIN validation and NHTSA decode | ✅ |
| People, ownership history | ✅ |
| Meter readings (odometer / engine hours / cycles) | ✅ |
| Work orders: lifecycle, job items, append-only log, photos | ✅ |
| Media pipeline: dedupe, async thumbnails, EXIF stripping | ✅ |
| Auth, roles, API tokens | ✅ |
| Global search, dashboard, instance health | ✅ |
| Backup, retention, restore, portable export | ✅ |
| Service-manual link pinning (LEMON / CHARM / ALLDATA DIY) | ✅ |
| Localization scaffolding + CI gate on unwrapped strings | ✅ |
| Ownership history, 30-day trash with restore | ✅ |
| Object storage (MinIO/S3) with presigned URLs | ✅ |
| OCR of documents, photographed receipts and scanned PDFs | ✅ |
| **Read a photographed printout — battery testers and anything else that only prints paper** | ✅ |
| REST API + OpenAPI at `/api/v1/docs` | ✅ |
| **Parts catalog, cross-refs, confirmed fitment** | ✅ |
| **Inventory: locations, FIFO lots, ledger, cycle counts** | ✅ |
| **Purchasing: vendors, partial receiving, landed cost, cores** | ✅ |
| **Read a supplier order PDF into the catalog — parts, prices, cores and fitment** | ✅ |
| **Costs: per work order, per vehicle, per distance, CSV export** | ✅ |
| **Time tracking** | ✅ |
| **LubeLogger one-time import (dry-run, idempotent)** | ✅ |
| **Maintenance: templates, interval math, due projection** | ✅ |
| **Completion linkage — closing a job item rolls the schedule** | ✅ |
| **Installed components with DOT-age condemnation** | ✅ |
| **DVI: templates, positional measurements, thresholds** | ✅ |
| **Wear projection — two readings become a due date** | ✅ |
| **Pre-purchase inspection on a prospect vehicle** | ✅ |
| **Vehicle specs with sensitive-code handling** | ✅ |
| **NHTSA recalls, with the US-only gap stated** | ✅ |
| **Per-vehicle PDF report — the sale document** | ✅ |
| **Reminders: digest by email or webhook, deduplicated** | ✅ |
| **Scan-tool report import — read, review, confirm** | ✅ |
| **Parser profiles as YAML, importable and versioned** | ✅ |
| **Re-parse historical reports with a better profile** | ✅ |
| **Manual mapping wizard that saves what it learns** | ✅ |
| **Offline trouble-code dictionary** | ✅ |
| **Code → work order, and flagged when it comes back** | ✅ |
| **ELM327 read over Web Serial, from the phone** | ✅ (needs Chromium) |
| **Plate lookup, with a cap and a per-call confirmation** | ✅ (needs your own provider) |
| **LubeLogger scheduled pull sync** | ✅ |
| **WrenchLedger — tool availability on a work order** | ✅ (needs a WrenchLedger account) |
| **Offline write queue with conflict merge** | ✅ |
| **CSV import — vehicles, parts, service history** | ✅ |
| **Installable PWA, offline pages, web push** | ✅ |
| **Accessibility pass, with a build gate behind it** | ✅ |
| **Scan a VIN barcode off the door jamb, on-device** | ✅ (needs Chromium) |
| **Printable QR labels for bins and vehicles** | ✅ |
| **Scan a bin label to see what is in it** | ✅ |
| **Scan a part's barcode to find it — or create it with the code kept** | ✅ |
| **LubeLogger: pair a vehicle by hand when it carries no VIN** | ✅ |
| **Instance settings in the UI, with the `.env` file as the fallback** | ✅ |
| **Backup, export and download from the UI — restore stays a command** | ✅ |

932 tests, all passing — on SQLite and on Postgres 18.

## Quick start

```bash
cp .env.example .env      # edit SECRET_KEY and the passwords
docker compose up -d
```

Then open the site. **A new instance ships with no accounts, on purpose** —
there is no default login to forget to change — so it sends you to a setup
page that makes the first one, names the shop, sets units and timezone, and
tells you how to get another device to trust the certificate. It runs once and
is unreachable afterwards. The same page comes back any time you start from an
empty database volume.

Then add your own hostname and trust the certificate — full walkthrough in
[docs/INSTALL.md](docs/INSTALL.md), including how to reach the instance from
another machine on the LAN.

Or without Docker — see [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md), which also
covers the two `.env` values that need changing for a laptop.

> **TLS is not optional.** Service workers, the camera (VIN barcode scanning),
> and Web Serial/Bluetooth all refuse to run outside a secure context, so Caddy
> terminates TLS for the stack.
>
> If you have a domain name, set `TLS_MODE=acme-dns` and get a real Let's
> Encrypt certificate through a DNS challenge — **nothing to install on any
> device, and the instance stays off the internet**. A free dynamic-DNS name
> does just as well as one you own. Otherwise `internal` works with no
> prerequisites, at the cost of installing a root certificate on every device
> that will open the site. [docs/INSTALL.md](docs/INSTALL.md) covers both.

## Design

The full specification lives in [Artifacts/](Artifacts/README.md):

- [SPEC.md](Artifacts/SPEC.md) — goals, architecture, domain model, ~150 numbered requirements
- [REFERENCE.md](Artifacts/REFERENCE.md) — work order state machine, seed data, glossary
- [SCHEMA-PARSER-PROFILES.md](Artifacts/SCHEMA-PARSER-PROFILES.md) — scan-tool report extraction
- [SCHEMA-INSPECTION-TEMPLATES.md](Artifacts/SCHEMA-INSPECTION-TEMPLATES.md) — DVI templates
- [INTEGRATION-LUBELOGGER.md](Artifacts/INTEGRATION-LUBELOGGER.md) · [INTEGRATION-WRENCHLEDGER.md](Artifacts/INTEGRATION-WRENCHLEDGER.md)

Five principles shape every decision in the code:

1. **Local-first.** The instance is authoritative. External services are
   accelerators, never dependencies, and a single Offline Mode switch stops all
   outbound traffic.
2. **Never lose a capture.** Photos, notes, and readings are append-only, so a
   phone in the garage can never lose work to a sync conflict.
3. **Semi-lightweight.** Postgres does search *and* the job queue. No
   Elasticsearch, no Redis, no broker.
4. **Own your data.** The export is newline-delimited JSON plus ordinary files,
   and must be readable without this application.
5. **Degrade, don't block.** A failed VIN lookup never prevents recording the
   vehicle by hand.

## Privacy

No telemetry, no analytics, no phone-home — including update checks. GPS EXIF is
stripped from photos by default, because a photo of a car in a driveway geotags
a home address. See [PRIVACY.md](PRIVACY.md).
