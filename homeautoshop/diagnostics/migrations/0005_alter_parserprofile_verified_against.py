"""Widen `verified_against` from one report name to a list of them.

`0004` added it as a `CharField`, which defaults every existing row to the
empty string. Postgres casts a text column to `jsonb` with `USING col::jsonb`,
and `''` is not valid JSON — so the bare `AlterField` this started as failed
with `invalid input syntax for type json` and took the app down with it.

SQLite never noticed, because it stores JSON in a text column and does not
cast anything, which is why the whole test suite passed over the problem.
That asymmetry is worth remembering: a field-type change is one of the few
things the SQLite development database cannot tell you about.

So the data is made valid first, in its own step, before the column changes
underneath it.
"""

from django.db import migrations, models


def to_empty_lists(apps, schema_editor):
    """`''` becomes `'[]'` — still text at this point, and valid JSON."""
    ParserProfile = apps.get_model("diagnostics", "ParserProfile")
    ParserProfile.objects.filter(verified_against="").update(verified_against="[]")


def back_to_blank(apps, schema_editor):
    ParserProfile = apps.get_model("diagnostics", "ParserProfile")
    ParserProfile.objects.filter(verified_against=[]).update(verified_against="")


class Migration(migrations.Migration):

    dependencies = [
        ('diagnostics', '0004_parserprofile_author_parserprofile_verified_against'),
    ]

    operations = [
        migrations.RunPython(to_empty_lists, back_to_blank),
        migrations.AlterField(
            model_name='parserprofile',
            name='verified_against',
            field=models.JSONField(blank=True, default=list, help_text='Captured reports this profile was run against and read correctly. Checked when published, so it is a fact rather than a claim — and several rather than one, because a single report proves only that the profile fits that report.'),
        ),
    ]
