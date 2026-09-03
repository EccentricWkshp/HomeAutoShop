import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    """What a re-reading is a re-reading of.

    Nothing recorded it, so re-parsing a confirmed report and confirming the
    result filed the same test in a vehicle's history twice — with no way to
    take either out, since removal was offered on drafts alone.
    """

    dependencies = [("diagnostics", "0009_diagnosticsession_test_results")]

    operations = [
        migrations.AddField(
            model_name="diagnosticsession",
            name="supersedes",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="superseded_by",
                to="diagnostics.diagnosticsession",
            ),
        ),
    ]
