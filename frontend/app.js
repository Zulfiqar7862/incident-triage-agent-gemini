// Operator console: plain JS, no build step.
// Run state is polled; the trace arrives over Server-Sent Events.
// Sections: state, pure helpers, API client, banner + actions, SSE stream, renderers, data loading, handlers.

// ===========================================================================
// State
// ===========================================================================
const $ = (id) => document.getElementById(id);
let current = null, es = null, lastEventId = 0, pollTimer = null;
let bannerKind = null;

// ===========================================================================
// Pure helpers (string in, string out)
// ===========================================================================
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const label = (status) => status.replace("_", " ");
const statusPill = (status) => `<span class="status s-${status}">${label(status)}</span>`;
const time = (ts) => new Date(ts * 1000).toLocaleTimeString();
const usd = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD", minimumFractionDigits: 4 });
const call = (tool, args) => `<code translate="no">${esc(tool)}(${esc(JSON.stringify(args))})</code>`;
// "top runbooks: ['a', 'b']" -> "top runbooks: a, b"
const plainList = (text) => String(text).replace(/\[([^\]]*)\]/g, (_, inner) => inner.replace(/['"]/g, ""));
const humanize = (key) => key.replace(/_/g, " ").replace(/^./, (c) => c.toUpperCase());
const humanValue = (v) => (typeof v === "object" ? Object.entries(v).map(([k, x]) => `${humanize(k)}: ${x}`).join(", ") : String(v));
// model text sometimes embeds the raw call ("Proposing rollback_deployment {...}"); the card shows it as fields instead
const plainReason = (text) => (text || "")
  .replace(/Proposing \w+ \{[^}]*\}\s*-\s*this changes production so it needs approval\.?/g,
    "It proposes the change on the left. That changes production, so it needs your approval.")
  .replace(/Proposing \w+ \{[^}]*\}/g, "It proposes the change on the left")
  .replace(/\s+/g, " ").trim();

// Provider errors can carry a long JSON dump: show the first sentence, keep the rest one click away.
function errorHtml(error) {
  const cut = error.search(/\s[{[]/);
  const short = cut > 0 ? error.slice(0, cut).replace(/[.\s]+$/, "") : error;
  return `<b>The run stopped.</b> ${esc(short)}. Press <b>Resume run</b> to continue from the last checkpoint.`
    + (cut > 0 ? `<details><summary>Show full error</summary><pre>${esc(error)}</pre></details>` : "");
}

// ===========================================================================
// API client
// ===========================================================================
async function api(path, opts = {}) {
  let r;
  try {
    r = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts });
  } catch {
    throw new Error("Can't reach the server. Check that it is running (make quickstart).");
  }
  const body = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(body.detail || `${r.status} ${r.statusText}`);
  return body;
}
const post = (path, data) => api(path, { method: "POST", body: JSON.stringify(data || {}) });

// ===========================================================================
// Error banner and button actions
// ===========================================================================
// Error banner: background polling errors clear themselves once the server answers again;
// errors from a button press stay until the next action or Retry.
function showError(msg, kind = "action") {
  bannerKind = kind;
  $("banner-text").innerHTML = `<b>${kind === "conn" ? "Connection problem." : "That didn't work."}</b> ${esc(msg)}`;
  $("banner").hidden = false;
}
function clearError(kind) {
  if (!kind || bannerKind === kind) { $("banner").hidden = true; bannerKind = null; }
}
// Run a button's action with a loading state; failures go to the banner.
async function act(btn, busyText, fn) {
  const text = btn.textContent;
  btn.disabled = true; btn.setAttribute("aria-busy", "true"); btn.textContent = busyText;
  try { clearError(); await fn(); } catch (err) { showError(err.message); } finally {
    btn.removeAttribute("aria-busy"); btn.textContent = text; btn.disabled = false;
    if (current) refresh();
  }
}
const poll = (fn) => async () => {
  try { await fn(); clearError("conn"); } catch (err) {
    showError(`${err.message} Retrying automatically.`, "conn");
    if (es && es.readyState !== EventSource.CLOSED) setConn("lost", "Disconnected. Reconnecting…");
  }
};

