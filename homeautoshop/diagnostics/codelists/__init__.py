"""
The ISO/SAE code sets, which ship in the image. One JSON file per published set.

Data, not code (SPEC §8.1a), for the same reason the VIN schemes are: three
thousand rows transcribed from somebody's published document is a table, and a
table in a Python module invites somebody to put a condition in it.

**Only the standard's own sets are here.** J2012 calls these codes *ISO/SAE
controlled*: they mean the same thing on every vehicle ever built, there are
three and a half thousand of them, and an instance that has never reached a
network still has to know what `P0420` means. So they are bundled, and they
are matched to no make at all — a vehicle never *is* the standard, and keying
one to a name would let it answer a manufacturer's code.

**A manufacturer's list is published rather than bundled**, in `catalog/codes/`,
one file per make. There are ninety-odd makes and a shop owns three;
shipping them all would put eighteen thousand definitions in every image so
that each operator could use a few hundred. Parser profiles were split the
same way and for the same reason — see `catalog/README.md`. An installed list
is a row (`InstalledCodeList`), read by `dtc` alongside these, and
`codelistlib` is the validator every route in goes through.

**Not translated, on purpose.** The hand-written ISO/SAE table in `dtc.py` goes
through gettext because those definitions are standardized and finite. These
transcriptions are a publisher's own wording; a translation of them would be
somebody's paraphrase of a diagnostic definition, which is exactly what §8.3c
refuses to do.

`_rejected.json` stays here rather than in the catalog: it is a register of
documents examined and kept out, not a list, and `build_dtc_list` reads it
before transcribing anything. Compilations that merge several makes circulate
widely, and every one examined while building this was one make's document
with the attribution filed off — shipping one would put Ford's `P1106` against
a Chevrolet, which is the exact failure that scoping definitions by make
exists to prevent.
"""
