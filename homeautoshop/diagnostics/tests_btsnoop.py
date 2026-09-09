"""
Reading a Bluetooth capture (`homeautoshop/diagnostics/btsnoop.py`).

The real captures cannot be tested against: they are gitignored, because each
one carries a vehicle's VIN and an adapter's serial in clear text. So the
fixtures here are built byte by byte, which is the better test anyway — a
synthetic capture can contain the cases that matter rather than the cases one
afternoon in a garage happened to produce.

Three of those cases are the ones that cost real time:

* **Byte stuffing.** The marker occurs in payloads, and the first reading of
  this protocol missed the escaping. It fails quietly: lengths come out wrong
  and checksums fail on about a quarter of frames, which reads as a flaky link.
* **Fragmentation.** A GATT write longer than the link's payload arrives in
  pieces and means nothing until they are put back together.
* **A cached attribute database.** A phone that has met the adapter before
  repeats no discovery, so a later connection's handles are known only from an
  earlier one — and without carrying them across, the longest session in a file
  decodes as empty.
"""

from __future__ import annotations

import struct
import tempfile
from pathlib import Path

from django.test import SimpleTestCase

from homeautoshop.diagnostics import btsnoop

NUS_TX = "6e400003-b5a3-f393-e0a9-e50e24dcca9e"
NUS_RX = "6e400002-b5a3-f393-e0a9-e50e24dcca9e"

TX_HANDLE = 0x000C
RX_HANDLE = 0x000F

ADAPTER_ADDRESS = "5B:49:BF:F2:29:88"


def le_uuid(text: str) -> bytes:
    return bytes.fromhex(text.replace("-", ""))[::-1]


def record(payload: bytes, *, sent: bool, ts: int = 0) -> bytes:
    flags = 0 if sent else 1
    return struct.pack(">IIIIq", len(payload), len(payload), flags, 0, ts) + payload


def capture_file(packets) -> bytes:
    head = btsnoop.MAGIC + struct.pack(">II", 1, 1002)
    return head + b"".join(record(p, sent=s, ts=t) for p, s, t in packets)


def connection_complete(handle: int, address: str) -> bytes:
    peer = bytes.fromhex(address.replace(":", ""))[::-1]
    params = (
        bytes([0x01, 0x00])                      # subevent, status
        + struct.pack("<H", handle)
        + bytes([0x00, 0x00])                    # role, peer address type
        + peer
        + struct.pack("<HHH", 6, 0, 500)
        + bytes([0x00])
    )
    return bytes([0x04, 0x3E, len(params)]) + params


def acl(handle: int, l2cap: bytes, *, start: bool = True) -> bytes:
    flags = handle | ((0x2 if start else 0x1) << 12)
    return bytes([0x02]) + struct.pack("<HH", flags, len(l2cap)) + l2cap


def att(payload: bytes) -> bytes:
    return struct.pack("<HH", len(payload), btsnoop.ATT_CID) + payload


def find_info_response() -> bytes:
    """The discovery an app does on a first connection, cut to two handles."""
    body = bytes([0x05, 0x02])
    body += struct.pack("<H", TX_HANDLE) + le_uuid(NUS_TX)
    body += struct.pack("<H", RX_HANDLE) + le_uuid(NUS_RX)
    return body


def framed(seq: int, kind: int, payload: bytes, marker: int) -> bytes:
    """One whole frame on the wire, stuffing and checksum included."""
    body = bytes([seq, len(payload), kind]) + payload
    check = 0
    for b in body:
        check ^= b
    return bytes([marker]) + btsnoop.stuff(body + bytes([check]), marker)


def write_command(handle: int, value: bytes) -> bytes:
    return bytes([0x52]) + struct.pack("<H", handle) + value


def notification(handle: int, value: bytes) -> bytes:
    return bytes([0x1B]) + struct.pack("<H", handle) + value


class StuffingTests(SimpleTestCase):
    """The escaping, on its own."""

    def test_a_payload_with_no_marker_in_it_is_left_alone(self):
        self.assertEqual(btsnoop.stuff(b"\x01\x02\x03", 0x55), b"\x01\x02\x03")

    def test_the_marker_is_escaped(self):
        self.assertEqual(btsnoop.stuff(b"\x55", 0x55), b"\x56\x02")

    def test_and_so_is_the_escape_itself(self):
        """Otherwise an escape in the data would start one."""
        self.assertEqual(btsnoop.stuff(b"\x56", 0x55), b"\x56\x01")

    def test_it_round_trips(self):
        for marker in (btsnoop.HOST, btsnoop.ADAPTER):
            for raw in (b"", b"\x55\x56\xaa\xab", bytes(range(256))):
                with self.subTest(marker=marker, length=len(raw)):
                    self.assertEqual(
                        btsnoop.unstuff(btsnoop.stuff(raw, marker), marker), raw
                    )

    def test_a_frame_containing_the_marker_still_checksums(self):
        """The case the first reading of this protocol got wrong.

        The vendor's firmware reply carries a `V`, which is `0x56` — the escape
        on the adapter's side. Unstuffed it is 13 bytes and adds up; read raw it
        is 14 bytes and does not.
        """
        payload = b"\x02\x01\x81V103\x00\x00\x00\x00\x00\x00"
        wire = framed(0x03, 0x60, payload, btsnoop.ADAPTER)

        decoded = list(btsnoop.frames(wire, btsnoop.ADAPTER))

        self.assertEqual(len(decoded), 1)
        self.assertTrue(decoded[0].checksum_ok)
        self.assertEqual(decoded[0].payload, payload)
        self.assertGreater(len(wire) - 1, len(payload) + 4, "nothing was escaped")

    def test_a_frame_with_a_corrupted_checksum_is_reported_not_dropped(self):
        wire = bytearray(framed(0x03, 0x60, b"\x01\x02", btsnoop.ADAPTER))
        wire[-1] ^= 0xFF

        decoded = list(btsnoop.frames(bytes(wire), btsnoop.ADAPTER))

        self.assertEqual(len(decoded), 1)
        self.assertFalse(decoded[0].checksum_ok)


