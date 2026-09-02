/*
 * Form behaviour that makes a page readable, and nothing that makes it work.
 *
 * Every one of these is an enhancement over markup that is already correct
 * without it: with this file blocked, dependent settings stay visible, the
 * status form shows both of its fields, the tool picker is a text box the
 * server resolves, and the delete button deletes. Nothing here is a check —
 * the server validates everything it did before.
 */
(function () {
  "use strict";

  /* ---------------------------------------------------------------- helpers */

  function isOn(control) {
    if (!control) return true;
    if (control.type === "checkbox") return control.checked;
    return String(control.value || "").trim() !== "";
  }

  function on(element, event, handler) {
    if (element) element.addEventListener(event, handler);
  }

  /* What the currently chosen part says about itself.
   *
   * Two shapes answer this, and both are a chooser: a `<select>` carries the
   * fact on the selected `<option>`, and a search picker carries it on the
   * hidden input holding the id, written there when a result is clicked. The
   * boxes downstream — how big a step, which units convert — should not have
   * to know which kind of chooser they are standing next to. */
  function chosen(source, key) {
    if (source.tagName === "SELECT") {
      var option = source.options[source.selectedIndex];
      return option ? option.getAttribute("data-" + key) : null;
    }
    return source.getAttribute("data-" + key);
  }

  /* --------------------------------------------------- dependent settings
   * A field whose parent switch is off does nothing. Showing it anyway makes
   * the reader work out which of forty controls are currently live.
   */
  function wireDependents(root) {
    var children = root.querySelectorAll("[data-child-of]");
    var parents = {};

    children.forEach(function (child) {
      var id = child.getAttribute("data-child-of");
      var parent = document.getElementById(id);
      if (!parent) return;
      (parents[id] = parents[id] || { control: parent, children: [] }).children.push(child);
    });

    Object.keys(parents).forEach(function (id) {
      var group = parents[id];
      var apply = function () {
        var live = isOn(group.control);
        group.children.forEach(function (child) {
          child.hidden = !live;
          // Hidden fields must not be validated by the browser, or a required
          // one nobody can see blocks submission with no visible reason.
          child.querySelectorAll("input, select, textarea").forEach(function (input) {
            input.disabled = !live;
          });
        });
      };
      apply();
      on(group.control, "change", apply);
      on(group.control, "input", apply);
    });
  }

  /* ------------------------------------------------------- a role-aware button
   * "Add owner" was shown whatever role was selected, so adding a primary
   * driver announced it was adding an owner.
   */
  function wireLabelledButtons(root) {
    root.querySelectorAll("button[data-label-from]").forEach(function (button) {
      var select = document.getElementById(button.getAttribute("data-label-from"));
      if (!select) return;
      var template = button.getAttribute("data-label-template") || "%(role)s";
      var apply = function () {
        var option = select.options[select.selectedIndex];
        if (option) button.textContent = template.replace("%(role)s", option.text);
      };
      apply();
      on(select, "change", apply);
    });
  }

  /* ------------------------------------------------ required fields, in advance
   * The status form refused a transition for a field it had never marked as
   * needed. Which target needs which field comes from the server.
   */
  function wireStatusForm(root) {
    root.querySelectorAll("[data-status-form]").forEach(function (form) {
      var payload = document.getElementById(form.getAttribute("data-requirements"));
      var status = form.querySelector('select[name="status"]');
      if (!payload || !status) return;

      var requirements;
      try {
        requirements = JSON.parse(payload.textContent);
      } catch (error) {
        return;
      }

      var fields = form.querySelectorAll("[data-required-for]");
      var apply = function () {
        var needed = requirements[status.value];
        fields.forEach(function (field) {
          var name = field.getAttribute("data-required-for");
          var input = field.querySelector("input, textarea");
          var marker = field.querySelector(".req");
          var wanted = name === needed;
          // Shown when it is needed, and also when it already holds something
          // — hiding a value somebody typed would look like losing it.
          var filled = input && String(input.value || "").trim() !== "";
          field.hidden = !wanted && !filled;
          if (input) input.required = wanted;
          if (marker) marker.hidden = !wanted;
        });
      };
      apply();
      on(status, "change", apply);
    });
  }

  /* ------------------------------------------------------------- tool search
   * Typing a WrenchLedger id from memory is not a lookup.
   */
  function wireToolPickers(root) {
    root.querySelectorAll(".toolpicker[data-search]").forEach(function (form) {
      var input = form.querySelector('input[name="tool_query"]');
      var idField = form.querySelector('input[name="tool_id"]');
      var nameField = form.querySelector('input[name="tool_name"]');
      var list = form.querySelector(".results");
      if (!input || !list) return;

      var timer = null;
      var lastQuery = "";

      var close = function () {
        list.hidden = true;
        list.textContent = "";
      };

      var choose = function (tool) {
        if (idField) idField.value = tool.id;
        if (nameField) nameField.value = tool.name;
        input.value = tool.name;
        close();
        form.submit();
      };

      var render = function (payload) {
        list.textContent = "";
        if (!payload.results.length) {
          close();
          return;
        }
        payload.results.forEach(function (tool) {
          var item = document.createElement("li");
          var button = document.createElement("button");
          button.type = "button";
          button.className = "linkish";
          button.textContent = tool.name;
          if (tool.detail) {
            var detail = document.createElement("span");
            detail.className = "muted";
            detail.textContent = " " + tool.detail;
            button.appendChild(detail);
          }
          button.addEventListener("click", function () {
            choose(tool);
          });
          item.appendChild(button);
          list.appendChild(item);
        });
        list.hidden = false;
      };

      var search = function () {
        var query = input.value.trim();
        // Typing into the box after picking something means the pick is stale.
        if (idField) idField.value = "";
        if (query.length < 2 || query === lastQuery) {
          if (query.length < 2) close();
          return;
        }
        lastQuery = query;
        fetch(form.getAttribute("data-search") + "?q=" + encodeURIComponent(query), {
          headers: { Accept: "application/json" },
        })
          .then(function (response) {
            return response.ok ? response.json() : { results: [] };
          })
          .then(render)
          // A search that cannot reach the server is not an error worth
          // shouting about: the box still submits and the server resolves it.
          .catch(close);
      };

      on(input, "input", function () {
        window.clearTimeout(timer);
        timer = window.setTimeout(search, 250);
      });
      // Same reasoning as the part picker below: blurring the input to reach
      // the first result must not delete it.
      on(form, "focusout", function () {
        window.setTimeout(function () {
          if (!form.contains(document.activeElement)) close();
        }, 150);
      });
    });
  }

  /* ------------------------------------------------------------- part search
   * The chooser this replaces was a `<select>` holding every part ever bought,
   * which is a control that gets worse the more the application is used. This
   * asks the server instead, so the number of parts stops being the reader's
   * problem — and it asks *before* anything is typed too, because the useful
   * default is a shortlist of what fits this vehicle and what is on the shelf,
   * not the first eight rows of a table.
   *
   * Unlike the tool picker, choosing here does not submit: there is a quantity
   * beside it, and often a job item, and submitting on the first click would
   * take the choice and throw away the rest of the form.
   */
  function wirePartPickers(root) {
    root.querySelectorAll("[data-part-search]").forEach(function (picker) {
      var input = picker.querySelector('input[name="part_query"]');
      var idField = picker.querySelector('input[name="part"]');
      var list = picker.querySelector(".results");
      var nomatch = picker.querySelector(".nomatch");
      var newLink = picker.querySelector("[data-new-part]");
      if (!input || !idField || !list) return;

      var endpoint = picker.getAttribute("data-part-search");
      var timer = null;
      var lastQuery = null;

      var close = function () {
        list.hidden = true;
        list.textContent = "";
      };

      var offerToAdd = function (query) {
        if (!nomatch) return;
        // Only worth offering for something somebody actually typed: an empty
        // shortlist on a new install means "add some parts", which the parts
        // screen says better than a line under a search box.
        var wanted = query.length >= 2;
        nomatch.hidden = !wanted;
        if (wanted && newLink) {
          var base = newLink.getAttribute("href").split("?")[0];
          newLink.href = base + "?name=" + encodeURIComponent(query);
          newLink.textContent = newLink.getAttribute("data-template")
            ? newLink.getAttribute("data-template").replace("%s", query)
            : newLink.textContent;
        }
      };

      var choose = function (part) {
        idField.value = part.id;
        // The quantity box and the unit picker read their settings from here,
        // exactly as they read them from a chosen `<option>` before.
        idField.setAttribute("data-step", part.step);
        idField.setAttribute("data-units", (part.units || []).join(","));
        input.value = part.name;
        input.setAttribute("data-picked", part.name);
        // Forget what was last asked, or retyping the same search after a
        // change of mind is answered with the early return below and no list.
        lastQuery = null;
        close();
        if (nomatch) nomatch.hidden = true;
        idField.dispatchEvent(new Event("change", { bubbles: true }));
      };

      var render = function (payload, query) {
        list.textContent = "";
        var results = payload.results || [];
        if (!results.length) {
          close();
          offerToAdd(query);
          return;
        }
        if (nomatch) nomatch.hidden = true;
        results.forEach(function (part) {
          // A plain list item: `role="option"` outside a listbox is an
          // invalid claim, and the list is not one — see the note in the
          // template about not promising combobox behaviour it lacks.
          var item = document.createElement("li");
          var button = document.createElement("button");
          button.type = "button";
          button.className = "linkish";
          button.appendChild(document.createTextNode(part.name));
          if (part.detail) {
            var detail = document.createElement("span");
            detail.className = "muted small";
            detail.textContent = " — " + part.detail;
            button.appendChild(detail);
          }
          button.addEventListener("click", function () {
            choose(part);
          });
          item.appendChild(button);
          list.appendChild(item);
        });
        list.hidden = false;
      };

      var ask = function () {
        var query = input.value.trim();
        // Typing after picking something means the pick is stale, and leaving
        // the old id in place would submit a part nobody is looking at.
        if (query !== input.getAttribute("data-picked")) idField.value = "";
        if (query.length === 1) {
          close();
          return;
        }
        if (query === lastQuery) return;
        lastQuery = query;

        var url = endpoint + "?q=" + encodeURIComponent(query);
        var asset = picker.getAttribute("data-asset");
        var exclude = picker.getAttribute("data-exclude");
        if (asset) url += "&asset=" + encodeURIComponent(asset);
        if (exclude) url += "&exclude=" + encodeURIComponent(exclude);

        fetch(url, { headers: { Accept: "application/json" } })
          .then(function (response) {
            return response.ok ? response.json() : { results: [] };
          })
          .then(function (payload) {
            render(payload, query);
          })
          // Unreachable is not an error worth shouting about: the box still
          // submits and the server resolves the name that was typed.
          .catch(close);
      };

      on(input, "input", function () {
        window.clearTimeout(timer);
        timer = window.setTimeout(ask, 250);
      });
      // The shortlist is the whole point, so it is shown on arrival rather
      // than waiting for somebody to guess at a first letter.
      on(input, "focus", function () {
        if (!input.value.trim()) {
          lastQuery = null;
          ask();
        }
      });
      // Closing on the input's own blur was a keyboard trap: tabbing to the
      // first result blurs the input, and a timer then deleted the button that
      // had just taken focus. So close only once focus has left the picker *as
      // a whole* — after a moment, which is both what lets a click on a result
      // land first and what gives focus time to arrive on the button.
      on(picker, "focusout", function () {
        window.setTimeout(function () {
          if (!picker.contains(document.activeElement)) close();
        }, 150);
      });
    });
  }

  /* --------------------------------------------------- quantity that suits the part
   * A quantity box beside a part picker cannot know, in the markup, whether it
   * is about gaskets or about litres of coolant. It defaults to whole ones —
   * right for nearly everything a shop counts, and the reason it does not offer
   * to record 0.003 of a gasket — and the chosen part relaxes it where the part
   * is genuinely measured out. Storage is three decimal places either way; this
   * only decides what the spinner does and what the browser will accept.
   */
  function wireQuantitySteps(root) {
    root.querySelectorAll("input[data-step-from]").forEach(function (input) {
      var select = document.getElementById(input.getAttribute("data-step-from"));
      if (!select) return;
      var floor = input.getAttribute("min");
      var apply = function () {
        var step = chosen(select, "step") || "1";
        input.step = step;
        // Only where the markup set one: a receiving box with no floor should
        // not acquire one here.
        if (floor !== null) input.min = step;
      };
      apply();
      on(select, "change", apply);
    });
  }

  /* ----------------------------------------------- the units the part uses
   * Which units apply depends on which part, and a quantity box beside a part
   * chooser cannot know that in the markup. So the chosen part carries its own
   * list — on the `<option>` where the chooser is a select, on the hidden field
   * where it is a search — and this fills the select beside it from whichever
   * is chosen.
   *
   * It starts empty and hidden, which is what makes this safe to skip: with no
   * script the field never reaches the server and the quantity is read in the
   * part's own unit, which the chooser's own results name. A counted part gets
   * no picker at all, because there is no factor between a gasket and a litre.
   */
  function wireUnitPickers(root) {
    root.querySelectorAll("select[data-units-from]").forEach(function (picker) {
      var parts = document.getElementById(picker.getAttribute("data-units-from"));
      if (!parts) return;
      var apply = function () {
        var units = (chosen(parts, "units") || "").split(",").filter(Boolean);
        picker.textContent = "";
        // One unit is not a choice, and a dropdown with a single option is a
        // control that asks a question with no answers.
        picker.hidden = units.length < 2;
        units.forEach(function (unit) {
          var entry = document.createElement("option");
          entry.value = unit;
          entry.textContent = unit;
          picker.appendChild(entry);
        });
      };
      apply();
      on(parts, "change", apply);
    });
  }

  /* ----------------------------------------------------------- upload forms
   * "Choose files" and "Add documents" read as two ways to do the same thing.
   * They are a sequence — the first opens a picker, the second sends what it
   * chose — and nothing on the screen said so, or said that anything had been
   * chosen at all. So the submit stays inert until there is something to send
   * and then says how much, which makes one control lead to the other instead
   * of competing with it.
   *
   * An enhancement, like everything else here: with this file blocked both
   * controls are present, labelled, and the form posts exactly as before.
   */
  function wireUploads(root) {
    root.querySelectorAll("form[data-upload]").forEach(function (form) {
      var inputs = form.querySelectorAll('input[type="file"]');
      var submit = form.querySelector("[data-upload-submit]");
      var readout = form.querySelector("[data-upload-chosen]");
      if (!inputs.length || !submit) return;

      var one = form.getAttribute("data-upload-one") || "";
      var many = form.getAttribute("data-upload-many") || "";
      var idle = submit.textContent;

      var apply = function () {
        var names = [];
        inputs.forEach(function (input) {
          Array.prototype.forEach.call(input.files || [], function (file) {
            names.push(file.name);
          });
        });
        submit.disabled = names.length === 0;
        var template = names.length === 1 ? one : many;
        submit.textContent =
          names.length && template ? template.replace("%(n)s", names.length) : idle;
        // The names, not just the count: seeing the file you meant is what
        // tells you the picker did anything at all.
        if (readout) readout.textContent = names.join(", ");
      };

      apply();
      inputs.forEach(function (input) {
        on(input, "change", apply);
      });
    });
  }

  /* ------------------------------------------------------ confirm before losing
   * Only where something disappears from view. A confirmation on everything
   * teaches people to dismiss confirmations.
   */
  function wireConfirms(root) {
    root.querySelectorAll("form[data-confirm]").forEach(function (form) {
      form.addEventListener("submit", function (event) {
        if (!window.confirm(form.getAttribute("data-confirm"))) {
          event.preventDefault();
        }
      });
    });
  }

  /* Run over a subtree rather than the whole document, because `liveform.js`
   * replaces a region's contents when a form in it is posted — and everything
   * above is bound to elements, so the replacements arrive unwired. Before
   * this, the second thing you did inside a live region met a tool picker that
   * had stopped searching and a delete button that had stopped confirming. */
  function enhance(root) {
    wireDependents(root);
    wireLabelledButtons(root);
    wireStatusForm(root);
    wireToolPickers(root);
    wirePartPickers(root);
    wireQuantitySteps(root);
    wireUnitPickers(root);
    wireUploads(root);
    wireConfirms(root);
  }

  window.homeautoshop = window.homeautoshop || {};
  window.homeautoshop.enhance = enhance;

  function start() {
    enhance(document);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", start);
  } else {
    start();
  }
})();
