"""Diagnostics views (SPEC §8.3, §9.1 — the vehicle's Diagnostics tab)."""

from __future__ import annotations

import json
import logging

from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import urlencode
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy
from django.utils.translation import ngettext
from django.views.decorators.http import require_GET, require_POST

from homeautoshop.accounts.policy import visible_assets, visible_assets_for

from homeautoshop.accounts.models import require
from homeautoshop.assets import service_info
from homeautoshop.assets import vin as vinlib
from homeautoshop.assets.models import Asset
from homeautoshop.mediafiles.models import Media
from homeautoshop.work.models import WorkOrder

from . import dtc, engine, profiles as profilelib, services
from .models import (
    CodeDescription,
    CodeStatus,
    DiagnosticCode,
    DiagnosticSession,
    ParseStatus,
    ParserProfile,
    ReviewStatus,
    SessionSource,
)

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# The import queue and the review screen
# --------------------------------------------------------------------------


@login_required
def queue(request):
    """Drafts waiting to be looked at (§8.3a).

    Separate from vehicle history on purpose: a draft has been read by a
    machine and by nobody else, and until somebody confirms it, it is not part
    of what this application claims about the vehicle.
    """
    drafts = (
        visible_assets_for(
            request.user,
            DiagnosticSession.objects.filter(review_status=ReviewStatus.DRAFT),
        )
        .select_related("asset", "parser_profile")
        .order_by("-created_at")
    )
    return render(
        request,
        "diagnostics/queue.html",
        {
            "drafts": drafts,
            "profiles": ParserProfile.objects.all(),
            "assets": visible_assets(request.user),
        },
    )


@login_required
def asset_diagnostics(request, pk):
    """A vehicle's scan history, and what its codes are doing."""
    asset = get_object_or_404(Asset, pk=pk)
    require(request.user, "diagnostic.read", asset)
    sessions = (
        asset.diagnostic_sessions.filter(review_status=ReviewStatus.CONFIRMED)
        .prefetch_related("codes")
        .order_by("-performed_on")
    )
    drafts = asset.diagnostic_sessions.filter(review_status=ReviewStatus.DRAFT).order_by(
        "-created_at"
    )
    open_codes = list(
        DiagnosticCode.objects.filter(
            session__asset=asset,
            session__review_status=ReviewStatus.CONFIRMED,
            # A join does not consult the related model's manager, so without
            # this a session removed from the history goes on contributing its
            # open codes to the vehicle it was removed from.
            session__deleted_at__isnull=True,
            status__in=[CodeStatus.OPEN, CodeStatus.RECURRING],
        )
        .select_related("session")
        .order_by("-session__performed_on", "code")
    )
    _with_meaning(open_codes, asset.make)
    return render(
        request,
        "diagnostics/asset.html",
        {
            "asset": asset,
            "sessions": sessions,
            "drafts": drafts,
            "open_codes": open_codes,
            "manuals": service_info.dtc_links(asset),
        },
    )


@require_POST
@login_required
def session_import(request, pk):
    """Upload a report against a vehicle (FR-INT-4)."""
    asset = get_object_or_404(Asset, pk=pk)
    require(request.user, "asset.edit", asset)

    # Either control: the file picker, or the camera button beside it. A photo
    # of a printout is a report here — see engine.read.
    upload = request.FILES.get("report") or request.FILES.get("report_photo")
    if not upload:
        messages.warning(request, _("Choose a report or a photo to read."))
        return redirect("asset_diagnostics", pk=asset.pk)

    try:
        session = services.session_from_upload(asset, upload, user=request.user)
    except services.VinMismatch as mismatch:
        messages.error(
            request,
            _("That report is for VIN %(found)s, which is not this vehicle.")
            % {"found": vinlib.mask(mismatch.found)},
        )
        return redirect("asset_diagnostics", pk=asset.pk)
    except Exception as exc:  # noqa: BLE001 - surfaced, not swallowed
        log.exception("scan report import failed")
        messages.error(
            request,
            _("That file could not be read as a scan report: %(detail)s") % {"detail": exc},
        )
        return redirect("asset_diagnostics", pk=asset.pk)

    if session.parse_status == ParseStatus.UNMATCHED:
        messages.info(
            request,
            _("No profile recognized that report. Map the values by hand and it still counts."),
        )
        return redirect("session_map", pk=session.pk)

    messages.success(request, _("Read. Check it over before it joins the history."))
    return redirect("session_detail", pk=session.pk)


@login_required
def session_detail(request, pk):
    """The review screen: what was extracted, how sure, and from where.

    Per-field confidence is shown as a word rather than a bar. "Low" is
    actionable; "0.42" invites the reader to decide what 0.42 means, and they
    will decide wrong in whichever direction suits them.
    """
    session = get_object_or_404(
        DiagnosticSession.objects.select_related("asset", "parser_profile", "raw_media"), pk=pk
    )
    require(request.user, "diagnostic.read", session)
    rows = []
    for name, found in sorted((session.extraction or {}).items()):
        if name.startswith("_"):
            continue
        confidence = float(found.get("confidence") or 0)
        rows.append(
            {
                "name": name,
                "label": found.get("label") or name.replace("_", " "),
                "value": found.get("value", ""),
                "now": _now(session, name, found.get("value", "")),
                # A field whose value is a mapping — the module identifiers —
                # is unreadable as raw JSON in a table cell, which is what it
                # looked like. Expanded here so the review screen shows what
                # was read rather than how it was stored.
                "pairs": _pairs(found.get("value", "")),
                "confidence": confidence,
                "band": _band(confidence),
            }
        )

    codes = _with_meaning(list(session.codes.all()), session.asset.make)

    return render(
        request,
        "diagnostics/session.html",
        {
            "session": session,
            "rows": rows,
            "codes": codes,
            "results": test_results(session),
            "back": _back_to(request, session),
            "match": (session.extraction or {}).get("_match", {}),
            "profiles": ParserProfile.objects.filter(is_active=True),
            "manuals": service_info.dtc_links(session.asset),
            "form": SessionForm(instance=session),
        },
    )


