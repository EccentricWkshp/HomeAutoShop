# GEARWRENCH GWSCAN — a rebadged XTOOL, and not readable

Probed 2026-09-03 with the adapter plugged into a vehicle, ignition on, from a
Windows machine over Bluetooth LE. Read-only throughout: mode 04 was never sent.

**It cannot be read by the browser code reader**, and the reason is the
protocol rather than the transport.

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
in twenty minutes what guessing could not. **There is no authentication.** The
session enables notifications and goes straight to reading the serial number —
no challenge, no key exchange — so the activation code is licence enforcement
inside the app, not a gate on the device.

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

The whole capture accounts for 11 CAN frames each way, **every one of them a
single frame**, and the services the app asked for were mode 01 PID 00, then
modes 03, 07 and 0A — the same set this application reads. There is nothing
exotic being done that we are not already doing.

**What the capture cannot answer**, because the car is healthy and speaks one
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
configuration, not observed behaviour**, and it is the thing a capture from a
car with stored codes would confirm in a minute.

Worth getting, because it is the case that matters: a reader that works only on
healthy cars fails exactly when somebody needs it.

*The capture itself is ignored by git (`Artifacts/samples/**/*.cfa`) and must
stay that way — it carries the vehicle's VIN and the adapter's serial in
cleartext.* Recovering that means
capturing the vendor's app talking to the adapter and decoding what it sends,
which is days of work, for one model, unverifiable in CI, and undone by a
firmware update. The report-import path below costs minutes and does not care
about any of it.

That mattered, because an empty reply decodes to zero trouble codes, and the
page used to report zero codes as *"Read complete — no codes found."* A tool
that had never spoken to the car produced a clean bill of health. Silence is
now reported as silence, and the same fix covers clearing — which had been
claiming that codes were cleared on no evidence whatsoever.

## What to do with one instead

Read it with the GWSCAN app and bring the result back: Diagnostics takes the
report it exports and learns the layout the first time. Worth trying the
**XTOOL D8 profile first** — the same vendor builds both, so the export may
already parse, or need only small changes.
