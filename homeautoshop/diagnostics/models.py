"""
Diagnostics: sessions, codes, and the profiles that read them (SPEC §6.2, §8.3).

Three entities, and the split between them is the whole design:

* A **`DiagnosticSession`** is one visit to the car with a tool. It holds the
  raw report forever (FR-INT-5) so a better profile can re-read it later, and
  it stays invisible to vehicle history until a person confirms it (FR-INT-4).
* A **`DiagnosticCode`** is append-only, because a code is something the car
  said at a moment in time. It cannot be edited into being right. `status` is
  the single mutable field, and it is a judgment about the code, not the code.
* A **`ParserProfile`** is how a report becomes fields. Adding a tool is meant
  to be authoring one of these rather than writing code (FR-INT-7).
"""

from __future__ import annotations

from django.conf import settings
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from homeautoshop.core.models import AppendOnlyModel, BaseModel, RevisionedModel


class SessionSource(models.TextChoices):
    PDF_REPORT = "pdf_report", _("PDF report")
    FILE_IMPORT = "file_import", _("Structured file")
    #: A photograph of a printout, read by OCR. Named apart from the others
    #: because it is the one whose values were *guessed from pixels* — the
    #: review screen has more reason to doubt it than a parsed CSV.
    PHOTO = "photo", _("Photo of a printout")
    ELM327 = "elm327", _("ELM327 adapter")
    MANUAL = "manual", _("Typed in")


class ParseStatus(models.TextChoices):
    PENDING = "pending", _("Not read yet")
    PARSED = "parsed", _("Read")
    UNMATCHED = "unmatched", _("No profile matched")
    FAILED = "failed", _("Could not be read")


class ReviewStatus(models.TextChoices):
    DRAFT = "draft", _("Draft")
    CONFIRMED = "confirmed", _("Confirmed")


class MediaType(models.TextChoices):
    PDF = "pdf", _("PDF")
    CSV = "csv", _("CSV")
    JSON = "json", _("JSON")
    TEXT = "text", _("Plain text")
    IMAGE = "image", _("Photo")


class Reports(models.TextChoices):
    """What a tool's report can contain, declared by its profile.

    A battery tester does not report trouble codes. Neither does a charging
    tester, a compression tester or an alignment rack — and a screen that shows
    one "0 codes" is not reporting a result, it is reporting the absence of a
    thing that was never going to be there. Guessing from an empty list would be
    wrong the other way just as often: a scan tool that found nothing is the
    best possible outcome and needs saying out loud.

    So the profile declares it. Adding a tool stays a data change, which is
    §8.3a's whole rule, and the answer comes from whoever wrote the profile and
    has actually seen the tool's output.
    """

    CODES = "codes", _("Trouble codes")
    LIVE_DATA = "live_data", _("Live data")
    READINESS = "readiness", _("Readiness monitors")
    TEST_RESULTS = "test_results", _("Bench test results")


class ProfileSource(models.TextChoices):
    BUILTIN = "builtin", _("Shipped")
    USER = "user", _("Written here")
    IMPORTED = "imported", _("Imported")


