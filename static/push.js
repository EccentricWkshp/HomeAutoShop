/*
 * Subscribing this browser to reminder notifications (SPEC §9.4).
 *
 * The permission prompt is asked for on a click, never on load. A prompt that
 * appears unbidden gets denied reflexively, and a denial in Chrome is sticky —
 * one badly timed prompt costs the feature permanently on that device.
 */
(function () {
  "use strict";

  var strings = JSON.parse(document.getElementById("push-strings").textContent);
  var status = document.getElementById("push-status");
  var button = document.getElementById("push-subscribe");

  function say(message) { status.textContent = message; }

  function supported() {
    return "serviceWorker" in navigator && "PushManager" in window && window.isSecureContext;
  }

  /* The applicationServerKey wants raw bytes, not the base64url the server sends. */
  function decodeKey(base64) {
    var padded = (base64 + "=".repeat((4 - base64.length % 4) % 4))
      .replace(/-/g, "+").replace(/_/g, "/");
    var raw = window.atob(padded);
    var bytes = new Uint8Array(raw.length);
    for (var i = 0; i < raw.length; i++) { bytes[i] = raw.charCodeAt(i); }
    return bytes;
  }

  function csrf() {
    var match = document.cookie.match(/(?:^|;\s*)csrftoken=([^;]+)/);
    return match ? decodeURIComponent(match[1]) : "";
  }

  function subscribe(key) {
    return navigator.serviceWorker.ready.then(function (registration) {
      return registration.pushManager.subscribe({
        // Required by every browser: a push service will not accept a
        // subscription that allows silent, invisible messages.
        userVisibleOnly: true,
        applicationServerKey: decodeKey(key)
      });
    }).then(function (subscription) {
      return fetch("/reminders/push/", {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-CSRFToken": csrf() },
        credentials: "same-origin",
        body: JSON.stringify({
          subscription: subscription.toJSON(),
          label: navigator.userAgentData && navigator.userAgentData.platform
            ? navigator.userAgentData.platform
            : "browser"
        })
      });
    }).then(function (response) {
      if (!response.ok) { throw new Error("refused"); }
      say(strings.subscribed);
      button.hidden = true;
    });
  }

  if (!supported()) {
    say(strings.unsupported);
    return;
  }

  fetch("/reminders/push/", { credentials: "same-origin" })
    .then(function (r) { return r.json(); })
    .then(function (data) {
      if (!data.available || !data.key) {
        say(strings.unavailable);
        return;
      }
      if (Notification.permission === "denied") {
        // Sticky, and not undoable from here. Say where it can be undone.
        say(strings.blocked);
        return;
      }
      say(strings.ready);
      button.hidden = false;
      button.addEventListener("click", function () {
        button.disabled = true;
        Notification.requestPermission().then(function (permission) {
          if (permission !== "granted") {
            say(strings.declined);
            button.disabled = false;
            return;
          }
          return subscribe(data.key).catch(function () {
            say(strings.failed);
            button.disabled = false;
          });
        });
      });
    })
    .catch(function () { say(strings.unavailable); });
})();
