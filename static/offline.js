/*
 * The offline write queue and its indicator (SPEC §5.4, §9.2).
 *
 * The rules this implements, from §5.4, and why each one is here:
 *
 * - **The client mints the id** (UUIDv7). That is what makes a replay
 *   idempotent: the server hits the primary key and treats it as success
 *   rather than writing a second copy. Every retry story downstream depends
 *   on this one decision.
 * - **Append-only writes always land.** Readings, notes, photos and time
 *   entries cannot conflict by construction, which is what makes losing a
 *   garage capture impossible rather than unlikely.
 * - **Mutable writes carry a revision** and may come back 409. A conflict is
 *   never auto-resolved and never silently dropped — it is kept and shown
 *   side by side for a person to settle.
 * - **The indicator is always visible and honest.** "3 waiting" with a way to
 *   look at them. A queue you cannot inspect is a queue you cannot trust, and
 *   a queue you cannot trust makes people re-enter everything by hand.
 * - **A write older than 14 days warns.** Silence at that point means
 *   something is wrong that will not fix itself.
 */
(function () {
  "use strict";

  var DB_NAME = "homeautoshop";
  var STORE = "queue";
  var CONFLICTS = "conflicts";
  var STALE_DAYS = 14;

  var strings = {};
  var element = null;
  var draining = false;

  // -- storage ------------------------------------------------------------

  function open() {
    return new Promise(function (resolve, reject) {
      var request = indexedDB.open(DB_NAME, 1);
      request.onupgradeneeded = function () {
        var db = request.result;
        if (!db.objectStoreNames.contains(STORE)) {
          db.createObjectStore(STORE, { keyPath: "client_id" });
        }
        if (!db.objectStoreNames.contains(CONFLICTS)) {
          db.createObjectStore(CONFLICTS, { keyPath: "client_id" });
        }
      };
      request.onsuccess = function () { resolve(request.result); };
      request.onerror = function () { reject(request.error); };
    });
  }

  function withStore(name, mode, fn) {
    return open().then(function (db) {
      return new Promise(function (resolve, reject) {
        var tx = db.transaction(name, mode);
        var result = fn(tx.objectStore(name));
        tx.oncomplete = function () { resolve(result && result.result); };
        tx.onerror = function () { reject(tx.error); };
      });
    });
  }

  function all(name) {
    return withStore(name, "readonly", function (store) { return store.getAll(); });
  }

  /*
   * UUIDv7: a 48-bit millisecond timestamp, then randomness. Time-ordered, so
   * the server's index stays local and the queue replays in capture order —
   * the same reason the server side uses it (SPEC §5.5).
   */
  function uuid7() {
    var bytes = new Uint8Array(16);
    crypto.getRandomValues(bytes);
    var now = Date.now();
    bytes[0] = (now / 1099511627776) & 0xff;
    bytes[1] = (now / 4294967296) & 0xff;
    bytes[2] = (now / 16777216) & 0xff;
    bytes[3] = (now / 65536) & 0xff;
    bytes[4] = (now / 256) & 0xff;
    bytes[5] = now & 0xff;
    bytes[6] = (bytes[6] & 0x0f) | 0x70;   // version 7
    bytes[8] = (bytes[8] & 0x3f) | 0x80;   // variant
    var hex = Array.prototype.map.call(bytes, function (b) {
      return ("0" + b.toString(16)).slice(-2);
    }).join("");
    return [hex.slice(0, 8), hex.slice(8, 12), hex.slice(12, 16),
            hex.slice(16, 20), hex.slice(20)].join("-");
  }

  // -- the queue ----------------------------------------------------------

  function enqueue(op, payload) {
    var entry = {
      client_id: uuid7(),
      op: op,
      payload: payload,
      queued_at: new Date().toISOString()
    };
    return withStore(STORE, "readwrite", function (store) { return store.put(entry); })
      .then(function () {
        render();
        requestDrain();
        return entry.client_id;
      });
  }

  function requestDrain() {
    if (navigator.onLine) { return drain(); }
    if ("serviceWorker" in navigator && "SyncManager" in window) {
      // Let the browser wake us on reconnect rather than polling a radio.
      navigator.serviceWorker.ready.then(function (reg) {
        return reg.sync.register("homeautoshop-queue");
      }).catch(function () { /* not supported; the online event covers it */ });
    }
    return Promise.resolve();
  }

  function csrf() {
    var match = document.cookie.match(/(?:^|;\s*)csrftoken=([^;]+)/);
    return match ? decodeURIComponent(match[1]) : "";
  }

  function drain() {
    if (draining || !navigator.onLine) { return Promise.resolve(); }
    draining = true;
    return all(STORE).then(function (items) {
      if (!items.length) { draining = false; return; }
      return fetch("/api/v1/sync/batch", {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-CSRFToken": csrf() },
        credentials: "same-origin",
        body: JSON.stringify({
          items: items.map(function (i) {
            return { client_id: i.client_id, op: i.op, payload: i.payload };
          })
        })
      }).then(function (response) {
        if (!response.ok) { throw new Error("batch rejected"); }
        return response.json();
      }).then(function (data) {
        return settle(items, data.results || []);
      });
    }).catch(function () {
      // Still offline, or the server is down. The queue is untouched, which
      // is the entire point of it.
    }).then(function () {
      draining = false;
      render();
    });
  }

  function settle(items, results) {
    var byId = {};
    results.forEach(function (r) { byId[r.client_id] = r; });
    return Promise.all(items.map(function (item) {
      var result = byId[item.client_id];
      if (!result) { return null; }
      if (result.status === 409) {
        // Kept, never dropped, never auto-merged. The operator decides.
        item.conflict = result;
        return withStore(CONFLICTS, "readwrite", function (s) { return s.put(item); })
          .then(function () {
            return withStore(STORE, "readwrite", function (s) { return s.delete(item.client_id); });
          });
      }
      if (result.status >= 400) {
        // A write the server will never accept. Moving it to the conflict
        // list is honest; leaving it to retry forever is not.
        item.conflict = result;
        return withStore(CONFLICTS, "readwrite", function (s) { return s.put(item); })
          .then(function () {
            return withStore(STORE, "readwrite", function (s) { return s.delete(item.client_id); });
          });
      }
      return withStore(STORE, "readwrite", function (s) { return s.delete(item.client_id); });
    }));
  }

  function discard(clientId) {
    return withStore(STORE, "readwrite", function (s) { return s.delete(clientId); })
      .then(function () {
        return withStore(CONFLICTS, "readwrite", function (s) { return s.delete(clientId); });
      })
      .then(render);
  }

  // -- the indicator ------------------------------------------------------

  function oldest(items) {
    var stamps = items.map(function (i) { return Date.parse(i.queued_at); })
      .filter(function (t) { return !isNaN(t); });
    return stamps.length ? Math.min.apply(null, stamps) : null;
  }

  function render() {
    if (!element) { return Promise.resolve(); }
    return Promise.all([all(STORE), all(CONFLICTS)]).then(function (both) {
      var queued = both[0] || [];
      var conflicts = both[1] || [];
      var count = queued.length + conflicts.length;

      element.hidden = count === 0 && navigator.onLine;
      element.classList.toggle("warn", conflicts.length > 0);

      var parts = [];
      if (!navigator.onLine) { parts.push(strings.offline); }
      if (queued.length) {
        parts.push(strings.waiting.replace("%(n)s", queued.length));
      }
      if (conflicts.length) {
        parts.push(strings.conflicts.replace("%(n)s", conflicts.length));
      }

      var since = oldest(queued);
      if (since && Date.now() - since > STALE_DAYS * 86400000) {
        parts.push(strings.stale);
        element.classList.add("warn");
      }
      if (!parts.length) { parts.push(strings.synced); }
      element.textContent = parts.join(" · ");
    });
  }

  // -- wiring -------------------------------------------------------------

  function init() {
    var config = document.getElementById("offline-strings");
    if (config) { strings = JSON.parse(config.textContent); }
    element = document.getElementById("sync-indicator");

    if ("serviceWorker" in navigator && window.isSecureContext) {
      navigator.serviceWorker.register("/static/sw.js").catch(function () { /* no worker, no cache */ });
      navigator.serviceWorker.addEventListener("message", function (event) {
        if (event.data && event.data.type === "drain-queue") { drain(); }
      });
    }

    window.addEventListener("online", function () { render(); drain(); });
    window.addEventListener("offline", render);

    /*
     * Forms marked `data-queue` post through the queue when offline and
     * normally when online. Opt-in rather than blanket: a form that must be
     * seen to have worked — signing in, taking a backup — should fail loudly
     * offline rather than sit in a queue looking successful.
     */
    document.addEventListener("submit", function (event) {
      var form = event.target;
      if (!form.dataset || !form.dataset.queue || navigator.onLine) { return; }
      event.preventDefault();
      var payload = {};
      new FormData(form).forEach(function (value, key) {
        if (key !== "csrfmiddlewaretoken") { payload[key] = value; }
      });
      enqueue(form.dataset.queue, payload).then(function () {
        form.reset();
        if (element) { element.focus(); }
      });
    });

    render();
    drain();
  }

  window.HomeAutoShop = { enqueue: enqueue, drain: drain, discard: discard, queued: function () {
    return Promise.all([all(STORE), all(CONFLICTS)]);
  } };

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
