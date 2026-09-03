from django.db import migrations, models


class Migration(migrations.Migration):
    """What a tool's report can contain, declared rather than guessed.

    A battery tester does not report trouble codes, and a screen showing one
    "0 codes" reports the absence of a thing that was never going to be there.
    Inferring it from an empty list would be wrong the other way just as often:
    a scan tool that found none is the best outcome there is.

    Empty means *undeclared*. Every profile written before this has an empty
    list, and reading that as "reports nothing" would hide what they do read.
    """

    dependencies = [("diagnostics", "0010_diagnosticsession_supersedes")]

    operations = [
        migrations.AddField(
            model_name="parserprofile",
            name="reports",
            field=models.JSONField(blank=True, default=list),
        ),
    ]
