"""
Fold a harvest into the published catalog, one document per make.

`read_manual_library` and `crawl_dtc_manuals` both stop short of publishing.
They write to git-ignored staging with `source` left empty, because whether
somebody else's compilation may be republished is a decision a person makes,
not a command. This is the command that carries it out once that decision has
been taken, and it is separate for exactly that reason: running a harvest is
routine, publishing one is not.

**A harvest is added, not substituted.** A make's bundle holds several
documents and `precedence` says which answers first, which matters here because
the two sources cover different ground rather than one superseding the other.
A harvest reads the service manuals of the vehicles it sampled, so it goes very
deep on those and can miss ranges a broad compilation happens to list: against
the lists already published, one library harvest held 5,710 Acura codes to the
compilation's 221 and *still* lacked 42 of them, and for Audi it lacked 368 of
586. Replacing would have read as an upgrade and lost a thousand definitions
across six makes. So the harvest goes in above — its wording is the
manufacturer's, and the compilations carry visible transcription damage such as
`Long trem fuel trim` and `Out Of Rage` — and the older document stays
underneath to answer what the harvest does not.

**Every route in goes through the one validator.** The merged bundle is parsed
by `codelistlib` before it is written, so a file this command produces is
refused here rather than at install time on a shop's machine.

**The copy check still runs.** Multi-make compilations are often one make's
list with the attribution removed, and a harvest is not immune: it reads what
the publisher filed under a make, and a publisher can file one document under
several. Overlaps already examined are recorded in `codelists/_rejected.json`
with the highest share seen, and reported in one quiet line at or below it —
a catalog of factory manuals throws twenty-six of them, all corporate families
or small denominators, and a real finding has to be visible among those rather
than the twenty-seventh line of the same shape.

    python manage.py catalog_dtc_harvest --from Artifacts/samples/dtc-lists/library \\
        --publication "example.com" --dry-run
    python manage.py catalog_dtc_harvest --from Artifacts/samples/dtc-lists/library \\
        --publication "example.com" --make Acura
"""

from __future__ import annotations

import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from homeautoshop.diagnostics import codelistlib, dtc

from .build_dtc_list import (
    COPY_THRESHOLD,
    _merge,
    _overlaps,
    catalog_codes,
    slug_for,
)
from .read_manual_library import RENAMED

#: Names a harvest must not still be carrying. `read_manual_library.make_of`
#: maps them on the way in, so a file under one of these was written before
#: that existed — and publishing it would put `dodge-and-ram.json` beside the
#: `dodge.json` and `ram.json` the catalog already has. Refused rather than
#: renamed here: `Dodge and Ram` needs the vehicles split before the codes are
#: pooled, which is a harvest to run again, not a field to edit.
CORPUS_NAMES = {"Dodge and Ram", *RENAMED}

#: Overlaps already examined, from `codelists/_rejected.json`. Read once.
_CLEARED: list | None = None


def cleared(one: str, other: str) -> float | None:
    """The share already accounted for between two makes, if any.

    A catalog built from factory service manuals reports twenty-six word-for-
    word overlaps and every one is a corporate family or a small denominator.
    Investigating them again on each rebuild is how the finding that matters
    gets lost among the ones that do not, so what has been answered is written
    down and this reads it back.
    """
    global _CLEARED
    if _CLEARED is None:
        from homeautoshop.diagnostics import codelists

        register = Path(codelists.__file__).parent / "_rejected.json"
        data = json.loads(register.read_text(encoding="utf-8")) if register.exists() else {}
        _CLEARED = data.get("overlaps") or []
    pair = {one.strip().lower(), other.strip().lower()}
    for entry in _CLEARED:
        known = {str(m).strip().lower() for m in entry.get("makes") or []}
        if pair <= known:
            return float(entry.get("upto") or 0)
    return None


#: Where a harvest sits above a compilation. Both stay; this decides which is
#: quoted first, and the manufacturer's own wording should be.
PRECEDENCE = 1

#: How many of the vehicles read are named in the citation. The validator keeps
#: twenty, and a citation is meant to let somebody find the document again
#: rather than to reproduce the run.
NAMED = 20


