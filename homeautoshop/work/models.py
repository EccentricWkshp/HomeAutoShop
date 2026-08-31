"""
Work orders — the central record (SPEC §6.2, §7.3, Appendix A).

One unit of shop work on one asset: what was wrong, what was done, what it
took. The three-C fields exist because six months later "brakes noisy" is
useless without "inner pad worn to the backing, caliper slide pins seized" and
"replaced pads and rotors, rebuilt caliper".
"""

from __future__ import annotations

from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from homeautoshop.core.models import (
    AppendOnlyModel,
    BaseModel,
    RevisionedModel,
    SoftDeleteQuerySet,
    alive_manager,
)


class WorkOrderType(models.TextChoices):
    MAINTENANCE = "maintenance", _("Maintenance")
    REPAIR = "repair", _("Repair")
    DIAGNOSIS = "diagnosis", _("Diagnosis")
    MODIFICATION = "modification", _("Modification")
    INSPECTION = "inspection", _("Inspection")
    PROJECT = "project", _("Project")


class WorkOrderStatus(models.TextChoices):
    PLANNED = "planned", _("Planned")
    IN_PROGRESS = "in_progress", _("In progress")
    WAITING_ON_PARTS = "waiting_on_parts", _("Waiting on parts")
    ON_HOLD = "on_hold", _("On hold")
    COMPLETE = "complete", _("Complete")
    ABANDONED = "abandoned", _("Abandoned")


OPEN_STATUSES = (
    WorkOrderStatus.PLANNED,
    WorkOrderStatus.IN_PROGRESS,
    WorkOrderStatus.WAITING_ON_PARTS,
    WorkOrderStatus.ON_HOLD,
)

# The lifecycle from REFERENCE.md §1. Reopening a completed work order is
# allowed and audit-logged; it does not rewrite history.
#
# **Every open status can go back to `planned`**, which the diagram did not
# show and the first person to use this immediately wanted. Starting a job by
# accident is not a rare event in a home shop, and the graph as drawn made an
# accidental "start" unrecoverable except by completing the work order and
# reopening it — pushing a false completion through the record, and firing the
# service completions attached to it, to undo a mis-tap. Going back is cheaper
# to allow than that is to explain.
TRANSITIONS: dict[str, tuple[str, ...]] = {
    WorkOrderStatus.PLANNED: (WorkOrderStatus.IN_PROGRESS, WorkOrderStatus.ABANDONED),
    WorkOrderStatus.IN_PROGRESS: (
        WorkOrderStatus.PLANNED,
        WorkOrderStatus.WAITING_ON_PARTS,
        WorkOrderStatus.ON_HOLD,
        WorkOrderStatus.COMPLETE,
        WorkOrderStatus.ABANDONED,
    ),
    WorkOrderStatus.WAITING_ON_PARTS: (
        WorkOrderStatus.PLANNED,
        WorkOrderStatus.IN_PROGRESS,
        WorkOrderStatus.ON_HOLD,
        WorkOrderStatus.ABANDONED,
    ),
    WorkOrderStatus.ON_HOLD: (
        WorkOrderStatus.PLANNED,
        WorkOrderStatus.IN_PROGRESS,
        WorkOrderStatus.WAITING_ON_PARTS,
        WorkOrderStatus.ABANDONED,
    ),
    WorkOrderStatus.COMPLETE: (WorkOrderStatus.PLANNED, WorkOrderStatus.IN_PROGRESS),
    WorkOrderStatus.ABANDONED: (WorkOrderStatus.PLANNED, WorkOrderStatus.IN_PROGRESS),
}

#: What each target needs before it will be accepted, so a form can say so
#: *before* it is submitted rather than after (§7.2). Read by the detail
#: template; enforced, as it always was, in `transition_to` below — a hint in a
#: browser is a courtesy, never the check.
REQUIREMENTS: dict[str, str] = {
    WorkOrderStatus.WAITING_ON_PARTS: "blocked_reason",
    WorkOrderStatus.COMPLETE: "odometer_out",
}


class IllegalTransition(ValidationError):
    pass


class WorkOrderQuerySet(SoftDeleteQuerySet):
    def open(self):
        return self.filter(status__in=OPEN_STATUSES)

    def blocked(self):
        return self.filter(status=WorkOrderStatus.WAITING_ON_PARTS)


