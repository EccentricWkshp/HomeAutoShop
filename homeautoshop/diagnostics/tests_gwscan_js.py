"""
The browser half of the GWSCAN reader, checked against the Python half.

`static/gwscan.js` and `homeautoshop/diagnostics/gwscan.py` implement one
protocol twice, and they have to: the transport is Web Bluetooth, so the codec
that talks to the adapter can only live in the browser — while the tests, the
setup script and the reasoning belong somewhere they can be exercised. Two
implementations of one byte format is a real risk, and it is the specific risk
that shows up as an intermittent fault on somebody else's car rather than as a
failure here.

So this runs the JavaScript, on the same vectors, and asserts the two agree
byte for byte. It needs `node` and **skips without it**: CI runs the suite
inside the application image, which is a Python image and has no business
gaining a JavaScript runtime to check one file. Locally it runs, which is
where the two are edited.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase

from homeautoshop.diagnostics import gwscan
from homeautoshop.diagnostics.tests_gwscan import (
    ASK_FIRMWARE,
    ASK_SUPPORTED,
    FIRMWARE_REPLY,
    NOT_SUPPORTED,
    NO_CODES,
    SUPPORTED_REPLY,
)

#: Handed to node as one argument, so nothing is quoted through a shell.
DRIVER = """
global.window = { setTimeout: setTimeout };
require(%(script)s);
var g = global.window.homeautoshop.gwscan;

function hex(bytes) {
  return Array.prototype.map.call(bytes, function (b) {
    return ("0" + b.toString(16)).slice(-2);
  }).join("");
}

var vectors = JSON.parse(%(vectors)s);
var out = {};

out.askSupported = hex(g.build(0x08, g.canRequest(g.service(0x01, 0x00))));
out.askFirmware = hex(g.build(0x03, new Uint8Array([0x02, 0x01, 0x81])));

var firmware = g.parse(g.hexToBytes(vectors.firmwareReply));
out.firmware = {
  count: firmware.frames.length,
  consumed: firmware.consumed,
  seq: firmware.frames[0].seq,
  payload: hex(firmware.frames[0].payload),
  ok: firmware.frames[0].checksumOk
};

var supported = g.parse(g.hexToBytes(vectors.supportedReply));
var received = g.canReply(supported.frames[0].payload);
out.supported = {
  id: received.id,
  data: hex(received.data),
  text: g.asElmText(received.data)
};

out.noCodes = g.asElmText(g.hexToBytes(vectors.noCodes));
var refused = g.negative(g.hexToBytes(vectors.notSupported));
out.refused = [refused.mode, refused.reason];
out.multiFrame = g.asElmText(g.hexToBytes(vectors.multiFrame));

// Half a frame decodes to nothing and consumes nothing, so the reader can
// simply keep the bytes and try again when the rest arrives.
var half = g.hexToBytes(vectors.supportedReply.slice(0, 22));
var partial = g.parse(half);
out.partial = { count: partial.frames.length, consumed: partial.consumed };

// Every byte value, through both halves of the escaping.
var all = new Uint8Array(256);
for (var i = 0; i < 256; i++) { all[i] = i; }
out.roundTrip = {};
[0xAA, 0x55].forEach(function (marker) {
  var back = g.unstuffUpto(g.stuff(all, marker), marker);
  out.roundTrip[marker] = hex(back.bytes);
});

process.stdout.write(JSON.stringify(out));
"""


class TheTwoReadersAgreeTests(SimpleTestCase):
    maxDiff = None

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.node = shutil.which("node")

    def setUp(self):
        if not self.node:
            self.skipTest("node is not installed; the JavaScript reader is unchecked")

    def run_driver(self) -> dict:
        script = Path(settings.BASE_DIR) / "static" / "gwscan.js"
        source = DRIVER % {
            "script": json.dumps(str(script).replace("\\", "/")),
            "vectors": json.dumps(
                json.dumps(
                    {
                        "firmwareReply": FIRMWARE_REPLY.hex(),
                        "supportedReply": SUPPORTED_REPLY.hex(),
                        "noCodes": NO_CODES.hex(),
                        "notSupported": NOT_SUPPORTED.hex(),
                        "multiFrame": "1014430133010200",
                    }
                )
            ),
        }
        finished = subprocess.run(
            [self.node, "-e", source], capture_output=True, text=True, timeout=60
        )
        self.assertEqual(finished.returncode, 0, finished.stderr)
        return json.loads(finished.stdout)

    def test_a_request_is_built_to_the_same_bytes(self):
        out = self.run_driver()

        self.assertEqual(out["askSupported"], ASK_SUPPORTED.hex())
        self.assertEqual(out["askFirmware"], ASK_FIRMWARE.hex())

    def test_a_stuffed_reply_is_read_the_same_way(self):
        """The frame that settled the framing, decoded twice."""
        out = self.run_driver()
        frames, consumed = gwscan.parse(FIRMWARE_REPLY)

        self.assertEqual(out["firmware"]["count"], 1)
        self.assertEqual(out["firmware"]["consumed"], consumed)
        self.assertEqual(out["firmware"]["seq"], frames[0].seq)
        self.assertEqual(out["firmware"]["payload"], frames[0].payload.hex())
        self.assertTrue(out["firmware"]["ok"])

    def test_a_can_reply_comes_out_as_the_same_hex(self):
        out = self.run_driver()
        frames, _consumed = gwscan.parse(SUPPORTED_REPLY)
        can_id, data = gwscan.can_reply(frames[0].payload)

        self.assertEqual(out["supported"]["id"], can_id)
        self.assertEqual(out["supported"]["data"], data.hex())
        self.assertEqual(out["supported"]["text"], gwscan.as_elm_text(data))

    def test_the_answers_that_are_not_codes_agree_too(self):
        out = self.run_driver()

        self.assertEqual(out["noCodes"], gwscan.as_elm_text(NO_CODES))
        self.assertEqual(list(out["refused"]), list(gwscan.negative(NOT_SUPPORTED)))
        self.assertEqual(
            out["multiFrame"], gwscan.as_elm_text(bytes.fromhex("1014430133010200"))
        )

    def test_half_a_frame_is_kept_rather_than_read(self):
        out = self.run_driver()
        frames, consumed = gwscan.parse(SUPPORTED_REPLY[:11])

        self.assertEqual(out["partial"]["count"], len(frames))
        self.assertEqual(out["partial"]["consumed"], consumed)

    def test_the_escaping_round_trips_over_every_byte(self):
        out = self.run_driver()
        every = bytes(range(256))

        for marker in (gwscan.HOST, gwscan.ADAPTER):
            with self.subTest(marker=marker):
                self.assertEqual(out["roundTrip"][str(marker)], every.hex())
