# Development

## Run it locally, without Docker

```bash
python -m venv venv
venv/Scripts/python -m pip install -r requirements.txt   # POSIX: venv/bin/python
cp .env.example .env      # then set DEBUG=true
venv/Scripts/python manage.py migrate
venv/Scripts/python manage.py seed
venv/Scripts/python manage.py runserver
```

Then open <http://localhost:8000>, which will send you to the setup page —
there are no accounts yet, so that is where a fresh instance starts. Jobs run out of process:

```bash
venv/Scripts/python manage.py run_worker        # or --once to drain and exit
```

## Run it as deployed

```bash
cp .env.example .env      # edit SECRET_KEY and the passwords
docker compose up -d
```

That pulls the published release, which is genuinely "as deployed" — it is the
image other people are running. **It is not your working tree.** To run the
stack against the code in front of you, add the build override:

```bash
docker compose -f docker-compose.yml -f docker-compose.build.yml up -d --build
```

or put `COMPOSE_FILE=docker-compose.yml:docker-compose.build.yml` in `.env` and
forget about the flags. Editing a file and wondering why nothing changed is the
failure this paragraph exists to prevent.

Then open the site and fill in the setup page. The account is not part of the
image because the database is a volume: recreate the volume and you have a
working instance with nobody in it, which is exactly when the setup page comes
back.
[docs/INSTALL.md](INSTALL.md) covers the whole first run, hostname and
certificate included.

Four services per SPEC §5.1. `docker compose --profile slim up` drops the
worker and runs jobs in-process, which is fine below roughly five vehicles.

### The one setting that matters locally

`.env.example` is written for the deployed stack, and one of its values is
wrong for a laptop:

| | Deployed | Local |
| --- | --- | --- |
| `DEBUG` | `false` | `true` — for tracebacks; static files work either way |

Media needs nothing: it goes to the filesystem under `data/media` either way,
so `runserver` and the Compose stack store files the same. If you set
`STORAGE_DRIVER=s3` to work on that driver, point `STORAGE_ENDPOINT` at a store
you actually have — an unreachable one does not fail fast, it hangs on DNS and
then errors, which reads like a bug in the media pipeline rather than a missing
service.

## Static files

`/static/` is served by **WhiteNoise**, from inside the application process.
This is not a convenience: gunicorn serves no static files and the Caddy site
block only reverse-proxies, so nothing else in the stack would answer for
`app.css`. The failure is quiet and easy to misread — Django replies to the
request with a 404 *page*, so the browser reports a MIME type error
(`text/html` is not a stylesheet) rather than a missing file.

`STATIC_HASHED` decides how they are named:

| | `STATIC_HASHED` | Names | Needs `collectstatic` |
| --- | --- | --- | --- |
| Container | `true`, set by compose | `app.9d73cd3b.css`, cached forever | yes, and compose runs it |
| Everywhere else | unset | `app.css`, revalidated | no — served through the finders |

Splitting on an explicit variable rather than on `DEBUG` keeps `runserver` and
the test suite working without a build step, while the deployed instance still
gets immutable, content-addressed filenames.

One MIME type is pinned in settings rather than looked up: Python's
`mimetypes` table has no entry for `.webmanifest`, and on Windows it consults
the registry, so the answer differs by host. An unknown type is served as
`application/octet-stream`, which browsers refuse to accept as a manifest.

## SQLite versus Postgres

**Postgres is the production target.** SQLite is supported for local
development and the test suite so the app runs without Docker, which is why the
whole suite finishes in about a second. Two things genuinely differ:

| | Postgres | SQLite |
| --- | --- | --- |
| Search (`core/search.py`) | `tsvector` ranking via `SearchRank` | unranked `icontains` scan |
| Backup (`core/backup.py`) | `pg_dump --format=custom` | file copy after a WAL checkpoint |

Everything else — JSON fields, UUIDv7 keys, constraints, the job queue — behaves
identically. Point `DATABASE_URL` at a Postgres instance to develop against the
real thing.

