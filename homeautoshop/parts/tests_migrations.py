"""The one migration that moves data rather than columns (0007).

Schema migrations are exercised every time the test database is built. A
`RunPython` on an empty table is not — it runs against nothing and passes, which
is exactly the shape of a data migration that turns out to be wrong on somebody
else's rows. So this one is walked backwards and forwards over real rows.
"""

from __future__ import annotations

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase


class WeightsBecomePricesTests(TransactionTestCase):
    app = "parts"
    before = "0006_stocklot_from_kit_lot_alter_stocktransaction_reason_and_more"
    after = "0007_kit_costs_in_money"

    def migrate_to(self, target):
        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        executor.migrate([(self.app, target)])
        executor.loader.build_graph()
        return executor.loader.project_state([(self.app, target)]).apps

    def setUp(self):
        self.addCleanup(self.migrate_to, self.after)

    def test_a_weight_the_importer_wrote_in_cents_stays_that_money(self):
        old = self.migrate_to(self.before)
        Part = old.get_model(self.app, "Part")
        PartKitItem = old.get_model(self.app, "PartKitItem")
        kit = Part.objects.create(name="A/C Kit")
        part = Part.objects.create(name="A/C Compressor")
        PartKitItem.objects.create(kit=kit, part=part, value_share=17526)

        new = self.migrate_to(self.after)

        item = new.get_model(self.app, "PartKitItem").objects.get()
        self.assertEqual(item.value_minor, 17526)
        self.assertEqual(item.value_currency, "USD")

    def test_an_even_split_becomes_no_price_rather_than_a_penny(self):
        """A weight of 1 meant "share evenly" and never meant one cent. `NULL`
        says "use the part's price", which falls back to an even split."""
        old = self.migrate_to(self.before)
        Part = old.get_model(self.app, "Part")
        PartKitItem = old.get_model(self.app, "PartKitItem")
        kit = Part.objects.create(name="A/C Kit")
        for name in ("A/C Compressor", "A/C Condenser"):
            PartKitItem.objects.create(
                kit=kit, part=Part.objects.create(name=name), value_share=1
            )

        new = self.migrate_to(self.after)

        items = new.get_model(self.app, "PartKitItem").objects.all()
        self.assertEqual([item.value_minor for item in items], [None, None])

    def test_it_goes_back_the_way_it_came(self):
        old = self.migrate_to(self.before)
        Part = old.get_model(self.app, "Part")
        kit = Part.objects.create(name="A/C Kit")
        part = Part.objects.create(name="A/C Compressor")
        old.get_model(self.app, "PartKitItem").objects.create(
            kit=kit, part=part, value_share=17526
        )

        self.migrate_to(self.after)
        back = self.migrate_to(self.before)

        self.assertEqual(
            back.get_model(self.app, "PartKitItem").objects.get().value_share, 17526
        )