// ===========================================================================
// Live trace stream (SSE)
// ===========================================================================
function setConn(kind, text) {
  $("conn").className = `conn ${kind}`;
  $("conn").textContent = text;
}

function openStream(id) {
  if (es) es.close();
  setConn("", "Connecting…");
  es = new EventSource(`/api/runs/${id}/stream`);
  es.onopen = () => setConn("live", "Live");
  // EventSource reconnects by itself; events already shown are skipped by id
  es.onerror = () => setConn("lost", es.readyState === EventSource.CLOSED ? "Disconnected. Reload to reconnect."
    : "Disconnected. Reconnecting…");
  es.addEventListener("trace", (m) => {
    const e = JSON.parse(m.data);
    if (e.id > lastEventId) { lastEventId = e.id; addEvent(e); think.add(e); refresh(); }
  });
  es.addEventListener("end", () => { es.close(); setConn("", "Trace complete"); });
}

// ===========================================================================
// Renderers
// ===========================================================================
const planHtml = (plan) => (plan || []).map((p) => `<li>${esc(p)}</li>`).join("")
  || '<li class="state">The agent writes its plan in the first step.</li>';

const infraHtml = (infra) => Object.entries(infra).map(([n, s]) =>
  `<tr><td><b>${esc(n)}</b></td><td>${statusPill(s.status)}</td><td>${esc(s.version)}</td><td>${s.replicas} pods</td></tr>`).join("");

const reportHtml = (rep, status) => `<h2>${status === "escalated" ? "Handed to a human" : "Incident report"}</h2>
      <p><b>Root cause:</b> ${esc(rep.root_cause)}</p>
      <p><b>Severity:</b> ${esc(rep.severity)}. <b>Confidence:</b> ${Math.round((rep.confidence || 0) * 100)}%</p>
      <p><b>Why this outcome:</b> ${esc(rep.routing_reason || "")}</p>
      <p><b>Actions taken:</b> ${esc((rep.actions_taken || []).join("; ") || "none")}</p>
      <p><b>Follow-ups:</b></p><ul>${(rep.follow_ups || []).map((f) => `<li>${esc(f)}</li>`).join("")}</ul>`;

function renderRunList(runs) {
  $("runs").innerHTML = runs.map((r) => `<li data-id="${r.id}" class="${r.id === current ? "active" : ""}">
      <span>${esc(r.title)}<br><span class="sub">Started ${time(r.created_at)}</span></span>
      ${statusPill(r.status)}</li>`).join("") || '<li class="state">No runs yet. Pick an incident above and press Start triage.</li>';
  document.querySelectorAll("#runs li[data-id]").forEach((li) => (li.onclick = () => select(li.dataset.id)));
}

function renderMemory(mem) {
  $("memory").innerHTML = mem.map((m) => `<li><b>${esc(m.service)}</b>: ${esc(m.root_cause)}</li>`).join("")
    || '<li class="state">Nothing yet. Each resolved incident is remembered here and used in later runs.</li>';
}

function renderRun(r) {
  $("run-title").textContent = r.title;
  $("run-alert").textContent = r.alert;
  $("run-status").className = `status s-${r.status}`;
  $("run-status").textContent = label(r.status);
  $("st-step").textContent = r.step;
  think.setRun(r);
  $("st-tokens").textContent = (r.tokens_in + r.tokens_out).toLocaleString();
  $("st-cost").textContent = usd.format(r.cost_usd);
  $("st-llm").textContent = r.llm_mode === "gemini" ? "Gemini" : "Offline mock";
  $("pause").disabled = r.status !== "running";
  $("resume").disabled = !["paused", "failed"].includes(r.status);
  $("cancel").disabled = ["completed", "escalated", "cancelled"].includes(r.status);
  $("run-error").hidden = !r.error;
  $("run-error").innerHTML = r.error ? errorHtml(r.error) : "";
  const waiting = (r.approvals || []).filter((a) => a.status === "pending").length;
  $("st-approvals").textContent = waiting ? `${waiting} waiting for you` : "None pending";
  $("st-approvals").classList.toggle("waiting", waiting > 0);
  $("plan").innerHTML = planHtml(r.plan);
  $("infra").innerHTML = infraHtml(r.infra);
  const ap = r.status === "waiting_approval" ? r.pending_approval : null;
  const appeared = ap && $("approval").hidden;
  $("approval").hidden = !ap;
  if (ap) renderApproval(ap, r);
  if (appeared) $("approval").focus();              // move focus to the decision, never onto Approve itself
  const decided = (r.approvals || []).filter((a) => a.status !== "pending").slice(-1)[0];
  $("ap-receipt").hidden = !!ap || !decided;
  if (decided && !ap) $("ap-receipt").innerHTML = receiptHtml(decided);
  const rep = r.report;
  $("report").hidden = !rep;
  if (rep) {
    $("report").className = "panel report " + r.status;
    $("report").innerHTML = reportHtml(rep, r.status);
  }
}

