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

Licensed [AGPL-3.0](LICENSE).

## What it does

**Vehicles and equipment.** One record for each thing you maintain. VINs are
validated locally and decoded against NHTSA's vPIC; a pre-1981 VIN is read from
the manufacturers' own published tables instead, because vPIC knows only the
17-character format and those are exactly the vehicles a home garage keeps.
Equipment — a generator, a mower, a pressure washer — carries a serial number
and an hour meter rather than a VIN and an odometer, and is never sent through a
VIN decode or a recall check.

**Work orders.** Job items, time, parts used, photographs, and an append-only
log of what actually happened, under a status lifecycle that will not let a job
skip states. Costs roll up per job, per vehicle and per mile or hour.

**Maintenance.** Service schedules with distance and time intervals, projected
against your own measured usage rate rather than an assumed annual mileage.
Closing a job item rolls the schedule forward, so the record and the plan cannot
drift apart. Installed components — tires, batteries, filters — turn a repeated
measurement into a wear rate, and a tire is condemned on its DOT date code even
when the tread still looks fine.

**Inspections.** Digital vehicle inspections from templates, with positional
measurements and thresholds. Two brake-pad measurements taken months apart
become a projected replacement date. A pre-purchase inspection can be run on a
vehicle you do not own yet.

**Parts, inventory and purchasing.** A catalog with cross-references and
fitment recorded as *confirmed* or as *stated by the vendor* — because those are
different claims. FIFO lots, locations, a stock ledger and cycle counts.
Vendors, partial receiving, landed cost and core charges. A supplier's order
confirmation PDF can be read straight into the catalog, prices and all.

**Diagnostics.** Scan-tool reports are imported, reviewed and confirmed rather
than trusted. Extraction is driven by YAML [parser profiles](Artifacts/SCHEMA-PARSER-PROFILES.md)
that can be shared and installed, so a new tool's format is a file rather than a
release; historical reports can be re-parsed once a better profile exists. A
trouble-code dictionary works offline, a code becomes a job item, and it is
flagged if it comes back. An ELM327 can be read directly over Web Serial
(Chromium only).

**Documents and photographs.** Deduplicated by content hash, thumbnailed in the
background, OCR'd where there is text to find — including a photographed
printout from a battery tester that only prints paper. GPS EXIF is stripped by
default, because a photo of a car in a driveway geotags a home address.

**Reports.** A per-vehicle PDF that is the document you hand a buyer, with
sensitive specs — key codes, radio codes — deliberately kept out of it. Spend,
time and inventory value across the shop, each exportable as CSV.

**In the garage.** An installable PWA with offline pages and a write queue that
merges conflicts rather than dropping work. On-device scanning for VIN
barcodes off a door jamb, part barcodes and bin labels (Chromium). Printable QR
labels for bins and vehicles.

**Your data.** Backup, retention and restore; a portable export that is
newline-delimited JSON plus ordinary files, readable without this application.
CSV import for vehicles, parts and service history, and a
[LubeLogger](Artifacts/INTEGRATION-LUBELOGGER.md) importer with optional
scheduled pull. A REST API with OpenAPI docs at `/api/v1/docs`.

A few integrations need something of your own and say so in the interface:
licence-plate lookup needs a provider account you supply, and
[WrenchLedger](Artifacts/INTEGRATION-WRENCHLEDGER.md) tool availability needs a
WrenchLedger account.

## Quick start

```bash
cp .env.example .env      # edit SECRET_KEY and the passwords
docker compose up -d
```

Then open the site. **A new instance ships with no accounts, on purpose** —
there is no default login to forget to change — so it sends you to a setup page
that makes the first one, names the shop, sets units and timezone, and tells you
how to get another device to trust the certificate. It runs once and is
unreachable afterwards, and comes back any time you start from an empty database
volume.

> **TLS is not optional.** Service workers, the camera and Web Serial all refuse
> to run outside a secure context, so Caddy terminates TLS for the stack.
>
> With a domain name, set `TLS_MODE=acme-dns` for a real Let's Encrypt
> certificate through a DNS challenge — **nothing to install on any device, and
> the instance stays off the internet.** A free dynamic-DNS name does as well as
> one you own. Otherwise `internal` works with no prerequisites, at the cost of
> installing a root certificate on every device that opens the site.

## Documentation

| | |
| --- | --- |
| [docs/INSTALL.md](docs/INSTALL.md) | From nothing to an instance you can sign in to, including reaching it from another machine on the LAN. |
| [docs/HELP.md](docs/HELP.md) | Day-to-day use. Describes what the application does today, not what the spec asks for. |
| [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md) | Running it without Docker, and the two `.env` values a laptop needs. |
| [Artifacts/](Artifacts/README.md) | The design documents: [SPEC.md](Artifacts/SPEC.md) holds the requirements, the rest are contracts and authoring guides. |
| [catalog/](catalog/README.md) | Shared service schedules, inspection checklists and parser profiles, and how to contribute one. |
| [PRIVACY.md](PRIVACY.md) | What leaves the instance, and what stops it. |

## Principles

Five decisions shape the rest of the code.

1. **Local-first.** The instance is authoritative. External services are
   accelerators, never dependencies, and one Offline Mode switch stops all
   outbound traffic.
2. **Never lose a capture.** Photos, notes and readings are append-only, so a
   phone in the garage cannot lose work to a sync conflict.
3. **Semi-lightweight.** Postgres does search *and* the job queue. No
   Elasticsearch, no Redis, no broker.
4. **Own your data.** The export must be readable without this application.
5. **Degrade, don't block.** A failed VIN lookup never prevents recording the
   vehicle by hand.

## Privacy

No telemetry, no analytics, no phone-home — including update checks. Every
outbound host is on an allowlist, and Offline Mode refuses all of them. See
[PRIVACY.md](PRIVACY.md).

## Status

This is one garage's tool, published in case it is useful to another. Interfaces
may change; the export exists so that never traps you.
