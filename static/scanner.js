/*
 * Camera barcode scanning (SPEC FR-VEH-5, FR-INV-2, FR-INV-3).
 *
 * Decoding happens **on the device** via the browser's own `BarcodeDetector`.
 * Nothing is uploaded, no frame leaves the phone, and it works with the WAN
 * unplugged — which is the only version of this feature consistent with P-1.
 *
 * `BarcodeDetector` is Chromium-only (Chrome and Edge, desktop and Android) and
 * needs a secure context. Safari and Firefox have not implemented it. Both
 * requirements are stated on the button rather than discovered as a control
 * that silently does nothing.
 *
 * Any element with `data-scan` opens the camera; the decoded value is handed to
 * the field named by `data-scan-target`, or to the URL in `data-scan-post`.
 */
(function () {
  "use strict";

  var strings = {};
  var node = document.getElementById("scanner-strings");
  if (node) { strings = JSON.parse(node.textContent); }

  var dialog = null;
  var video = null;
  var status = null;
  var stream = null;
  var running = false;

  function supported() {
    return "BarcodeDetector" in window && window.isSecureContext;
  }

  /*
   * A door-jamb VIN barcode is Code 39, and what comes off it is rarely just
   * the VIN: some labels prefix it with `I`, some append a checksum, some wrap
   * it in asterisks (Code 39's own start/stop characters). So the payload is
   * searched for something VIN-shaped rather than trusted whole.
   */
  function vinFrom(raw) {
    var cleaned = String(raw || "").toUpperCase().replace(/[^A-Z0-9]/g, "");
    // I, O and Q never appear in a VIN, so a run containing one is not one.
    var match = cleaned.match(/[A-HJ-NPR-Z0-9]{17}/);
    return match ? match[0] : cleaned;
  }

  function build() {
    if (dialog) { return; }
    dialog = document.createElement("dialog");
    dialog.className = "scanner";
    dialog.innerHTML =
      '<video playsinline muted aria-label="' + (strings.viewfinder || "Camera") + '"></video>' +
      '<p class="small" role="status" aria-live="polite"></p>' +
      '<button type="button" class="btn">' + (strings.cancel || "Cancel") + "</button>";
    document.body.appendChild(dialog);
    video = dialog.querySelector("video");
    status = dialog.querySelector("p");
    dialog.querySelector("button").addEventListener("click", close);
    dialog.addEventListener("cancel", close);
  }

  function close() {
    running = false;
    if (stream) {
      stream.getTracks().forEach(function (track) { track.stop(); });
      stream = null;
    }
    if (dialog && dialog.open) { dialog.close(); }
  }

  function open(formats, onResult) {
    build();
    status.textContent = strings.starting || "";
    dialog.showModal();

    // `environment` is the rear camera. On a phone held up to a door jamb the
    // selfie camera is never the one wanted.
    navigator.mediaDevices.getUserMedia({
      video: { facingMode: { ideal: "environment" } }, audio: false
    }).then(function (media) {
      stream = media;
      video.srcObject = media;
      return video.play();
    }).then(function () {
      var detector = new window.BarcodeDetector({ formats: formats });
      running = true;
      status.textContent = strings.looking || "";
      tick(detector, onResult);
    }).catch(function (err) {
      status.textContent = (err && err.name === "NotAllowedError")
        ? (strings.denied || String(err))
        : (strings.noCamera || String(err));
    });
  }

  function tick(detector, onResult) {
    if (!running) { return; }
    detector.detect(video).then(function (codes) {
      if (!running) { return; }
      if (codes.length) {
        var value = codes[0].rawValue;
        close();
        onResult(value);
        return;
      }
      // ~10 fps. Detection is the expensive part and running it every frame
      // heats the phone without finding anything sooner.
      setTimeout(function () { tick(detector, onResult); }, 100);
    }).catch(function () {
      setTimeout(function () { tick(detector, onResult); }, 250);
    });
  }

  function handle(button, value) {
    var targetName = button.dataset.scanTarget;
    if (targetName) {
      var field = document.querySelector('[name="' + targetName + '"]');
      if (field) {
        field.value = button.dataset.scanKind === "vin" ? vinFrom(value) : value.trim();
        // Anything already watching the field — the live VIN check on the
        // vehicle form — should see this as a real edit.
        field.dispatchEvent(new Event("input", { bubbles: true }));
        field.focus();
      }
      return;
    }

    /*
     * Our own labels encode a URL on this origin. Following it is the whole
     * point of a bin label, and refusing anything off-origin means a scanned
     * sticker from somewhere else cannot send somebody to a strange site.
     */
    var lookup = button.dataset.scanPost;
    if (lookup) {
      window.location = lookup + (lookup.indexOf("?") < 0 ? "?" : "&") +
        "code=" + encodeURIComponent(value.trim());
      return;
    }

    try {
      var url = new URL(value, window.location.origin);
      if (url.origin === window.location.origin) {
        window.location = url.pathname + url.search;
        return;
      }
    } catch (err) { /* not a URL; fall through */ }
    window.alert(strings.notOurs || value);
  }

  document.addEventListener("click", function (event) {
    var button = event.target.closest("[data-scan]");
    if (!button) { return; }
    event.preventDefault();

    if (!supported()) {
      window.alert(strings.unsupported || "");
      return;
    }
    var formats = (button.dataset.scan || "qr_code").split(/[,\s]+/).filter(Boolean);
    open(formats, function (value) { handle(button, value); });
  });

  /* Buttons say so up front rather than failing when pressed. */
  document.addEventListener("DOMContentLoaded", function () {
    if (supported()) { return; }
    document.querySelectorAll("[data-scan]").forEach(function (button) {
      button.disabled = true;
      button.title = strings.unsupported || "";
    });
  });
})();