def _back_to(request, session) -> dict:
    """Where the crumb at the top of a session goes.

    It went to the import queue unconditionally, which is wrong for a confirmed
    session in the plainest way available: that list holds drafts, so the way
    back led to a page that does not contain the thing you were looking at.

    A vehicle's own scan list holds both, and drafts are reachable from either
    — so a link from the vehicle says so and is taken at its word. Anything
    else falls back to the queue, which is where a draft lives.
    """
    if request.GET.get("from") == "vehicle" or not session.is_draft:
        return {
            "url": reverse("asset_diagnostics", args=[session.asset_id]),
            "label": session.asset.nickname,
        }
    return {"url": reverse("diagnostic_queue"), "label": _("Scans to check")}


def _with_meaning(codes, make: str):
    """Attach each reading's best meaning, for display only.

    Resolved here rather than written into the record: a reading holds what the
    *tool* said, and freezing today's best answer into it would make "has
    anybody actually named this?" unanswerable ever after.

    This is also where a reported description stops outranking everything else.
    It used to be read straight out of the column, so a shop that had looked
    `B1695` up and written down what it means went on being shown the tool's
    *"Please See The Vehicle Service Manual."* — the definition was recorded,
    reused, and never once displayed.
    """
    for row in codes:
        row.meaning = dtc.explain(row.code, make=make, reported=row.description)
    return codes


#: What each kind of bench test is called on screen. The parser deals in
#: identifiers because it has no gettext; the wording lives here.
#:
#: **Lazy, because these are built at import.** `gettext` resolves when it is
#: called, and at import time no language is active — so an eager call here
#: freezes English into the dictionary and every one of these captions stayed
#: English on a French page while the table headings around them translated.
TEST_KINDS = {
    "battery": gettext_lazy("Battery test"),
    "cranking": gettext_lazy("Cranking test"),
    "charging": gettext_lazy("Charging test"),
}

#: What each value is called. Falls back to the label the tester printed, so a
#: firmware that starts printing something new still shows *something* rather
#: than a slug — and shows it in the tester's own words, which is what the
#: operator is comparing against the paper.
VALUE_LABELS = {
    "verdict": gettext_lazy("Verdict"),
    "performed_on": gettext_lazy("Taken at"),
    "health": gettext_lazy("Health"),
    "charge": gettext_lazy("Charge"),
    "voltage": gettext_lazy("Voltage"),
    "measured": gettext_lazy("Measured"),
    "rated": gettext_lazy("Rated"),
    "internal_r": gettext_lazy("Internal resistance"),
    "time": gettext_lazy("Cranking time"),
    "unloaded": gettext_lazy("Unloaded"),
    "loaded": gettext_lazy("Loaded"),
    "ripple": gettext_lazy("Ripple"),
    "standard": gettext_lazy("Rating standard"),
    "type": gettext_lazy("Battery type"),
}

#: A parser reports findings as codes, because a parser that runs over a sample
#: corpus has no translation catalog to write sentences with. This is where
#: they become something to read.
VALUE_WARNINGS = {
    "unreadable": gettext_lazy("Nothing could be made of these characters."),
    "out_of_range": gettext_lazy(
        "Outside what this measurement can be, so it was not used."
    ),
    "repaired": gettext_lazy("Characters had to be repaired before this was a number."),
    "low_confidence": gettext_lazy("This was hard to read off the photograph."),
    "not_beside_its_label": gettext_lazy(
        "Printed away from its label, so it may belong elsewhere."
    ),
    "missing": gettext_lazy("Some of what this test usually prints was not found."),
    "no_timestamp": gettext_lazy("This receipt carried no time of its own."),
    "serial_disagrees": gettext_lazy(
        "Two different tester serials on one strip of paper."
    ),
    "unclassified": gettext_lazy(
        "Part of the picture looked like a report and could not be read."
    ),
}


