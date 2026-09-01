# Parser Profile Schema — scan tool report ingestion

|  |  |
| --- | --- |
| **Document** | `Artifacts/SCHEMA-PARSER-PROFILES.md` |
| **Status** | Draft for review |
| **Version** | 0.3.0 |
| **Date** | 2026-09-01 |
| **v0.3.0 changes** | §1 said the right moment to freeze this contract is when a second tool's reports arrive. They have — fourteen tools' worth, gathered from the public web (see the [corpus README](samples/scan-reports/README.md)) — and new §1a records what four working profiles needed that the contract could not express: `multiline` rows, a column with a fallback `group`, `map` as a closed vocabulary, and a fallback profile that stops outranking a specific one. §2 is rewritten around the corpus as it now is. |
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

## 1a. What four more tools added to the contract

The schema above was written against one format. Four more profiles now ship in the catalog — Ross-Tech VCDS, Autel MaxiSys, BlueDriver, Car Scanner ELM OBD2 — written against reports gathered from the public web, and they needed four things the original contract had no way to express. Each was added because a real format demanded it, and each is measured rather than anticipated.

### `media_type: text` is the common case, not the exception

Three of the four read text rather than word geometry, and one of those is a PDF. §8.3a's finding was that *the D8* needs geometry — its section boundaries are colours — and that finding does not generalize: Autel labels every section in words and states the code status in a vocabulary, so its PDF is read as text and the same profile keeps working if the operator has a CSV export instead. **Word geometry is what a format costs you, not what a parser deserves.**

### `multiline: true` — a row may be printed across lines

```yaml
table_extractor:
  multiline: true
  # The vendor fault line, then the J2012 line under it where there is one.
  # The newline is written into the pattern; nothing splits the body into rows.
  row_pattern: '^(\d{5,8})\s*-\s*(.+?)[ \t]*\r?\n[ \t]*(?:([PBCU][0-9A-F]{4}) ... )?'
  columns:
    - { role: code, group: [3, 1] }   # J2012 where there is one, vendor's otherwise
    - { role: description, group: 2 }
```

Rows were matched one line at a time. A VCDS Auto-Scan states a fault as two:

```
000772 - Cylinder 4
               P0304 - 000 - Misfire Detected - Intermittent
```

A line-at-a-time reader has to pick one and is wrong either way. **Measured across nine real Auto-Scans: 191 faults, 30 of them carrying a J2012 code.** Reading only the indented line reports five faults in six as absent; reading only the first throws away the code a person can look up. Hence `multiline`, and hence a column's `group` accepting a *list* — the first group that matched wins, so one column can say "the standard code, or the vendor's if that is all there is".

`multiline` also carries every PDF profile, for an unrelated reason: **a page of extracted PDF text is a single line**, so `^` and `$` match once per page and a row cannot be anchored to one. Rows are found by shape, ended by the status vocabulary.

### `map` is a closed vocabulary

An unrecognized value used to pass straight through. Harmless for a description; not for `state`, which is a four-value field twelve characters wide. Car Scanner reports a status as the DTC status bits written out — `Confirmed, Test failed since last DTC clear, Warning indicator requested` — and sixty characters of prose went in as a state. A value outside the map now leaves the row default standing, or `map_default` if the profile names one, and the tool's own wording survives in `state_raw`, which is deliberately never mapped.

### A fallback profile does not compete with a specific one

`detect` ranked on score alone. `Generic code list` claims any text with a trouble code in it — deliberately — and scores **1.0** on a VCDS Auto-Scan where the VCDS profile scores 0.85, because the older Beta builds omit one of its four signals. Three real Auto-Scans went to the fallback, which read *nothing* out of reports holding 61, 14 and 0 faults. A profile naming a `tool_vendor` or `tool_model` now outranks one naming neither, provided it clears its own threshold — which is exactly the claim its author makes by publishing it. Among profiles that both name a tool, the score decides as before.

### What a declarative profile still cannot do

**Attribute a code to its module.** Autel, THINKCAR and TOPDON all print the module as a heading above a group of rows, and the table extractor matches a row at a time with no notion of the section it sits in — so a code arrives correct and unattributed, and a nine-module all-system scan imports as one list. Car Scanner's text export puts `ECU:` inside each fault block and is read correctly, which is a fair argument for §8.3b's preference for a text export where a tool offers one.

**Read a report whose columns interleave.** THINKCAR's newer PDF wraps the status cell around the code — `Curren … EOBD/OBD II P0A80 Replace Hybrid/EV Battery Pack … t` — and TOPDON embeds a second identifier mid-description. No profile is published for either: half-reading a diagnostic report is worse than not reading it, which is the catalog README's own argument. Their captures are in the corpus, marked unread, waiting for whoever wants them.

## 2. Development workflow for a new profile

```
Artifacts/samples/scan-reports/
  xtool d8/
    originals/Silverado202606181500.pdf     ← the report, git-ignored
    Silverado202606181500.words.json        ← redacted capture, committed
    Silverado202606181500.expected.json     ← golden fixture, committed
  ross-tech vcds/
    golf-mk6-auto-scan.text.json
    golf-mk6-auto-scan.expected.json
    sources.json                            ← where it came from
```

1. Get real reports into the corpus — `capture_fixture` for one of your own, `fetch_scan_samples` and `capture_scan_samples` for the public ones. Both redact.
2. Iterate the profile YAML against the captures until it reads them.
3. Name the reports in `verified_against`. `build_catalog` **runs** the profile against each and refuses to publish a file that cannot read them, so the badge is a fact rather than a claim.
4. The fixtures stay in CI forever — a profile change that breaks an older report fails the build.

**Sample coverage that actually matters**, in priority order: (1) *no codes found* — the empty-table case breaks naive table parsers; (2) *several stored codes* — the normal case; (3) *codes plus freeze frame and readiness monitors* — the multi-section case; (4) a multi-page report. Three files covering 1–3 are enough to write a profile that holds up; one file tends to overfit to incidental layout, which is why the verified badge needs two from different vehicles.
