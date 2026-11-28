/* The ToolSpotter website. Plain JavaScript over the REST API.
 *
 * Tool names, descriptions and requests come from outside, so they are only ever
 * placed on the page as text (never as HTML).
 */
"use strict";

const PAGE = 100;                 // tools shown at a time
const state = { summary: null, tools: [], shown: PAGE, upload: null, editing: null };

const $ = (id) => document.getElementById(id);

/** Build an element: el("td", {class: "number"}, "3 ms", otherNode) */
function el(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else if (value === true) node.setAttribute(key, "");
    else if (value !== false && value !== null && value !== undefined) node.setAttribute(key, value);
  }
  node.append(...children.filter((c) => c !== null && c !== undefined && c !== false));
  return node;
}

const number = (value) => value.toLocaleString("en-US");
const percent = (value) => (value === null || value === undefined ? "–" : `${(value * 100).toFixed(1)}%`);
const millis = (value) => (value === null || value === undefined ? "–" : `${value < 10 ? value.toFixed(1) : Math.round(value)} ms`);
const plural = (count, word) => `${number(count)} ${word}${count === 1 ? "" : "s"}`;

/** A short time such as "5 min ago". */
function ago(iso) {
  const minutes = Math.floor((Date.now() - new Date(iso).getTime()) / 60000);
  if (minutes < 1) return "just now";
  if (minutes < 60) return `${minutes} min ago`;
  if (minutes < 24 * 60) return `${Math.floor(minutes / 60)} h ago`;
  return `${Math.floor(minutes / (24 * 60))} d ago`;
}

/* ---------------------------------------------------------------- server */

class ApiError extends Error {
  constructor(status, detail) {
    super(describe(detail) || `The server answered ${status}.`);
    this.status = status;
    this.detail = detail;
  }
}

function describe(detail) {
  if (!detail) return "";
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) return detail.map((d) => d.msg || String(d)).join("; ");
  if (detail.issues) return detail.issues.join(" ");
  if (detail.rejected) return detail.rejected.map((r) => `${r.id || "tool"}: ${r.issues.join(" ")}`).join("; ");
  return detail.reason || "";
}

async function api(path, method = "GET", body = undefined) {
  const options = { method, headers: {} };
  if (body !== undefined) {
    options.headers["content-type"] = "application/json";
    options.body = JSON.stringify(body);
  }
  let response;
  try {
    response = await fetch(`..${path}`, options);
  } catch {
    throw new ApiError(0, "The server cannot be reached. Is it still running?");
  }
  const data = await response.json().catch(() => null);
  if (!response.ok) throw new ApiError(response.status, data && data.detail);
  return data;
}

function show(node, text, kind = "") {
  node.className = `notice ${kind}`.trim();
  node.textContent = text;
  node.hidden = !text;
}

/* ------------------------------------------------------------- dashboard */

function numberCard(value, label) {
  return el("div", { class: "card" }, el("strong", {}, value), el("span", {}, label));
}

function bars(container, entries) {
  const largest = Math.max(1, ...entries.map(([, count]) => count));
  container.replaceChildren(...entries.map(([label, count, kind]) => {
    const fill = el("div", { class: `fill ${kind || ""}` });
    fill.style.width = `${(count / largest) * 100}%`;
    return el("div", { class: "bar" },
      el("span", { class: "label", title: label }, label),
      el("div", { class: "track" }, fill),
      el("span", { class: "value" }, number(count)));
  }));
  if (!entries.length) container.append(el("p", { class: "muted" }, "Nothing yet."));
}

function drawRegistry(summary) {
  $("empty").hidden = summary.loaded;
  $("registry-numbers").replaceChildren(
    numberCard(number(summary.tool_count), "Tools"),
    numberCard(number(Object.keys(summary.groups).length), "Groups"),
    numberCard(number(summary.duplicate_groups.length), "Duplicate groups"),
    numberCard(number(summary.old_tools), "Old tool versions"));

  const groups = Object.entries(summary.groups);
  // A single left-over group is shown by name rather than as "more".
  const top = groups.slice(0, groups.length > 11 ? 10 : 11);
  const rest = groups.slice(top.length);
  if (rest.length) top.push([`${rest.length} more groups`, rest.reduce((sum, [, count]) => sum + count, 0)]);
  bars($("by-group"), top);
  bars($("by-effect"), summary.loaded
    ? Object.entries(summary.side_effects).map(([name, count]) => [name, count, name]) : []);

  $("duplicates").hidden = !summary.duplicate_groups.length;
  $("duplicate-list").replaceChildren(
    ...summary.duplicate_groups.map((group) => el("li", {}, el("code", {}, group.join(", ")))));

  $("status").classList.toggle("off", !summary.loaded);
  $("status").textContent = summary.loaded
    ? `${plural(summary.tool_count, "tool")} · ${summary.planner === "llm" ? "LLM planner" : "local mode"}${summary.saved ? "" : " · not saved"}`
    : "No tools loaded";
}