def test_results(session) -> list[dict]:
    """Whole results from a bench tester, ready to show (§8.3a, FR-INT-4).

    A scan tool's answer is a list of codes and fits the table above. A battery
    tester's answer is a verdict, a clock and a handful of readings, printed
    once per test — and one photograph can hold two of those. So they get their
    own section, one card per receipt, rather than being flattened into the
    field list where the second test's voltage would overwrite the first's.
    """
    photo = ""
    if session.raw_media_id and session.raw_media.kind == Media.Kind.PHOTO:
        photo = session.raw_media.url_for

    out = []
    for index, result in enumerate(session.test_results or []):
        out.append(
            {
                "index": index,
                "kind": result.get("kind", ""),
                "title": TEST_KINDS.get(result.get("kind", ""), _("Test result")),
                "verdict": _value(result.get("verdict"), index, photo),
                "performed_on": _value(result.get("performed_on"), index, photo, edit=True),
                # Split by what kind of fact each is, not by where the parser
                # happened to put it. A capacity the operator keyed into the
                # tester and a capacity it measured are both numbers in CCA,
                # and showing them in one list invites the reader to take the
                # first for a reading of the battery.
                "readings": [
                    _value(v, index, photo, edit=True)
                    for v in (result.get("readings") or [])
                    if not v.get("entered")
                ],
                # An attribute is offered read-only, and that is not a design
                # preference: `correct_results` accepts readings and the clock
                # and nothing else, so a box beside a battery chemistry was a
                # box whose contents were discarded on submit. Asking for a
                # value and then ignoring it is worse than not asking.
                "entered": [
                    _value(v, index, photo, edit=v in (result.get("readings") or []))
                    for v in (result.get("attributes") or [])
                    + (result.get("readings") or [])
                    if v.get("entered")
                ],
                "warnings": [
                    VALUE_WARNINGS.get(w, w) for w in result.get("warnings") or []
                ],
                "band": _band(float(result.get("confidence") or 0)),
            }
        )
    return out


#: Where the session's own field for an extracted value lives, so the review
#: screen can show that a correction landed.
CORRECTED_TO = {
    "tool_vendor": lambda s: s.tool,
    "tool_model": lambda s: s.tool_model,
    "odometer": lambda s: "" if s.odometer is None else f"{s.odometer:f}".rstrip("0").rstrip("."),
    "odometer_unit": lambda s: s.odometer_unit,
}


def _now(session, name: str, was: str) -> str:
    """What this value is *now*, where somebody has changed it.

    The extraction is never edited — it is the record of what the machine read,
    and overwriting it would answer "what did the tool say?" with whatever
    somebody typed afterwards. But leaving the screen showing only the machine's
    answer meant a reader who retyped a misread clock, saved it, and came back
    still saw the misreading presented as the reading. The correction was kept;
    the page just never mentioned it. Asking somebody for a value and then
    appearing to ignore it is worse than not asking.
    """
    if name == "performed_on":
        if session.performed_on is None:
            return ""
        before = services._datetime(was) if was else None
        if before is not None and abs((session.performed_on - before).total_seconds()) < 1:
            return ""
        return timezone.localtime(session.performed_on).strftime("%Y-%m-%d %H:%M:%S")
    reader = CORRECTED_TO.get(name)
    if reader is None:
        return ""
    current = str(reader(session) or "")
    return current if current and current != was else ""


def _display(raw: dict) -> str:
    """What to show where there is no box to type in.

    **The reading, not the characters it was read from.** Those live in their
    own column beside the crop, which is where a reader compares them against
    the paper; putting them in the value column meant a confirmed scan showed
    `79% CS,` as the health of the battery, with the `CS,` being a smudge on
    the next line and no way to be rid of it. The reading was `79` the whole
    time.

    The exceptions are values that are not numbers. A battery chemistry is
    stored as `regular_flooded` so anything downstream can switch on it, and
    the tester's own `REGULAR FLOODED` is what a person should see — until
    somebody corrects it, at which point what they typed is the answer.
    """
    value, printed = raw.get("value", ""), raw.get("raw", "")
    if value and (raw.get("unit") or raw.get("corrected")):
        return value
    return printed or value


def _folded(text: str) -> str:
    return "".join(ch for ch in str(text).lower() if ch.isalnum())


def _value(raw, index: int, photo: str, *, edit: bool = False) -> dict | None:
    if not isinstance(raw, dict):
        return None
    key = raw.get("key", "")
    confidence = float(raw.get("confidence") or 0)
    printed = raw.get("label", "")
    label = VALUE_LABELS.get(key) or printed or key
    return {
        "key": key,
        "name": services.CORRECTION.format(index=index, key=key),
        "label": str(label),
        # Only where it adds something. `Charge CHARGE` is a word said twice;
        # `Internal resistance INTERNAL R` tells the reader which row on the
        # paper to look at. Under a translated locale every printed label
        # differs from its caption, which is exactly when it is most useful —
        # the paper is in English whatever the shop speaks.
        "printed": printed if _folded(printed) != _folded(label) else "",
        "value": raw.get("value", ""),
        "display": _display(raw),
        "unit": raw.get("unit", ""),
        "raw": raw.get("raw", ""),
        "corrected": bool(raw.get("corrected")),
        "entered": bool(raw.get("entered")),
        "editable": edit,
        # A unit is what makes a value a number here: `HEALTH` is a percentage
        # and `TYPE` is a word. It decides the keyboard a phone offers and how
        # wide the box is, which matters in a garage.
        "numeric": bool(raw.get("unit")),
        "confidence": confidence,
        "band": _band(confidence),
        "warnings": [VALUE_WARNINGS.get(w, w) for w in raw.get("warnings") or []],
        "crop": _crop(raw.get("source") or {}, photo),
    }


#: How much of the paper around a value to show with it. A number on its own
#: proves nothing; the label printed beside it is what tells the reader they
#: are looking at the right row.
CROP_PAD_X = 0.35
CROP_PAD_Y = 1.1


