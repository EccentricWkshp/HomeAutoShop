"""
What a file actually is, as opposed to what the browser said it was.

Reported as: *I added webp images to a vehicle and they didn't show up.* They
were stored, and they were filed as **documents** — so they landed in the
Documents card instead of Photos, and because nothing there is treated as an
image, no thumbnail was ever made for them either.

The cause is that `Content-Type` on a multipart upload is a claim by the
client, and on Windows it is a claim the browser makes by looking the
extension up in the registry. `.webp` frequently has no association there, so
Chrome and Edge send `application/octet-stream` — for a file whose first twelve
bytes say plainly what it is. The same happens to `.avif`, and to anything else
whose format is newer than the machine's file associations.

So the bytes decide. Only a handful of signatures are checked, and that is
deliberate: this is not a general file-type oracle, it is the answer to *is
this a picture, and which kind*. What it cannot identify it does not guess at —
the claim stands, then the filename, and an unidentifiable file is still stored
and still downloadable.

The order matters and is the whole design: **content, then claim, then name.**
A claim is evidence and a name is a hint, but neither outranks the file.
"""

from __future__ import annotations

import mimetypes

#: Types that say nothing. A browser sends one of these when it has no idea,
#: which is exactly when the bytes are worth reading — and is not the same as
#: a browser stating a type that happens to be wrong.
VAGUE = frozenset({"", "application/octet-stream", "binary/octet-stream"})

#: `(offset, bytes, mime)`, checked in order. Kept short on purpose: every
#: entry is a format this application does something specific about.
SIGNATURES = (
    (0, b"\xff\xd8\xff", "image/jpeg"),
    (0, b"\x89PNG\r\n\x1a\n", "image/png"),
    (0, b"GIF87a", "image/gif"),
    (0, b"GIF89a", "image/gif"),
    (0, b"%PDF-", "application/pdf"),
    (0, b"II*\x00", "image/tiff"),
    (0, b"MM\x00*", "image/tiff"),
)

#: ISO base media files — HEIC, AVIF and friends — all begin the same way and
#: are told apart by the brand that follows. `ftyp` sits at offset 4.
ISO_BRANDS = {
    b"avif": "image/avif",
    b"avis": "image/avif",
    b"heic": "image/heic",
    b"heix": "image/heic",
    b"hevc": "image/heic",
    b"heim": "image/heic",
    b"heis": "image/heic",
    b"mif1": "image/heic",
    b"msf1": "image/heic",
}


def from_bytes(data: bytes) -> str:
    """The type the file's own first bytes state, or empty when they do not."""
    for offset, magic, mime in SIGNATURES:
        if data[offset:offset + len(magic)] == magic:
            return mime

    # RIFF containers carry their real type at offset 8: `WEBP` for a picture,
    # `WAVE` for audio. Checking only for `RIFF` would call a sound file a
    # photograph.
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"

    if data[4:8] == b"ftyp":
        return ISO_BRANDS.get(data[8:12], "")

    return ""


def content_type(data: bytes, *, filename: str = "", claimed: str = "") -> str:
    """What to record as this upload's type.

    The bytes first, because they are the only party with nothing to gain. A
    claim that is merely *wrong* is still kept where the content is
    unrecognizable — `application/vnd.…` for a spreadsheet is a fact this knows
    nothing about, and overriding it with a guess from the extension would be a
    downgrade.
    """
    found = from_bytes(data)
    if found:
        return found
    claim = (claimed or "").split(";")[0].strip().lower()
    if claim not in VAGUE:
        return claim
    guessed, _encoding = mimetypes.guess_type(filename or "")
    return guessed or claim
