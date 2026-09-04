/*
 * Dragging cards on the board — an enhancement of two buttons, not a mechanism.
 *
 * `work/views.py::job_item_move` argued against dragging and the argument still
 * holds: it needs a script to exist at all, it is unreachable from a keyboard
 * without building a second mechanism beside it, and it is unkind on a phone
 * held in one gloved hand. So the second mechanism is built first. Every card
 * carries ↑ and ↓ buttons that post an ordinary form to `asset_move`, and that
 * is the real way to rearrange a board. It works with this file blocked, with
 * scripting off entirely, and from a Tab key.
 *
 * What this adds is the gesture, for the pointer that has one. The grips are
 * `display: none` until this file puts `can-drag` on the document element, so a
 * browser that never runs it never advertises an affordance it does not have.
 *
 * ---------------------------------------------------------------------------
 * Nothing reflows until the drag is over.
 *
 * The first version reordered the DOM on every `pointermove` and chose the
 * target by measuring the cards live. That flickers, and the flicker is not a
 * tuning problem — it is a loop. Moving a card reflows the grid, the reflow
 * puts a different card under the pointer, the next event moves it back, and
 * the board oscillates between two arrangements with the pointer perfectly
 * still. Landing a card on purpose was luck.
 *
 * So the geometry is frozen. At `pointerdown` every card's box is measured once
 * — those are the **slots** — and for the rest of the gesture:
 *
 *   - the target is "which frozen slot is the pointer in", which cannot feed
 *     back into anything, because the slots do not move;
 *   - the preview is drawn with `transform`, which paints a card somewhere else
 *     without changing layout, so the slots stay true;
 *   - the DOM is reordered exactly once, on drop, into the arrangement the
 *     preview has been showing all along.
 *
 * Slots are held in page coordinates, so a page that scrolls mid-drag — under
 * the finger, or from the edge autoscroll below — does not invalidate them.
 *
 * Two decisions from the first version were right and stay:
 *
 *   - **Pointer events, not the HTML5 drag-and-drop API.** `dragstart` does not
 *     fire from a touch on any mobile browser, and a garage phone is the
 *     machine this screen is read on. One code path for mouse, pen and finger.
 *   - **It posts the same form the buttons post**, to a sibling endpoint that
 *     takes a whole sequence instead of one direction. Not JSON, no bespoke
 *     protocol — a failure lands on a page that is still correct.
 */
