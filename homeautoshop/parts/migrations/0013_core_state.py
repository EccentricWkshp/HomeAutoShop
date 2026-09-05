"""A core deposit has three endings, not two.

`core_returned` was a boolean, so the only way to stop a core nagging from the
Owed list was to say it went back — a false statement about a part still on the
shelf. The third ending is the shop deciding not to return it, which on a
caliper whose return shipping costs more than the deposit is the cheaper answer
and a decision somebody made rather than money lost to forgetting.

Written by hand rather than generated because the date field is *renamed*:
`core_returned_on` is a wrong word on a row that was kept, and `makemigrations`
can only tell a rename from a drop-and-add by asking.
"""

from django.db import migrations, models


def settle_returned_cores(apps, schema_editor):
    """Every core already marked returned keeps that meaning exactly."""
    PartUsage = apps.get_model("parts", "PartUsage")
    PartUsage.objects.filter(core_returned=True).update(core_state="returned")


def back_to_a_boolean(apps, schema_editor):
    """Reversing loses the distinction, because a boolean cannot hold it.

    A kept core becomes an owed one rather than a returned one: it is still on
    the shelf, and the list nagging about a deposit somebody decided to spend is
    the recoverable error. Saying it went back would not be.
    """
    PartUsage = apps.get_model("parts", "PartUsage")
    PartUsage.objects.filter(core_state="returned").update(core_returned=True)


class Migration(migrations.Migration):

    dependencies = [("parts", "0012_part_supplier_url")]

    operations = [
        migrations.RenameField(
            model_name="partusage",
            old_name="core_returned_on",
            new_name="core_settled_on",
        ),
        migrations.AddField(
            model_name="partusage",
            name="core_state",
            field=models.CharField(
                choices=[
                    ("owed", "Owed"),
                    ("returned", "Returned"),
                    ("kept", "Kept"),
                ],
                default="owed",
                db_index=True,
                max_length=8,
            ),
        ),
        migrations.RunPython(settle_returned_cores, back_to_a_boolean),
        migrations.RemoveField(model_name="partusage", name="core_returned"),
    ]