function drawHistory(history) {
  const { summary, requests, enabled } = history;
  $("history").hidden = !enabled;
  $("clear-history").hidden = !enabled;
  $("history-note").textContent = enabled
    ? `The last ${number(summary.kept_at_most)} requests at most are saved, as plain text. MCP requests are not recorded.`
    : "History is off because the server was started with --no-persist.";
  if (!enabled) return;

  $("request-numbers").replaceChildren(
    numberCard(number(summary.requests), "Saved requests"),
    numberCard(millis(summary.average_ms), "Average routing time"),
    numberCard(percent(summary.average_tokens_saved_pct), "Tool-definition tokens saved (estimate)"));
  const results = summary.results;
  bars($("by-result"), summary.requests
    ? [["Tools chosen", results.plan], ["Question asked", results.question], ["No tool", results.none]] : []);

  const rows = requests.map((request) => el("tr", {},
    el("td", { class: "cut" }, el("button", {
      type: "button", class: "link", title: "Try this request again",
      onclick: () => { $("query").value = request.query; location.hash = "#try"; $("query").focus(); },
    }, request.query)),
    el("td", { class: "plan" }, request.result === "plan" ? request.plan.join("\n→ ")
      : request.result === "question" ? "Asked a question" : "No tool"),
    el("td", { class: "number" }, millis(request.ms)),
    el("td", { class: "nowrap", title: new Date(request.at).toLocaleString() }, ago(request.at))));
  if (!rows.length) rows.push(el("tr", {}, el("td", { colspan: 4, class: "muted" }, "No requests yet. Use “Try it” below.")));
  $("recent").replaceChildren(...rows);
}

async function refreshHistory() {
  drawHistory(await api("/history?limit=10"));
}

/** Reload everything that depends on the registry. */
async function refreshAll() {
  try {
    state.summary = await api("/registry/summary");
    drawRegistry(state.summary);
    state.tools = state.summary.loaded ? (await api("/registry/tools")).tools : [];
    drawFilters();
    drawTools();
    drawPermissions();
    await refreshHistory();
    show($("problem"), "");
  } catch (error) {
    show($("problem"), error.message, "error");
    $("status").textContent = "Not connected";
    $("status").classList.add("off");
  }
}

function drawPermissions() {
  const { loaded, mcp_scan: canScan } = state.summary;
  $("query").disabled = !loaded;
  $("route-form").querySelector("button").disabled = !loaded;
  $("new-tool").disabled = !loaded;
  $("export").hidden = !loaded;
  $("scan").disabled = !canScan;
  show($("scan-off"), canScan ? "" :
    "Scanning is off because this server accepts other machines. Start it with --allow-mcp-scan to turn it on.");
}

/* ---------------------------------------------------------------- try it */

function effectTag(effect) {
  return el("span", { class: `tag ${effect}` }, effect);
}

function table(headings, rows) {
  return el("div", { class: "scroll" }, el("table", {},
    el("thead", {}, el("tr", {}, ...headings.map((h) => el("th", {}, h)))),
    el("tbody", {}, ...rows.map((cells) => el("tr", {}, ...cells.map((c) => el("td", {}, c)))))));
}

