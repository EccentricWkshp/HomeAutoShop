"""A fifth confidence, for parts where fitment is not a question.

Reported as: an A/C system flush, brake cleaner, a rag or a hose clamp does
not really fit any of confirmed, does not fit, stated by vendor, or
unverified. It does not — every one of those is a sentence about evidence, and
there is no evidence to have about a bottle of cleaner.

Choices are not part of the table, so nothing here touches a column. The
migration exists so the model state and the migration state agree, which is
what keeps the *next* change to this field from generating a diff nobody
wrote.
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [("parts", "0013_core_state")]

    operations = [
        migrations.AlterField(
            model_name="partfitment",
            name="confidence",
            field=models.CharField(
                choices=[
                    ("confirmed_installed", "Confirmed — installed on this vehicle"),
                    ("does_not_fit", "Does not fit — tried it"),
                    ("general_purpose", "General purpose — fits anything"),
                    ("stated_by_vendor", "Stated by vendor"),
                    ("unverified", "Unverified"),
                ],
                default="unverified",
                max_length=24,
            ),
        ),
    ]
