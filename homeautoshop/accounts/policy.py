"""Per-vehicle authorization for the `helper` role (SPEC §12.2a, R-2).

A helper is somebody you let work on one vehicle. They see that vehicle and
everything about it, they record what they did, and they see nothing else —
not the other cars, not what anything cost, not the shelf, not the suppliers,
not who else is in the address book.

**Why this is a gate and not another call site.** §12.2 promised that adding
this role would be "populating a table and adding policy rules, not auditing
every view", on the strength of every decision going through `can()`. It did
not hold: of 225 view functions, 48 called `require()` and only 19 of those
named a resource — all of them in `assets/views.py`. The apps where a helper
actually works had none at all. The scaffold was real for admin-versus-member
and absent for per-vehicle, and the reason is structural: a rule enforced by
225 people remembering is a rule that holds until somebody adds view 226.

So the outer boundary is enforced **once, on every request**, against an
allow-list of URL names. A helper reaching anything not on that list is
refused before the view runs. A new screen is therefore closed to helpers on
the day it is written, and opening it is a deliberate line in this file rather
than an omission nobody notices.

The inner boundary — *which* vehicle — still needs the view's own knowledge of
what it is looking at, so allow-listed views call `require(user, action, obj)`
as before. Two layers, and the outer one is the one that cannot be forgotten.
"""

from __future__ import annotations

#: Verbs that only read. Everything else is treated as a write, so a verb
#: nobody thought about is a write — the safe direction to be wrong in.
READ_VERBS = frozenset({"read", "view", "list", "search", "describe"})

#: Pages any signed-in person may reach, helper included: they are not about a
#: vehicle at all. Kept separate from the vehicle screens below so that "what
#: does a helper get for free" is one short list somebody can actually check.
ALWAYS = frozenset({
    "login", "logout", "dashboard", "search", "healthz", "readyz",
    "service_worker", "push_subscribe", "scan_target", "media_file",
    "media_file_variant",
})

#: The vehicle screens. A helper reaching one of these still has to hold a
#: grant on the vehicle in question — that is the view's own check — but a URL
#: absent from here is refused outright, whatever the grants say.
#:
#: What is deliberately *not* here is as much of the design as what is:
#: `asset_costs`, `asset_report` and `export_csv` are money; `purchase_*`,
#: `vendor_*` and `order_import` are the supply chain; `inventory`, `lot_*`,
#: `location_*` and `kit_item_*` are the shelf; `person_*` is the address
#: book. A helper fixing a truck needs none of them, and each is somebody
#: else's private business.
VEHICLE = frozenset({
    # The vehicle and its history
    "asset_list", "asset_detail", "asset_timeline", "asset_schedule",
    "asset_specs", "asset_diagnostics", "asset_recalls", "asset_photo_upload",
    "reading_create", "component_add", "component_remove", "media_unlink",
    "media_rename", "recall_status",
    # Attaching a manual and keeping the forum thread that solved the fault
    # are part of working on the vehicle, not privileges over the shop. Each
    # is object-checked as `asset.edit`, so a read-only grant still cannot.
    "asset_document_upload", "asset_link_add", "asset_link_delete",
    # Work
    "work_order_list", "work_order_detail", "work_order_create",
    "work_order_edit", "work_order_transition", "work_order_photo",
    "work_order_time_add", "time_entry_edit", "time_entry_delete",
    "note_create", "job_item_create", "job_item_edit", "job_item_delete",
    "job_item_move", "job_item_toggle",
    # Recording what went on the vehicle. The catalog is readable and the
    # shelf is not: a helper says "I fitted this filter", and what it cost and
    # how many are left are not their business.
    "work_order_part_use", "work_order_part_require",
    "work_order_part_unrequire", "part_use",
    "part_list", "part_detail", "part_search", "part_by_code",
    # Maintenance
    "due_list", "service_item_add", "service_item_update",
    "service_item_complete", "service_item_snooze", "service_item_remove",
    "apply_schedule_template",
    # Inspections
    "inspection_list", "inspection_start", "inspection_detail",
    "inspection_resume", "inspection_add_check", "inspection_complete",
    "inspection_abandon", "inspection_convert", "result_record",
    "result_remove", "wear_chart",
    # Diagnostics
    "diagnostic_queue", "session_detail", "session_import", "session_confirm",
    "session_discard", "session_map", "session_reparse", "code_status",
    "code_describe", "code_promote", "elm327", "elm_capture",
})

