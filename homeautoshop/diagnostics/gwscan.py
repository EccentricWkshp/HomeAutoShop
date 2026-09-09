"""
The GWSCAN (XTOOL AD20) protocol, as far as two captures establish it.

The adapter advertises Nordic UART, which this application already connects to
— and then answers no ELM327 command at all, because that profile is a pipe
and what goes through it is the vendor's own protocol. The reasoning and the
evidence are in `Artifacts/samples/code-reader/GEARWRENCH GWSCAN/notes.md`;
this is the protocol itself, in the one place both the tests and the browser
reader take it from.

    marker  SEQ  LEN  KIND  PAYLOAD[LEN]  XOR(SEQ..PAYLOAD)

`marker` is `AA` outbound and `55` inbound, and it never occurs inside a frame:
the stream is byte-stuffed (`btsnoop.stuff`). `LEN` and the checksum are both
counted on the unstuffed bytes, which is the detail that makes a first
implementation look like a flaky link rather than a wrong reader.

**What is understood, and what is replay.** The two commands that matter are
plain: `09` transmits a CAN frame and `0a` is one received, so an OBD request
is `02 03 00 …` to `0x7DF` and the answer is `43 …` from `0x7E8` — ordinary
OBD-II inside a thin wrapper, which the existing byte decoder reads unchanged.
The `01 …` frames that configure the bus are **not** understood; they are
replayed in the order the vendor's app sent them, and `SETUP` says so frame by
frame. That is the honest state of it: enough to read a CAN vehicle, and a
guess on anything else.

Nothing here talks to a device. The transport is Web Bluetooth and therefore
the browser's; this module is the codec, the script, and the vocabulary — so
that the part which can be tested is tested, and the part that cannot is as
small as it can be made.
"""

from __future__ import annotations

from dataclasses import dataclass

from .btsnoop import ADAPTER, HOST, stuff, unstuff_upto

#: The service the adapter answers on. Every payload seen in either capture
#: carries it, and a frame of another class has never been observed.
CLASS = 0x60

#: `0x7DF` is the OBD-II functional request address and `0x7E8` the first ECU's
#: reply. Both captures use them and nothing else.
REQUEST_ID = 0x7DF
RESPONSE_ID = 0x7E8

#: Sub-commands, by what they ask for. Only the first two are used by the
#: reader — they are the hello that proves the protocol is working before any
#: question is put to the car, which is worth having when the alternative
#: symptom is silence.
FIRMWARE = bytes((0x02, 0x01, 0x81))
SERIAL = bytes((0x02, 0x01, 0x82))
VIN = bytes((0x11, 0x01, 0x02))

#: Transmit a CAN frame; receive one.
SEND_CAN = 0x09
GOT_CAN = 0x0A

#: The adapter's own acknowledgement of a configure frame.
ACK = bytes((0x0B, 0x60, 0x01, 0x01))


@dataclass(frozen=True)
class Step:
    """One setup frame, and how much is actually known about it."""

    payload: bytes
    what: str
    #: False where the frame is replayed from the capture without its meaning
    #: being understood. Kept as a field rather than a comment because it is
    #: the honest answer to "why did this stop working on my car".
    understood: bool = False


#: What the vendor's app sends between connecting and asking its first
#: question, in that order.
#:
#: Two frames from the capture are deliberately **not** here: `01 01 17 …` and
#: `01 01 19 …` carry four bytes that differ between sessions in the way a
#: clock does, and replaying one shop's timestamp at another is a guess with
#: nothing behind it. Codes were read in both captures before either was sent.
#:
#: The rest is replay. `07 e0` and `07 e8` in the last two are the request and
#: response addresses, and `30 00 00 00 00 00 00 00` in the third is the
#: canonical ISO-TP flow-control frame — which reads as the adapter being told
#: what to emit so it can run ISO-TP itself. That is inference from the bytes,
#: not observed behavior: neither capture has a car with enough stored codes to
#: need a second frame.
SETUP = (
    Step(
        bytes.fromhex(
            "010c0e001300020007a1201002110212000007e00000000f01000600"
            "000500020400c80300500700"
        ),
        "open the bus at 500 kbit/s",
    ),
    Step(bytes.fromhex("0104080b090807df02010000000000000bffff0a0320"), "arm a request"),
    Step(bytes.fromhex("01010701"), "unknown"),
    Step(bytes.fromhex("010314081530000000000000001301"), "hand over flow control"),
    Step(bytes.fromhex("01031002110212000007e800000000"), "name the reply address"),
)


def checksum(body: bytes) -> int:
    value = 0
    for byte in body:
        value ^= byte
    return value


def build(seq: int, payload: bytes, *, kind: int = CLASS, marker: int = HOST) -> bytes:
    """One frame, ready for the wire.

    `seq` wraps at a byte and is echoed in the reply, which is what lets an
    answer be matched to its question on a link where notifications arrive
    whenever the adapter feels like it.
    """
    if len(payload) > 0xFF:
        raise ValueError("a payload longer than one byte's length cannot be framed")
    body = bytes((seq & 0xFF, len(payload), kind)) + payload
    return bytes((marker,)) + stuff(body + bytes((checksum(body),)), marker)


