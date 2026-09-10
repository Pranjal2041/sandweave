/* The dashboard uses only same-origin, read-only monitoring endpoints. */
"use strict";
const $ = (selector, root = document) => root.querySelector(selector);
const esc = (value) =>
  String(value ?? "").replace(
    /[&<>"']/g,
    (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[
        c
      ],
  );
const labels = {
  overview: "Overview",
  workers: "Workers",
  gpus: "GPUs",
  sandboxes: "Sandboxes",
  pools: "Pools",
  jobs: "Jobs",
  tasks: "Tasks",
  snapshots: "Snapshots",
  events: "Events",
  logs: "Controller logs",
};
const descriptions = {
  overview: "Activity and capacity across your workers.",
  workers: "Worker health, eligible resources, and measured usage.",
  gpus: "Eligible GPUs across all workers, with device measurements and history.",
  sandboxes: "Follow each sandbox from placement to cleanup.",
  pools: "Ready reserves, active leases, and scheduling policies.",
  jobs: "Submitted workloads, task outcomes, and command output.",
  tasks: "Task attempts, assigned sandboxes, and output.",
  snapshots: "Saved state registered with the controller.",
  events: "Scheduling decisions and lifecycle changes, newest first.",
  logs: "Recent output from the controller process.",
};
const paths = {
  overview: "M3 3h7v7H3z M14 3h7v7h-7z M3 14h7v7H3z M14 14h7v7h-7z",
  workers: "M4 3h16v7H4z M4 14h16v7H4z M7 6h.01 M7 17h.01 M11 6h6 M11 17h6",
  sandboxes: "M12 2l9 5v10l-9 5-9-5V7z M3 7l9 5 9-5 M12 12v10",
  pools: "M3 3h7v7H3z M14 3h7v7h-7z M3 14h7v7H3z M14 14h7v7h-7z",
  jobs: "M5 3h14v18H5z M9 7h6 M9 11h6 M9 15h3",
  snapshots: "M3 6l9-4 9 4-9 4z M3 11l9 4 9-4 M3 16l9 4 9-4",
  events: "M3 12h4l3-8 4 16 3-8h4",
  logs: "M4 4h16v16H4z M7 8l3 3-3 3 M13 15h4",
  cpu: "M7 7h10v10H7z M9 3v4 M15 3v4 M9 17v4 M15 17v4 M3 9h4 M3 15h4 M17 9h4 M17 15h4",
  memory:
    "M3 7h18v10H3z M6 10v4 M10 10v4 M14 10v4 M18 10v4 M6 17v3 M10 17v3 M14 17v3 M18 17v3",
};
const icon = (name) =>
  `<svg class="icon" viewBox="0 0 24 24" aria-hidden="true"><path d="${paths[name] || paths.sandboxes}"/></svg>`;
document.querySelectorAll("[data-icon]").forEach((el) => {
  el.innerHTML = icon(el.dataset.icon);
});
const app = {
  section: "overview",
  authenticated: false,
  paused: false,
  busy: false,
  revision: 0,
  overview: null,
  offset: 0,
  before: 0,
  eventPages: [],
  query: {},
  detail: null,
  detailRevision: 0,
  logStream: "stdout",
  attempt: "",
  timer: null,
  lastSuccess: 0,
  failures: 0,
};
const fmt = (value, digits = 1) =>
  value == null || !Number.isFinite(Number(value))
    ? "—"
    : Number(value).toLocaleString(undefined, {
        maximumFractionDigits: digits,
      });
const bytes = (value) => {
  if (value == null) return "—";
  if (value === 0) return "0 B";
  const i = Math.min(
    4,
    Math.max(0, Math.floor(Math.log(Math.abs(value)) / Math.log(1024))),
  );
  return fmt(value / 1024 ** i, 1) + " " + ["B", "KiB", "MiB", "GiB", "TiB"][i];
};
const seconds = (value) =>
  value == null
    ? "—"
    : value < 60
      ? fmt(value) + "s"
      : value < 3600
        ? fmt(value / 60) + "m"
        : fmt(value / 3600) + "h";
const ago = (stamp) =>
  stamp == null
    ? "Never"
    : seconds(Math.max(0, Date.now() / 1000 - stamp)) + " ago";
const date = (stamp) =>
  stamp == null ? "—" : new Date(stamp * 1000).toLocaleString();
const short = (value) =>
  value
    ? value.length > 23
      ? value.slice(0, 11) + "…" + value.slice(-7)
      : value
    : "—";
const badge = (value) =>
  `<span class="badge ${["ready", "running", "leased", "succeeded", "registered"].includes(value) ? "good" : ["failed", "unreachable", "unknown"].includes(value) ? "bad" : ["pending", "waiting", "starting", "preparing", "paused", "draining"].includes(value) ? "warn" : ""}">${esc(value || "unknown")}</span>`;
const link = (kind, id, name) =>
  id
    ? `<button class="resource-link" data-kind="${esc(kind)}" data-id="${esc(id)}" title="${esc(id)}">${esc(name || short(id))}</button>`
    : '<span class="value-placeholder">—</span>';
const empty = (title, body = "") =>
  `<div class="empty"><strong>${esc(title)}</strong><span>${esc(body)}</span></div>`;
const panel = (title, subtitle, body, extra = "") =>
  `<section class="panel"><div class="panel-head"><div><h2>${esc(title)}</h2>${subtitle ? `<p>${esc(subtitle)}</p>` : ""}</div>${extra}</div>${body}</section>`;
const eventKind = (kind) =>
  ({
    worker: "workers",
    allocation: "sandboxes",
    pool: "pools",
    job: "jobs",
    task: "tasks",
    artifact: "snapshots",
  })[kind];

async function api(endpoint, query = {}, options = {}) {
  const response = await fetch(
    "api/" +
      endpoint +
      (Object.keys(query).length ? "?" + new URLSearchParams(query) : ""),
    {
      credentials: "same-origin",
      cache: "no-store",
      signal: AbortSignal.timeout(12000),
      ...options,
    },
  );
  let body;
  try {
    body = await response.json();
  } catch {
    throw new Error("The controller returned an unreadable response.");
  }
  if (response.status === 401) {
    showLogin(body.error);
    throw new Error(body.error);
  }
  if (!response.ok)
    throw new Error(body.error || `Request failed (${response.status}).`);
  return body;
}
function showLogin(error = "") {
  app.authenticated = false;
  clearTimeout(app.timer);
  $("#login").hidden = false;
  $("#app").hidden = true;
  $("#login-error").textContent = error;
  if ($("#detail-dialog").open) $("#detail-dialog").close();
}
async function signIn(body) {
  const button = $("#login-form button");
  button.disabled = true;
  try {
    await api(
      "session",
      {},
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      },
    );
    $("#token").value = "";
    await enter();
  } catch (error) {
    $("#login-error").textContent = error.message;
  } finally {
    button.disabled = false;
  }
}
$("#login-form").addEventListener("submit", (event) => {
  event.preventDefault();
  signIn({ token: $("#token").value.trim() });
});
$("#logout").addEventListener("click", async () => {
  try {
    await api("logout", {}, { method: "POST" });
    showLogin();
  } catch (error) {
    notice(error.message);
  }
});

function notice(message) {
  $("#notice").textContent = message;
  $("#notice").hidden = !message;
}
function connected() {
  const age = Date.now() / 1000 - (app.overview?.time || 0);
  const stale =
    app.failures > 0 || age > Math.max(20, (app.overview?.interval || 5) * 3);
  $("#connection-status").textContent = app.paused
    ? "Paused"
    : stale
      ? "Data is stale"
      : "Live";
  $("#live-dot").classList.toggle("off", app.paused || stale);
  $("#last-update").textContent = app.overview?.time
    ? "Updated " + ago(app.overview.time)
    : "Waiting for data";
}
function chart(points, key, format = fmt, bucket = 5) {
  const valid = points.filter((p) => p[key] != null && Number.isFinite(p[key]));
  if (!valid.length)
    return `<div class="chart-empty">No measurements in this time range.<br><span class="small">Samples appear as workers report them.</span></div>`;
  const width = 540,
    height = 184,
    left = 63,
    right = 18,
    top = 16,
    bottom = 31;
  const start = points[0].time,
    end = Math.max(start + bucket, points.at(-1).time);
  const max = Math.max(...valid.map((p) => p[key]), 1) * 1.15;
  const x = (t) =>
    left + ((t - start) / (end - start)) * (width - left - right);
  const y = (v) => height - bottom - (v / max) * (height - top - bottom);
  let line = "",
    previous = null;
  for (const p of points) {
    if (p[key] == null) {
      previous = null;
      continue;
    }
    line +=
      (previous && p.time - previous.time <= bucket * 3 ? " L" : " M") +
      x(p.time).toFixed(1) +
      "," +
      y(p[key]).toFixed(1);
    previous = p;
  }
  let grid = "";
  for (let i = 0; i < 4; i++) {
    const v = (max * i) / 3,
      py = y(v);
    grid += `<line class="gridline" x1="${left}" y1="${py}" x2="${width - right}" y2="${py}"/><text x="${left - 9}" y="${py + 3}" text-anchor="end">${esc(format(v))}</text>`;
  }
  const stamp = (t) =>
    new Date(t * 1000).toLocaleTimeString([], {
      hour: "2-digit",
      minute: "2-digit",
    });
  const dots = valid
    .map(
      (p) =>
        `<circle class="dot ${valid.length === 1 ? "single" : ""}" cx="${x(p.time)}" cy="${y(p[key])}" r="${valid.length === 1 ? 3 : 7}"><title>${esc(date(p.time))}: ${esc(format(p[key]))}</title></circle>`,
    )
    .join("");
  return `<div class="chart"><svg viewBox="0 0 ${width} ${height}" role="img" aria-label="${esc(key)} over time. Latest value ${esc(format(valid.at(-1)[key]))}">${grid}<path class="line" d="${line}"/>${dots}<text x="${left}" y="${height - 8}">${esc(stamp(start))}</text><text x="${width - right}" y="${height - 8}" text-anchor="end">${esc(stamp(end))}</text></svg></div><details class="chart-values"><summary>View sample values</summary><div class="table-wrap"><table><thead><tr><th>Time</th><th>Value</th></tr></thead><tbody>${valid
    .slice(-180)
    .reverse()
    .map(
      (p) =>
        `<tr><td>${esc(date(p.time))}</td><td>${esc(format(p[key]))}</td></tr>`,
    )
    .join("")}</tbody></table></div>`;
}
function metricCard(label, value, foot, name) {
  return `<section class="card"><div class="card-label">${esc(label)}${icon(name)}</div><div class="card-value">${value}</div><div class="card-foot">${foot}</div></section>`;
}
function capacity(label, used, total, format = fmt, name = "cpu") {
  const ratio = total ? Math.max(0, Math.min(100, (100 * used) / total)) : 0;
  return `<div class="capacity"><div class="capacity-icon">${icon(name)}</div><span class="capacity-label">${esc(label)}</span><div class="meter"><div class="meter-top"><span>${esc(format(used))} <span class="muted">/ ${esc(format(total))}</span></span><span class="muted">${esc(fmt(total ? (100 * used) / total : 0, 0))}%</span></div><svg viewBox="0 0 100 5" preserveAspectRatio="none" role="img" aria-label="${esc(label)}: ${esc(format(used))} of ${esc(format(total))}"><rect width="${ratio}" height="5" rx="2"/></svg></div></div>`;
}
function eventList(items) {
  if (!items.length)
    return empty(
      "No events yet",
      "Changes to workers and workloads will appear here.",
    );
  return `<div class="panel-body">${items.map((e) => `<div class="event"><span class="event-dot"></span><div><div class="event-message">${esc(e.detail.message || "State updated")}</div><div class="event-meta"><span>${esc(e.kind)}</span>${eventKind(e.kind) ? link(eventKind(e.kind), e.id) : esc(short(e.id))}${e.detail.state ? badge(e.detail.state) : ""}</div>${e.detail.error ? `<p class="small break muted">${esc(e.detail.error)}</p>` : ""}</div><time class="event-time" title="${esc(date(e.time))}">${esc(ago(e.time))}</time></div>`).join("")}</div>`;
}
function workerTable(items, compact = false) {
  if (!items.length)
    return empty(
      "No workers connected",
      "Join a worker with sandweave cluster join, using your controller address.",
    );
  return `<div class="table-wrap"><table><thead><tr><th>Worker</th><th>Status</th><th>Slots</th>${compact ? "" : "<th>CPU busy</th><th>Sandbox RSS</th><th>GPUs</th>"}<th>Last contact</th></tr></thead><tbody>${items.map((w) => `<tr><td>${link("workers", w.id, w.name || w.hostname || w.id)}<span class="id-sub">${esc(w.hostname || short(w.id))}</span></td><td>${badge(w.draining ? "draining" : w.state)}</td><td>${fmt(w.reserved.slots + (w.external?.slots || 0), 0)} <span class="muted">/ ${fmt(w.capacity.slots, 0)}</span></td>${compact ? "" : `<td>${w.telemetry_stale ? "—" : fmt(w.telemetry.cpu_busy_percent) + "%"}</td><td>${w.telemetry_stale ? "—" : bytes(w.telemetry.sandbox_rss_bytes)}</td><td>${fmt(w.capacity.gpu, 0)}</td>`}<td class="muted" title="${esc(date(w.seen))}">${esc(ago(w.seen))}</td></tr>`).join("")}</tbody></table></div>`;
}
function resourceTable(kind, items) {
  if (kind === "workers") return workerTable(items);
  if (!items.length)
    return empty(
      "No " + labels[kind].toLowerCase() + " found",
      "New resources appear automatically. Adjust your filters to include more results.",
    );
  let heads, rows;
  if (kind === "sandboxes") {
    heads = [
      "Sandbox",
      "State",
      "Template",
      "Worker",
      "Pool",
      "CPU / RSS",
      "Created",
    ];
    rows = items.map((a) => [
      link(kind, a.id, a.name || short(a.id)),
      badge(a.state),
      esc(a.template),
      link("workers", a.worker),
      link("pools", a.parent),
      esc(
        fmt(a.telemetry.cpu_cores) + " cores / " + bytes(a.telemetry.rss_bytes),
      ),
      esc(ago(a.created)),
    ]);
  } else if (kind === "gpus") {
    heads = [
      "GPU",
      "Worker",
      "State",
      "Utilization",
      "Device memory",
      "Temperature",
      "Power",
    ];
    rows = items.map((g) => [
      link(kind, g.id, g.name || g.uuid),
      link("workers", g.worker),
      badge(g.state),
      fmt(g.telemetry.utilization_percent) + "%",
      bytes(g.telemetry.memory_used_bytes) +
        " / " +
        bytes(g.telemetry.memory_total_bytes),
      fmt(g.telemetry.temperature_c) + " °C",
      fmt(g.telemetry.power_watts) + " W",
    ]);
  } else if (kind === "pools") {
    heads = [
      "Pool",
      "State",
      "Template",
      "Active",
      "Ready / warm",
      "Waiting",
      "Weight / priority",
    ];
    rows = items.map((p) => [
      link(kind, p.id, p.name || short(p.id)),
      badge(p.state),
      esc(p.template),
      fmt(p.active) + " / " + fmt(p.size),
      fmt(p.ready) + " / " + fmt(p.warm),
      fmt(p.waiting),
      fmt(p.weight) + " / " + fmt(p.priority),
    ]);
  } else if (kind === "jobs") {
    heads = ["Job", "State", "Pool", "Tasks", "Succeeded", "Failed", "Created"];
    rows = items.map((j) => [
      link(kind, j.id),
      badge(j.state),
      link("pools", j.pool),
      fmt(j.tasks),
      fmt(j.succeeded),
      fmt(j.failed),
      esc(ago(j.created)),
    ]);
  } else if (kind === "tasks") {
    heads = ["Task", "State", "Attempt", "Sandbox", "Exit code"];
    rows = items.map((t) => [
      link(kind, t.id, "Task " + t.index),
      badge(t.state),
      fmt(t.attempt, 0),
      link("sandboxes", t.sandbox),
      fmt(t.returncode, 0),
    ]);
  } else {
    heads = ["Snapshot", "State", "Replicas", "Created"];
    rows = items.map((s) => [
      link(kind, s.id),
      badge(s.state),
      fmt(s.replicas),
      esc(date(s.created)),
    ]);
  }
  return `<div class="table-wrap"><table><thead><tr>${heads.map((h) => "<th>" + h + "</th>").join("")}</tr></thead><tbody>${rows.map((r) => "<tr>" + r.map((c) => "<td>" + c + "</td>").join("") + "</tr>").join("")}</tbody></table></div>`;
}
function tableFooter(total, count) {
  return `<div class="table-foot"><span>${fmt(total, 0)} results${total ? " · " + fmt(app.offset + 1, 0) + "–" + fmt(app.offset + count, 0) : ""}</span><div class="pagination"><button data-page="prev" ${app.offset === 0 ? "disabled" : ""}>Previous</button><button data-page="next" ${app.offset + count >= total ? "disabled" : ""}>Next</button></div></div>`;
}
function filters() {
  if (app.section === "events")
    return `<div class="filters"><input id="search" type="search" aria-label="Search event resource IDs" placeholder="Search resource IDs or kinds…" value="${esc(app.query.q || "")}"><select id="event-kind" aria-label="Event kind"><option value="">All event kinds</option>${["worker", "allocation", "pool", "job", "task", "artifact"].map((k) => `<option ${app.query.kind === k ? "selected" : ""}>${k}</option>`).join("")}</select><button data-action="clear">Clear</button></div>`;
  return `<div class="filters"><input id="search" type="search" aria-label="Search resources" placeholder="Search names, IDs, labels…" value="${esc(app.query.q || "")}"><select id="state-filter" aria-label="Resource state"><option value="">All states</option>${["ready", "running", "leased", "pending", "waiting", "starting", "preparing", "paused", "unknown", "unreachable", "succeeded", "failed", "cancelled", "terminated", "stopped", "closed", "removed", "registered"].map((s) => `<option ${app.query.state === s ? "selected" : ""}>${s}</option>`).join("")}</select><input id="template-filter" type="text" aria-label="Filter template" placeholder="Template (exact name)" value="${esc(app.query.template || "")}"><select id="sort" aria-label="Sort resources"><option value="updated" ${app.query.sort === "updated" ? "selected" : ""}>Recently updated</option><option value="created" ${app.query.sort === "created" ? "selected" : ""}>Newest created</option><option value="name" ${app.query.sort === "name" ? "selected" : ""}>Name A–Z</option><option value="state" ${app.query.sort === "state" ? "selected" : ""}>State A–Z</option></select><button data-action="clear">Clear</button><button data-action="export" class="export">Export page</button></div>${app.query.worker || app.query.pool ? `<div class="filter-caption">Filtered by ${esc(app.query.worker ? "worker " + app.query.worker : "pool " + app.query.pool)}</div>` : ""}`;
}
function mountSection() {
  const section = app.section;
  $("#page-title").textContent =
    section === "overview" ? "Cluster overview" : labels[section];
  $("#page-description").textContent = descriptions[section];
  $("#crumb").textContent = labels[section];
  document.title = labels[section] + " · Sandweave";
  document.querySelectorAll("[data-nav]").forEach((el) => {
    const selected = el.dataset.nav === section;
    el.classList.toggle("active", selected);
    if (selected) el.setAttribute("aria-current", "page");
    else el.removeAttribute("aria-current");
  });
  $("#range").disabled = !(section === "overview" || app.detail);
  if (section === "overview")
    $("#content").innerHTML =
      '<div id="overview-content">' +
      empty("Loading cluster overview…") +
      "</div>";
  else if (section === "logs")
    $("#content").innerHTML =
      panel(
        "Controller health",
        "Current controller process and reconciliation activity.",
        '<dl id="controller-health" class="details-grid"></dl>',
      ) +
      '<div class="detail-section">' +
      panel(
        "Controller output",
        "The latest 64 KiB. Refreshes while this page is open.",
        `<div class="panel-body"><div class="log-toolbar"><button data-action="download-log">Download visible log</button></div><pre id="controller-output" class="log-window" tabindex="0"></pre><p id="controller-log-meta" class="log-meta"></p></div>`,
      ) +
      "</div>";
  else
    $("#content").innerHTML =
      filters() +
      '<section class="panel" id="resource-results">' +
      empty("Loading…") +
      "</section>";
}
async function renderOverview(overview, revision) {
  const [history, workers, events] = await Promise.all([
    api("history", { seconds: $("#range").value }),
    api("resources", { kind: "workers", limit: 5 }),
    api("events", { limit: 5 }),
  ]);
  if (revision !== app.revision) return;
  const s = overview.summary;
  if (!s.capacity) {
    $("#overview-content").innerHTML = empty(
      "Collecting the first sample",
      "The controller is ready. Its first monitoring sample will appear shortly.",
    );
    return;
  }
  const running =
    (s.sandbox_states.ready || 0) + (s.sandbox_states.leased || 0);
  const alerts = overview.alerts?.length
    ? `<section class="panel alert-list"><div class="panel-head"><h2>Needs attention <span class="badge warn">${fmt(overview.alert_count, 0)}</span></h2></div><div class="panel-body">${overview.alerts
        .slice(0, 4)
        .map(
          (a) =>
            `<div class="alert-item"><div><span class="badge ${a.severity === "error" ? "bad" : "warn"}">${esc(a.severity)}</span>${esc(a.message)}</div>${link(a.kind, a.id, "Inspect →")}</div>`,
        )
        .join("")}</div></section>`
    : "";
  $("#overview-content").innerHTML =
    `<div class="cards">${metricCard("Running sandboxes", fmt(running, 0), `${fmt(s.sandboxes, 0)} live · ${fmt(s.sandbox_states.paused || 0, 0)} paused`, "sandboxes")}${metricCard("Connected workers", fmt(s.ready_workers, 0) + ` <small>/ ${fmt(s.workers, 0)}</small>`, `${s.ready_workers === s.workers && s.workers ? '<span class="badge good">All reachable</span>' : fmt(s.workers - s.ready_workers, 0) + " unreachable"} · ${fmt(s.draining_workers, 0)} draining`, "workers")}${metricCard("Waiting for placement", fmt(s.pending, 0), `${fmt(s.unknown, 0)} sandboxes with unknown status`, "events")}${metricCard("Eligible CPU busy", fmt(s.cpu_busy_percent) + `<small>${s.cpu_busy_percent == null ? "" : "%"}</small>`, `${fmt(s.capacity.cpus, 0)} eligible cores · includes other processes`, "cpu")}</div>${alerts}<div class="grid-two">${panel("Sandbox CPU", "Measured host CPU cores used by sandbox processes", chart(history.points, "cpu_cores", (v) => fmt(v) + " cores", history.bucket_seconds) + '<p class="chart-note">Sampled process trees. Short-lived processes between samples may be missed.</p>')}${panel("Sandbox memory", "Sum of resident memory in sandbox process trees", chart(history.points, "rss_bytes", bytes, history.bucket_seconds) + `<p class="chart-note">${fmt(s.measured_sandboxes, 0)} of ${fmt(s.sandboxes, 0)} live sandboxes measured. Shared pages can be counted more than once.</p>`)}</div><div class="grid-wide">${panel("Reserved capacity", "Scheduling reservations, including other sandboxes on these workers", `<div class="panel-body">${capacity("Slots", s.reserved.slots, s.capacity.slots, fmt, "sandboxes")}${capacity("Memory", s.reserved.memory, s.capacity.memory, bytes, "memory")}${capacity("GPUs", s.reserved.gpu, s.capacity.gpu, fmt, "cpu")}</div><p class="chart-note">CPU cores are shared. Reserved memory is a budget, not measured usage.</p>`)}${panel("Workload outcomes", "Retained task records across all jobs", `<div class="summary-strip"><span><strong>${fmt(s.tasks.succeeded || 0, 0)}</strong> succeeded</span><span><strong>${fmt(s.tasks.failed || 0, 0)}</strong> failed</span><span><strong>${fmt(s.tasks.running || 0, 0)}</strong> running</span></div><div class="panel-body"><div class="capacity"><span class="muted small">Sandbox startup · last hour</span></div><div class="summary-strip"><span><strong>${seconds(s.startup.p50)}</strong> p50</span><span><strong>${seconds(s.startup.p95)}</strong> p95</span><span><strong>${fmt(s.startup.count, 0)}</strong> samples</span></div></div><p class="chart-note">Startup is the worker's ready time. Installation and queueing happen before it.</p>`, `<a href="#jobs">View jobs →</a>`)}</div><div class="grid-wide">${panel("Workers", "Placement and last acknowledged contact", workerTable(workers.items, true), '<a href="#workers">View all →</a>')}${panel("Recent activity", "Latest recorded cluster events", eventList(events.items), '<a href="#events">View all →</a>')}</div>`;
}
async function refresh(force = false) {
  if (
    !app.authenticated ||
    app.busy ||
    ((app.paused || document.hidden) && !force)
  )
    return;
  app.busy = true;
  const revision = app.revision;
  $("#refresh").disabled = true;
  try {
    const overview = await api("overview");
    if (revision !== app.revision) return;
    app.overview = overview;
    $("#cluster-id").textContent = overview.cluster_id.slice(0, 18);
    $("#count-workers").textContent = fmt(overview.summary.workers || 0, 0);
    $("#count-sandboxes").textContent = fmt(overview.summary.sandboxes || 0, 0);
    $("#retention").textContent =
      `Samples every ${overview.interval}s · Up to ${overview.history_hours}h retained`;
    const stale =
      overview.time &&
      Date.now() / 1000 - overview.time > Math.max(20, overview.interval * 3);
    notice(
      overview.error
        ? "Collection error: " + overview.error
        : overview.reconcile_error
          ? "Controller reconciliation error: " + overview.reconcile_error
          : stale
            ? "The last monitoring sample is stale. Displaying the last known state."
            : "",
    );
    if (app.section === "overview") await renderOverview(overview, revision);
    else if (app.section === "logs") {
      const c = overview.controller;
      if (c && $("#controller-health"))
        $("#controller-health").innerHTML =
          field("Host", esc(c.hostname)) +
          field("Uptime", seconds(overview.uptime_seconds)) +
          field("Resident memory", bytes(c.rss_bytes)) +
          field("Threads", fmt(c.threads, 0)) +
          field("Worker operations in flight", fmt(c.pending_operations, 0)) +
          field("Forwarded requests in flight", fmt(c.forwarding_requests, 0)) +
          field("Last reconciliation", esc(ago(overview.last_reconcile))) +
          field("Last sample", esc(ago(overview.time)));
      const log = await api("logs");
      if (revision !== app.revision) return;
      setLog($("#controller-output"), log);
      $("#controller-log-meta").textContent = logMeta(log);
    } else if (app.section === "events") {
      const events = await api("events", {
        ...app.query,
        before: app.before,
        limit: 50,
      });
      if (revision !== app.revision) return;
      app.page = events.items;
      app.nextBefore = events.items.at(-1)?.sequence;
      $("#resource-results").innerHTML =
        eventList(events.items) +
        `<div class="table-foot"><span>${app.before ? "Earlier events" : "Latest events"}</span><div class="pagination"><button data-events="newer" ${!app.eventPages.length ? "disabled" : ""}>Newer</button><button data-events="older" ${!events.more ? "disabled" : ""}>Older</button></div></div>`;
    } else {
      const result = await api("resources", {
        kind: app.section,
        offset: app.offset,
        limit: 50,
        ...app.query,
      });
      if (revision !== app.revision) return;
      app.page = result.items;
      $("#resource-results").innerHTML =
        resourceTable(app.section, result.items) +
        tableFooter(result.total, result.items.length);
    }
    app.lastSuccess = Date.now();
    app.failures = 0;
    if (app.detail && $("#detail-dialog").open) await renderDetail(false);
  } catch (error) {
    if (app.authenticated && revision === app.revision) {
      app.failures++;
      notice(
        "Unable to refresh: " +
          error.message +
          " Showing the last available data.",
      );
    }
  } finally {
    app.busy = false;
    $("#refresh").disabled = false;
    connected();
    clearTimeout(app.timer);
    if (app.authenticated)
      app.timer = setTimeout(
        () => refresh(),
        Math.min(
          30000,
          (app.overview?.interval || 5) * 1000 * 2 ** Math.min(app.failures, 3),
        ),
      );
    if (revision !== app.revision) refresh(true);
  }
}
function field(label, value) {
  return `<div><dt>${esc(label)}</dt><dd>${value}</dd></div>`;
}
function logMeta(log) {
  return [
    log.message,
    log.truncated ? "Showing the last 64 KiB. Earlier output is omitted." : "",
    log.bytes != null ? bytes(log.bytes) + " total" : "",
    log.returncode != null ? "Exit code " + log.returncode : "",
  ]
    .filter(Boolean)
    .join(" · ");
}
function setLog(el, log) {
  const nearEnd = el.scrollHeight - el.scrollTop - el.clientHeight < 40;
  const previous = el.scrollTop;
  el.textContent = log.text || log.message || "No output yet.";
  el.scrollTop = nearEnd ? el.scrollHeight : previous;
}
function logControls(kind) {
  return `<div class="log-toolbar">${(kind === "sandboxes" ? ["launcher", "runtime"] : ["stdout", "stderr"]).map((s) => `<button data-stream="${s}" class="${app.logStream === s ? "active" : ""}">${s}</button>`).join("")}<button data-action="download-detail-log">Download visible log</button></div><pre id="detail-output" class="log-window" tabindex="0"></pre><p id="detail-log-meta" class="log-meta"></p>`;
}
async function renderDetail(initial = true) {
  if (!app.detail) return;
  const { kind, id } = app.detail,
    revision = ++app.detailRevision;
  try {
    const value = await api("detail", { kind, identity: id });
    if (revision !== app.detailRevision) return;
    let fields =
      field("State", badge(value.draining ? "draining" : value.state)) +
      field("ID", `<span class="mono">${esc(value.id)}</span>`);
    let extra = "",
      related = "";
    if (kind === "workers") {
      fields +=
        field("Hostname", esc(value.hostname)) +
        field("CPU cores", fmt(value.capacity.cpus, 0)) +
        field("CPU IDs", esc(value.cpu_ids?.join(", "))) +
        field("Memory budget", bytes(value.capacity.memory)) +
        field(
          "Slots",
          fmt(value.reserved.slots) + " / " + fmt(value.capacity.slots),
        ) +
        field("Last contact", esc(date(value.seen))) +
        field(
          "Labels",
          esc(
            Object.entries(value.labels || {})
              .map(([k, v]) => k + "=" + v)
              .join(", ") || "None",
          ),
        ) +
        field(
          "Measurements",
          value.telemetry_stale
            ? '<span class="badge warn">Stale or unavailable</span>'
            : esc(date(value.telemetry.time)),
        );
      const m = value.telemetry;
      extra = panel(
        "Worker measurements",
        "Host memory, network, and storage include other workloads.",
        `<dl class="details-grid">${field("CPU busy on eligible cores", value.telemetry_stale ? "—" : fmt(m.cpu_busy_percent) + "%")}${field("Sandbox RSS", value.telemetry_stale ? "—" : bytes(m.sandbox_rss_bytes))}${field("Host memory available", bytes(m.host_memory_available_bytes))}${field("Storage available", bytes(m.disk_available_bytes) + " / " + bytes(m.disk_total_bytes))}${field("Host network received", bytes(m.network_receive_bytes_per_second) + "/s")}${field("Host network sent", bytes(m.network_send_bytes_per_second) + "/s")}</dl>`,
      );
      const gpus = value.gpus || [];
      extra += `<section class="detail-section"><h2>GPUs (${gpus.length})</h2>${
        gpus.length
          ? gpus
              .map((g) => {
                const m =
                  value.telemetry.gpus?.find((v) => v.uuid === g.uuid) || {};
                return `<section class="panel"><dl class="details-grid">${field("Model", esc(g.model))}${field("UUID", `<span class="mono">${esc(g.uuid)}</span>`)}${field("Device utilization", value.telemetry_stale ? "—" : fmt(m.utilization_percent) + "%")}${field("Device memory", bytes(m.memory_used_bytes) + " / " + bytes(m.memory_total_bytes))}${field("Temperature", fmt(m.temperature_c) + " °C")}${field("Power", fmt(m.power_watts) + " W")}</dl></section>`;
              })
              .join("")
          : empty("No GPUs assigned", "This worker contributes CPU capacity.")
      }<p class="tabs-caption">Device readings include all processes using that GPU.${value.telemetry.gpu_error ? " " + esc(value.telemetry.gpu_error) : ""}</p></section>`;
      related = `<button data-filter-worker="${esc(id)}">View this worker's sandboxes →</button>`;
    } else if (kind === "gpus") {
      fields +=
        field("Model", esc(value.name)) +
        field("UUID", `<span class="mono">${esc(value.uuid)}</span>`) +
        field("Worker", link("workers", value.worker)) +
        field(
          "Device utilization",
          fmt(value.telemetry.utilization_percent) + "%",
        ) +
        field(
          "Device memory",
          bytes(value.telemetry.memory_used_bytes) +
            " / " +
            bytes(value.telemetry.memory_total_bytes),
        ) +
        field("Temperature", fmt(value.telemetry.temperature_c) + " °C") +
        field("Power", fmt(value.telemetry.power_watts) + " W");
      extra =
        '<p class="tabs-caption">Device measurements include all processes on the GPU. They are not per-sandbox usage.</p>';
    } else if (kind === "sandboxes") {
      fields +=
        field("Template", esc(value.template)) +
        field("Runtime", esc(value.runtime)) +
        field("Worker", link("workers", value.worker)) +
        field("Pool", link("pools", value.parent)) +
        field("Virtual CPUs", fmt(value.resources.cpu.vcpus)) +
        field("CPU weight", fmt(value.resources.cpu.weight)) +
        field("Guest memory", esc(value.resources.memory.guest)) +
        field("Runtime memory", esc(value.resources.memory.runtime)) +
        field("CPU measured", fmt(value.telemetry.cpu_cores) + " cores") +
        field("Resident memory", bytes(value.telemetry.rss_bytes)) +
        field("Startup", seconds(value.timings.ready_seconds)) +
        field("Created", esc(date(value.created)));
      extra = `<section class="detail-section"><h2>Runtime logs</h2>${logControls(kind)}</section>`;
    } else if (kind === "pools") {
      for (const [label, key] of [
        ["Template", "template"],
        ["Capacity", "size"],
        ["Warm target", "warm"],
        ["Ready", "ready"],
        ["Active", "active"],
        ["Waiting leases", "waiting"],
        ["Weight", "weight"],
        ["Priority", "priority"],
        ["Placement", "placement"],
      ])
        fields += field(label, esc(value[key]));
      fields += field("Labels", esc(JSON.stringify(value.labels || {})));
      related = `<button data-filter-pool="${esc(id)}">View this pool's sandboxes →</button>`;
    } else if (kind === "jobs") {
      fields +=
        field("Pool", link("pools", value.pool)) +
        field("Tasks", fmt(value.tasks)) +
        field("Succeeded", fmt(value.succeeded)) +
        field("Failed", fmt(value.failed)) +
        field("Created", esc(date(value.created)));
      const tasks = await api("resources", {
        kind: "tasks",
        pool: id,
        limit: 200,
        sort: "created",
        direction: "asc",
      });
      if (revision !== app.detailRevision) return;
      extra = `<section class="detail-section"><h2>Tasks</h2><div class="panel">${resourceTable("tasks", tasks.items)}</div><p class="tabs-caption">Select a task to view stdout, stderr, and saved attempts.${tasks.total > 200 ? " Showing the first 200 tasks." : ""}</p></section>`;
      related = `<button data-filter-job="${esc(id)}">View all tasks →</button>`;
    } else if (kind === "tasks") {
      fields +=
        field("Job", link("jobs", value.parent)) +
        field("Sandbox", link("sandboxes", value.sandbox)) +
        field("Worker", link("workers", value.worker)) +
        field("Current attempt", fmt(value.attempt, 0)) +
        field("Exit code", fmt(value.returncode, 0));
      extra = `<section class="detail-section"><h2>Command output</h2><div class="log-toolbar"><label for="attempt-select">Attempt</label><select id="attempt-select"><option value="">Current / latest</option>${(value.attempts || []).map((a) => `<option value="${esc(a.id)}" ${app.attempt === a.id ? "selected" : ""}>${esc(a.id.split("-").at(-1))} · exit ${esc(a.returncode)}${a.timed_out ? " · timed out" : ""}</option>`).join("")}</select></div>${logControls(kind)}</section>`;
    } else if (kind === "snapshots")
      fields +=
        field("Replicas", fmt(value.replicas)) +
        field("Created", esc(date(value.created)));
    const html = `<h1 id="detail-title" class="detail-title">${esc(value.name || (kind === "tasks" ? "Task " + value.index : short(value.id)))}</h1><p class="detail-id">${esc(value.id)}</p><div id="detail-notice">${value.error || value.reason ? `<div class="notice">${esc(value.error || value.reason)}</div>` : ""}</div><div class="panel"><dl id="detail-fields" class="details-grid">${fields}</dl></div><div class="detail-actions">${related}</div>${extra}<section class="detail-section" id="detail-history"></section><section class="detail-section" id="detail-events"></section><details class="raw-record"><summary>View monitoring record</summary><pre>${esc(JSON.stringify(value, null, 2))}</pre></details>`;
    // Retain log selection and scroll position through polling.
    if (initial || !$("#detail-output")) $("#detail-content").innerHTML = html;
    else if (kind !== "tasks" && kind !== "sandboxes")
      $("#detail-content").innerHTML = html;
    else {
      $("#detail-fields").innerHTML = fields;
      $("#detail-notice").innerHTML =
        value.error || value.reason
          ? `<div class="notice">${esc(value.error || value.reason)}</div>`
          : "";
      $(".raw-record pre").textContent = JSON.stringify(value, null, 2);
      if (
        kind === "tasks" &&
        $("#attempt-select") &&
        document.activeElement !== $("#attempt-select")
      ) {
        $("#attempt-select").innerHTML =
          '<option value="">Current / latest</option>' +
          (value.attempts || [])
            .map(
              (a) =>
                `<option value="${esc(a.id)}" ${app.attempt === a.id ? "selected" : ""}>${esc(a.id.split("-").at(-1))} · exit ${esc(a.returncode)}${a.timed_out ? " · timed out" : ""}</option>`,
            )
            .join("");
      }
    }
    if (kind === "tasks" || kind === "sandboxes") await loadDetailLog();
    const history = await api("history", {
      kind,
      identity: id,
      seconds: $("#range").value,
    });
    if (revision !== app.detailRevision) return;
    if (history.points.length) {
      const key =
        kind === "gpus"
          ? "utilization_percent"
          : kind === "workers"
            ? "cpu_busy_percent"
            : kind === "pools"
              ? "active"
              : "cpu_cores";
      $("#detail-history").innerHTML = panel(
        kind === "gpus"
          ? "GPU utilization"
          : kind === "workers"
            ? "Eligible CPU busy"
            : kind === "pools"
              ? "Active leases"
              : "Sandbox CPU",
        "Selected time range",
        chart(
          history.points,
          key,
          kind === "gpus" || kind === "workers" ? (v) => fmt(v) + "%" : fmt,
          history.bucket_seconds,
        ),
      );
    }
    const events = await api("events", { identity: id, limit: 8 });
    if (revision !== app.detailRevision) return;
    $("#detail-events").innerHTML = panel(
      "Resource activity",
      "Latest lifecycle events",
      eventList(events.items),
    );
  } catch (error) {
    if (revision === app.detailRevision) {
      if (initial)
        $("#detail-content").innerHTML = empty(
          "Unable to load this resource",
          error.message,
        );
      else notice("Unable to update resource details: " + error.message);
    }
  }
}
async function loadDetailLog() {
  if (
    !app.detail ||
    !["tasks", "sandboxes"].includes(app.detail.kind) ||
    !$("#detail-output")
  )
    return;
  const detail = app.detail,
    stream = app.logStream,
    attempt = app.attempt;
  try {
    const log = await api("logs", {
      kind: detail.kind,
      identity: detail.id,
      stream,
      attempt,
    });
    if (
      app.detail !== detail ||
      stream !== app.logStream ||
      attempt !== app.attempt
    )
      return;
    setLog($("#detail-output"), log);
    $("#detail-log-meta").textContent = logMeta(log);
  } catch (error) {
    if (app.detail === detail && $("#detail-log-meta"))
      $("#detail-log-meta").textContent = error.message;
  }
}
function openDetail(kind, id) {
  if (!labels[kind]) return;
  app.detail = { kind, id };
  app.attempt = "";
  app.logStream = kind === "sandboxes" ? "launcher" : "stdout";
  $("#detail-kind").textContent = labels[kind] + " / DETAILS";
  $("#detail-content").innerHTML = empty("Loading resource…");
  if (!$("#detail-dialog").open) $("#detail-dialog").showModal();
  $("#range").disabled = false;
  $("#detail-range").value = $("#range").value;
  $("#detail-range").hidden = ![
    "workers",
    "gpus",
    "sandboxes",
    "pools",
  ].includes(kind);
  renderDetail();
}
function route() {
  const raw = location.hash.slice(1).split("/");
  let section = raw[0] || "overview";
  if (!labels[section]) section = "overview";
  if (section !== app.section) {
    app.section = section;
    app.offset = 0;
    app.before = 0;
    app.eventPages = [];
    app.query = {};
  }
  app.revision++;
  mountSection();
  if (raw[1]) {
    try {
      openDetail(section, decodeURIComponent(raw.slice(1).join("/")));
    } catch {
      notice("Invalid resource link.");
    }
  } else {
    app.detail = null;
    app.detailRevision++;
    if ($("#detail-dialog").open) $("#detail-dialog").close();
  }
  refresh(true);
}
function closeDetail() {
  app.detail = null;
  app.detailRevision++;
  $("#detail-dialog").close();
  if (location.hash.slice(1).includes("/"))
    history.replaceState(null, "", "#" + app.section);
  $("#range").disabled = app.section !== "overview";
}
$("#detail-close").addEventListener("click", closeDetail);
$("#detail-dialog").addEventListener("cancel", (event) => {
  event.preventDefault();
  closeDetail();
});
$("#detail-dialog").addEventListener("click", (event) => {
  if (event.target === $("#detail-dialog")) {
    const r = event.target.getBoundingClientRect();
    if (event.clientX < r.left || event.clientX > r.right) closeDetail();
  }
});
window.addEventListener("hashchange", () => {
  if (app.authenticated) route();
});
$("#refresh").addEventListener("click", () => refresh(true));
$("#pause").addEventListener("click", () => {
  app.paused = !app.paused;
  $("#pause").textContent = app.paused ? "Resume" : "Pause";
  $("#pause").setAttribute("aria-pressed", String(app.paused));
  connected();
  if (!app.paused) refresh(true);
});
$("#range").addEventListener("change", () => {
  app.revision++;
  refresh(true);
});
$("#detail-range").addEventListener("change", () => {
  $("#range").value = $("#detail-range").value;
  renderDetail(false);
});
document.addEventListener("visibilitychange", () => {
  if (!document.hidden && app.authenticated) refresh(true);
});
window.addEventListener("online", () => refresh(true));
function download(text, name, type = "text/plain") {
  const url = URL.createObjectURL(new Blob([text], { type }));
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = name;
  anchor.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
document.addEventListener("click", (event) => {
  const el = event.target.closest("button");
  if (!el) return;
  if (el.dataset.kind) {
    history.replaceState(
      null,
      "",
      "#" + el.dataset.kind + "/" + encodeURIComponent(el.dataset.id),
    );
    openDetail(el.dataset.kind, el.dataset.id);
  }
  if (el.dataset.page) {
    app.offset = Math.max(
      0,
      app.offset + (el.dataset.page === "next" ? 50 : -50),
    );
    app.revision++;
    refresh(true);
  }
  if (el.dataset.events) {
    if (el.dataset.events === "older") {
      app.eventPages.push(app.before);
      app.before = app.nextBefore;
    } else app.before = app.eventPages.pop() || 0;
    app.revision++;
    refresh(true);
  }
  if (el.dataset.stream) {
    app.logStream = el.dataset.stream;
    document
      .querySelectorAll("[data-stream]")
      .forEach((b) =>
        b.classList.toggle("active", b.dataset.stream === app.logStream),
      );
    loadDetailLog();
  }
  if (
    el.dataset.filterWorker ||
    el.dataset.filterPool ||
    el.dataset.filterJob
  ) {
    app.section = el.dataset.filterJob ? "tasks" : "sandboxes";
    app.query = el.dataset.filterWorker
      ? { worker: el.dataset.filterWorker }
      : { pool: el.dataset.filterJob || el.dataset.filterPool };
    app.offset = 0;
    app.revision++;
    closeDetail();
    history.replaceState(null, "", "#" + app.section);
    mountSection();
    refresh(true);
  }
  if (el.dataset.action === "clear") {
    app.query = {};
    app.offset = app.before = 0;
    app.eventPages = [];
    app.revision++;
    mountSection();
    refresh(true);
  }
  if (el.dataset.action === "export")
    download(
      JSON.stringify(app.page || [], null, 2),
      "sandweave-" + app.section + ".json",
      "application/json",
    );
  if (el.dataset.action === "download-log")
    download($("#controller-output").textContent, "controller.log");
  if (el.dataset.action === "download-detail-log")
    download(
      $("#detail-output").textContent,
      app.detail.id + "-" + app.logStream + ".log",
    );
});
let searchTimer;
document.addEventListener("input", (event) => {
  if (!["search", "template-filter"].includes(event.target.id)) return;
  const key = event.target.id === "search" ? "q" : "template";
  app.query[key] = event.target.value;
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => {
    app.offset = app.before = 0;
    app.revision++;
    refresh(true);
  }, 250);
});
document.addEventListener("change", (event) => {
  if (event.target.id === "attempt-select") {
    app.attempt = event.target.value;
    loadDetailLog();
    return;
  }
  const key = { "state-filter": "state", "event-kind": "kind", sort: "sort" }[
    event.target.id
  ];
  if (!key) return;
  app.query[key] = event.target.value;
  if (key === "sort")
    app.query.direction = ["name", "state"].includes(event.target.value)
      ? "asc"
      : "desc";
  app.offset = app.before = 0;
  app.revision++;
  refresh(true);
});
async function enter() {
  app.authenticated = true;
  $("#login").hidden = true;
  $("#app").hidden = false;
  route();
}
(async () => {
  const hash = new URLSearchParams(location.hash.slice(1));
  if (hash.has("ticket") || hash.has("token")) {
    const key = hash.has("ticket") ? "ticket" : "token";
    const value = hash.get(key);
    history.replaceState(null, "", location.pathname + location.search);
    showLogin();
    await signIn({ [key]: value });
    return;
  }
  try {
    app.overview = await api("overview");
    await enter();
  } catch (error) {
    showLogin(
      error.message === "Sign in to view this cluster." ? "" : error.message,
    );
  }
})();