def _crop(source: dict, photo: str) -> dict | None:
    """The patch of the photograph a value was read from.

    The offsets are **fractions of the image itself**, applied with a
    `transform`, whose percentages resolve against the element's own box. The
    first version positioned the image with `inset-*` percentages instead,
    which resolve against the *container* — and the container is a span in a
    table cell whose width comes out of table layout, so the image rendered
    narrower than it was told to and every crop drifted upward in proportion
    to how far down the receipt it was. The timestamp, at the very bottom,
    missed the paper entirely. Nothing about the arithmetic was wrong; it was
    measured against the photograph with PIL before this was touched.

    Returns already-formatted CSS lengths rather than floats for the template
    to render, because `{{ 33.33 }}` under fr-CA is `33,33` and a stylesheet
    does not read French. The same trap the budget bars fell into (§17, R-6).

    Nothing where there is no photograph or no box — a session re-parsed from
    stored text has neither, and offering an empty frame would be worse than
    offering nothing.
    """
    box = source.get("box") or []
    page = source.get("page") or []
    if not photo or len(box) != 4 or len(page) != 2 or not all(page):
        return None

    width, height = float(page[0]), float(page[1])
    pad_x = (box[2] - box[0]) * CROP_PAD_X + 8
    pad_y = (box[3] - box[1]) * CROP_PAD_Y + 6
    left = max(0.0, box[0] - pad_x) / width
    top = max(0.0, box[1] - pad_y) / height
    right = min(width, box[2] + pad_x) / width
    bottom = min(height, box[3] + pad_y) / height
    across, down = right - left, bottom - top
    if across <= 0 or down <= 0:
        return None

    return {
        "url": photo,
        # Of the container's width; the container is then given the crop's own
        # shape, so exactly this window shows through it.
        "zoom": f"{100 / across:.2f}%",
        # Of the image's own width and height, via `transform`.
        "x": f"{-left * 100:.4f}%",
        "y": f"{-top * 100:.4f}%",
        "ratio": f"{across * width:.0f} / {down * height:.0f}",
    }


def _pairs(value) -> list[tuple[str, str]]:
    """Read a JSON object out of an extracted value, or return nothing."""
    if not isinstance(value, str) or not value.startswith("{"):
        return []
    try:
        parsed = json.loads(value)
    except ValueError:
        return []
    if not isinstance(parsed, dict):
        return []
    return [(str(k), str(v)) for k, v in parsed.items() if str(v).strip()]


def _band(confidence: float) -> str:
    if confidence >= 0.85:
        return "high"
    if confidence >= 0.6:
        return "medium"
    return "low"


class SessionForm(forms.ModelForm):
    class Meta:
        model = DiagnosticSession
        fields = ["performed_on", "tool", "tool_model", "odometer", "odometer_unit", "notes"]
        widgets = {"notes": forms.Textarea(attrs={"rows": 2})}


@require_POST
@login_required
def session_confirm(request, pk):
    """Admit a draft to vehicle history (FR-INT-4)."""
    session = get_object_or_404(DiagnosticSession, pk=pk)
    require(request.user, "asset.edit", session.asset)

    if session.review_status == ReviewStatus.CONFIRMED:
        messages.info(request, _("That scan is already in the history."))
        return redirect("session_detail", pk=session.pk)

    form = SessionForm(request.POST, instance=session)
    if not form.is_valid():
        messages.error(request, _("Check the corrected values before confirming."))
        return redirect("session_detail", pk=session.pk)
    form.save()

    # Whatever the operator typed over a tester's readings goes in with the
    # confirmation, not before it — a draft is a draft until this moment.
    _saved, problems = services.correct_results(session, request.POST)
    for problem in problems:
        messages.warning(request, problem)

    recurring, displaced = services.confirm(session, user=request.user)
    if displaced is not None:
        messages.info(
            request,
            _("This replaced the earlier reading of the same report, which is now in the trash.")
        )
    if recurring:
        messages.warning(
            request,
            _("%(n)d code(s) came back after being addressed — the fix did not hold.")
            % {"n": recurring},
        )
    messages.success(request, _("Added to the vehicle's history."))
    return redirect("asset_diagnostics", pk=session.asset_id)


@require_POST
@login_required
def session_correct(request, pk):
    """Save corrections to a bench tester's readings without confirming yet.

    Separate from confirming because they are separate decisions. Correcting a
    misread voltage is looking at the paper; confirming is admitting the whole
    session to the vehicle's history, and a screen that only offered the second
    would make every correction an act of commitment.
    """
    session = get_object_or_404(DiagnosticSession, pk=pk)
    require(request.user, "asset.edit", session.asset)

    saved, problems = services.correct_results(session, request.POST)
    for problem in problems:
        messages.warning(request, problem)
    if saved:
        messages.success(
            request,
            ngettext("%(n)d correction saved.", "%(n)d corrections saved.", saved)
            % {"n": saved},
        )
    elif not problems:
        messages.info(request, _("Nothing was changed."))
    return redirect("session_detail", pk=session.pk)


@require_POST
@login_required
def session_discard(request, pk):
    """Take a scan out — a draft, or one already in the history.

    Removal used to be offered on drafts alone, which left no way to undo a
    mistake that had already been confirmed. Since the same report can be read
    twice — from a re-parse, or from the same photograph uploaded again — that
    meant a duplicate in a vehicle's history was permanent.

    Soft, like every other delete here, and the trash lists sessions so this is
    reversible for thirty days rather than being a delete that is permanent and
    invisible at once.
    """
    session = get_object_or_404(DiagnosticSession, pk=pk)
    require(request.user, "asset.edit", session.asset)
    asset_id = session.asset_id
    was_draft = session.is_draft
    session.delete()
    messages.success(
        request,
        _("Draft discarded. It is in the trash for 30 days.")
        if was_draft
        else _("Removed from the history. It is in the trash for 30 days."),
    )
    return redirect("asset_diagnostics", pk=asset_id)


