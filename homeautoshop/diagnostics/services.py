"""
Turning a scan into a session, and a code into work (SPEC §8.3, FR-INT-4..6).

The rule this module exists to enforce: **extraction never auto-commits**. Every
path in §8.3 — a PDF, a CSV export, an ELM327 read, a typed-in code — lands in
the same place, a draft `DiagnosticSession` that is invisible to vehicle history
until a person has looked at it. A misread VIN or odometer poisons the vehicle
record and every cost-per-distance figure derived from it, and nothing later
would reveal the mistake.
"""

from __future__ import annotations

import logging
from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext as _

from homeautoshop.assets import vin as vinlib
from homeautoshop.mediafiles.models import Media, MediaLink
from homeautoshop.mediafiles.services import ingest, link

from . import dtc, engine
from .models import (
    CodeStatus,
    DiagnosticCode,
    DiagnosticSession,
    ParseStatus,
    ParserProfile,
    ReviewStatus,
    SessionSource,
)

log = logging.getLogger(__name__)


class VinMismatch(Exception):
    """The report names a different vehicle than the one being imported into.

    Refused rather than warned. Another car's codes look exactly like this
    one's afterwards, and the vehicle history is the thing this whole
    application exists to keep true.
    """

    def __init__(self, found: str) -> None:
        self.found = found
        super().__init__(found)


def profiles_for(document: engine.Document):
    return ParserProfile.objects.filter(is_active=True)


@transaction.atomic
def session_from_upload(asset, upload, *, user=None, work_order=None) -> DiagnosticSession:
    """Ingest a report into a draft session (FR-INT-4, FR-INT-5).

    The raw file is stored permanently and the extracted text and word geometry
    are kept on the session, so a profile written a year from now can re-read
    every historical report without the operator touching anything.
    """
    document = engine.read(upload)
    profile, confidence = engine.detect(profiles_for(document), document)

    media: Media | None = None
    try:
        media, _created = ingest(
            upload,
            kind=Media.Kind.PHOTO if document.media_type == "image" else Media.Kind.DOCUMENT,
            user=user,
        )
    except Exception:  # noqa: BLE001 - a session without its original still works
        # The extracted text and words are already in hand, so an unreachable
        # object store costs the operator the original PDF, not the import.
        log.exception("could not store the raw scan report")

    session = DiagnosticSession(
        asset=asset,
        work_order=work_order,
        source=_source_for(document.media_type),
        raw_media=media,
        # The same file, on the same vehicle, already read and confirmed: this
        # is that report again, not a second one. Recorded so confirming
        # replaces it rather than filing the same test twice.
        supersedes=already_in_history(asset, media),
        extracted_text=document.text[:200_000],
        # Whatever geometry the format had. A photograph has some now, and
        # keeping it is what lets a parser written next year read a receipt
        # somebody uploaded today by its columns rather than by its text.
        extracted_words=document.pages,
        parser_profile=profile,
        parser_version=profile.version if profile else None,
        parse_status=ParseStatus.PARSED if profile else ParseStatus.UNMATCHED,
        created_by=user if getattr(user, "pk", None) else None,
    )
    session.save()
    if media is not None:
        link(media, session, role=MediaLink.Role.OTHER, caption="scan report")

    if profile is None:
        # Not a failure. An unmatched report still produces a usable session
        # through the mapping wizard (FR-INT-6) — the scaffold has to be useful
        # before a profile exists, or nobody ever writes the first one.
        session.extraction = {"_confidence": {"value": "", "confidence": 0.0, "label": ""}}
        session.save(update_fields=["extraction"])
        return session

    _apply_extraction(session, engine.apply(profile, document), asset=asset, confidence=confidence)
    return session


def _source_for(media_type: str) -> str:
    if media_type == "pdf":
        return SessionSource.PDF_REPORT
    if media_type == "image":
        return SessionSource.PHOTO
    return SessionSource.FILE_IMPORT