class ReadingACaptureTests(SimpleTestCase):
    """From a file of HCI records to what the adapter said."""

    def written(self, packets) -> Path:
        directory = tempfile.mkdtemp()
        path = Path(directory) / "btsnoop_hci.cfa"
        path.write_bytes(capture_file(packets))
        self.addCleanup(lambda: path.unlink(missing_ok=True))
        return path

    def one_session(self, extra=()):
        packets = [
            (connection_complete(2, ADAPTER_ADDRESS), False, 0),
            (acl(2, att(find_info_response())), False, 1),
        ]
        packets.extend(extra)
        return self.written(packets)

    def test_a_file_that_is_not_a_capture_says_so(self):
        directory = tempfile.mkdtemp()
        path = Path(directory) / "not-a-capture.bin"
        path.write_bytes(b"PK\x03\x04 this is a zip")

        with self.assertRaises(btsnoop.Malformed):
            btsnoop.Capture.read(path)

    def test_a_connection_is_found_with_the_address_it_was_to(self):
        capture = btsnoop.Capture.read(self.one_session())

        self.assertEqual(len(capture.links), 1)
        self.assertEqual(capture.links[0].peer, ADAPTER_ADDRESS)

    def test_a_written_frame_comes_back_out(self):
        wire = framed(0x02, 0x60, b"\x02\x01\x82", btsnoop.HOST)
        path = self.one_session([(acl(2, att(write_command(RX_HANDLE, wire))), True, 2)])

        link = btsnoop.Capture.read(path).uart_links()[0]
        decoded = list(btsnoop.frames(bytes(link.streams["host"]), btsnoop.HOST))

        self.assertEqual([f.payload for f in decoded], [b"\x02\x01\x82"])
        self.assertTrue(all(f.checksum_ok for f in decoded))

    def test_a_notification_split_across_two_packets_is_put_back_together(self):
        """A GATT value longer than the link's payload arrives in fragments,
        and each half on its own decodes to nothing."""
        wire = framed(0x02, 0x60, b"GWSCAN" + bytes(20), btsnoop.ADAPTER)
        whole = att(notification(TX_HANDLE, wire))
        path = self.one_session([
            (acl(2, whole[:12], start=True), False, 2),
            (acl(2, whole[12:], start=False), False, 3),
        ])

        link = btsnoop.Capture.read(path).uart_links()[0]
        decoded = list(btsnoop.frames(bytes(link.streams["adapter"]), btsnoop.ADAPTER))

        self.assertEqual(len(decoded), 1)
        self.assertTrue(decoded[0].checksum_ok)
        self.assertTrue(decoded[0].payload.startswith(b"GWSCAN"))

    def test_a_later_connection_inherits_the_discovery_of_an_earlier_one(self):
        """The phone caches the attribute database and does not ask twice."""
        wire = framed(0x02, 0x60, b"\x02\x01\x82", btsnoop.HOST)
        path = self.one_session([
            (bytes([0x04, 0x05, 0x04, 0x00]) + struct.pack("<H", 2) + bytes([0x16]),
             False, 4),
            (connection_complete(7, ADAPTER_ADDRESS), False, 5),
            (acl(7, att(write_command(RX_HANDLE, wire))), True, 6),
        ])

        capture = btsnoop.Capture.read(path)
        later = capture.links[1]
        decoded = list(btsnoop.frames(bytes(later.streams.get("host", b"")), btsnoop.HOST))

        self.assertEqual(len(capture.links), 2)
        self.assertEqual(later.attributes, capture.links[0].attributes)
        self.assertEqual([f.payload for f in decoded], [b"\x02\x01\x82"])

    def test_a_link_with_no_pairing_says_so(self):
        """The question the whole exercise started with: the vendor says the
        device must be 'paired', and means their activation screen."""
        link = btsnoop.Capture.read(self.one_session()).links[0]

        self.assertFalse(link.paired)
        self.assertFalse(link.encrypted)

    def test_pairing_is_noticed_when_it_happens(self):
        """So that "no pairing" is a reading and not a blind spot."""
        pairing = struct.pack("<HH", 7, btsnoop.SMP_CID) + bytes([0x01] + [0] * 6)
        path = self.one_session([(acl(2, pairing), True, 2)])

        self.assertTrue(btsnoop.Capture.read(path).links[0].paired)

    def test_encryption_is_noticed_too(self):
        change = bytes([0x04, 0x08, 0x04, 0x00]) + struct.pack("<H", 2) + bytes([0x01])
        path = self.one_session([(change, False, 2)])

        self.assertTrue(btsnoop.Capture.read(path).links[0].encrypted)

    def test_a_truncated_final_record_does_not_lose_the_rest(self):
        """Captures are pulled off phones mid-write; that is the normal case."""
        good = capture_file([(connection_complete(2, ADAPTER_ADDRESS), False, 0)])
        directory = tempfile.mkdtemp()
        path = Path(directory) / "cut.cfa"
        path.write_bytes(good + struct.pack(">IIIIq", 40, 40, 0, 0, 9) + b"\x02\x00")

        capture = btsnoop.Capture.read(path)

        self.assertEqual(len(capture.links), 1)

    def test_a_capture_of_something_else_reports_no_uart_link(self):
        path = self.written([(connection_complete(2, "AA:BB:CC:DD:EE:FF"), False, 0)])

        self.assertEqual(btsnoop.Capture.read(path).uart_links(), [])
