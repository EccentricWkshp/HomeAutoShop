"""Evaluate and deliver reminders (SPEC FR-MAINT-10)."""

from django.core.management.base import BaseCommand

from homeautoshop.core.notifications import collect, run


class Command(BaseCommand):
    help = "Send the reminder digest to every enabled channel."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="Show the digest and stop.")
        parser.add_argument(
            "--force", action="store_true", help="Send even if REMINDERS_ENABLED is false."
        )

    def handle(self, *args, **options):
        if options["dry_run"]:
            digest = collect()
            if digest.is_empty:
                self.stdout.write("Nothing to report — no message would be sent.")
                return
            self.stdout.write(self.style.MIGRATE_HEADING(digest.subject()))
            self.stdout.write(digest.as_text())
            return

        result = run(force=options["force"])
        if result["skipped"]:
            self.stdout.write(f"Nothing sent ({result['skipped']}).")
            return
        self.stdout.write(
            self.style.SUCCESS(
                f"{result['alerts']} alert(s) delivered to {result['sent']} of "
                f"{result['channels']} channel(s)."
            )
        )