@require_POST
@login_required
def session_reparse(request, pk):
    """Read a stored report again (FR-INT-5).

    A confirmed session is copied to a new draft first. Re-parsing in place
    would rewrite a reading somebody already vouched for, and the whole reason
    the raw text is retained is to make a *comparison* possible.
    """
    session = get_object_or_404(DiagnosticSession, pk=pk)
    require(request.user, "asset.edit", session.asset)

    chosen = None
    if profile_id := request.POST.get("profile"):
        chosen = ParserProfile.objects.filter(pk=profile_id).first()

    target = session
    if session.review_status == ReviewStatus.CONFIRMED:
        target = DiagnosticSession.objects.create(
            asset=session.asset,
            work_order=session.work_order,
            performed_on=session.performed_on,
            source=session.source,
            raw_media=session.raw_media,
            extracted_text=session.extracted_text,
            extracted_words=session.extracted_words,
            notes=session.notes,
            # What it is a re-reading *of*. Without this the copy was an
            # unrelated second scan, and confirming it filed the same test in
            # the vehicle's history twice.
            supersedes=session,
            created_by=request.user,
        )

    try:
        services.reparse(target, profile=chosen)
    except services.VinMismatch as mismatch:
        messages.error(
            request,
            _("That profile read VIN %(found)s out of the report, which is not this vehicle.")
            % {"found": vinlib.mask(mismatch.found)},
        )
        return redirect("session_detail", pk=target.pk)

    if target.parse_status == ParseStatus.UNMATCHED:
        messages.warning(request, _("Still no profile matches that report."))
    elif target.pk != session.pk:
        messages.success(request, _("Re-read as a new draft, so the original stays as it was."))
    else:
        messages.success(request, _("Re-read with %(p)s.") % {"p": target.parser_profile.label})
    return redirect("session_detail", pk=target.pk)


# --------------------------------------------------------------------------
# Manual mapping wizard (FR-INT-6)
# --------------------------------------------------------------------------


@login_required
def session_map(request, pk):
    """Make an unrecognized report usable, and optionally learn from it.

    This is what keeps the pipeline useful *before* a profile exists for a
    tool — which matters, because otherwise nobody ever gets far enough to
    write the first one.
    """
    session = get_object_or_404(DiagnosticSession.objects.select_related("asset"), pk=pk)
    require(request.user, "asset.edit", session.asset)

    header, rows = engine.rows_from_csv(session.extracted_text)
    guessed = engine.sniff_columns(header, rows) if header else {}

    if request.method == "POST":
        mapping = {
            role: request.POST.get(f"map_{role}", "").strip()
            for role in ("code", "description", "state", "module")
        }
        mapping = {role: column for role, column in mapping.items() if column}

        if header and mapping.get("code"):
            codes = engine.codes_from_mapping(rows, mapping)
        else:
            codes = _codes_from_text(request.POST.get("codes", ""))

        if not codes:
            messages.error(
                request,
                _("Nothing in that mapping looks like a trouble code. Check the column choice."),
            )
            return redirect("session_map", pk=session.pk)

        services._replace_codes(session, codes, make=session.asset.make)
        session.parse_status = ParseStatus.PARSED
        session.tool = request.POST.get("tool", "")[:60] or session.tool
        session.save(update_fields=["parse_status", "tool"])

        if request.POST.get("save_profile") and header and mapping.get("code"):
            _profile_from_mapping(request, session, header, mapping)

        messages.success(request, _("Mapped. Check it over before it joins the history."))
        return redirect("session_detail", pk=session.pk)

    # The choices are assembled here rather than in the template: which column
    # is pre-selected is a comparison, and a template that can only test for
    # truth ends up either silently selecting nothing or growing a filter.
    roles = [
        {
            "role": role,
            "label": label,
            "columns": [
                {"name": column, "selected": guessed.get(role) == column} for column in header
            ],
        }
        for role, label in (
            ("code", _("Code")),
            ("description", _("Description")),
            ("state", _("State")),
            ("module", _("Module")),
        )
    ]
    return render(
        request,
        "diagnostics/map.html",
        {
            "session": session,
            "header": header,
            "roles": roles,
            "sample": [[row.get(column, "") for column in header] for row in rows[:5]],
            "excerpt": session.extracted_text[:4000],
        },
    )


def _codes_from_text(blob: str) -> list[dict]:
    """Take codes out of whatever the operator pasted.

    Deliberately forgiving about separators and deliberately strict about what
    counts as a code: anything J2012 does not recognize is dropped rather than
    stored as a code nobody can look up.
    """
    found = []
    for chunk in (blob or "").replace(",", "\n").splitlines():
        parts = chunk.strip().split(None, 1)
        if not parts:
            continue
        if not dtc.parse(parts[0]):
            continue
        found.append(
            {
                "code": dtc.normalize(parts[0]),
                "description": parts[1].strip() if len(parts) > 1 else "",
            }
        )
    return found


