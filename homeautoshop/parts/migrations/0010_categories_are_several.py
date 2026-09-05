"""A part is in several categories, and each of them is a row.

The generated migration added the relation and dropped the column in the same
breath, which would have thrown away every category the shop had typed. The
`RunPython` between the two is the whole point of hand-editing this: it reads
each part's old single value, folds it into a `Category` — case-insensitively,
so `Brakes` and `brakes` converge on one row here rather than surviving as two
— and files the part under it. A value that held a compound, `Electrical /
Lighting`, becomes the two categories somebody was trying to say.

Reversible. Going back keeps the first category alphabetically, because a
single column can hold one and dropping the migration should not be the thing
that decides *which* silently.
"""


import re

import django.db.models.functions.text
import django.utils.timezone
import homeautoshop.core.models
from django.db import migrations, models


SEPARATORS = re.compile(r"[,;/|]")


def file_them(apps, schema_editor):
    Category = apps.get_model("parts", "Category")
    Part = apps.get_model("parts", "Part")

    known = {}
    for part in Part.objects.exclude(category="").iterator():
        for written in SEPARATORS.split(part.category or ""):
            name = " ".join(written.split())[:64]
            if not name:
                continue
            key = name.casefold()
            if key not in known:
                # `get_or_create` on the raw name would make a second row for a
                # second spelling, which is the thing this migration exists to
                # end. Matched the way the constraint matches.
                found = Category.objects.filter(name__iexact=name).first()
                known[key] = found or Category.objects.create(name=name)
            part.categories.add(known[key])


def unfile_them(apps, schema_editor):
    Part = apps.get_model("parts", "Part")
    # `chunk_size` because a prefetch cannot stream without one, and the
    # prefetch is what stops this being one query per part.
    for part in Part.objects.prefetch_related("categories").iterator(chunk_size=500):
        first = sorted((c.name for c in part.categories.all()), key=str.casefold)
        part.category = first[0][:64] if first else ""
        part.save(update_fields=["category"])


class Migration(migrations.Migration):

    dependencies = [
        ('parts', '0009_part_units'),
    ]

    operations = [
        migrations.CreateModel(
            name='Category',
            fields=[
                ('id', models.UUIDField(default=homeautoshop.core.models.uuid7, editable=False, primary_key=True, serialize=False)),
                ('name', models.CharField(max_length=64)),
                ('created_at', models.DateTimeField(default=django.utils.timezone.now, editable=False)),
            ],
            options={
                'verbose_name_plural': 'categories',
                'ordering': [django.db.models.functions.text.Lower('name')],
            },
        ),
        migrations.RemoveIndex(
            model_name='part',
            name='parts_part_categor_28b15f_idx',
        ),
        migrations.AlterField(
            model_name='part',
            name='unit',
            field=models.CharField(choices=[('Counted', [('each', 'each')]), ('Weight', [('lb', 'pounds'), ('oz', 'ounces'), ('kg', 'kilograms'), ('g', 'grams')]), ('Volume', [('qt', 'quarts'), ('gal', 'gallons'), ('floz', 'fluid ounces'), ('L', 'liters'), ('ml', 'milliliters')]), ('Length', [('ft', 'feet'), ('in', 'inches'), ('m', 'meters')])], default='each', max_length=8),
        ),
        migrations.AddConstraint(
            model_name='category',
            constraint=models.UniqueConstraint(django.db.models.functions.text.Lower('name'), name='parts_category_one_spelling'),
        ),
        migrations.AddField(
            model_name='part',
            name='categories',
            field=models.ManyToManyField(blank=True, help_text='A headlight is electrical and lighting. Both is allowed.', related_name='parts', to='parts.category'),
        ),
        migrations.RunPython(file_them, unfile_them),
        migrations.RemoveField(
            model_name='part',
            name='category',
        ),
    ]
