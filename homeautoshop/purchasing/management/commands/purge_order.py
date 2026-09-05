"""Remove every trace of one imported order (FR-PUR-1, FR-ADM-7).

For clearing up after an import that went wrong — the case this was written for
being an order deleted from the Django admin, which soft-deleted the purchase,
cascaded to nothing, and left the lines, the stock they had received, the
tooling expenses and the provenance row all behind and all invisible.

**It reports first.** A bare run prints the footprint and changes nothing; the
operator has to ask with `--yes`. Both halves matter on a live instance, where
the thing being deleted is somebody's money record.

**It refuses to lie about stock.** A received lot that has since moved — used on
a job, adjusted, scrapped — is history, and deleting the row would silently
change what the shop believes it has and what it believes that cost. Those lots
are reported and kept unless `--force-stock` says otherwise, and the exit is
non-zero so a script notices.

What is deliberately **not** removed: parts, fitments, cross-references and
vendors. An import creates those, but they outlive the order — a part is a thing
the shop knows about, not a line on a receipt — and removing one that another
order also stocked would take real inventory with it.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from homeautoshop.core.models import AuditLog, ExternalRef
from homeautoshop.parts.models import StockLot, StockTransaction
from homeautoshop.purchasing.importers.service import SOURCE, TOOLING_REF
from homeautoshop.purchasing.models import Expense, Purchase, PurchaseLine


class Command(BaseCommand):
    help = "Permanently remove an imported order and everything it created."

    def add_arguments(self, parser):
        parser.add_argument("order_number", help="The vendor's order number.")
        parser.add_argument(
            "--source",
            default=None,
            help=f"Provenance source system. Defaults to any; the importer writes {SOURCE!r}.",
        )
        parser.add_argument("--yes", action="store_true", help="Actually delete.")
        parser.add_argument(
            "--force-stock",
            action="store_true",
            help="Delete received lots even when the stock has since moved.",
        )

    def handle(self, *args, **options):
        number: str = options["order_number"]
        purchases = Purchase.all_objects.filter(order_number=number)
        refs = ExternalRef.objects.filter(external_type="order", external_id=number)
        tooling_refs = ExternalRef.objects.filter(
            external_type=TOOLING_REF, external_id__startswith=f"{number}:"
        )
        if options["source"]:
            refs = refs.filter(source_system=options["source"])
            tooling_refs = tooling_refs.filter(source_system=options["source"])

        # The ref is the authority on which purchase an import made, but the
        # purchase may have been deleted and the ref left pointing at it — so
        # both directions are followed and the results merged.
        pks = set(purchases.values_list("pk", flat=True)) | set(
            refs.values_list("entity_id", flat=True)
        )
        purchases = Purchase.all_objects.filter(pk__in=pks)
        lines = PurchaseLine.all_objects.filter(purchase_id__in=pks)
        lots = StockLot.all_objects.filter(purchase_line__in=lines.values("pk"))
        transactions = StockTransaction.all_objects.filter(stock_lot__in=lots.values("pk"))
        expenses = Expense.all_objects.filter(
            pk__in=tooling_refs.values_list("entity_id", flat=True)
        )

        if not pks and not refs.exists() and not tooling_refs.exists():
            raise CommandError(f"Nothing here refers to order {number}.")

        self.stdout.write(f"Order {number}:")
        for label, queryset in (
            ("purchase", purchases),
            ("purchase line", lines),
            ("stock lot", lots),
            ("stock transaction", transactions),
            ("tooling expense", expenses),
            ("provenance row", refs),
            ("tooling provenance row", tooling_refs),
        ):
            count = queryset.count()
            if count:
                self.stdout.write(f"  {count:>5}  {label}{'' if count == 1 else 's'}")

        for purchase in purchases:
            state = "in the trash" if purchase.is_deleted else "live"
            self.stdout.write(f"    · {purchase} ({state})")

        moved = [lot for lot in lots if self._has_moved(lot)]
        if moved:
            self.stdout.write(
                self.style.WARNING("\n  Stock that has moved since it was received:")
            )
            for lot in moved:
                self.stdout.write(f"    · {lot}")

        if not options["yes"]:
            self.stdout.write(
                self.style.WARNING("\nNothing changed. Re-run with --yes to delete all of it.")
            )
            return

        if moved and not options["force_stock"]:
            raise CommandError(
                f"{len(moved)} received lot(s) have moved since. Un-receive them, or pass "
                "--force-stock to delete them anyway and accept that the inventory "
                "history will no longer explain itself."
            )

        with transaction.atomic():
            # Written before the rows stop existing: afterwards there is nothing
            # left to describe, and an unexplained gap is what the log prevents.
            AuditLog.objects.bulk_create(
                [
                    AuditLog(
                        entity_type="Purchase",
                        entity_id=purchase.pk,
                        action=AuditLog.Action.DELETE,
                        source="cli",
                        summary=f"purge_order {number}: {str(purchase)[:180]}",
                    )
                    for purchase in purchases
                ]
            )
            # Children first: a hard delete is the first moment the database's
            # own rules apply, and `PartUsage.stock_lot` is a SET_NULL that has
            # to fire before the lot goes.
            removed = {
                "stock transactions": transactions.hard_delete()[0],
                "stock lots": lots.hard_delete()[0],
                "purchase lines": lines.hard_delete()[0],
                "purchases": purchases.hard_delete()[0],
                "tooling expenses": expenses.hard_delete()[0],
                "provenance rows": refs.delete()[0] + tooling_refs.delete()[0],
            }

        for label, count in removed.items():
            if count:
                self.stdout.write(self.style.SUCCESS(f"  removed {count:>5}  {label}"))
        self.stdout.write(
            self.style.SUCCESS(f"\nOrder {number} is gone. It can be read in again.")
        )

    def _has_moved(self, lot) -> bool:
        """Anything on this lot that is not the receipt that created it."""
        return (
            lot.transactions(manager="all_objects")
            .exclude(reason=StockTransaction.Reason.RECEIVE)
            .exists()
        )