function drawAnswer(answer, took) {
  const byId = new Map(answer.selected_tool_schemas.map((t) => [t.id, t]));
  const box = el("div", { class: "card" });
  if (answer.clarify) {
    box.append(el("h4", {}, "The router asks a question"), el("p", {}, answer.clarify_question || ""));
  } else if (!answer.plan.length) {
    box.append(el("h4", {}, "No suitable tool"), el("p", { class: "muted" }, "Nothing in the registry matches this request."));
  } else {
    box.append(
      el("h4", {}, `${plural(answer.plan.length, "step")} · ${percent(answer.tokens_saved_pct)} of tool-definition tokens saved (estimate)`),
      el("ol", { class: "steps" }, ...answer.plan.map((id) => {
        const tool = byId.get(id) || {};
        return el("li", {}, el("code", {}, id), " ", effectTag(tool.side_effects || "read"),
          el("div", { class: "muted" }, tool.description || ""));
      })));
  }
  box.append(el("p", { class: "muted" }, `Answered in ${millis(took)}, including the network.`));

  const trace = answer.trace || {};
  const candidates = trace.top_candidates || [];
  if (candidates.length) {
    box.append(el("details", {}, el("summary", {}, "Why these tools?"),
      el("p", { class: "muted" }, "Best matches before planning. The score combines the vector and keyword matches, minus penalties."),
      table(["Tool", "Score", "Vector", "Keyword"], candidates.map((c) =>
        [el("code", {}, c.tool), c.fused.toFixed(3), c.dense.toFixed(3), c.bm25.toFixed(3)]))));
  }
  const dropped = Object.entries(trace.dropped || {}).filter(([, ids]) => ids.length);
  if (dropped.length) {
    const names = { version_family: "Another version was preferred", near_duplicate: "A duplicate scored higher" };
    box.append(el("details", {}, el("summary", {}, "What was dropped?"),
      table(["Reason", "Tools"], dropped.map(([reason, ids]) => [names[reason] || reason, el("code", {}, ids.join(", "))]))));
  }
  box.append(el("details", {}, el("summary", {}, "Raw answer (JSON)"), el("pre", {}, JSON.stringify(answer, null, 2))));
  $("answer").replaceChildren(box);
}

$("route-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const query = $("query").value;
  if (!query.trim()) return;
  const button = event.submitter || $("route-form").querySelector("button");
  button.disabled = true;
  button.setAttribute("aria-busy", "true");
  // Local routing takes milliseconds; show that it is working only when it takes longer.
  const waiting = setTimeout(() => $("answer").replaceChildren(
    el("div", { class: "card thinking" }, el("i"), el("i"), el("i"), " Finding the tools…")), 200);
  try {
    const started = performance.now();
    const answer = await api("/route", "POST", { query });
    clearTimeout(waiting);                  // or it could replace the answer below
    drawAnswer(answer, performance.now() - started);
    await refreshHistory();
  } catch (error) {
    clearTimeout(waiting);
    $("answer").replaceChildren(el("div", { class: "notice error" }, error.message));
  } finally {
    button.disabled = false;
    button.removeAttribute("aria-busy");
  }
});

/* ------------------------------------------------------------- add tools */

function resetUpload() {
  state.upload = null;
  $("preview").hidden = true;
  $("found").hidden = true;
  show($("add-message"), "");
}

function listed(title, ids) {
  if (!ids.length) return null;
  return el("details", {}, el("summary", {}, `${title}: ${number(ids.length)}`), el("p", {}, el("code", {}, ids.join(", "))));
}

async function preview(mode = "merge") {
  const body = { ...state.upload, mode };
  const report = await api("/registry/preview", "POST", body);
  const total = report.added.length + report.changed.length + report.removed.length;
  const accepted = report.added.length + report.changed.length + report.unchanged.length;
  const choice = (value, text) => el("label", {},
    el("input", {
      type: "radio", name: "mode", value, checked: mode === value,
      onchange: () => preview(value).catch(showAddError),
    }), text);

  const save = el("button", { type: "button", disabled: !accepted || !total }, "Save");
  save.addEventListener("click", async () => {
    if (report.removed.length && !confirm(`Replace all tools? ${plural(report.removed.length, "tool")} will be removed.`)) return;
    save.disabled = true;
    try {
      const done = await api("/registry/apply", "POST", body);
      resetUpload();
      $("file").value = "";
      show($("add-message"), `Saved: ${done.added.length} added, ${done.changed.length} changed, ${done.removed.length} removed. ${plural(done.tool_count_after, "tool")} now registered.`, "good");
      await refreshAll();
    } catch (error) {
      showAddError(error);
      save.disabled = false;
    }
  });

  $("preview").replaceChildren(...[
    el("h4", {}, "Preview (nothing is saved yet)"),
    el("p", {}, `${report.added.length} new · ${report.changed.length} changed · ${report.unchanged.length} unchanged · ${report.removed.length} removed · ${report.rejected.length} rejected`),
    el("div", { class: "choices" }, choice("merge", "Add and update"), choice("replace", "Replace all")),
    listed("New", report.added),
    listed("Changed", report.changed),
    listed("Removed", report.removed),
    report.rejected.length ? el("details", { open: true },
      el("summary", {}, `Rejected, will be skipped: ${number(report.rejected.length)}`),
      table(["Tool", "Why"], report.rejected.slice(0, 200).map((r) =>
        [el("code", {}, r.id || `entry ${r.index + 1}`), r.issues.join(" ")]))) : null,
    !accepted ? el("p", { class: "notice error" }, "No tool in this upload can be saved.")
      : !total ? el("p", { class: "notice" }, "Nothing would change.") : null,
    el("div", { class: "row end" }, el("span", { class: "grow" }),
      el("button", { type: "button", class: "quiet", onclick: () => { resetUpload(); $("file").value = ""; } }, "Cancel"),
      save),
  ].filter(Boolean));
  $("preview").hidden = false;
}

