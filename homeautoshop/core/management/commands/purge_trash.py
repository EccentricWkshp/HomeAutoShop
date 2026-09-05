"""Actually empty the trash (FR-ADM-7).

The trash has always promised thirty days. Nothing enforced it: `hard_delete()`
existed and was called from two service functions, `TRASH_RETENTION_DAYS` was
read by exactly one queryset method used by exactly one screen, and no command
or job ever removed anything. A soft delete was therefore permanent in the
other direction — the row never left, and there was no supported way to make it.

What this does, in order, and why the order is the whole design:

* **Age first.** Only rows past `TRASH_RETENTION_DAYS` are eligible, so the
  restore window the UI promises is honored rather than described.
* **Children before parents.** A hard delete is the first moment the database's
  own `on_delete` rules apply to these rows, and a `PROTECT` in the wrong order
  aborts the lot. Models are purged in reverse dependency order.
* **Report rather than guess.** Reporting is the default posture for anything
  that reads "delete permanently": a bare run says what would go, and the
  operator has to ask for it with `--yes`.
"""

from __future__ import annotations

from datetime import timedelta

from django.apps import apps
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import ProtectedError
from django.utils import timezone

from homeautoshop.core.models import TRASH_RETENTION_DAYS, AuditLog


def soft_delete_models():
    """Every model with a trash, children before the things they hang off.

    Django's `sort_dependencies` is about serialization order (parents first);
    a purge needs the reverse, so a line goes before the order it points at.
    """
    models = [m for m in apps.get_models() if hasattr(m, "all_objects")]

    def depth(model) -> int:
        return sum(
            1
            for field in model._meta.get_fields()
            if field.many_to_one and field.related_model in models
        )

    return sorted(models, key=depth, reverse=True)


class Command(BaseCommand):
    help = "Permanently remove trashed rows past the retention window."

    def add_arguments(self, parser):
        parser.add_argument(
            "--days",
            type=int,
            default=TRASH_RETENTION_DAYS,
            help=f"Retention window. Default {TRASH_RETENTION_DAYS}.",
        )
        parser.add_argument(
            "--model",
            action="append",
            default=[],
            metavar="app.Model",
            help="Limit to these models. Repeatable.",
        )
        parser.add_argument(
            "--yes",
            action="store_true",
            help="Actually delete. Without it this only reports.",
        )

    def handle(self, *args, **options):
        days: int = options["days"]
        if days < 0:
            raise CommandError("--days cannot be negative.")
        cutoff = timezone.now() - timedelta(days=days)

        wanted = {name.lower() for name in options["model"]}
        models = soft_delete_models()
        if wanted:
            models = [m for m in models if m._meta.label.lower() in wanted]
            missing = wanted - {m._meta.label.lower() for m in models}
            if missing:
                raise CommandError(f"Not soft-deleting models: {', '.join(sorted(missing))}")

        total = 0
        for model in models:
            queryset = model.all_objects.filter(
                deleted_at__isnull=False, deleted_at__lt=cutoff
            )
            count = queryset.count()
            if not count:
                continue
            total += count
            label = model._meta.label
            if not options["yes"]:
                self.stdout.write(f"  would purge {count:>6}  {label}")
                continue
            try:
                with transaction.atomic():
                    AuditLog.objects.bulk_create(
                        [
                            AuditLog(
                                entity_type=model.__name__,
                                entity_id=obj.pk,
                                action=AuditLog.Action.DELETE,
                                source="cli",
                                summary=f"purged from trash: {str(obj)[:200]}",
                            )
                            for obj in queryset
                        ]
                    )
                    queryset.hard_delete()
            except ProtectedError as exc:
                # Something outside the trash still points at this row. That is
                # a fact about the data, not a reason to abandon the run.
                self.stderr.write(
                    self.style.WARNING(f"  skipped {label}: still referenced ({exc})")
                )
                total -= count
                continue
            self.stdout.write(self.style.SUCCESS(f"  purged  {count:>6}  {label}"))

        if not total:
            self.stdout.write(f"Nothing in the trash older than {days} days.")
            return
        if not options["yes"]:
            self.stdout.write(
                self.style.WARNING(
                    f"\n{total} row(s) older than {days} days. Re-run with --yes to delete them."
                )
            )
        else:
            self.stdout.write(self.style.SUCCESS(f"\nPurged {total} row(s)."))
