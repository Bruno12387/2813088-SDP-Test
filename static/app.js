/* Repo Analysis Tool - dashboard front end.
   Talks to the Flask JSON API: repositories, job progress, metrics,
   authors and the commit picker. */
(function () {
  "use strict";

  function $(selector) { return document.querySelector(selector); }

  var state = {
    repos: [],
    repoId: null,
    tab: "dashboard",
    path: "",
    kind: "dir",
    groups: [],                 // author group keys of the current repo
    selectedGroups: new Set(),
    commitMode: "all",          // 'all' | 'range' | 'list'
    from: "",
    to: "",
    shas: [],
    modalShas: new Set(),
    authors: [],
    checkedAuthors: new Set(),
    children: [],
    sortKey: "churn",
    sortDir: -1,
    lastPayload: null,
    reqSeq: 0,
    commitOffset: 0,
    commitTotal: 0,
    commitQuery: "",
    toastTimer: null,
    searchTimer: null,
    resizeTimer: null,
  };

  // ------------------------------------------------------------- helpers --
  function fmtInt(value) {
    return Number(value || 0).toLocaleString("en-US");
  }

  function fmtRate(value) {
    return Number(value || 0).toLocaleString("en-US", {
      minimumFractionDigits: 2,
      maximumFractionDigits: 2,
    });
  }

  function shortStamp(ts) {
    return new Date(ts * 1000).toLocaleDateString(undefined, {
      month: "short", day: "numeric", year: "numeric",
    });
  }

  function dateToEpoch(value) {
    if (!value) return null;
    var millis = new Date(value).getTime();
    return Number.isFinite(millis) ? Math.floor(millis / 1000) : null;
  }

  function textCell(text) {
    var td = document.createElement("td");
    td.textContent = text;
    return td;
  }

  function numCell(text) {
    var td = document.createElement("td");
    td.className = "num";
    td.textContent = text;
    return td;
  }

  async function api(url, options) {
    var response = await fetch(url, options);
    var payload = null;
    try { payload = await response.json(); } catch (err) { payload = null; }
    if (!response.ok) {
      var message = payload && payload.error
        ? payload.error
        : "request failed (" + response.status + ")";
      throw new Error(message);
    }
    return payload;
  }

  function toast(message, isError) {
    var el = $("#toast");
    el.textContent = message;
    el.style.borderColor = isError ? "var(--danger)" : "var(--accent)";
    el.classList.remove("hidden");
    clearTimeout(state.toastTimer);
    state.toastTimer = setTimeout(function () {
      el.classList.add("hidden");
    }, 3500);
  }

  function banner(message) {
    $("#banner-text").textContent = message;
    $("#banner").classList.remove("hidden");
  }

  function currentRepo() {
    return state.repos.find(function (repo) {
      return repo.id === state.repoId;
    }) || null;
  }

  function commitFilterActive() {
    if (state.commitMode === "range") return Boolean(state.from || state.to);
    if (state.commitMode === "list") return state.shas.length > 0;
    return false;
  }

  // ------------------------------------------------------- repo sidebar ---
  async function loadRepos() {
    try {
      state.repos = (await api("/api/repos")).repos;
    } catch (err) {
      state.repos = [];
      banner("Could not load repositories: " + err.message);
    }
    var known = state.repos.some(function (repo) {
      return repo.id === state.repoId;
    });
    if (!known) {
      state.repoId = state.repos.length ? state.repos[0].id : null;
      state.lastPayload = null;
    }
    renderRepos();
    updateVisibility();
    if (state.repoId != null) {
      if (state.tab === "authors") loadAuthorsPage();
      refreshGroups().finally(function () { loadMetrics(); });
    }
  }

  function renderRepos() {
    var list = $("#repo-list");
    list.textContent = "";
    state.repos.forEach(function (repo) {
      var item = document.createElement("li");
      if (repo.id === state.repoId) item.classList.add("active");
      var name = document.createElement("span");
      name.className = "repo-name";
      name.textContent = repo.name;
      name.title = repo.source;
      var meta = document.createElement("span");
      meta.className = "repo-meta";
      meta.textContent = repo.kind + " \u00b7 " + fmtInt(repo.commit_count) +
        " commits";
      var remove = document.createElement("button");
      remove.type = "button";
      remove.className = "delete";
      remove.textContent = "\u00d7";
      remove.title = "Delete repository and its imported data";
      remove.addEventListener("click", function (event) {
        event.stopPropagation();
        deleteRepo(repo);
      });
      item.appendChild(name);
      item.appendChild(meta);
      item.appendChild(remove);
      item.addEventListener("click", function () { selectRepo(repo.id); });
      list.appendChild(item);
    });
  }

  function selectRepo(repoId) {
    if (state.repoId === repoId && state.lastPayload) return;
    state.repoId = repoId;
    state.path = "";
    state.kind = "dir";
    state.commitMode = "all";
    state.from = "";
    state.to = "";
    state.shas = [];
    state.lastPayload = null;
    resetCommitControls();
    renderRepos();
    updateVisibility();
    if (state.tab === "authors") loadAuthorsPage();
    refreshGroups().finally(function () { loadMetrics(); });
  }

  async function deleteRepo(repo) {
    if (!window.confirm('Delete "' + repo.name + '" and its imported data?')) {
      return;
    }
    try {
      await api("/api/repos/" + repo.id, { method: "DELETE" });
    } catch (err) {
      toast(err.message, true);
      return;
    }
    toast("Deleted " + repo.name);
    state.repos = state.repos.filter(function (item) {
      return item.id !== repo.id;
    });
    if (state.repoId === repo.id) {
      state.repoId = state.repos.length ? state.repos[0].id : null;
      state.lastPayload = null;
    }
    renderRepos();
    updateVisibility();
    if (state.repoId != null) {
      if (state.tab === "authors") loadAuthorsPage();
      refreshGroups().finally(function () { loadMetrics(); });
    } else {
      clearPanels();
    }
  }

  function clearPanels() {
    $("#cards").textContent = "";
    $("#breadcrumb").textContent = "";
    $("#children-body").textContent = "";
    $("#children-table").classList.add("hidden");
    $("#children-empty").classList.add("hidden");
    $("#object-authors-body").textContent = "";
    $("#object-authors-empty").classList.add("hidden");
    $("#treemap").textContent = "";
    $("#authors-body").textContent = "";
    state.children = [];
    RATCharts.renderChurn($("#churn-chart"), []);
    RATCharts.renderOwnership($("#ownership-chart"), []);
  }

  // --------------------------------------------------------- author filter -
  async function refreshGroups() {
    var box = $("#author-options");
    state.selectedGroups = new Set();
    var authors = [];
    if (state.repoId != null) {
      try {
        authors = (await api("/api/repos/" + state.repoId + "/authors"))
          .authors;
      } catch (err) {
        banner("Could not load author identities: " + err.message);
      }
    }
    var totals = new Map();
    authors.forEach(function (author) {
      totals.set(author.group_key,
        (totals.get(author.group_key) || 0) + author.commits);
    });
    state.groups = Array.from(totals.keys()).sort();

    box.textContent = "";
    if (!state.groups.length) {
      var note = document.createElement("p");
      note.className = "muted small";
      note.textContent = state.repoId == null
        ? "Select a repository first."
        : "No authors imported yet.";
      box.appendChild(note);
    }
    state.groups.forEach(function (key) {
      var label = document.createElement("label");
      label.className = "radio";
      var checkbox = document.createElement("input");
      checkbox.type = "checkbox";
      checkbox.addEventListener("change", function () {
        if (checkbox.checked) state.selectedGroups.add(key);
        else state.selectedGroups.delete(key);
        updateAuthorSummary();
        loadMetrics();
      });
      label.appendChild(checkbox);
      var text = document.createElement("span");
      text.textContent = " " + key + " (" + fmtInt(totals.get(key)) + ")";
      label.appendChild(text);
      box.appendChild(label);
    });
    updateAuthorSummary();
  }

  function updateAuthorSummary() {
    var selected = state.selectedGroups.size;
    $("#author-summary").textContent = selected === 0
      ? "all"
      : selected + " of " + state.groups.length;
  }

  // --------------------------------------------------------- commit filter -
  function resetCommitControls() {
    $("#from-input").value = "";
    $("#to-input").value = "";
    $("#range-inputs").classList.add("hidden");
    var all = document.querySelector('input[name="cmode"][value="all"]');
    if (all) all.checked = true;
    updateCommitSummary();
  }

  function updateCommitSummary() {
    var text;
    if (state.commitMode === "range") {
      var from = dateToEpoch(state.from);
      var to = dateToEpoch(state.to);
      if (from != null && to != null) {
        text = shortStamp(from) + " \u2013 " + shortStamp(to);
      } else if (from != null) {
        text = "from " + shortStamp(from);
      } else if (to != null) {
        text = "until " + shortStamp(to);
      } else {
        text = "all time";
      }
    } else if (state.commitMode === "list") {
      text = state.shas.length
        ? state.shas.length + " selected"
        : "none selected";
    } else {
      text = "all time";
    }
    $("#commit-summary").textContent = text;
    $("#sha-count").textContent = String(state.shas.length);
  }

  // ---------------------------------------------------------------- metrics -
  function zeroPayload() {
    return {
      repo: currentRepo() || { id: state.repoId, name: "repository" },
      object: { path: state.path, kind: state.kind,
                label: state.path || "repository" },
      metrics: { commits: 0, added: 0, removed: 0, growth: 0, churn: 0,
                 modifications: 0, modification_frequency: 0,
                 churn_rate: 0 },
      authors: [],
      children: [],
      timeseries: [],
    };
  }

  async function loadMetrics() {
    if (state.repoId == null) return;
    var seq = ++state.reqSeq;
    var payload;

    if (state.commitMode === "list" && state.shas.length === 0) {
      renderDashboard(zeroPayload());
      return;
    }

    var params = new URLSearchParams();
    params.set("path", state.path);
    params.set("kind", state.kind);
    if (state.selectedGroups.size > 0) {
      params.set("authors",
        JSON.stringify(Array.from(state.selectedGroups)));
    }
    if (state.commitMode === "range") {
      var from = dateToEpoch(state.from);
      var to = dateToEpoch(state.to);
      if (from != null) params.set("from", from);
      if (to != null) params.set("to", to);
    } else if (state.commitMode === "list") {
      params.set("shas", state.shas.join(","));
    }

    $("#loading").classList.remove("hidden");
    try {
      payload = await api("/api/repos/" + state.repoId + "/metrics?" +
        params.toString());
    } catch (err) {
      if (seq === state.reqSeq) {
        $("#loading").classList.add("hidden");
        banner("Could not compute metrics: " + err.message);
      }
      return;
    }
    if (seq !== state.reqSeq) return;      // a newer request superseded this
    $("#loading").classList.add("hidden");
    renderDashboard(payload);
  }

  function selectObject(path, kind) {
    state.path = path;
    state.kind = kind;
    loadMetrics();
  }

  function renderDashboard(payload) {
    state.lastPayload = payload;
    renderBreadcrumb(payload);
    renderCards(payload.metrics);
    renderObjectAuthors(payload.authors);
    renderChildren(payload.children, payload.object.kind);

    var points = payload.timeseries || [];
    RATCharts.renderChurn($("#churn-chart"), points);
    $("#bucket-note").textContent = points.length
      ? "(" + RATCharts.bucketFormat(points) + " buckets)"
      : "";
    RATCharts.renderOwnership($("#ownership-chart"), payload.authors);
    renderTreemapPanel(payload);
  }

  function renderBreadcrumb(payload) {
    var el = $("#breadcrumb");
    el.textContent = "";
    var segments = payload.object.path
      ? payload.object.path.split("/")
      : [];

    var root = document.createElement("button");
    root.type = "button";
    root.className = "seg";
    root.textContent = payload.repo.name;
    root.title = "Repository root";
    root.addEventListener("click", function () { selectObject("", "dir"); });
    el.appendChild(root);

    var accumulated = "";
    segments.forEach(function (part, index) {
      var sep = document.createElement("span");
      sep.className = "sep";
      sep.textContent = "\u203a";
      el.appendChild(sep);

      accumulated = accumulated ? accumulated + "/" + part : part;
      var isLast = index === segments.length - 1;
      if (isLast) {
        var here = document.createElement("span");
        here.className = "here";
        here.textContent = part;
        el.appendChild(here);
      } else {
        (function (path) {
          var button = document.createElement("button");
          button.type = "button";
          button.className = "seg";
          button.textContent = part;
          button.addEventListener("click", function () {
            selectObject(path, "dir");
          });
          el.appendChild(button);
        })(accumulated);
      }
    });

    if (payload.object.kind === "file") {
      var pill = document.createElement("span");
      pill.className = "kind-pill";
      pill.textContent = "file";
      el.appendChild(pill);
    }
    if (state.selectedGroups.size > 0 || commitFilterActive()) {
      var badge = document.createElement("span");
      badge.className = "badge";
      badge.textContent = "filtered H";
      badge.title = "Metrics restricted by the active filters";
      el.appendChild(badge);
    }
  }

  function renderCards(metrics) {
    var specs = [
      { label: "Added lines", value: fmtInt(metrics.added),
        sub: "l+ over H" },
      { label: "Removed lines", value: fmtInt(metrics.removed),
        sub: "l- over H" },
      { label: "Growth", value: fmtInt(metrics.growth), sub: "l+ minus l-",
        tone: metrics.growth > 0 ? "pos"
          : metrics.growth < 0 ? "neg" : "" },
      { label: "Churn", value: fmtInt(metrics.churn), sub: "l+ plus l-" },
      { label: "Modifications", value: fmtInt(metrics.modifications),
        sub: "n(H,o): commits touching o" },
      { label: "Modification frequency",
        value: fmtRate(metrics.modification_frequency), sub: "n / |H|" },
      { label: "Churn rate", value: fmtRate(metrics.churn_rate),
        sub: "churn / |H|" },
      { label: "Commits in H", value: fmtInt(metrics.commits),
        sub: "|H| non-merge commits" },
    ];
    var box = $("#cards");
    box.textContent = "";
    specs.forEach(function (spec) {
      var card = document.createElement("div");
      card.className = "card" + (spec.tone ? " " + spec.tone : "");
      var label = document.createElement("div");
      label.className = "card-label";
      label.textContent = spec.label;
      var value = document.createElement("div");
      value.className = "card-value";
      value.textContent = spec.value;
      var sub = document.createElement("div");
      sub.className = "card-sub";
      sub.textContent = spec.sub;
      card.appendChild(label);
      card.appendChild(value);
      card.appendChild(sub);
      box.appendChild(card);
    });
  }

  function renderObjectAuthors(authors) {
    var tbody = $("#object-authors-body");
    tbody.textContent = "";
    $("#object-authors-empty").classList.toggle("hidden", authors.length > 0);
    authors.forEach(function (author) {
      var tr = document.createElement("tr");
      tr.appendChild(textCell(author.name));
      tr.appendChild(numCell(fmtInt(author.added)));
      tr.appendChild(numCell(fmtInt(author.removed)));
      tr.appendChild(numCell(fmtInt(author.churn)));
      tr.appendChild(numCell(fmtInt(author.modifications)));

      var ownership = document.createElement("td");
      var bar = document.createElement("span");
      bar.className = "own-bar";
      var inner = document.createElement("i");
      inner.style.width = Math.round(author.ownership * 100) + "%";
      bar.appendChild(inner);
      ownership.appendChild(bar);
      ownership.appendChild(document.createTextNode(
        (author.ownership * 100).toFixed(1) + "%"));
      tr.appendChild(ownership);
      tbody.appendChild(tr);
    });
  }

  // ---------------------------------------------------------- child table --
  function childrenComparator(a, b) {
    var key = state.sortKey;
    if (key === "name" || key === "kind") {
      return a[key].localeCompare(b[key]) * state.sortDir;
    }
    return (a[key] - b[key]) * state.sortDir;
  }

  function renderChildren(rows, kind) {
    state.children = rows || [];
    var isDir = kind === "dir";
    $("#children-table").classList.toggle("hidden", !isDir);
    var empty = $("#children-empty");
    empty.textContent = isDir
      ? "No objects in this commit set."
      : "This object is a file - the metric cards above describe it.";
    empty.classList.toggle("hidden", isDir && state.children.length > 0);
    $("#export-csv").classList.toggle("hidden",
      !isDir || state.children.length === 0);
    $("#children-title").textContent = isDir
      ? "Contents of " + (state.path || "repository")
      : "File";
    renderChildrenRows();
  }

  function renderChildrenRows() {
    var tbody = $("#children-body");
    tbody.textContent = "";
    state.children.slice().sort(childrenComparator).forEach(function (row) {
      var tr = document.createElement("tr");

      var nameCell = document.createElement("td");
      nameCell.className = "name";
      var button = document.createElement("button");
      button.type = "button";
      button.className = "obj";
      button.textContent = row.name;
      button.title = row.kind === "dir"
        ? "Open directory " + row.path
        : "Show metrics for " + row.path;
      button.addEventListener("click", function () {
        selectObject(row.path, row.kind);
      });
      nameCell.appendChild(button);
      tr.appendChild(nameCell);

      var kindCell = document.createElement("td");
      var pill = document.createElement("span");
      pill.className = "kind-pill";
      pill.textContent = row.kind;
      kindCell.appendChild(pill);
      tr.appendChild(kindCell);

      tr.appendChild(numCell(fmtInt(row.added)));
      tr.appendChild(numCell(fmtInt(row.removed)));
      tr.appendChild(numCell(fmtInt(row.growth)));
      tr.appendChild(numCell(fmtInt(row.churn)));
      tr.appendChild(numCell(fmtInt(row.modifications)));
      tr.appendChild(numCell(fmtRate(row.modification_frequency)));
      tr.appendChild(numCell(fmtRate(row.churn_rate)));
      tbody.appendChild(tr);
    });
  }

  function updateSortHeaders() {
    document.querySelectorAll("#children-table th[data-sort]")
      .forEach(function (th) {
        var base = th.dataset.label || th.textContent;
        var marker = th.dataset.sort === state.sortKey
          ? (state.sortDir > 0 ? " \u25b2" : " \u25bc")
          : "";
        th.textContent = base + marker;
      });
  }

  function wireTableSort() {
    document.querySelectorAll("#children-table th[data-sort]")
      .forEach(function (th) {
        th.dataset.label = th.textContent;
        th.addEventListener("click", function () {
          var key = th.dataset.sort;
          if (state.sortKey === key) {
            state.sortDir = -state.sortDir;
          } else {
            state.sortKey = key;
            state.sortDir = (key === "name" || key === "kind") ? 1 : -1;
          }
          updateSortHeaders();
          renderChildrenRows();
        });
      });
    updateSortHeaders();
  }

  // -------------------------------------------------------------- treemap --
  function renderTreemapPanel(payload) {
    var container = $("#treemap");
    if (payload.object.kind === "dir") {
      RATCharts.renderTreemap(container, payload.children, function (item) {
        selectObject(item.path, item.kind);
      });
    } else {
      container.textContent = "";
      var note = document.createElement("p");
      note.className = "muted small";
      note.textContent =
        "The treemap shows the contents of a directory - this object is a file.";
      container.appendChild(note);
    }
  }

  // ------------------------------------------------------------------ CSV --
  function csvCell(value) {
    var text = String(value == null ? "" : value);
    return /[",\n]/.test(text)
      ? '"' + text.replace(/"/g, '""') + '"'
      : text;
  }

  function exportCsv() {
    if (!state.children.length) {
      toast("Nothing to export for this object", true);
      return;
    }
    var header = ["name", "type", "added", "removed", "growth", "churn",
                  "modifications", "modification_frequency", "churn_rate"];
    var lines = [header.join(",")];
    state.children.forEach(function (row) {
      lines.push([
        csvCell(row.name), row.kind, row.added, row.removed, row.growth,
        row.churn, row.modifications,
        row.modification_frequency.toFixed(4),
        row.churn_rate.toFixed(4),
      ].join(","));
    });
    var blob = new Blob([lines.join("\n") + "\n"], { type: "text/csv" });
    var url = URL.createObjectURL(blob);
    var repo = currentRepo();
    var label = (state.path || (repo ? repo.name : "repository"))
      .replace(/[^a-z0-9._-]+/gi, "-");
    var link = document.createElement("a");
    link.href = url;
    link.download = "rat-" + label + ".csv";
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
    URL.revokeObjectURL(url);
  }

  // -------------------------------------------------------- commit picker --
  function openModal() {
    if (state.repoId == null) {
      toast("Select a repository first", true);
      return;
    }
    state.modalShas = new Set(state.shas);
    state.commitQuery = "";
    state.commitOffset = 0;
    $("#commit-search").value = "";
    $("#commit-list").textContent = "";
    $("#modal").classList.remove("hidden");
    updateSelectionCount();
    loadCommitPage(true);
  }

  function closeModal() {
    $("#modal").classList.add("hidden");
  }

  function updateSelectionCount() {
    $("#selection-count").textContent =
      state.modalShas.size + " selected";
  }

  async function loadCommitPage(reset) {
    if (state.repoId == null) return;
    if (reset) {
      state.commitOffset = 0;
      $("#commit-list").textContent = "";
      $("#commit-list").classList.add("loading");
    }
    var params = new URLSearchParams();
    params.set("limit", "200");
    params.set("offset", state.commitOffset);
    if (state.commitQuery) params.set("q", state.commitQuery);
    var payload;
    try {
      payload = await api("/api/repos/" + state.repoId + "/commits?" +
        params.toString());
    } catch (err) {
      $("#commit-list").classList.remove("loading");
      banner("Could not load commits: " + err.message);
      return;
    }
    $("#commit-list").classList.remove("loading");
    state.commitTotal = payload.total;
    state.commitOffset += payload.commits.length;
    renderCommitRows(payload.commits);
    $("#modal-total").textContent = state.commitQuery
      ? fmtInt(payload.total) + " matching commits"
      : fmtInt(payload.total) + " non-merge commits";
  }

  function renderCommitRows(commits) {
    var list = $("#commit-list");
    var existing = list.querySelector(".load-more");
    if (existing) existing.remove();

    commits.forEach(function (commit) {
      var row = document.createElement("div");
      row.className = "commit-row";

      var checkbox = document.createElement("input");
      checkbox.type = "checkbox";
      checkbox.checked = state.modalShas.has(commit.sha);
      checkbox.addEventListener("change", function () {
        if (checkbox.checked) state.modalShas.add(commit.sha);
        else state.modalShas.delete(commit.sha);
        updateSelectionCount();
      });
      row.appendChild(checkbox);

      var sha = document.createElement("span");
      sha.className = "sha";
      sha.textContent = commit.sha.slice(0, 7);
      row.appendChild(sha);

      var when = document.createElement("span");
      when.className = "when";
      when.textContent = shortStamp(commit.ts);
      row.appendChild(when);

      var message = document.createElement("span");
      message.className = "msg";
      message.textContent = commit.subject || "(no subject)";
      message.title = commit.subject || "";
      row.appendChild(message);

      var who = document.createElement("span");
      who.className = "who";
      who.textContent = commit.author;
      who.title = commit.author;
      row.appendChild(who);

      row.addEventListener("click", function (event) {
        if (event.target === checkbox) return;
        checkbox.checked = !checkbox.checked;
        checkbox.dispatchEvent(new Event("change"));
      });
      list.appendChild(row);
    });

    if (state.commitOffset < state.commitTotal) {
      var wrap = document.createElement("div");
      wrap.className = "load-more";
      var more = document.createElement("button");
      more.type = "button";
      more.className = "ghost small";
      more.textContent = "Load more\u2026";
      more.addEventListener("click", function () { loadCommitPage(false); });
      wrap.appendChild(more);
      list.appendChild(wrap);
    }
  }

  // ---------------------------------------------------------- authors page -
  async function loadAuthorsPage() {
    if (state.repoId == null) return;
    var payload;
    try {
      payload = await api("/api/repos/" + state.repoId + "/authors");
    } catch (err) {
      banner("Could not load authors: " + err.message);
      return;
    }
    state.authors = payload.authors;
    state.checkedAuthors = new Set();
    renderAuthorsTable();
  }

  function renderAuthorsTable() {
    var tbody = $("#authors-body");
    tbody.textContent = "";
    state.authors.forEach(function (author) {
      var tr = document.createElement("tr");

      var pickCell = document.createElement("td");
      pickCell.className = "pick";
      var checkbox = document.createElement("input");
      checkbox.type = "checkbox";
      checkbox.checked = state.checkedAuthors.has(author.id);
      checkbox.addEventListener("change", function () {
        if (checkbox.checked) state.checkedAuthors.add(author.id);
        else state.checkedAuthors.delete(author.id);
        updateMergeHint();
      });
      pickCell.appendChild(checkbox);
      tr.appendChild(pickCell);

      tr.appendChild(textCell(author.name));
      tr.appendChild(textCell(author.email));
      tr.appendChild(textCell(author.group_key));
      tr.appendChild(numCell(fmtInt(author.commits)));
      tr.appendChild(numCell(fmtInt(author.churn)));

      tr.addEventListener("click", function (event) {
        if (event.target === checkbox) return;
        checkbox.checked = !checkbox.checked;
        checkbox.dispatchEvent(new Event("change"));
      });
      tbody.appendChild(tr);
    });
    updateMergeHint();
  }

  function updateMergeHint() {
    var count = state.checkedAuthors.size;
    $("#merge-hint").textContent = count
      ? count + " identit" + (count === 1 ? "y" : "ies") + " selected"
      : "select identities to merge or rename";
    $("#merge-button").disabled = count === 0;
  }

  async function mergeAuthors() {
    var name = $("#merge-name").value.trim();
    if (!state.checkedAuthors.size) {
      toast("Select at least one identity to merge", true);
      return;
    }
    if (!name) {
      toast("Enter a display name for the merged identity", true);
      return;
    }
    var payload;
    try {
      payload = await api(
        "/api/repos/" + state.repoId + "/authors/merge", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            author_ids: Array.from(state.checkedAuthors),
            name: name,
          }),
        });
    } catch (err) {
      toast(err.message, true);
      return;
    }
    state.authors = payload.authors;
    state.checkedAuthors = new Set();
    $("#merge-name").value = "";
    renderAuthorsTable();
    toast("Merged " + payload.updated + " identities into " + name);
    refreshGroups().finally(function () { loadMetrics(); });
  }

  // ------------------------------------------------------------- add repo --
  function setAddMode(mode) {
    $("#mode-url").classList.toggle("active", mode === "url");
    $("#mode-zip").classList.toggle("active", mode === "zip");
    $("#url-pane").classList.toggle("hidden", mode !== "url");
    $("#zip-pane").classList.toggle("hidden", mode !== "zip");
  }

  async function cloneRepo() {
    var url = $("#url-input").value.trim();
    if (!url) {
      toast("Enter a repository URL", true);
      return;
    }
    var button = $("#url-add");
    button.disabled = true;
    try {
      var job = await api("/api/repos", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ url: url }),
      });
      await watchJob(job.id);
      $("#url-input").value = "";
    } catch (err) {
      toast(err.message, true);
    } finally {
      button.disabled = false;
    }
  }

  async function uploadZip(file) {
    if (!file.name.toLowerCase().endsWith(".zip")) {
      toast("Only .zip archives are supported", true);
      return;
    }
    var form = new FormData();
    form.append("file", file, file.name);
    try {
      var job = await api("/api/repos", { method: "POST", body: form });
      await watchJob(job.id);
    } catch (err) {
      toast(err.message, true);
    }
  }

  function watchJob(jobId) {
    return new Promise(function (resolve) {
      var panel = $("#job-progress");
      var bar = $("#job-bar");
      var message = $("#job-msg");
      var error = $("#job-error");
      panel.classList.remove("hidden");
      error.classList.add("hidden");
      error.textContent = "";
      bar.style.width = "2%";
      message.textContent = "starting import\u2026";

      var timer = setInterval(poll, 400);

      async function poll() {
        var job;
        try {
          job = await api("/api/jobs/" + jobId);
        } catch (err) {
          clearInterval(timer);
          message.textContent = "";
          error.textContent = err.message;
          error.classList.remove("hidden");
          resolve();
          return;
        }
        bar.style.width = Math.round((job.progress || 0) * 100) + "%";
        message.textContent = job.state +
          (job.message ? " \u2013 " + job.message : "");
        if (job.state === "done") {
          clearInterval(timer);
          toast("Imported " + (job.name || "repository"));
          await loadRepos();
          if (job.repo_id != null) selectRepo(job.repo_id);
          setTimeout(function () { panel.classList.add("hidden"); }, 1500);
          resolve();
        } else if (job.state === "error") {
          clearInterval(timer);
          error.textContent = job.error || "import failed";
          error.classList.remove("hidden");
          resolve();
        }
      }
    });
  }

  // ----------------------------------------------------------------- wiring -
  function updateVisibility() {
    var hasRepos = state.repos.length > 0;
    $("#empty-state").classList.toggle("hidden", hasRepos);
    $("#dashboard").classList.toggle("hidden",
      !(hasRepos && state.tab === "dashboard"));
    $("#authors-page").classList.toggle("hidden",
      !(hasRepos && state.tab === "authors"));
  }

  function setTab(tab) {
    state.tab = tab;
    document.querySelectorAll(".tab").forEach(function (button) {
      button.classList.toggle("active", button.dataset.tab === tab);
    });
    updateVisibility();
    if (tab === "authors" && state.repoId != null) {
      loadAuthorsPage();
    } else if (tab === "dashboard" && state.lastPayload) {
      // charts were rendered while hidden (zero width) - repaint
      var payload = state.lastPayload;
      RATCharts.renderChurn($("#churn-chart"), payload.timeseries);
      RATCharts.renderOwnership($("#ownership-chart"), payload.authors);
      renderTreemapPanel(payload);
    }
  }

  function resetFilters() {
    state.path = "";
    state.kind = "dir";
    state.selectedGroups = new Set();
    document.querySelectorAll("#author-options input").forEach(function (box) {
      box.checked = false;
    });
    state.commitMode = "all";
    state.from = "";
    state.to = "";
    state.shas = [];
    resetCommitControls();
    updateAuthorSummary();
    loadMetrics();
  }

  function wireFilters() {
    document.querySelectorAll('input[name="cmode"]').forEach(function (radio) {
      radio.addEventListener("change", function () {
        state.commitMode = radio.value;
        $("#range-inputs").classList.toggle("hidden",
          state.commitMode !== "range");
        updateCommitSummary();
        loadMetrics();
      });
    });
    $("#from-input").addEventListener("change", function (event) {
      state.from = event.target.value;
      updateCommitSummary();
      loadMetrics();
    });
    $("#to-input").addEventListener("change", function (event) {
      state.to = event.target.value;
      updateCommitSummary();
      loadMetrics();
    });
    $("#reset-filters").addEventListener("click", resetFilters);
    $("#open-commit-modal").addEventListener("click", openModal);
    $("#selection-clear").addEventListener("click", function () {
      state.modalShas = new Set();
      document.querySelectorAll("#commit-list input[type=checkbox]")
        .forEach(function (box) { box.checked = false; });
      updateSelectionCount();
    });
    $("#selection-apply").addEventListener("click", function () {
      state.shas = Array.from(state.modalShas).sort();
      state.commitMode = "list";
      var radio = document.querySelector('input[name="cmode"][value="list"]');
      radio.checked = true;
      $("#range-inputs").classList.add("hidden");
      updateCommitSummary();
      closeModal();
      loadMetrics();
    });
    $("#modal-close").addEventListener("click", closeModal);
    $("#modal").addEventListener("click", function (event) {
      if (event.target === $("#modal")) closeModal();
    });
    document.addEventListener("keydown", function (event) {
      if (event.key === "Escape" && !$("#modal").classList.contains("hidden")) {
        closeModal();
      }
    });
    var search = $("#commit-search");
    search.addEventListener("input", function () {
      state.commitQuery = search.value.trim();
      clearTimeout(state.searchTimer);
      state.searchTimer = setTimeout(function () {
        loadCommitPage(true);
      }, 250);
    });
    // close open dropdowns when clicking elsewhere
    document.addEventListener("click", function (event) {
      document.querySelectorAll("details.dd[open]").forEach(function (dd) {
        if (!dd.contains(event.target)) dd.open = false;
      });
    });
  }

  function wireAddRepo() {
    $("#mode-url").addEventListener("click", function () { setAddMode("url"); });
    $("#mode-zip").addEventListener("click", function () { setAddMode("zip"); });
    $("#url-add").addEventListener("click", cloneRepo);
    $("#url-input").addEventListener("keydown", function (event) {
      if (event.key === "Enter") cloneRepo();
    });

    var zone = $("#dropzone");
    ["dragenter", "dragover"].forEach(function (name) {
      zone.addEventListener(name, function (event) {
        event.preventDefault();
        zone.classList.add("dragover");
      });
    });
    ["dragleave", "dragend"].forEach(function (name) {
      zone.addEventListener(name, function () {
        zone.classList.remove("dragover");
      });
    });
    zone.addEventListener("drop", function (event) {
      event.preventDefault();
      zone.classList.remove("dragover");
      var file = event.dataTransfer && event.dataTransfer.files[0];
      if (file) uploadZip(file);
    });
    $("#file-input").addEventListener("change", function (event) {
      var file = event.target.files[0];
      event.target.value = "";
      if (file) uploadZip(file);
    });
  }

  function wireTabs() {
    document.querySelectorAll(".tab").forEach(function (button) {
      button.addEventListener("click", function () {
        setTab(button.dataset.tab);
      });
    });
  }

  function init() {
    wireTabs();
    wireFilters();
    wireAddRepo();
    wireTableSort();
    $("#merge-button").addEventListener("click", mergeAuthors);
    $("#export-csv").addEventListener("click", exportCsv);
    $("#banner-close").addEventListener("click", function () {
      $("#banner").classList.add("hidden");
    });
    window.addEventListener("unhandledrejection", function (event) {
      var reason = event.reason;
      banner("Unexpected error: " +
        (reason && reason.message ? reason.message : String(reason)));
    });
    window.addEventListener("resize", function () {
      clearTimeout(state.resizeTimer);
      state.resizeTimer = setTimeout(function () {
        if (state.tab === "dashboard" && state.lastPayload) {
          renderTreemapPanel(state.lastPayload);
        }
      }, 200);
    });
    loadRepos();
  }

  document.addEventListener("DOMContentLoaded", init);
})();
