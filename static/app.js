// Operator UI: plain JS, no build step. Polls run state + listens to the SSE trace stream.
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
const badge = (el, status) => { el.textContent = status.replace("_", " "); el.className = "badge b-" + status; };

async function init() {
  const h = await api("/health");
  $("mode").textContent = h.llm_mode === "gemini" ? `Gemini: ${h.model}` : "Offline mock LLM (no API key)";
  const scenarios = await api("/api/scenarios");
  $("scenario").innerHTML = scenarios.map((s) => `<option value="${s.id}">${esc(s.title)}</option>`).join("");
  const showAlert = () => { $("scenario-alert").textContent = scenarios.find((s) => s.id === $("scenario").value).alert; };
  $("scenario").onchange = showAlert; showAlert();
  $("start").onclick = async () => { const r = await post("/api/runs", { scenario: $("scenario").value }); select(r.id); };
  $("pause").onclick = () => post(`/api/runs/${current}/pause`).then(refresh);
  $("resume").onclick = () => post(`/api/runs/${current}/resume`).then(refresh);
  $("cancel").onclick = () => confirm("Cancel this run?") && post(`/api/runs/${current}/cancel`).then(refresh);
  $("approve").onclick = () => decide("approve");
  $("reject").onclick = () => decide("reject");
  $("send").onclick = async () => {
    const m = $("guidance").value.trim(); if (!m) return;
    await post(`/api/runs/${current}/guidance`, { message: m }); $("guidance").value = "";
  };
  await loadRuns();
  const fromUrl = new URLSearchParams(location.search).get("run");
  if (fromUrl) select(fromUrl);
  setInterval(loadRuns, 3000);
}

async function decide(d) {
  await post(`/api/runs/${current}/approval`, { decision: d, comment: $("ap-comment").value });
  $("ap-comment").value = ""; refresh();
}

async function loadRuns() {
  const runs = await api("/api/runs");
  $("runs").innerHTML = runs.map((r) => `<li data-id="${r.id}" class="${r.id === current ? "active" : ""}">
      <span>${esc(r.title)}<br><span class="muted small">${r.id} · ${new Date(r.created_at * 1000).toLocaleTimeString()}</span></span>
      <span class="badge b-${r.status}">${r.status.replace("_", " ")}</span></li>`).join("") || '<li class="muted">No runs yet</li>';
  document.querySelectorAll("#runs li[data-id]").forEach((li) => (li.onclick = () => select(li.dataset.id)));
  const mem = await api("/api/memory");
  $("memory").innerHTML = mem.map((m) => `<li><b>${esc(m.service)}</b>: ${esc(m.root_cause)}</li>`).join("") || '<li class="muted">Empty. Completed runs are remembered here.</li>';
}

function select(id) {
  current = id; lastEventId = 0;
  history.replaceState(null, "", `?run=${id}`);
  $("empty").hidden = true; $("run").hidden = false; $("timeline").innerHTML = "";
  if (es) es.close();
  es = new EventSource(`/api/runs/${id}/stream`);
  es.addEventListener("trace", (m) => { const e = JSON.parse(m.data); if (e.id > lastEventId) { lastEventId = e.id; addEvent(e); refresh(); } });
  es.addEventListener("end", () => es.close());
  clearInterval(pollTimer); pollTimer = setInterval(refresh, 1500);
  refresh(); loadRuns();
}

