"""One-time LubeLogger import (SPEC §8.6, FR-INT-11..15)."""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from homeautoshop.core.integrations.importer import run_import
from homeautoshop.core.integrations.lubelogger import LubeLoggerClient, NotConfigured


class Command(BaseCommand):
    help = "Import history from a LubeLogger instance. Dry run unless --commit is given."

    def add_arguments(self, parser):
        parser.add_argument("--commit", action="store_true", help="Actually write. Default is a dry run.")
        parser.add_argument(
            "--create-missing",
            action="store_true",
            help="Create assets for LubeLogger vehicles that do not match anything here.",
        )
        parser.add_argument("--check", action="store_true", help="Test the connection and stop.")
        parser.add_argument("--url", default=None)
        parser.add_argument("--api-key", default=None)

    def handle(self, *args, **options):
        try:
            client = LubeLoggerClient(options["url"], options["api_key"])
        except NotConfigured as exc:
            raise CommandError(str(exc)) from exc

        diagnosis = client.check()
        self.stdout.write(f"Instance:   {client.base_url}")
        self.stdout.write(f"Reachable:  {_yn(diagnosis.reachable)}")
        self.stdout.write(f"Auth:       {_yn(diagnosis.authenticated)}")
        self.stdout.write(f"Invariant:  {_yn(diagnosis.invariant)}")
        self.stdout.write(f"Vehicles:   {diagnosis.vehicle_count}")
        if diagnosis.message:
            self.stdout.write(f"            {diagnosis.message}")

        if not diagnosis.ok:
            # Refusing here is the point: importing locale-formatted numbers
            # produces wrong money that nobody notices for months.
            raise CommandError("Connection check failed; nothing was imported.")

        if options["check"]:
            return

        dry_run = not options["commit"]
        report = run_import(
            dry_run=dry_run, create_missing=options["create_missing"], client=client
        )

        self.stdout.write("")
        self.stdout.write(self.style.MIGRATE_HEADING("DRY RUN" if dry_run else "IMPORTED"))

        for label, bucket in (("create", report.created), ("skip (already here)", report.skipped)):
            if bucket:
                self.stdout.write(f"  {label}:")
                for kind, count in sorted(bucket.items()):
                    self.stdout.write(f"    {kind:<12} {count}")

        if report.matched:
            self.stdout.write(f"  matched vehicles: {len(report.matched)}")
            for match in report.matched[:10]:
                self.stdout.write(f"    {match.label}  ({match.how})")

        if report.unmatched:
            self.stdout.write(self.style.WARNING(f"  unmatched vehicles: {len(report.unmatched)}"))
            for match in report.unmatched:
                self.stdout.write(f"    {match.label}  ({match.how})")
            self.stdout.write(
                "    Pair them by hand at Settings > Import from LubeLogger, which "
                "remembers the pairing for every future run. Or give the matching "
                "vehicle here the same VIN or plate, or re-run with --create-missing."
            )

        if report.samples:
            self.stdout.write("  sample of what would be created:")
            for line in report.samples:
                self.stdout.write(f"    {line}")

        for conflict in report.conflicts:
            self.stdout.write(self.style.WARNING(f"  conflict: {conflict}"))
        for error in report.errors:
            self.stdout.write(self.style.ERROR(f"  error: {error}"))

        self.stdout.write("")
        if dry_run:
            self.stdout.write(
                self.style.SUCCESS("Nothing was written. Re-run with --commit when this looks right.")
            )
        else:
            self.stdout.write(self.style.SUCCESS(f"Imported {report.total_created} record(s)."))


def _yn(value: bool) -> str:
    return "yes" if value else "no"
