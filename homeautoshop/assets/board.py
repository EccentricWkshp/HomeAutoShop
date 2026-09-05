"""The board: what order the cards are in, and what each one says.

Two jobs that belong together because they read the same table.

**Order.** Per user (`AssetCardPreference.board_order`). Three rules make it
behave under the things that actually happen to a board:

* *A vehicle nobody has placed sorts last, alphabetically.* Adding a car must
  not shuffle a board somebody arranged, and inserting it at a position derived
  from its nickname would do exactly that.
* *Arranging anything places everything.* The first move materializes a
  position for every vehicle the person can see — not just the ones on screen.
  Without that, a swap on the Equipment tab would be comparing a number against
  a `NULL`, and the two orders would have to be merged on every read.
* *A move inside a filtered view moves the card, not the slots.* The Vehicles
  screen filters by kind and hides sold vehicles, and the dashboard shows six
  of them. Dragging the third card above the second there must not renumber a
  board from a view that was never showing all of it — so the visible cards
  swap **which slots they occupy**, and everything hidden keeps the slot it
  had, still interleaved where it was.

**Contents.** `cards_for` assembles what each card shows in a fixed number of
queries — one per pin kind that something on the board actually uses, and none
at all for a pin nobody has. The alternative is the per-asset property (`asset
.current_usage` is a query, `current_owner` is another), which is correct and
costs two queries per card times however many cards; a board of twenty is forty
queries to draw a list.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from django.utils.translation import gettext_lazy as _

from . import cards as cardlib
from .models import INACTIVE_STATUSES, Asset, AssetCardPreference, AssetOwnership


# --------------------------------------------------------------------- scope

#: How many vehicles the dashboard's Fleet panel shows. Named here because the
#: panel and the reorder that answers it have to agree about what "the third
#: card" is, and a limit written twice is a limit that drifts.
FLEET_PREVIEW = 6

#: The two screens that draw a board. A rearrangement says which one it came
#: from, because "the card above this one" is a different card on each.
SCOPE_VEHICLES = "vehicles"
SCOPE_FLEET = "fleet"


def scope_for(user, scope: str, *, kind: str = "", show_all: bool = False) -> tuple:
    """`(everything, visible)` — the whole board, and the slice a screen drew.

    Rebuilt from the request rather than trusted from it. The submitted ids say
    what somebody dragged; what they were allowed to be looking at is the
    server's answer, and a request naming a vehicle that was never on the
    screen simply finds it missing from `visible` and moves nothing.
    """
    from homeautoshop.accounts.policy import visible_assets

    everything = list(visible_assets(user, Asset.objects.all()))
    if scope == SCOPE_FLEET:
        fleet = [asset for asset in everything if asset.in_fleet]
        return everything, in_board_order(fleet, preferences_for(user, fleet))[:FLEET_PREVIEW]

    visible = everything
    if kind:
        visible = [asset for asset in visible if asset.asset_kind == kind]
    if not show_all:
        # Matching `asset_list`, which hides disposals but keeps prospects: a
        # car you are thinking about buying is one you want on the screen.
        visible = [asset for asset in visible if asset.status not in INACTIVE_STATUSES]
    return everything, visible


# ---------------------------------------------------------------- preferences


def preferences_for(user, assets) -> dict:
    """`{asset_id: AssetCardPreference}` for the assets given, in one query."""
    ids = [asset.pk for asset in assets]
    if not ids:
        return {}
    return {
        pref.asset_id: pref
        for pref in AssetCardPreference.objects.filter(user=user, asset_id__in=ids)
    }


def _sort_key(pref) -> tuple:
    """Placed cards first in their order; unplaced last. Nickname breaks ties."""
    order = getattr(pref, "board_order", None) if pref else None
    return (1, 0) if order is None else (0, order)


def in_board_order(assets, prefs: dict) -> list:
    """Sort assets by this person's board, leaving the input untouched."""
    return sorted(
        assets,
        key=lambda asset: (*_sort_key(prefs.get(asset.pk)), asset.nickname.lower()),
    )