def _apply_extraction(
    session: DiagnosticSession, extraction: engine.Extraction, *, asset, confidence: float = 0.0
) -> DiagnosticSession:
    """Write an extraction onto a draft session, codes included.

    The VIN check happens here rather than at the view, because every entry
    path has to be subject to it — including re-parse, which would otherwise be
    a way to attach the wrong vehicle's codes a year after the upload.
    """
    found = extraction.value("vin")
    if found and asset.vin and vinlib.normalize(found) != vinlib.normalize(asset.vin):
        raise VinMismatch(found)

    session.extraction = extraction.as_dict()
    session.extraction["_match"] = {
        "value": f"{confidence:.2f}",
        "confidence": confidence,
        "label": "profile match",
    }
    session.tool = extraction.value("tool_vendor") or session.tool
    session.tool_model = extraction.value("tool_model") or session.tool_model
    session.odometer = _decimal(extraction.value("odometer")) or session.odometer
    session.odometer_unit = extraction.value("odometer_unit") or session.odometer_unit
    session.live_data = extraction.live_data
    session.readiness_monitors = extraction.readiness
    session.test_results = extraction.test_results
    if performed := extraction.value("performed_on"):
        parsed = _datetime(performed)
        if parsed is not None:
            session.performed_on = parsed
    session.parse_status = ParseStatus.PARSED
    session.save()

    _replace_codes(session, extraction.codes, make=asset.make)
    return session


def _replace_codes(session: DiagnosticSession, rows: list[dict], *, make: str = "") -> None:
    """Rewrite a draft's codes from an extraction.

    Codes are append-only, so this hard-deletes and re-creates rather than
    editing. That is sound only because a draft has never been seen by anything
    else: once confirmed, a re-parse makes a *new* session instead, and the
    original reading stays exactly as it was read.
    """
    if session.review_status != ReviewStatus.DRAFT:
        raise ValueError("codes on a confirmed session are immutable")

    session.codes.all().hard_delete()
    for row in rows:
        code = dtc.normalize(row.get("code", ""))
        parsed = dtc.parse(code)
        if parsed is None:
            continue
        # Only a description that is *somebody's* — the report's, the standard's,
        # or the operator's — is stored. The structural summary is derived from
        # the code itself, so writing it into the column would freeze today's
        # phrasing into the record and make "has anyone named this yet?"
        # unanswerable. The display layer falls back to it instead.
        described, authoritative = dtc.describe(code, make=make)
        DiagnosticCode.objects.create(
            session=session,
            code=code,
            description=(row.get("description") or (described if authoritative else ""))[:255],
            system=parsed["system"],
            is_iso_sae=parsed["is_iso_sae"],
            module=(row.get("module") or "")[:80],
            state=row.get("state") or "stored",
            state_raw=(row.get("state_raw") or "")[:120],
            freeze_frame=row.get("freeze_frame") or {},
        )


#: How a corrected value is named on the review form: which receipt, which
#: value. Flat rather than nested because an HTML form is flat, and because the
#: alternative — one form per receipt — makes "save what I changed" several
#: buttons instead of one.
CORRECTION = "tr-{index}-{key}"


