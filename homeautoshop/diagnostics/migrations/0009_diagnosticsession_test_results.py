from django.db import migrations, models


class Migration(migrations.Migration):
    """Whole results from a bench tester, kept beside the flat extraction.

    A new column rather than a shape change to `extraction`, because the two
    hold different things and one of them is evidence. `extraction` is what the
    machine read and is never edited; `test_results` is the copy an operator
    corrects on the review screen. Merging them would answer "what did the tool
    actually say?" with whatever somebody typed afterwards.
    """

    dependencies = [("diagnostics", "0008_installedcodelist")]

    operations = [
        migrations.AddField(
            model_name="diagnosticsession",
            name="test_results",
            field=models.JSONField(blank=True, default=list),
        ),
    ]