async function refresh() {
  if (!current) return;
  const r = await api(`/api/runs/${current}`);
  $("run-title").textContent = r.title; $("run-alert").textContent = r.alert;
  badge($("run-status"), r.status);
  $("st-step").textContent = r.step;
  $("st-tokens").textContent = (r.tokens_in + r.tokens_out).toLocaleString();
  $("st-cost").textContent = "$" + r.cost_usd.toFixed(4);
  $("st-llm").textContent = r.llm_mode;
  $("pause").disabled = r.status !== "running";
  $("resume").disabled = !["paused", "failed"].includes(r.status);
  $("cancel").disabled = ["completed", "escalated", "cancelled"].includes(r.status);
  $("run-error").hidden = !r.error; $("run-error").textContent = r.error ? "Error: " + r.error : "";
  $("plan").innerHTML = (r.plan || []).map((p) => `<li>${esc(p)}</li>`).join("") || '<li class="muted">No plan yet</li>';
  $("infra").innerHTML = Object.entries(r.infra).map(([n, s]) => `<tr><td><b>${n}</b></td><td><span class="badge b-${s.status === "healthy" ? "completed" : "failed"}">${s.status}</span></td><td>${s.version}</td><td>${s.replicas} pods</td></tr>`).join("");
  const ap = r.status === "waiting_approval" ? r.pending_approval : null;
  $("approval").hidden = !ap;
  if (ap) { $("ap-call").textContent = `${ap.tool}(${JSON.stringify(ap.args, null, 1)})`; $("ap-reason").textContent = ap.reason || ""; }
  const rep = r.report;
  $("report").hidden = !rep;
  if (rep) {
    $("report").className = "card report " + r.status;
    $("report").innerHTML = `<h2>${r.status === "escalated" ? "🙋 Handed to a human" : "✅ Incident report"}</h2>
      <p><b>Root cause:</b> ${esc(rep.root_cause)}</p>
      <p class="small muted">Routing: ${esc(rep.routing_reason || "")}</p><p><b>Severity:</b> ${esc(rep.severity)} · <b>Confidence:</b> ${Math.round((rep.confidence || 0) * 100)}%</p>
      <p><b>Actions taken:</b> ${esc((rep.actions_taken || []).join("; ") || "none")}</p>
      <p><b>Follow-ups:</b></p><ul>${(rep.follow_ups || []).map((f) => `<li>${esc(f)}</li>`).join("")}</ul>`;
  }
}

function addEvent(e) {
  const d = e.data, t = e.type;
  let html = "";
  if (t === "thought") html = `💭 <b>Thinking:</b> ${esc(d.text)}`;
  else if (t === "plan" || t === "plan_revised") html = `🗺️ <b>${t === "plan" ? "Plan" : "Plan revised"}</b><ol>${d.steps.map((s) => `<li>${esc(s)}</li>`).join("")}</ol>`;
  else if (t === "tool_call") html = `🔧 <b>Call</b> <code>${d.tool}(${esc(JSON.stringify(d.args))})</code>`;
  else if (t === "tool_result") html = `${d.ok ? "📥" : "❌"} <b>${d.tool}</b> → ${esc(d.ok ? d.summary : d.error)} <span class="muted small">(${d.latency_ms} ms, ${d.attempts} attempt${d.attempts === 1 ? "" : "s"})</span>` + (d.output ? `<details><summary>raw output</summary><pre>${esc(d.output)}</pre></details>` : "");
  else if (t === "llm_call") html = `🤖 LLM call <span class="muted small">${d.model} · in ${d.tokens_in} / out ${d.tokens_out} tokens · ${d.latency_ms} ms · $${d.cost_usd} · context ≈ ${d.context_layers.total_tokens_est} tok (${d.context_layers.steps_in_full} steps full, ${d.context_layers.steps_compacted} compacted) · memories ${d.memories_used}</span>`;
  else if (t === "approval_requested") html = `✋ <b>Approval requested</b> for <code>${d.tool}(${esc(JSON.stringify(d.args))})</code>`;
  else if (t === "approval_decided") html = `🧑‍💼 Operator <b>${d.decision}</b> ${d.tool}${d.comment ? `: “${esc(d.comment)}”` : ""}`;
  else if (t === "guidance") html = `🧑‍💼 <b>Operator guidance:</b> ${esc(d.message)}`;
  else if (t === "retry") html = `🔁 <b>${d.tool}</b> needed ${d.attempts} attempts (retry with backoff) → ${d.ok ? "recovered" : "gave up"}`;
  else if (t === "warning") html = `⚠️ ${esc(d.message)}`;
  else if (t === "error") html = `🛑 <b>Error:</b> ${esc(d.message)}`;
  else if (t === "status") html = `↔️ Status ${d.from} → <b>${d.to}</b> (${esc(d.why)})`;
  else if (t === "run_finished") html = `🏁 <b>Finished: ${d.status}</b> — ${esc(d.report.root_cause)}`;
  else if (t === "memory_saved") html = `🧠 Saved to long-term memory: ${esc(d.services.join(", "))}`;
  else if (t === "run_created") html = `🚨 <b>Incident:</b> ${esc(d.alert)} <span class="muted small">(LLM: ${d.llm})</span>`;
  else if (t === "resumed_after_restart") html = `♻️ <b>Resumed after server restart</b> from the last checkpoint`;
  else html = `${t}: ${esc(JSON.stringify(d))}`;
  const div = document.createElement("div");
  div.className = `ev ${t} ${d.ok === false ? "fail" : ""}`;
  div.innerHTML = `<div class="meta">#${e.id} · step ${e.step} · ${new Date(e.ts * 1000).toLocaleTimeString()}</div>${html}`;
  $("timeline").appendChild(div);
}

init();
