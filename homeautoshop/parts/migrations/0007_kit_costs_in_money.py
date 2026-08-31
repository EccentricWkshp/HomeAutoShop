"""A kit's contents carry a price instead of a weight (FR-INV-9).

Reported as: the cost share is confusing, and nobody is getting a calculator
out to work out what proportion a compressor is of a kit. Money is the number
people already have — the vendor prints it, the shelf records it — so the
proportions are derived rather than asked for.

`value_share` was a relative weight. The two states it was ever actually in:

* **1 everywhere**, which is what the form defaulted to and means an even
  split. `NULL` reproduces that exactly, and better — it now means "use the
  part's own price", which falls back to an even split only when no price is
  known anywhere.
* **Cents**, written by the order importer from a `Price EA` column. Those
  carry across as the money they always were.

The boundary is 1: a weight of 1 or less carried no information, and anything
above it came from the importer. No weight in between was reachable through any
screen, so nothing is being guessed at here.
"""

from django.db import migrations, models


def weights_to_prices(apps, schema_editor):
    PartKitItem = apps.get_model("parts", "PartKitItem")
    for item in PartKitItem.objects.all():
        if item.value_share is not None and item.value_share > 1:
            item.value_minor = int(item.value_share)
            item.value_currency = "USD"
            item.save(update_fields=["value_minor", "value_currency"])


def prices_to_weights(apps, schema_editor):
    """Reversing gives the weights back, which is all the old column held."""
    PartKitItem = apps.get_model("parts", "PartKitItem")
    for item in PartKitItem.objects.all():
        item.value_share = item.value_minor if item.value_minor else 1
        item.save(update_fields=["value_share"])


class Migration(migrations.Migration):

    dependencies = [
        ('parts', '0006_stocklot_from_kit_lot_alter_stocktransaction_reason_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='part',
            name='typical_cost_currency',
            field=models.CharField(blank=True, default='USD', max_length=3),
        ),
        migrations.AddField(
            model_name='part',
            name='typical_cost_minor',
            field=models.BigIntegerField(blank=True, default=None, help_text='Minor units (e.g. cents). Never a float.', null=True, verbose_name='usual price'),
        ),
        migrations.AddField(
            model_name='partkititem',
            name='value_currency',
            field=models.CharField(blank=True, default='USD', max_length=3),
        ),
        migrations.AddField(
            model_name='partkititem',
            name='value_minor',
            field=models.BigIntegerField(blank=True, default=None, help_text='Minor units (e.g. cents). Never a float.', null=True, verbose_name='price each'),
        ),
        # Between the two schema halves, so the old column is still there to
        # read from and the new one is already there to write to.
        migrations.RunPython(weights_to_prices, prices_to_weights),
        migrations.RemoveField(
            model_name='partkititem',
            name='value_share',
        ),
    ]