def correct_results(session: DiagnosticSession, data) -> tuple[int, list[str]]:
    """Write an operator's corrections onto a draft's test results (FR-INT-4).

    **`extraction` is not touched.** That column is what the machine read, and
    it is the only way to answer "what did the tool actually say?" a year from
    now — a question that stops being answerable the moment an edit overwrites
    it. `test_results` is the corrected copy, and every value it holds records
    whether a person put it there.

    A value that fails its own shape is refused rather than stored: a reading
    has to be a number and a timestamp has to be a timestamp, because both feed
    trends that nothing downstream re-validates. The refusal comes back as a
    sentence for the operator, not as an exception.
    """
    if session.review_status != ReviewStatus.DRAFT:
        return 0, []

    results = session.test_results or []
    changed, problems, clocks = 0, [], False
    for index, result in enumerate(results):
        for value in _correctable(result):
            name = CORRECTION.format(index=index, key=value.get("key", ""))
            if name not in data:
                continue
            typed = str(data.get(name) or "").strip()
            if typed == (value.get("value") or ""):
                continue
            label = value.get("label") or value.get("key") or name
            if value.get("key") == "performed_on":
                parsed = _datetime(typed) if typed else None
                if typed and parsed is None:
                    problems.append(
                        _("%(label)s is not a date and time.") % {"label": label}
                    )
                    continue
                value["value"] = parsed.isoformat() if parsed else ""
                clocks = True
            else:
                if typed and _decimal(typed) is None:
                    problems.append(_("%(label)s has to be a number.") % {"label": label})
                    continue
                value["value"] = typed
            value["corrected"] = True
            # A person looked at the paper. That is not a guess with a
            # confidence attached to it, and showing it beside the machine's
            # own numbers as "0.93" would invite exactly the wrong comparison.
            value["confidence"] = 1.0
            changed += 1

    if not changed:
        return 0, problems

    fields = ["test_results", "updated_at"]
    session.test_results = results
    if clocks:
        # **The correction has to reach the session, or it was not a
        # correction.** A reader who retypes a misread clock and then sees the
        # scan filed under the misreading has been asked for something that was
        # thrown away, which is worse than not offering the box. The session is
        # dated the way the parser dates it — by the *latest* test on the strip,
        # because print order is not time order.
        when = _latest(results)
        if when is not None:
            session.performed_on = when
            fields.append("performed_on")
    session.save(update_fields=fields)
    return changed, problems


def _latest(results: list[dict]):
    found = []
    for result in results:
        value = result.get("performed_on")
        if isinstance(value, dict) and value.get("value"):
            parsed = _datetime(value["value"])
            if parsed is not None:
                found.append(parsed)
    return max(found) if found else None


def _correctable(result: dict) -> list[dict]:
    """Readings and the clock, and **exactly** what the review screen offers a
    box for. A verdict and a battery chemistry are words from the tester's own
    vocabulary, not numbers to retype."""
    found = [v for v in (result.get("readings") or []) if isinstance(v, dict)]
    when = result.get("performed_on")
    return found + ([when] if isinstance(when, dict) else [])


def reparse(session: DiagnosticSession, *, profile: ParserProfile | None = None) -> DiagnosticSession:
    """Read a stored report again with a better profile (FR-INT-5).

    Works from `extracted_words` where they exist and `extracted_text`
    otherwise, so it needs neither the original upload nor object storage to be
    reachable. A confirmed session is never rewritten in place — see
    :func:`_replace_codes`.

    A photograph is the exception, and re-reads its pixels where they are still
    reachable. For every other format the stored extraction *is* the report —
    a PDF's word geometry is lossless and re-extracting it would produce the
    same words — but a photograph's words are whatever OCR made of it on the
    day, so an improvement to the image pipeline is worth nothing to the
    reports already uploaded unless re-reading means re-reading the picture.
    """
    document = _document_for(session)
    chosen = profile
    match = 0.0
    if chosen is None:
        chosen, match = engine.detect(profiles_for(document), document)
    else:
        match = engine.score(chosen, document)

    if chosen is None:
        session.parse_status = ParseStatus.UNMATCHED
        session.save(update_fields=["parse_status"])
        return session

    session.parser_profile = chosen
    session.parser_version = chosen.version
    return _apply_extraction(
        session, engine.apply(chosen, document), asset=session.asset, confidence=match
    )


#: What a stored session was read from, so a re-parse offers a profile the same
#: kind of document the import did. Inferred from `extracted_words` once, which
#: was right while only PDFs had any: a photograph with word geometry then
#: re-parsed as a PDF and matched no image profile at all.
MEDIA_FOR_SOURCE = {
    SessionSource.PDF_REPORT: "pdf",
    SessionSource.PHOTO: "image",
}


def _document_for(session: DiagnosticSession) -> engine.Document:
    media_type = MEDIA_FOR_SOURCE.get(
        session.source, "pdf" if session.extracted_words else "text"
    )
    if media_type == "image" and session.raw_media_id:
        fresh = _reread_photo(session)
        if fresh is not None:
            return fresh
    return engine.Document(
        text=session.extracted_text,
        pages=session.extracted_words or [],
        media_type=media_type,
    )