def _profile_from_mapping(request, session, header: list[str], mapping: dict) -> None:
    """Learn-from-example: save the operator's mapping as a reusable profile."""
    name = request.POST.get("profile_name", "").strip() or _("%(tool)s import") % {
        "tool": session.tool or _("Untitled tool")
    }
    if ParserProfile.all_objects.filter(name=name, version=1).exists():
        messages.info(
            request, _("A profile called %(name)s already exists, so none was saved.") % {"name": name}
        )
        return
    ParserProfile.objects.create(
        name=name[:120],
        tool_vendor=(session.tool or "")[:60],
        media_type="csv",
        source="user",
        fingerprint={
            "threshold": 0.6,
            # The header row is the fingerprint. It is what actually
            # distinguishes one tool's export from another's, and it is the one
            # signal the operator has already confirmed by mapping it.
            "signals": [
                {"kind": "doc_text", "pattern": _escape(column), "weight": 1.0 / len(header)}
                for column in header
                if column.strip()
            ],
        },
        table_extractor={
            "row_pattern": r"([PBCU][0-9A-F]{4}(?:-[0-9A-F]{2})?)[\s,:.-]+(.*)",
            "columns": [
                {"role": "code", "group": 1, "validate": "dtc_format"},
                {"role": "description", "group": 2},
            ],
        },
        notes=str(
            _("Learned from a mapping on %(when)s. Columns: %(cols)s")
            % {"when": session.created_at.date().isoformat(), "cols": ", ".join(mapping.values())}
        ),
        created_by=request.user,
    )
    messages.success(request, _("Saved as a profile, so the next one of these reads itself."))


def _escape(value: str) -> str:
    import re as _re

    return _re.escape(value.strip())


# --------------------------------------------------------------------------
# Codes
# --------------------------------------------------------------------------


@require_POST
@login_required
def code_promote(request, pk):
    """Code → work order (§8.3c). The reason for reading codes at all."""
    code = get_object_or_404(DiagnosticCode.objects.select_related("session__asset"), pk=pk)
    require(request.user, "asset.edit", code.session.asset)

    if code.session.review_status != ReviewStatus.CONFIRMED:
        messages.error(request, _("Confirm the scan first — a draft is not history yet."))
        return redirect("session_detail", pk=code.session_id)

    existing = None
    if wanted := request.POST.get("work_order"):
        existing = WorkOrder.objects.filter(pk=wanted, asset=code.session.asset).first()

    work_order = services.promote_to_work_order(code, user=request.user, work_order=existing)
    messages.success(
        request,
        _("%(code)s is now on %(number)s.") % {"code": code.code, "number": work_order.number},
    )
    return redirect("work_order_detail", pk=work_order.pk)


@require_POST
@login_required
def code_status(request, pk):
    """Record the operator's verdict on a code — the one mutable field."""
    code = get_object_or_404(DiagnosticCode.objects.select_related("session__asset"), pk=pk)
    require(request.user, "asset.edit", code.session.asset)

    wanted = request.POST.get("status", "")
    if wanted not in CodeStatus.values:
        messages.error(request, _("That is not a status a code can have."))
        return redirect("asset_diagnostics", pk=code.session.asset_id)

    code.status = wanted
    code.save(update_fields=["status"])
    messages.success(
        request,
        _("%(code)s marked %(status)s.")
        % {"code": code.code, "status": code.get_status_display()},
    )
    return redirect("asset_diagnostics", pk=code.session.asset_id)


@require_POST
@login_required
def code_describe(request, pk):
    """Name a manufacturer-specific code once, for every vehicle of that make.

    Scoped by make because `P1345` is a different fault to GM and to Toyota,
    and a dictionary that pretends otherwise is worse than an empty one.
    """
    code = get_object_or_404(DiagnosticCode.objects.select_related("session__asset"), pk=pk)
    require(request.user, "asset.edit", code.session.asset)

    text = request.POST.get("description", "").strip()[:255]
    if not text:
        messages.warning(request, _("Nothing to record."))
        return redirect("session_detail", pk=code.session_id)

    make = code.session.asset.make or ""
    services.record_description(make=make, code=code.code, text=text)
    code.refresh_from_db(fields=["description"])
    if not code.description:
        # This reading already carried the tool's own wording, which the
        # service leaves alone. The note is still recorded for every other one.
        code.description = text
        code.save(update_fields=["description"])
    messages.success(
        request,
        _("Recorded. Every %(make)s in the shop will show that for %(code)s.")
        % {"make": make or _("vehicle"), "code": code.code},
    )
    return redirect("session_detail", pk=code.session_id)


# --------------------------------------------------------------------------
# One code, on its own page (§8.3c)
# --------------------------------------------------------------------------


