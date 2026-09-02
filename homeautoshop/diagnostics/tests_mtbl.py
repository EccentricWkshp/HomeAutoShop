"""
Reading MTBL, the table format the online manual libraries publish as.

The corpus this was written for is a 31 GB file, which is not a fixture. So
these tests build small ones: :func:`write` is a minimal MTBL writer, and every
test here is a round trip through it. That is a stronger test than a captured
sample would be — it exercises the parts that are easy to get subtly wrong
(shared-prefix keys, blocks that end mid-run, the index naming a block by its
last key) at sizes where a wrong answer is visible.

The reader was derived from the file rather than from a specification, so what
is checked here is mostly the things that would have made it *look* right on
one file and fail on the next.
"""

from __future__ import annotations

import struct

from django.test import SimpleTestCase

from . import mtbl


def leb128(value: int) -> bytes:
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        out.append(byte | (0x80 if value else 0))
        if not value:
            return bytes(out)


def block(pairs) -> bytes:
    """One block's contents, in LevelDB's layout, restarting only at the top."""
    body, previous = bytearray(), b""
    for key, value in pairs:
        shared = 0
        while shared < min(len(key), len(previous)) and key[shared] == previous[shared]:
            shared += 1
        body += leb128(shared) + leb128(len(key) - shared) + leb128(len(value))
        body += key[shared:] + value
        previous = key
    body += struct.pack("<I", 0)      # one restart point, at offset 0
    body += struct.pack("<I", 1)      # and the count of them
    return bytes(body)


def framed(payload: bytes) -> bytes:
    """A block as it sits on disk: its length, a CRC this does not check, itself."""
    return leb128(len(payload)) + b"\0\0\0\0" + payload


def write(path, pairs, *, per_block: int = 2) -> None:
    """A whole MTBL file. `pairs` must be sorted by key."""
    blocks, offset, index = [], 0, []
    for at in range(0, len(pairs), per_block):
        chunk = pairs[at:at + per_block]
        on_disk = framed(block(chunk))
        index.append((chunk[-1][0], leb128(offset)))
        blocks.append(on_disk)
        offset += len(on_disk)

    index_block = framed(block(index))
    trailer = struct.pack(
        "<9Q", offset, 131072, 0, len(pairs), len(blocks),
        offset, len(index_block), 0, 0,
    )
    trailer += b"\0" * (mtbl.TRAILER - len(trailer) - 4) + mtbl.MAGIC
    with open(path, "wb") as handle:
        handle.write(b"".join(blocks) + index_block + trailer)


class Base(SimpleTestCase):
    pairs = [
        (b"html_root_aaaa", b"<html>one</html>"),
        (b"html_root_bbbb", b"<html>two</html>"),
        (b"html_root_cccc", b"<html>three</html>"),
        (b"uri_table_root_dddd", b'{"final":{}}'),
        (b"uri_table_root_eeee", b'{"final":{"a":"b"}}'),
        (b"zzz_last", b"end"),
    ]

    def table(self, pairs=None, **options):
        import tempfile
        from pathlib import Path

        folder = Path(self.enterContext(tempfile.TemporaryDirectory()))
        path = folder / "pages.mtbl"
        write(path, pairs if pairs is not None else self.pairs, **options)
        reader = mtbl.Reader(path)
        self.addCleanup(reader.close)
        return reader


class ReadingItBackTests(Base):
    def test_every_value_comes_back(self):
        table = self.table()
        for key, value in self.pairs:
            with self.subTest(key=key):
                self.assertEqual(table.get(key), value)

    def test_a_key_that_is_not_there(self):
        """A miss and an empty value are different answers."""
        self.assertIsNone(self.table().get(b"html_root_zzzz"))

    def test_a_key_past_the_end(self):
        self.assertIsNone(self.table().get(b"zzzzzzzz"))

    def test_a_key_before_the_start(self):
        self.assertIsNone(self.table().get(b"aaa"))

    def test_the_counts_the_file_states_about_itself(self):
        table = self.table()
        self.assertEqual(table.count, len(self.pairs))
        self.assertEqual(table.blocks, 3)
        self.assertEqual(len(table.keys), table.blocks)


