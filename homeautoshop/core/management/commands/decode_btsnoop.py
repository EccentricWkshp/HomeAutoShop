"""Read a Bluetooth capture and print what an adapter actually said.

    python manage.py decode_btsnoop "path/to/btsnoop_hci.cfa"

Written while working out the GEARWRENCH GWSCAN's protocol, and kept because
the next silent adapter will need the same afternoon and should not cost the
same days. The reasoning lives in `homeautoshop/diagnostics/btsnoop.py`; this
is the way to point it at a file.

Three things it answers, in the order they get asked:

* **Is there any pairing?** A vendor saying a device must be "paired" may mean
  Bluetooth pairing or may mean typing an activation code into their app, and
  those send an investigation in opposite directions. The capture settles it.
* **What does the framing look like?** The decoded/undecoded counts are the
  test of a framing guess: a rule that is nearly right decodes most frames, and
  "most" is how a protocol reader ships a bug.
* **What was on the wire?** Which is usually ordinary OBD-II wrapped in
  something thin, and worth seeing rather than assuming.

Captures carry a vehicle's VIN and the adapter's serial in clear text, so
nothing here writes a file: what it finds goes to the terminal, and where it
goes next is the operator's decision.
"""

from __future__ import annotations

import datetime as dt
import struct
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from homeautoshop.diagnostics import btsnoop


def when(ts: int) -> str:
    return dt.datetime.fromtimestamp(
        (ts - btsnoop.EPOCH_US) / 1_000_000, dt.UTC
    ).strftime("%Y-%m-%d %H:%M:%S")


def describe(payload: bytes) -> str:
    """One line about a frame's payload, for the shapes worth naming.

    Deliberately shallow. Naming every sub-command would be a second place to
    keep a protocol description true, and the notes beside the samples are the
    first — so this names the wrapper and prints the rest as bytes.
    """
    if not payload:
        return "(empty)"
    tag = payload[0]
    if tag in (0x09, 0x0A) and len(payload) >= 6:
        can_id = struct.unpack(">H", payload[3:5])[0]
        data = payload[5:5 + 8]
        way = "sent" if tag == 0x09 else "received"
        return f"CAN {way} id=0x{can_id:03X} {data.hex()}"
    text = "".join(chr(b) if 32 <= b < 127 else "." for b in payload)
    return f"{payload.hex()}  |{text}|"


class Command(BaseCommand):
    help = "Decode an Android btsnoop_hci capture of a Bluetooth LE adapter."

    def add_arguments(self, parser):
        parser.add_argument("path", help="The btsnoop_hci capture to read.")
        parser.add_argument(
            "--frames",
            action="store_true",
            help="Print every frame, not just the summary.",
        )
        parser.add_argument(
            "--session",
            type=int,
            default=None,
            help="Only this session, numbered from 1 in the order they opened.",
        )

    def handle(self, *args, **options):
        path = Path(options["path"])
        if not path.exists():
            raise CommandError(f"no such capture: {path}")
        try:
            capture = btsnoop.Capture.read(path)
        except btsnoop.Malformed as exc:
            raise CommandError(str(exc)) from exc

        links = capture.uart_links()
        if not links:
            # Worth saying which of the two it is: a capture of the wrong
            # session and a capture of a device using another profile look
            # identical from here, and lead different places.
            self.stdout.write(
                f"{len(capture.links)} connection(s), none of them Nordic UART. "
                "Either the adapter was not used while recording, or it speaks "
                "something else."
            )
            return

        self.stdout.write(f"{len(links)} session(s) on the Nordic UART profile:")
        for n, link in enumerate(links, start=1):
            closed = when(link.closed_at) if link.closed_at else "still open"
            self.stdout.write(
                f"  {n}. {link.peer}  {when(link.opened_at)} to {closed} UTC"
            )
            self.stdout.write(
                f"     pairing: {'yes' if link.paired else 'none'}   "
                f"encryption: {'yes' if link.encrypted else 'none'}"
            )

        for n, link in enumerate(links, start=1):
            if options["session"] and options["session"] != n:
                continue
            self.stdout.write("")
            self.stdout.write(f"=== session {n}: {link.peer}")
            for who, marker in (("host", btsnoop.HOST), ("adapter", btsnoop.ADAPTER)):
                blob = bytes(link.streams.get(who, b""))
                if not blob:
                    continue
                decoded = [f for f in btsnoop.frames(blob, marker)]
                good = sum(1 for f in decoded if f.checksum_ok)
                self.stdout.write(
                    f"  {who:7s} {len(blob):6d} bytes, "
                    f"{good} frames decoded, {len(decoded) - good} not"
                )
            if not options["frames"]:
                continue
            for who, marker in (("host", btsnoop.HOST), ("adapter", btsnoop.ADAPTER)):
                blob = bytes(link.streams.get(who, b""))
                if not blob:
                    continue
                arrow = "-->" if who == "host" else "<--"
                for frame in btsnoop.frames(blob, marker):
                    flag = "" if frame.checksum_ok else "   [checksum]"
                    self.stdout.write(
                        f"  {arrow} seq={frame.seq:02x} kind={frame.kind:02x} "
                        f"{describe(frame.payload)}{flag}"
                    )