def _reread_photo(session: DiagnosticSession) -> engine.Document | None:
    """OCR the original photograph again, and keep the better reading.

    Returns nothing where the file is gone, the object store is unreachable or
    OCR is switched off — all of which are ordinary, and none of which is a
    reason to refuse to re-parse. The stored text is still there.
    """
    try:
        with session.raw_media.file.open("rb") as handle:
            document = engine.read(handle.read())
    except Exception:  # noqa: BLE001
        log.exception("could not re-read the photograph for session %s", session.pk)
        return None
    if not document.text.strip():
        return None
    session.extracted_text = document.text[:200_000]
    session.extracted_words = document.pages
    return document


def session_from_codes(
    asset,
    rows: list[dict],
    *,
    user=None,
    source: str = SessionSource.MANUAL,
    tool: str = "",
    odometer=None,
) -> DiagnosticSession:
    """Build a draft from codes the operator or an adapter supplied.

    The ELM327 path (§8.3c) and the mapping wizard (FR-INT-6) both land here.
    Neither auto-commits either: a browser talking to a $12 dongle is not more
    trustworthy than a PDF.
    """
    session = DiagnosticSession.objects.create(
        asset=asset,
        source=source,
        tool=tool[:60],
        odometer=_decimal(str(odometer)) if odometer not in (None, "") else None,
        parse_status=ParseStatus.PARSED,
        created_by=user if getattr(user, "pk", None) else None,
    )
    _replace_codes(session, rows, make=asset.make)
    return session


# --------------------------------------------------------------------------
# Code to work order, and back again
# --------------------------------------------------------------------------


def recurrence_check(session: DiagnosticSession) -> int:
    """Flag codes that were addressed on this vehicle and have come back.

    The fix did not hold, which is exactly the fact you want a year later and
    exactly the fact nobody writes down. Run on confirmation, so it compares
    against history rather than against a draft that may be discarded.
    """
    flagged = 0
    previous = (
        DiagnosticCode.objects.filter(
            session__asset=session.asset,
            session__review_status=ReviewStatus.CONFIRMED,
            status=CodeStatus.ADDRESSED,
        )
        .exclude(session=session)
        # A session in the trash is not history. Joining through a foreign key
        # does not consult the related model's manager, so `session__...` sees
        # soft-deleted rows unless it is told not to — and a reading that was
        # removed from the history should not be able to say a fix did not hold.
        .filter(session__deleted_at__isnull=True)
        .filter(session__performed_on__lt=session.performed_on)
        .values_list("code", flat=True)
    )
    healed = set(previous)
    for code in session.codes.filter(status=CodeStatus.OPEN):
        if code.code in healed:
            code.status = CodeStatus.RECURRING
            code.save(update_fields=["status"])
            flagged += 1
    return flagged


@transaction.atomic
def confirm(session: DiagnosticSession, *, user=None) -> tuple[int, DiagnosticSession | None]:
    """Admit a draft to vehicle history, replacing what it re-read.

    Returns how many codes came back after being addressed, and the session
    this one displaced, if any.

    **A re-reading is not a second scan.** Re-parsing a confirmed report makes
    a new draft rather than rewriting the original — that part is right, and
    it is what lets somebody compare two profiles' answers before choosing.
    But confirming the new one used to leave both in the vehicle's history:
    the same battery test, twice, an hour apart in the list and identical in
    every other respect. So the one that was re-read is retired here, into the
    trash, where it can be brought back for thirty days.
    """
    displaced = session.supersedes
    session.confirm(user=user)
    if displaced is not None and not displaced.is_deleted:
        displaced.delete()
    else:
        displaced = None
    return recurrence_check(session), displaced


def already_in_history(asset, media) -> DiagnosticSession | None:
    """The confirmed session this exact file is already in the history as.

    Media is deduplicated by SHA-256, so the same photograph uploaded twice is
    the same `Media` row — which makes this the same question as "have I read
    this report before", asked of the bytes rather than of the operator.
    """
    if media is None:
        return None
    return (
        DiagnosticSession.objects.filter(
            asset=asset, raw_media=media, review_status=ReviewStatus.CONFIRMED
        )
        .order_by("-performed_on")
        .first()
    )