> **The search difference is not only about ranking, and it has bitten once.**
> SQLite's fallback is `icontains`, which is a *substring* match. Postgres full
> text search matches whole lexemes. So a search for the last six characters of
> a VIN — the only part the masked vehicle page shows — worked perfectly on
> SQLite and returned nothing in production, and no test caught it because the
> suite ran on SQLite.
>
> Identifier columns are now matched as substrings on both backends, and
> `core/tests_search.py` covers each case. **Run the suite against Postgres
> before believing a search change**, because SQLite is the more permissive of
> the two and will tell you what you want to hear:
>
> ```bash
> docker compose exec app python manage.py test homeautoshop.core.tests_search
> ```

## Tests

```bash
venv/Scripts/python manage.py test
```

2,372 tests, ~3½ min. They cover the invariants that are cheap to break and expensive
to find later: unit round-trips, money arithmetic, optimistic concurrency, the
append-only guarantee, the work-order state machine, VIN check digits, decode
override preservation, the outbound allowlist, media deduplication, and export
readability.

Two conventions worth keeping:

- **A string in a module-level constant must be `gettext_lazy`.**
  `gettext` resolves when it is *called*, and a dict built at import time is
  built before any language is active — so the English is frozen in and the
  caption stays English on a translated page while everything around it
  translates. `check_translations` cannot see this: the string is wrapped, which
  is all that check is about.
- **Fixtures must be verifiable.** The VIN tests use the published ISO 3779
  worked example rather than an invented VIN, because an invented VIN with a
  made-up check digit tests only that the code agrees with the test author.
- **Every test names the requirement it defends** in its docstring, so a
  failure says which promise broke.
- **A test that renders in another language puts it back.**
  `LocaleMiddleware` activates a language per request and never deactivates —
  correct in production, where the next request sets it again, and a leak in a
  test process, where the next *case* inherits it. Add
  `self.addCleanup(translation.deactivate)`. This cost months of an
  intermittent failure in `tests_search.py` that read as flakiness.
- **A fixture for a photograph is OCR's output, never a transcription.**
  Captures under `Artifacts/samples/scan-reports/` record how they were read and
  the suite refuses an image capture that does not say `ocr`. Typing the words
  out by hand is tempting when the JPEG cannot be committed and Tesseract is not
  installed locally, and it produces a record of what somebody imagined OCR
  does. Capture in the container, which has Tesseract:

  ```bash
  docker compose run --rm app python -m homeautoshop.scantools.capture       "Artifacts/samples/scan-reports/topdon bt600 plus/20260830_105614.jpg"
  venv/Scripts/python -m homeautoshop.scantools.fixtures --write
  ```

  Read the diff both times. A fixture regenerated without looking is a test that
  has been switched off.
- **No test may touch the network.** The media tests pin `STORAGES` to a
  temporary directory rather than inheriting `STORAGE_DRIVER` from whatever
  `.env` happens to say — a suite that depends on ambient configuration passes
  and fails for reasons unrelated to the code. It matters most for the tests
  that exercise the `s3` driver: `mediafiles/testing.py` has the fixtures, and
  `migrate_storage` is tested between two directories rather than against a
  real object store.

## Testing

To run a clean test version:
>docker compose down                 # stop the real stack; volumes kept
>docker compose -p hastest up -d     # fresh, empty everything → setup wizard

   ... do your clean test run ...

>docker compose -p hastest down -v   # destroys ONLY hastest_* volumes
>docker compose up -d                # your shop, exactly as it was


## Layout

```
config/            settings, urls, wsgi/asgi
homeautoshop/
  core/            base models, units and money, jobs, search, backup, outbound
  accounts/        User, roles, the can() policy layer, API tokens
  people/          Person
  assets/          Asset (vehicles + equipment), meters, VIN, service-info links
  work/            WorkOrder, JobItem, WorkOrderNote
  mediafiles/      Media, MediaLink, the derivation pipeline
  api/             django-ninja routes (/api/v1, docs at /api/v1/docs)
templates/         server-rendered UI
static/app.css     garage-first styling
```

## Conventions

- **Every user-facing string goes through `gettext`** (SPEC §5.6). No literal
  text in templates or views — this is the one thing that cannot be retrofitted
  cheaply.
