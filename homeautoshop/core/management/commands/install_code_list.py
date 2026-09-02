"""
Install a manufacturer's published code list without going near the network.

The catalog is how most shops will get one: open the browse screen, press
Install. That screen needs an address it can reach, and P-1 says an instance
that never reaches anything must still work. Before this, a make's list
shipped in the image, so offline meant *no worse*. It no longer does — the
lists are published rather than bundled — so the offline path has to be built
rather than assumed.

Two ways in, and they are the same way underneath:

* **A make already published in this repository's catalog**, by name or slug.
  This is the useful one on a development checkout and on any instance built
  from source, where `catalog/codes/` is right there on disk.
* **A file**, wherever it came from — downloaded on a machine that does have a
  connection, carried in on a stick, written by hand.

Both go through `codelistlib.load`, which is the same validator the catalog
install path calls and the same one a future upload form will call. There is
no privileged route for a file that arrived one way rather than another; that
is the whole trust model and it only holds while nothing bypasses it.

    python manage.py install_code_list Ford
    python manage.py install_code_list --all
    python manage.py install_code_list ~/Downloads/subaru.json
"""

from __future__ import annotations

from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from homeautoshop.core.management.commands.build_dtc_list import catalog_codes, slug_for
from homeautoshop.diagnostics import codelistlib
from homeautoshop.diagnostics.models import InstalledCodeList


class Command(BaseCommand):
    help = "Install a published manufacturer code list from the catalog folder or a file."

    def add_arguments(self, parser):
        parser.add_argument(
            "wanted", nargs="*",
            help="Makes to install, or paths to code-list files.",
        )
        parser.add_argument(
            "--all", action="store_true", help="Install every published list."
        )
        parser.add_argument(
            "--list", action="store_true", help="Say what is published and what is here."
        )

    def handle(self, *args, **options):
        folder = catalog_codes()

        if options["list"]:
            held = {row.make.lower(): row.version for row in InstalledCodeList.objects.all()}
            for path in sorted(folder.glob("*.json")):
                data = codelistlib.parse(path.read_text(encoding="utf-8"))
                mine = held.get(data["make"].lower())
                where = (
                    "installed" if mine == data["version"]
                    else f"installed v{mine}, published v{data['version']}" if mine
                    else "available"
                )
                codes = len({c for d in data["documents"] for c in d["codes"]})
                self.stdout.write(f"  {data['make']:20} {codes:6} codes   {where}")
            return

        if options["all"]:
            paths = sorted(folder.glob("*.json"))
        elif options["wanted"]:
            paths = [self._find(folder, name) for name in options["wanted"]]
        else:
            raise CommandError("Name a make, or a file, or pass --all.")

        installed = 0
        for path in paths:
            try:
                held = codelistlib.load(path.read_text(encoding="utf-8"))
            except codelistlib.CodeListInvalid as exc:
                # Named and carried on rather than raised, so installing
                # twenty lists reports all of the bad ones rather than the
                # first. Nothing is half-installed: each file is its own row.
                self.stderr.write(self.style.ERROR(f"{path.name}: {exc}"))
                continue
            installed += 1
            self.stdout.write(
                f"  {held.make}: {held.code_count} definitions, v{held.version}"
            )

        self.stdout.write(
            self.style.SUCCESS(f"{installed} code list(s) installed.")
        )

    def _find(self, folder: Path, name: str) -> Path:
        candidate = Path(name)
        if candidate.exists():
            return candidate
        published = folder / f"{slug_for(name)}.json"
        if published.exists():
            return published
        raise CommandError(
            f"No published list for {name!r}, and no such file. "
            f"`--list` says what is published."
        )