function showAddError(error) {
  show($("add-message"), error.message, "error");
}

$("file").addEventListener("change", async () => {
  const file = $("file").files[0];
  resetUpload();
  if (!file) return;
  try {
    let data;
    try {
      data = JSON.parse((await file.text()).replace(/^﻿/, ""));
    } catch {
      throw new Error("This file is not valid JSON.");
    }
    const tools = Array.isArray(data) ? data : data && data.tools;
    if (!Array.isArray(tools) || !tools.length) throw new Error("The file needs a non-empty “tools” list.");
    state.upload = { tools, near_duplicate_groups: (data && data.near_duplicate_groups) || [] };
    await preview();
  } catch (error) {
    showAddError(error);
  }
});

function drawFound(result) {
  const boxes = result.tools.map((tool) => {
    const box = el("input", { type: "checkbox", checked: true });
    box.tool = tool;
    return box;
  });
  const next = el("button", { type: "button", disabled: !boxes.length }, "Preview selected");
  next.addEventListener("click", async () => {
    const tools = boxes.filter((b) => b.checked).map((b) => b.tool);
    if (!tools.length) return showAddError(new Error("Select at least one tool."));
    show($("add-message"), "");
    state.upload = { tools, near_duplicate_groups: [] };
    await preview().catch(showAddError);
    $("preview").scrollIntoView({ block: "nearest" });
  });
  const all = (checked) => () => boxes.forEach((b) => { b.checked = checked; });

  $("found").replaceChildren(...[
    el("h4", {}, `${result.server}: ${result.tools.length} of ${plural(result.found, "tool")} can be added`),
    el("div", { class: "row" },
      el("button", { type: "button", class: "quiet", onclick: all(true) }, "Select all"),
      el("button", { type: "button", class: "quiet", onclick: all(false) }, "Select none")),
    el("div", { class: "picks" }, ...boxes.map((b) => el("label", {}, b,
      el("span", {}, el("code", {}, b.tool.id), " ", effectTag(b.tool.side_effects),
        el("div", { class: "muted" }, b.tool.description))))),
    result.rejected.length ? el("details", {}, el("summary", {}, `Cannot be added: ${result.rejected.length}`),
      el("ul", {}, ...result.rejected.map((r) => el("li", {}, r)))) : null,
    el("div", { class: "row end" }, el("span", { class: "grow" }), next),
  ].filter(Boolean));
  $("found").hidden = false;
}

$("scan-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  resetUpload();
  $("file").value = "";
  const button = $("scan");
  button.disabled = true;
  button.textContent = "Scanning…";
  button.setAttribute("aria-busy", "true");
  try {
    const body = { url: $("mcp-url").value.trim(), header: $("mcp-header").value.trim() || "Authorization" };
    if ($("mcp-token").value) body.token = $("mcp-token").value;
    if ($("mcp-prefix").value.trim()) body.prefix = $("mcp-prefix").value.trim();
    const result = await api("/mcp/scan", "POST", body);
    $("mcp-token").value = "";
    drawFound(result);
  } catch (error) {
    showAddError(error);
  } finally {
    button.textContent = "Scan";
    button.removeAttribute("aria-busy");
    button.disabled = !(state.summary && state.summary.mcp_scan);
  }
});

/* ----------------------------------------------------------------- tools */

