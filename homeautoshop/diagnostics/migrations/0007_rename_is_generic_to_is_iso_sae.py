"""
`is_generic` becomes `is_iso_sae`, because that is what the standard calls it.

J2012 divides codes into **ISO/SAE controlled** and **manufacturer controlled**.
"Generic" is the shop-floor word for the first of those and appears nowhere in
the specification, which made every screen and docstring here describe the
distinction in a vocabulary its own source document does not use.

`RenameField`, not a drop and an add: `diagnostic_code` is append-only and the
column holds a reading of a real vehicle.
"""

from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [("diagnostics", "0006_parserprofile_live_data_extractor")]

    operations = [
        migrations.RenameField(
            model_name="diagnosticcode",
            old_name="is_generic",
            new_name="is_iso_sae",
        ),
    ]
