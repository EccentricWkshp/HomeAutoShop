# HomeAutoShop — document set

Design documentation for a self-hosted, local-first application managing a home/DIY shop working on personally owned vehicles and equipment.

## Reading order

**Start here.** [SPEC.md](SPEC.md) is the product and technical specification and the only document that defines requirements. Everything else is detail it points to.

| Document | What it is | Read it when |
| --- | --- | --- |
| **[SPEC.md](SPEC.md)** | Goals, principles, architecture, domain model, ~150 numbered requirements, NFRs, security, phasing, resolved decisions, roadmap | Always. Start at §1–§4 for intent, §6–§7 for the model and requirements, and **§15 / §15.1 / §15.2 — the plan, what was built, what remains**. |
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

Each document carries its own version and date in its header. Repeating them
here only produced a second copy to forget to update, so this table says what
state each one is in and nothing a header already answers.

| Document | Status |
| --- | --- |
| SPEC.md | **Phases 1–4 built, and R-1, R-2, R-5 through R-10 with them.** Status lives in three sections and nowhere else: §15 is the plan, **§15.1 what shipped** and where the implementation decided differently, **§15.2 what remains** — seven deferred items, six claims the code does not answer, and no open questions. §19 keeps the record of claims this document made in the present tense that were not true, and how each was found; two of those turned up by building a roadmap item on top of the claim. |
| INTEGRATION-WRENCHLEDGER.md | Draft — WL-Q1–Q11 all resolved against source and a live workspace. Built: the readiness gate. Not built: the webhook receiver and the three optional Shop-plan surfaces. |
| INTEGRATION-LUBELOGGER.md | Draft — LL-Q1–Q3 open. Built: one-time import and scheduled pull. |
| SCHEMA-PARSER-PROFILES.md | **Engine implemented**, and the seven profiles in `catalog/profiles/` are written against it. The contract stays a draft where it describes something no profile has needed yet. |
| SCHEMA-INSPECTION-TEMPLATES.md | Draft. |
| REFERENCE.md | Draft. |

## Open questions

**None are open in SPEC.** OQ-1 through OQ-19 are answered, §16 keeps each decision with its consequence, and §15.2 records that nothing is outstanding — OQ-17 was the last, and it closed as WL-Q4 in the WrenchLedger document rather than in SPEC, which is why it outlived itself on the list. `WL-Q1–Q12` are likewise all answered. **`LL-Q1–Q3` in the LubeLogger document are the only live questions**, and none blocks anything.

Nothing is blocked on an answer — every question carries a stated default. The one exception has resolved itself: the XTOOL D8 parser profile could not be written until a real report existed, nine arrived, and it is written. Six of the assumptions it was going to be built on turned out to be wrong, which is the argument for having waited.
