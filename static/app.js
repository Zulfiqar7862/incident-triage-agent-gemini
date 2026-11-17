// Operator console: plain JS, no build step.
// Run state is polled; the trace arrives over Server-Sent Events.
const $ = (id) => document.getElementById(id);
let current = null, es = null, lastEventId = 0, pollTimer = null;

async function api(path, opts = {}) {
  const r = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts });
  const body = await r.json().catch(() => ({}));
  if (!r.ok) { alert(body.detail || r.statusText); throw new Error(body.detail); }
  return body;
}
const post = (path, data) => api(path, { method: "POST", body: JSON.stringify(data || {}) });
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const label = (status) => status.replace("_", " ");
const statusPill = (status) => `<span class="status s-${status}">${label(status)}</span>`;
const time = (ts) => new Date(ts * 1000).toLocaleTimeString();
const call = (tool, args) => `<code>${esc(tool)}(${esc(JSON.stringify(args))})</code>`;

async function init() {
  const h = await api("/health");
  $("mode").textContent = h.llm_mode === "gemini" ? `Live model: ${h.model}` : "Offline demo mode (no API key)";
  const scenarios = await api("/api/scenarios");
  $("scenario").innerHTML = scenarios.map((s) => `<option value="${s.id}">${esc(s.title)}</option>`).join("");
  const showAlert = () => { $("scenario-alert").textContent = scenarios.find((s) => s.id === $("scenario").value).alert; };
  $("scenario").onchange = showAlert;
  showAlert();
  $("start").onclick = async () => { const r = await post("/api/runs", { scenario: $("scenario").value }); select(r.id); };
  $("pause").onclick = () => post(`/api/runs/${current}/pause`).then(refresh);
  $("resume").onclick = () => post(`/api/runs/${current}/resume`).then(refresh);
  $("cancel").onclick = () => confirm("Cancel this run?") && post(`/api/runs/${current}/cancel`).then(refresh);
  $("approve").onclick = () => decide("approve");
  $("reject").onclick = () => decide("reject");
  $("send").onclick = async () => {
    const m = $("guidance").value.trim();
    if (!m) return;
    await post(`/api/runs/${current}/guidance`, { message: m });
    $("guidance").value = "";
  };
  await loadRuns();
  const fromUrl = new URLSearchParams(location.search).get("run");
  if (fromUrl) select(fromUrl);
  setInterval(loadRuns, 3000);
}

async function decide(d) {
  await post(`/api/runs/${current}/approval`, { decision: d, comment: $("ap-comment").value });
  $("ap-comment").value = "";
  refresh();
}

async function loadRuns() {
  const runs = await api("/api/runs");
  $("runs").innerHTML = runs.map((r) => `<li data-id="${r.id}" class="${r.id === current ? "active" : ""}">
      <span>${esc(r.title)}<br><span class="sub">Started ${time(r.created_at)}</span></span>
      ${statusPill(r.status)}</li>`).join("") || '<li class="sub">No runs yet. Start one above.</li>';
  document.querySelectorAll("#runs li[data-id]").forEach((li) => (li.onclick = () => select(li.dataset.id)));
  const mem = await api("/api/memory");
  $("memory").innerHTML = mem.map((m) => `<li><b>${esc(m.service)}</b>: ${esc(m.root_cause)}</li>`).join("")
    || "<li>Nothing yet. Each resolved incident is remembered here and used in later runs.</li>";
}

function select(id) {
  current = id;
  lastEventId = 0;
  history.replaceState(null, "", `?run=${id}`);
  $("empty").hidden = true;
  $("run").hidden = false;
  $("timeline").innerHTML = "";
  think.reset();
  if (es) es.close();
  es = new EventSource(`/api/runs/${id}/stream`);
  es.addEventListener("trace", (m) => {
    const e = JSON.parse(m.data);
    if (e.id > lastEventId) { lastEventId = e.id; addEvent(e); think.add(e); refresh(); }
  });
  es.addEventListener("end", () => es.close());
  clearInterval(pollTimer);
  pollTimer = setInterval(refresh, 1500);
  refresh();
  loadRuns();
}

async function refresh() {
  if (!current) return;
  const r = await api(`/api/runs/${current}`);
  $("run-title").textContent = r.title;
  $("run-alert").textContent = r.alert;
  $("run-status").className = `status s-${r.status}`;
  $("run-status").textContent = label(r.status);
  $("st-step").textContent = r.step;
  think.setRun(r);
  $("st-tokens").textContent = (r.tokens_in + r.tokens_out).toLocaleString();
  $("st-cost").textContent = "$" + r.cost_usd.toFixed(4);
  $("st-llm").textContent = r.llm_mode === "gemini" ? "Gemini" : "Offline mock";
  $("pause").disabled = r.status !== "running";
  $("resume").disabled = !["paused", "failed"].includes(r.status);
  $("cancel").disabled = ["completed", "escalated", "cancelled"].includes(r.status);
  $("run-error").hidden = !r.error;
  $("run-error").textContent = r.error ? `The run stopped: ${r.error}. Press Resume run to continue from the last checkpoint.` : "";
  $("plan").innerHTML = (r.plan || []).map((p) => `<li>${esc(p)}</li>`).join("") || '<li class="sub">The agent writes its plan in the first step.</li>';
  $("infra").innerHTML = Object.entries(r.infra).map(([n, s]) =>
    `<tr><td><b>${esc(n)}</b></td><td>${statusPill(s.status)}</td><td>${esc(s.version)}</td><td>${s.replicas} pods</td></tr>`).join("");
  const ap = r.status === "waiting_approval" ? r.pending_approval : null;
  $("approval").hidden = !ap;
  if (ap) {
    $("ap-call").textContent = `${ap.tool}(${JSON.stringify(ap.args, null, 1)})`;
    $("ap-reason").textContent = ap.reason || "";
  }
  const rep = r.report;
  $("report").hidden = !rep;
  if (rep) {
    $("report").className = "panel report " + r.status;
    $("report").innerHTML = `<h2>${r.status === "escalated" ? "Handed to a human" : "Incident report"}</h2>
      <p><b>Root cause:</b> ${esc(rep.root_cause)}</p>
      <p><b>Severity:</b> ${esc(rep.severity)}. <b>Confidence:</b> ${Math.round((rep.confidence || 0) * 100)}%</p>
      <p><b>Why this outcome:</b> ${esc(rep.routing_reason || "")}</p>
      <p><b>Actions taken:</b> ${esc((rep.actions_taken || []).join("; ") || "none")}</p>
      <p><b>Follow-ups:</b></p><ul>${(rep.follow_ups || []).map((f) => `<li>${esc(f)}</li>`).join("")}</ul>`;
  }
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
}

// ---------------------------------------------------------------------------
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
      : `Thought for ${this.seconds()}s · ${this.step} step${this.step === 1 ? "" : "s"}`;
    $("th-line").textContent = live ? this.line() : (this.status === "waiting_approval" ? "Waiting for your decision" : label(this.status));
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

init();
