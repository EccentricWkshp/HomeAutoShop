# HomeAutoShop Help

HomeAutoShop is a self-hosted record for a home garage. It keeps vehicles and
equipment, maintenance, inspections, work, parts, purchases, documents, and
diagnostic history together. It is designed for personally owned vehicles and
equipment; it is not an invoicing, customer-billing, or payroll system.

This guide describes the user-visible behavior implemented in the current
application. It does not treat requirements or ideas in `Artifacts/` as shipped
features. Installation and server administration are covered separately in
[INSTALL.md](INSTALL.md).

## Contents

- [Getting started](#getting-started)
- [Finding your way around](#finding-your-way-around)
- [Vehicles and equipment](#vehicles-and-equipment)
- [Maintenance schedules](#maintenance-schedules)
- [Inspections](#inspections)
- [Oil and fluid analysis](#oil-and-fluid-analysis)
- [Work orders](#work-orders)
- [Parts and inventory](#parts-and-inventory)
- [Purchasing and receipts](#purchasing-and-receipts)
- [Diagnostics and scan reports](#diagnostics-and-scan-reports)
- [Costs, reports, and exports](#costs-reports-and-exports)
- [People, accounts, and permissions](#people-accounts-and-permissions)
- [Templates, checklists, and parser profiles](#templates-checklists-and-parser-profiles)
- [Search, photos, documents, and OCR](#search-photos-documents-and-ocr)
- [Reminders and notifications](#reminders-and-notifications)
- [Integrations and outbound privacy](#integrations-and-outbound-privacy)
- [Using the installed web app and losing connectivity](#using-the-installed-web-app-and-losing-connectivity)
- [Backups, trash, and recovery](#backups-trash-and-recovery)
- [Spreadsheet import and the API](#spreadsheet-import-and-the-api)
- [Common questions](#common-questions)

## Getting started

### Set up a new shop

A new instance has no default account. The first browser to open an empty
instance is sent to **Set up your shop**. Create the administrator account,
choose the shop name, units, currency, timezone, and language, and decide
whether to install the starter schedules, inspection checklists, scan-report
profiles, and manual-library definitions.

The setup screen stops being available as soon as the first account is
created. Administrators add everyone else under **People and accounts**.

HomeAutoShop must be opened over HTTPS. Camera scanning, the installed web app,
and direct ELM327 access depend on browser secure-context features. See
[INSTALL.md](INSTALL.md) for hostname, TLS, LAN access, and certificate setup.

### A useful first pass

1. Add a vehicle under **Vehicles**. Only a nickname is required.
2. Record its current odometer, hour-meter, or cycle reading.
3. Open **Schedule** on the vehicle and apply a starter schedule, then edit the
   intervals to match the vehicle and its real service information.
4. Add storage locations under **Shelf** and the parts already on hand.
5. Create a work order and list the job items and parts it will need.
6. Add another account only after deciding whether that person should be an
   administrator, a full member, or a helper limited to named vehicles.
7. Go to **Backup**, run the first backup, download it, and copy it off the
   server's disk.

## Finding your way around

The wide-screen navigation contains nine working sections:

- **Vehicles** — vehicles, trailers, and serviceable equipment.
- **Work** — planned, active, blocked, completed, and abandoned work orders.
- **Due** — maintenance that is due soon or overdue.
- **Inspect** — digital vehicle inspections and measurement history.
- **Parts** — the parts catalog, cross-references, fitment, kits, and cores.
- **Shelf** — stock by physical location, low stock, expiring stock, and labels.
- **Purchases** — vendors, orders, receiving, receipts, and return windows.
- **Reports** — spending, cost forecasts, vehicle costs, inventory value, and
  warranty dates.
- **People** — the household, owners, operators, and other contacts.

**Today** is the dashboard. It brings together due maintenance, open work,
registration expirations, jobs deliberately marked as waiting for parts, and
jobs that the stock calculation says are short of parts.

On a phone, the bottom bar keeps **Today**, **Vehicles**, **New job**, **Due**,
and **Search** one tap away. The other sections are in the account menu.
Administrator-only screens—including settings, accounts, backups, imports,
integrations, templates, tools, health, and trash—are also in that menu.

The search box in the header searches across vehicle identity, work orders,
parts and alternate part numbers, people, work-order notes, filenames, and text
read from uploaded documents.

## Vehicles and equipment

### Add and identify an asset

Choose **Vehicles → Add**. A record can represent either a vehicle or
serviceable equipment. Vehicle classes include car, truck, motorcycle,
trailer, RV, bus, and other plated vehicles.

The form shows only the fields the chosen kind has. Equipment has
manufacturer, model-number and serial-number fields and no VIN, license plate,
vehicle class or registration at all, and it measures engine hours rather than
miles. Changing the kind on an existing record clears the fields belonging to
the other one. Opening the form from the **Equipment** tab starts it on
equipment.

Use the status to describe the asset's real place in the shop:

- **Prospect** for something being considered or inspected before purchase.
- **Active**, **Project**, or **Stored** for something still owned.
- **Sold**, **Parted out**, or **Totaled** to keep its history without treating
  it as part of the current fleet.

Disposed vehicles are hidden from the default vehicle list but remain
available with **Include sold**. Prospects and disposed vehicles do not count
toward the dashboard fleet total.

### VINs and license plates

VIN validation happens locally as the number is entered. For a modern
17-character VIN, the application checks allowed characters and the check
digit. A short VIN can be accepted as a pre-1981 format when its model year
supports that interpretation.

**Scan** reads Code 39, QR, Data Matrix, or PDF417 from a door-jamb label on the
device. Camera barcode detection currently requires Chrome or Edge over HTTPS.
The scan is processed on the device; camera frames are not uploaded.

**Look up** asks the NHTSA vPIC service to decode the VIN. It fills only fields
that are still empty. Values already typed are not overwritten, and later
manual corrections are protected from a subsequent decode. Offline Mode
disables the network lookup but never prevents saving the vehicle by hand.

For supported older vehicles, HomeAutoShop can show one or more readings from
the manufacturer's historical VIN tables. It shows the source and any
unrecognized positions instead of silently choosing between ambiguous schemes.

License-plate lookup is off until an administrator configures a commercial
provider. Each lookup has a confirmation screen showing that the plate will
leave the network, the month's usage, the configured cap, and the estimated
per-call cost. A returned VIN is still checked locally before it is offered for
creating a vehicle.

### What lives on a vehicle record

The vehicle page brings together:

- recent activity and a full chronological history;
- work orders and inspections;
- odometer, engine-hour, or cycle readings;
- photos, documents, and useful web links;
- owners and other ownership roles over time;
- maintenance schedule and installed components;
- specifications, recall campaigns, and diagnostic history;
- service-manual links;
- cost detail and the vehicle report.

Use **Add reading** whenever the meter changes outside a work order. Readings
are append-only. A lower reading is retained and marked as a rollback rather
than rewriting the earlier record.

Photos open in an in-page viewer. Documents such as titles, insurance cards,
manuals, and PDFs stay in a separate document list and open in a browser tab.
**Rename** on a document row gives it a name of your own — a manual that
arrives as `31P8770110E1.pdf` can be called what you would actually look for —
and the file name stays underneath it. The name belongs to that attachment, so
a receipt filed against both a purchase and a work order can be called
something different in each.

Adding files is two steps in order: choose the files, then **Upload**. The
upload button stays inactive until something is chosen and then names how many.

Ordinary links store only their label, address, and note; HomeAutoShop does not
fetch the linked page.

### Specifications

**Specs** stores values such as fluid capacities, torque values, tire
pressures, alignment ranges, electrical identifiers, paint codes, and access
codes. A spec can carry a unit, a condition, a source, notes, and an optional
maximum value.

**Pin** on a spec row puts it on the quick-reference panel of every work order
for that vehicle, and **Unpin** takes it off again; neither needs the edit
form. A sensitive spec can be pinned but still will not appear there — access
codes are kept off work orders and reports deliberately, and the message says
so when you pin one.

Specs can be entered manually, copied from another vehicle, proposed from a VIN
decode, or read from a supported scan-tool PDF. Scan-derived module identifiers
are previewed before they are recorded.

Key, radio, and other access codes are marked sensitive automatically.
Sensitive specs stay out of the vehicle sale report and shared exports.

### Recall campaigns

The recall screen can ask NHTSA for campaigns by year, make, and model. Results
are not VIN-specific, and the free data does not say whether a repair was
already performed on a particular vehicle. Record the owner status, completion
date, and dealer notes yourself.

Coverage is for US-market NHTSA data. An empty result is not proof that a
vehicle is clear, especially for vehicles registered outside the United
States. The screen links to NHTSA's VIN-specific checker for a separate check.

### Deleting versus disposing

Use **Sold**, **Parted out**, or **Totaled** for a vehicle whose history should
remain. Deletion is offered only while the vehicle has nothing attached. A
deleted vehicle is soft-deleted and can be restored from **Trash** during the
retention period.

## Maintenance schedules

Open a vehicle and choose **Schedule**.

Each scheduled item can use a distance interval, a time interval, an engine
hour interval, or any combination. Whichever threshold arrives first makes the
item due. The due-soon windows are configured under **Settings → Maintenance**.

When enough readings exist, HomeAutoShop estimates future distance-based due
dates from that vehicle's observed usage rate. With too little history it uses
the shop's configured fallback and says that it did so. Meterless equipment
does not receive a made-up usage rate.

### Build a schedule

- **Start from a template** adds editable schedule items appropriate to the
  asset kind. Built-in intervals are generic starting points, not manufacturer
  service data.
- Applying a template again does not overwrite intervals already changed.
- **Replace what is here** removes untouched items absent from the new template.
  Items with completion history stay because that history must not be erased.
- **Add an item** creates a custom schedule entry.
- **Ignore** keeps an item and its history but stops tracking it. **Track**
  turns it back on.
- **Snooze** temporarily takes an item out of the due list. Completing it clears
  the snooze.
- **Remove** is available only when the item has no completion history. Ignore
  an item that already has history.

Use **Done** to backfill service completed without a HomeAutoShop work order.
An older backfilled completion is retained but does not move a schedule
backward past a newer completion.

A work-order job item can be linked to a scheduled service. Marking the job
item done rolls that service interval forward once; toggling the item does not
create duplicate completions.

### Installed components

Record items whose age or wear matters—such as tires or batteries—with what
it is, where it is (LF, RF, LR, RR), and its serial or DOT code. The
installation date and the meter reading are filled in for you from today and
the vehicle's current reading, which is what later turns a measurement into a
wear rate. Inspection
measurements can then be interpreted against the life of that component.

For tires, a readable DOT date code produces an age warning at six years and a
failure at ten years regardless of remaining tread. A missing or malformed code
is shown as unknown rather than guessed.

## Inspections

Choose **Inspect**, select a vehicle and checklist, and choose **Start
inspection**. To inspect a vehicle before buying it, first add the vehicle with
the **Prospect** status.

Each inspection receives a snapshot of its checklist. Later changes to the
template do not rewrite inspections already started.

For every check, record a status—**Pass**, **Attention**, **Fail**, **Not
applicable**, or **Not inspected**—plus an optional note, measurement, and
photo. Safety-critical and optional-if-fitted points are marked. Measurement
thresholds can suggest a status; the human can override it, and the record
keeps both the rule's conclusion and the override.

Use **Add a check** for a one-off accessory, drivetrain item, or known weak
point. The new check belongs only to this inspection and does not alter the
template. One-off checks may be removed while the inspection is in progress.

An inspection can be signed off, abandoned while keeping its results, resumed,
or deleted to Trash if it was started by mistake. If a checklist requires photo
evidence, the inspection warns before sign-off. After sign-off, **Turn findings
into work** creates work from attention and failed findings. The detail page
also compares results with the previous inspection and calls out changes that
became better or worse.

**Measurement trends** gathers repeated measurements for the vehicle. With two
or more readings against meter history, it can show a wear rate and projected
due point.

## Oil and fluid analysis

Open a vehicle and choose **Fluids** to record a lab report and to see how its
numbers have moved. Samples also appear on the vehicle's history, and on the
work order they were taken for if you named one.

Record where the sample came from — engine oil, transmission, a differential,
coolant — and, if the vehicle has more than one of that thing, which one. Two
figures matter and one of them is easy to leave out:

- **The meter reading** when the sample was drawn.
- **How far the fluid itself had run** since it was changed.

The second is what makes one sample comparable with the next. Wear metals build
up while the fluid is in service, so 24 ppm of iron on 3,000 miles of oil and
24 ppm on 9,000 miles are not the same result. A sample recorded without it is
kept and shown, but it is left out of the trend and the screen says so rather
than quietly averaging it in.

**Paste the panel rather than typing it.** Put one analyte per line in the
Results box — `Iron 24`, `Fe: 24 ppm`, `Viscosity @ 100C 10.9` all work, and so
does a lab's own average in brackets, as in `Iron 24 ppm (avg 18)`. Element
symbols are understood. A line that cannot be read is listed back to you with
the reason instead of being dropped, so a panel that looks complete is
complete. Something the application has never heard of is still recorded under
its own name.

The trend shows each measurement, its latest value, its rate per 1,000 units of
fluid life, and how that compares with the previous sample. The rate column is
filled in only where a rate means something: wear metals and contamination
accumulate, so they are shown as rates, while viscosity and the additive pack
describe the fluid as it is now and are compared on their face value.

**HomeAutoShop does not tell you whether a result is good.** Limits depend on
the engine and labs disagree about them, so the only judgment on the page is
the lab's own comment, stored as written and attributed to them. What the
application adds is arithmetic on your own samples — *three times the previous
one* — which is a fact about your history rather than an opinion about your
engine.

**Attach the report to the sample.** The sample page has a box for it — the
PDF the lab emailed, or a photograph of the printed sheet. It belongs on the
sample rather than loose among the vehicle's documents, because the question it
answers is *did I type this figure correctly*, and that is asked while looking
at the figure. The report is not read automatically; the numbers are the ones
you pasted.

## Work orders

### Plan the job

Choose **New job** and select the vehicle. Work types are maintenance, repair,
diagnosis, modification, inspection, and project. Long-running work can be a
parent project with child work orders; a work order cannot become its own
ancestor.

The main description follows the three-C pattern: **Complaint** is what was
reported, **Cause** is what diagnosis found, and **Correction** is what was
done.

Add ordered job items, assign them to a person, and mark each as pending, in
progress, done, or skipped. The up/down controls work without drag-and-drop. A
job item that has parts used against it cannot be removed.

The work-order **Log** is append-only. Add short, time-stamped notes as the job
develops. Existing notes are deliberately not editable.

Photos can be marked general, before, after, or receipt. On a phone, **Take a
photo** opens the camera while the adjacent file chooser can attach images
already taken.

### Check parts readiness

List every required part under **Parts needed**, optionally against a specific
job item. This is a plan and does not remove anything from inventory.

HomeAutoShop compares the requirement with quantity on the shelf, stock already
claimed by earlier open jobs, quantity already used, and placed orders linked
to this job. A draft shopping cart does not count as on order until it is
placed.

Shortages appear both on the work order and on **Today**. You can mark the job
**Waiting on these** or create a draft purchase for exactly the missing
quantities. These are warnings and planning aids; they do not prevent starting
work.

**Take it** or **Use** records what actually went on the vehicle. Stock is drawn
from the oldest lot first and carries that lot's real cost into the work order.
Installing a part on a vehicle also confirms its fitment.

### Track time, expenses, tools, and costs

Time entries record who worked, when, and for how long. They remain editable so
a running-time mistake can be corrected. Expenses record non-part costs by
category, amount, date, vendor, and description.

When the WrenchLedger integration is configured, job items can reference tools.
HomeAutoShop warns if a required tool is unavailable, on loan, overdue for
calibration, or represented by stale data. A tool warning never prevents work.
Tools can also be added locally when they are not in WrenchLedger.

The work order shows its running cost from parts, expenses, and any cost options
enabled by the administrator.

**Budget.** Give a work order a budget and it grows a burn-down. The bar shows
three things separately, because they answer different questions: what has been
**fitted** to the vehicle, what is **on the shelf** — bought for this job,
arrived, not yet used — and what is still **on order**. What is left is measured
against all three, so a job does not read as comfortably under budget the day
before a delivery arrives.

A budget on a project counts everything underneath it. The teardown, the
machine work and the reassembly are the project's spend, and the project's page
lists them with the whole total above.

An overrun is shown as a figure and drawn past the budget marker rather than
stopping at the end of the bar. Your own time is never charged against a
budget, even when a labour rate is configured: a household budget is money, and
the hours are reported beside the figures instead.

### Move a work order through its lifecycle

The available next statuses depend on the current status. Planned work can
start, wait on parts, or be abandoned. In-progress work can return to planned,
wait on parts, go on hold, complete, or be abandoned. Waiting and on-hold work
can return to planning or active work. Completed or abandoned work can be
reopened.

Moving to **Waiting on parts** requires a reason so the dashboard can explain
the block. Completing work on an asset with a meter requires the meter reading
at completion; that value becomes a vehicle reading. Meterless equipment can
complete without one.

Deleting a work order sends it to Trash from any status. Deleting a parent is
refused while child work orders still point to it.

## Parts and inventory

### Build the parts catalog

Choose **Parts → Add part**. Record the name, category, manufacturer, part
number, type, stocking unit, usual price, minimum quantity, notes, and whether
the part is a consumable or carries a core charge.

Search finds names, brands, manufacturer numbers, interchange numbers, vendor
SKUs, and UPCs. **Scan a barcode** can find a part by UPC/EAN, Code 128, or QR
in Chrome or Edge over HTTPS. If the code is unknown, the create flow keeps the
scanned code so it can become a cross-reference.

Use **Other numbers** on the part to add OEM, interchange, vendor SKU, and UPC
values. Removing one means future searches and scans for that value stop
finding the part.

Fitment can name one of the shop's vehicles or a year/make/model range, engine,
and position. Confidence distinguishes confirmed fitment, a vendor claim, an
inference, and a known non-fit. Fitment remains editable because a confident
wrong answer is worse than a missing answer.

### Stock lots and the ledger

Inventory is held in lots. Each lot records its location, quantity, unit cost,
acquired date, expiry date, and source purchase when applicable. Consumption is
FIFO: the oldest available lot is used first.

Quantity is a ledger, not a box that is silently overwritten. Use **Adjust** to
record a physical count and the reason for the difference. Edit a lot to
correct its cost, location, or dates. A lot cannot be deleted while quantity
remains or while history depends on it.

Parts may be used directly without a work order. The direct-use form can record
the vehicle, date, and note, and still removes stock FIFO. This is useful when
backfilling older maintenance or recording a quick installation.

A part cannot be deleted while stock remains. When eligible, deletion is soft
and recoverable from Trash.

### Organize the shelf

**Shelf** groups lots by nested physical locations. Create locations that match
the real shop—cabinet, shelf, drawer, bin—and move them within the hierarchy.
The screen also shows unfiled lots, items below their minimum, lots nearing
expiry, and total inventory value.

**Print bin labels** produces QR codes for locations. Scanning one with the
in-app scanner opens the contents of that location. The same label screen can
print vehicle QR labels.

### Kits

A kit is a catalog part whose box contains other catalog parts. Record each
component, quantity, and optional price. The contents can be found in search
even while they remain inside the closed box, but they do not count as separate
shelf stock yet.

Opening a kit removes the box from stock and creates lots for its contents.
The kit cost is divided in proportion to the component prices; if any component
has no price, the split is even and the page says so. **Put it back together**
reverses the opening only while none of the released contents have been used.

### Core returns and warranties

When a core-bearing part is installed, it appears under **Parts → Cores** as
owed. Multiple cores can be marked returned in one action. A mistaken return
can be changed back to **Still owed**. The page totals the outstanding value.

If an installed-part record carries warranty metadata—for example from an
import—the unexpired warranty appears under **Reports → Under warranty**. The
ordinary part-use form does not currently ask for warranty terms.

## Purchasing and receipts

### Vendors and orders

Vendors can be online sellers, local stores, dealers, salvage yards, machine
shops, or individuals. Record contact details, account number, notes, and the
normal return-window length.

A purchase holds the vendor, order number, status, order date, tax, shipping,
discount, payment method, optional work order, and notes. Lines carry the part
or description, ordered quantity, unit price, and core charge.

Statuses are **Cart**, **Ordered**, **Partially received**, **Received**,
**Returned**, and **Canceled**. A parts-shortage action on a work order creates
a Cart linked to that job so quantities can be reviewed and priced before the
order is placed.

### Receive parts

Receive each line into a location. Partial receiving is supported. Receiving
creates stock lots, distributes tax and shipping across lines in proportion to
value, assigns landed unit cost, updates the purchase status, and starts the
vendor return window when the order is fully received.

**Undo receiving** writes a reversing stock-ledger entry. It is refused when
the received stock has since been consumed. A line can be corrected only
before any of it is received, and a purchase cannot be deleted until receiving
has been undone.

Attach receipt photos or PDFs to the purchase. Receipt text is read in the
background when OCR is enabled, making the receipt searchable.

### Read a supplier order PDF

**Read an order file** currently supports a RockAuto order-confirmation PDF.
The preview shows the order, totals, lines, new and matched parts, fitment, kit
contents, and warnings before writing anything. The held preview can then be
committed without choosing the file again.

The import creates or matches the purchase, catalog parts, lines, vendor-stated
fitment, and supported kit relationships. Importing the same order again does
not create a duplicate. Once any of the order has been received, re-import
leaves it alone rather than rewriting the source of existing stock.

## Diagnostics and scan reports

Open a vehicle and choose **Diagnostics**.

### Import a report or photographed printout

The report reader accepts PDF, CSV, text, JSON, and images. Prefer a tool's
native text or CSV export when available. A photo can be used for a battery
tester, charging-system printout, or another device that produces only paper;
OCR output must be checked before confirmation.

**Photographing a printout.** Lay the slip flat, fill the frame with it, and
avoid a shadow falling across part of the paper. The photo does not need to be
straight or rotated first — the reader handles the phone's own orientation — but
a receipt half in shade reads much worse than one evenly lit. If a strip holds
several test results, one photo of the whole strip is fine: each is read as its
own result.

An imported scan starts as a draft. The review screen shows the parser profile
and match confidence, extracted fields, trouble codes and descriptions, live
data when present, and the original report. Nothing enters the vehicle history
until **Add to the history** is pressed. Correct the date, tool, model, meter,
or notes first, or discard the draft.

### Bench tester results

A battery, cranking or charging tester prints a verdict and a handful of
readings rather than trouble codes, so those appear in their own **Test
results** section — one card per receipt, with the verdict, the tester's own
clock and each reading beside the patch of the photograph it was read from.

Anything marked *hard to read* is worth comparing against that crop. That mark
is about the photograph, not about the reading — a rated capacity is no less
true for having been printed faintly.

What the tester **measured** and what it was **set to** are listed separately.
The capacity, rating standard and battery type are read off the battery's own
label and typed into the tester before the test runs; `Measured 755 CCA` against
`Rated 850 CCA` is the result.

A reading can be typed over while the scan is a draft, and doing so re-dates the
scan if it was the clock that was wrong. Corrections are saved separately from
what the reader saw, which is kept unchanged so it stays possible to ask later
what the tool actually printed — where a value has been corrected, the original
is shown struck through beside it. A value the reader could not make sense of is
shown as the characters it saw with the reason beside it, rather than being
dropped or guessed at.

A battery, cranking or charging tester never reports trouble codes, so that
section is not shown for one, and its line in the scan history names what it
found rather than saying "0 codes". Which tools can report what is part of the
parser profile, so a new tool declares it rather than the screens guessing.

### Re-reading, and taking a scan back out

**Re-read** on a scan already in the history makes a new draft and leaves the
original alone, so two profiles' answers can be compared before choosing. That
draft says which reading it replaces, and confirming it *replaces* that one
rather than adding a second — the earlier reading goes to the trash. Uploading
the same file again behaves the same way, because it is the same report.

A scan already in the history can be taken back out with **Remove from the
history**. It and its codes leave the vehicle at once and sit in the trash,
where they can be restored for 30 days.

ISO/SAE codes — the ones that mean the same thing on every vehicle — use the
built-in offline dictionary, which needs no network and no setup.

Manufacturer-specific codes, where `P1345` means one thing to GM and another to
Toyota, are answered by that maker's own published list. Those are **installed
rather than shipped**: there are around ninety makes and most shops work on
two or three. Install the ones you need under **Templates, checklists and
profiles → Browse the catalog → Manufacturer code lists**. Until a make's
list is installed, its manufacturer codes fall back to the description imported
from the report and to any description recorded in the shop. Nothing is ever
assigned a meaning by guesswork.

### Unrecognized reports and better profiles

When no profile recognizes a tabular file, **Map the values by hand** lets the
user identify the important columns. The mapping can be saved as a parser
profile for future reports from that tool. For unstructured text, codes can be
entered one per line.

The original file is retained. **Re-read** can apply a different or improved
profile later. Re-reading a confirmed scan creates a new draft and leaves the
existing history unchanged.

### Work from trouble codes

Confirmed codes can be open, addressed, ignored, or marked as having come
back. **Make a job** creates work from a code. If a code appears again after it
was addressed, it is called out as recurring rather than silently opened as a
new unrelated problem.

Configured manual-library links can open the relevant diagnostic-code section
for the vehicle. These are constructed links; HomeAutoShop does not check that
the target library has the page before opening it.

### What a code means

Every code is a link to its own page, wherever it appears. That page shows what
the code means and **who says so**, which matters because the sources are not
equally reliable:

- **The standard (SAE J2012).** *ISO/SAE controlled* codes — `P0` and `P2`,
  and their equivalents in the other systems — mean the same thing on every
  vehicle ever built. Nothing overrides these. You may hear them called
  "generic"; the standard does not use that word.
- **The manufacturer's own list.** Lists for 56 makes are published — about
  fifty-eight thousand manufacturer-controlled definitions in all, from Acura
  and Alfa Romeo through to Volvo and VW, including the heavy trucks. Most of
  them are read from the makers' own service manuals, so the wording is the
  manufacturer's rather than somebody's summary of it. These are **installed
  rather than built in**: most shops work on two or three makes, and carrying
  ninety would be a great deal of weight for a few hundred useful rows.
  Install the ones you need under **Templates, checklists and profiles →
  Browse the catalog**. Infiniti's is the largest at around eight thousand.
  A make can have more than one, and where it does the more specific answers
  first: Lincoln has its own list now and reads Ford's underneath it for the
  codes its own does not carry, and where a particular vehicle's service
  manual has been added it answers ahead of the general list. The page always
  names the document it came from. Until a make's list is installed, its
  manufacturer-controlled codes fall back to what the tool printed and what
  you have written down.
- **The standard's own P, B, C and U lists, for an ISO/SAE code.** These mean
  the same thing on every vehicle, so they answer whatever you drive — about
  thirty-five hundred codes. Manufacturer-controlled codes are never borrowed
  across makes this way, and the page always says whose wording you are
  reading.
- **What you wrote down.** A note you record for a make is shown ahead of a
  published list, because you are the one holding the vehicle. It does not
  displace a standard definition, which is the same on every vehicle.
- **What the scan tool printed.** Ranked below all of the above, because a tool
  is a third party rendering somebody else's definition — it truncates, and it
  sometimes declines outright. One tool answers a Ford `B1695` with "Please See
  The Vehicle Service Manual." where Ford's own list says "Autolamp On Circuit
  Short To Battery". What the tool read is still shown underneath, since the
  reading is what it is.
- **The shape of the code.** Failing all of the above, the code's own structure
  still says the system, the subsystem for powertrain codes, and whether it is
  generic or the manufacturer's. That is derived, never guessed.

Nothing is invented for a code nobody has defined. A plausible-sounding guess
about a fault is worse than a blank, because it gets acted on.

**Say what it means** records a definition for one make, and every vehicle of
that make in the shop then reads it. Readings already stored that had no
description are filled in; anything the scan tool itself printed is left alone.
Clearing the box removes your note again. The make is asked for rather than
assumed, because `P1345` is one fault to Ford and a different one to Toyota.

The page also lists everywhere that code has turned up in the shop, which is
often the more useful answer — a code that came back twice on the same vehicle
after the same repair is telling you something no definition can.

### Read an ELM327 adapter directly

**Read the car directly** uses Web Serial with an ELM327-compatible USB
adapter. It requires Chrome or Edge, HTTPS, and a device/browser combination
that exposes the serial port. Stored, pending, and permanent codes are saved as
a draft for review, just like an imported report.

Clearing codes is available, but it also resets emissions readiness monitors.
The vehicle may need a full drive cycle before an emissions test will accept
it. Clear codes after the repair, not as a way to make the warning lamp
temporarily disappear.

## Costs, reports, and exports

**Reports** contains spend by month with CSV export, a 12-month maintenance
cost forecast, cost by vehicle, inventory value at actual lot cost, and
installed parts still under warranty.

The forecast uses scheduled due dates and the cost of past related work. A
service with no usable historical cost is reported as unpriced rather than
given a guessed value. It should be read as a planning floor, not a quote.

The vehicle cost page breaks costs down into their source work and expenses.
Cost per distance is shown only when enough readings exist. Fuel is not
included. An administrator can choose whether tools count toward vehicle cost
and can assign a value to the owner's time; labor remains an estimate, never a
bill.

### Vehicle report

Open a vehicle and choose **Report** to preview the sale/history document. The
preview shows which sections have data and which gaps will remain in the
output. Costs can be included or excluded. Download the same data as PDF or
CSV.

Sensitive specs, including key and access codes, are deliberately omitted. The
preview reports how many sensitive entries were withheld.

## People, accounts, and permissions

A **person** is someone named in the shop record—for example an owner,
requester, or worker. An **account** is a login. Linking them means recorded
work is attributed to a person's display name rather than only a username.

There are three roles:

- **Administrator** — full access, including accounts, settings, integrations,
  backups, imports, exports, templates, health, reminders, and Trash.
- **Member** — day-to-day access across the shop, without administrator-only
  configuration and recovery actions.
- **Helper** — sees only specifically granted vehicles and the work,
  maintenance, inspections, and diagnostics needed for those vehicles.

Helper grants may be read-only or read/write. Helpers can read the parts
catalog and record what they fitted to an allowed vehicle, but they cannot see
shop costs, inventory quantities, suppliers, purchases, reports, or the people
directory.

Administrators create accounts and set the initial password directly; no
invitation email is sent. Each account can override locale, timezone, and
display units.

Deactivation prevents sign-in but preserves attribution and can be reversed.
The current account cannot deactivate itself, and the last administrator
cannot be demoted or deactivated. An unused account with no historical traces
can be permanently deleted; an account named on shop history must be
deactivated instead.

## Templates, checklists, and parser profiles

Administrators manage all three under **Templates, checklists and profiles**:

- **Schedule templates** create editable recurring maintenance items.
- **Inspection checklists** define areas, guidance, photo requirements,
  measurements, and thresholds.
- **Parser profiles** describe how a scan-tool report becomes fields and codes.
- **Manufacturer code lists** say what one maker's own trouble codes mean.

Each type can be imported from YAML and exported for another instance. Imports
are validated before anything is written. Installed profiles can be turned off
without removing them. Removing a template or profile never rewrites schedules,
inspections, or scan sessions already made from it. **Restore built-ins**
returns shipped definitions that were removed.

If a shared catalog address is configured, **Browse published templates**
fetches it only when the page is opened or **Check again** is pressed. Catalog
items are validated with the same rules as uploaded YAML. Installing a schedule
does not apply it to a vehicle. A catalog entry's intervals and provenance
still need human judgment; parser profiles verified against real reports are
marked separately from unproven ones.

Manufacturer code lists carry a version. When a newer one is published, the
browse screen says so and offers **Update** — it is never applied on its own,
so a definition being read today does not change underneath the reader.
Removing a code list leaves every reading already recorded untouched; only the
lookup falls back.

On a machine with no connection, **Import a manufacturer code list** takes a
`.json` file downloaded elsewhere and carried over, and
`manage.py install_code_list <make>` installs one straight from a source
checkout. Both are checked exactly as a catalog download is.

## Search, photos, documents, and OCR

Global search accepts a nickname, VIN, part number, alternate number, person's
name, work-order text, note phrase, filename, or text found inside a document.
Results are grouped by record type.

It also looks codes up. Type a trouble code (`P0420`), part of one (`P042`) if
it is half-remembered or the screen is cracked, or words from what it means
(`catalyst efficiency`) when the symptom is known and the number is not. Each
result says who defines it — the ISO/SAE standard, which is true of every
vehicle, or one manufacturer's own list. This does not need a scan report or an
imported session; it is the quick lookup for a code read straight off a tool.

Uploads are de-duplicated by content. Image derivatives and OCR run as
background jobs. By default, GPS EXIF is removed from photos because a garage
photo may otherwise disclose a home address. Administrators can change the
upload limit, GPS stripping, OCR, OCR languages, and scanned-PDF page cap under
**Settings → Photos and documents**.

OCR runs on the HomeAutoShop server with Tesseract; files are not sent to an
online recognition service. Turning OCR off leaves new files waiting so they
can be caught up when it is enabled again. **Instance health** shows whether
Tesseract and the configured languages are available, plus pending and failed
file counts.

Original media is served through authenticated application routes. A document
or photo is not public merely because someone knows a filename.

## Reminders and notifications

Administrators configure reminder channels under **Reminders**. Delivery is a
digest, not one message per item, and nothing is sent when there is nothing to
say. The page previews what would be sent now.

Supported channels are email, webhook, and browser push. Email requires SMTP;
a webhook receives JSON at the configured address. Browser push is subscribed
from the device itself. Push crosses the browser vendor's push service, so the
lock-screen message says only that something is due; the vehicle and item
remain behind the authenticated tap.

Each channel can be tested, enabled, disabled, or removed. A channel may
include all routine items or only safety and overdue items. The cooldown keeps
the same condition from being repeated every day. Offline Mode suppresses all
delivery.

## Integrations and outbound privacy

Every integration is optional. The shop continues to store and manage its own
records when all integrations are off.

### Offline Mode

**Settings → Outbound requests → Offline Mode** is the master outbound kill
switch. It stops NHTSA VIN and recall requests, plate lookup, template-catalog
fetches, reminders, and connected-service calls. Manual entry and other local
HomeAutoShop functions remain available.

Offline Mode does not mean that the browser can stop reaching the HomeAutoShop
server. It means the server will not make requests outside the allowed local
workflow. See [Using the installed web app and losing connectivity](#using-the-installed-web-app-and-losing-connectivity)
for browser/server disconnections.

### LubeLogger

LubeLogger supports a previewed, idempotent one-time import. Vehicles match by
VIN, plate, or an explicit saved pairing. A year/make/model resemblance is
shown but never merged automatically. Unmatched vehicles can be paired by hand,
left out, or created during import. Re-running the import skips records already
copied.

When configured for scheduled pull, HomeAutoShop can periodically fetch
changes; an optional mode also pushes odometer readings back. Numbers must be
provided by LubeLogger in invariant form to avoid locale-dependent money and
decimal errors.

### WrenchLedger

WrenchLedger supplies tool identity and availability for work-order job items.
HomeAutoShop caches the tool list and can request changes or rebuild the full
copy. Availability and calibration problems are warnings only. Consumable
ownership can be assigned to HomeAutoShop, split between systems, or assigned
to WrenchLedger.

### Other outbound connections

The Integrations page shows what is configured and offers connection tests or
manual sync where applicable. Service-information entries are links, not
mirrored manuals. NHTSA VIN decode needs no key. Recall and plate behavior are
described in the vehicle section above.

Integration secrets stored in the UI are encrypted and never displayed back.
They are excluded from backups and portable exports. After a restore, the
settings page lists secrets that must be entered again.

## Using the installed web app and losing connectivity

HomeAutoShop includes a web-app manifest and can be installed from a supporting
browser. The installed app has shortcuts for a new job, due maintenance, and
vehicles.

The service worker caches the application shell, recently visited pages, API
reads, static files, and thumbnails. A previously visited page may therefore
remain readable during a temporary disconnect. Full-size originals are not
cached, to avoid filling a phone with large shop photos. A page never visited
on that device falls back to an offline screen.

The application includes a per-device **Waiting to sync** inspector and server
support for compatible queued readings, notes, job-item status changes, and
work-order status changes. It keeps conflicts for a human choice instead of
merging or discarding them silently.

The current browser forms are not wired to place ordinary submissions into
that queue. Do not rely on the web UI for new data entry while the browser
cannot reach the HomeAutoShop server. Confirm the sync indicator is clear and
the saved record is visible before leaving a disconnected device. Each device
has its own local queue; one device cannot inspect another device's queue.

## Backups, trash, and recovery

### Backup versus portable export

Under **Backup** an administrator can run a recoverable instance backup, build
a portable ZIP containing plain JSON and every application-managed file,
download or delete held artifacts, and review or change automatic retention.

A backup is for restoring this HomeAutoShop instance. A portable export is for
reading the data without HomeAutoShop. Neither is safe merely because it sits
in the server's backup volume: download it and copy it to another device or
backup system.

When media lives in an external object store, the instance backup cannot reach
into that store and labels the backup as not containing photos. Back up the
object store separately.

Restore is deliberately a command-line operation because replacing a live
database from a web request risks a half-restored instance. The Backup page
prints the exact restore command for the current configuration. Integration
secrets must be re-entered afterward.

### Trash

Supported deletions are soft deletes. **Trash** groups recoverable records and
shows when each was deleted. Administrators can restore them during the
configured 30-day retention period.

Some actions are intentionally not deletions: append-only notes and readings
preserve what was recorded; stock counts write adjustments; received orders
must be un-received before deletion; used history prevents removal of the
record it explains.

## Spreadsheet import and the API

### Spreadsheet import

**Import a spreadsheet** accepts comma-, semicolon-, or tab-delimited files for
vehicles, parts, and service history. Choose the record type, upload the file,
and map its columns to HomeAutoShop fields. The page shows sample rows and
requires a dry-run preview before **Do it for real** is offered. The outcome
counts new, already-present, and skipped rows and lists the first problems.

### REST API

Interactive API documentation is at `/api/v1/docs`. Browser sessions work in
the documentation UI, and bearer-token authentication is implemented for
scripts. The current application has no self-service token-creation screen;
token records have to be provisioned through administrator or deployment
tooling.

The shipped API covers listing and reading assets and readings, appending a
reading, listing and reading work orders, appending a work-order note, changing
work-order status, global search, and batch sync for compatible clients.

Append-only creates may carry a client-generated UUID so a replay is
idempotent. Mutable sync writes use revision numbers and return a conflict
instead of silently overwriting another change.

## Common questions

### Why did my vehicle disappear from the normal list?

Sold, parted-out, and totaled vehicles are hidden by default. Choose **Include
sold**. A deleted vehicle appears in **Trash** instead.

### Why can I not complete a work order?

An asset with an odometer, hour meter, or cycle meter requires the completion
reading. A meterless asset does not. If a proposed status is not shown, the
current status does not allow that transition; move it back to planned or in
progress first.

### Why is a job short when the part is on the shelf?

An earlier open job may already claim the available stock. Open the **Parts
needed** table to see on-hand quantity, quantity committed elsewhere, quantity
on order for this job, and the remaining shortfall.

### Why can I not correct a stock quantity by editing the lot?

Quantity comes from the stock ledger. Use **Adjust** and record the physical
count; edit the lot only for its cost, location, or dates.

### Why can I not edit an order line or delete a purchase?

Received lines are the source of stock quantities and costs. Use **Undo
receiving** first. Undo is refused once some of that stock has been used.

### Why is a template interval not automatically correct for my vehicle?

Shipped and catalog schedules are generic starting points. Verify them against
the vehicle's manual and edit the applied items. Catalog provenance and
validation do not make an interval manufacturer-approved.

### Why does an empty recall screen not mean the vehicle is clear?

The integrated source is US NHTSA campaign data matched by year, make, and
model, not a completed-repair lookup for the VIN. Use the linked VIN-specific
checker and the appropriate national source for non-US vehicles.

### Why did a page work offline but saving did not?

The installed web app caches pages that were already visited. Current ordinary
forms still need a connection to the HomeAutoShop server. Browser offline
caching and the administrator's outbound **Offline Mode** are different
features.

### Where should I look when background work is stuck?

Open **Instance health**. It shows database type, media count and size, backup
age, OCR availability and language packs, pending or failed OCR files, Offline
Mode, outbound allowlist, and background-job states.