HELPER_URLS = ALWAYS | VEHICLE

#: Screens a helper may open but only read. The catalog tells them which
#: filter they are holding; it does not let them edit the shop's parts list.
HELPER_READ_ONLY_URLS = frozenset({
    "part_list", "part_detail", "part_search", "part_by_code", "asset_list",
    "asset_timeline", "asset_specs", "asset_diagnostics", "asset_recalls",
    "due_list", "inspection_list", "work_order_list", "diagnostic_queue",
    "wear_chart", "code_describe",
})


def is_helper(user) -> bool:
    from .models import Role

    return getattr(user, "role", None) == Role.HELPER and not user.is_admin


def asset_of(resource):
    """The vehicle a record belongs to, however many hops away it is.

    A helper's permission on a job item is really a permission on the work
    order's vehicle, and on a part usage it is the vehicle it was fitted to.
    Walking the relation here means each view can hand over whatever object it
    happens to be holding rather than every view knowing the shape of every
    other app's models.
    """
    from homeautoshop.assets.models import Asset

    hops = 0
    while resource is not None and hops < 5:
        if isinstance(resource, Asset):
            return resource
        for attr in ("asset", "work_order", "service_item", "inspection", "session"):
            nxt = getattr(resource, attr, None)
            if nxt is not None:
                resource = nxt
                break
        else:
            return None
        hops += 1
    return None


def granted(user, asset, *, write: bool) -> bool:
    """Whether this helper holds a grant on this vehicle at this level."""
    from .models import AssetAccess

    if asset is None:
        return False
    grant = AssetAccess.objects.filter(user=user, asset=asset).first()
    if grant is None:
        return False
    return grant.level == "write" or not write


def helper_can(user, action: str, resource=None) -> bool:
    """Whether a helper may take `action`, on `resource` where there is one.

    Deny by default. An action in a domain nobody has thought about is refused
    rather than allowed, which is the difference between this and the v1
    implied-allow that `member` still gets.
    """
    domain, _, verb = action.partition(".")
    write = verb not in READ_VERBS

    if domain not in HELPER_DOMAINS:
        return False
    if domain in HELPER_READ_ONLY_DOMAINS and write:
        return False

    asset = asset_of(resource)
    if asset is not None:
        return granted(user, asset, write=write)

    # No vehicle in hand. A read is a listing, and listings are filtered by
    # `visible_assets` rather than refused — showing an empty page is the
    # correct answer for a helper with no grants. A write with no vehicle
    # named is refused: creating a car, or editing the shop's parts list, is
    # not something a per-vehicle grant can authorise.
    return not write


#: Domains a helper may act in at all. The URL gate above is the outer fence;
#: this is the same boundary expressed for the object-level checks, so a view
#: that forgets to name its resource still cannot be used to write.
HELPER_DOMAINS = frozenset({
    "asset", "work", "job", "note", "time", "media", "reading", "component",
    "maintenance", "service", "inspection", "diagnostic", "code", "part",
    "recall",
})

#: …of which these may only ever be read by a helper.
HELPER_READ_ONLY_DOMAINS = frozenset({"part"})


def visible_assets(user, queryset=None):
    """The vehicles this user may see — every one of them, unless a helper.

    The single place a listing is narrowed. Every screen that lists vehicles
    or anything belonging to one filters through this, so "what can a helper
    see" has one answer rather than one answer per page.
    """
    from homeautoshop.assets.models import Asset

    qs = Asset.objects.all() if queryset is None else queryset
    if not is_helper(user):
        return qs
    return qs.filter(access_grants__user=user)


def visible_assets_for(user, queryset, field: str = "asset"):
    """The same narrowing, applied to a queryset of something else.

    `field` names the path from that model to its vehicle — `asset` for a work
    order, `work_order__asset` for a job item.
    """
    if not is_helper(user):
        return queryset
    return queryset.filter(**{f"{field}__access_grants__user": user})