- **Never bypass `homeautoshop.accounts.models.can()`** for an authorization
  decision. It exists so a narrower `helper` role is policy rules later rather
  than an audit of every view.
- **Append-only models are append-only.** Server-computed derivatives are
  declared in `server_writable_fields`; everything else is a new row.
- **Outbound HTTP goes through `core/outbound.fetch_json`**, which enforces
  Offline Mode, the host allowlist, and the audit log in one place.

## Translations

Every user-facing string goes through `gettext`, enforced by a check that fails
the build:

```bash
venv/Scripts/python manage.py check_translations           # CI gate
venv/Scripts/python manage.py check_translations --strict  # also scans template text nodes
```

The `--strict` pass is advisory: proving that every template text node is
wrapped needs a real template parser and still trips over punctuation and
icons, so the default check stays precise rather than exhaustive. It catches the
unambiguous cases — a literal handed to `messages.*()`, an untranslated
`help_text`/`label`, a template using `{% translate %}` without loading i18n.

Generating the catalogs needs **GNU gettext** (`xgettext`, `msguniq`), which
is not installed by default on Windows:

```bash
python manage.py makemessages -l en_CA -l fr_CA -l es_MX     --ignore=venv --ignore=Artifacts --ignore=staticfiles --no-obsolete --no-wrap
python manage.py compilemessages --ignore venv
```

**On Windows with WSL**, the gettext binaries need not be on the Windows PATH.
Django is pure Python, so WSL can run the extraction against the same
virtualenv the rest of the project uses:

```bash
wsl -e bash -lc "cd /mnt/<drive>/<path>/HomeAutoShop && \
  PYTHONPATH=\$PWD/venv/Lib/site-packages python3 -m django makemessages \
  -l en_CA -l fr_CA -l es_MX --ignore=venv --ignore=Artifacts \
  --ignore=staticfiles --no-obsolete --no-wrap"
```

Expect the diff to be large: an extraction picks up everything added since the
last one, and `msgmerge` marks approximate matches fuzzy — which `compilemessages`
drops, so a fuzzy entry ships as untranslated. Review those before compiling, or
`tests_locales.py` will refuse them.

The image installs gettext, so `docker compose exec app python manage.py makemessages ...`
works without setting anything up locally. **Pass `--ignore venv` to
`compilemessages`** or it walks into the virtualenv and tries to rebuild every
catalog Django ships, read-only, failing on all of them.

There is no `en_US` catalog: it is the source language, so gettext falling
through to the `msgid` is already correct, and an empty one would be 1,777
blank entries churning on every extraction.

fr-CA and es-MX are **machine-drafted, not reviewed by a native speaker**, and
they are **not complete**: an extraction on 2026-09-02 found roughly a thousand
msgids in the code that had never reached a catalog. Every screen built since
the last `makemessages` run had been quietly falling through to English in all
four locales.

`check_translations` did not catch it and could not: it proves every string is
*wrapped*, which is a property of the source, and says nothing about whether a
catalog answers for it. Running the extraction is the only thing that shows the
gap, and nothing runs it. See SPEC §19.

en-CA is generated by spelling rule and covers ten strings. See
[locale/README.md](../locale/README.md) for what was deliberately left alone
and how to correct one.

The `.mo` files are committed, because `.dockerignore` keeps `.po` out of the
image — so an edited `.po` that nobody recompiled means the running instance
disagrees with the repository. `homeautoshop/core/tests_locales.py` fails when
they drift, and also checks that no translation drops or invents a `%(name)s`.

## Right-to-left

```bash
venv/Scripts/python manage.py check_rtl
```

The layout is direction-neutral and this is what keeps it that way: physical
properties (`margin-left`, `text-align: right`), the physical corner radii, a
four-value `margin`/`padding` shorthand whose sides differ, an inline `style`
attribute doing any of the above, and any HTML document that does not declare
a `dir`. Use `margin-inline-start`, `text-align: start`, `inset-inline-end`.

The block axis is deliberately not checked. `top`, `margin-bottom` and
`border-bottom` mean the same thing mirrored, and a rule demanding churn for no
behavior is a rule somebody switches off.

No right-to-left catalog ships — the locale set is North American — so
`core/tests_rtl.py` adds a language for the duration of a test rather than
translating one. What is being verified is the *layout*, and the layout does
not care which words are in it.