@dataclass(frozen=True)
class Reply:
    seq: int
    kind: int
    payload: bytes
    checksum_ok: bool


def parse(stream: bytes, *, marker: int = ADAPTER):
    """Every whole frame in a stream, in order, with what is left over.

    A stream, not a packet: notifications arrive in twenty-byte pieces and a
    frame spans as many of them as it needs. Returns `(frames, consumed)`, and
    the caller keeps `stream[consumed:]` for the next notification.

    **A frame ends where its length says**, not where the next marker starts.
    Waiting for a marker is what a capture can afford — it has the whole file —
    and it would mean a live reader never sees an answer until the following
    one arrives, which on a device that answers one question at a time is a
    reader that never sees anything.

    The marker cannot occur inside a frame, so a byte that is not one is
    skipped rather than puzzled over: that is what lets a reader which joined
    mid-message find its footing instead of misreading the rest.
    """
    frames: list[Reply] = []
    consumed = 0
    i = 0
    while i < len(stream):
        if stream[i] != marker:
            i += 1
            consumed = i
            continue
        # Three bytes of header first, because the length is the second of
        # them and nothing can be decided without it.
        head, used = unstuff_upto(stream[i + 1:], marker, 3)
        if len(head) < 3:
            break  # the rest of the header has not arrived
        want = 3 + head[1] + 1
        body, used = unstuff_upto(stream[i + 1:], marker, want)
        if len(body) < want:
            break  # the rest of the frame has not arrived
        frames.append(
            Reply(body[0], body[2], body[3:-1], checksum(body[:-1]) == body[-1])
        )
        i = i + 1 + used
        consumed = i
    return frames, consumed


# ---------------------------------------------------------------------------
# What goes inside a frame
# ---------------------------------------------------------------------------


def can_request(data: bytes, *, can_id: int = REQUEST_ID) -> bytes:
    """A CAN frame to transmit, as the adapter wants it.

        09  0b  08  <id, big endian>  <8 data bytes>

    The eight bytes are padded rather than trimmed, which is what the capture
    does and what CAN expects: a frame is eight bytes whether or not the
    request fills them.
    """
    padded = (bytes(data) + bytes(8))[:8]
    return bytes((SEND_CAN, 0x0B, 0x08, (can_id >> 8) & 0xFF, can_id & 0xFF)) + padded


def service(mode: int, pid: int | None = None) -> bytes:
    """An OBD-II request, single-frame.

    The leading byte is the ISO-TP length — one byte for a bare mode, two with
    a PID — and everything after it is the request. This is the same shape an
    ELM327 puts on the bus when it is handed `03`; the difference is that here
    the shape is ours to write.
    """
    body = bytes((mode,)) if pid is None else bytes((mode, pid))
    return bytes((len(body),)) + body


def can_reply(payload: bytes):
    """`(can_id, data)` for a received-CAN payload, or `None` for anything else."""
    if len(payload) < 13 or payload[0] != GOT_CAN:
        return None
    return (payload[3] << 8) | payload[4], payload[5:13]


def obd_bytes(data: bytes) -> bytes:
    """The OBD-II response inside a single-frame CAN reply.

    The first byte is the ISO-TP length and the rest is padding, so a `43 00`
    answer arrives as `02 43 00 00 00 00 00 00` and only the first three bytes
    of it mean anything. Trimming here rather than in the reader means the
    decoder downstream sees exactly what an ELM327 would have printed.
    """
    if not data:
        return b""
    frame_type = data[0] >> 4
    if frame_type != 0:
        # A first or consecutive frame: a multi-frame reply, which nothing has
        # been seen to send and this does not pretend to reassemble.
        return b""
    length = data[0] & 0x0F
    return bytes(data[1:1 + length])


def as_elm_text(data: bytes) -> str:
    """A CAN reply as the hex an ELM327 would have printed.

    So that everything downstream — the mode decoder, the DTC lettering, the
    duplicate handling — is the code that already exists and is already tested,
    rather than a second implementation reached by a different transport.
    """
    return " ".join(f"{byte:02X}" for byte in obd_bytes(data))


def negative(data: bytes):
    """`(mode, reason)` when a reply is a refusal, else `None`.

    `7F 0A 11` — service not supported — is not "no permanent codes", and a
    reader that shows the two the same way tells somebody their car is clean
    when nobody asked it anything. Seen on a Ford in the second capture.
    """
    body = obd_bytes(data)
    if len(body) >= 3 and body[0] == 0x7F:
        return body[1], body[2]
    return None


def script() -> list[dict]:
    """The setup, as the page will be handed it.

    Hex rather than bytes because it crosses into JSON, and a list rather than
    a constant because the browser should be told what to send instead of
    holding a second copy of it. The frame that turns out to be unnecessary, or
    wrong on some other vehicle, is then one edit here — in the module that has
    tests — rather than one in a script that has none.
    """
    return [{"payload": step.payload.hex(), "what": step.what} for step in SETUP]
