"""Correct British spelling in a shipped service definition name.

`Fuel stabiliser / winterise` went out in the seed data. The project's default
is American English, and this is user-facing text on everybody's schedule
screen, not a comment.

**Keyed on `translation_key`, not on the name.** That field exists precisely so
a shipped item has an identity independent of the words shown for it — which
means this migration is idempotent, survives an instance where somebody has
already renamed it by hand, and cannot half-apply. Editing `seed.py` alone
would not have been enough either: `install()` matches definitions by name, so
a renamed one would have arrived as a *second* definition beside the first,
leaving two rows and every existing schedule pointing at the old one.

The key itself is deliberately left as `svc.winterise`. A translation key is an
identifier, and changing it to match the corrected spelling would break exactly
the thing it exists to keep stable — for a cosmetic gain nobody sees.
"""

from django.db import migrations

RENAMES = [
    ("svc.winterise", "Fuel stabilizer / winterize"),
]


def to_american(apps, schema_editor):
    ServiceDefinition = apps.get_model("maintenance", "ServiceDefinition")
    for key, name in RENAMES:
        # `filter().update()` rather than get-and-save: an instance that never
        # seeded has no such row, and that is not an error.
        ServiceDefinition.objects.filter(translation_key=key).exclude(
            name=name
        ).update(name=name)


def back_to_british(apps, schema_editor):
    ServiceDefinition = apps.get_model("maintenance", "ServiceDefinition")
    ServiceDefinition.objects.filter(translation_key="svc.winterise").update(
        name="Fuel stabiliser / winterise"
    )


class Migration(migrations.Migration):

    dependencies = [
        ("maintenance", "0003_scheduletemplate_author"),
    ]

    operations = [
        migrations.RunPython(to_american, back_to_british),
    ]
