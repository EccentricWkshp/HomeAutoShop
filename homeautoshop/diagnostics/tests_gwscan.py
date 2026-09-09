"""
The GWSCAN protocol (`homeautoshop/diagnostics/gwscan.py`).

The vectors below are real: bytes lifted out of the captures in
`Artifacts/samples/code-reader/GEARWRENCH GWSCAN/`, which are themselves
gitignored because they carry a VIN and the adapter's serial in clear text.
Only frames that identify nothing are copied here — a firmware version, a
support bitmask, an empty list of codes, a refusal — and the frames that would
carry an identifier are built synthetically instead.

That distinction is the point of the file. A protocol reader is written from
evidence, and a test with invented bytes in it proves the reader agrees with
its author rather than with the device.
"""

from __future__ import annotations

from django.test import SimpleTestCase

from homeautoshop.diagnostics import gwscan

#: Host to adapter: read the firmware version, and ask the car what it
#: supports. Both taken from the 2026-09-05 capture.
ASK_FIRMWARE = bytes.fromhex("aa030360020181e2")
ASK_SUPPORTED = bytes.fromhex("aa080d60090b0807df0201000000000000b4")

#: Adapter to host. The firmware reply is the frame that settled the framing:
#: the `56 01` in the middle of it is an escaped `V`, and unstuffed the frame
#: is exactly its stated thirteen bytes and checksums to the byte.
FIRMWARE_REPLY = bytes.fromhex("55030d60020181560131303300000000000088")
SUPPORTED_REPLY = bytes.fromhex("55080d600a0b0807e8064100b63fa81300f6")

#: `43 00` — no stored codes — and `7F 0A 11`, service not supported, off a
#: Ford that has no permanent-code support at all.
NO_CODES = bytes.fromhex("0243000000000000")
NOT_SUPPORTED = bytes.fromhex("037f0a1100000000")


class FramingTests(SimpleTestCase):
    def test_a_request_matches_the_bytes_the_vendor_app_sent(self):
        """Built from parts, and equal to the capture byte for byte."""
        payload = gwscan.can_request(gwscan.service(0x01, 0x00))

        self.assertEqual(gwscan.build(0x08, payload), ASK_SUPPORTED)

    def test_and_so_does_a_bare_sub_command(self):
        self.assertEqual(gwscan.build(0x03, gwscan.FIRMWARE), ASK_FIRMWARE)

    def test_a_reply_carrying_the_escape_byte_still_adds_up(self):
        """The case a first reading of this protocol gets wrong: unstuffed the
        payload is thirteen bytes, read raw it is fourteen and fails."""
        frames, consumed = gwscan.parse(FIRMWARE_REPLY)

        self.assertEqual(len(frames), 1)
        self.assertTrue(frames[0].checksum_ok)
        self.assertEqual(len(frames[0].payload), 13)
        self.assertEqual(frames[0].payload[3:7], b"V103")

    def test_the_sequence_number_comes_back_with_the_answer(self):
        """Which is what lets a reply be matched to its question on a link
        where notifications arrive whenever the adapter feels like it."""
        frames, _consumed = gwscan.parse(SUPPORTED_REPLY)

        self.assertEqual(frames[0].seq, 0x08)

    def test_a_frame_split_across_notifications_waits_for_the_rest(self):
        """Twenty bytes at a time is what the link delivers; a frame is
        whatever length it is."""
        first, second = SUPPORTED_REPLY[:11], SUPPORTED_REPLY[11:]

        frames, consumed = gwscan.parse(first)
        self.assertEqual(frames, [])
        self.assertEqual(consumed, 0)

        frames, consumed = gwscan.parse(first + second)
        self.assertEqual(len(frames), 1)
        self.assertEqual(consumed, len(SUPPORTED_REPLY))

    def test_a_frame_is_complete_on_its_own(self):
        """It ends where its length says, not where the next marker starts —
        so an answer is readable the moment it arrives rather than when the
        following one does."""
        frames, consumed = gwscan.parse(SUPPORTED_REPLY)

        self.assertEqual(len(frames), 1)
        self.assertEqual(consumed, len(SUPPORTED_REPLY))

    def test_two_frames_in_one_notification_are_both_read(self):
        stream = FIRMWARE_REPLY + SUPPORTED_REPLY

        frames, _consumed = gwscan.parse(stream)

        self.assertEqual([f.seq for f in frames], [0x03, 0x08])

    def test_a_corrupted_frame_is_reported_rather_than_dropped(self):
        broken = bytearray(SUPPORTED_REPLY)
        broken[-1] ^= 0xFF

        frames, _consumed = gwscan.parse(bytes(broken))

        self.assertEqual(len(frames), 1)
        self.assertFalse(frames[0].checksum_ok)

    def test_a_payload_too_long_to_frame_is_refused(self):
        with self.assertRaises(ValueError):
            gwscan.build(1, bytes(300))