class WorkOrder(RevisionedModel):
    asset = models.ForeignKey("assets.Asset", on_delete=models.CASCADE, related_name="work_orders")
    number = models.CharField(max_length=24, unique=True, editable=False)
    title = models.CharField(max_length=200)
    type = models.CharField(max_length=16, choices=WorkOrderType.choices, default=WorkOrderType.REPAIR)
    status = models.CharField(
        max_length=20, choices=WorkOrderStatus.choices, default=WorkOrderStatus.PLANNED, db_index=True
    )
    priority = models.PositiveSmallIntegerField(default=3, help_text=_("1 highest, 5 lowest."))

    # The three C's (SPEC FR-WO-4)
    complaint = models.TextField(blank=True, help_text=_("What is wrong, in the reporter's words."))
    cause = models.TextField(blank=True, help_text=_("What you found."))
    correction = models.TextField(blank=True, help_text=_("What you did about it."))

    opened_at = models.DateTimeField(default=timezone.now)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    odometer_in = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    odometer_out = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)

    requested_by = models.ForeignKey(
        "people.Person", null=True, blank=True, on_delete=models.SET_NULL, related_name="requested_work"
    )
    parent = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="children",
        help_text=_("A long project collects child work orders while staying one record."),
    )
    blocked_reason = models.TextField(
        blank=True, help_text=_("Required while waiting on parts, so the dashboard can explain itself.")
    )
    is_safety_critical = models.BooleanField(default=False)
    tags = models.JSONField(default=list, blank=True)

    objects = alive_manager(WorkOrderQuerySet)
    all_objects = models.Manager.from_queryset(WorkOrderQuerySet)()

    class Meta:
        ordering = ["-opened_at"]
        indexes = [
            models.Index(fields=["status", "-opened_at"]),
            models.Index(fields=["asset", "-opened_at"]),
        ]

    def __str__(self) -> str:
        return f"{self.number} — {self.title}"

    def save(self, *args, **kwargs):
        if not self.number:
            self.number = self._next_number()
        return super().save(*args, **kwargs)

    @staticmethod
    def _next_number() -> str:
        year = timezone.localdate().year
        prefix = f"WO-{year}-"
        last = (
            WorkOrder.all_objects.filter(number__startswith=prefix)
            .order_by("-number")
            .values_list("number", flat=True)
            .first()
        )
        seq = int(last.rsplit("-", 1)[1]) + 1 if last else 1
        return f"{prefix}{seq:04d}"

    # -- lifecycle (Appendix A) ------------------------------------------

    def descendant_ids(self) -> set:
        """Every work order underneath this one, however deep.

        Used to keep the parent picker from offering a cycle. Walked in Python
        rather than with a recursive CTE because a home shop's project tree is
        a handful of rows and a portable query beats a clever one.
        """
        found: set = set()
        frontier = [self.pk]
        while frontier:
            children = list(
                WorkOrder.all_objects.filter(parent_id__in=frontier)
                .exclude(pk__in=found)
                .values_list("pk", flat=True)
            )
            if not children:
                break
            found.update(children)
            frontier = children
        return found

    def can_transition_to(self, status: str) -> bool:
        return status in TRANSITIONS.get(self.status, ())

    def transition_to(self, status: str, *, user=None, odometer_out=None) -> None:
        if status == self.status:
            return
        if not self.can_transition_to(status):
            raise IllegalTransition(
                _("A work order cannot go from %(a)s to %(b)s.")
                % {"a": self.get_status_display(), "b": dict(WorkOrderStatus.choices).get(status, status)}
            )

        if status == WorkOrderStatus.WAITING_ON_PARTS and not self.blocked_reason.strip():
            raise ValidationError(
                {"blocked_reason": _("Say what it is waiting for, so the dashboard can explain the block.")}
            )

        if status == WorkOrderStatus.COMPLETE:
            # FR-WO-9: closing out without the odometer loses the one number
            # that makes the record useful later.
            if odometer_out is not None:
                self.odometer_out = odometer_out
            if self.asset.has_meter and self.odometer_out is None:
                raise ValidationError(
                    {"odometer_out": _("Record the meter reading before completing this work order.")}
                )
            self.completed_at = timezone.now()

        if status == WorkOrderStatus.IN_PROGRESS and self.started_at is None:
            self.started_at = timezone.now()

        if self.status == WorkOrderStatus.COMPLETE and status != WorkOrderStatus.COMPLETE:
            # Reopening. History is not rewritten — completed_at stays until
            # the work order is completed again.
            self.completed_at = None

        self.status = status
        self.save()

    # -- presentation ----------------------------------------------------

    @property
    def is_open(self) -> bool:
        return self.status in OPEN_STATUSES

    @property
    def job_item_progress(self) -> tuple[int, int]:
        items = list(self.job_items.all())
        return sum(1 for i in items if i.is_done), len(items)


