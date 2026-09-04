# Tools and hardware

What has been tried, what worked, and what to buy. **Tested** means somebody
here held the thing and read the result; **expected** means it should work by
the specification and nobody has proved it. Do not buy on the strength of an
expected row.

Tools reach this application by one of three routes, and which one a tool takes
is the single most useful thing to know about it before buying:

| Route | What it means | Which tools |
| --- | --- | --- |
| **Read directly** | The browser talks to the adapter over Bluetooth or a cable | Code readers |
| **Import a report** | The tool's own app produces a file; Diagnostics reads it | Scan tools |
| **Photograph the slip** | The tool prints on paper; the camera and OCR read it | Battery and electrical testers |

Direct reading happens in the **browser**, not on the server — the server runs
in a container with no USB and no Bluetooth, and the phone in the garage is the
device actually next to the car (SPEC §8.3c).

---

## Code readers

OBD-II adapters with no screen of their own, which the browser drives directly.
This is the only category where the hardware talks to this application.

| Adapter | Connects by | Result | Tested |
| --- | --- | --- | --- |
| **OBDLink MX+** | Bluetooth Classic (SPP) | ✅ **Works** — codes read end to end, from a phone and a desktop | 2026-09-03, engine running |
| **GEARWRENCH GWSCAN** | Bluetooth LE | ❌ **Cannot be read** — its own protocol, and its app exports nothing | 2026-09-03, ignition on |
| Generic ELM327, wired | USB | ⚪ Expected | untested |
| Generic ELM327, Bluetooth Classic | Bluetooth Classic (SPP) | ⚪ Expected | untested |
| Generic ELM327, BLE | Bluetooth LE | ⚪ Expected | untested |

### If you are buying one

**Buy an ELM327-compatible adapter.** That is the whole rule. This application
speaks ELM327 and nothing else, so an adapter advertising ELM327 compatibility
will work and one that does not, will not.

The **OBDLink MX+** is the one proven here, and it is the safer kind for a
second reason: it uses the standard Serial Port Profile, so every browser that
supports Bluetooth serial at all offers it, with nothing to configure.

**Be wary of a code reader sold around its own app** — the giveaway is an
activation code printed on the case. Those speak their maker's protocol and
cannot be read here. They remain usable *if* their app exports a report, which
puts them in the scan-tool category below; the GWSCAN is listed as unusable
precisely because its app does not.

### The detail

**OBDLink MX+** — standard Serial Port Profile
(`00001101-0000-1000-8000-00805f9b34fb`). Pair it in the system Bluetooth
settings first, since the page can only offer an adapter the operating system
already knows about. Read end to end from Chrome on Android against a running
vehicle: modes 03, 07 and 0A, two ECUs answering.

**Generic ELM327 adapters** are expected to work because the page speaks plain
ELM327 and nothing else. The BLE side is configured for the service layouts
those adapters use in practice — OBDLink's `FFF0`, the common clone `FFE0`, and
Nordic UART — but nobody has tested one, and those UUIDs come from vendor
documentation rather than from anything observed here.

**GEARWRENCH GWSCAN** — a rebadged XTOOL, advertising over BLE as `AD20-…`. It
exposes Nordic UART and connects perfectly happily, then answers nothing,
because it speaks its maker's framing rather than ELM327. The protocol has since
been decoded far enough to show it is an ordinary CAN pass-through with no
authentication at all — the activation code is licence enforcement inside the
app, not a lock on the device — so support would be practical if wanted.
`Artifacts/samples/code-reader/GEARWRENCH GWSCAN/notes.md` has the framing, the
commands, and the one question still open.

---

## Scan tools

Tools with their own screen and software. None of them talk to this application
directly, and they do not need to: read the car with the tool, export or share
the report, and Diagnostics reads it. Everything lands as a **draft** for you to
check, exactly like a direct read.

| Tool | Report support | Tested |
| --- | --- | --- |
| **XTOOL D8** | ✅ Built in, no setup | 2026-09-03 (adapter probed), reports parsed |
| Autel MaxiSys | ✅ Profile in the catalog | from sample reports |
| BlueDriver | ✅ Profile in the catalog | from sample reports |
| Car Scanner (ELM OBD2 app) | ✅ Profile in the catalog | from sample reports |
| Carly | ✅ Profile in the catalog | from sample reports |
| Ross-Tech VCDS | ✅ Profile in the catalog | from sample reports |
| ThinkCar | ✅ Profile in the catalog | from sample reports |
| Topdon (full system report) | ✅ Profile in the catalog | from sample reports |
| Anything else | ⚪ Teach it once | — |

