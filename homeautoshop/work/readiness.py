"""
The readiness gate (SPEC §8.7, FR-WL-3/4, INTEGRATION-WRENCHLEDGER.md §6.2).

*Can I actually do this job today?* — answered on Wednesday rather than on
Saturday morning with the wheel already off.

**It is a warning, never a block.** The operator may know the torque wrench is
fine, or may be borrowing one from a neighbor. Blocking work on data from an
optional external system would be indefensible, and NG-8 means this application
has no standing to be sure it is right.

The whole surface degrades to nothing: with WrenchLedger absent, disabled, or
unreachable, `for_work_order` returns an empty list and every screen renders
exactly as it did before the integration existed (FR-WL-7).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from homeautoshop.core.runtime import conf


@dataclass(slots=True)
class ToolWarning:
    tool_name: str
    tool_url: str
    issues: list[str] = field(default_factory=list)
    stale: bool = False
    job_item: str = ""


def enabled() -> bool:
    return bool(conf.WRENCHLEDGER_API_KEY) and not conf.OFFLINE_MODE


def for_work_order(work_order) -> list[ToolWarning]:
    """Tools referenced by this job that are not simply available."""
    from .models import JobItemTool

    if not enabled():
        return []

    warnings = []
    references = (
        JobItemTool.objects.filter(job_item__work_order=work_order)
        .select_related("tool", "job_item")
        .order_by("job_item__sequence", "tool__name")
    )
    for reference in references:
        issues = reference.tool.issues
        if not issues:
            continue
        warnings.append(
            ToolWarning(
                tool_name=reference.tool.name or reference.tool.tool_id,
                tool_url=reference.tool.url,
                issues=[str(issue) for issue in issues],
                stale=reference.tool.is_stale,
                job_item=reference.job_item.title,
            )
        )
    return warnings


def blocked_work_orders(limit: int = 20):
    """Open jobs whose tools are not all available, for the planning dashboard.

    Sits beside `waiting_on_parts` (FR-REP-1) because it is the same kind of
    fact: work you cannot start yet, and the reason why.
    """
    from .models import OPEN_STATUSES, JobItemTool, WorkOrder

    if not enabled():
        return []

    candidates = (
        # `OPEN_STATUSES`, not a hand-written list: this said `"open"`,
        # which is not a work-order status at all, so the query quietly
        # skipped every `planned` and `on_hold` job — which is most of the
        # ones you would want warned about before Saturday.
        WorkOrder.objects.filter(status__in=OPEN_STATUSES)
        .filter(job_items__tools__isnull=False)
        .distinct()[:limit]
    )
    found = []
    for work_order in candidates:
        warnings = for_work_order(work_order)
        if warnings:
            found.append((work_order, warnings))
    return found
