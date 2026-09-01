"""Build `catalog/index.json` from the files beside it (SPEC §8.1b).

Contributing used to mean adding a file *and* hand-editing an index, where
forgetting the second half published nothing and said nothing about why. That
is a bad job to give a person: it is exactly the bookkeeping a program does
without being asked, and the failure is silent.

So the index is generated. A contributor drops a `.yaml` file in the right
folder and opens a pull request; this writes the index, and CI runs it with
`--check` so a stale one fails the build rather than shipping.

**It validates while it walks.** Every file is parsed by the same validator an
instance will use on it — `templatelib.parse` for schedules and checklists,
`profiles.from_yaml` for parser profiles — so a malformed template cannot be
published at all. That is worth more than the index-writing: it moves the
first failure from somebody's garage to the pull request, where the person who
wrote it is still looking.

Names and descriptions come out of the files themselves, so the index cannot
disagree with what it points at.
"""

from __future__ import annotations

import json
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

#: folder → kind. The reader returns `(name, slug, description, author)` or
#: raises, and is the *same* code that runs on an operator's instance.
FOLDERS = {
    "schedules": "schedule",
    "checklists": "checklist",
    "profiles": "profile",
}


def _read(kind: str, text: str) -> tuple[str, str, str, str]:
    if kind in ("schedule", "checklist"):
        if kind == "schedule":
            from homeautoshop.maintenance import templatelib as lib
        else:
            from homeautoshop.inspections import templatelib as lib

        data = lib.parse(text)
        return (
            str(data["name"]),
            str(data.get("slug") or ""),
            str(data.get("description") or ""),
            str(data.get("author") or ""),
        )

    from homeautoshop.diagnostics import profiles as profilelib

    profile = profilelib.from_yaml(text)
    if profile.verified_against:
        # Run it. A profile is regexes over somebody's scan report, and its
        # correctness is not readable the way a schedule's intervals are — so
        # a claim of having tested it is worth very little, and proving it
        # against a captured report with known-good output is worth a lot.
        # Refused rather than downgraded: a file claiming a verification it
        # cannot pass is the one thing worse than one claiming nothing.
        verify(profile)
    return (
        profile.name,
        "",
        f"{profile.tool_vendor} {profile.tool_model}".strip(),
        profile.author,
    )


class NotVerified(Exception):
    """The profile named a captured report and did not read it correctly."""


#: How many captured reports a profile must read before it earns the badge.
#: One proves the profile fits one report — which is also what a profile
#: overfitted to a single vehicle's quirks would prove. Two different reports,
#: ideally from different vehicles and dates, is the smallest number that says
#: anything about generalizing.
PROVEN_AT_LEAST = 2


def verify(profile) -> None:
    """Prove a profile against the reports it names (FR-INT-7).

    **What this proves:** that the profile recognizes genuine captured reports
    from that tool and reads them without gaps or complaints — across several,
    so that fitting one report by accident is not enough. That is the question
    the badge answers, *did somebody actually have this hardware*, and it is a
    bar a profile written from imagination does not clear.

    **What it does not prove** is that every extracted value is correct. The
    corpus records a report's full nested structure while a profile extracts
    flat named fields, and reconciling the two is a larger job than this. So
    the badge says the profile reads real reports, which is what is checked,
    rather than "verified correct", which the evidence would not support.

    Each named report must exist, and for each the profile must:

    * **match its own fingerprint threshold** — the check a made-up profile
      fails, because a fingerprint invented for hardware nobody has does not
      appear in a report from hardware somebody does;
    * **extract something**, and specifically a value for every field it
      declares an extractor for. A profile using one of this build's built-in
      parsers declares none, so the general rule carries it: an extraction
      producing no fields at all has not read the report;
    * **raise no warnings.**
    """
    from homeautoshop.diagnostics import engine
    from homeautoshop.scantools import fixtures

    wanted = list(profile.verified_against or [])
    if len(wanted) < PROVEN_AT_LEAST:
        raise NotVerified(
            f"names {len(wanted)} report(s); the badge needs at least "
            f"{PROVEN_AT_LEAST}. Publish it without `verified_against` to "
            f"offer it unproven."
        )

    known = {fixtures.stem(c): c for c in fixtures.samples()}
    for name in wanted:
        capture = known.get(name)
        if capture is None:
            raise NotVerified(
                f"names {name!r}, which is not in the captured corpus. "
                f"Add the report, or remove it from `verified_against`."
            )
        _prove_one(profile, capture, name, engine, fixtures)