**A tool not on this list is not unsupported.** If no profile recognises the
file, you are offered a mapping screen, and saving the mapping means the next
report from that tool reads itself. That is usually a five-minute job rather
than a code change, so an unlisted scan tool is a weak reason not to buy one.

The **XTOOL D8's VCI dongle** is BLE with Nordic UART and silent to ELM327, the
same as the GWSCAN — so it cannot be read directly, and does not need to be.

---

## Battery and electrical testers

These print a paper slip and have no export at all, so the camera is the
interface: photograph the slip and OCR reads it.

| Tool | Result | Tested |
| --- | --- | --- |
| **TOPDON BT600 Plus** | ✅ **Works** — battery, cranking and charging tests read from a photograph | against real photographed slips |
| Other printing testers | ⚪ Teach it once | — |

A tester's slip carries several results at once — a cranking test and a charging
test taken forty seconds apart — and they are kept as separate results rather
than flattened into one reading.

Photographs are harder to read than PDFs, so results carry a **confidence** and
land as drafts. Check them, particularly the numbers.

---

## Other tools

Nothing recorded here yet. This is where a tool that fits none of the three
routes above would go — anything with no direct protocol, no exportable report,
and nothing printed to photograph.

If you try one, a row in this file is worth more than any amount of prose
elsewhere.

---

## What the browser has to provide

Only relevant to **code readers**; the other categories are files and
photographs, and work in any browser.

| | Chrome / Edge, desktop | Chrome, Android | Safari / Firefox |
| --- | --- | --- | --- |
| Wired adapters | yes | no | no |
| Bluetooth Classic adapters | yes | Chrome 137+ | no |
| Bluetooth LE adapters | yes | yes | no |

HTTPS is required everywhere: neither browser API runs on a plain-HTTP origin
that is not `localhost`.

**On Android, Chrome needs the "Nearby devices" permission.** Without it the
adapter chooser opens empty and nothing reports the refusal, which is
indistinguishable from an adapter that will not pair. It is the commonest cause
of "it cannot see my adapter" by a distance.

## Testing an adapter nobody has tried

Two hurdles, and they fail differently. Cheapest order:

1. **Can the browser reach it?** Pair it and look for a serial port. On Windows,
   `Get-PnpDevice -Class Ports` lists them, and the instance ID contains the
   Bluetooth service class ID — `{00001101-…}` is standard SPP and will be
   offered to the page. A BLE adapter appears in no such list at all; it is not
   paired, only connected, so use *Bluetooth LE adapter* on the read page and
   pick it from the browser's own chooser.

2. **Does it speak ELM327?** Open the port and send `ATI`. A version string back
   — an OBDLink MX+ answers `ELM327 v1.4b` — means it will work. Silence means a
   protocol of its own, and the scan-tool route is the answer instead.

```powershell
$p = New-Object System.IO.Ports.SerialPort 'COM5',38400,'None',8,'One'
$p.Open(); $p.Write("ATI`r"); Start-Sleep -Milliseconds 1500; $p.ReadExisting(); $p.Close()
```

An adapter that hides its serial service behind a maker's own identifier is
offered on a desktop, where the operating system maps it to a port regardless,
and withheld on a phone, which maps nothing. If it appears on a laptop and not
on a phone, that is why — the page prints the service class ID it connected to,
and adding that to `ELM327_BLUETOOTH_SERVICE_UUIDS` makes the phone list it.

## One thing worth knowing before trusting a reading

Connecting proves the transport and **nothing else**. A device can pair,
connect, accept every command and never answer — which is exactly what the
GWSCAN does. Empty replies decode to zero trouble codes, so an adapter that has
never spoken to the car can look like a clean bill of health. The read page
distinguishes the three cases, and the distinction is the point:

* **no codes found** — the car answered, and has nothing to report.
* **the adapter is working, but the car did not answer** — usually the ignition
  is off. The socket has power from the battery either way, so the adapter
  lights up and answers while the car stays silent.
* **the adapter connected, but it never answered** — not an ELM327.
