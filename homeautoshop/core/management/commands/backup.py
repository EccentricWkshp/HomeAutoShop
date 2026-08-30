from django.core.management.base import BaseCommand

from homeautoshop.core.backup import prune_backups, run_backup


class Command(BaseCommand):
    help = "Back up the database and media (SPEC §13.1)."

    def handle(self, *args, **options):
        target = run_backup()
        self.stdout.write(self.style.SUCCESS(f"backup written to {target}"))
        if removed := prune_backups():
            self.stdout.write(f"pruned {removed} old backup(s)")