class ParserProfile(BaseModel):
    """Declarative extraction rules for one tool's report format (§8.3a).

    `engine` is the honest part. The spec's intent is that a profile is data —
    and for a report whose labels and values line up in extracted text, it is:
    `declarative` runs the fingerprint, field extractors and table extractor in
    `engine.py`, with no code involved and no deploy.

    Some formats defeat that. The XTOOL D8 separates a module banner from a
    section heading by **color**, which no regex over extracted text can see,
    and prints a cell's first line above its own row. Pretending otherwise
    would mean either a profile language that is really a programming language,
    or a parser that quietly mis-associates. So a profile may instead name a
    built-in code parser — and the session still records which profile and
    which version read it, which is what re-parse and regression triage
    actually need.
    """

    name = models.CharField(max_length=120)
    tool_vendor = models.CharField(max_length=60, blank=True)
    tool_model = models.CharField(max_length=60, blank=True)
    version = models.PositiveIntegerField(default=1)
    media_type = models.CharField(max_length=8, choices=MediaType.choices, default=MediaType.PDF)

    #: Empty for a declarative profile; otherwise a key in `engine.BUILTINS`.
    engine = models.CharField(
        max_length=40,
        blank=True,
        help_text=_("Name of a built-in parser, for formats that regex cannot read."),
    )

    #: Which of :class:`Reports` this tool's reports contain. **Empty means
    #: undeclared, not "nothing"** — every profile written before this existed
    #: has an empty list, and a screen that read that as "reports nothing" would
    #: hide the codes those profiles do read. Undeclared falls back to showing
    #: whatever the session turns out to hold.
    reports = models.JSONField(default=list, blank=True)

    fingerprint = models.JSONField(default=dict, blank=True)
    field_extractors = models.JSONField(default=dict, blank=True)
    table_extractor = models.JSONField(default=dict, blank=True)
    #: The data-stream table, in the same shape as `table_extractor` but keyed
    #: on a reading's name rather than a code. `DiagnosticSession.live_data`
    #: and the Reading/Value/Min/Max table on the session screen both predate
    #: this - only the built-in D8 parser could fill them, so a THINKCAR
    #: data-stream report holding 159 readings and no fault codes imported as
    #: an empty session.
    live_data_extractor = models.JSONField(default=dict, blank=True)

    source = models.CharField(
        max_length=10, choices=ProfileSource.choices, default=ProfileSource.USER
    )
    is_active = models.BooleanField(default=True)
    author = models.CharField(
        max_length=80,
        blank=True,
        help_text=_("Who published this profile."),
    )
    verified_against = models.JSONField(
        default=list,
        blank=True,
        help_text=_(
            "Captured reports this profile was run against and read correctly. "
            "Checked when published, so it is a fact rather than a claim — and "
            "several rather than one, because a single report proves only that "
            "the profile fits that report."
        ),
    )
    notes = models.TextField(blank=True)

    class Meta(BaseModel.Meta):
        ordering = ["tool_vendor", "tool_model", "name"]
        constraints = [
            models.UniqueConstraint(
                fields=["name", "version"],
                condition=models.Q(deleted_at__isnull=True),
                name="unique_parser_profile_version",
            )
        ]

    def __str__(self) -> str:
        return f"{self.name} v{self.version}"

    @property
    def label(self) -> str:
        return f"{self.name} v{self.version}"

    def could_report(self, what: str) -> bool:
        """Whether a report from this tool can contain `what` at all.

        True where the profile has not said, because an undeclared profile
        is one nobody has answered the question for — and hiding a section
        on the strength of a field that was never filled in would lose real
        content.
        """
        return not self.reports or what in self.reports