(function () {
  "use strict";

  if (!window.fetch || !window.PointerEvent) return;

  /* Put on the document element, so the grips are revealed from the stylesheet
     rather than one at a time. An affordance is still never advertised by a
     browser that cannot honour it, and markup that arrives later — a board
     swapped in by `liveform.js` after a move — is already right without
     anything having to notice it. */
  var READY = "can-drag";
  var LIFTED = "is-dragging";
  var SORTING = "is-sorting";

  /* How near the edge of the window the pointer has to be before the page
     follows it, and how fast. Without this, a board taller than the screen can
     only be rearranged as far as the fold. */
  var EDGE = 64;
  var SPEED = 14;

  function items(board) {
    return Array.prototype.slice.call(board.querySelectorAll("[data-board-item]"));
  }

  /* The ends of the list are the two positions where a button would do
     nothing, and a control that does nothing must say so. Recomputed after a
     drop because the card that was last may not be any more. */
  function refreshEnds(board) {
    var rows = items(board);
    rows.forEach(function (row, index) {
      var up = row.querySelector('[name="direction"][value="up"]');
      var down = row.querySelector('[name="direction"][value="down"]');
      if (up && up.form) up.form.querySelector("button").disabled = index === 0;
      if (down && down.form) {
        down.form.querySelector("button").disabled = index === rows.length - 1;
      }
    });
  }

  function csrf(board) {
    var field = board.querySelector('[name="csrfmiddlewaretoken"]');
    return field ? field.value : "";
  }

  /* Tell the server the sequence it can now see on screen. The scope and the
     filter go with it because "the card above this one" is a different card on
     the Equipment tab than it is on All, and the server rebuilds both rather
     than trusting the ids alone. */
  function commit(board) {
    var body = new FormData();
    body.append("csrfmiddlewaretoken", csrf(board));
    body.append("scope", board.dataset.boardScope || "vehicles");
    body.append("kind", board.dataset.boardKind || "");
    body.append("all", board.dataset.boardAll || "");
    items(board).forEach(function (row) {
      body.append("ids", row.dataset.id);
    });
    return fetch(board.dataset.boardUrl, {
      method: "POST",
      body: body,
      credentials: "same-origin",
      headers: { "X-Requested-With": "fetch" },
    }).then(function (response) {
      // A rearrangement the server did not accept must not be left on the
      // screen looking accepted. Reloading is blunt and it is honest: the page
      // that comes back is the order that was actually stored.
      if (!response.ok) window.location.reload();
    }, function () {
      window.location.reload();
    });
  }

  // -- frozen geometry -----------------------------------------------------

  /* Every card's box in page coordinates, at the moment the drag began. */
  function measure(rows) {
    return rows.map(function (row) {
      var box = row.getBoundingClientRect();
      return {
        x: box.left + window.scrollX,
        y: box.top + window.scrollY,
        w: box.width,
        h: box.height,
      };
    });
  }

  /* Which slot the pointer is in.
   *
   * Inside one wins outright, so the answer does not drift while the pointer
   * sits still inside a card. Otherwise the nearest centre, which covers the
   * gaps between cards, the margins either side of the grid, and the empty
   * space past the last row. One rule serves the two-dimensional Vehicles grid
   * and the single-column Fleet panel alike, because it is asked about boxes
   * rather than about rows and columns.
   */
  function slotAt(slots, px, py) {
    var best = 0;
    var bestDistance = Infinity;
    for (var i = 0; i < slots.length; i++) {
      var slot = slots[i];
      if (px >= slot.x && px <= slot.x + slot.w && py >= slot.y && py <= slot.y + slot.h) {
        return i;
      }
      var dx = px - (slot.x + slot.w / 2);
      var dy = py - (slot.y + slot.h / 2);
      var distance = dx * dx + dy * dy;
      if (distance < bestDistance) {
        bestDistance = distance;
        best = i;
      }
    }
    return best;
  }

  /* Draw the arrangement without building it.
   *
   * The card being dragged is under the pointer; every card between where it
   * came from and where it is headed shifts one slot, closing the gap behind
   * it and opening one in front. Each shift is the distance between two
   * *measured* slots, so a card pushed off the end of a row lands at the start
   * of the next one with no arithmetic here about how the grid wraps.
   */
  function preview(drag) {
    drag.rows.forEach(function (row, i) {
      if (i === drag.index) return;
      var to = i;
      if (drag.index < drag.target && i > drag.index && i <= drag.target) to = i - 1;
      else if (drag.target < drag.index && i >= drag.target && i < drag.index) to = i + 1;
      if (to === i) {
        row.style.transform = "";
        return;
      }
      var from = drag.slots[i];
      var dest = drag.slots[to];
      row.style.transform = "translate(" + (dest.x - from.x) + "px," + (dest.y - from.y) + "px)";
    });
  }

  // -- the gesture ---------------------------------------------------------

  var drag = null;

  /* Read the pointer against the frozen slots and redraw.
   *
   * Separate from the move handler because the autoscroll needs it too: the
   * page moving under a stationary finger changes where that finger is on the
   * board, and a preview that only updated on `pointermove` would sit frozen
   * while the board slid past it.
   */
  function track() {
    if (!drag) return;
    var px = drag.pointerX + window.scrollX;
    var py = drag.pointerY + window.scrollY;
    drag.item.style.transform =
      "translate(" + (px - drag.grabX) + "px," + (py - drag.grabY) + "px)";
    var target = slotAt(drag.slots, px, py);
    if (target !== drag.target) {
      drag.target = target;
      preview(drag);
    }
  }

  function autoscroll() {
    if (!drag) return;
    var step = 0;
    if (drag.pointerY < EDGE) step = -SPEED;
    else if (drag.pointerY > window.innerHeight - EDGE) step = SPEED;
    if (step) {
      var before = window.scrollY;
      window.scrollBy(0, step);
      // Only redraw when the page actually went somewhere. At the top or the
      // bottom of the document it does not, and re-running the preview every
      // frame for no movement is work with nothing to show for it.
      if (window.scrollY !== before) track();
    }
    drag.frame = window.requestAnimationFrame(autoscroll);
  }

  document.addEventListener("pointerdown", function (event) {
    // Only a primary press. A right-click, or a second finger arriving
    // mid-drag, is not somebody starting to move a card.
    if (event.button !== 0 || drag) return;
    // A press on the scrollbar targets the document, which has no `closest`.
    if (!event.target || !event.target.closest) return;
    var grip = event.target.closest("[data-board-grip]");
    if (!grip) return;
    var item = grip.closest("[data-board-item]");
    var board = grip.closest("[data-board]");
    if (!item || !board) return;
    var rows = items(board);
    var index = rows.indexOf(item);
    if (index < 0) return;

    drag = {
      board: board,
      item: item,
      rows: rows,
      slots: measure(rows),
      index: index,
      target: index,
      // Where the pointer took hold, so the card keeps its place under the
      // finger instead of jumping so that a corner meets it.
      grabX: event.pageX,
      grabY: event.pageY,
      pointerX: event.clientX,
      pointerY: event.clientY,
      moved: false,
      frame: 0,
    };

    // Captured on the grip so the pointer keeps reporting to it once the
    // finger has left the card it started on — which it does immediately.
    grip.setPointerCapture(event.pointerId);
    board.classList.add(SORTING);
    item.classList.add(LIFTED);
    drag.frame = window.requestAnimationFrame(autoscroll);
    // Otherwise the page scrolls under the finger instead of the card moving.
    // `touch-action: none` on the grip says the same to browsers that honour
    // it; this covers the rest.
    event.preventDefault();
  });

  document.addEventListener("pointermove", function (event) {
    if (!drag) return;
    drag.pointerX = event.clientX;
    drag.pointerY = event.clientY;
    drag.moved = true;
    track();
  });

  function finish() {
    if (!drag) return;
    var here = drag;
    drag = null;
    window.cancelAnimationFrame(here.frame);

    here.rows.forEach(function (row) {
      row.style.transform = "";
    });
    here.item.classList.remove(LIFTED);
    here.board.classList.remove(SORTING);

    if (!here.moved || here.target === here.index) return;

    // The one reflow of the whole gesture, into the arrangement the preview
    // has been showing. Re-appending every row in sequence rather than
    // computing an insertion point: the list is short, and "put them in this
    // order" cannot be off by one the way "insert before that one" can.
    var order = here.rows.slice();
    order.splice(here.index, 1);
    order.splice(here.target, 0, here.item);
    order.forEach(function (row) {
      here.board.appendChild(row);
    });

    refreshEnds(here.board);
    commit(here.board);
  }

  document.addEventListener("pointerup", finish);
  // A cancelled pointer — the browser claimed it for a gesture, the pen left
  // range — has still shown the reader an arrangement, so it is committed
  // rather than abandoned in a state the server does not know about.
  document.addEventListener("pointercancel", finish);

  document.documentElement.classList.add(READY);
})();