class WhatIsInsideAFrameTests(SimpleTestCase):
    def test_a_received_frame_names_the_ecu_that_sent_it(self):
        frames, _consumed = gwscan.parse(SUPPORTED_REPLY)
        can_id, data = gwscan.can_reply(frames[0].payload)

        self.assertEqual(can_id, gwscan.RESPONSE_ID)
        self.assertEqual(data.hex(), "064100b63fa81300")

    def test_anything_that_is_not_a_can_frame_says_so(self):
        frames, _consumed = gwscan.parse(FIRMWARE_REPLY)

        self.assertIsNone(gwscan.can_reply(frames[0].payload))

    def test_the_padding_is_dropped_by_the_length_the_frame_states(self):
        """A CAN frame is eight bytes whether or not the answer fills them."""
        self.assertEqual(gwscan.obd_bytes(NO_CODES), bytes.fromhex("4300"))

    def test_and_the_result_reads_as_an_elm327_would_have_printed_it(self):
        """So the decoder downstream is the one that already exists."""
        self.assertEqual(gwscan.as_elm_text(NO_CODES), "43 00")

    def test_a_refusal_is_not_an_empty_list(self):
        """`7F 0A 11` is *service not supported*, and showing it as no codes
        tells somebody their car is clean when nobody asked it anything."""
        self.assertEqual(gwscan.negative(NOT_SUPPORTED), (0x0A, 0x11))
        self.assertIsNone(gwscan.negative(NO_CODES))

    def test_a_multi_frame_reply_is_declined_rather_than_half_read(self):
        """No capture has a car with enough stored codes to send one, so
        nothing here can claim to reassemble it. Returning the first frame's
        bytes would be a list of codes that is quietly short."""
        first_frame = bytes.fromhex("1014430133010200")

        self.assertEqual(gwscan.obd_bytes(first_frame), b"")
        self.assertEqual(gwscan.as_elm_text(first_frame), "")

    def test_a_request_is_padded_to_a_whole_can_frame(self):
        payload = gwscan.can_request(gwscan.service(0x03))

        self.assertEqual(payload.hex(), "090b0807df0103000000000000")

    def test_a_mode_with_a_pid_states_the_longer_length(self):
        self.assertEqual(gwscan.service(0x01, 0x0C).hex(), "02010c")
        self.assertEqual(gwscan.service(0x03).hex(), "0103")


class TheSetupScriptTests(SimpleTestCase):
    """What the reader replays, and what it deliberately does not."""

    def test_every_step_says_whether_it_is_understood(self):
        for step in gwscan.SETUP:
            with self.subTest(step=step.what):
                self.assertTrue(step.what)
                self.assertIsInstance(step.understood, bool)

    def test_it_is_honest_that_the_configuration_is_replay(self):
        """None of the `01 …` frames is understood, and a comment claiming
        otherwise would be the kind of thing somebody later relies on."""
        self.assertFalse(any(step.understood for step in gwscan.SETUP))

    def test_the_frames_that_look_like_a_clock_are_left_out(self):
        """`01 01 17 …` and `01 01 19 …` carry four bytes that differ between
        sessions the way a timestamp does. Codes were read in both captures
        before either was sent, so replaying one shop's is a guess with nothing
        behind it."""
        replayed = b"".join(step.payload for step in gwscan.SETUP)

        self.assertNotIn(bytes.fromhex("010117"), replayed)
        self.assertNotIn(bytes.fromhex("010119"), replayed)

    def test_the_flow_control_frame_is_still_handed_over(self):
        """`30 00 00 00 00 00 00 00` is the canonical ISO-TP ContinueToSend,
        and the straightforward reading is that the adapter is being told what
        to emit so it can run ISO-TP itself. Untested against a car with codes,
        and the reason to keep sending it."""
        replayed = b"".join(step.payload for step in gwscan.SETUP)

        self.assertIn(bytes.fromhex("3000000000000000"), replayed)

    def test_the_script_crosses_to_the_page_as_hex(self):
        script = gwscan.script()

        self.assertEqual(len(script), len(gwscan.SETUP))
        self.assertEqual(
            bytes.fromhex(script[0]["payload"]), gwscan.SETUP[0].payload
        )

    def test_a_frame_built_from_the_script_round_trips(self):
        """The bytes the page will put on the wire, read back by the parser
        that reads the adapter's — one framing, exercised from both ends."""
        for n, step in enumerate(gwscan.SETUP):
            with self.subTest(step=step.what):
                wire = gwscan.build(n, step.payload)
                frames, _consumed = gwscan.parse(wire, marker=gwscan.HOST)

                self.assertEqual(len(frames), 1)
                self.assertTrue(frames[0].checksum_ok)
                self.assertEqual(frames[0].payload, step.payload)


class SomethingWithAnIdentifierInItTests(SimpleTestCase):
    """Built rather than copied, because the real one names the adapter."""

    def test_a_serial_reply_is_read_back_as_text(self):
        made_up = b"GWSCAN-TESTUNIT0001"
        wire = gwscan.build(
            0x02, gwscan.SERIAL + made_up + b"\x00\x00", marker=gwscan.ADAPTER
        )

        frames, _consumed = gwscan.parse(wire)

        self.assertTrue(frames[0].checksum_ok)
        self.assertEqual(frames[0].payload[3:].rstrip(b"\x00"), made_up)
