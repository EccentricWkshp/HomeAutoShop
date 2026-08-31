/*
 * Enlarging a photo without leaving the record it belongs to.
 *
 * Like forms.js, everything here is an enhancement over markup that already
 * worked: each thumbnail is a link to its file, and with this script blocked —
 * or on a browser with no `<dialog>` — clicking one opens the file exactly as
 * it did before. Nothing below is load-bearing.
 *
 * Two decisions worth stating, because both were the other way first:
 *
 * * **Documents are not intercepted.** `_thumb.html` marks a tile with
 *   `data-lightbox` only when the file is a photograph. A PDF receipt has a
 *   picture — its rendered first page — and showing that picture is the wrong
 *   answer to the click: the reader wants the document, with its other pages
 *   and its text. Those tiles carry `target="_blank"` instead and this file
 *   never sees them.
 *
 * * **The group is the gallery, not the page.** Arrow keys move between the
 *   photos on one work order, not into the receipts further down. Where there
 *   is no gallery — the inspection screen hangs a single photo off each result
 *   row — the group is the page, which is what somebody stepping through an
 *   inspection actually wants.
 */
(function () {
  "use strict";

  var dialog = document.getElementById("lightbox");
  // No dialog element, or a browser too old for modal dialogs: leave every
  // link alone. `show()` without `showModal()` is not worth having — it is the
  // modality that brings the focus trap and the inert background with it, and
  // hand-rolling those is how a lightbox becomes a keyboard trap.
  if (!dialog || typeof dialog.showModal !== "function") return;

  var caption = document.getElementById("lightbox-caption");
  // Made here rather than in the template so that pages with no photographs on
  // them — the label sheet, where thirty QR codes are inline SVG and nothing is
  // fetched — carry no image element at all.
  var image = document.createElement("img");
  image.alt = "";
  caption.parentNode.insertBefore(image, caption);
  var full = document.getElementById("lightbox-full");
  var previous = document.getElementById("lightbox-prev");
  var next = document.getElementById("lightbox-next");
  var close = document.getElementById("lightbox-close");

  var group = [];
  var at = 0;
  //: What was clicked, so focus can go back to it. `<dialog>` restores focus
  //: on close by itself in current browsers; this is the belt to that braces,
  //: and costs one variable.
  var opener = null;

  function tiles(from) {
    // The gallery it sits in, or the page when it sits in none.
    var scope = from.closest(".thumbs") || document;
    return Array.prototype.slice.call(scope.querySelectorAll("a[data-lightbox]"));
  }

  function show(index) {
    var link = group[index];
    if (!link) return;
    at = index;
    image.src = link.getAttribute("data-lightbox");
    // The caption is the alt text. One string: a photo described one way to a
    // sighted reader and another to a screen reader is two descriptions to
    // keep true, and the second one silently rots.
    var text = link.getAttribute("data-lightbox-caption") || "";
    image.alt = text;
    caption.textContent = text;
    // The original, not the preview — the reason to leave the lightbox is to
    // see the full-resolution file, and `href` is where the tile pointed.
    full.href = link.getAttribute("href");

    var many = group.length > 1;
    previous.hidden = !many;
    next.hidden = !many;
  }

  function step(by) {
    if (group.length < 2) return;
    // Wraps, because reaching the end of a gallery and finding the button dead
    // reads as a bug. `+ length` keeps the modulo positive going backwards.
    show((at + by + group.length) % group.length);
  }

  function open(link) {
    group = tiles(link);
    opener = link;
    var index = group.indexOf(link);
    show(index < 0 ? 0 : index);
    dialog.showModal();
  }

  document.addEventListener("click", function (event) {
    var link = event.target.closest ? event.target.closest("a[data-lightbox]") : null;
    if (!link) return;
    // Anything that means "give me this in its own tab" is left to the
    // browser: a middle click, a modified click, and whatever the user's
    // platform uses to open in the background.
    if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) {
      return;
    }
    event.preventDefault();
    open(link);
  });

  previous.addEventListener("click", function () { step(-1); });
  next.addEventListener("click", function () { step(1); });
  close.addEventListener("click", function () { dialog.close(); });

  dialog.addEventListener("keydown", function (event) {
    if (event.key === "ArrowLeft") { event.preventDefault(); step(-1); }
    if (event.key === "ArrowRight") { event.preventDefault(); step(1); }
    // Escape needs nothing: `showModal()` already closes on it.
  });

  // Clicking the backdrop closes. The backdrop is not its own element, so the
  // test is whether the click landed on the dialog itself rather than on
  // anything inside it — which is true only outside the figure and the bar.
  dialog.addEventListener("click", function (event) {
    if (event.target === dialog) dialog.close();
  });

  dialog.addEventListener("close", function () {
    // Dropped so a closed lightbox is not holding a megabyte of decoded image,
    // and so reopening cannot flash the previous photo before the new one
    // arrives. `removeAttribute` rather than `src = ""`, which some browsers
    // treat as a request for the current page.
    image.removeAttribute("src");
    image.alt = "";
    caption.textContent = "";
    if (opener) { opener.focus(); opener = null; }
  });
})();