def ensure_placed(user, assets) -> dict:
    """Give every one of these vehicles a position, if it has not got one.

    Called before any rearrangement, with **every** vehicle the person can see
    rather than the filtered page they are looking at, because a position is
    only meaningful against the whole board. The order handed out is the order
    they were already being shown in, so materializing it changes nothing on
    screen — it only turns an implied order into a stored one that a swap can
    work against.
    """
    prefs = preferences_for(user, assets)
    ordered = in_board_order(assets, prefs)

    missing = []
    for position, asset in enumerate(ordered):
        pref = prefs.get(asset.pk)
        if pref is None:
            pref = AssetCardPreference(user=user, asset=asset, board_order=position)
            prefs[asset.pk] = pref
            missing.append(pref)
        elif pref.board_order != position:
            pref.board_order = position
            pref.save(update_fields=["board_order"])
    if missing:
        AssetCardPreference.objects.bulk_create(missing)
    return prefs


def _apply_slots(prefs: dict, ordered_ids: list) -> None:
    """Reassign the slots these cards already occupy, in the order given.

    The slots are the fixed thing. Whatever positions this subset held on the
    full board — 0, 4 and 5, say, with three hidden vehicles between them —
    those same three positions are handed back out in the new sequence, so
    nothing outside the subset moves relative to anything else.
    """
    subset = [prefs[pk] for pk in ordered_ids if pk in prefs]
    slots = sorted(pref.board_order for pref in subset if pref.board_order is not None)
    if len(slots) != len(subset):
        return
    for pref, slot in zip(subset, slots):
        if pref.board_order != slot:
            pref.board_order = slot
            pref.save(update_fields=["board_order"])


def move(user, everything, visible, asset, direction: str) -> None:
    """Swap one card with its neighbor *as the person can see it*.

    "Up" means above the card that is drawn above it, which on a filtered
    screen is not the next slot on the board. Resolving the neighbor from the
    visible list and then swapping their two slots is what makes the button do
    what the screen shows.
    """
    prefs = ensure_placed(user, everything)
    ordered = in_board_order(visible, prefs)
    ids = [row.pk for row in ordered]
    if asset.pk not in ids:
        return
    here = ids.index(asset.pk)
    there = here - 1 if direction == "up" else here + 1
    if not 0 <= there < len(ids):
        return
    ids[here], ids[there] = ids[there], ids[here]
    _apply_slots(prefs, ids)


def reorder(user, everything, visible, ordered_ids: list) -> None:
    """Put the visible cards into the sequence given (the drag-and-drop path).

    Ids that are not on screen are dropped rather than trusted: the request
    says what the person dragged, and what they could see is the server's
    business. An incomplete list is also fine — anything omitted keeps its
    slot, which is what happens when a card is filtered out mid-drag.
    """
    prefs = ensure_placed(user, everything)
    visible_ids = {row.pk for row in visible}
    wanted, seen = [], set()
    for pk in ordered_ids:
        if pk in visible_ids and pk not in seen:
            wanted.append(pk)
            seen.add(pk)
    # Whatever the request left out stays where it was, in board order, so a
    # partial list rearranges part of the board rather than truncating it.
    for row in in_board_order(visible, prefs):
        if row.pk not in seen:
            wanted.append(row.pk)
    _apply_slots(prefs, wanted)


# --------------------------------------------------------------------- cards


@dataclass
class Fact:
    """One label/value row on a card."""

    label: str
    value: str
    mono: bool = False


@dataclass
class Badge:
    """One small pill on a card, optionally linking somewhere."""

    text: str
    level: str = ""
    url: str = ""


@dataclass
class CardLine:
    """A heading and its badges — what is due, what is open, what is stored."""

    label: str
    badges: list = field(default_factory=list)


@dataclass
class Card:
    """Everything the template needs to draw one card, already resolved."""

    asset: Asset
    color: str = ""
    pins: tuple = ()
    status: object = None
    descriptor: str = ""
    meter: str = ""
    photo: object = None
    facts: list = field(default_factory=list)
    lines: list = field(default_factory=list)

    #: Templates cannot call a method with an argument, and the two pins whose
    #: *value* can legitimately be empty — a vehicle with no year, make or
    #: model, and one with no reading yet — need "pinned but blank" to be
    #: distinguishable from "not pinned". Both draw a placeholder, and drawing
    #: it on a card that never asked for the row would be a card saying
    #: something nobody chose.
    @property
    def shows_descriptor(self) -> bool:
        return "descriptor" in self.pins

    @property
    def shows_meter(self) -> bool:
        return "meter" in self.pins


#: How many badges one line carries before it says "and N more". A card is a
#: recognition aid; a truck with nineteen open codes must not push the next
#: vehicle off the screen to say so.
BADGE_LIMIT = 3


