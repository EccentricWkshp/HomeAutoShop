/*
 * Service worker (SPEC §5.4, §9.4).
 *
 * The client is an **offline-capable cache with a write queue**, not a replica.
 * That distinction is the whole design, and this file only handles the read
 * half — the shell and recently-visited pages. The write queue lives in
 * offline.js, because a queued write has to survive this worker being replaced.
 *
 * What is cached:
 *   - the shell and the stylesheet, precached on install, cache-first;
 *   - pages and API reads, **network-first**, cached as a fallback for when
 *     the network is not there;
 *   - thumbnails. **Originals are never cached** — a phone with a hundred
 *     full-size photos of a bellhousing in its cache is a phone with no space.
 *
 * A GET that misses while offline renders the offline page rather than the
 * browser's error, so the app looks like it is waiting rather than broken.
 *
 * **Pages were stale-while-revalidate, and that was wrong for this app.** SWR
 * answers from the cache and refreshes behind the reader, which is right for
 * something whose content nobody in the room is editing. Every write here is
 * post-redirect-get, so the page that lands *immediately after adding a part*
 * is exactly the page SWR answers from a copy taken before it existed: the
 * form submits, the list comes back without the new row, and the only way to
 * see it is to reload. Reported as "it looks like nothing was saved", which is
 * a worse thing for a record-keeping application to look like than slow.
 *
 * So: the network decides, and the cache is what answers when there is no
 * network. Offline still works — that is P-7 and it is not negotiable — it is
 * only the priority between the two that changes, and it changes in the
 * direction of never showing somebody a page that predates their own edit.
 */
"use strict";

/*
 * Rewritten by `core.views.service_worker` before this file is served, to a
 * token that changes whenever a static asset does. The literal below is the
 * fallback and is only ever reached if that substitution fails.
 *
 * It has to change, and `"v1"` did not. `/static/` is cache-first (below), and
 * `activate` only deletes caches whose name lacks the current VERSION — so a
 * constant meant `shell-v1` was written once, on the first visit a browser
 * ever made, and served for the life of the installation. Every later release
 * of the stylesheet and of every script was fetched into a cache nobody read.
 * The symptom is an edit that works for the developer, works in a private
 * window, and is simply absent for everybody who had used the app before.
 *
 * The fallback is deliberately not `"v1"` any more, and that is what rescues a
 * browser already carrying the bug. A worker's own script is fetched from the
 * network rather than through its own `fetch` handler, so this file reaches an
 * installed worker even while that worker is serving a frozen copy of every
 * other asset — and `activate` then drops `shell-v1`, because the name no
 * longer carries the version. Without that the recovery cannot bootstrap: the
 * corrected registration lives in `offline.js`, and `offline.js` was one of
 * the files being served stale.
 */
var VERSION = "v2-unstamped";
var SHELL = "shell-" + VERSION;
var PAGES = "pages-" + VERSION;
var MEDIA = "media-" + VERSION;

var PRECACHE = ["/static/app.css", "/static/offline.html", "/static/manifest.webmanifest"];

self.addEventListener("install", function (event) {
  event.waitUntil(
    caches.open(SHELL).then(function (cache) {
      // addAll rejects the whole install if any one URL 404s, which would
      // leave the app with no worker at all. Each is added independently.
      return Promise.all(PRECACHE.map(function (url) {
        return cache.add(url).catch(function () { return null; });
      }));
    }).then(function () { return self.skipWaiting(); })
  );
});

self.addEventListener("activate", function (event) {
  event.waitUntil(
    caches.keys().then(function (names) {
      return Promise.all(names.map(function (name) {
        if (name.indexOf(VERSION) < 0) { return caches.delete(name); }
        return null;
      }));
    }).then(function () {
      /*
       * Retire a copy of this worker that an older version registered at
       * `/static/sw.js`, where the scope is a directory of stylesheets rather
       * than the site.
       *
       * Done here, by the worker itself, because it is the only party that can
       * still be reached. The corrected registration is in `offline.js` — and
       * a misscoped worker is serving `offline.js` from a frozen cache, so the
       * page can never learn better on its own. A worker's own script is
       * exempt from its own `fetch` handler, so this line arrives regardless,
       * and once it has unregistered, the next load fetches everything from
       * the network and the page registers `/sw.js` properly.
       */
      if (new URL(self.registration.scope).pathname !== "/") {
        return self.registration.unregister();
      }
      return self.clients.claim();
    })
  );
});

function isMedia(url) {
  return url.pathname.indexOf("/media/") === 0;
}

function isThumbnail(url) {
  return url.pathname.indexOf("/media/derivatives/") === 0;
}