// Approval card: a plain-language review of the proposed change, built only from
// data the run already has (pending approval, steps, tool summaries).
const ACTIONS = {
  restart_service: {
    title: (a) => `Restart service ${a.service}`,
    effect: "All pods of the service are recycled. Requests in flight may fail briefly.",
  },
  rollback_deployment: {
    title: (a, r) => {
      const from = r.infra?.[a.service]?.version;
      return `Roll back ${a.service}${from ? ` from ${from}` : ""} to ${a.to_version}`;
    },
    effect: "The service is redeployed on the earlier version. Changes in the newer release are undone.",
  },
  scale_service: {
    title: (a, r) => {
      const from = r.infra?.[a.service]?.replicas;
      return `Scale ${a.service}${from ? ` from ${from}` : ""} to ${a.replicas} replicas`;
    },
    effect: "Pods are added or removed. Cost changes with the number of replicas.",
  },
};
const FIELDS = {
  service: ["Service", "The service this change applies to"],
  to_version: ["Target version", "The version the service runs after the rollback"],
  replicas: ["Replicas", "How many pods the service runs after the change"],
};
const FINDING = { get_metrics: "Metrics", get_logs: "Logs", get_recent_deployments: "Latest deploy", search_runbooks: "Runbook" };

// After a decision the card collapses to a one-line receipt.
function receiptHtml(a) {
  const action = ACTIONS[a.tool];
  const what = action ? action.title(a.args, {}) : `Run ${humanize(a.tool).toLowerCase()}`;
  return `<b>You ${esc(a.status)}:</b> ${esc(what)}${a.comment ? ` <span class="meta">"${esc(a.comment)}"</span>` : ""}`;
}

function renderApproval(ap, r) {
  const action = ACTIONS[ap.tool];
  $("ap-title").textContent = action ? action.title(ap.args, r) : `Run ${humanize(ap.tool).toLowerCase()}`;
  const rows = Object.entries(ap.args).map(([k, v]) => {
    const [name, help] = FIELDS[k] || [humanize(k), ""];
    return `<div><dt>${esc(name)}</dt><dd>${esc(humanValue(v))}${help ? `<span>${esc(help)}</span>` : ""}</dd></div>`;
  });
  if (action) rows.push(`<div><dt>Effect</dt><dd>${esc(action.effect)}</dd></div>`);
  $("ap-fields").innerHTML = rows.join("");

  const steps = r.steps || [];
  const earlier = steps.filter((s) => s.n < ap.step && s.thought).slice(-1)[0];
  $("ap-reason").textContent = plainReason(ap.reason) || "The agent gave no reason.";
  $("ap-earlier").textContent = earlier ? `Before that: ${plainReason(earlier.thought)}` : "";

  const texts = [ap.reason, ...steps.map((s) => s.thought)].filter(Boolean).reverse();
  const m = texts.map((t) => t.match(/confidence[^0-9]{0,12}(\d+(?:\.\d+)?)\s*(%)?/i)).find(Boolean);
  const pct = m ? Math.round(m[2] || parseFloat(m[1]) > 1 ? parseFloat(m[1]) : parseFloat(m[1]) * 100) : null;
  $("ap-confidence").hidden = pct === null || pct > 100;
  if (pct !== null) { $("ap-conf-value").textContent = `${pct}%`; $("ap-conf-bar").style.width = `${pct}%`; }

  $("ap-incident").textContent = `${r.title}. ${r.alert}`;
  const latest = new Map();
  for (const s of steps) for (const c of s.calls || []) {
    if (c.ok && !c.risky && !["list_services", "update_plan", "finish_triage"].includes(c.tool)) latest.set(c.tool, c.summary);
  }
  $("ap-findings").innerHTML = [...latest].slice(-4).map(([tool, summary]) =>
    `<li><b>${esc(FINDING[tool] || humanize(tool))}:</b> ${esc(plainList(summary))}</li>`).join("")
    || '<li class="state">No findings recorded yet.</li>';
}