class SharedKeyPrefixesTests(Base):
    """Keys are stored as a delta against the one before, which is where a
    reader that looks right on one file goes wrong on the next."""

    def test_keys_that_share_a_long_prefix(self):
        pairs = [(b"uri_table_root_" + bytes([c]) * 40, bytes([c])) for c in b"abcdef"]
        table = self.table(pairs, per_block=3)
        for key, value in pairs:
            with self.subTest(key=key[:24]):
                self.assertEqual(table.get(key), value)

    def test_a_block_holding_one_entry(self):
        table = self.table(per_block=1)
        self.assertEqual(table.get(b"html_root_bbbb"), b"<html>two</html>")

    def test_a_block_holding_all_of_them(self):
        table = self.table(per_block=99)
        self.assertEqual(table.blocks, 1)
        self.assertEqual(table.get(b"zzz_last"), b"end")


class ScanningARangeTests(Base):
    def test_a_prefix_returns_only_its_own(self):
        found = dict(self.table().scan(b"uri_table_root_"))

        self.assertEqual(sorted(found), [b"uri_table_root_dddd", b"uri_table_root_eeee"])

    def test_it_stops_rather_than_reading_the_rest_of_the_table(self):
        """The point of scanning a prefix. On the real corpus the routing
        tables are 170 MB of 31 GB, and reading to the end anyway would be the
        difference between a minute and an afternoon."""
        table = self.table()
        reads = []
        original = table.block
        table.block = lambda offset: (reads.append(offset), original(offset))[1]

        list(table.scan(b"html_root_"))

        self.assertLess(len(reads), table.blocks)

    def test_no_prefix_is_the_whole_table(self):
        self.assertEqual(len(list(self.table().scan())), len(self.pairs))

    def test_a_prefix_nothing_matches(self):
        self.assertEqual(list(self.table().scan(b"nothing_")), [])


class WhatItRefusesTests(Base):
    def test_a_file_that_is_not_one(self):
        import tempfile
        from pathlib import Path

        folder = Path(self.enterContext(tempfile.TemporaryDirectory()))
        path = folder / "not.mtbl"
        path.write_bytes(b"x" * 2048)

        with self.assertRaises(mtbl.NotAnMtblFile):
            mtbl.Reader(path)

    def test_a_trailer_that_disagrees_with_its_own_index(self):
        """The check that catches a reader drifting from the format rather
        than a file being broken: if the index does not decode to exactly the
        number of blocks the trailer claims, something is misread."""
        import tempfile
        from pathlib import Path

        folder = Path(self.enterContext(tempfile.TemporaryDirectory()))
        path = folder / "pages.mtbl"
        write(path, self.pairs)
        raw = bytearray(path.read_bytes())
        struct.pack_into("<Q", raw, len(raw) - mtbl.TRAILER + 32, 999)  # count_data_blocks
        path.write_bytes(raw)

        with self.assertRaises(mtbl.NotAnMtblFile):
            mtbl.Reader(path)

    def test_a_compression_it_has_no_decompressor_for(self):
        import tempfile
        from pathlib import Path

        folder = Path(self.enterContext(tempfile.TemporaryDirectory()))
        path = folder / "pages.mtbl"
        write(path, self.pairs)
        raw = bytearray(path.read_bytes())
        struct.pack_into("<Q", raw, len(raw) - mtbl.TRAILER + 16, 1)  # snappy
        path.write_bytes(raw)

        with self.assertRaises(mtbl.NotAnMtblFile) as caught:
            mtbl.Reader(path)
        self.assertIn("snappy", str(caught.exception))


class VarintTests(SimpleTestCase):
    def test_it_reads_what_the_writer_wrote(self):
        for value in (0, 1, 127, 128, 300, 21307, 60047630, 31458594388):
            with self.subTest(value=value):
                self.assertEqual(mtbl.varint(leb128(value))[0], value)

    def test_it_reports_where_it_stopped(self):
        buffer = leb128(300) + b"rest"
        value, at = mtbl.varint(buffer)

        self.assertEqual(value, 300)
        self.assertEqual(buffer[at:], b"rest")
