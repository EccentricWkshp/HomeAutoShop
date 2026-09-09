# GEARWRENCH GWSCAN — a rebadged XTOOL, readable but not read

Probed 2026-09-03 with the adapter plugged into a vehicle, ignition on, from a
Windows machine over Bluetooth LE. Read-only throughout: mode 04 was never sent.

**It could not be read by the browser code reader**, and the reason was the
protocol rather than the transport. Two captures of its own app have since been
decoded and the protocol is implemented, behind `GWSCAN_READER` and off by
default — see *What to do with one* at the end for what that default is
protecting against.

## It is Bluetooth LE, and it does not pair

It never appears in Windows' Bluetooth device list, because BLE peripherals of
this kind are not paired at all — an app connects straight to GATT. That also
means the usual advice ("pair it first") is wrong here and sends people looking
for a fault that is not there.

The vendor's documentation does say the device must be "paired", which is what
sent this investigation looking for a Bluetooth Classic device that does not
exist. **It means the app's activation step** — entering the serial number and
the activation code printed on the adapter — not Bluetooth pairing. Worth
knowing before reading any vendor's wording as a protocol claim.

A BLE scan finds it without any pairing:

```text
5B:49:BF:F2:29:88  rssi=-75  AD20-12872
```

**`AD20` is XTOOL's own dongle designation.** The device announces itself as an
XTOOL product despite the GEARWRENCH branding, which is worth writing down: the
vendor documentation says only "Bluetooth", and published guesses put these
house-brand tools on Launch's platform. Both are wrong for this one.

The advertised name is that prefix plus the last five digits of the serial
printed on the case, which is how this was tied to the adapter in hand rather
than to something else in range. Two adapters of this make in one workshop
would be distinguishable the same way.

*The photographs in this folder are ignored by git (`Artifacts/samples/**/*.jpg`)
and should stay that way — the case label carries the activation code beside the
serial, and the QR code appears to encode both.*

## The transport is one we already support

```text
service 6e400001-b5a3-f393-e0a9-e50e24dcca9e  (Nordic UART Service)
    char 6e400003-...  [notify]                        Nordic UART TX
    char 6e400002-...  [write,write-without-response]  Nordic UART RX
    char 6e400004-...  [write,write-without-response,notify]
```

That is exactly the third entry in `ELM327_BLE_PROFILES`, so the page connects
to it, discovers the characteristics, and writes to them successfully.

## And then it says nothing

```text
ATZ  → (nothing)
ATI  → (nothing)
ATE0 → (nothing)
0100 → (nothing)
03   → (nothing)
```

Not an error, not an echo — silence, to every command, including the reset that
every ELM327 answers. It uses Nordic UART purely as a pipe for XTOOL's own
protocol.

**The lesson worth keeping: a matching GATT profile does not mean a compatible
adapter.** Connecting proves the transport and nothing else, and this device
gets all the way to a working connection before failing.

A further probe listened for ten seconds with no writes — the device announces
nothing on connect — and then tried both write characteristics, including the
non-standard `6e400004`, with `\r` and `\r\n` line endings. Silence throughout.

The reason turned out to be simple, and not the one assumed here at first: it
is waiting for its own framing, and ASCII is not it.

## The protocol, from a capture

An Android `btsnoop_hci` log of the vendor's app talking to the adapter settled
in twenty minutes what guessing could not.

**No authentication appears in the session.** It enables notifications and goes
straight to reading the serial number — no challenge, no key exchange, no
session key — and every later command is answered on its merits. So a
third-party client should be able to drive *this* adapter.

That is not the same as "the adapter has no lock", and the difference is worth
keeping straight. The activation screen was photographed at 20:03 and the
capture began at 20:15, so whatever activation does had already been done before
recording started. A device-side binding that happens once, at first activation,
would be invisible here. What can be said is that an **activated** adapter
answers a plain client without further ceremony; whether a factory-fresh one
does is untested.

Writes go to the Nordic UART RX characteristic and replies arrive as
notifications on TX, split across 20-byte BLE writes and reassembled into one
stream per direction:

```text
AA (host -> adapter) / 55 (adapter -> host)
SEQ      one byte, echoed in the reply
LEN      length of PAYLOAD
0x60     class
PAYLOAD  LEN bytes
XOR      over SEQ..PAYLOAD inclusive
```

