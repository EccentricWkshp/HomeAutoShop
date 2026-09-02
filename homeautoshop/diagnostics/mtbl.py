"""
Reading MTBL, the immutable sorted-string table `libmtbl` writes.

Two of the online manual libraries publish their whole corpus as one of these —
a single file holding every page, keyed by content hash — and reading it beats
crawling the site it is served from on every axis that matters: it is complete
rather than sampled, it is the same answer every time, and it asks nothing of
somebody else's server. One 31 GB file here held 52.6 million entries.

There is no pure-Python MTBL library, and `libmtbl` is a C dependency this
application will not take on for one importer. So the format is read directly.
It is small enough to state in full:

* **Trailer** — the last 512 bytes: nine little-endian `uint64` fields (index
  block offset, data block size, compression, then counts), zero padding, and
  the four-byte magic `MTBL` stored little-endian, so it reads `LBTM`.
* **Block** — `varint(compressed length)`, a four-byte CRC, then the payload.
  The payload is a zstd frame when the file is compressed, and stored raw when
  compressing it would not have paid — which is how the index block of a table
  full of hex keys ends up plain.
* **Contents** — LevelDB's block layout, because that is what `libmtbl` uses:
  entries of `varint(shared) varint(unshared) varint(value length)`, the part
  of the key that differs from the one before, then the value; followed by an
  array of restart offsets and its count.

**Every step of that was checked against a number the file states about
itself** rather than taken on trust, and the same checks run on open: the magic
must match, and the index block must decode to exactly the number of entries
the trailer claims data blocks. A file that fails either is not read.

The CRC is skipped rather than verified. `libmtbl` writes a CRC32C, which the
standard library does not implement, and a wrong answer about corruption is
worse than an honest silence about it — a caller that needs the guarantee
should checksum the file.

    from homeautoshop.diagnostics import mtbl

    with mtbl.Reader("pages.mtbl") as table:
        page = table.get(b"html_root_7e20b492...")
        for key, value in table.scan(b"uri_table_root_"):
            ...
"""

from __future__ import annotations

import bisect
import struct
from compression import zstd

#: `MTBL` little-endian, at the very end of the file.
MAGIC = b"LBTM"
TRAILER = 512

#: Enough for the varint that opens a block plus room to spare.
HEADER = 16

#: What `libmtbl` numbers the algorithms. Only the ones that can actually turn
#: up here are named; anything else is refused by name rather than guessed at.
COMPRESSION = {0: "none", 1: "snappy", 2: "zlib", 3: "lz4", 4: "lz4hc", 5: "zstd"}
ZSTD_MAGIC = bytes.fromhex("28b52ffd")


class NotAnMtblFile(ValueError):
    """The file is not one of these, or is not one this can read."""


def varint(buf: bytes, at: int = 0) -> tuple[int, int]:
    """One LEB128 integer and the offset just past it."""
    value = shift = 0
    while True:
        byte = buf[at]
        at += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, at
        shift += 7


def entries(block: bytes):
    """Every key and value in one decoded block, in order.

    Keys are stored as a delta against the one before, so this has to be read
    forwards from a restart point. It always reads from the first.
    """
    restarts = struct.unpack_from("<I", block, len(block) - 4)[0]
    end = len(block) - 4 - 4 * restarts
    at, key = 0, b""
    while at < end:
        shared, at = varint(block, at)
        unshared, at = varint(block, at)
        length, at = varint(block, at)
        key = key[:shared] + block[at:at + unshared]
        at += unshared
        yield key, block[at:at + length]
        at += length


class Reader:
    """One MTBL file, opened for reading.

    The block index is held in memory — 60 MB for the 31 GB table this was
    written against, which is the price of answering a lookup with one seek
    instead of a scan. Everything else is read on demand.
    """

    def __init__(self, path):
        self.path = str(path)
        self.file = open(self.path, "rb")
        try:
            self._read_trailer()
            self.keys, self.offsets = self._read_index()
        except Exception:
            self.file.close()
            raise
        if len(self.keys) != self.blocks:
            raise NotAnMtblFile(
                f"{self.path}: the index holds {len(self.keys)} blocks and the "
                f"trailer claims {self.blocks}"
            )

    # -- opening ----------------------------------------------------------

    def _read_trailer(self) -> None:
        self.file.seek(-TRAILER, 2)
        trailer = self.file.read(TRAILER)
        if len(trailer) != TRAILER or trailer[-4:] != MAGIC:
            raise NotAnMtblFile(f"{self.path}: no MTBL magic at the end")
        (
            self.index_at,
            self.block_size,
            compression,
            self.count,
            self.blocks,
            self.bytes_blocks,
            self.bytes_index,
            self.bytes_keys,
            self.bytes_values,
        ) = struct.unpack_from("<9Q", trailer, 0)
        self.compression = COMPRESSION.get(compression, str(compression))
        if self.compression not in ("none", "zstd"):
            raise NotAnMtblFile(
                f"{self.path}: compressed with {self.compression}, which this "
                "reader does not carry a decompressor for"
            )

    def _read_index(self):
        keys, offsets = [], []
        for key, value in entries(self.block(self.index_at)):
            keys.append(key)
            offsets.append(varint(value, 0)[0])
        return keys, offsets

    # -- reading ----------------------------------------------------------

    def block(self, offset: int) -> bytes:
        """One block, decompressed.

        The length that opens a block is of the bytes *on disk*, which is what
        bounds the zstd frame. Handing the decompressor everything from here to
        the end of the file makes it read the next block as a second frame and
        refuse the lot.
        """
        self.file.seek(offset)
        length, used = varint(self.file.read(HEADER), 0)
        self.file.seek(offset + used + 4)  # past the length and the CRC
        payload = self.file.read(length)
        if payload[:4] == ZSTD_MAGIC:
            return zstd.decompress(payload)
        return payload

    def get(self, key: bytes) -> bytes | None:
        """The value stored under `key`, or None.

        Each index entry names a key at least as large as the last key in its
        block, so the first entry not less than the wanted key names the only
        block that could hold it.
        """
        at = bisect.bisect_left(self.keys, key)
        if at >= len(self.keys):
            return None
        for found, value in entries(self.block(self.offsets[at])):
            if found == key:
                return value
            if found > key:
                return None
        return None

    def scan(self, prefix: bytes = b""):
        """Every key and value beginning with `prefix`, in order.

        Reads only the blocks that can hold the prefix. On a table where one
        kind of key is a small part of the whole — the per-vehicle routing
        tables here are about 170 MB of 31 GB — that is the difference between
        a minute and an afternoon.
        """
        at = bisect.bisect_left(self.keys, prefix) if prefix else 0
        while at < len(self.keys):
            for key, value in entries(self.block(self.offsets[at])):
                if key.startswith(prefix):
                    yield key, value
                elif key > prefix:
                    # Sorted, so nothing further along can match either.
                    return
            at += 1

    # -- housekeeping -----------------------------------------------------

    def close(self) -> None:
        self.file.close()

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()

    def __repr__(self) -> str:
        return (
            f"<mtbl.Reader {self.path} {self.count:,} entries, "
            f"{self.blocks:,} blocks, {self.compression}>"
        )
