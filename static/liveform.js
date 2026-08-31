/*
 * Posting a form without losing your place.
 *
 * Every action in this application is a form POST, a redirect, and a fresh page.
 * That is the right foundation — it works with no script, it survives a dropped
 * connection, and the back button means what it says — but it has one cost that
 * is felt constantly: ticking the fourth job item on a long work order reloads
 * the page and puts you back at the top, looking for where you were. On a phone
 * held in one hand, that is the whole interaction.
 *
 * So: when a form inside a `[data-live]` region is submitted, post it in the
 * background and replace **only that region** with the same region from the
 * response.
 *
 * The load-bearing decision is that the server is not asked for anything new.
 * It answers the same full page it always did, and this picks the region out of
 * it. Two consequences, both of which are why it is built this way:
 *
 *   - the enhanced path and the no-script path render *byte-identical* HTML,
 *     from the same template, so they cannot drift apart the way a page and its
 *     hand-written JSON partial always eventually do;
 *   - with this file blocked, or on a browser without `fetch`, every form still
 *     posts, still redirects, and still works. Nothing here is load-bearing.
 *
 * It is not a general router. Anything that leaves the page — a delete that
 * redirects to a list, a form that lands somewhere else — is detected and
 * followed as a normal navigation, because pretending otherwise would leave
 * somebody on a page that no longer exists.
 */
(function () {
  "use strict";

  if (!window.fetch || !window.DOMParser || !window.FormData) return;

  var LIVE = "[data-live]";

  function regionName(element) {
    return element.getAttribute("data-live");
  }

  /* The messages list is a live region, so its *element* is kept and only its
     contents are replaced. Swapping the `<ul>` itself would hand the screen
     reader a brand new region it has never been watching, and the message
     would pass in silence. */
  function updateMessages(doc) {
    var here = document.querySelector(".messages");
    var there = doc.querySelector(".messages");
    if (!here) return;
    here.innerHTML = there ? there.innerHTML : "";
  }

  /* Focus falls to `<body>` when the element holding it is replaced, which
     drops a keyboard user at the top of the document — the exact problem this
     is here to solve, arriving by a different route. Landing on the region
     keeps them where the change happened. */
  function keepFocus(region, hadFocus) {
    if (!hadFocus) return;
    if (region.contains(document.activeElement)) return;
    region.setAttribute("tabindex", "-1");
    region.focus({ preventScroll: true });
  }

  function replaceRegion(region, doc) {
    var name = regionName(region);
    var fresh = doc.querySelector('[data-live="' + name + '"]');
    if (!fresh) return false;
    var hadFocus = region.contains(document.activeElement);
    region.innerHTML = fresh.innerHTML;
    // The markup is new, so nothing in it is wired: `forms.js` binds to
    // elements, and the ones it bound to have just been thrown away. Without
    // this the part picker inside a swapped region stops searching and the
    // delete button beside it stops confirming — enhancements that quietly
    // lapse partway through using the page, which is worse than never having
    // been there.
    if (window.homeautoshop && window.homeautoshop.enhance) {
      window.homeautoshop.enhance(region);
    }
    updateMessages(doc);
    keepFocus(region, hadFocus);
    return true;
  }

  function busy(form, on) {
    form.querySelectorAll("button, input[type=submit]").forEach(function (button) {
      button.disabled = on;
    });
  }

  function submit(form, region) {
    var body = new FormData(form);
    var action = form.getAttribute("action") || window.location.href;
    busy(form, true);

    fetch(action, {
      method: "POST",
      body: body,
      credentials: "same-origin",
      headers: { "X-Requested-With": "fetch" },
      redirect: "follow",
    })
      .then(function (response) {
        // The action moved us somewhere else — a delete that returns to a list,
        // or a form whose success page is a different screen. Go there properly
        // rather than splicing one page's region into another's.
        var landed = new URL(response.url, window.location.href);
        if (landed.pathname !== window.location.pathname) {
          window.location.href = response.url;
          return null;
        }
        return response.text();
      })
      .then(function (html) {
        if (html === null) return;
        var doc = new DOMParser().parseFromString(html, "text/html");
        if (!replaceRegion(region, doc)) {
          // The region is gone from the response, so the page is no longer the
          // shape this assumed. A reload is the honest answer.
          window.location.reload();
          return;
        }
        busy(form, false);
      })
      .catch(function () {
        // Offline, or the request failed. Submit it the ordinary way so the
        // browser's own error handling and the offline queue both apply.
        busy(form, false);
        form.submit();
      });
  }

  document.addEventListener("submit", function (event) {
    // `wireConfirms` in forms.js listens on the form itself, which bubbles to
    // here — so a cancelled confirmation has already stopped this.
    if (event.defaultPrevented) return;

    var form = event.target;
    if (!form || form.tagName !== "FORM") return;
    if ((form.method || "").toLowerCase() !== "post") return;
    if (form.hasAttribute("data-no-live")) return;

    var region = form.closest(LIVE);
    if (!region) return;

    event.preventDefault();
    submit(form, region);
  });
})();
