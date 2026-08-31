# Parts orders — sample corpus

Supplier order confirmations, used to develop and regression-test the readers in
`homeautoshop/purchasing/importers/`.

## The PDFs are not in the repository, on purpose

An order confirmation is a **shipping document**. Every one of these carries a
name, a street address, a phone number and an email address — twice, in the Ship
To and Bill To block. None of it is anything the parser reads, and none of it
belongs in a public repository.

So the originals stay on the machine that downloaded them (`.gitignore`), and
what ships is a **capture**: the text and the word geometry with the address
block removed entirely.

```bash
python -m homeautoshop.purchasing.importers.capture
```

That writes `<order>.capture.json` and `<order>.expected.json` into
`homeautoshop/purchasing/importers/fixtures/`, which is what the tests run
against. The capture **refuses** rather than half-redacting: if it cannot find
the `Ship To:` block it writes nothing, because the first version searched each
*word* for `Ship To:` — which is two words, so it never matched, and every
capture came out with a real name in it.

## Adding a vendor

Drop the PDFs in a folder named for the vendor, write a reader beside
`rockauto.py`, and re-run the capture. Two things are worth copying from the
RockAuto one:

* **Read by word geometry, not by lines of text.** These layouts wrap, and they
  wrap *around* the row they belong to — a part number's second half prints
  below its own line and a description's first half above it. Reading order gets
  both wrong.
* **Reconcile against the document's own total.** Lines plus tax plus shipping
  less any rebate has to equal what the page says. It is the only check that
  catches a mis-read price, a dropped line or a quantity read as one instead of
  two, and it needs no fixture to be written by hand.
