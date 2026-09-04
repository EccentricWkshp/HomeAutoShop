"""
Arranging the board: order, colour, and what each card says (SPEC §9.3).

The Vehicles screen was an alphabetical grid of identical cards, which answers
"list my vehicles" and not the question it is actually opened for — "which of
these is the truck". Three things follow, and each is tested for the property
that makes it worth having rather than for the fact that it renders:

* **Order is per person and survives a filter.** The awkward case is not
  dragging on the All tab, it is dragging on the Equipment tab: two cards swap
  there, and the vehicles hidden between them must not move relative to
  anything. That is the `_apply_slots` contract and most of what is below.
* **Dragging is an enhancement, not the mechanism.** The ↑/↓ buttons post an
  ordinary form and do the whole job with no script, so they are what the tests
  drive; `asset_reorder` is the same rearrangement said all at once.
* **A card says what was pinned to it, and never more.** In particular it shows
  the *masked* VIN — a board is the most-visited screen in the application and
  the easiest one to be standing behind somebody while they read (NFR-S-5).
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from homeautoshop.accounts.models import Role, User
from homeautoshop.assets import board, cards
from homeautoshop.assets.models import (
    Asset,
    AssetCardPreference,
    AssetKind,
    AssetOwnership,
    AssetStatus,
    UsageReading,
)
from homeautoshop.people.models import Person

VIN = "1FTFW1ET5DFC10312"


def order_of(user, assets):
    """The nicknames on this person's board, in the order they are drawn."""
    prefs = board.preferences_for(user, assets)
    return [asset.nickname for asset in board.in_board_order(assets, prefs)]


class BoardOrderTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="andy", password="x" * 16)
        self.client.force_login(self.user)
        # Deliberately created out of alphabetical order, so a test that passes
        # by accident of insertion order fails here.
        self.truck = Asset.objects.create(nickname="Truck")
        self.car = Asset.objects.create(nickname="Car")
        self.mower = Asset.objects.create(nickname="Mower", asset_kind=AssetKind.EQUIPMENT)
        self.all = [self.truck, self.car, self.mower]

    def move(self, asset, direction, **extra):
        return self.client.post(
            reverse("asset_move", args=[asset.pk]),
            {"direction": direction, "scope": board.SCOPE_VEHICLES, **extra},
        )

    def test_an_unarranged_board_is_alphabetical(self):
        """The order it had before any of this existed. Nobody is surprised."""
        self.assertEqual(order_of(self.user, self.all), ["Car", "Mower", "Truck"])

    def test_moving_a_card_up_swaps_it_with_the_one_above(self):
        self.move(self.truck, "up")
        self.assertEqual(order_of(self.user, self.all), ["Car", "Truck", "Mower"])

    def test_moving_the_first_card_up_does_nothing(self):
        """There is no position above the first one, and the button says so.

        Disabled in the markup, and refused here too: the disabled attribute is
        a hint to a browser, not a rule about what may be posted.
        """
        self.move(self.car, "up")
        self.assertEqual(order_of(self.user, self.all), ["Car", "Mower", "Truck"])

    def test_the_order_is_one_persons_and_not_the_shops(self):
        other = User.objects.create_user(username="sam", password="x" * 16)
        self.move(self.truck, "up")
        self.assertEqual(order_of(self.user, self.all), ["Car", "Truck", "Mower"])
        self.assertEqual(order_of(other, self.all), ["Car", "Mower", "Truck"])

    def test_a_vehicle_added_later_lands_at_the_end(self):
        """Not inserted alphabetically into a board somebody arranged.

        An unplaced card sorts after every placed one. Anything else means
        adding a vehicle silently reshuffles the screen around it.
        """
        self.move(self.truck, "up")
        added = Asset.objects.create(nickname="Aardvark")
        self.assertEqual(
            order_of(self.user, [*self.all, added]), ["Car", "Truck", "Mower", "Aardvark"]
        )

    def test_moving_inside_a_filter_leaves_the_hidden_cards_alone(self):
        """The case the whole slot arrangement exists for.

        On the Equipment tab only the mower is visible, so a move there can see
        no neighbour and must change nothing — and, crucially, must not
        renumber the board from a view that was never showing all of it.
        """
        self.move(self.truck, "up")
        before = order_of(self.user, self.all)
        self.move(self.mower, "up", kind=AssetKind.EQUIPMENT)
        self.assertEqual(order_of(self.user, self.all), before)

    def test_a_swap_on_a_filtered_tab_moves_past_what_is_hidden(self):
        """Two vehicles either side of a hidden mower swap with each other.

        "Up" means above the card that is *drawn* above it. On the Vehicle tab
        the mower is not there, so the truck moving up must land above the car
        — and the mower must still sit between them on the unfiltered board,
        which is what proves the hidden card kept its slot.
        """
        AssetCardPreference.objects.create(user=self.user, asset=self.car, board_order=0)
        AssetCardPreference.objects.create(user=self.user, asset=self.mower, board_order=1)
        AssetCardPreference.objects.create(user=self.user, asset=self.truck, board_order=2)
        self.move(self.truck, "up", kind=AssetKind.VEHICLE)
        self.assertEqual(order_of(self.user, self.all), ["Truck", "Mower", "Car"])

    def test_a_finished_drag_stores_the_whole_sequence(self):
        response = self.client.post(
            reverse("asset_reorder"),
            {
                "scope": board.SCOPE_VEHICLES,
                "ids": [str(self.mower.pk), str(self.truck.pk), str(self.car.pk)],
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(order_of(self.user, self.all), ["Mower", "Truck", "Car"])

    def test_a_drag_naming_a_vehicle_that_was_not_on_screen_ignores_it(self):
        """The request says what was dragged; the server says what was visible.

        Sent from the Equipment tab, a sequence naming the truck cannot move
        the truck — it was not on that screen, and a client asserting otherwise
        is not a reason to believe it.
        """
        self.client.post(
            reverse("asset_reorder"),
            {
                "scope": board.SCOPE_VEHICLES,
                "kind": AssetKind.EQUIPMENT,
                "ids": [str(self.truck.pk), str(self.mower.pk)],
            },
        )
        self.assertEqual(order_of(self.user, self.all), ["Car", "Mower", "Truck"])

    def test_a_malformed_id_does_not_lose_the_rest_of_the_drag(self):
        self.client.post(
            reverse("asset_reorder"),
            {
                "scope": board.SCOPE_VEHICLES,
                "ids": ["not-a-uuid", str(self.truck.pk), str(self.car.pk), str(self.mower.pk)],
            },
        )
        self.assertEqual(order_of(self.user, self.all), ["Truck", "Car", "Mower"])

    def test_reordering_returns_to_the_tab_it_came_from(self):
        response = self.move(self.mower, "up", kind=AssetKind.EQUIPMENT)
        self.assertIn("kind=equipment", response["Location"])

    def test_a_helper_arranges_only_the_board_they_can_see(self):
        """Per-user preferences are not a route to a vehicle nobody granted.

        The helper may reorder — it is their own board — but `scope_for` builds
        the visible list from `visible_assets`, so a vehicle they hold no grant
        on is not in it and cannot be moved.
        """
        helper = User.objects.create_user(username="pat", password="x" * 16, role=Role.HELPER)
        helper.asset_access.create(asset=self.car, level="read")
        self.client.force_login(helper)
        self.client.post(
            reverse("asset_move", args=[self.truck.pk]),
            {"direction": "up", "scope": board.SCOPE_VEHICLES},
        )
        self.assertFalse(
            AssetCardPreference.objects.filter(user=helper, asset=self.truck).exists()
        )


class FleetPanelTests(TestCase):
    """The dashboard's Fleet panel is the same board, showing its first few."""

    def setUp(self):
        self.user = User.objects.create_user(username="andy", password="x" * 16)
        self.client.force_login(self.user)
        self.car = Asset.objects.create(nickname="Car")
        self.truck = Asset.objects.create(nickname="Truck")

    def test_the_panel_follows_the_board_not_what_changed_last(self):
        self.client.post(
            reverse("asset_move", args=[self.truck.pk]),
            {"direction": "up", "scope": board.SCOPE_FLEET},
        )
        panel = board.panel_for(self.user, Asset.objects.fleet())
        self.assertEqual([card.asset.nickname for card in panel], ["Truck", "Car"])

    def test_rearranging_the_panel_rearranges_the_vehicles_screen(self):
        """One board, two screens. Two orders would be two things to maintain."""
        self.client.post(
            reverse("asset_move", args=[self.truck.pk]),
            {"direction": "up", "scope": board.SCOPE_FLEET},
        )
        self.assertEqual(order_of(self.user, [self.car, self.truck]), ["Truck", "Car"])

    def test_a_sold_vehicle_is_not_on_the_panel(self):
        self.car.status = AssetStatus.SOLD
        self.car.save()
        panel = board.panel_for(self.user, Asset.objects.fleet())
        self.assertEqual([card.asset.nickname for card in panel], ["Truck"])


class CardContentTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="andy", password="x" * 16)
        self.client.force_login(self.user)
        self.truck = Asset.objects.create(
            nickname="Truck", vin=VIN, plate="ABC123", plate_region="OR",
            year=2013, make="Ford", model="F-150", engine="3.5L V6",
        )

    def pin(self, *keys):
        AssetCardPreference.objects.update_or_create(
            user=self.user, asset=self.truck, defaults={"pins": list(keys)}
        )

    def card(self):
        return board.cards_for(self.user, [self.truck])[0]

    def test_an_unconfigured_card_takes_the_defaults(self):
        self.assertEqual(tuple(self.card().pins), cards.DEFAULT_PINS)

    def test_pinning_nothing_is_a_choice_and_is_kept(self):
        """Distinct from never having chosen, which takes the defaults.

        An empty checkbox group submits nothing at all, so the two states are
        indistinguishable in a POST body — which is what `card_prefs` is for.
        Stored, they must stay distinguishable.
        """
        self.pin()
        self.assertEqual(self.card().pins, ())

    def test_a_pinned_vin_is_masked(self):
        """NFR-S-5. The board is the easiest screen to read over a shoulder."""
        self.pin("vin")
        values = [fact.value for fact in self.card().facts]
        self.assertIn(self.truck.masked_vin, values)
        self.assertNotIn(VIN, values)
        page = self.client.get(reverse("asset_list")).content.decode()
        self.assertNotIn(VIN, page)

    def test_rearranging_the_board_does_not_blank_the_cards(self):
        """The bug that made `pins` nullable rather than `default=list`.

        `ensure_placed` writes a row for every vehicle the first time anybody
        moves one, purely to hold a position. With an empty list as the column
        default, each of those rows also said "pin nothing" — so the first drag
        anybody made silently reduced every card on the board to its nickname.
        Never having chosen has to be a distinguishable state, and it is
        `NULL`.
        """
        self.client.post(
            reverse("asset_move", args=[self.truck.pk]),
            {"direction": "down", "scope": board.SCOPE_VEHICLES},
        )
        self.assertTrue(
            AssetCardPreference.objects.filter(user=self.user, asset=self.truck).exists()
        )
        self.assertEqual(tuple(self.card().pins), cards.DEFAULT_PINS)

    def test_a_pin_with_nothing_behind_it_draws_no_row(self):
        """An empty row is worse than an absent one: it reads as a missing fact."""
        blank = Asset.objects.create(nickname="Project")
        AssetCardPreference.objects.create(
            user=self.user, asset=blank, pins=["vin", "plate", "engine"]
        )
        card = next(c for c in board.cards_for(self.user, [blank]) if c.asset == blank)
        self.assertEqual(card.facts, [])

    def test_equipment_never_carries_a_vin_pin(self):
        """`Asset.clean` refuses a VIN on equipment, so the pin cannot apply.

        Stored is not the same as honoured — a vehicle turned into equipment
        keeps whatever was pinned to it, and the card is what has to be right.
        """
        mower = Asset.objects.create(nickname="Mower", asset_kind=AssetKind.EQUIPMENT)
        AssetCardPreference.objects.create(user=self.user, asset=mower, pins=["vin", "meter"])
        card = board.cards_for(self.user, [mower])[0]
        self.assertEqual(tuple(card.pins), ("meter",))

    def test_the_meter_pin_shows_the_newest_reading(self):
        self.pin("meter")
        UsageReading.objects.create(
            asset=self.truck, value=90000, unit="mi", read_on=timezone.localdate() - timedelta(days=30)
        )
        UsageReading.objects.create(
            asset=self.truck, value=91500, unit="mi", read_on=timezone.localdate()
        )
        self.assertEqual(self.card().meter, "91,500 mi")

    def test_the_owner_and_driver_pins_name_the_current_ones(self):
        self.pin("owner", "driver")
        owner = Person.objects.create(display_name="Dana")
        driver = Person.objects.create(display_name="Rye")
        past = Person.objects.create(display_name="Sold To Nobody")
        AssetOwnership.objects.create(asset=self.truck, person=owner, role="owner")
        AssetOwnership.objects.create(asset=self.truck, person=driver, role="primary_driver")
        AssetOwnership.objects.create(
            asset=self.truck, person=past, role="owner", to_date=timezone.localdate()
        )
        values = [fact.value for fact in self.card().facts]
        self.assertIn("Dana", values)
        self.assertIn("Rye", values)
        self.assertNotIn("Sold To Nobody", values)

    def test_imminent_schedule_items_reach_the_card(self):
        from homeautoshop.maintenance.models import (
            AssetServiceItem,
            ServiceDefinition,
            ServiceStatus,
        )

        self.pin("schedule")
        definition = ServiceDefinition.objects.create(name="Oil change")
        AssetServiceItem.objects.create(
            asset=self.truck,
            definition=definition,
            interval_distance=5000,
            status=ServiceStatus.OVERDUE,
        )
        line = self.card().lines[0]
        self.assertEqual([badge.text for badge in line.badges], ["Oil change"])
        self.assertEqual(line.badges[0].level, "danger")

    def test_a_healthy_schedule_puts_no_line_on_the_card(self):
        """Nothing due is not a fact worth a row. The card stays quiet."""
        from homeautoshop.maintenance.models import (
            AssetServiceItem,
            ServiceDefinition,
            ServiceStatus,
        )

        self.pin("schedule")
        definition = ServiceDefinition.objects.create(name="Oil change")
        AssetServiceItem.objects.create(
            asset=self.truck, definition=definition, interval_distance=5000,
            status=ServiceStatus.OK,
        )
        self.assertEqual(self.card().lines, [])

    def test_a_long_list_is_capped_rather_than_allowed_to_fill_the_screen(self):
        from homeautoshop.maintenance.models import (
            AssetServiceItem,
            ServiceDefinition,
            ServiceStatus,
        )

        self.pin("schedule")
        for n in range(7):
            AssetServiceItem.objects.create(
                asset=self.truck,
                definition=ServiceDefinition.objects.create(name=f"Job {n}"),
                interval_distance=5000,
                status=ServiceStatus.OVERDUE,
            )
        badges = self.card().lines[0].badges
        self.assertEqual(len(badges), board.BADGE_LIMIT + 1)
        self.assertIn("4", str(badges[-1].text))


class CardFormTests(TestCase):
    """Colour and pins are set where the vehicle is edited, and are per person."""

    def setUp(self):
        self.user = User.objects.create_user(username="andy", password="x" * 16)
        self.client.force_login(self.user)
        self.truck = Asset.objects.create(nickname="Truck", year=2013, make="Ford")
        self.url = reverse("asset_edit", args=[self.truck.pk])

    def post(self, **extra):
        data = {
            "nickname": "Truck",
            "asset_kind": AssetKind.VEHICLE,
            "status": AssetStatus.ACTIVE,
            "meter": "odometer",
            "meter_unit": "mi",
            "card_prefs": "1",
            **extra,
        }
        return self.client.post(self.url, data)

    def test_the_edit_screen_offers_the_colours_and_the_pins(self):
        page = self.client.get(self.url).content.decode()
        self.assertIn('name="card_color"', page)
        self.assertIn('value="schedule"', page)

    def test_saving_a_colour_and_pins_writes_this_persons_row(self):
        self.post(card_color="blue", card_pins=["status", "plate"])
        pref = AssetCardPreference.objects.get(user=self.user, asset=self.truck)
        self.assertEqual(pref.color, "blue")
        self.assertEqual(pref.pins, ["status", "plate"])

    def test_pins_are_stored_in_the_catalogs_order_not_the_forms(self):
        """So two vehicles pinned the same way read the same way down the card.

        The order checkboxes were submitted in is not a layout decision
        anybody meant to make.
        """
        self.post(card_pins=["plate", "status", "descriptor"])
        pref = AssetCardPreference.objects.get(user=self.user, asset=self.truck)
        self.assertEqual(pref.pins, ["descriptor", "status", "plate"])

    def test_another_persons_card_is_untouched(self):
        other = User.objects.create_user(username="sam", password="x" * 16)
        AssetCardPreference.objects.create(user=other, asset=self.truck, color="red")
        self.post(card_color="blue", card_pins=["status"])
        self.assertEqual(
            AssetCardPreference.objects.get(user=other, asset=self.truck).color, "red"
        )

    def test_a_post_without_the_card_section_leaves_the_card_alone(self):
        """The guard `card_prefs` exists for.

        An empty checkbox group and a post that never carried the section look
        identical in a request body, and one of them means "pin nothing".
        """
        AssetCardPreference.objects.create(
            user=self.user, asset=self.truck, color="red", pins=["status"]
        )
        self.client.post(
            self.url,
            {
                "nickname": "Truck",
                "asset_kind": AssetKind.VEHICLE,
                "status": AssetStatus.ACTIVE,
                "meter": "odometer",
                "meter_unit": "mi",
            },
        )
        pref = AssetCardPreference.objects.get(user=self.user, asset=self.truck)
        self.assertEqual(pref.color, "red")
        self.assertEqual(pref.pins, ["status"])

    def test_a_card_back_at_its_defaults_keeps_no_row(self):
        """Absent means default, which is the state every card starts in."""
        self.post(card_pins=list(cards.DEFAULT_PINS))
        self.assertFalse(
            AssetCardPreference.objects.filter(user=self.user, asset=self.truck).exists()
        )

    def test_clearing_the_colour_never_costs_a_position(self):
        """Order and colour share a row, and only one of them was being changed."""
        AssetCardPreference.objects.create(
            user=self.user, asset=self.truck, color="red", board_order=3
        )
        self.post(card_pins=list(cards.DEFAULT_PINS))
        pref = AssetCardPreference.objects.get(user=self.user, asset=self.truck)
        self.assertEqual(pref.board_order, 3)
        self.assertEqual(pref.color, "")


class BoardMarkupTests(TestCase):
    """What the screen has to carry for the no-script path to work at all."""

    def setUp(self):
        self.user = User.objects.create_user(username="andy", password="x" * 16)
        self.client.force_login(self.user)
        Asset.objects.create(nickname="Car")
        Asset.objects.create(nickname="Truck")

    def test_every_card_carries_move_buttons_that_post(self):
        page = self.client.get(reverse("asset_list")).content.decode()
        self.assertEqual(page.count('name="direction"'), 4)
        self.assertIn('value="up"', page)

    def test_the_grip_is_not_advertised_until_a_script_reveals_it(self):
        """An affordance a browser may not have must not be advertised.

        `.boardgrip` is `display: none` and `board.js` puts `can-drag` on the
        document element. Asserted from the stylesheet as well as the markup,
        because the grip *is* in the HTML — it is the pair of rules that keeps
        it from being offered, and deleting either one would be silent.

        Read as "hidden, then revealed" rather than matched against the exact
        declarations. The first version of this pinned `display: block`, so
        making the grip a flex box to give it a real tap target failed a test
        about whether the grip is advertised — which is not what changed.
        """
        page = self.client.get(reverse("asset_list")).content.decode()
        self.assertIn("data-board-grip", page)
        css = Path("static/app.css").read_text(encoding="utf-8")
        base = css.split(".boardgrip {")[1].split("}")[0]
        self.assertIn("display: none", base)
        revealed = css.split(".can-drag .boardgrip {")[1].split("}")[0]
        self.assertIn("display:", revealed)
        self.assertNotIn("display: none", revealed)

    def test_the_dashboard_panel_reorders_too(self):
        page = self.client.get(reverse("dashboard")).content.decode()
        self.assertIn('data-board-scope="fleet"', page)


class BoardScriptTests(TestCase):
    """The properties that keep the drag from flickering (`static/board.js`).

    The first version reordered the DOM on every `pointermove` and chose the
    target by measuring the cards live. That is a loop, not a rough edge: the
    move reflows the grid, the reflow puts a different card under the pointer,
    the next event moves it back, and the board oscillates between two
    arrangements while the pointer sits perfectly still. What reached the
    garage was a card that flashed between two places and landed somewhere
    arbitrary.

    So the fix is structural and these are the three load-bearing pieces of it.
    Each is a property of the source, and each brings the flicker straight back
    if it is undone:

    * measure **once**, at `pointerdown`;
    * hold those boxes in **page** coordinates, so a scroll mid-drag — the edge
      autoscroll included — does not silently invalidate them;
    * preview with `transform`, which paints elsewhere without reflowing, and
      touch the DOM **exactly once**, on drop.

    Checked as source rather than executed, the same bargain `tests_elm327.py`
    and `tests_forms.py` make: there is no JavaScript runtime in the image.
    """

    @classmethod
    def setUpTestData(cls):
        cls.source = Path("static/board.js").read_text(encoding="utf-8")

    @staticmethod
    def body(source: str, header: str) -> str:
        """One function's body, by matching braces from its opening one."""
        start = source.index(header)
        opened = source.index("{", start)
        depth = 0
        for i in range(opened, len(source)):
            if source[i] == "{":
                depth += 1
            elif source[i] == "}":
                depth -= 1
                if depth == 0:
                    return source[opened : i + 1]
        raise AssertionError(f"{header} is not closed")

    def test_the_cards_are_measured_once_and_only_at_the_start(self):
        """A second measurement mid-drag is a measurement of the preview.

        The cards are painted somewhere other than where they are laid out, so
        asking the browser where they are during a drag returns the answer the
        preview drew — and choosing the next target from that is the loop.
        """
        self.assertEqual(self.source.count("getBoundingClientRect"), 1)
        self.assertIn("getBoundingClientRect", self.body(self.source, "function measure("))

    def test_the_frozen_boxes_are_page_coordinates(self):
        """Or the edge autoscroll quietly invalidates every one of them.

        `getBoundingClientRect` is relative to the viewport, so a board that
        scrolls under the finger would leave the slots describing where the
        cards *were* on screen a moment ago.
        """
        measure = self.body(self.source, "function measure(")
        self.assertIn("window.scrollX", measure)
        self.assertIn("window.scrollY", measure)

    def test_the_preview_moves_nothing_it_only_paints(self):
        preview = self.body(self.source, "function preview(")
        self.assertIn("style.transform", preview)
        for reflow in ("appendChild", "insertBefore", "removeChild"):
            self.assertNotIn(reflow, preview)

    def test_the_dom_is_reordered_exactly_once_and_on_the_drop(self):
        """The single reflow of the whole gesture, into what was on screen."""
        self.assertEqual(self.source.count("appendChild"), 1)
        self.assertNotIn("insertBefore", self.source)
        self.assertIn("appendChild", self.body(self.source, "function finish("))

    def test_a_drag_that_ends_where_it_began_posts_nothing(self):
        """Picking a card up and putting it back is not a rearrangement."""
        finish = self.body(self.source, "function finish(")
        self.assertIn("here.target === here.index", finish)
        self.assertLess(finish.index("here.target === here.index"), finish.index("commit("))