class Command(BaseCommand):
    help = "Add a staged DTC harvest to the published catalog."

    def add_arguments(self, parser):
        parser.add_argument(
            "--from", dest="from_dir", required=True,
            help="The staging folder a harvest command wrote.",
        )
        parser.add_argument(
            "--publication", required=True,
            help=(
                "Who published the manuals, for the citation. Required because "
                "attribution is the decision this command exists to carry out, "
                "and a default would be the command making it."
            ),
        )
        parser.add_argument("--make", action="append", default=[], help="Only these. Repeatable.")
        parser.add_argument(
            "--precedence", type=int, default=PRECEDENCE,
            help=f"Where this document ranks against those already held (default {PRECEDENCE}).",
        )
        parser.add_argument(
            "--out", default="",
            help="Where the bundles are written (default: the published catalog).",
        )
        parser.add_argument("--dry-run", action="store_true", help="Report what would change.")

    def handle(self, *args, **options):
        staged = Path(options["from_dir"])
        if not staged.is_dir():
            raise CommandError(f"{staged} is not a folder.")

        self.out = Path(options["out"]) if options["out"] else catalog_codes()
        self.out.mkdir(parents=True, exist_ok=True)
        wanted = {m.strip().lower() for m in options["make"]}
        files = [p for p in sorted(staged.glob("*.json")) if not p.name.startswith("_")]
        if not files:
            raise CommandError(f"{staged} holds no harvested lists.")

        written = skipped = 0
        for path in files:
            fresh = json.loads(path.read_text(encoding="utf-8"))
            make = str(fresh.get("make") or "").strip()
            codes = fresh.get("codes") or {}
            if wanted and make.lower() not in wanted:
                continue
            if not codes:
                self.stdout.write(self.style.WARNING(f"  {make}: defines nothing, skipped"))
                skipped += 1
                continue
            if make in CORPUS_NAMES:
                self.stdout.write(
                    self.style.WARNING(
                        f"  {make}: still the library's name for this make. "
                        f"Re-harvest it, then publish."
                    )
                )
                skipped += 1
                continue
            if self._one(make, codes, fresh, options):
                written += 1
            else:
                skipped += 1

        self.stdout.write(
            self.style.SUCCESS(
                f"{written} make(s) written, {skipped} skipped"
                + (" (dry run, nothing changed)" if options["dry_run"] else "")
            )
        )

    # -- one make ---------------------------------------------------------

    def _one(self, make: str, codes: dict, fresh: dict, options) -> bool:
        read_from = [str(v) for v in (fresh.get("read_from") or [])][:NAMED]
        source = self._source(make, fresh, read_from, options["publication"])
        document = {
            "source": source,
            "precedence": options["precedence"],
            "codes": dict(sorted(codes.items())),
            "read_from": read_from,
        }

        out = self.out / f"{slug_for(make)}.json"
        # The published name wins, and it has to be settled *before* the copy
        # check: that check skips a make's own documents by name, so comparing
        # `Mercedes Benz` against a bundle published as `Mercedes-Benz` reads
        # its own last run as a hundred per cent copy and refuses it.
        named = self._published_name(out) or make
        if named != make:
            self.stdout.write(f"  {make}: published as {named}, keeping that name")

        specific = {c: t for c, t in codes.items() if not dtc.parse(c)["is_iso_sae"]}
        for other, share, examples in _overlaps(specific, own_make=named):
            answered = cleared(named, other)
            # Compared as the percentages a person reads and writes down,
            # not as floats: 0.6215 is "62%" on screen and in the register.
            recorded = answered is not None and round(share * 100) <= round(answered * 100)
            if recorded and share < COPY_THRESHOLD:
                # Already looked at. Said once, quietly, so a rebuild does not
                # bury a new finding under two dozen settled ones.
                self.stdout.write(
                    f"  {named}: {share:.0%} shared with {other}, as recorded"
                )
                continue
            style = self.style.ERROR if share >= COPY_THRESHOLD else self.style.WARNING
            self.stdout.write(
                style(f"  {named}: {share:.0%} of this is {other}'s list, word for word")
            )
            if answered is not None:
                self.stdout.write(
                    self.style.WARNING(
                        f"      above the {answered:.0%} recorded for these makes — "
                        "check it, then raise `upto` in codelists/_rejected.json."
                    )
                )
            for code, text in examples:
                self.stdout.write(f"      {code}: {text}")
            if share >= COPY_THRESHOLD:
                self.stdout.write(
                    self.style.ERROR(f"  {named}: refused — this is {other}'s document.")
                )
                return False

        payload, note = _merge(out, make=named, aliases=[], author="", document=document)
        try:
            codelistlib.parse(json.dumps(payload, ensure_ascii=False))
        except codelistlib.CodeListInvalid as exc:
            self.stdout.write(self.style.ERROR(f"  {make}: would not install — {exc}"))
            return False

        held = self._already(out, source, codes)
        self.stdout.write(
            f"  {named:16} {len(codes):6,} codes{note}"
            + (f", {held:,} kept from {len(payload['documents']) - 1} other document(s)"
               if held else "")
        )
        if not options["dry_run"]:
            out.write_text(
                json.dumps(payload, indent=1, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
        return True

    @staticmethod
    def _published_name(out: Path) -> str:
        """What the catalog already calls this make, if it covers it."""
        if not out.exists():
            return ""
        return str(json.loads(out.read_text(encoding="utf-8")).get("make") or "")

    @staticmethod
    def _source(make: str, fresh: dict, read_from: list, publication: str) -> str:
        """Where these definitions came from, in one line a person can act on.

        The build is in it because a later dump of the same library is a
        different document rather than a newer version of this one, and the
        vehicle count because a list read from eleven manuals and offered for a
        whole make is a wider claim than the document makes.
        """
        build = str(fresh.get("build") or "").strip()
        vehicles = len(fresh.get("read_from") or [])
        parts = [f"{publication} — {make} service manuals"]
        if vehicles:
            parts.append(f"{vehicles} vehicles read")
        if build:
            parts.append(f"build {build}")
        return ", ".join(parts)[:200]

    @staticmethod
    def _already(out: Path, source: str, codes: dict) -> int:
        """How many codes the other documents answer that this one does not.

        The number that decides whether replacing them would lose anything, so
        it is printed on every run rather than worked out afterwards.
        """
        if not out.exists():
            return 0
        held = json.loads(out.read_text(encoding="utf-8"))
        theirs = {
            c
            for d in held.get("documents", [])
            if d.get("source") != source
            for c in (d.get("codes") or {})
        }
        return len(theirs - set(codes))
