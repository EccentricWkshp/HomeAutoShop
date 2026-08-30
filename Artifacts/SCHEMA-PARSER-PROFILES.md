# Parser Profile Schema — scan tool report ingestion

|  |  |
| --- | --- |
| **Document** | `Artifacts/SCHEMA-PARSER-PROFILES.md` |
| **Status** | Draft for review |
| **Version** | 0.2.0 |
| **Date** | 2026-08-29 |
| **Parent spec** | [SPEC.md](SPEC.md) |
| **Implements** | SPEC.md §8.3a (`FR-INT-4`–`FR-INT-7`), entity `parser_profile` |
| **Sample corpus** | [`samples/scan-reports/`](samples/scan-reports/README.md) |

---

## 1. The contract

A parser profile is **data**. This is the contract the extraction engine implements (SPEC §8.3a); writing one requires no code and no deploy.

> **Status: implemented, still a draft, and the first real profile still does not use it.**
>
> The engine ships (`homeautoshop/diagnostics/engine.py`). A declarative profile
> is genuinely data: the bundled `Generic code list` profile is the YAML below
> and reads a DTC table out of a plain-text or CSV export with no code involved
> and no deploy. Fingerprints score over the whole document, field extractors
> are label-anchored or regex, and every pattern is compiled at import time so a
> broken one is refused where the error can still name the field.
>
> The D8 is the other case, and the schema now models it honestly:
> `parser_profile.engine` may name a **built-in parser** instead of carrying
> rules. That is not a retreat from "a profile is data" — it is the boundary of
> it, and the boundary is drawn by the format, not by convenience. See SPEC
> §15.1.
>
> The XTOOL D8 parser in `homeautoshop/scantools/` is written as code with its
> patterns and vocabularies declared together at the top of the module, so that
> lifting them into YAML is mechanical *if a future format makes that worth
> doing*. That was deliberate. Six of the
> assumptions below were written before any sample existed and turned out to be
> wrong, and generalizing a schema from a single format would have encoded that
> format's accidents as though they were the shape of the problem. The right
> moment to freeze this contract is when a second tool's reports arrive.
>
> What nine real reports showed, against what was assumed:
>
> | Assumed | Measured |
> | --- | --- |
> | `pdf_metadata` fingerprint on `/Producer` | the tool writes **no metadata at all**; the signal can never fire |
> | `label_anchored` works on extracted text | text order puts the footer first and every label *after* its value. The labels do precede their values **visually** — so label anchoring is right, but only against word coordinates |
> | ASCII hyphens | **U+2011** throughout: dates, `F‑150`, `B1352‑20`. Normalize on the way in or every pattern silently misses |
> | DTC matches `[PBCU][0-9A-F]{4}` | correct, and a narrower `\d{4}` is not — `P219A` is a real code. Codes also carry a failure-type byte: `B1352‑20` |
> | states are Stored/Pending/Permanent/History | also `CMDTCs(Storage Trouble Code)`, `ODDTCs(Request Trouble Code)`, and GM's three separate outcomes (`Last test`, `This ignition`, `Since Clear`) |
> | readiness monitors are the optional extra section | **no sample has readiness.** They all have **Live Data**: name, value, maximum, minimum, unit — which the schema does not model |
>
> Two structural facts no field-extractor vocabulary would have captured:
>
> * **Sections are colored banners, and a table belongs to the banner above it.**
>   Module names are white on blue, section headings dark blue, field labels
>   gray. Color separates them exactly; position does not, because a wrapped
>   label starts at the same x as a banner. A report listing nine modules and
>   printing two tables is saying seven modules are clean.
> * **A cell's first line can render above its own row.** GM prints
>   `Last test:Passed` a row-height above the row number it belongs to, so a
>   parser that only ever appends to the current row loses it.

