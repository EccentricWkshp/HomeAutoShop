# The shared template catalog

Service schedules, inspection checklists and parser profiles that other people
can install. Implements SPEC §17 R-1 / §8.1b.

An instance points here by default — `CATALOG_URL` needs no configuring —
and reads it only when somebody opens **Templates and checklists → Browse
the catalog** and presses a button. There is no background check, nothing on
start-up, and nothing here is required for the application to work — the
bundled templates ship in the image, and this is additive.

## Contributing

1. Drop your `.yaml` file in `schedules/`, `checklists/` or `profiles/`.
2. Put an `author:` line in it — a name or handle, whatever you want beside
   your work. It shows on the browse screen and stays with the template after
   somebody installs it, because *who said these intervals were right* is the
   question a stranger's schedule raises.
3. Run `python manage.py build_catalog`.
4. Open a pull request.

That is the whole workflow. The file you add is the file an instance reads —
plain YAML, exactly as exported from a running shop, with nothing to escape or
wrap. If you have a template you like, **Templates → Export** gives you a file
that can be committed as-is.

`build_catalog` writes `index.json` for you, taking each entry's name and
description out of the file itself so the index cannot disagree with what it
points at. It is generated, never hand-edited: an index somebody had to
remember to update is one that silently publishes nothing when they forget.

**It validates while it walks.** Every file is parsed by the same validator
that will run on an operator's instance, and a file that would not import
fails the command with the reason and the line. The test suite runs
`build_catalog --check`, so a stale index or a broken template fails the suite
rather than shipping. That check is worth more than the index-writing: it moves the first
failure from somebody's garage to the pull request, while the person who wrote
it is still looking at it.

## Why parser profiles are published rather than bundled

There are hundreds of scan tools and you own one. Shipping a profile for every
format anybody has ever written would mean an operator scrolling an expansive,
almost entirely inapplicable list to find the two lines that matter to them —
and every profile in it would be a claim this project could not check.

So the image ships **two**: the XTOOL D8, and a generic plain-text reader that
is the worked example of "a profile is data". Everything else lives here, and
an operator installs the one for the tool in their hand. That also means a
profile can be contributed by whoever owns the hardware, which is the only
person in a position to prove it reads their reports.

## What is already here

Seven templates and seven parser profiles. They are meant to be read as
examples as much as installed:

| File | What it shows |
| --- | --- |
| `schedules/towing-heavy-use.yaml` | Distance and time intervals side by side, shortened for heat. |
| `schedules/four-wheel-drive.yaml` | A schedule meant to be applied *alongside* another rather than instead of one. |
| `schedules/stored-or-seasonal.yaml` | Deliberately **no distance intervals at all** — a vehicle covering four hundred miles a year never reaches a mileage service, and its brake fluid ages anyway. |
| `schedules/trailer.yaml` | No engine, so nothing here is an engine service. Distance is towed miles. |
| `checklists/roadworthy-quick-check.yaml` | A deliberately short one; a check nobody finishes is worse than a shorter one everybody does. |
| `checklists/pre-tow-check.yaml` | Five minutes in the driveway, every item something that fails at highway speed. |
| `checklists/out-of-storage.yaml` | Faults that happen because a vehicle *sat*, which an ordinary service checklist misses. |
| `profiles/ross-tech-vcds-auto-scan.yaml` | A fault stated across two lines, and a code column that falls back from J2012 to the vendor's own numbering. Verified against seven Auto-Scans. |
| `profiles/autel-maxisys-vehicle-diagnostic-report.yaml` | A PDF read as text, with the status vocabulary ending each row and the module read off the heading above it. Verified against three reports from three tablets. |
| `profiles/thinkcar-all-system-report.yaml` | The only profile that reads a **data stream** as well as a code table - one of its reports found no faults at all and is 159 readings. Also shows a profile **declining** a format: THINKCAR's older generator wraps its columns around the code, and the fingerprint is written so as not to claim it. |
| `profiles/topdon-full-system-report.yaml` | A description reassembled from a cell wrapped over two printed lines. Unproven, and its notes say exactly which of the vendor's two layouts it reads. |
| `profiles/carly-diagnostics-report.yaml` | The simplest structure here — a module heading and one fault per line — and a report with no VIN and no odometer at all. Unproven. |
| `profiles/bluedriver-scan-report.yaml` | Numbered lines, one code each. Unproven: one sample exists. |
| `profiles/car-scanner-elm-obd2-dtc-report.yaml` | A text export, one fault per block, with the module *inside* each fault rather than above it. Unproven. |

Every template is generic and says so in its own description. None is
transcribed from a manufacturer's schedule, and none should be treated as one —
they are starting points to be adjusted against the manual for the actual
vehicle. Every profile says what it was written against and what it does *not*
read, which is the equivalent honesty for a thing whose correctness you cannot
judge by eye.

