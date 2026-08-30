from pathlib import Path

from django.core.management.base import BaseCommand

from homeautoshop.core.backup import build_export


class Command(BaseCommand):
    help = "Write a portable ZIP export usable without this application (SPEC §13.3)."

    def add_arguments(self, parser):
        parser.add_argument("--out", type=Path, default=None)

    def handle(self, *args, **options):
        path = build_export(options["out"])
        self.stdout.write(self.style.SUCCESS(f"export written to {path}"))