// Each trace event becomes [kind label | details].
function describe(t, d) {
  switch (t) {
    case "run_created": return ["Incident", `${esc(d.alert)} <span class="meta">(model: ${esc(d.llm)})</span>`];
    case "llm_call": return ["Model call", `<span class="meta">${esc(d.model)}: ${d.tokens_in} tokens in, ${d.tokens_out} out, ${d.latency_ms} ms, $${d.cost_usd}. Context about ${d.context_layers.total_tokens_est} tokens (${d.context_layers.steps_in_full} steps in full, ${d.context_layers.steps_compacted} summarized), ${d.memories_used} past incidents.</span>`];
    case "thought": return ["Thinking", esc(d.text)];
    case "plan": case "plan_revised": return [t === "plan" ? "Plan" : "Plan revised", `<ol>${d.steps.map((s) => `<li>${esc(s)}</li>`).join("")}</ol>`];
    case "tool_call": return ["Tool call", call(d.tool, d.args)];
    case "tool_result": return [d.ok ? "Result" : "Tool error",
      `<b>${esc(d.tool)}</b>: ${esc(d.ok ? d.summary : d.error)} <span class="meta">(${d.latency_ms} ms, ${d.attempts} attempt${d.attempts === 1 ? "" : "s"})</span>`
      + (d.output ? `<details><summary>Show raw output</summary><pre>${esc(d.output)}</pre></details>` : "")];
    case "retry": return ["Retry", `<b>${esc(d.tool)}</b> needed ${d.attempts} attempts with backoff and ${d.ok ? "recovered" : "gave up"}.`];
    case "approval_requested": return ["Needs approval", call(d.tool, d.args)];
    case "approval_decided": return ["Decision", `Operator ${esc(d.decision)} ${esc(d.tool)}${d.comment ? `: "${esc(d.comment)}"` : ""}`];
    case "guidance": return ["Your message", esc(d.message)];
    case "warning": return ["Warning", esc(d.message)];
    case "error": return ["Error", esc(d.message)];
    case "status": return ["Status", `${label(d.from)} to <b>${label(d.to)}</b> (${esc(d.why)})`];
    case "run_finished": return ["Finished", `<b>${label(d.status)}</b>: ${esc(d.report.root_cause)}`];
    case "memory_saved": return ["Remembered", `Saved to long-term memory: ${esc(d.services.join(", "))}`];
    case "resumed_after_restart": return ["Resumed", "Server restarted; continued from the last checkpoint."];
    default: return [t, esc(JSON.stringify(d))];
  }
}

function addEvent(e) {
  const [kind, html] = describe(e.type, e.data);
  const div = document.createElement("div");
  div.className = `ev ev-${e.type}${e.data.ok === false ? " ev-fail" : ""}`;
  div.innerHTML = `<div class="ev-kind">${kind}<span class="ev-when">Step ${e.step}, ${time(e.ts)}</span></div><div class="ev-body">${html}</div>`;
  $("timeline").appendChild(div);
  $("trace-empty").hidden = true;
}

