# Scan tool report samples

The test corpus that parser profiles are developed against (SPEC.md §8.3a).

It comes from two places, and the difference matters. Nine reports were made in
this shop with an XTOOL D8. A hundred and thirty-four more were **published on
the public web** by other people, gathered by `fetch_scan_samples` from the
manifests in this folder, and cover fourteen more tools.

## What is committed, and what is not

**No report is in this repository.** A scan report carries a VIN, and often an
odometer reading, a licence plate, a workshop code and the serial of the tool
that produced it — a durable pointer to one person's vehicle. This repository
is public, and a public repository is forever: a VIN committed once survives in
the history until somebody rewrites it and tells every clone. The reports
gathered from the web are somebody else's besides, published to be read rather
than for us to redistribute.

So what ships is a **capture**: for a PDF, every word with its position and
colour; for a photograph, every word OCR found with its box and its confidence;
for anything else, the text. Identifying values are rewritten on the way in. The
parser consumes words rather than documents, so the corpus loses nothing that
matters — the wrapped labels, the coloured banners, the status line that renders
above its own row are all preserved exactly.

**A photograph's capture is OCR's own output and never a transcription.** It
says so in `read_by`, and the suite refuses a capture that says anything else.
Writing the words out by hand looks reasonable when the JPEG cannot be committed
and the machine in front of you has no Tesseract on it, and it produces a record
of what somebody *imagined* OCR does. Every hard case in the BT600 Plus format
is a case where OCR does something surprising — `850CCA(CCA)` read as
`BSOCCA(CCA)`, a value landing on the line above its own label, a section banner
torn in half — and a transcription would have passed on all of them.

Capture a photograph inside the application container, which has Tesseract:

```sh
docker compose run --rm app python -m homeautoshop.scantools.capture     "Artifacts/samples/scan-reports/topdon bt600 plus/20260830_105614.jpg"
```

```text
scan-reports/
  tool-model/
    originals/                ← fetched or yours, git-ignored, never committed
    <report>.words.json       ← redacted capture of a PDF's word geometry
    <report>.text.json        ← redacted capture of a text or CSV report
    <report>.expected.json    ← golden fixture, committed
    sources.json              ← where the fetched ones came from, committed
  synthetic-vins.json     ← the stand-ins in use, so the guard knows them
```

`originals/` is ignored as a **whole directory** rather than by extension. Your
own reports are a PDF from a scan tool or a JPEG from a phone, so a list of
extensions covers them; a file off the public web has whatever extension its
publisher felt like, and one unanticipated `.dat` would be in a public
repository forever.

## Adding a sample of your own

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

## Adding samples from the public web

```bash
python manage.py fetch_scan_samples --dry-run     # what would be pulled
python manage.py fetch_scan_samples               # pull it
python manage.py capture_scan_samples             # redact, capture, audit
```

The first reads the `automotive_scan_tool_public_examples*.json` manifests in
this folder — research notes, git-ignored, and `.gitignore` says why — and
downloads what they name into `<tool>/originals/`, one request at a time with a
real User-Agent and a pause between requests to the same host. It writes
`sources.json` beside the folder: vendor, digest, size, licence and the URL, so
the attribution survives and a later copy can be checked against this one.

It does not pull everything the manifests list. Of 402 entries, 203 name a file
a program can fetch, and 177 of those are worth having: reports, configuration
dumps, protocol captures and live-data exports that arrive as text. The rest
are proprietary logger containers — `.xrk`, `.mlg`, `.daq` — that this
application will never own a decoder for. **A corpus is not improved by being
larger**; 81 of the entries are one research dataset of the same car driving
the same road, and pulling them in would bury the thirty-odd real diagnostic
reports that are the point.

The second command captures and redacts, then **audits what it wrote** and
prints anything that still looks like somebody's details. Read that before
committing. It runs alone as `capture_scan_samples --audit`.

## Redaction is a rule, not a list

So a new sample is protected the moment it is captured rather than when
somebody remembers.

**VINs, by two rules rather than one.** Anything VIN-shaped whose ISO 3779
check digit validates is replaced wherever it appears, and anything VIN-shaped
that *follows a VIN label* is replaced whatever its check digit says. The
second rule exists because position 9 is a check digit only where a regulator
requires one: a VIN issued in Europe carries a filler there, so
`WVWZZZ1K…` — a real Golf, in a real VCDS Auto-Scan — fails the check, and the
check-digit rule on its own would have published every European VIN in this
folder. A stand-in keeps the manufacturer, model descriptor, model year and
plant — the parts shared with millions of vehicles, and the parts that make a
sample representative — while the serial is derived from a digest of the
original, and the filler is preserved where there was one.

**Labelled values.** A licence plate, a customer, a technician, a shop name, an
address, a telephone number and an e-mail address are removed; a workshop code,
a tester serial and a module coding id are zeroed, keeping their shape because
that is what a parser matches on. Nothing about the shape of `raffi` says it is
a person — the `User:` above it does.