function drawFilters() {
  const select = $("group-filter");
  const chosen = select.value;
  const groups = Object.keys(state.summary.groups).sort();
  select.replaceChildren(el("option", { value: "" }, "All groups"), ...groups.map((g) => el("option", { value: g }, g)));
  select.value = groups.includes(chosen) ? chosen : "";
}

function drawTools() {
  const words = $("search").value.trim().toLowerCase();
  const group = $("group-filter").value;
  const effect = $("effect-filter").value;
  const found = state.tools.filter((t) =>
    (!group || t.cluster === group) && (!effect || t.side_effects === effect) &&
    (!words || `${t.id} ${t.name} ${t.description}`.toLowerCase().includes(words)));

  $("tool-rows").replaceChildren(...found.slice(0, state.shown).map((t) => el("tr", {},
    el("td", {}, el("button", { type: "button", class: "link", onclick: () => openEditor(t) }, el("code", {}, t.id))),
    el("td", {}, t.cluster || ""),
    el("td", {}, effectTag(t.side_effects || "read")),
    el("td", { class: "nowrap" }, `${t.version || "–"}${t.deprecated || t.replaced_by ? " (old)" : ""}`),
    el("td", { class: "cut" }, t.description || ""))));
  $("tool-count").textContent = state.tools.length
    ? `Showing ${number(Math.min(found.length, state.shown))} of ${number(found.length)} matching (${plural(state.tools.length, "tool")} in total). Select a tool to view or edit it.`
    : "No tools yet.";
  $("more").hidden = found.length <= state.shown;
}

for (const id of ["search", "group-filter", "effect-filter"]) {
  $(id).addEventListener("input", () => { state.shown = PAGE; drawTools(); });
}
$("more").addEventListener("click", () => { state.shown += PAGE; drawTools(); });

function openEditor(tool) {
  state.editing = tool ? tool.id : null;
  const template = {
    name: "get_weather", cluster: "general",
    description: "Get the current weather conditions for a named city",
    side_effects: "read",
    parameters: [{ name: "city", type: "string", required: true, description: "City name" }],
  };
  $("editor-title").textContent = tool ? tool.id : "New tool";
  $("editor-text").value = JSON.stringify(tool || template, null, 2);
  $("delete-tool").hidden = !tool;
  show($("editor-error"), "");
  $("editor").showModal();
}

async function change(button, action) {
  button.disabled = true;
  try {
    await action();
    $("editor").close();
    await refreshAll();
  } catch (error) {
    show($("editor-error"), error.message, "error");
  } finally {
    button.disabled = false;
  }
}

$("new-tool").addEventListener("click", () => openEditor(null));

$("save-tool").addEventListener("click", () => change($("save-tool"), async () => {
  let tool;
  try {
    tool = JSON.parse($("editor-text").value);
  } catch (error) {
    throw new Error(`This is not valid JSON: ${error.message}`);
  }
  if (state.editing === null) return api("/registry/tools", "POST", tool);
  return api(`/registry/tools/${encodeURIComponent(state.editing)}`, "PUT", tool);
}));

$("delete-tool").addEventListener("click", () => {
  if (!confirm(`Delete ${state.editing}?`)) return;
  change($("delete-tool"), () => api(`/registry/tools/${encodeURIComponent(state.editing)}`, "DELETE"));
});

/* ----------------------------------------------------------------- start */

$("refresh").addEventListener("click", refreshAll);
$("clear-history").addEventListener("click", async () => {
  if (!confirm("Delete all saved requests?")) return;
  try {
    await api("/history", "DELETE");
    await refreshHistory();
  } catch (error) {
    show($("problem"), error.message, "error");
  }
});

const base = new URL("..", location.href).href;
$("curl").textContent = `curl -X POST ${base}route \\\n  -H "Content-Type: application/json" \\\n  -d '{"query": "find the open issues"}'`;

/* A link such as /ui/#add jumps before the data has arrived, and the page then grows
 * above that section. Move there again, unless the visitor has already moved. */
let moved = false;
if (location.hash) history.scrollRestoration = "manual";   // the address decides, not the last position
for (const kind of ["wheel", "touchstart", "mousedown", "keydown"]) {
  window.addEventListener(kind, () => { moved = true; }, { once: true, passive: true });
}

refreshAll().then(() => {
  const section = location.hash && document.getElementById(location.hash.slice(1));
  if (section && !moved) section.scrollIntoView({ behavior: "instant" });
  document.documentElement.classList.add("ready");
});
