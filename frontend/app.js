// Operator console: plain JS, no build step.
// Run state is polled; trace events and live text deltas arrive over Server-Sent Events.

const $ = (id) => document.getElementById(id);
let current = null, run = null, es = null, pollTimer = null, bannerKind = null, scenarios = [];

// ---------------------------------------------------------------- helpers
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const label = (status) => status.replace("_", " ");
const time = (ts) => new Date(ts * 1000).toLocaleTimeString();
const pct = (x) => `${Math.round((x || 0) * 100)}%`;
const usd = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD", minimumFractionDigits: 4 });
const humanize = (key) => key.replace(/_/g, " ").replace(/^./, (c) => c.toUpperCase());
const argText = (args) => Object.values(args || {}).join(", ");
const ACTIVE = ["pending", "running", "paused", "waiting_approval", "failed"];
// "top runbooks: ['a', 'b']" -> "top runbooks: a, b"
const plainList = (text) => String(text).replace(/\[([^\]]*)\]/g, (_, inner) => inner.replace(/['"]/g, ""));
// model text sometimes embeds the raw call ("Proposing rollback_deployment {...}"); the card shows fields instead
const plainReason = (text) => (text || "").replace(/Proposing \w+ \{[^}]*\}\s*-?\s*/g, "It proposes this change; ")
  .replace(/\s+/g, " ").trim();

const TOOL_NAMES = {
  list_services: "List services", get_metrics: "Read metrics", get_logs: "Read logs",
  get_recent_deployments: "Check deployments", search_runbooks: "Search runbooks",
  restart_service: "Restart service", rollback_deployment: "Roll back deployment", scale_service: "Scale service",
};

// ---------------------------------------------------------------- API and errors
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

// Polling errors clear themselves once the server answers again; button errors stay until the next action.
function showError(msg, kind = "action") {
  bannerKind = kind;
  $("banner-text").innerHTML = `<b>${kind === "conn" ? "Connection problem." : "That didn't work."}</b> ${esc(msg)}`;
  $("banner").hidden = false;
}
function clearError(kind) {
  if (!kind || bannerKind === kind) { $("banner").hidden = true; bannerKind = null; }
}
async function act(btn, busyText, fn) {
  const text = btn.textContent;
  btn.disabled = true; btn.setAttribute("aria-busy", "true"); btn.textContent = busyText;
  try { clearError(); await fn(); } catch (err) { showError(err.message); } finally {
    btn.removeAttribute("aria-busy"); btn.textContent = text; btn.disabled = false;
    if (current) refresh();
  }
}
const poll = (fn) => async () => {
  try { await fn(); clearError("conn"); } catch (err) { showError(`${err.message} Retrying automatically.`, "conn"); }
};

// ---------------------------------------------------------------- live view of one run (built from the stream)
// Final events replace the live text of the same kind and step; late deltas for finished text are ignored.
const live = {
  reset() { this.thoughts = []; this.acts = new Map(); this.text = {}; this.final = new Set(); this.t0 = null; this.t1 = null; },
  delta(d) {
    const key = `${d.kind}:${d.step}`;
    if (this.final.has(key)) return;
    this.text[d.kind] = this.text[d.kind]?.step === d.step ? { step: d.step, s: this.text[d.kind].s + d.text } : { step: d.step, s: d.text };
    render();
  },
  event(e) {
    this.t0 ??= e.ts; this.t1 = e.ts;
    const d = e.data, key = (k) => this.final.add(`${k}:${e.step}`);
    const actKey = (tool, args) => `${e.step}:${tool}:${JSON.stringify(args)}`;
    if (e.type === "plan" || e.type === "plan_revised") { key("plan"); delete this.text.plan; }
    if (e.type === "thought") { key("thought"); delete this.text.thought; this.thoughts.push({ step: e.step, text: d.text }); }
    if (e.type === "answer") { key("answer"); delete this.text.answer; }
    if (e.type === "guidance") this.thoughts.push({ step: e.step, text: d.message, you: true });
    if (e.type === "tool_call") this.acts.set(actKey(d.tool, d.args), { tool: d.tool, args: d.args, step: e.step, status: "running" });
    if (e.type === "approval_requested") this.acts.set(actKey(d.tool, d.args), { tool: d.tool, args: d.args, step: e.step, status: "waiting" });
    if (e.type === "tool_result") {
      this.acts.set(actKey(d.tool, d.args), { tool: d.tool, args: d.args, step: e.step,
        status: d.ok ? "done" : "failed", note: d.ok ? d.summary : d.error });
    }
    if (e.type === "approval_decided" && d.decision !== "approved") {
      this.acts.set(actKey(d.tool, d.args), { tool: d.tool, args: d.args, step: e.step, status: "rejected",
        note: `You ${d.decision} it${d.comment ? `: "${d.comment}"` : ""}` });
    }
    render();
  },
};

function openStream(id) {
  if (es) es.close();
  setConn("", "Connecting…");
  es = new EventSource(`/api/runs/${id}/stream`);
  es.onopen = () => setConn("live", "Live");
  es.onerror = () => setConn("lost", es.readyState === EventSource.CLOSED ? "Disconnected. Reload to reconnect."
    : "Disconnected. Reconnecting…");
  es.addEventListener("token", (m) => live.delta(JSON.parse(m.data)));
  es.addEventListener("trace", (m) => { live.event(JSON.parse(m.data)); refresh(); });
  es.addEventListener("end", () => { es.close(); setConn("", ""); });
}
function setConn(kind, text) { $("conn").className = `conn ${kind}`; $("conn").textContent = text; }

// ---------------------------------------------------------------- renderers
const caret = '<span class="caret" aria-hidden="true"></span>';
const statusPill = (status) => `<span class="status s-${status}">${label(status)}</span>`;

function renderRunList(runs) {
  // each row is a real <button>, so it is reachable with Tab and opens with Enter or Space
  $("runs").innerHTML = runs.map((r) => `<li><button class="run-item" data-id="${r.id}" aria-current="${r.id === current}">
      <span>${esc(r.title)}</span><span class="sub">${time(r.created_at)} ${statusPill(r.status)}</span></button></li>`).join("")
    || '<li class="state">No runs yet.</li>';
  document.querySelectorAll("#runs [data-id]").forEach((b) => (b.onclick = () => select(b.dataset.id)));
}

function render() {
  if (!run) return;
  const isLive = ["running", "pending"].includes(run.status);
  // plan: final list from the run, or the list being streamed right now
  const streaming = live.text.plan;
  const steps = streaming ? streaming.s.split("\n") : run.plan || [];
  $("plan").innerHTML = steps.map((s, i) => `<li>${esc(s)}${streaming && i === steps.length - 1 ? caret : ""}</li>`).join("")
    || '<li class="state">The agent writes its plan first.</li>';
  // thinking: every step's reasoning, the current one streaming
  const secs = live.t0 ? Math.max(1, Math.round(live.t1 - live.t0)) : 0;
  $("th-title").textContent = isLive ? "Thinking…" : `Thought for ${secs}s over ${run.step} step${run.step === 1 ? "" : "s"}`;
  $("th-title").classList.toggle("live", isLive);
  const now = live.text.thought;
  $("thoughts").innerHTML = live.thoughts.map((t) => (t.you ? `<p class="bubble">${esc(t.text)}</p>`
    : `<p><span class="step">Step ${t.step}</span>${esc(t.text)}</p>`)).join("")
    + (now ? `<p><span class="step">Step ${now.step}</span>${esc(now.s)}${caret}</p>` : "")
    || '<p class="state">Waiting on the model…</p>';
  // acting: one row per tool call
  $("acts").innerHTML = [...live.acts.values()].map((a) => `<li class="act ${a.status}">
      <span class="act-name">${esc(TOOL_NAMES[a.tool] || humanize(a.tool))}</span>
      <span class="act-args">${esc(argText(a.args))}</span>
      <span class="act-status">${{ running: "Running…", waiting: "Waiting for you", done: "Done", failed: "Failed", rejected: "Not run" }[a.status]}</span>
      ${a.note ? `<span class="act-note">${esc(plainList(a.note))}</span>` : ""}</li>`).join("")
    || '<li class="state">No tool calls yet.</li>';
  // result: streamed diagnosis first, then the full answer
  const ans = live.text.answer;
  $("result").hidden = !run.answer && !ans;
  if (run.answer) $("answer").innerHTML = answerHtml(run.answer, run.report);
  else if (ans) $("answer").innerHTML = `<p class="lead">${esc(ans.s)}${caret}</p>`;
}

const answerHtml = (a, rep) => `<p class="lead">${esc(a.diagnosis)}</p>
  <dl class="facts">
    <div><dt>Action taken</dt><dd>${esc(a.action_taken)}</dd></div>
    <div><dt>Outcome</dt><dd>${esc(a.outcome)}</dd></div>
    <div><dt>Confidence</dt><dd>${pct(a.confidence)}, severity ${esc(rep.severity)}</dd></div>
  </dl>
  ${(rep.follow_ups || []).length ? `<h3>Follow-ups</h3><ul>${rep.follow_ups.map((f) => `<li>${esc(f)}</li>`).join("")}</ul>` : ""}`;

const stepTag = (n) => (n ? `<span class="step">Step ${n}</span>` : "");
function renderSources(r) {
  $("sources-block").hidden = !r.answer;
  if (!r.answer) return;
  $("sources").innerHTML = r.sources.map((s) => `<li>${stepTag(s.step)}<b>${esc(s.source)}</b> ${esc(plainList(s.text))}</li>`).join("");
  const items = (list) => list.map((x) => `<li>${stepTag(x.step)}${esc(x.text)}</li>`).join("") || '<li class="state">Nothing</li>';
  $("done").innerHTML = items(r.done);
  $("not-done").innerHTML = items(r.not_done);
}

function renderRun(r) {
  if (run && run.status !== r.status) poll(loadRuns)();   // keep the sidebar status in step
  run = r;
  $("run-title").textContent = r.title;
  $("run-status").className = `status s-${r.status}`;
  $("run-status").textContent = label(r.status);
  $("query").textContent = r.alert;
  $("st-step").textContent = r.step;
  $("st-tokens").textContent = (r.tokens_in + r.tokens_out).toLocaleString();
  $("st-cost").textContent = usd.format(r.cost_usd);
  $("st-llm").textContent = r.llm_mode === "gemini" ? "Gemini" : "Offline mock";
  $("pause").disabled = r.status !== "running";
  $("resume").disabled = !["paused", "failed"].includes(r.status);
  $("cancel").disabled = !ACTIVE.includes(r.status);
  $("run-error").hidden = !r.error;
  $("run-error").innerHTML = r.error ? errorHtml(r.error) : "";
  const ap = r.status === "waiting_approval" ? r.pending_approval : null;
  const appeared = ap && $("approval").hidden;
  $("approval").hidden = !ap;
  if (ap) renderApproval(ap, r);
  if (appeared) $("approval").focus();              // move focus to the decision, never onto Approve itself
  const decided = (r.approvals || []).filter((a) => a.status !== "pending").slice(-1)[0];
  $("ap-receipt").hidden = !!ap || !decided;
  if (decided && !ap) $("ap-receipt").innerHTML = `<b>You ${esc(decided.status)}:</b> ${esc(actionTitle(decided.tool, decided.args, {}))}`;
  renderSources(r);
  setComposer(ACTIVE.includes(r.status));
  render();
}

// Provider errors can carry a long JSON dump: show the first sentence only.
function errorHtml(error) {
  const cut = error.search(/\s[{[]/);
  const short = cut > 0 ? error.slice(0, cut).replace(/[.\s]+$/, "") : error;
  return `<b>The run stopped.</b> ${esc(short)}. Press <b>Resume</b> to continue from the last checkpoint.`;
}

// ---------------------------------------------------------------- approval card (plain words, no raw JSON)
function actionTitle(tool, a, r) {
  const infra = r.infra?.[a.service] || {};
  if (tool === "restart_service") return `Restart service ${a.service}`;
  if (tool === "rollback_deployment") return `Roll back ${a.service}${infra.version ? ` from ${infra.version}` : ""} to ${a.to_version}`;
  if (tool === "scale_service") return `Scale ${a.service}${infra.replicas ? ` from ${infra.replicas}` : ""} to ${a.replicas} replicas`;
  return `Run ${humanize(tool).toLowerCase()}`;
}
const EFFECTS = {
  restart_service: "All pods of the service are recycled. Requests in flight may fail briefly.",
  rollback_deployment: "The service is redeployed on the earlier version. Changes in the newer release are undone.",
  scale_service: "Pods are added or removed. Cost changes with the number of replicas.",
};
const FIELDS = { service: "Service", to_version: "Target version", replicas: "Replicas" };
const FINDING = { get_metrics: "Metrics", get_logs: "Logs", get_recent_deployments: "Latest deploy", search_runbooks: "Runbook" };

function renderApproval(ap, r) {
  $("ap-title").textContent = actionTitle(ap.tool, ap.args, r);
  const rows = Object.entries(ap.args).map(([k, v]) => `<div><dt>${esc(FIELDS[k] || humanize(k))}</dt><dd>${esc(v)}</dd></div>`);
  if (EFFECTS[ap.tool]) rows.push(`<div><dt>Effect</dt><dd>${esc(EFFECTS[ap.tool])}</dd></div>`);
  $("ap-fields").innerHTML = rows.join("");
  $("ap-reason").textContent = plainReason(ap.reason) || "The agent gave no reason.";
  const texts = [ap.reason, ...(r.steps || []).map((s) => s.thought)].filter(Boolean).reverse();
  const m = texts.map((t) => t.match(/confidence[^0-9]{0,12}(\d+(?:\.\d+)?)\s*(%)?/i)).find(Boolean);
  const confPct = m ? Math.round(m[2] || parseFloat(m[1]) > 1 ? parseFloat(m[1]) : parseFloat(m[1]) * 100) : null;
  $("ap-confidence").hidden = confPct === null || confPct > 100;
  if (confPct !== null) { $("ap-conf-value").textContent = `${confPct}%`; $("ap-conf-bar").style.width = `${confPct}%`; }
  const latest = new Map();
  for (const s of r.steps || []) for (const c of s.calls || []) if (c.ok && FINDING[c.tool]) latest.set(c.tool, c.summary);
  $("ap-findings").innerHTML = [...latest].map(([tool, summary]) =>
    `<li><b>${FINDING[tool]}:</b> ${esc(plainList(summary))}</li>`).join("");
}

// ---------------------------------------------------------------- composer: starts a run, or messages the active one
function setComposer(active) {
  $("scenario").hidden = active;
  $("send").textContent = active ? "Send to agent" : "Start triage";
  $("composer").placeholder = active ? "Message the agent. It reads it at its next step…"
    : (scenarios.find((s) => s.id === $("scenario").value)?.alert || "") + "…";
  $("composer-hint").textContent = active ? "Enter sends" : "Optional: describe the incident in your words";
}
async function send() {
  const text = $("composer").value.trim();
  if (run && ACTIVE.includes(run.status)) {
    if (!text) return;
    await post(`/api/runs/${current}/guidance`, { message: text });
  } else {
    const r = await post("/api/runs", { scenario: $("scenario").value, alert: text || undefined });
    select(r.id);
  }
  $("composer").value = "";
}

// ---------------------------------------------------------------- data loading and start-up
const loadRuns = async () => renderRunList(await api("/api/runs"));
async function refresh() { if (current) renderRun(await api(`/api/runs/${current}`)); }

function select(id) {
  current = id; run = null;
  history.replaceState(null, "", `?run=${id}`);
  $("empty").hidden = true; $("run").hidden = false;
  live.reset();
  openStream(id);
  clearInterval(pollTimer);
  pollTimer = setInterval(poll(refresh), 1500);
  poll(refresh)(); poll(loadRuns)();
}
function newRun() {
  current = null; run = null;
  if (es) es.close();
  clearInterval(pollTimer);
  history.replaceState(null, "", "/");
  $("empty").hidden = false; $("run").hidden = true;
  setComposer(false); poll(loadRuns)(); $("composer").focus();
}

async function init() {
  $("banner-retry").onclick = () => { clearError(); poll(loadRuns)(); poll(refresh)(); };
  let h;
  try { h = await api("/health"); } catch (err) {
    $("mode").textContent = "Server unreachable";
    showError(`${err.message} Then press Retry now or reload the page.`, "conn");
    $("banner-retry").onclick = () => location.reload();
    return;
  }
  $("mode").textContent = h.llm_mode === "gemini" ? `Live model: ${h.model}` : "Offline demo mode";
  scenarios = await api("/api/scenarios");
  $("scenario").innerHTML = scenarios.map((s) => `<option value="${s.id}">${esc(s.title)}</option>`).join("");
  $("scenario").disabled = false; $("send").disabled = false;
  $("scenario").onchange = () => setComposer(false);
  $("new-run").onclick = newRun;
  $("send").onclick = () => act($("send"), "Sending…", send);
  $("composer").onkeydown = (e) => {               // Enter sends, Shift+Enter adds a line
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); $("send").click(); }
  };
  $("pause").onclick = () => act($("pause"), "Pausing…", () => post(`/api/runs/${current}/pause`));
  $("resume").onclick = () => act($("resume"), "Resuming…", () => post(`/api/runs/${current}/resume`));
  $("cancel").onclick = () => confirm("Cancel this run?") && act($("cancel"), "Cancelling…", () => post(`/api/runs/${current}/cancel`));
  $("approve").onclick = () => act($("approve"), "Approving…", () => decide("approve"));
  $("reject").onclick = () => act($("reject"), "Rejecting…", () => decide("reject"));
  setComposer(false);
  await poll(loadRuns)();
  const fromUrl = new URLSearchParams(location.search).get("run");
  if (fromUrl) select(fromUrl);
  setInterval(poll(loadRuns), 3000);
}
async function decide(d) {
  await post(`/api/runs/${current}/approval`, { decision: d, comment: $("ap-comment").value });
  $("ap-comment").value = "";
}

init();