**In word geometry, a label labels what is printed beside it.** Blanking one
word after the label published four fifths of a shop's name, so blanking runs
to the end of the printed line and stops at the next label. Blanking on reading
order alone zeroed a D8 section heading in all nine reports, because the tool
emits `SN:` and then, three lines down the page, `Diagnosis` — so the geometry
decides, not the word order.

Make and model stay visible. They are in the file names and the report bodies,
and hiding them would stop the corpus exercising make-specific parsing.

A test walks the whole tree on every run and fails on any VIN that validates
and is not a known stand-in. It has caught four mistakes now: a substitution
table that stored the real values it was replacing, a real tool serial used as
sample input in a test, a VIN in a `sources.json` title, and a VIN quoted in
the docstring explaining that last one.

## Fixtures

Each capture has an `.expected.json` beside it, and the suite fails if the
parser stops reproducing one. There are two shapes, because there are two kinds
of parser:

* A capture under a folder a **built-in parser** reads — only `xtool d8`, and
  `engine.py` explains why it needs one — records a whole `ScanReport`.
* Everything else records what the best-scoring available profile extracted,
  and the profile's name with it.

A capture no profile reads yet gets `{"unread": true}`, which is not a
placeholder. 111 of them say plainly that nobody has written a profile for that
format. Almost all are consumer OBD-app *logs* — a time series rather than a
set of readings — and 81 are one research dataset of a single car driving the
same road. And if a new profile's fingerprint is ever loose enough to start
claiming those 81, that fixture diff is what says so.

**A PDF is read as the lines it was printed as**, not in the order the words
come out of it. That is a property of the corpus as much as of the parser: it
is what makes `fixtures.lines` and the redaction audit agree with each other
about the shape of a page, and it is why a report whose column wraps —
`EOBD/OBD II P0A80 Replace Hybrid/EV Battery Pack`, split into three fragments
by extraction order — can be read at all.

## Files deliberately not captured

`not-captured.json` names fetched files that are **not** turned into captures,
with the reason beside each — 37 of them. A capture is committed, so anything
no parser here will ever read is kept out by name rather than by somebody
remembering. Their provenance stays in `sources.json` and the file stays in
`originals/`, so nothing is lost and a line can be deleted to change the mind.

Most are **race and tuning loggers** — MegaSquirt, COBB, Haltech, MHD,
RomRaider, Woolich, Zeitronix, RaceChrono and a dozen more — and they share one
reason. They are time series, not readings. This application models live data
as a reading with a value, a minimum and a maximum, which is what a scan tool
prints during a session, and shows it that way on the session screen. A
datalog of ten thousand rows of RPM against time is a different artifact for a
different job: collapsing it to a minimum and a maximum throws away the only
thing it is for, and keeping the series is something neither the schema nor the
screen can hold. SPEC NG-4 puts scan data on the per-session side of that line.

The consumer OBD-app logs are still here — Torque, OBDLink, OBD Auto Doctor —
and they are the same shape. The line drawn is *could this application ever
read it*: a phone app logging an OBD session is a plausible future import; a
standalone racing ECU's telemetry is not.

A PDF with **no text layer** is not written as an empty capture either. It goes
to OCR, and if that reads nothing the report is reported as unreadable rather
than committed — a fixture over nothing passes every test there is. One report
in the corpus is in that state today: a Techstream printout scanned into a
recall filing, which needs `OCR_ENABLED` and tesseract to capture.

## What to capture (priority order)

| # | Case | Why it matters | Covered |
| --- | --- | --- | --- |
| 1 | **No codes found** | The empty-table case breaks naive parsers, and it is the most common real report. | ✅ `Silverado202606181500`, `lexus-lx570…` |
| 2 | **Several stored codes** | The normal case — establishes the DTC table shape. | ✅ `F150202404071738` and most of the fetched set |
| 3 | **Multi-section** | Proves section boundaries are detected. | ✅ `Silverado202504120859` (ECU + codes + live data) |
| 4 | **Multi-page** | Page-spanning tables are where extraction usually fails. | ✅ `Silverado202309291358` (13 pages, 395 rows) |
| 5 | **Readiness monitors** | Modeled in the schema, never yet seen. | ❌ still wanted |
| 6 | **A second tool per format family** | The verified badge needs two, and four profiles are stuck at one. | ❌ TOPDON, Carly, BlueDriver, Car Scanner |
| 7 | **Readiness monitors** | See row 5. | ❌ |

A single sample overfits to incidental layout; several is what caught `P219A`,
`0mile`, a status cell that renders above the row it belongs to, and — from the
fetched set — the fact that a VCDS Auto-Scan numbers five faults in six in
Ross-Tech's own vocabulary rather than J2012's.

## Other samples in this folder

The battery-tester photographs one level up (`Artifacts/samples/*.jpg`) are
git-ignored for the same reason and are not yet parsed — they are thermal
receipts photographed at an angle, so they need OCR rather than text
extraction.