class DiagnosticSession(RevisionedModel):
    """One scan of one vehicle, from any of the three paths in §8.3.

    Mutable — the operator corrects a misread odometer on the review screen —
    but the codes hanging off it are not.
    """

    asset = models.ForeignKey(
        "assets.Asset", on_delete=models.CASCADE, related_name="diagnostic_sessions"
    )
    work_order = models.ForeignKey(
        "work.WorkOrder",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="diagnostic_sessions",
    )
    performed_on = models.DateTimeField(default=timezone.now)
    tool = models.CharField(max_length=60, blank=True)
    tool_model = models.CharField(max_length=60, blank=True)
    odometer = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    odometer_unit = models.CharField(max_length=8, blank=True)

    source = models.CharField(
        max_length=16, choices=SessionSource.choices, default=SessionSource.PDF_REPORT
    )
    raw_media = models.ForeignKey(
        "mediafiles.Media",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="diagnostic_sessions",
        help_text=_("The report as uploaded. Kept forever so it can be re-read."),
    )
    #: Retained so a profile improvement can re-parse without the operator
    #: re-uploading anything (FR-INT-5).
    extracted_text = models.TextField(blank=True)
    #: Word geometry, where the source had any. A positional parser needs it,
    #: and re-parsing a PDF from flattened text would have thrown it away.
    extracted_words = models.JSONField(default=list, blank=True)

    #: The session in the history this one is a **re-reading of**, where it is
    #: one. Confirming a session that supersedes another retires that one.
    #:
    #: Without this a re-read was a second scan. Re-parsing a confirmed report
    #: copies it to a new draft — deliberately, so a reading somebody vouched
    #: for is never rewritten under them — and nothing recorded that the copy
    #: was the *same report*, so confirming it put the same test in the
    #: vehicle's history twice with no way to take either out.
    supersedes = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="superseded_by",
    )

    parser_profile = models.ForeignKey(
        ParserProfile, null=True, blank=True, on_delete=models.SET_NULL, related_name="sessions"
    )
    parser_version = models.PositiveIntegerField(null=True, blank=True)
    parse_status = models.CharField(
        max_length=12, choices=ParseStatus.choices, default=ParseStatus.PENDING
    )
    parse_error = models.CharField(max_length=255, blank=True)

    #: ``{field: {"value": ..., "confidence": 0.0-1.0, "page": n, "label": ...}}``
    extraction = models.JSONField(default=dict, blank=True)
    review_status = models.CharField(
        max_length=10, choices=ReviewStatus.choices, default=ReviewStatus.DRAFT
    )
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)
    readiness_monitors = models.JSONField(default=dict, blank=True)
    live_data = models.JSONField(default=list, blank=True)
    #: Whole results from a bench tester, one per receipt — verdict, clock,
    #: readings, each with its own confidence, bounding box and warnings. See
    #: `scantools/report.py` for the shape and why it is not flattened into
    #: `extraction`.
    #:
    #: This is the **operator-correctable** copy. `extraction` stays exactly as
    #: the machine read it, forever, because the whole value of retaining a
    #: reading is being able to ask later what the tool actually said — and an
    #: edit that overwrote it would answer that question with the edit.
    test_results = models.JSONField(default=list, blank=True)
    notes = models.TextField(blank=True)

    class Meta(RevisionedModel.Meta):
        ordering = ["-performed_on"]
        indexes = [
            models.Index(fields=["asset", "-performed_on"]),
            models.Index(fields=["review_status", "-created_at"]),
        ]

    def __str__(self) -> str:
        return f"{self.tool or 'Scan'} - {self.performed_on:%Y-%m-%d}"

    @property
    def is_draft(self) -> bool:
        return self.review_status == ReviewStatus.DRAFT

    @property
    def shows_codes(self) -> bool:
        """Whether this scan's screens should have a trouble-code section.

        `0 codes` beside a battery test is not a result — it is the absence of
        a thing that was never going to be there. Where a scan *tool* found
        none, that is the best possible outcome and needs saying out loud, so
        this asks the profile rather than inferring from an empty list.
        """
        if self.parser_profile_id and not self.parser_profile.could_report(Reports.CODES):
            return False
        return True

    @property
    def headline(self) -> str:
        """One line saying what this scan found, for a list of scans."""
        from django.utils.translation import ngettext

        count = self.codes.count()
        if count or self.shows_codes:
            return ngettext("%(n)d code", "%(n)d codes", count) % {"n": count}
        verdicts = [
            (result.get("verdict") or {}).get("raw", "")
            for result in self.test_results or []
        ]
        return " · ".join(v for v in verdicts if v)

    def confirm(self, user=None) -> None:
        """Admit the session to vehicle history (§8.3a).

        Until this happens the session lives in the import queue and appears
        nowhere else — not in the timeline, not in search, not in a report.
        """
        self.review_status = ReviewStatus.CONFIRMED
        self.reviewed_by = user if getattr(user, "pk", None) else None
        self.reviewed_at = timezone.now()
        self.save(update_fields=["review_status", "reviewed_by", "reviewed_at"])


class CodeState(models.TextChoices):
    STORED = "stored", _("Stored")
    PENDING = "pending", _("Pending")
    PERMANENT = "permanent", _("Permanent")
    HISTORY = "history", _("History")


class CodeStatus(models.TextChoices):
    OPEN = "open", _("Open")
    ADDRESSED = "addressed", _("Addressed")
    RECURRING = "recurring", _("Came back")
    IGNORED = "ignored", _("Ignored")