@login_required
def code_reference(request, code):
    """What a single trouble code means, and a place to say so if nobody knows.

    A code was a dead end wherever it appeared: five characters of monospace,
    sometimes with a definition beside it and sometimes with an em dash. If the
    dash was there, the only way to fix it was to find a *draft* session that
    still had the inline form on it — so a code read last year could never be
    named at all.

    It is a page rather than another inline box because the answer has parts:
    what the shape says, who says what the fault is, and where in this shop the
    code has actually turned up. The last of those is often the real answer —
    "this came back twice on the truck after the same repair" is not something
    a definition can tell you.
    """
    parsed = dtc.parse(code)
    if parsed is None:
        raise Http404("not a trouble code")
    canonical = parsed["code"]

    # The make is what makes an answer true, so it comes from the URL and the
    # page says which make it is answering for.
    make = (request.GET.get("make") or "").strip()
    seen = list(
        DiagnosticCode.objects.filter(
            code=canonical,
            session__asset__in=visible_assets(request.user, Asset.objects.all()),
            session__deleted_at__isnull=True,
        )
        .select_related("session", "session__asset")
        .order_by("-session__performed_on")[:25]
    )
    if not make and seen:
        make = seen[0].session.asset.make or ""

    # The most recent reading's own wording joins the ranking, so this page's
    # headline is the same answer the tables show rather than a second opinion.
    definition = dtc.explain(
        canonical, make=make, reported=seen[0].description if seen else ""
    )
    published = dtc.code_list_for(make)
    on_vehicle = seen[0].session.asset if seen else None

    # A blank is only honest if it is true. Where this make has no answer,
    # another installed list may still define the same code — `C1281` is a
    # Ford code and a Suzuki code and they are different faults — and saying
    # "nothing defines this" while a list on this very instance defines it is
    # the same false blank that a search link dropping `?make=` produced.
    # Offered rather than chosen: the reader knows which badge is in the shop.
    elsewhere = []
    if not definition.is_known:
        elsewhere = [
            other
            for other in dtc.answers_for(canonical)
            if other.make and other.make.casefold() != make.casefold()
        ]

    return render(
        request,
        "diagnostics/code.html",
        {
            "code": canonical,
            "parsed": parsed,
            "make": make,
            "definition": definition,
            "elsewhere": elsewhere,
            "structural": dtc.structural(canonical),
            "published": published,
            "seen": seen,
            "manuals": service_info.dtc_links(on_vehicle) if on_vehicle else [],
            "makes_with_lists": dtc.makes_with_lists(),
        },
    )


@require_POST
@login_required
def code_define(request, code):
    """Say what a code means on one make, from anywhere the code appears.

    Distinct from `code_describe`, which names the code on a reading you are
    looking at. This one needs no reading at all, which is the point: a code
    you have not scanned yet, or one whose only session is two years old, is
    still a code you can write down what you learned about.
    """
    parsed = dtc.parse(code)
    if parsed is None:
        raise Http404("not a trouble code")

    # No vehicle in hand, so no per-vehicle grant can authorise it: this writes
    # a dictionary the whole shop reads. `helper_can` refuses a write with no
    # resource, which is the behaviour wanted here rather than an exception.
    require(request.user, "asset.edit")

    make = (request.POST.get("make") or "").strip()
    text = (request.POST.get("description") or "").strip()[:255]
    if not make:
        messages.error(
            request,
            _("A definition needs a make — a code means different things to "
              "different manufacturers."),
        )
        return redirect("code_reference", code=parsed["code"])

    touched = services.record_description(make=make, code=parsed["code"], text=text)
    if text:
        messages.success(
            request,
            _("Recorded. Every %(make)s in the shop reads that for %(code)s.")
            % {"make": make, "code": parsed["code"]},
        )
    else:
        messages.success(
            request,
            _("Removed. %(code)s falls back to whatever else is known about it.")
            % {"code": parsed["code"]},
        )
    if touched:
        messages.info(
            request,
            ngettext(
                "%(n)d stored reading updated.", "%(n)d stored readings updated.", touched
            )
            % {"n": touched},
        )
    where = reverse("code_reference", args=[parsed["code"]])
    return redirect(f"{where}?{urlencode({'make': make})}")


# --------------------------------------------------------------------------
# ELM327 (§8.3c) — the browser talks to the adapter, not the server
# --------------------------------------------------------------------------


@login_required
def elm327(request, pk):
    """The live-read screen.

    The container has no USB and no Bluetooth, and the phone in the garage is
    the device actually near the car — so the *browser* drives the adapter and
    posts what it read. That also makes the requirements concrete rather than
    mysterious: a secure context and a Chromium browser, both stated on the
    page rather than silently graying out a button.
    """
    from django.middleware.csrf import get_token

    asset = get_object_or_404(Asset, pk=pk)
    return render(
        request,
        "diagnostics/elm327.html",
        {
            "asset": asset,
            # Handed to the script as JSON rather than interpolated into it, so
            # the strings stay in the message catalog (§5.6) and the script
            # itself is a static file that caches.
            "config": {
                "captureUrl": reverse("elm_capture", args=[asset.pk]),
                "csrf": get_token(request),
                "baudRate": 38400,
            },
            "strings": {
                "ready": _("Ready. Plug the adapter in and press Read codes."),
                "insecure": _(
                    "This page is not on HTTPS, so the browser will not open a serial port. "
                    "See the install guide for turning on TLS."
                ),
                "noSerial": _(
                    "This browser has no Web Serial. Chrome or Edge on desktop or Android "
                    "will do it; Safari and Firefox will not."
                ),
                "notConnected": _("No adapter was chosen."),
                "connected": _("Connected. Reading…"),
                "stored": _("stored"),
                "pending": _("pending"),
                "permanent": _("permanent"),
                "noCodes": _("Read complete — no codes found."),
                "done": _("Read. Keep it and check it over."),
                "readFailed": _("The adapter stopped answering."),
                "saveFailed": _("Could not save that read."),
                "cleared": _("Codes cleared. Readiness monitors are now unset."),
                "clearWarning": _(
                    "Clear all codes on %(vehicle)s?\n\n"
                    "This also resets the readiness monitors, and a car with unset monitors "
                    "fails an emissions test until it has been driven enough to set them again."
                )
                % {"vehicle": asset.nickname},
            },
        },
    )


