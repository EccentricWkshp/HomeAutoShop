"""
Reading a Bluetooth capture, down to what a scan-tool adapter actually said.

An adapter that answers nothing is the hardest kind to support: connecting
proves the transport and nothing else, and the GEARWRENCH GWSCAN gets all the
way to a working GATT connection before going silent (see
`Artifacts/samples/code-reader/GEARWRENCH GWSCAN/notes.md`). Guessing at the
framing cost days; an Android `btsnoop_hci` log of the vendor's own app settled
it in an afternoon. This is the part of that afternoon worth keeping.

Four layers, each one only as much as the layer above needs:

* **btsnoop** — a header and fixed-size records, one HCI packet each.
* **HCI/ACL** — reassembly, because a GATT write longer than the link's payload
  arrives in fragments and means nothing until they are put back together.
* **L2CAP/ATT** — the writes and notifications, tagged with the connection they
  belong to, and a handle-to-UUID map built from whatever discovery the capture
  happens to contain.
* **The adapter's own framing** — for the Nordic UART profile, which is a pipe
  and not a protocol: what goes through it is the vendor's business.

**The stuffing is the part that matters.** A stream framed by a marker byte has
to say what it means when that byte occurs in the data, and this one escapes it.
A reader that skips that step sees wrong lengths and failing checksums on
roughly a quarter of frames — which looks exactly like a flaky link, and is not.

The only adapter-specific things here are the Nordic UART UUIDs and the two
frame markers; everything below them is Bluetooth. And nothing here writes: a
capture is evidence, and this only reads it.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field

MAGIC = b"btsnoop\x00"

#: btsnoop timestamps count microseconds from year zero. This is 1970-01-01 in
#: that scale, which is what makes them comparable to anything else.
EPOCH_US = 0x00DCDDB30F2F8000

ATT_CID = 0x0004
SMP_CID = 0x0006

#: Nordic UART: the profile this family of adapters advertises. RX is written
#: to, TX notifies back, and `6e400004` is a non-standard fourth characteristic
#: the GWSCAN accepts writes on as well.
NUS_WRITE = ("6e400002", "6e400004")
NUS_NOTIFY = ("6e400003",)

#: The marker each direction frames with, and the escape that follows from it.
HOST = 0xAA
ADAPTER = 0x55

ATT_OPS = {
    0x01: "ERROR_RSP", 0x02: "MTU_REQ", 0x03: "MTU_RSP", 0x04: "FIND_INFO_REQ",
    0x05: "FIND_INFO_RSP", 0x06: "FIND_BY_TYPE_REQ", 0x07: "FIND_BY_TYPE_RSP",
    0x08: "READ_BY_TYPE_REQ", 0x09: "READ_BY_TYPE_RSP", 0x0A: "READ_REQ",
    0x0B: "READ_RSP", 0x0C: "READ_BLOB_REQ", 0x0D: "READ_BLOB_RSP",
    0x10: "READ_BY_GROUP_REQ", 0x11: "READ_BY_GROUP_RSP", 0x12: "WRITE_REQ",
    0x13: "WRITE_RSP", 0x16: "PREPARE_WRITE_REQ", 0x18: "EXEC_WRITE_REQ",
    0x1B: "NOTIFY", 0x1D: "INDICATE", 0x1E: "CONFIRM", 0x52: "WRITE_CMD",
}

SMP_OPS = {
    0x01: "PAIRING_REQ", 0x02: "PAIRING_RSP", 0x03: "PAIRING_CONFIRM",
    0x04: "PAIRING_RANDOM", 0x05: "PAIRING_FAILED", 0x06: "ENCRYPT_INFO",
    0x07: "CENTRAL_ID", 0x08: "IDENTITY_INFO", 0x09: "IDENTITY_ADDR",
    0x0A: "SIGNING_INFO", 0x0B: "SECURITY_REQ", 0x0C: "PUBLIC_KEY",
    0x0D: "DHKEY_CHECK", 0x0E: "KEYPRESS",
}

#: These carry data rather than describing the database, so they are the ones
#: a stream is rebuilt from.
CARRIES_VALUE = (0x12, 0x52, 0x1B, 0x1D)


class Malformed(Exception):
    """The file is not a btsnoop capture, or stops in the middle of one."""


# ---------------------------------------------------------------------------
# btsnoop
# ---------------------------------------------------------------------------


def records(path):
    """Yield `(timestamp, sent_by_host, hci_packet)` for every record.

    A truncated final record is ignored rather than raised on: a capture pulled
    off a phone while it was still being written is the ordinary case, and the
    hundred thousand records before the cut are still evidence.
    """
    with open(path, "rb") as fh:
        head = fh.read(16)
        if head[:8] != MAGIC:
            raise Malformed("not a btsnoop capture")
        while True:
            header = fh.read(24)
            if len(header) < 24:
                return
            _original, length, flags, _drops, ts = struct.unpack(">IIIIq", header)
            payload = fh.read(length)
            if len(payload) < length:
                return
            # Flag bit 0 is the direction: clear means the host sent it.
            yield ts, not (flags & 1), payload


def address(raw: bytes) -> str:
    """Six little-endian bytes as the address people read off a scanner."""
    return ":".join(f"{b:02X}" for b in raw[::-1])


def uuid(raw: bytes) -> str:
    if len(raw) == 2:
        return f"{struct.unpack('<H', raw)[0]:04x}"
    if len(raw) == 16:
        b = raw[::-1]
        return f"{b[0:4].hex()}-{b[4:6].hex()}-{b[6:8].hex()}-{b[8:10].hex()}-{b[10:16].hex()}"
    return raw.hex()


# ---------------------------------------------------------------------------
# The adapter's framing
# ---------------------------------------------------------------------------


def unstuff_upto(raw: bytes, marker: int, count: int | None = None):
    """Undo the escaping, stopping after `count` bytes of output.

    The escape is the marker plus one, and the byte after it says which value
    was meant: `02` for the marker itself, `01` for the escape. Anything else
    after an escape is left alone rather than guessed at — a stream with a
    dropped byte should look wrong here, not silently decode into something
    plausible.

    Returns `(bytes, consumed)`. `count` is what a live reader needs and a
    capture does not: a frame states its own length, so the reader unstuffs
    exactly that much and knows where the next frame starts. Getting fewer
    bytes than asked for means the rest has not arrived yet.

    A lone escape at the very end is **not** consumed, for the same reason: on
    a live link it is half of a pair whose other half is still in flight. Only
    a truncated stream ends that way, and dropping it beats inventing a byte.
    """
    escape = (marker + 1) & 0xFF
    out = bytearray()
    i = 0
    while i < len(raw) and (count is None or len(out) < count):
        if raw[i] == escape:
            if i + 1 >= len(raw):
                break
            nxt = raw[i + 1]
            if nxt == 0x02:
                out.append(marker)
                i += 2
                continue
            if nxt == 0x01:
                out.append(escape)
                i += 2
                continue
        out.append(raw[i])
        i += 1
    return bytes(out), i


def unstuff(raw: bytes, marker: int) -> bytes:
    """The whole of a stream, unescaped. See `unstuff_upto`."""
    return unstuff_upto(raw, marker)[0]


def stuff(raw: bytes, marker: int) -> bytes:
    """The other half of `unstuff`, for a client that has to send a frame.

    Kept beside the reader rather than left to whoever writes the client: a
    stuffing rule and an unstuffing rule that disagree fail in the least
    helpful way available, which is intermittently and only on the payloads
    that happen to contain the marker.
    """
    escape = (marker + 1) & 0xFF
    out = bytearray()
    for b in raw:
        if b == marker:
            out += bytes([escape, 0x02])
        elif b == escape:
            out += bytes([escape, 0x01])
        else:
            out.append(b)
    return bytes(out)


@dataclass
class Frame:
    """One message in the adapter's own protocol."""

    seq: int
    kind: int
    payload: bytes
    checksum_ok: bool
    #: Everything between two markers, unstuffed — kept so a frame that does
    #: not add up can be looked at rather than merely counted.
    body: bytes