class DiagnosticCode(AppendOnlyModel):
    """One trouble code as read. Append-only except for `status` (§6.2).

    `status` is the operator's verdict on the code — did we fix it, did it come
    back, are we living with it — so it is a server-writable field on a row
    that is otherwise immutable. The reading itself never changes.
    """

    server_writable_fields = frozenset(
        {"status", "resolved_by_job_item", "resolved_by_job_item_id", "description"}
    )

    session = models.ForeignKey(DiagnosticSession, on_delete=models.CASCADE, related_name="codes")
    code = models.CharField(max_length=16, db_index=True)
    description = models.CharField(max_length=255, blank=True)
    system = models.CharField(max_length=1, blank=True)
    is_iso_sae = models.BooleanField(default=True)
    module = models.CharField(max_length=80, blank=True)
    state = models.CharField(max_length=12, choices=CodeState.choices, default=CodeState.STORED)
    #: Exactly what the tool printed. Normalizing into `state` is lossy and the
    #: vocabulary varies by manufacturer, so the original stays for review.
    state_raw = models.CharField(max_length=120, blank=True)
    freeze_frame = models.JSONField(default=dict, blank=True)

    status = models.CharField(max_length=12, choices=CodeStatus.choices, default=CodeStatus.OPEN)
    resolved_by_job_item = models.ForeignKey(
        "work.JobItem",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="resolved_codes",
    )

    class Meta(AppendOnlyModel.Meta):
        ordering = ["code"]
        indexes = [models.Index(fields=["code", "status"])]

    def __str__(self) -> str:
        return f"{self.code} {self.description}"[:80]


class CodeDescription(BaseModel):
    """An operator's own words for a manufacturer-specific code (§8.3c).

    No free comprehensive source exists for P1xxx and friends, so the operator
    types it once and every vehicle of the same make reuses it. Scoped by make
    because `P1345` means different things to GM and to Toyota.
    """

    make = models.CharField(max_length=60, blank=True)
    code = models.CharField(max_length=16)
    description = models.CharField(max_length=255)

    class Meta(BaseModel.Meta):
        ordering = ["make", "code"]
        constraints = [
            models.UniqueConstraint(
                fields=["make", "code"],
                condition=models.Q(deleted_at__isnull=True),
                name="unique_code_description",
            )
        ]

    def __str__(self) -> str:
        return f"{self.make} {self.code}"


class InstalledCodeList(BaseModel):
    """A manufacturer's published code list, installed from the catalog.

    The ISO/SAE sets ship in the image because they answer for every vehicle
    ever built and are finite. A manufacturer's list is neither: there are
    ninety-odd makes, a shop owns two or three of them, and bundling all of
    them would put eighteen thousand definitions in every image so that each
    operator could use a few hundred. Parser profiles were split the same way
    and for the same reason — see `catalog/README.md`.

    So this is a **row rather than a file**: installed on request, removable,
    and backed up with the rest of the database. `dtc` reads these alongside
    the bundled standard, and a make with nothing installed falls through to
    the standard and to what the shop wrote down itself, which is exactly
    where it stood before any list existed.

    `documents` holds one entry per published document — `source`,
    `precedence`, `codes` — because a make may be covered by more than one
    and they are different claims. `version` is the publisher's, and is what
    tells the browse screen that what is installed is behind what is offered.
    """

    make = models.CharField(max_length=60)
    slug = models.CharField(max_length=64, blank=True)
    aliases = models.JSONField(default=list, blank=True)
    version = models.PositiveIntegerField(default=1)
    description = models.TextField(blank=True)
    author = models.CharField(max_length=80, blank=True)
    documents = models.JSONField(default=list, blank=True)

    class Meta(BaseModel.Meta):
        ordering = ["make"]
        constraints = [
            models.UniqueConstraint(
                fields=["make"],
                condition=models.Q(deleted_at__isnull=True),
                name="unique_installed_code_list",
            )
        ]

    def __str__(self) -> str:
        return self.make

    @property
    def name(self) -> str:
        """What the catalog calls it. The make is the whole identity here."""
        return self.make

    @property
    def code_count(self) -> int:
        return len({c for d in self.documents for c in (d.get("codes") or {})})