// Thinking panel: a one-line live summary of what the agent is doing, built only
// from trace events the backend already sends. Click to see the reasoning.
const PHASES = ["Understanding the incident", "Planning", "Acting", "Concluding"];
const think = {
  events: [], status: "pending", step: 0, wasLive: false,
  reset() { this.events = []; this.status = "pending"; this.step = 0; this.wasLive = false; $("thinking").open = false; this.render(); },
  add(e) { this.events.push(e); this.render(); },
  setRun(r) { this.status = r.status; this.step = r.step; this.render(); },
  phase() {
    const types = this.events.map((e) => e.type);
    if (types.includes("run_finished")) return 3;
    const last = this.events.filter((e) => ["tool_call", "approval_requested", "tool_result"].includes(e.type)).length;
    if (last) {
      const lastThought = [...this.events].reverse().find((e) => e.type === "thought");
      return lastThought && /finish|report|writing the report|escalat|back to normal/i.test(lastThought.data.text) ? 3 : 2;
    }
    return types.includes("plan") ? 1 : 0;
  },
  line() {
    const last = [...this.events].reverse().find((e) => ["thought", "tool_call", "approval_requested", "run_finished"].includes(e.type));
    if (!last) return "Reading the alert";
    const newest = this.events[this.events.length - 1];
    const waitsOnModel = ["run_created", "plan", "plan_revised", "tool_result", "approval_decided", "guidance", "status",
      "resumed_after_restart", "warning", "retry"];
    if (this.status === "running" && newest && waitsOnModel.includes(newest.type)) return "Waiting on the model…";
    if (last.type === "tool_call") return `Calling ${last.data.tool}`;
    if (last.type === "approval_requested") return `Waiting for your decision on ${last.data.tool}`;
    if (last.type === "run_finished") return `Concluded: ${last.data.report.root_cause}`;
    return last.data.text;
  },
  seconds() {
    if (this.events.length < 2) return 0;
    return Math.max(1, Math.round(this.events[this.events.length - 1].ts - this.events[0].ts));
  },
  render() {
    const live = ["running", "pending"].includes(this.status);
    const panel = $("thinking");
    panel.classList.toggle("live", live);
    if (this.wasLive && !live) panel.open = false;     // collapse when the agent stops to wait or finishes
    this.wasLive = live;
    $("th-title").textContent = live ? "Thinking…"
      : `Thought for ${this.seconds()}s over ${this.step} step${this.step === 1 ? "" : "s"}`;
    const idle = { waiting_approval: "Waiting for your decision", failed: "Stopped with an error", paused: "Paused by you",
      cancelled: "Cancelled", completed: "Finished: fix applied", escalated: "Finished: handed to a human" };
    $("th-line").textContent = live ? this.line() : (idle[this.status] || label(this.status));
    const p = this.phase();
    $("th-phases").innerHTML = PHASES.map((name, i) =>
      `<li class="${i < p ? "done" : i === p ? (live ? "now" : "done") : ""}">${name}</li>`).join("");
    $("th-body").innerHTML = this.detail();
  },
  detail() {
    const plans = this.events.filter((e) => e.type === "plan" || e.type === "plan_revised");
    const plan = plans.length ? plans[plans.length - 1].data.steps : [];
    const byStep = new Map();
    for (const e of this.events) {
      if (!["thought", "tool_call", "approval_requested", "tool_result", "approval_decided", "guidance"].includes(e.type)) continue;
      if (!byStep.has(e.step)) byStep.set(e.step, []);
      byStep.get(e.step).push(e);
    }
    let html = plan.length ? `<h3>Plan</h3><ol>${plan.map((s) => `<li>${esc(s)}</li>`).join("")}</ol>` : "";
    html += "<h3>Reasoning by step</h3>";
    if (!byStep.size) html += '<p class="meta">Nothing yet. The agent is reading the alert.</p>';
    for (const [step, evs] of byStep) {
      const thought = evs.find((e) => e.type === "thought");
      const chose = evs.filter((e) => e.type === "tool_call" || e.type === "approval_requested")
        .map((e) => `${esc(e.data.tool)}${e.type === "approval_requested" ? " (needs approval)" : ""}`);
      const results = evs.filter((e) => e.type === "tool_result").map((e) => esc(e.data.ok ? e.data.summary : e.data.error));
      const human = evs.filter((e) => e.type === "approval_decided" || e.type === "guidance")
        .map((e) => e.type === "guidance" ? `You said: ${esc(e.data.message)}` : `You ${esc(e.data.decision)} ${esc(e.data.tool)}`);
      html += `<div class="th-step"><b>Step ${step}</b>${thought ? ` <span class="why">${esc(thought.data.text)}</span>` : ""}`
        + (chose.length ? `<div class="meta">Chose: ${chose.join(", ")}</div>` : "")
        + (results.length ? `<div class="meta">Learned: ${results.join("; ")}</div>` : "")
        + (human.length ? `<div class="meta">${human.join("; ")}</div>` : "") + "</div>";
    }
    const done = this.events.find((e) => e.type === "run_finished");
    if (done) {
      const rep = done.data.report;
      html += `<h3>Conclusion</h3><p>${esc(rep.root_cause)}</p><p class="meta">Confidence ${Math.round((rep.confidence || 0) * 100)}%. ${esc(rep.routing_reason || "")}</p>`;
    }
    return html;
  },
};