def frames(blob: bytes, marker: int):
    """Split one direction of a Nordic UART stream into frames.

        marker  SEQ  LEN  KIND  PAYLOAD[LEN]  XOR(SEQ..PAYLOAD)

    The marker cannot occur inside a frame, so the bytes between two markers
    are exactly one frame's — which is what makes this recoverable from a
    stream that starts mid-message, as a capture usually does.
    """
    i = 0
    while i < len(blob):
        if blob[i] != marker:
            i += 1
            continue
        end = blob.find(bytes([marker]), i + 1)
        if end < 0:
            end = len(blob)
        body = unstuff(blob[i + 1:end], marker)
        i = end
        if len(body) < 4:
            continue
        seq, length, kind = body[0], body[1], body[2]
        if len(body) < 3 + length + 1:
            yield Frame(seq, kind, b"", False, body)
            continue
        stated = body[:3 + length + 1]
        check = 0
        for b in stated[:-1]:
            check ^= b
        yield Frame(seq, kind, stated[3:3 + length], check == stated[-1], body)


# ---------------------------------------------------------------------------
# HCI, and what was said on top of it
# ---------------------------------------------------------------------------


@dataclass
class Link:
    """One BLE connection, from the moment it opened."""

    handle: int
    peer: str
    opened_at: int
    closed_at: int | None = None
    encrypted: bool = False
    paired: bool = False
    #: `{attribute handle: uuid}`, as far as this capture's discovery says.
    attributes: dict = field(default_factory=dict)
    #: Every attribute value written or notified on this link, in order, as
    #: `(attribute handle, bytes)`. Kept raw because which handle is the pipe
    #: is not always known until the whole file has been read.
    values: list = field(default_factory=list)
    #: `{"host"|"adapter": bytes}` — the Nordic UART streams, reassembled.
    streams: dict = field(default_factory=dict)