**And the stream is byte-stuffed**, which the first reading of this missed. The
marker never occurs inside a frame: the escape is the marker plus one — `AB`
going out, `56` coming back — and the byte after it says which value was meant.

```text
ESC 02  ->  the marker  (AA / 55)
ESC 01  ->  ESC itself  (AB / 56)
```

`LEN` and the checksum are both counted on the **unstuffed** bytes, so a reader
that does not unescape sees the wrong length and a checksum that fails. That is
what the first pass recorded as occasional bad frames and set aside as noise; it
was not noise. The firmware reply is the clearest case — `56 01` in the middle of
it is one escaped `V`, and unescaped the frame is exactly its stated 13 bytes and
checksums to the byte:

```text
on the wire   55 03 0d 60 02 01 81 56 01 31 30 33 00 00 00 00 00 00 88
unstuffed        03 0d 60 02 01 81 56    31 30 33 00 00 00 00 00      = "V103"
```

Across the two captures this rule takes the decode from about three frames in
four to **all of them**: 21 of 21, 305 of 305 and 304 of 304 on the three
sessions that use this framing alone, with no frame left over in either
direction. It is worth stating plainly because it is the one detail that would
have made a from-scratch implementation fail in a way that looks like a
transport bug.

Payloads seen: `02 01 82` reads the serial number, `02 01 81` the firmware
version, `11 01 02` the VIN, and `01 …` frames configure the bus. The two that
matter carry raw CAN:

```text
--> 09 0b 08 07df 02 01 00 00 00 00 00 00     transmit on CAN 0x7DF
<-- 0a 0b 08 07e8 06 41 00 b6 3f a8 13 00     received on CAN 0x7E8
```

That is `09`/`0a`, a length, a big-endian 11-bit CAN ID, and eight data bytes —
**ordinary OBD-II inside a thin wrapper.** The reply above is byte for byte
what the OBDLink MX+ read from the same vehicle, and later frames carry
`02 43 00` and `02 4a 00` from `0x7E8`: modes 03 and 0A, no codes stored.

So reading this adapter needs no ELM327 at all. Send `02 03 00 …` to `0x7DF`,
take `43 <count> …` off `0x7E8`, and the existing byte decoder handles the rest
— it already drops the count byte by payload parity, which is the same rule on
the wire as it is behind an ELM327.

That first capture accounts for 11 CAN frames each way, **every one of them a
single frame**, and the services the app asked for were mode 01 PID 00, then
modes 03, 07 and 0A — the same set this application reads. The second capture
below is much longer and adds only more of the same: live data, one freeze-frame
read, and the same three code services. There is nothing exotic being done that
we are not already doing.

**What the captures cannot answer**, because both cars are healthy and speak one
protocol:

* **Multi-frame replies.** A car with several stored codes answers mode 03
  across more than one CAN frame, and somebody has to send ISO-TP flow control.
  No multi-frame reply occurs here, and the phone sends no flow control — but
  there is no occasion for either.
* **Other buses.** The `01 …` frames configure the adapter for 11-bit CAN. What
  they mean is not known, only what they were on this car, so replaying them is
  a guess on any vehicle that is not ISO 15765-4 11-bit.

On the first, the configuration itself is suggestive:

```text
seq=0c  01 03 14 08 15 30 00 00 00 00 00 00 00 13 01
seq=0d  01 03 10 02 11 02 12 00 00 07 e8 00 00 00 00
```

`30 00 00 00 00 00 00 00` is the canonical ISO-TP flow-control frame —
ContinueToSend, block size 0, no separation time — and it is being handed to
the adapter during setup, next to a frame naming the ECU's response ID
`0x7E8`. The straightforward reading is that the adapter is told what flow
control to emit so it can run ISO-TP itself. **That is inference from the
configuration, not observed behavior**, and it is the thing a capture from a
car with stored codes would confirm in a minute.

Worth getting, because it is the case that matters: a reader that works only on
healthy cars fails exactly when somebody needs it.


## A second capture, and what it settled

Taken 2026-09-05 on a different vehicle, with the app started cold. Android's
snoop buffer is a ring, so the file also still holds both sessions from the
2026-09-03 capture — three sessions in one file, and only the last is new.

**There is no authentication over Bluetooth, and now that is a measurement
rather than an inference.** The earlier note could only say that the recording
began after the activation screen had been used, so a one-time binding would
have been invisible. This one records the connection itself, and the whole file
contains:

* **no SMP traffic at all** — L2CAP channel 6 is never opened, in either
  direction, on any of the three connections;
* **no bonding and no encryption** on the adapter's link. The one
  `LE Start Encryption` in the file is on another connection entirely, to an
  audio device that happened to be in the room.

The new session is a cold connect: full GATT discovery, one write to the
notification descriptor, and then straight into reading the serial number. No
challenge, no key, no handshake of any kind. Whatever the app's activation step
does, **it does not happen on this wire.**

**Five code scans, and one of them answers a question the first capture could
not.** The session runs 215 mode 01 reads across some forty-five PIDs, one mode
02 freeze-frame read, five mode 03 scans, four mode 07 and three mode 0A. The
mode 0A requests come back:

```text
--> 09 0b 08 07df 02 0a 00 ...
<-- 0a 0b 08 07e8 03 7f 0a 11 ...
```

`7F 0A 11` is a **negative response** — service not supported. This vehicle has
no permanent-code support, and an implementation has to tell that apart from
`4A 00`, which is the same screen's *no permanent codes stored*. One means
nothing to report and the other means nothing was asked. Reading a `7F` as an
empty list is the same failure this adapter already caused once, in the other
direction (see below).

**Still no multi-frame reply and still no flow control**, on either vehicle.
Both cars are healthy, every mode 03 answers `43 00`, and there has been no
occasion for ISO-TP. That remains the one thing worth capturing next, and it
needs a car with stored codes rather than another session on a well one.

**One session uses a second framing that is not decoded.** The middle session of
2026-09-03 switches, part way through, to a channel with its own two-byte magic
— `55 AA` going out and `AA 55` coming back — carrying

```text
MAGIC(2)  SEQ(2, big endian)  LEN(2)  CMD(2)  BODY[LEN]  CHECK(1)
```

with commands in an `FF Ex`/`FF Fx` range and bodies that wrap something of the
same `0x60` class. One request and reply pair (`FF EC` / `FF F1`) accounts for
about a hundred exchanges. The frame boundaries are certain — sequence numbers
run without a gap — but the checksum rule is not, and none of it was needed for
anything the other two sessions do. Noted so that a client meeting it knows it
is a second channel and not a corrupted stream.

*Both captures are ignored by git (`Artifacts/samples/**/*.cfa`) and must stay
that way — between them they carry two vehicles' VINs and the adapter's serial
in cleartext.*

## What probing it found

An adapter that could not be read still paid for itself. Every command was
answered with silence, and an empty reply decodes to zero trouble codes — so the
page reported *"Read complete — no codes found."* A device that had never spoken
to the car produced a clean bill of health. Silence is now reported as silence,
and the same fix covers clearing, which had been claiming codes were cleared on
no evidence at all.

## What to do with one

**There is no report to import.** The usual answer for a tool with its own
protocol — read the car with the maker's app, export the report, bring it here —
does not apply: no way to save or share a report was found in the GWSCAN app.
So there are two options rather than three.

* **Use a different adapter.** Any ELM327-compatible one works today, and the
  OBDLink MX+ is the one proven against this application.
* **Use this protocol — it is implemented, and switched off.**
  `homeautoshop/diagnostics/gwscan.py` holds the codec, the setup script and
  the tests; `static/gwscan.js` is the same codec where the transport lives,
  checked against the Python one on these captures' own bytes wherever `node`
  is available. `GWSCAN_READER=1` turns it on, and the reader is reached only
  after an ELM327 handshake meets silence — so a real ELM327 never sees it.

  It is off because of what the captures do not contain. **Multi-frame
  replies**: both cars are healthy, every mode 03 answered `43 00`, and a
  vehicle with several stored codes answers across more than one frame. The
  reader declines those and says so rather than reading a short list — which is
  the right behavior and is itself unproven, because nothing has sent it one.
  **Non-CAN buses**: the `01 …` setup frames are replayed, not understood, so
  anything that is not ISO 15765-4 11-bit is a guess. And whether a
  factory-fresh adapter behaves like this activated one is still untested,
  though the second capture removes the reason to expect otherwise: an app
  starting cold performs no authentication over Bluetooth at all.

  The capture worth taking next is the same one it has always been: this
  adapter, on a car with a light on.
