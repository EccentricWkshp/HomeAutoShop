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

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core import signing
from django.shortcuts import redirect, render
from django.utils.translation import gettext as _

from homeautoshop.accounts.models import require
from homeautoshop.mediafiles.models import Media, MediaLink
from homeautoshop.mediafiles.services import ingest, link

from .importers import rockauto, service

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


@login_required
def order_import(request):
    """Read a RockAuto order confirmation into a purchase."""
    require(request.user, "purchase.edit")

    context: dict = {"vendor": rockauto.VENDOR_NAME}

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
        report = service.read_and_run(source, dry_run=not commit, user=request.user)
    except rockauto.NotARockAutoOrder:
        messages.error(
            request,
            _(
                "That does not look like a RockAuto order confirmation. Save the "
                "confirmation page or email as a PDF and try that."
            ),
        )
        return redirect("order_import")
    except Exception as exc:  # noqa: BLE001 - surfaced, not swallowed
        log.exception("parts order import failed")
        messages.error(
            request, _("That file could not be read: %(detail)s") % {"detail": exc}
        )
        return redirect("order_import")

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