def _prove_one(profile, capture, name, engine, fixtures) -> None:
    pages = fixtures.pages(capture)
    # Built the way `engine.read` builds one from a PDF: scoring matches on
    # `text` and extraction uses `pages`, so a document carrying only one of
    # the two fails for a reason that has nothing to do with the profile.
    document = engine.Document(
        text=chr(10).join(
            " ".join(engine.normalize(str(w.get("text", ""))) for w in page)
            for page in pages
        ),
        pages=pages,
        media_type="pdf",
    )

    threshold = float((profile.fingerprint or {}).get("threshold", 0.7))
    score = engine.score(profile, document)
    if score < threshold:
        raise NotVerified(
            f"does not recognize {name}: scored {score:.2f} against its own "
            f"threshold of {threshold:.2f}."
        )

    extraction = engine.apply(profile, document)
    if not any(str(f.value).strip() for f in extraction.fields.values()):
        raise NotVerified(f"recognizes {name} and reads nothing out of it.")

    empty = sorted(
        field for field in (profile.field_extractors or {})
        if not str(extraction.value(field)).strip()
    )
    if empty:
        raise NotVerified(
            f"reads {name} but extracts nothing for: {', '.join(empty[:5])}."
        )
    if extraction.warnings:
        raise NotVerified(f"reads {name} with warnings: {extraction.warnings[0]}")


def verified_against(kind: str, text: str) -> str:
    """The report a profile was proven against, for the index. Blank for the
    other kinds, whose correctness is readable rather than testable."""
    if kind != "profile":
        return ""
    from homeautoshop.diagnostics import profiles as profilelib

    return ", ".join(profilelib.from_yaml(text).verified_against)


class Command(BaseCommand):
    help = "Generate catalog/index.json from the template files beside it."

    def add_arguments(self, parser):
        parser.add_argument(
            "--root",
            default=str(Path(settings.BASE_DIR) / "catalog"),
            help="The catalog folder (default: catalog/ in this repository).",
        )
        parser.add_argument(
            "--check",
            action="store_true",
            help="Fail if the index on disk is not what this would write. For CI.",
        )

    def handle(self, *args, **options):
        root = Path(options["root"])
        if not root.is_dir():
            raise CommandError(f"{root} is not a folder.")

        entries, problems = [], []
        for folder, kind in sorted(FOLDERS.items()):
            for path in sorted((root / folder).glob("*.yaml")):
                text = path.read_text(encoding="utf-8")
                try:
                    name, slug, description, author = _read(kind, text)
                except Exception as exc:
                    # Named and collected rather than raised on the first one:
                    # a contributor fixing three files wants all three errors.
                    problems.append(f"{path.relative_to(root).as_posix()}: {exc}")
                    continue
                entry = {
                    "kind": kind,
                    "slug": slug or path.stem,
                    "name": name,
                    "path": f"{folder}/{path.name}",
                    "description": description,
                }
                if author:
                    entry["author"] = author
                if verified_against(kind, text):
                    entry["verified"] = True
                entries.append(entry)

        if problems:
            for problem in problems:
                self.stderr.write(self.style.ERROR(problem))
            raise CommandError(
                f"{len(problems)} file(s) would not import. The index was not written."
            )

        index = {"version": 1, "entries": entries}
        body = json.dumps(index, indent=2, ensure_ascii=False) + "\n"
        target = root / "index.json"

        if options["check"]:
            current = target.read_text(encoding="utf-8") if target.exists() else ""
            if current != body:
                raise CommandError(
                    "catalog/index.json is out of date. Run `manage.py build_catalog`."
                )
            self.stdout.write(self.style.SUCCESS(f"Index is current ({len(entries)} entries)."))
            return

        target.write_text(body, encoding="utf-8")
        self.stdout.write(
            self.style.SUCCESS(f"Wrote {target} — {len(entries)} entries, all valid.")
        )
