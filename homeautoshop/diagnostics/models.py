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

    fingerprint = models.JSONField(default=dict, blank=True)
    field_extractors = models.JSONField(default=dict, blank=True)
    table_extractor = models.JSONField(default=dict, blank=True)

    source = models.CharField(
        max_length=10, choices=ProfileSource.choices, default=ProfileSource.USER
    )
    is_active = models.BooleanField(default=True)
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
    is_generic = models.BooleanField(default=True)
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
