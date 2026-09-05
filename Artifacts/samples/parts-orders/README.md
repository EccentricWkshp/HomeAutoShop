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

## What is read

| Folder | Document | Reader |
| --- | --- | --- |
| `rockauto/` | *RockAuto Order Confirmation* — the emailed one, or the page | `rockauto.py` |
| `napa/` | *Your Order History Details* — printed from the browser | `napa.py` |
| `amazon/` | *Final Details for Order #…* — the invoice view | `amazon.py` |

Two documents in this corpus are **not** read, and both are recorded rather
than quietly failing:

* `rockauto/Gmail - Here's your receipt…` is a Gmail print of the receipt
  rather than the confirmation, and has a different layout.
* `amazon/Order Details 1.pdf` is the older *Order Summary* page, which is not
  the invoice layout the other four use.

Neither is a crash — every reader refuses a document it does not recognize, and
the screen names the formats it does read.

## Fixtures: captured, or written out

`rockauto.py` is tested from **captures**, because its layout is a table read by
word geometry and no fixture anybody writes by hand would exercise the wrapping
that makes it difficult.

`napa.py` and `amazon.py` are tested from fixtures **written out in
`tests_orders.py`**. Their layouts are rows of text, so a fixture is legible in
the test file, and it means neither of those documents' personal data has to be
redacted correctly for the suite to run at all — the strongest version of not
having it in the repository is not extracting it in the first place.

`capture.py` only knows how to find and remove RockAuto's `Ship To:` block. It
would have to learn NAPA's `Pickup Person:` block and Amazon's `Shipping
Address:` and `Billing address` blocks before it could be pointed at those
folders, and it refuses rather than half-redacting, so running it against them
today writes nothing.

## Adding a vendor

Drop the PDFs in a folder named for the vendor, write a reader beside
`rockauto.py`, and add it to `orders.readers()`. A reader has to expose
`VENDOR_NAME` and a `parse(bytes)` that **raises a `ValueError` when the
document is not its own** — recognition is each reader's own job, and one that
tried hardest to extract something would be wrong in the expensive direction.

Things worth copying:

* **Read by word geometry, not by lines of text.** These layouts wrap, and they
  wrap *around* the row they belong to — a part number's second half prints
  below its own line and a description's first half above it. Reading order gets
  both wrong.
* **Reconcile against the document's own total.** Lines less any discount, plus
  tax and shipping, has to equal what the page says. It is the only check that
  catches a mis-read price, a dropped line or a quantity read as one instead of
  two, and it needs no fixture to be written by hand. It earned its place
  immediately: the Amazon reader took the printed price as the line total, and
  a two-parcel order of washer fluid reported $11.94 of $35.82 until the
  reconciliation said so. Amazon prints a price *each*; NAPA prints the
  extended figure. Nothing but the arithmetic tells you which.

* **Take the figure the document states.** Where a vendor prints what the line
  came to, store that — do not divide it by the quantity and multiply back.
  NAPA sells a five-gallon drum for $182.39, which is $36.478 a gallon, and a
  per-unit price rounded to the cent turns the line into $182.40.

* **A general retailer is not a parts vendor.** An Amazon basket carries
  whatever was in it: one sample here has eight items of which exactly one is a
  part and seven are tools. Nothing in the document says which, so the reader
  takes no view and the review screen asks. Lines left out are left out of the
  tax and shipping too.