```yaml
# profiles/xtool-d8-dtc-report.yaml
name: XTOOL D8 — DTC report
tool_vendor: XTOOL
tool_model: D8
version: 1
media_type: pdf

# Regexes use single-quoted YAML scalars: YAML processes escapes inside
# double quotes, which would eat the backslashes.

# --- identification -------------------------------------------------------
# Scored match; the highest-scoring profile above `threshold` wins.
# Scored over the WHOLE document: the disclaimer this tool prints is not always
# on page 1, and a page-scoped signal missed it on four reports in nine.
fingerprint:
  threshold: 0.7
  signals:
    - { kind: doc_text, pattern: '(?i)this report is only responsible', weight: 0.25 }
    - { kind: doc_text, pattern: '\bD8-\d{6}\b',                       weight: 0.25 }
    - { kind: doc_text, pattern: '(?i)vehicle\s+information',           weight: 0.25 }
    - { kind: doc_text, pattern: '(?i)mileage\s*:',                     weight: 0.25 }
# Not usable for this tool, kept as a warning: `pdf_metadata` matches nothing,
# because the D8 writes no metadata. `Diagnosis Route` looks like an obvious
# signal and never fires — its two words are drawn far enough apart that text
# extraction never joins them.

# --- scalar fields --------------------------------------------------------
# Each extractor yields value + confidence + source (page, char offset) so the
# review screen can point at where a value came from.
field_extractors:
  vin:
    strategy: label_anchored          # find the label, take what follows
    labels: ["VIN", "VIN Code", "Vehicle Identification Number"]
    pattern: '([A-HJ-NPR-Z0-9]{17})'
    validate: vin_check_digit         # reuse §5.5 local validation
    required: true
  odometer:
    strategy: label_anchored
    labels: ["Odometer", "Mileage", "ODO"]
    pattern: '([\d,.]+)\s*(mi|miles|km)?'
    coerce: { type: measurement, default_unit: mi }
  performed_on:
    strategy: label_anchored
    labels: ["Date", "Report Date", "Test Date"]
    coerce: { type: datetime, formats: ["MM/dd/yyyy HH:mm", "yyyy-MM-dd"] }
  vehicle_description:
    strategy: label_anchored
    labels: ["Vehicle", "Model"]
  tool_version:
    strategy: regex
    pattern: '(?i)software\s*(?:version)?[:\s]+([\d.]+)'

# --- the DTC table --------------------------------------------------------
table_extractor:
  locate:
    strategy: heading_then_rows
    headings: ["Trouble Code", "DTC", "Fault Code", "Code"]
    stop_at: ["Readiness", "Live Data", "End of Report"]
  row_pattern: '^\s*([PBCU][0-9A-F]{4})\s+(.+?)\s{2,}(.*)$'
  columns:
    - { role: code,        group: 1, validate: dtc_format }
    - { role: description, group: 2 }
    - { role: state,       group: 3, map: { Stored: stored, Current: stored,
                                            Pending: pending, Permanent: permanent,
                                            History: history } }
  row_filters:
    drop_if_matches: ['(?i)no (fault|trouble) codes? (found|detected)']

# --- readiness monitors (optional) ---------------------------------------
readiness_extractor:
  strategy: label_value_pairs
  section_headings: ["Readiness", "I/M Readiness", "Monitor Status"]
  value_map: { Complete: complete, Incomplete: incomplete, "N/A": not_supported }

# --- how confident must we be to skip nothing in review? -----------------
review:
  always_review: true                 # per §8.3a — extraction never auto-commits
  flag_below_confidence: 0.85
```

## 2. Development workflow for a new profile

```
Artifacts/samples/scan-reports/
  xtool-d8/
    2026-08-29_no-codes.pdf            ← sample report
    2026-08-29_no-codes.expected.json  ← golden fixture (hand-verified once)
    2026-08-29_three-stored.pdf
    2026-08-29_three-stored.expected.json
```

1. Drop a real report into the corpus.
2. Hand-write the expected JSON once, verifying it against the PDF by eye.
3. Iterate the profile YAML until the parser reproduces the fixture.
4. The fixture stays in CI forever — a profile change that breaks an older report fails the build.

**Sample coverage that actually matters**, in priority order: (1) *no codes found* — the empty-table case breaks naive table parsers; (2) *several stored codes* — the normal case; (3) *codes plus freeze frame and readiness monitors* — the multi-section case; (4) a multi-page report, if the D8 produces one. Three files covering 1–3 are enough to write a profile that holds up; one file tends to overfit to incidental layout.
