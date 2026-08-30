/*
 * The queue inspector and conflict merge (SPEC §5.4).
 *
 * "The client shows a persistent queue indicator — N changes waiting to sync —
 * tappable to inspect or discard individual queued writes." This is the tappable
 * part, and the side-by-side merge for the writes that came back 409.
 *
 * The merge is deliberately a choice between two whole versions rather than a
 * field-by-field editor. A conflict here is one person's odometer reading
 * against another's, or a status change against a status change — small,
 * whole facts where "mine or theirs" is the real question. A field-level merge
 * UI would be more machinery in service of a decision nobody wants to make
 * that finely.
 */
(function () {
  "use strict";

  var strings = JSON.parse(document.getElementById("queue-strings").textContent);
  var queueList = document.getElementById("queue-list");
  var conflictList = document.getElementById("conflict-list");

  function describe(item) {
    var known = strings.ops[item.op];
    return known || item.op;
  }

  function summarize(payload) {
    return Object.keys(payload || {}).map(function (key) {
      return key + ": " + payload[key];
    }).join(", ");
  }

  function age(item) {
    var when = Date.parse(item.queued_at);
    if (isNaN(when)) { return ""; }
    var days = Math.floor((Date.now() - when) / 86400000);
    if (days < 1) { return strings.today; }
    return strings.daysAgo.replace("%(n)s", days);
  }

  function row(item, options) {
    var card = document.createElement("article");
    card.className = "row spread";

    var left = document.createElement("div");
    var title = document.createElement("strong");
    title.textContent = describe(item);
    left.appendChild(title);

    var detail = document.createElement("p");
    detail.className = "small muted";
    detail.textContent = summarize(item.payload) + " · " + age(item);
    left.appendChild(detail);

    if (options.conflict && item.conflict) {
      var why = document.createElement("p");
      why.className = "small warn";
      why.textContent = item.conflict.status === 409
        ? strings.changedElsewhere
        : (item.conflict.detail || strings.refused);
      left.appendChild(why);
    }
    card.appendChild(left);

    var actions = document.createElement("div");
    actions.className = "row";

    if (options.conflict) {
      var keepMine = document.createElement("button");
      keepMine.type = "button";
      keepMine.className = "primary";
      keepMine.textContent = strings.keepMine;
      keepMine.addEventListener("click", function () { retry(item); });
      actions.appendChild(keepMine);
    }

    var drop = document.createElement("button");
    drop.type = "button";
    drop.className = "linkish small";
    drop.textContent = options.conflict ? strings.keepTheirs : strings.discard;
    drop.addEventListener("click", function () {
      if (!window.confirm(strings.confirmDiscard)) { return; }
      window.HomeAutoShop.discard(item.client_id).then(render);
    });
    actions.appendChild(drop);

    card.appendChild(actions);
    return card;
  }

  /*
   * "Keep mine" re-queues the write with the revision the server just reported,
   * which is what makes it land this time. It is an overwrite, and the button
   * says so — the alternative, retrying with the stale revision, would 409
   * forever and look like a broken button.
   */
  function retry(item) {
    if (!window.confirm(strings.confirmOverwrite)) { return; }
    var payload = Object.assign({}, item.payload);
    if (item.conflict && item.conflict.current_revision) {
      payload.revision = item.conflict.current_revision;
    }
    window.HomeAutoShop.discard(item.client_id)
      .then(function () { return window.HomeAutoShop.enqueue(item.op, payload); })
      .then(function () { return window.HomeAutoShop.drain(); })
      .then(render);
  }

  function fill(container, items, options) {
    container.innerHTML = "";
    if (!items.length) {
      var empty = document.createElement("p");
      empty.className = "muted";
      empty.textContent = options.emptyText;
      container.appendChild(empty);
      return;
    }
    items.forEach(function (item) { container.appendChild(row(item, options)); });
  }

  function render() {
    return window.HomeAutoShop.queued().then(function (both) {
      fill(queueList, both[0] || [], { emptyText: strings.nothingQueued });
      fill(conflictList, both[1] || [], { conflict: true, emptyText: strings.noConflicts });
    });
  }

  document.getElementById("queue-drain").addEventListener("click", function () {
    window.HomeAutoShop.drain().then(render);
  });

  render();
})();