class Capture:
    """Everything one capture says, decoded on the way past.

    Connection handles are reused by the controller, so a `Link` is closed and
    a new one started rather than the handle being treated as an identity —
    three sessions with one adapter in one file is the ordinary case, and
    merging them would put one session's answers under another's questions.
    """

    def __init__(self):
        self.links: list[Link] = []
        self.open: dict[int, Link] = {}
        self._acl: dict[int, bytearray] = {}
        self._acl_host: dict[int, bool] = {}

    # -- reading --------------------------------------------------------

    @classmethod
    def read(cls, path) -> "Capture":
        capture = cls()
        for ts, sent, packet in records(path):
            capture.feed(ts, sent, packet)
        return capture.resolve()

    def feed(self, ts: int, sent: bool, packet: bytes) -> None:
        if not packet:
            return
        kind = packet[0]
        if kind == 0x04:
            self._event(ts, packet[1:])
        elif kind == 0x02:
            self._acl_packet(ts, sent, packet[1:])

    def _event(self, ts: int, body: bytes) -> None:
        if len(body) < 2:
            return
        code, params = body[0], body[2:]
        if code == 0x3E and params:
            sub = params[0]
            # Connection Complete, and its enhanced form: same fields where
            # this cares, different length after them.
            if sub in (0x01, 0x0A) and len(params) >= 12 and params[1] == 0:
                handle = struct.unpack("<H", params[2:4])[0]
                link = Link(handle, address(params[6:12]), ts)
                self.links.append(link)
                self.open[handle] = link
        elif code == 0x05 and len(params) >= 3:  # Disconnection Complete
            handle = struct.unpack("<H", params[1:3])[0]
            link = self.open.pop(handle, None)
            if link:
                link.closed_at = ts
        elif code in (0x08, 0x59) and len(params) >= 4:  # Encryption Change
            handle = struct.unpack("<H", params[1:3])[0]
            link = self.open.get(handle)
            if link and params[3]:
                link.encrypted = True

    def _acl_packet(self, ts: int, sent: bool, body: bytes) -> None:
        if len(body) < 4:
            return
        flags, length = struct.unpack("<HH", body[:4])
        handle = flags & 0x0FFF
        boundary = (flags >> 12) & 0x3
        if boundary != 0x1:  # start of a higher-layer message
            self._acl[handle] = bytearray(body[4:4 + length])
            self._acl_host[handle] = sent
        elif handle in self._acl:
            self._acl[handle] += body[4:4 + length]
        buffered = self._acl.get(handle)
        if buffered is None or len(buffered) < 4:
            return
        l2_length, cid = struct.unpack("<HH", buffered[:4])
        if len(buffered) - 4 < l2_length:
            return  # more fragments to come
        frame = bytes(buffered[4:4 + l2_length])
        del self._acl[handle]
        from_host = self._acl_host.get(handle, sent)
        if cid == ATT_CID:
            self._att(handle, from_host, frame)
        elif cid == SMP_CID and frame:
            link = self.open.get(handle)
            if link:
                link.paired = True

    def _att(self, handle: int, from_host: bool, frame: bytes) -> None:
        link = self.open.get(handle)
        if link is None or not frame:
            return
        op, body = frame[0], frame[1:]
        known = link.attributes

        if op == 0x05 and body:  # Find Information Response
            size = 4 if body[0] == 1 else 18
            rest = body[1:]
            for i in range(0, len(rest) - size + 1, size):
                known.setdefault(
                    struct.unpack("<H", rest[i:i + 2])[0], uuid(rest[i + 2:i + size])
                )
        elif op == 0x09 and body:  # Read By Type Response: characteristics
            size, rest = body[0], body[1:]
            for i in range(0, len(rest) - size + 1, size):
                entry = rest[i:i + size]
                if len(entry) >= 5:
                    known[struct.unpack("<H", entry[3:5])[0]] = uuid(entry[5:])
        elif op == 0x11 and body:  # Read By Group Response: services
            size, rest = body[0], body[1:]
            for i in range(0, len(rest) - size + 1, size):
                entry = rest[i:i + size]
                if len(entry) >= 4:
                    known.setdefault(
                        struct.unpack("<H", entry[0:2])[0], "svc " + uuid(entry[4:])
                    )
        elif op in CARRIES_VALUE and len(body) >= 2:
            link.values.append((struct.unpack("<H", body[:2])[0], body[2:]))

    # -- what it found --------------------------------------------------

    def resolve(self) -> "Capture":
        """Work out the pipes, once the whole file has been read.

        Two passes rather than one, because a phone caches a device's attribute
        database: the second and third connections to the same adapter repeat no
        discovery at all, and their handles are known only from the first. Done
        as the values arrive, the longest sessions in a file come out empty.
        """
        by_address: dict[str, dict] = {}
        for link in self.links:
            by_address.setdefault(link.peer, {}).update(link.attributes)
        for link in self.links:
            link.attributes = dict(by_address.get(link.peer, {}))
            link.streams = {}
            for attribute, value in link.values:
                found = link.attributes.get(attribute, "")[:8]
                if found in NUS_WRITE:
                    link.streams.setdefault("host", bytearray()).extend(value)
                elif found in NUS_NOTIFY:
                    link.streams.setdefault("adapter", bytearray()).extend(value)
        return self

    def uart_links(self) -> list[Link]:
        """The connections that spoke Nordic UART, in the order they opened."""
        return [link for link in self.links if link.streams]