## What is verified, and what is not

The suite covers backup **guards** (refusing a non-backup directory, a schema
mismatch, and overwriting a populated instance) and proves a backup contains
real rows by opening the copied SQLite file directly and querying it.

It does **not** perform a full restore round-trip — swapping the database file
underneath a running test would take the connection with it. The
disaster-recovery runbook (SPEC scenario 7) remains a manual drill:

```bash
docker compose down
docker volume rm homeautoshop_db-data
docker compose up -d db
docker compose run --rm app python manage.py restore /data/backups/<stamp> --force
```

## Importing from LubeLogger

A one-time migration of existing history (SPEC §8.6). It is **optional** —
nothing in HomeAutoShop depends on it.

In the UI it lives under the account menu, **Import from LubeLogger**
(administrators only). That screen runs the same connection check, dry run and
commit as the command below, and refuses to import for the same reasons. The
command line is still the better place for a large or scripted import, because
it streams progress instead of holding a request open.

```bash
export LUBELOGGER_URL=https://lubelogger.home.arpa
export LUBELOGGER_API_KEY=...            # a Viewer-scoped key is enough

python manage.py import_lubelogger --check            # reachability, auth, invariant formatting
python manage.py import_lubelogger                    # dry run: counts, unmatched vehicles, samples
python manage.py import_lubelogger --commit           # write, once the dry run looks right
python manage.py import_lubelogger --commit --create-missing
```

### The one thing that will bite you

LubeLogger returns **locale-formatted numbers** unless the `culture-invariant`
header is honored. A `1.234,56` fuel cost imported as `1.23` is a bug nobody
notices for months, and it corrupts every cost report downstream.

Two defenses are in place, because one is not enough:

1. `--check` inspects a sample of the response and **refuses to import** if it
   sees comma-decimals.
2. Every number is parsed strictly. A comma-decimal raises rather than
   truncating, so a mis-configured instance fails loudly mid-import instead of
   producing plausible-looking wrong money.

If the check fails, set `LUBELOGGER_INVARIANT_API=true` on the LubeLogger side.

### What it does and does not do

| | |
| --- | --- |
| Vehicle matching | By VIN, then by a prior import's `ExternalRef`. A year/make/model match is **reported, never auto-merged** — a wrong link writes another vehicle's history into this one. |
| Re-runs | Idempotent via `ExternalRef`. Re-running skips what is already here. |
| Source edits after import | Reported as a conflict. The local record is never overwritten. |
| Source deletions | Never propagated. The row is marked orphaned and kept. |
| A missing endpoint | Reported and skipped; the rest of the import still lands. |
| Costs | An imported record has a total, not a parts breakdown, so it becomes one expense. Inventing part lines would be fabrication. |
| Reminders | **Not imported** — HomeAutoShop's maintenance schedule is Phase 3. |
| Equipment | Not imported (non-plated assets, roadmap R-3). |

Endpoint paths are pinned in `core/integrations/lubelogger.py::ENDPOINTS` and
were written against LubeLogger's documented record types. **Verify them against
your running instance** before a real import (LL-Q1); a 404 on one type is
reported and skipped rather than fatal.

## Reminders

Off by default, and a channel must also be created and enabled before anything
is delivered:

```bash
python manage.py send_reminders --dry-run   # print the digest, send nothing
python manage.py send_reminders             # send, if REMINDERS_ENABLED
python manage.py send_reminders --force     # send even while disabled
```

The `reminders.evaluate` job type does the same thing on the worker, so a cron
entry or scheduler can call it daily.

Three behaviors are deliberate and tested:

- **One digest, never one message per item.** A stream of notifications is how
  a reminder system gets muted, and a muted system is worse than none.
- **Silence when there is nothing to say.** There is no "all clear" message —
  it would train people to ignore the one that matters.
- **A per-item cooldown** (`REMINDER_COOLDOWN_DAYS`, default 7). An overdue item
  you have decided to live with waits before mentioning itself again.

Web push is **not** here: it needs a service worker, which lands with the PWA in
Phase 4 (SPEC §15).
