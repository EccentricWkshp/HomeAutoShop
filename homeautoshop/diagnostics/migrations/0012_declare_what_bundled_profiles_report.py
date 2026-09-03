from django.db import migrations

#: `(name, version)` -> what that bundled profile's reports contain.
#:
#: Frozen here rather than imported from `profiles.SEED`, because a migration
#: that reads today's source describes today's source, not the change it was
#: written to make.
DECLARED = {
    ("XTOOL D8 - DTC report", 1): ["codes", "live_data"],
    ("Generic code list - plain text", 1): ["codes"],
    ("TOPDON BT600 Plus - printed test report", 1): ["test_results"],
}


def declare(apps, schema_editor):
    """Fill in `reports` on the bundled profiles already installed.

    `seed()` is idempotent by skipping any profile row that already exists, so
    that an operator's edit is never overwritten. That rule is right and it is
    also why a field added to a bundled profile reaches nobody who is already
    running: their row was written before the field existed, and re-seeding
    steps over it forever. Bumping the version would not help either — that
    makes a *new* row, and the sessions in a vehicle's history point at the old
    one.

    Empty is not an edit here. `reports` did not exist until the migration
    before this, there is no screen that sets it, and an imported profile
    carries its own value in the YAML — so an empty list on one of these three
    is nobody's answer rather than somebody's.
    """
    ParserProfile = apps.get_model("diagnostics", "ParserProfile")
    for (name, version), reports in DECLARED.items():
        ParserProfile.objects.filter(name=name, version=version, reports=[]).update(
            reports=reports
        )


class Migration(migrations.Migration):
    dependencies = [("diagnostics", "0011_parserprofile_reports")]

    operations = [
        # No reverse: undeclaring is what these rows already are, and
        # `could_report` treats it as "nobody has said" rather than "nothing",
        # so leaving the value in place is safe on a rollback.
        migrations.RunPython(declare, migrations.RunPython.noop),
    ]