class JobItem(RevisionedModel):
    """One line of work within a work order, independently completable.

    A garage session is usually "oil change + brake pads + chase that rattle";
    each is its own item so they can close out separately.
    """

    class Status(models.TextChoices):
        TODO = "todo", _("To do")
        DOING = "doing", _("In progress")
        DONE = "done", _("Done")
        SKIPPED = "skipped", _("Skipped")

    work_order = models.ForeignKey(WorkOrder, on_delete=models.CASCADE, related_name="job_items")
    sequence = models.PositiveIntegerField(default=0)
    title = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.TODO)
    assigned_to = models.ForeignKey(
        "accounts.User", null=True, blank=True, on_delete=models.SET_NULL, related_name="job_items"
    )
    service_item = models.ForeignKey(
        "maintenance.AssetServiceItem",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="job_items",
        help_text=_("Completing this rolls that maintenance interval forward (FR-WO-7)."),
    )
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["sequence", "created_at"]

    def __str__(self) -> str:
        return self.title

    @property
    def is_done(self) -> bool:
        return self.status in (self.Status.DONE, self.Status.SKIPPED)

    def save(self, *args, **kwargs):
        newly_done = self.status == self.Status.DONE and self.completed_at is None
        if newly_done:
            self.completed_at = timezone.now()
        if self.status != self.Status.DONE:
            self.completed_at = None
        super().save(*args, **kwargs)

        # Doing the work IS resetting the schedule — there is no second place
        # to remember to update (SPEC §6.2, FR-MAINT-5).
        if newly_done and self.service_item_id:
            from homeautoshop.maintenance.services import complete as complete_service

            already = self.service_completions.exists()
            if not already:
                complete_service(
                    self.service_item,
                    usage=self.work_order.odometer_out or self.work_order.asset.current_usage,
                    job_item=self,
                    work_order=self.work_order,
                    note=self.title[:200],
                )

        # Same principle for a trouble code: closing the work IS the answer to
        # "did we deal with it", and a code left `open` because somebody forgot
        # to go back and say so is a code that never gets flagged when it
        # returns (SPEC §8.3c).
        if newly_done:
            from homeautoshop.diagnostics.services import close_codes_for

            close_codes_for(self)
        return None


