"""
Importing a supplier order confirmation (FR-PUR-1, FR-ADM-6).

Same shape as every other import in this application, for the same reason: it
**rehearses before it writes**. A parts order carries prices and part numbers,
and a misread price that auto-commits is wrong money that looks plausible for
months (§8.3a says this about scan reports; it is no less true here).

**The preview has to lead somewhere.** A browser clears a file input after it is
submitted, and nothing can put it back — so the first version of this screen
previewed the order and then asked for the same file again to import it, which
made the safe path the annoying one. Nobody uses a preview twice; they stop
using it.

So the preview keeps the document. It is ingested exactly as the commit would
have ingested it — deduplicated by SHA-256, so previewing the same file five
times stores it once — and the review screen carries a **signed** reference to
it. Committing re-reads the stored bytes and parses them again. Two parses
rather than one, which costs milliseconds and means the confirm path re-derives
everything from the document rather than trusting a round-trip through a form.

The reference is signed rather than a bare id because it is a filename the
browser could otherwise change: without the signature, editing it would let
somebody import a document they were never shown.
"""

from __future__ import annotations

import logging
from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core import signing
from django.shortcuts import redirect, render
from django.utils.translation import gettext as _

from homeautoshop.accounts.models import require
from homeautoshop.mediafiles.models import Media, MediaLink
from homeautoshop.mediafiles.services import ingest, link

from .importers import orders, service

log = logging.getLogger(__name__)

#: A held document is only meant to survive the walk from preview to confirm.
HOLD_SALT = "purchasing.order_import"
HOLD_SECONDS = 60 * 60


def _hold(media: Media) -> str:
    return signing.dumps(str(media.pk), salt=HOLD_SALT)


def _held(token: str) -> Media | None:
    try:
        pk = signing.loads(token, salt=HOLD_SALT, max_age=HOLD_SECONDS)
    except signing.BadSignature:
        return None
    return Media.objects.filter(pk=pk).first()


#: What a line can be told to become. `part` is the default because a parts
#: supplier's document is entirely parts, and because guessing the other way
#: would silently drop things somebody meant to keep.
TREATMENTS = ("part", "tooling", "out")


def _chosen(request) -> tuple[set[int] | None, set[int] | None]:
    """`(as parts, as tooling)`, or `(None, None)` when nothing was asked yet.

    Absent on the first pass, because nothing has been shown — a preview of a
    document nobody has seen cannot sensibly default to leaving things out. On
    the confirming pass the controls are there, and an empty set of parts is a
    real answer meaning *none of it was a part*, which is why this returns
    `None` only when the field was never rendered.
    """
    if "lines_offered" not in request.POST:
        return None, None
    parts: set[int] = set()
    tooling: set[int] = set()
    for key, value in request.POST.items():
        if not key.startswith("treat_") or value not in TREATMENTS:
            continue
        index = key.removeprefix("treat_")
        if not index.isdigit():
            continue
        if value == "part":
            parts.add(int(index))
        elif value == "tooling":
            tooling.add(int(index))
    return parts, tooling


#: Room for a pallet of gaskets and not for a typo. A count is a small whole
#: number in every real case; the ceiling exists so a slipped keypress cannot
#: build a hundred thousand stock rows out of one line of somebody's receipt.
MOST_OF_ANYTHING = Decimal(10000)


def _counts(request) -> tuple[dict[int, Decimal], bool]:
    """`(count per line index, whether any could not be read)`.

    How many of the part a line turned out to be for, which is not always the
    number the vendor put on it: an Amazon two-pack of relays is `1 of:` and
    two relays. Only the count is asked for — never the money, which is the one
    thing the invoice is unambiguous about.

    Anything unreadable is dropped rather than guessed at, and the caller says
    so. Falling back to the document's own count is the conservative direction:
    it is a number somebody actually printed, and it is what this screen did
    before it asked at all.
    """
    counts: dict[int, Decimal] = {}
    misread = False
    for key, value in request.POST.items():
        if not key.startswith("count_"):
            continue
        index = key.removeprefix("count_")
        if not index.isdigit():
            continue
        try:
            count = Decimal((value or "").strip())
        except (InvalidOperation, ValueError):
            misread = True
            continue
        if not count.is_finite() or count <= 0 or count > MOST_OF_ANYTHING:
            misread = True
            continue
        counts[int(index)] = count
    return counts, misread


@login_required
def order_import(request):
    """Read a supplier order document into a purchase."""
    require(request.user, "purchase.edit")

    # Named rather than chosen. The operator knows which vendor the file
    # came from; being asked to say so before it can be read is a step that
    # exists only because the software could not be bothered to look.
    context: dict = {"formats": orders.formats()}

    if request.method != "POST":
        return render(request, "purchasing/order_import.html", context)

    commit = request.POST.get("action") == "commit"
    upload = request.FILES.get("order")
    media: Media | None = None

    if upload is None and (token := request.POST.get("held")):
        # Confirming what was previewed a moment ago. The document is already
        # stored; re-read it rather than asking for it a second time.
        media = _held(token)
        if media is None:
            messages.warning(
                request,
                _("That preview has expired. Choose the file again."),
            )
            return redirect("order_import")

    if media is None and upload is None:
        messages.warning(request, _("Choose an order confirmation to read."))
        return redirect("order_import")

    if media is None:
        # Stored on the way past, whichever button was pressed. The document is
        # the record of what was actually paid, and the only way to re-read the
        # order if this parser improves later.
        try:
            media, _created = ingest(upload, kind=Media.Kind.DOCUMENT, user=request.user)
        except Exception:  # noqa: BLE001 - reading it still works without storage
            log.exception("could not store the order confirmation")
            media = None

    try:
        source = media.file.open("rb") if media is not None else upload
        keep, as_tooling = _chosen(request)
        counts, misread = _counts(request)
        report = service.read_and_run(
            source, dry_run=not commit, user=request.user,
            keep=keep, as_tooling=as_tooling, counts=counts,
        )
    except orders.UnreadableOrder as exc:
        messages.error(
            request,
            _(
                "%(detail)s Save the order page or its emailed confirmation as a "
                "PDF and try that."
            ) % {"detail": exc},
        )
        return redirect("order_import")
    except Exception as exc:  # noqa: BLE001 - surfaced, not swallowed
        log.exception("parts order import failed")
        messages.error(
            request, _("That file could not be read: %(detail)s") % {"detail": exc}
        )
        return redirect("order_import")

    if misread:
        messages.warning(
            request,
            _(
                "Some of the quantities could not be read as numbers, so those "
                "lines kept the count the order itself gives."
            ),
        )

    context["report"] = report
    if media is not None:
        context["held"] = _hold(media)
        context["held_name"] = media.original_filename

    if not commit:
        messages.info(request, _("Nothing has been written yet. This is what it would do."))
        return render(request, "purchasing/order_import.html", context)

    if media is not None and report.purchase is not None:
        link(media, report.purchase, role=MediaLink.Role.RECEIPT)

    messages.success(
        request,
        _("Imported %(lines)s line(s): %(new)s new part(s), %(known)s already here.")
        % {
            "lines": len(report.order.charged_lines),
            "new": report.parts_created,
            "known": report.parts_matched,
        },
    )
    if report.purchase is not None:
        return redirect("purchase_detail", pk=report.purchase.pk)
    return render(request, "purchasing/order_import.html", context)
