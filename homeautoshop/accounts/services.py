"""Removing an account that never did anything (SPEC FR-ADM-2, FR-ADM-8).

Deactivation is the right answer for somebody who worked here: their name stays
on what they did, and taking the key away does not rewrite the history. It is
the wrong answer for an account created by mistake, or while trying the
application out, which has no history to protect — and until now those could
only be hidden, never removed, so an instance used for a while silently filled
with accounts that meant nothing and could not be tidied away.

So the rule is the narrowest one that solves it: **an account may be deleted
only if nothing in the shop carries its name.** That is the same shape
FR-ADM-8 already uses for vendors, locations and order lines — a record that
explains something does not disappear from underneath it — and it keeps the
risk near zero, because by definition there is nothing to lose.

Everything turns on `traces()` being exhaustive, and the reason it is computed
from `_meta` rather than written out by hand is worth stating: `User` has 57
relations and about fifty of them are `created_by` audit fields declared with
`related_name="+"`. Those are **hidden relations** — they do not appear in
`_meta.related_objects`, they are invisible to a grep for the model name, and
they are exactly the ones that record that somebody did something. A
hand-maintained list would have missed them on the day it was written and
would rot afterwards; asking Django enumerates them, including the ones added
next year.
"""

from __future__ import annotations

from collections import defaultdict

from django.db.models import Q

#: Relations that *are* the account rather than work done with it. These go
#: when it goes, and none of them is a reason to refuse: a personal access
#: token, a reminder channel, a vehicle grant and a row in Django's own admin
#: log describe the login, not the shop.
ACCOUNT_ONLY = frozenset({
    "accounts.AssetAccess.user",
    "accounts.ApiToken.user",
    "accounts.User_groups.user",
    "accounts.User_user_permissions.user",
    "core.NotificationChannel.user",
    "core.NotificationChannel.created_by",
    "admin.LogEntry.user",
})


def traces(user) -> list[tuple[str, int]]:
    """Everything in the shop carrying this account's name, most first.

    Counted through `_base_manager` on purpose, so a soft-deleted work order
    still counts. It is in the trash rather than gone, somebody may restore
    it, and it would come back with its author already deleted.
    """
    by_model: dict = defaultdict(list)
    for rel in user._meta._get_fields(forward=False, reverse=True, include_hidden=True):
        field = getattr(rel, "field", None)
        if field is None:
            continue
        label = f"{rel.related_model._meta.label}.{field.name}"
        if label in ACCOUNT_ONLY:
            continue
        by_model[rel.related_model].append(field.name)

    found: list[tuple[str, int]] = []
    for model, fields in by_model.items():
        # One query per model with the field names OR'd, so a time entry that
        # is both created by and worked by the same person counts once. Two
        # rows where there is one is the kind of small wrongness that makes
        # somebody distrust the whole message.
        query = Q()
        for name in fields:
            query |= Q(**{name: user})
        count = model._base_manager.filter(query).distinct().count()
        if count:
            found.append((str(model._meta.verbose_name_plural), count))
    return sorted(found, key=lambda row: (-row[1], row[0]))


def describe_traces(marks: list[tuple[str, int]], limit: int = 4) -> str:
    """The holding list as a phrase, for a message somebody has to act on.

    Naming them beats "this account has history": the answer to *why can I not
    delete this* is a list of things to go and deal with, and a refusal that
    does not say what to deal with is a dead end.
    """
    shown = ", ".join(f"{count} {label}" for label, count in marks[:limit])
    if len(marks) > limit:
        shown += f", and {len(marks) - limit} more"
    return shown
