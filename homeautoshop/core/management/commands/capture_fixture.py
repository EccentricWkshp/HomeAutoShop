"""Turn a scan-tool PDF into a corpus fixture (FR-INT-7, §8.1b).

A parser profile earns its badge by being run against captured reports, which
means somebody has to contribute the captures. This is that step: a PDF in, a
`.words.json` (the extracted word geometry) and a `.expected.json` (what the
parser makes of it) out, both named after the report.

Deliberately thin. `scantools/capture.py` already does the hard and delicate
half — it reads the report, replaces every VIN whose **check digit validates**
with a synthetic stand-in, and masks tool serials. Keying off the check digit
is what lets a part number or a calibration ID of the same shape survive
unmangled, and it is not a rule worth reimplementing a second time in a
management command. This adds the fixture generation and a screen to run it
from.

The word geometry is kept rather than the PDF because it is what the parser
actually reads, it is a fraction of the size, and it is a text file a reviewer
can search for anything that should not be there.
"""

from __future__ import annotations

import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Build a corpus fixture from a scan-tool PDF report, redacting it."

    def add_arguments(self, parser):
        parser.add_argument("pdf", help="The report to capture.")
        parser.add_argument(
            "--tool",
            required=True,
            help=(
                "Which scanner produced it, as a folder name — `xtool d8`. "
                "The corpus is filed per tool, because a flat pile of reports "
                "stops saying what made them the moment there are two."
            ),
        )

    def handle(self, *args, **options):
        from homeautoshop.scantools import capture, fixtures

        source = Path(options["pdf"])
        if not source.is_file():
            raise CommandError(f"{source} is not a file.")

        try:
            written, produced = capture.write(source, options["tool"])
        except Exception as exc:
            raise CommandError(f"That report could not be read: {exc}") from exc

        expected = fixtures.fixture_path(written)
        expected.write_text(
            json.dumps(fixtures.build(written), indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

        if produced:
            self.stdout.write(
                f"Replaced {len(produced)} VIN(s) with synthetic stand-ins: "
                f"{', '.join(sorted(produced))}"
            )
        else:
            # Said out loud rather than passed over in silence. No VIN found
            # is usually a report that never had one — and occasionally a
            # report whose VIN did not survive extraction, which is worth a
            # second look before it goes somewhere public.
            self.stdout.write(
                "No VIN was found to replace. Check the report yourself: this "
                "is either a report without one or one this could not read."
            )

        self.stdout.write(
            self.style.SUCCESS(
                f"Wrote {written.name} and {expected.name}. Read them before "
                f"committing — they are going somewhere public."
            )
        )