def _capped(badges: list, total: int) -> list:
    if total <= BADGE_LIMIT:
        return badges
    rest = total - BADGE_LIMIT
    return [*badges[:BADGE_LIMIT], Badge(_("+%(n)d more") % {"n": rest})]


def panel_for(user, assets, limit: int = FLEET_PREVIEW) -> list:
    """The dashboard's Fleet panel — this person's order and colors, no pins.

    Deliberately not `cards_for`. The panel is a list of names, and running the
    pin queries to draw six links would be paying for a card nobody asked for
    on the busiest screen in the application.
    """
    assets = list(assets)
    prefs = preferences_for(user, assets)
    ordered = in_board_order(assets, prefs)[:limit]
    return [
        Card(asset=asset, color=(getattr(prefs.get(asset.pk), "color", "") or ""))
        for asset in ordered
    ]


def cards_for(user, assets) -> list:
    """Assemble the cards for a board, in this person's order.

    Returns `Card` objects, not annotated assets, because the template's job is
    to draw what it is handed — a template deciding whether a mower has a plate
    is the rule living in the one place it cannot be tested.
    """
    assets = list(assets)
    prefs = preferences_for(user, assets)
    ordered = in_board_order(assets, prefs)

    resolved = {}
    for asset in ordered:
        pref = prefs.get(asset.pk)
        stored = pref.pins if pref is not None else None
        # `None` is "never chose", which takes the defaults. An empty list is a
        # choice — a card showing nothing but its nickname — and is honored.
        keys = cardlib.DEFAULT_PINS if stored is None else stored
        resolved[asset.pk] = tuple(cardlib.valid_pins(keys, kind=asset.asset_kind))

    wanted = set().union(*resolved.values()) if resolved else set()
    ids = [asset.pk for asset in ordered]

    meters = _meters(ids) if "meter" in wanted else {}
    people = _people(ids) if {"owner", "driver"} & wanted else {}
    photos = _photos(ordered) if "photo" in wanted else {}
    schedule = _schedule(ids) if "schedule" in wanted else {}
    work = _work_orders(ids) if "work_orders" in wanted else {}
    codes = _codes(ids) if "codes" in wanted else {}

    board = []
    for asset in ordered:
        pref = prefs.get(asset.pk)
        pins = resolved[asset.pk]
        card = Card(
            asset=asset,
            color=(pref.color if pref else "") or "",
            pins=pins,
        )
        if "status" in pins:
            card.status = asset.get_status_display()
        if "descriptor" in pins:
            card.descriptor = asset.descriptor
        if "photo" in pins:
            card.photo = photos.get(asset.pk)
        if "meter" in pins:
            card.meter = meters.get(asset.pk, "")
        if "vin" in pins and asset.vin:
            # The mask, never the VIN (NFR-S-5). A board is the most-visited
            # screen in the application and the easiest one to be standing
            # behind somebody while they read.
            card.facts.append(Fact(_("VIN"), asset.masked_vin, mono=True))
        if "plate" in pins and asset.plate:
            plate = f"{asset.plate} ({asset.plate_region})" if asset.plate_region else asset.plate
            card.facts.append(Fact(_("Plate"), plate, mono=True))
        if "engine" in pins and asset.engine:
            card.facts.append(Fact(_("Engine"), asset.engine))
        if "owner" in pins:
            name = people.get((asset.pk, AssetOwnership.Role.OWNER))
            if name:
                card.facts.append(Fact(_("Owner"), name))
        if "driver" in pins:
            name = people.get((asset.pk, AssetOwnership.Role.DRIVER))
            if name:
                card.facts.append(Fact(_("Driver"), name))
        for key, line in (
            ("schedule", schedule.get(asset.pk)),
            ("work_orders", work.get(asset.pk)),
            ("codes", codes.get(asset.pk)),
        ):
            if key in pins and line and line.badges:
                card.lines.append(line)
        board.append(card)
    return board


def _meters(ids) -> dict:
    """The newest reading per vehicle, as one string, in one query.

    Ordered by asset first so the first row seen for each is its latest; the
    per-asset `latest_reading()` is the same question asked once per card.
    """
    from .models import UsageReading

    out = {}
    rows = (
        UsageReading.objects.filter(asset_id__in=ids)
        .order_by("asset_id", "-read_on", "-created_at")
        .only("asset_id", "value", "unit")
    )
    for row in rows:
        if row.asset_id not in out:
            out[row.asset_id] = f"{row.value:,.0f} {row.unit}"
    return out


