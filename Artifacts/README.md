# HomeAutoShop — document set

Design documentation for a self-hosted, local-first application managing a home/DIY shop working on personally owned vehicles and equipment.

## Reading order

**Start here.** [SPEC.md](SPEC.md) is the product and technical specification and the only document that defines requirements. Everything else is detail it points to.

| Document | What it is | Read it when |
| --- | --- | --- |
| **[SPEC.md](SPEC.md)** | Goals, principles, architecture, domain model, ~150 numbered requirements, NFRs, security, phasing, resolved decisions, roadmap | Always. Start at §1–§4 for intent, §6–§7 for the model and requirements, **§15.1 for what is actually built**. |
| [REFERENCE.md](REFERENCE.md) | Work order state machine · seed data · glossary | Looking up a term or a lifecycle rule. |
| [SCHEMA-PARSER-PROFILES.md](SCHEMA-PARSER-PROFILES.md) | YAML contract for scan-tool report extraction, and why the D8 does not use it | Writing or debugging a parser profile. Implements SPEC §8.3a. |
| [SCHEMA-INSPECTION-TEMPLATES.md](SCHEMA-INSPECTION-TEMPLATES.md) | YAML contract for DVI templates, points, positions, and thresholds | Authoring an inspection template. Implements SPEC §7.8. |
| [INTEGRATION-LUBELOGGER.md](INTEGRATION-LUBELOGGER.md) | Modes, API facts, entity mapping, sync semantics | Building the LubeLogger import. Detail behind SPEC §8.6. |
| [INTEGRATION-WRENCHLEDGER.md](INTEGRATION-WRENCHLEDGER.md) | Boundary, transport, verified API facts, caching limits, house placement | Building the WrenchLedger integration. Detail behind SPEC §8.7. |
| [samples/scan-reports/](samples/scan-reports/README.md) | Scan-tool report corpus and fixture conventions | Contributing a sample report. |

## Why the split

The base spec defines what the application must do and changes when *we* decide something. The companion documents describe contracts with things outside our control — another product's API, a tool vendor's report format — and change on **someone else's** schedule. Keeping them separate means an upstream API revision does not produce a diff against the requirements.

The two `SCHEMA-*` documents are authoring guides: they are opened by whoever is writing a profile or a template, not by whoever is reading the requirements.

## Status

| Document | Version | Status |
| --- | --- | --- |
| SPEC.md | 0.6.0 | **Phases 1–4 built** — §15.1 records what shipped and where the implementation decided differently |
| INTEGRATION-WRENCHLEDGER.md | 0.4.0 | Draft — WL-Q1–Q11 all resolved against source and a live workspace. Built: the readiness gate. Not built: the webhook receiver and the three optional Shop-plan surfaces |
| INTEGRATION-LUBELOGGER.md | 0.1.0 | Draft — LL-Q1–Q3 open. Built: one-time import and scheduled pull |
| SCHEMA-PARSER-PROFILES.md | 0.2.0 | **Engine implemented**; contract still a draft, and deliberately so until a second tool's reports arrive |
| SCHEMA-INSPECTION-TEMPLATES.md | 0.1.0 | Draft |
| REFERENCE.md | 0.1.0 | Draft |

## Open questions

Consolidated in SPEC §16.2 (OQ-17 onward), with integration-specific questions held in their own documents: `WL-Q*` in the WrenchLedger document, `LL-Q*` in the LubeLogger document.

Nothing is blocked on an answer — every question carries a stated default. The one exception has resolved itself: the XTOOL D8 parser profile could not be written until a real report existed, nine arrived, and it is written. Six of the assumptions it was going to be built on turned out to be wrong, which is the argument for having waited.
