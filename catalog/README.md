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
fails the command with the reason and the line. CI runs `build_catalog
--check`, so a stale index or a broken template fails the build rather than
shipping. That check is worth more than the index-writing: it moves the first
failure from somebody's garage to the pull request, while the person who wrote
it is still looking at it.

## What is already here

Six entries, and they are meant to be read as examples as much as installed:

| File | What it shows |
| --- | --- |
| `schedules/gasoline-normal.yaml` | The simplest useful shape — distance and time intervals side by side. |
| `schedules/gasoline-severe.yaml` | The same vehicle on the schedule most owners actually qualify for. |
| `schedules/generic-diesel-severe.yaml` | A light diesel that tows, with `severity: safety` on the brake item. |
| `schedules/small-engine-equipment.yaml` | Scheduling on **running hours** rather than distance, for `asset_kind: equipment`. |
| `checklists/pre-purchase-inspection.yaml` | The full checklist shape — positions, sub-positions, measurement thresholds. |
| `checklists/roadworthy-quick-check.yaml` | A deliberately short one; a check nobody finishes is worse than a shorter one everybody does. |

Every one is generic and says so in its own description. None is transcribed
from a manufacturer's schedule, and none should be treated as one — they are
starting points to be adjusted against the manual for the actual vehicle.

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

```
python manage.py capture_fixture path/to/report.pdf
```

That writes a `.words.json` (the extracted word geometry the parser actually
reads) and a `.expected.json` beside it, **redacting as it goes**: every VIN
whose check digit validates is replaced with a synthetic stand-in, and tool
serials are masked. Keying off the check digit is what lets a part number or a
calibration ID of the same shape survive unmangled.

**Read the result before committing it.** The redactor handles VINs and
serials; it does not know that a technician's name is in a header or that the
customer's address is on page four. The corpus exists to prove parsers, not to
publish anybody's vehicle, and it is going somewhere public and permanent — a
contributor who cannot scrub a report should publish the profile **without**
`verified_against` rather than publish the report.

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
