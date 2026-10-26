// Small helpers: confirm before a status change, and dashboard auto-refresh for agents.
(function () {
  "use strict";

  var REFRESH_MS = 60000;
  var COLUMNS = ["title", "category", "priority", "status", "requester", "seat", "team", "due_at", "time_left"];

  function cell(text, className) {
    var td = document.createElement("td");
    td.textContent = text;
    if (className) td.className = className;
    return td;
  }

  function renderRows(table, tickets) {
    var tbody = table.querySelector("tbody");
    tbody.textContent = "";
    tickets.forEach(function (t) {
      var tr = document.createElement("tr");
      if (t.is_sos) tr.className = "sos-row";
      var first = document.createElement("td");
      var link = document.createElement("a");
      link.href = t.url;
      link.textContent = t.ticket_no;
      first.appendChild(link);
      if (t.is_sos) {
        var badge = document.createElement("span");
        badge.className = "badge-sos";
        badge.textContent = "SOS";
        first.appendChild(document.createTextNode(" "));
        first.appendChild(badge);
      }
      tr.appendChild(first);
      COLUMNS.forEach(function (key) {
        if (key === "status") {
          var td = document.createElement("td");
          var span = document.createElement("span");
          span.className = "status status-" + t.status;
          span.textContent = t.status;
          td.appendChild(span);
          tr.appendChild(td);
        } else if (key === "time_left") {
          tr.appendChild(cell(t.time_left, t.overdue ? "overdue" : ""));
        } else {
          tr.appendChild(cell(t[key] || (key === "team" || key === "seat" ? "—" : "")));
        }
      });
      tbody.appendChild(tr);
    });
    var empty = document.getElementById("ticket-empty");
    if (empty) empty.hidden = tickets.length > 0;
    table.hidden = tickets.length === 0;
  }

  function renderCounts(counts) {
    Object.keys(counts).forEach(function (key) {
      var el = document.querySelector('[data-count="' + key + '"]');
      if (el) el.textContent = counts[key];
    });
  }

  function refresh(table) {
    fetch(table.dataset.api + window.location.search, {
      credentials: "same-origin",
      headers: { Accept: "application/json" }
    })
      .then(function (resp) {
        if (!resp.ok) throw new Error("HTTP " + resp.status);
        return resp.json();
      })
      .then(function (data) {
        // A new SOS needs the full page (red banner), so reload instead of patching.
        var sosTile = document.querySelector('[data-count="sos"]');
        if (sosTile && data.counts.sos > Number(sosTile.textContent)) {
          window.location.reload();
          return;
        }
        renderRows(table, data.tickets);
        renderCounts(data.counts);
        var stamp = document.getElementById("refreshed");
        if (stamp) stamp.textContent = "Updated " + new Date().toLocaleTimeString();
      })
      .catch(function () { /* keep showing the last data; try again next tick */ });
  }

  document.addEventListener("DOMContentLoaded", function () {
    document.querySelectorAll("form[data-confirm]").forEach(function (form) {
      form.addEventListener("submit", function (event) {
        var select = form.querySelector("select[name=status]");
        var message = form.dataset.confirm.replace("{status}", select ? select.value : "");
        if (!window.confirm(message)) event.preventDefault();
      });
    });

    // Admin user form: the team only applies to agents.
    var roleSelect = document.querySelector("select[data-team-toggle]");
    var teamField = document.getElementById("team-field");
    if (roleSelect && teamField) {
      var syncTeam = function () { teamField.hidden = roleSelect.value !== "agent"; };
      roleSelect.addEventListener("change", syncTeam);
      syncTeam();
    }

    var table = document.getElementById("ticket-table");
    if (table && table.dataset.api) {
      setInterval(function () { refresh(table); }, REFRESH_MS);
    }
  });
})();
