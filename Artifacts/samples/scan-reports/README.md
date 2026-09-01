# Scan tool report samples

The test corpus that parser profiles are developed against (SPEC.md §8.3a).

## What is committed, and what is not

**The reports themselves are not in this repository.** A scan report carries a
VIN, and often an odometer reading and the serial of the tool that produced it —
a durable pointer to one person's vehicle. This repository is public, and a
public repository is forever: a VIN committed once survives in the history until
somebody rewrites it and tells every clone.

So what ships is **captured word geometry**: every word from the report with its
position and color, with identifying values rewritten. The parser consumes
words rather than documents, so the corpus loses nothing that matters — the
wrapped labels, the colored banners, the status line that renders above its own
row are all preserved exactly.

```text
scan-reports/
  tool-model/
    <report>.pdf            ← yours, git-ignored, never committed
    <report>.words.json     ← redacted capture, committed
    <report>.expected.json  ← golden fixture, committed
  synthetic-vins.json     ← the stand-ins in use, so the guard knows them
```

## Adding a sample

```bash
python manage.py capture_fixture path/to/report.pdf --tool "xtool d8"
python manage.py test homeautoshop.scantools
```

One command writes both halves — the redacted capture and the expected output —
into the folder `--tool` names, creating it if this is the first report from
that scanner. The folder is the only record of what produced a report, so it is
required rather than guessed.

Read the fixture diff before committing: a fixture regenerated without looking
is a test that has been switched off.

**Redaction is a rule, not a list**, so a new sample is protected the moment it
is captured rather than when somebody remembers. Anything VIN-shaped whose ISO
3779 check digit validates is replaced by a stand-in that keeps the
manufacturer, model descriptor, model year and plant — the parts shared with
millions of vehicles, and the parts that make a sample representative — while
the serial is derived from a digest of the original and the check digit
recomputed. Tool serial numbers are blanked. Nothing anywhere stores an
original.

Make and model stay visible. They are in the file names and the report bodies,
and hiding them would stop the corpus exercising make-specific parsing.

A second test asserts the corpus is **there** and filed this way — at least
five captures, each under a tool folder, each with its expected output beside
it. That guard exists because the layout above was specified here long before
the code followed it: the reports were moved into per-tool folders and
`fixtures.samples()` kept using a flat pattern, so it matched nothing, and the
whole suite stayed green while every parser test in it iterated an empty list.

A test walks the whole tree on every run and fails on any VIN that validates and
is not a known stand-in. It has already caught two mistakes: a substitution
table that stored the real values it was replacing, and a real tool serial used
as sample input in a test. That is the argument for having it.

## What to capture (priority order)

| # | Case | Why it matters | Covered |
| --- | --- | --- | --- |
| 1 | **No codes found** | The empty-table case breaks naive parsers, and it is the most common real report. | ✅ `Silverado202606181500` |
| 2 | **Several stored codes** | The normal case — establishes the DTC table shape. | ✅ `F150202404071738` |
| 3 | **Multi-section** | Proves section boundaries are detected. | ✅ `Silverado202504120859` (ECU + codes + live data) |
| 4 | **Multi-page** | Page-spanning tables are where extraction usually fails. | ✅ `Silverado202309291358` (13 pages, 395 rows) |
| 5 | **Readiness monitors** | Modeled in the schema, never yet seen. | ❌ still wanted |

Nine reports, five vehicles, 2023–2026. A single sample overfits to incidental
layout; this many is what caught `P219A`, `0mile`, and a status cell that
renders above the row it belongs to.

## Other samples in this folder

The battery-tester photographs one level up (`Artifacts/samples/*.jpg`) are
git-ignored for the same reason and are not yet parsed — they are thermal
receipts photographed at an angle, so they need OCR rather than text
extraction.