class WorkOrderNote(AppendOnlyModel):
    """The running log of a job. Append-only and never edited (SPEC §6.2)."""

    work_order = models.ForeignKey(WorkOrder, on_delete=models.CASCADE, related_name="notes")
    body = models.TextField()
    author = models.ForeignKey(
        "accounts.User", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    noted_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-noted_at"]

    def __str__(self) -> str:
        return self.body[:60]


class TimeEntry(AppendOnlyModel):
    """Time on a job (SPEC §7.6, FR-TIME-1..3).

    Append-only like the rest of the capture surface. Time may be valued at an
    instance rate for reporting, but that figure is labeled an estimate and is
    never rendered as a bill — this is not a commercial shop (NG-1).
    """

    class Category(models.TextChoices):
        DIAGNOSIS = "diagnosis", _("Diagnosis")
        WRENCHING = "wrenching", _("Wrenching")
        PARTS_RUN = "parts_run", _("Parts run")
        CLEANUP = "cleanup", _("Cleanup")
        RESEARCH = "research", _("Research")

    work_order = models.ForeignKey(WorkOrder, on_delete=models.CASCADE, related_name="time_entries")
    job_item = models.ForeignKey(
        JobItem, null=True, blank=True, on_delete=models.SET_NULL, related_name="time_entries"
    )
    user = models.ForeignKey(
        "accounts.User", null=True, blank=True, on_delete=models.SET_NULL, related_name="time_entries"
    )
    started_at = models.DateTimeField(null=True, blank=True)
    ended_at = models.DateTimeField(null=True, blank=True)
    minutes = models.PositiveIntegerField(default=0)
    category = models.CharField(max_length=16, choices=Category.choices, default=Category.WRENCHING)
    note = models.CharField(max_length=200, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.hours:.2f} h — {self.get_category_display()}"

    @property
    def hours(self) -> float:
        return round(self.minutes / 60, 2)

    def save(self, *args, **kwargs):
        # A timer supplies start/end; after-the-fact entry supplies minutes.
        # Either is valid, and one derives the other.
        if self.started_at and self.ended_at and not self.minutes:
            self.minutes = max(0, int((self.ended_at - self.started_at).total_seconds() // 60))
        return super().save(*args, **kwargs)


class ShopTool(BaseModel):
    """A cached shadow of one WrenchLedger tool (SPEC §8.7, FR-WL-8).

    Deliberately thin, and the thinness is the point. HomeAutoShop does not
    track tools — WrenchLedger does (NG-8) — so what is kept here is exactly
    what the readiness gate needs to answer *can I do this job today*, plus
    when it was last checked so staleness is visible rather than assumed.

    What is **not** here is the specification: no storage location, no purchase
    price, no serial number, no photos, and no borrower contact details. Those
    arrive in the payload anyway, and are dropped at the edge by an allow-list
    rather than a deny-list — a deny-list would silently start storing
    valuation data the first time WrenchLedger adds a field.

    One row per tool rather than one per reference: the same breaker bar named
    on three job items is one thing that is either on loan or not, and caching
    it three times means three chances to disagree.
    """

    tool_id = models.CharField(max_length=64, unique=True)
    name = models.CharField(max_length=160, blank=True)
    brand = models.CharField(max_length=80, blank=True)
    model = models.CharField(max_length=80, blank=True)
    #: stored / under repair / missing / sold, in WrenchLedger's vocabulary.
    lifecycle = models.CharField(max_length=32, blank=True)
    on_loan_to = models.CharField(max_length=80, blank=True)
    loan_due_on = models.DateField(null=True, blank=True)
    from_kit = models.CharField(max_length=80, blank=True)
    calibration_due_on = models.DateField(null=True, blank=True)
    checked_at = models.DateTimeField(null=True, blank=True)

    class Meta(BaseModel.Meta):
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name or self.tool_id

    @property
    def url(self) -> str:
        """Link out rather than reproduce. WrenchLedger owns the detail page."""
        return f"https://www.wrench-ledger.app/tools/{self.tool_id}"

    @property
    def issues(self) -> list[str]:
        """Reasons this tool might not be usable today. Warnings, never blocks."""
        from django.utils import timezone as _tz

        found = []
        if self.on_loan_to:
            if self.loan_due_on:
                found.append(
                    _("on loan to %(who)s, due %(when)s")
                    % {"who": self.on_loan_to, "when": self.loan_due_on}
                )
            else:
                found.append(_("on loan to %(who)s") % {"who": self.on_loan_to})
        if self.lifecycle and self.lifecycle.lower() not in ("stored", "active", "available"):
            found.append(_("marked %(state)s") % {"state": self.lifecycle})
        if self.calibration_due_on and self.calibration_due_on <= _tz.localdate():
            found.append(_("calibration due %(when)s") % {"when": self.calibration_due_on})
        return found

    @property
    def is_stale(self) -> bool:
        """Older than a day is shown as an age, not asserted as current fact."""
        from django.utils import timezone as _tz

        if self.checked_at is None:
            return True
        return (_tz.now() - self.checked_at).total_seconds() > 86400


class JobItemTool(BaseModel):
    """A job item's reference to a tool. The id, and nothing else (FR-WL-2).

    Not a copy of the tool record — a pointer. Names change, and `revision`
    exists on WrenchLedger's Tool object precisely because it expects edits.
    """

    job_item = models.ForeignKey(JobItem, on_delete=models.CASCADE, related_name="tools")
    tool = models.ForeignKey(ShopTool, on_delete=models.CASCADE, related_name="references")

    class Meta(BaseModel.Meta):
        constraints = [
            models.UniqueConstraint(
                fields=["job_item", "tool"],
                condition=models.Q(deleted_at__isnull=True),
                name="unique_job_item_tool",
            )
        ]

    def __str__(self) -> str:
        return f"{self.tool} on {self.job_item}"