self.addEventListener("fetch", function (event) {
  var request = event.request;
  if (request.method !== "GET") {
    /*
     * A write makes cached pages wrong, not merely old — the list this POST
     * just added a row to is sitting in PAGES without it. Dropping the whole
     * page cache is blunt and correct: it is a convenience cache, and the cost
     * of being wrong about it is showing somebody a record they just changed
     * as though they had not.
     *
     * **`respondWith`, and one fetch.** This branch used to call
     * `fetch(request.clone())` inside `waitUntil` and then `return` — and a
     * `fetch` handler that returns without responding hands the request back to
     * the browser, which then performs it itself. That is two POSTs on the wire
     * for every write in the application: the worker's, whose response was read
     * only to decide whether to clear a cache and was otherwise thrown away,
     * and the browser's, whose response the person actually saw. The clone was
     * there because the body can only be read once — which is the tell, since
     * nothing needs a second copy of a body it is not sending twice.
     *
     * Adding a vehicle added it twice. So did anything else that creates a row,
     * while an edit setting the same fields twice looked fine, which is why it
     * survived: the failure is invisible on every idempotent write and doubles
     * every other one. Two imports of the same order racing each other also
     * both found no provenance row and both inserted, which surfaced as a
     * unique-constraint error on the import screen.
     *
     * Responding with the fetch makes the worker's request *be* the request.
     * Offline it rejects, the page gets the browser's own network failure as it
     * would with no worker at all, the queue in offline.js takes `data-queue`
     * forms, and the cache is left alone — which is the one moment those cached
     * pages are the only pages there are.
     *
     * A navigation POST that redirects comes back `opaqueredirect` with a
     * status of 0, so `response.ok` is false on the most ordinary successful
     * write there is. It is named here rather than left to make the cache-clear
     * silently never happen.
     */
    event.respondWith(
      fetch(request).then(function (response) {
        if (response.ok || response.type === "opaqueredirect") {
          event.waitUntil(caches.delete(PAGES));
        }
        return response;
      })
    );
    return;
  }

  var url = new URL(request.url);
  if (url.origin !== self.location.origin) { return; }

  // Never cache an original. Thumbnails only.
  if (isMedia(url) && !isThumbnail(url)) { return; }

  if (isThumbnail(url) || url.pathname.indexOf("/static/") === 0) {
    event.respondWith(cacheFirst(request, isThumbnail(url) ? MEDIA : SHELL));
    return;
  }

  event.respondWith(networkFirst(request));
});

function cacheFirst(request, cacheName) {
  return caches.match(request).then(function (hit) {
    if (hit) { return hit; }
    return fetch(request).then(function (response) {
      if (response.ok) {
        var copy = response.clone();
        caches.open(cacheName).then(function (cache) { cache.put(request, copy); });
      }
      return response;
    });
  });
}

function networkFirst(request) {
  return fetch(request).then(function (response) {
    if (response.ok) {
      var copy = response.clone();
      caches.open(PAGES).then(function (cache) { cache.put(request, copy); });
    }
    return response;
  }).catch(function () {
    // Offline. A cached copy beats an error page; the offline page beats the
    // browser's dinosaur.
    return caches.match(request).then(function (hit) {
      return hit || caches.match("/static/offline.html");
    });
  });
}

/*
 * Background Sync. Where the browser supports it, a reconnect wakes the worker
 * and it tells every open tab to drain the queue — the tab owns the queue
 * because IndexedDB access and the CSRF token both live there.
 */
self.addEventListener("sync", function (event) {
  if (event.tag !== "homeautoshop-queue") { return; }
  event.waitUntil(
    self.clients.matchAll({ includeUncontrolled: true }).then(function (clients) {
      clients.forEach(function (client) { client.postMessage({ type: "drain-queue" }); });
    })
  );
});

/*
 * Web push for reminders (§9.4). The payload carries only a title and a body
 * the server already decided to send — never a vehicle's identity, because a
 * notification renders on a lock screen in front of whoever is standing there.
 */
self.addEventListener("push", function (event) {
  var data = {};
  try { data = event.data ? event.data.json() : {}; } catch (err) { data = {}; }
  event.waitUntil(
    self.registration.showNotification(data.title || "HomeAutoShop", {
      body: data.body || "",
      tag: data.tag || "homeautoshop",
      data: { url: data.url || "/" }
    })
  );
});

self.addEventListener("notificationclick", function (event) {
  event.notification.close();
  var target = (event.notification.data && event.notification.data.url) || "/";
  event.waitUntil(
    self.clients.matchAll({ type: "window", includeUncontrolled: true }).then(function (clients) {
      for (var i = 0; i < clients.length; i++) {
        if (clients[i].url.indexOf(target) >= 0 && "focus" in clients[i]) {
          return clients[i].focus();
        }
      }
      return self.clients.openWindow(target);
    })
  );
});