@transaction.atomic
def promote_to_work_order(code: DiagnosticCode, *, user=None, work_order=None):
    """Turn a code into work — the point of reading it (§8.3c).

    The code becomes the complaint, in the car's words rather than a summary,
    because the complaint field is what the operator reads in six months when
    deciding whether the fix held.
    """
    from homeautoshop.work.models import JobItem, WorkOrder

    session = code.session
    asset = session.asset
    # The resolved meaning, not the column. A job titled "B1695 — Please See
    # The Vehicle Service Manual." is the complaint field answering nothing.
    meaning = dtc.explain(code.code, make=asset.make, reported=code.description)
    described = meaning.text if meaning else ""

    if work_order is None:
        work_order = WorkOrder.objects.create(
            asset=asset,
            title=_("%(code)s — %(what)s") % {"code": code.code, "what": described}
            if described
            else code.code,
            complaint=_("%(code)s read on %(when)s with %(tool)s.")
            % {
                "code": code.code,
                "when": session.performed_on.date().isoformat(),
                "tool": session.tool or _("a scan tool"),
            },
            created_by=user if getattr(user, "pk", None) else None,
        )

    item = JobItem.objects.create(
        work_order=work_order,
        title=_("Diagnose %(code)s") % {"code": code.code},
        description=described,
        created_by=user if getattr(user, "pk", None) else None,
    )
    code.resolved_by_job_item = item
    code.save(update_fields=["resolved_by_job_item"])
    if session.work_order_id is None:
        session.work_order = work_order
        session.save(update_fields=["work_order"])
    return work_order


def close_codes_for(job_item) -> int:
    """Mark the codes a completed job item was raised against as addressed.

    Called when a job item is completed, so closing the work marks the code
    without anybody remembering to. A code that shows up again on a later scan
    is then flagged `recurring` by :func:`recurrence_check`, which is the whole
    reason for tracking `addressed` separately from `ignored`.
    """
    return DiagnosticCode.objects.filter(
        resolved_by_job_item=job_item, status=CodeStatus.OPEN
    ).update(status=CodeStatus.ADDRESSED)


# --------------------------------------------------------------------------
# Small conversions
# --------------------------------------------------------------------------


def _decimal(value: str | None):
    if not value:
        return None
    try:
        return Decimal(str(value).replace(",", ""))
    except (InvalidOperation, ValueError):
        return None


def _datetime(value: str):
    from django.utils.dateparse import parse_datetime

    parsed = parse_datetime(value)
    if parsed is None:
        return None
    if timezone.is_naive(parsed):
        parsed = timezone.make_aware(parsed, timezone.get_current_timezone())
    return parsed


def record_description(*, make: str, code: str, text: str) -> int:
    """Name a manufacturer-specific code for one make, everywhere it appears.

    Typed once and reused instance-wide, because `P1345` means one thing to
    every Ford ever built and something else entirely to Toyota — the make is
    what makes the answer true, not the vehicle.

    An empty `text` **removes** the note rather than storing a blank, and
    unwinds it from the readings it had been written onto. Only readings whose
    description is exactly the note being removed are touched: anything the
    scan tool itself printed is what the tool said and is not ours to erase.

    Returns how many stored readings the change reached, which is what the
    message on the screen is counting.
    """
    from .models import CodeDescription, DiagnosticCode

    canonical = dtc.normalize(code)
    text = (text or "").strip()[:255]
    # Matched case-insensitively rather than exactly. vPIC says `FORD` and a
    # person types `Ford`; the unique constraint is on the exact string, so
    # creating blind makes two rows for one make and the lookup then picks
    # whichever sorts first.
    existing = CodeDescription.objects.filter(code=canonical, make__iexact=make).first()

    if not text:
        if existing is None:
            return 0
        was = existing.description
        existing.delete()
        return DiagnosticCode.objects.filter(
            code=canonical, session__asset__make__iexact=make, description=was
        ).update(description="")

    if existing is not None:
        existing.description = text
        existing.save(update_fields=["description"])
    else:
        CodeDescription.objects.create(make=make, code=canonical, description=text)

    return DiagnosticCode.objects.filter(
        code=canonical, session__asset__make__iexact=make, description=""
    ).update(description=text)