def _people(ids) -> dict:
    """`{(asset_id, role): name}` for current owners and drivers."""
    rows = (
        AssetOwnership.objects.filter(
            asset_id__in=ids,
            to_date__isnull=True,
            role__in=[AssetOwnership.Role.OWNER, AssetOwnership.Role.DRIVER],
        )
        .select_related("person")
        .order_by("-from_date")
    )
    out = {}
    for row in rows:
        out.setdefault((row.asset_id, row.role), str(row.person))
    return out


def _photos(assets) -> dict:
    """One picture per card — the chosen one, else the first one attached.

    `primary_photo` is a column and the photo grid is `MediaLink`, and a
    vehicle can easily have the second without the first: nothing forces
    somebody to nominate one. Falling back means the pin does something on the
    ordinary vehicle rather than only on the curated one.
    """
    from homeautoshop.mediafiles.models import Media, MediaLink

    out = {asset.pk: asset.primary_photo for asset in assets if asset.primary_photo_id}
    missing = [asset.pk for asset in assets if not asset.primary_photo_id]
    if not missing:
        return out
    links = (
        MediaLink.objects.filter(
            entity_type="Asset", entity_id__in=missing, media__kind=Media.Kind.PHOTO
        )
        .select_related("media")
        .order_by("entity_id", "sort_order", "created_at")
    )
    for link in links:
        out.setdefault(link.entity_id, link.media)
    return out


def _schedule(ids) -> dict:
    """What is overdue or due soon, per vehicle.

    `next_due_on` and `status` are stored on the row by `maintenance.services
    .recalculate`, so this needs no projection arithmetic — `project()` reads
    the odometer and computes a usage rate per item, which is the right thing
    on the schedule page and forty queries here.
    """
    from homeautoshop.maintenance.models import AssetServiceItem, ServiceStatus

    rows = (
        AssetServiceItem.objects.needing_attention()
        .filter(asset_id__in=ids)
        .select_related("definition")
        .order_by("asset_id", "next_due_on", "definition__name")
    )
    grouped: dict = {}
    for row in rows:
        grouped.setdefault(row.asset_id, []).append(row)
    out = {}
    for asset_id, items in grouped.items():
        badges = [
            Badge(
                text=item.definition.name,
                level="danger" if item.status == ServiceStatus.OVERDUE else "warn",
            )
            for item in items[:BADGE_LIMIT]
        ]
        out[asset_id] = CardLine(label=_("Due"), badges=_capped(badges, len(items)))
    return out


def _work_orders(ids) -> dict:
    from homeautoshop.work.models import WorkOrder

    rows = (
        WorkOrder.objects.open()
        .filter(asset_id__in=ids)
        .order_by("asset_id", "-created_at")
        .only("id", "asset_id", "number", "title", "status")
    )
    grouped: dict = {}
    for row in rows:
        grouped.setdefault(row.asset_id, []).append(row)
    out = {}
    for asset_id, orders in grouped.items():
        badges = [Badge(text=order.number) for order in orders[:BADGE_LIMIT]]
        out[asset_id] = CardLine(label=_("Open work"), badges=_capped(badges, len(orders)))
    return out


def _codes(ids) -> dict:
    """Trouble codes still standing against each vehicle.

    The same three conditions the vehicle's own diagnostics page uses: a
    confirmed session, not one removed from the history, and a code the
    operator has not marked addressed. A join does not consult the related
    model's manager, so the `deleted_at` test has to be written out.
    """
    from homeautoshop.diagnostics.models import CodeStatus, DiagnosticCode, ReviewStatus

    rows = (
        DiagnosticCode.objects.filter(
            session__asset_id__in=ids,
            session__review_status=ReviewStatus.CONFIRMED,
            session__deleted_at__isnull=True,
            status__in=[CodeStatus.OPEN, CodeStatus.RECURRING],
        )
        .order_by("session__asset_id", "code")
        .values_list("session__asset_id", "code")
    )
    grouped: dict = {}
    for asset_id, code in rows:
        grouped.setdefault(asset_id, []).append(code)
    out = {}
    for asset_id, found in grouped.items():
        badges = [Badge(text=code, level="danger") for code in found[:BADGE_LIMIT]]
        out[asset_id] = CardLine(label=_("Codes"), badges=_capped(badges, len(found)))
    return out