// ===========================================================================
// Data loading
// ===========================================================================
async function loadRuns() {
  renderRunList(await api("/api/runs"));
  renderMemory(await api("/api/memory"));
}

async function refresh() {
  if (!current) return;
  renderRun(await api(`/api/runs/${current}`));
}

function select(id) {
  current = id;
  lastEventId = 0;
  history.replaceState(null, "", `?run=${id}`);
  $("empty").hidden = true;
  $("run").hidden = false;
  $("timeline").innerHTML = "";
  $("trace-empty").hidden = false;
  think.reset();
  openStream(id);
  clearInterval(pollTimer);
  pollTimer = setInterval(poll(refresh), 1500);
  poll(refresh)();
  poll(loadRuns)();
}

// ===========================================================================
// Event handlers and start-up
// ===========================================================================
async function init() {
  $("banner-retry").onclick = () => { clearError(); poll(loadRuns)(); poll(refresh)(); };
  let h;
  try { h = await api("/health"); } catch (err) {
    $("mode").textContent = "Server unreachable";
    showError(`${err.message} Then press Retry now or reload the page.`, "conn");
    $("banner-retry").onclick = () => location.reload();
    return;
  }
  $("mode").textContent = h.llm_mode === "gemini" ? `Live model: ${h.model}` : "Offline demo mode (no API key)";
  const scenarios = await api("/api/scenarios");
  $("scenario").innerHTML = scenarios.map((s) => `<option value="${s.id}">${esc(s.title)}</option>`).join("");
  $("scenario").disabled = false;
  $("start").disabled = false;
  const showAlert = () => { $("scenario-alert").textContent = scenarios.find((s) => s.id === $("scenario").value).alert; };
  $("scenario").onchange = showAlert;
  showAlert();
  bindHandlers();
  await poll(loadRuns)();
  const fromUrl = new URLSearchParams(location.search).get("run");
  if (fromUrl) select(fromUrl);
  setInterval(poll(loadRuns), 3000);
}

function bindHandlers() {
  $("start").onclick = () => act($("start"), "Starting…", async () => {
    const r = await post("/api/runs", { scenario: $("scenario").value });
    select(r.id);
  });
  $("pause").onclick = () => act($("pause"), "Pausing…", () => post(`/api/runs/${current}/pause`));
  $("resume").onclick = () => act($("resume"), "Resuming…", () => post(`/api/runs/${current}/resume`));
  $("cancel").onclick = () => confirm("Cancel this run?")
    && act($("cancel"), "Cancelling…", () => post(`/api/runs/${current}/cancel`));
  $("approve").onclick = () => act($("approve"), "Approving…", () => decide("approve"));
  $("reject").onclick = () => act($("reject"), "Rejecting…", () => decide("reject"));
  $("guidance").onkeydown = (e) => {               // Enter sends, Shift+Enter adds a line
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); $("send").click(); }
  };
  $("send").onclick = () => {
    const m = $("guidance").value.trim();
    if (!m) return;
    act($("send"), "Sending…", async () => {
      await post(`/api/runs/${current}/guidance`, { message: m });
      $("guidance").value = "";
    });
  };
}

async function decide(d) {
  await post(`/api/runs/${current}/approval`, { decision: d, comment: $("ap-comment").value });
  $("ap-comment").value = "";
}

init();