@require_POST
@login_required
def elm_capture(request, pk):
    """Record what the browser read off the adapter.

    Lands in the same draft queue as a PDF. A $12 dongle and a bad connector
    misread codes at least as often as a PDF parser does.
    """
    asset = get_object_or_404(Asset, pk=pk)
    require(request.user, "asset.edit", asset)

    try:
        payload = json.loads(request.body.decode("utf-8") or "{}")
    except ValueError:
        return JsonResponse({"error": "malformed"}, status=400)

    rows = [
        {"code": entry.get("code", ""), "state": entry.get("state", "stored")}
        for entry in payload.get("codes") or []
        if isinstance(entry, dict)
    ]
    session = services.session_from_codes(
        asset,
        rows,
        user=request.user,
        source=SessionSource.ELM327,
        tool=str(payload.get("adapter", "ELM327"))[:60],
        odometer=payload.get("odometer"),
    )
    session.readiness_monitors = payload.get("readiness") or {}
    session.live_data = payload.get("live_data") or []
    session.save(update_fields=["readiness_monitors", "live_data"])

    return JsonResponse(
        {
            "session": str(session.pk),
            "url": f"/diagnostics/sessions/{session.pk}/",
            "codes": session.codes.count(),
        }
    )


# --------------------------------------------------------------------------
# Profiles (FR-INT-7)
# --------------------------------------------------------------------------


@login_required
def profile_list(request):
    """Kept as an address, not as a page.

    Parser profiles used to have a screen of their own here, reachable only
    from the scan queue — which meant the one page listing everything a shop
    imports, exports and shares listed two of the three kinds, while its own
    copy already said it covered "scan-tool profiles". A profile is the same
    sort of thing as a schedule and a checklist: YAML, an author, a source,
    installed from the same catalog by the same validator. It belongs beside
    them, so it is now listed there.

    This redirect stays because the URL was linked and bookmarkable, and a
    dead link is a worse answer than a hop.
    """
    require(request.user, "integration.manage")
    return redirect(reverse("template_list") + "#profiles")


@require_GET
@login_required
def profile_export(request, pk):
    profile = get_object_or_404(ParserProfile, pk=pk)
    body = profilelib.to_yaml(profile)
    response = HttpResponse(body, content_type="application/yaml; charset=utf-8")
    slug = profile.name.lower().replace(" ", "-").replace("/", "-")
    response["Content-Disposition"] = f'attachment; filename="{slug}-v{profile.version}.yaml"'
    return response


@require_POST
@login_required
def profile_import(request):
    require(request.user, "integration.manage")
    from homeautoshop.core.imports import NothingToImport, text_from

    try:
        text = text_from(request, "profile")
    except NothingToImport as exc:
        messages.warning(request, str(exc))
        return redirect("template_list")

    try:
        profile = profilelib.from_yaml(text)
    except profilelib.ProfileInvalid as exc:
        messages.error(request, _("That profile was refused: %(detail)s") % {"detail": exc})
        return redirect("template_list")

    if ParserProfile.all_objects.filter(name=profile.name, version=profile.version).exists():
        messages.error(
            request,
            _("%(name)s v%(v)d is already here. Bump the version to import a revision.")
            % {"name": profile.name, "v": profile.version},
        )
        return redirect("template_list")

    profile.created_by = request.user
    profile.save()
    messages.success(request, _("Imported %(name)s.") % {"name": profile.label})
    return redirect("template_list")


@require_POST
@login_required
def profile_delete(request, pk):
    """Remove a parser profile (FR-INT-7).

    Switching one off was the only way to say no to it, which is the same gap
    a scheduled item had: a profile that is wrong, superseded or simply for a
    tool nobody here owns stayed in the list forever, and the catalog could
    install one that nothing could take away.

    Nothing that was already read is disturbed. `DiagnosticSession` points at
    its profile with `SET_NULL` and keeps `parser_version` in a column of its
    own, so a session goes on saying which version read it after the profile
    is gone. Soft, like every other delete here, so it is in the trash for
    thirty days — and a shipped profile can also be put back at any time from
    **Restore shipped templates**, which matters because removing the built-in
    that reads XTOOL D8 reports would otherwise be a one-way door.
    """
    require(request.user, "integration.manage")
    profile = get_object_or_404(ParserProfile, pk=pk)
    name = profile.label
    profile.delete()
    messages.success(
        request,
        _("Removed %(name)s. Scans already read with it keep their results.")
        % {"name": name},
    )
    return redirect(request.POST.get("back") or "template_list")


@require_POST
@login_required
def profile_toggle(request, pk):
    require(request.user, "integration.manage")
    profile = get_object_or_404(ParserProfile, pk=pk)
    profile.is_active = not profile.is_active
    profile.save(update_fields=["is_active"])
    messages.success(
        request,
        _("%(name)s is now %(state)s.")
        % {
            "name": profile.label,
            "state": _("in use") if profile.is_active else _("switched off"),
        },
    )
    return redirect("template_list")
