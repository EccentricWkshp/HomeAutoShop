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
      on(input, "blur", function () {
        // Long enough for a click on a result to land first.
        window.setTimeout(close, 200);
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

  function start() {
    wireDependents(document);
    wireLabelledButtons(document);
    wireStatusForm(document);
    wireToolPickers(document);
    wireConfirms(document);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", start);
  } else {
    start();
  }
})();