## Parser profiles need more than a name

A schedule states its intervals in prose you can read and argue with. A parser
profile is regexes run over a scan report, and whether it reads that report
correctly is **not knowable by looking at it**. Two files both called
`XTOOL D8` — one written by somebody holding the tool, one written by somebody
guessing from a screenshot — are indistinguishable by name, and the second one
silently mis-reads diagnostic data.

So a profile carries an `author`, and it may name captured reports it has been
proven against:

```yaml
name: XTOOL D8
author: Somebody
verified_against:
  - F150202304251736
  - Corolla202308281741
```

**That is not a claim; it is checked.** `build_catalog` loads each named
report from the corpus in `Artifacts/samples/scan-reports/`, runs the profile
over it, and requires that the profile recognize it at its own fingerprint
threshold, extract a value for every field it declares, and raise no warnings.
A profile that names a report it cannot read **fails the build** — a file
claiming a verification it cannot pass is worse than one claiming nothing,
because the badge is what people read instead of the regexes.

**At least two reports**, and more is better. A profile overfitted to a single
capture passes a single-report check by construction, which is the failure the
whole mechanism exists to catch. Reports from different vehicles, different
model years and different capture dates are worth far more than two from the
same afternoon.

### Contributing the reports

To earn the badge you contribute the captures too:

```bash
python manage.py capture_fixture path/to/report.pdf --tool "your scanner"
```

That writes a capture — `.words.json` for a PDF, the word geometry the parser
actually reads; `.text.json` for anything else — and a `.expected.json` beside
it, **redacting as it goes**. Two rules find a VIN: one that validates its
check digit anywhere in the document, and one that follows a VIN label whatever
its check digit says, because position 9 is a check digit only where a
regulator requires one and a European VIN carries a filler there. Licence
plates, customers, technicians, shop names and addresses are removed; workshop
codes and tool serials are zeroed, keeping the shape a parser matches on.

**Read the result before committing it.** The redactor handles what a rule can
name. It does not know that a technician signed page four in handwriting, or
that a note names the street the car broke down on. So `capture_scan_samples`
ends by auditing what it wrote and printing anything that still looks like
somebody's details — read that, and then read the diff. The corpus exists to
prove parsers, not to publish anybody's vehicle, and it is going somewhere
public and permanent: a contributor who cannot scrub a report should publish
the profile **without** `verified_against` rather than publish the report.

An unproven profile is still welcome and still installable. The browse screen
says plainly which it is, and *"somebody wrote this and nobody has run it
against a real report"* is a fair thing for an operator to decide about for
themselves. What is not fair is that state being invisible.

## Reviewing

**The review is the point.** The mechanical checks catch a file that is
malformed. They cannot catch one that is well-formed and *wrong*, and "the
timing belt interval is double what it should be" is the failure that costs
somebody an engine. Only a person reading it catches that.

When reviewing, check that:

- the intervals or thresholds match a source you can name — an owner's manual,
  a service bulletin — and the description says which;
- the template is honest about being generic; it will be applied to vehicles
  nobody reviewing it has seen;
- safety-critical inspection points are marked `is_safety_critical`, and
  safety-critical services `severity: safety`;
- nothing identifies the contributor's own vehicles;
- for a parser profile, that the reports it names are genuinely different
  captures rather than the same vehicle twice, and that they carry no real
  VIN, plate or location.

## What an instance will not do

- **Follow a URL from `index.json`.** An entry names a *path*, resolved under
  the configured catalog address; an absolute URL, a scheme, a host or `..` is
  refused. The repository does not get to choose which host an instance talks
  to — that decision belongs to the operator's allowlist.
- **Trust a catalog file further than an uploaded one.** Both go through the
  same validator. There is no privileged import path, which is what makes this
  safe to point at a repository the operator does not control.
- **Apply anything.** Installing adds a template to the shop's list; putting it
  on a vehicle stays a separate, deliberate act.

## Formats

Each kind is documented where it is implemented, and any file exported from a
running instance is already in the right format:

| Folder | Kind | Format |
| --- | --- | --- |
| `schedules/` | Service schedule | `homeautoshop/maintenance/templatelib.py` |
| `checklists/` | Inspection checklist | [SCHEMA-INSPECTION-TEMPLATES.md](../Artifacts/SCHEMA-INSPECTION-TEMPLATES.md) |
| `profiles/` | Scan-tool parser profile | [SCHEMA-PARSER-PROFILES.md](../Artifacts/SCHEMA-PARSER-PROFILES.md) |

`index.json` carries `kind`, `slug`, `name`, `path` and `description` per
entry. A `kind` an older instance does not recognize is **skipped and
counted**, not refused, so publishing a new kind does not break instances that
predate it — they show what they can use and say how many entries they could
not.
