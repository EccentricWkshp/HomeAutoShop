"""A purchase line stores what it cost, not what one of them cost.

Not a rename, which is why this is written by hand — `makemigrations` offered
one and the answer is no. The column changes meaning and every value in it
changes with it: `unit_price_minor` held the price of a single unit, and
`extended_minor` holds the price of all of them, so the data migration
multiplies each row out by its own quantity.

That multiplication is lossless in this direction, and it is the reason the old
shape had to go. Five gallons of brake cleaner bought for $182.39 has a
per-gallon price of $36.478 — not a number of cents — so stored as money it
became $36.48 and the line then claimed $182.40. Going forward, every existing
line keeps exactly the total it already displayed.

Reversing divides back, and **that** direction can lose a fraction of a cent,
because it is the direction the bug was in. Recorded here rather than made
irreversible: a migration that refuses to go back is worse to be stuck in front
of than one that says what going back costs.
"""

from decimal import ROUND_HALF_UP, Decimal

from django.db import migrations, models

from homeautoshop.core.money import MINOR_UNITS_HELP


def multiply_out(apps, schema_editor):
    Line = apps.get_model("purchasing", "PurchaseLine")
    for line in Line.objects.all().iterator(chunk_size=500):
        each = Decimal(line.unit_price_minor or 0)
        qty = Decimal(str(line.qty_ordered or 0))
        line.extended_minor = int(
            (each * qty).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
        )
        line.extended_currency = line.unit_price_currency or "USD"
        line.save(update_fields=["extended_minor", "extended_currency"])


def divide_back(apps, schema_editor):
    Line = apps.get_model("purchasing", "PurchaseLine")
    for line in Line.objects.all().iterator(chunk_size=500):
        qty = Decimal(str(line.qty_ordered or 0))
        total = Decimal(line.extended_minor or 0)
        each = total if qty == 0 else total / qty
        line.unit_price_minor = int(each.quantize(Decimal("1"), rounding=ROUND_HALF_UP))
        line.unit_price_currency = line.extended_currency or "USD"
        line.save(update_fields=["unit_price_minor", "unit_price_currency"])


class Migration(migrations.Migration):

    dependencies = [
        ("purchasing", "0004_tax_rate"),
    ]

    operations = [
        migrations.AddField(
            model_name="purchaseline",
            name="extended_minor",
            field=models.BigIntegerField(
                default=0,
                help_text=MINOR_UNITS_HELP,
                verbose_name="extended",
            ),
        ),
        migrations.AddField(
            model_name="purchaseline",
            name="extended_currency",
            field=models.CharField(blank=True, default="USD", max_length=3),
        ),
        migrations.RunPython(multiply_out, divide_back),
        migrations.RemoveField(model_name="purchaseline", name="unit_price_minor"),
        migrations.RemoveField(model_name="purchaseline", name="unit_price_currency"),
    ]
