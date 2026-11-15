// Claimline client: live WebRTC call to OpenAI Realtime, or scripted replay.
// Either way, every tool call runs through the real backend engine.

const $ = (id) => document.getElementById(id);
const els = {
  scenario: $("scenarioSelect"), recorded: $("recorded"), newCall: $("newCallBtn"),
  accessCode: $("accessCode"), startError: $("startError"), personas: $("personas"), role: $("roleCard"), maxMin: $("maxMin"),
  start: $("startBtn"), end: $("endBtn"), callState: $("callState"),
  transcript: $("transcript"), textForm: $("textForm"), textInput: $("textInput"), textBtn: $("textBtn"),
  claimNo: $("claimNo"), sevMeter: $("sevMeter"), sevDrivers: $("sevDrivers"), codeField: $("codeField"),
  routing: $("routing"), status: $("status"), actions: $("actions"), flags: $("flags"), missing: $("missing"),
  docs: $("docs"), json: $("claimJson"), log: $("toolLog"), guards: $("guards"),
};

let pc = null, dc = null, micStream = null, sessionId = null;
let prevJsonLines = new Set();
let seenActions = new Set();
let replaying = false;
let callTimer = null;

// ------------------------------------------------------------------ fillers + latency

const LOOKUP_TOOLS = new Set(["lookup_policy", "lookup_claim", "submit_claim", "create_service_request"]);
let fillerClips = { ack: [], lookup: [] };
let lastClipFile = null;
let latencies = [];
let fillerCount = 0;
let filler = null;

async function loadFillers() {
  try {
    const m = await (await fetch("/static/fillers/manifest.json", { cache: "no-store" })).json();
    for (const kind of Object.keys(fillerClips)) {
      fillerClips[kind] = (m.clips[kind] || []).map((c) => {
        const a = new Audio(`/static/fillers/${c.file}`);
        a.preload = "auto";
        return { file: c.file, text: c.text, audio: a };
      });
    }
  } catch { /* no clips recorded yet: fillers stay off */ }
}

function playFiller(kind) {
  const pool = (fillerClips[kind] && fillerClips[kind].length ? fillerClips[kind] : fillerClips.ack)
    .filter((c) => c.file !== lastClipFile);
  if (!pool.length) return null;
  const clip = pool[Math.floor(Math.random() * pool.length)];
  lastClipFile = clip.file;
  const a = clip.audio;
  a.pause(); a.currentTime = 0; a.volume = 0.85;
  a.play().catch(() => {});
  let fadeTimer = null;
  return {
    text: clip.text,
    stop() { clearInterval(fadeTimer); a.pause(); },
    fade() {
      clearInterval(fadeTimer);
      fadeTimer = setInterval(() => {
        a.volume = Math.max(0, a.volume - 0.2);
        if (a.volume <= 0.01) { clearInterval(fadeTimer); a.pause(); }
      }, 25);
    },
  };
}

function renderLatency() {
  const el = $("latency");
  if (!latencies.length) { el.textContent = ""; return; }
  const sorted = [...latencies].sort((a, b) => a - b);
  const p50 = sorted[Math.floor((sorted.length - 1) / 2)];
  el.textContent = `Typical reply ${Math.round(p50)} ms`;
  el.title = `Last reply ${Math.round(latencies[latencies.length - 1])} ms. Pauses covered with a filler: ${fillerCount}.`;
}

function newFillerController(thresholdMs) {
  let lastText = "";
  return FillerController({
    thresholdMs,
    playClip: (kind) => { const c = playFiller(kind); if (c) lastText = c.text; return c; },
    onFiller: ({ kind, waitedMs }) => {
      fillerCount++;
      const row = document.createElement("div");
      row.innerHTML = `<span class="filler">~ </span>filler "${esc(lastText)}" (${kind}, agent silent ${Math.round(waitedMs)} ms)`;
      els.log.prepend(row);
      renderLatency();
    },
    onLatency: (ms) => { latencies.push(ms); renderLatency(); },
  });
}
const handledCalls = new Set();

// ------------------------------------------------------------------ helpers

const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const pretty = (s) => String(s || "").replace(/_/g, " ");
const cap1 = (s) => { const t = pretty(s); return t ? t[0].toUpperCase() + t.slice(1) : ""; };
const params = new URLSearchParams(location.search);
const SPEED = Math.max(0.25, Number(params.get("speed")) || 1); // ?speed=2 plays replays twice as fast
const sleep = (ms) => new Promise((r) => setTimeout(r, ms / SPEED));
const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

let clockTimer = null;
function setCallState(text, cls) {
  clearInterval(clockTimer);
  els.callState.className = `callstate ${cls || ""}`;
  if (cls === "live") {
    const t0 = Date.now();
    const tick = () => {
      const s = Math.floor((Date.now() - t0) / 1000);
      els.callState.textContent = `On call ${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
    };
    tick();
    clockTimer = setInterval(tick, 1000);
  } else {
    els.callState.textContent = text;
  }
}

function flash(el) {
  if (reduceMotion || !el) return;
  el.classList.remove("fresh");
  void el.offsetWidth;
  el.classList.add("fresh");
}

const prevVals = {};
function setField(id, value) {
  const el = $(id);
  const v = value || "";
  if (prevVals[id] === v) return;
  const hadValue = prevVals[id] !== undefined;
  el.textContent = v;
  prevVals[id] = v;
  if (v && hadValue) flash(el);
}

function clearAll() {
  els.transcript.innerHTML = `<p class="waiting">Connecting. The agent speaks first.</p>`;
  els.log.innerHTML = "";
  prevJsonLines = new Set();
  seenActions = new Set();
  seenFlags = new Set();
  for (const k of Object.keys(prevVals)) delete prevVals[k];
  handledCalls.clear();
  responseActive = false;
  pendingCorrection = null;
}

function addLine(who, text) {
  if (!text || !text.trim()) return;
  const waiting = els.transcript.querySelector(".waiting");
  if (waiting) waiting.remove();
  const div = document.createElement("div");
  div.className = `turn ${who}`;
  const name = who === "agent" ? "Claimline" : who === "note" ? "Note" : "You";
  // Card-length digit runs are masked on screen too, not just in the claim file.
  const shown = text.trim().replace(/(?:\d[ -]?){12,18}\d/g, (m) => `•••• ${m.replace(/\D/g, "").slice(-4)}`);
  div.innerHTML = `<span class="who">${name}</span><p>${esc(shown)}</p>`;
  els.transcript.appendChild(div);
  els.transcript.scrollTop = els.transcript.scrollHeight;
}

function logTool(name, args, result) {
  const row = document.createElement("div");
  const bad = result && result.error;
  const rej = result && result.rejected && Object.keys(result.rejected).length;
  row.innerHTML = `<span class="${bad || rej ? "err" : "ok"}">${bad || rej ? "x " : "ok"}</span> ${esc(name)}(${esc(JSON.stringify(args))})` +
    (bad ? ` -> ${esc(result.error)}` : rej ? ` -> rejected ${esc(Object.keys(result.rejected).join(", "))}` : "");
  els.log.prepend(row);
}

function dueText(iso) {
  const ms = new Date(iso).getTime() - Date.now();
  if (ms <= 0) return { t: "deadline passed", passed: true };
  const h = Math.floor(ms / 3.6e6), m = Math.floor((ms % 3.6e6) / 6e4), s = Math.floor((ms % 6e4) / 1000);
  return { t: h ? `${h} h ${String(m).padStart(2, "0")} min left` : `${m} min ${String(s).padStart(2, "0")} s left`, passed: false };
}

function tickCountdowns() {
  document.querySelectorAll(".due[data-deadline]").forEach((el) => {
    const c = dueText(el.dataset.deadline);
    el.textContent = c.t;
    el.classList.toggle("passed", c.passed);
  });
}
setInterval(tickCountdowns, 1000);

function fmtWhen(dateStr, timeStr) {
  if (!dateStr) return "";
  const d = new Date(`${dateStr}T${timeStr || "12:00"}`);
  if (isNaN(d)) return dateStr;
  const day = d.toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric" });
  return timeStr ? `${day}, ${d.toLocaleTimeString("en-US", { hour: "numeric", minute: "2-digit" })}` : day;
}

// ------------------------------------------------------------------ render the form

let seenFlags = new Set();
const QUEUES = ["rapid_response", "breach_response", "litigation", "liability_desk", "desk_adjuster", "fast_track"];

function render(c) {
  if (!c) return;
  const f = c.fields || {};
  const ec = c.existing_claim;
  const sr = c.service_request;
  const nonClaim = !c.line && c.call_reason && c.call_reason !== "new_claim";

  // header
  els.claimNo.textContent = c.claim_number || (ec ? ec.claim_number : sr ? sr.number : "Claim number pending");
  const stamp = c.status === "submitted" ? ["Filed", ""] : c.status === "submitted_incomplete" ? ["Filed, incomplete", "warn"]
    : ec ? ["Existing claim", ""] : sr ? ["Request logged", ""] : null;
  els.status.classList.toggle("hidden", !stamp);
  if (stamp) { els.status.textContent = stamp[0]; els.status.className = `stamp ${stamp[1]}`; }

  // caller
  setField("caller", c.caller ? `${c.caller.name}, ${pretty(c.caller.role)}` : "");
  setField("reason", cap1(c.call_reason));
  const w = c.caller_wellbeing || {};
  setField("wellbeing", w.ok === true ? `Yes${w.note ? `. ${w.note}` : ""}` : w.ok === false ? `No. ${w.note || ""}`.trim()
    : c.call_reason === "new_claim" ? "Not asked yet" : "");

  // policy and loss
  setField("insured", c.policy ? `${c.policy.insured} (${c.policy.policy_number})` : ec ? ec.insured : "");
  setField("line", cap1(c.line || (ec && ec.line)));
  const when = f.loss_time ? fmtWhen(f.loss_date, f.loss_time)
    : f.discovered_at ? fmtWhen(f.discovered_at.slice(0, 10), f.discovered_at.slice(11, 16))
    : fmtWhen(f.loss_date || (ec && ec.loss_date));
  setField("when", when);
  setField("where", [f.location, f.state].filter(Boolean).join(", ") || f.systems_affected || f.claimant_name || "");
  setField("what", f.description || (ec && ec.summary) || (sr && sr.details) || "");

  // act now
  const acts = c.urgent_actions || [];
  $("actNow").hidden = !acts.length;
  els.actions.innerHTML = acts.map((a) => {
    const due = a.deadline ? `<span class="due" data-deadline="${esc(a.deadline)}"></span>` : "";
    return `<li data-id="${esc(a.id)}"><span>${esc(a.text)}</span>${due}</li>`;
  }).join("");
  els.actions.querySelectorAll("li").forEach((li) => { if (!seenActions.has(li.dataset.id)) flash(li); seenActions.add(li.dataset.id); });
  tickCountdowns();

  // assessment
  const sev = c.severity || 0;
  els.sevMeter.innerHTML = [1, 2, 3, 4, 5].map((i) => `<i class="${i === sev ? "on" : ""}">${i}</i>`).join("");
  els.sevDrivers.textContent = c.severity_drivers && c.severity_drivers.length ? `Because: ${c.severity_drivers.join(", ")}` : "";
  if (nonClaim) {
    const target = ec ? ec.team : sr ? `${cap1(sr.topic)} team` : cap1(c.call_reason);
    els.routing.innerHTML = `<span class="check on">${esc(target)}</span>`;
  } else {
    const q = c.routing ? c.routing.queue : null;
    const also = c.routing ? c.routing.secondary : [];
    els.routing.innerHTML = QUEUES.map((x) => `<span class="check${x === q ? " on" : ""}">${esc(cap1(x))}</span>`).join("")
      + also.map((x) => `<span class="check on also">Also: ${esc(pretty(x))}</span>`).join("");
  }

  // for the adjuster
  const flags = c.flags || [];
  els.flags.innerHTML = flags.map((fl) => {
    const cls = [fl.severity === "low" ? "low" : "", fl.internal ? "internal" : ""].join(" ").trim();
    return `<li class="${cls}" data-k="${esc(fl.code + fl.reason)}">${esc(cap1(fl.code))}<span class="why">${esc(fl.reason)}</span></li>`;
  }).join("");
  els.flags.querySelectorAll("li").forEach((li) => { if (!seenFlags.has(li.dataset.k)) flash(li); seenFlags.add(li.dataset.k); });

  const cov = c.coverage_triggers || [];
  setField("coverage", cov.map((t) => `${pretty(t.coverage)}${t.on_policy ? " (on the policy)" : " (not on the policy)"}`).join("; "));

  const miss = c.missing_required || [];
  els.missing.innerHTML = c.line
    ? (miss.length ? miss.map((m) => `<li>${esc(cap1(m))}</li>`).join("") : `<li>Nothing. Ready to file.</li>`)
    : ec ? `<li>Nothing. This is an existing claim.</li>` : sr ? `<li>Nothing. Request logged.</li>` : "";
  const docs = ec && !c.line ? ec.outstanding || [] : c.missing_documents || [];
  els.docs.innerHTML = docs.map((d) => `<li>${esc(d)}</li>`).join("");

  // guardrails: what the deterministic checks caught on this call
  const g = c.guardrails || { events: [], agent_turns_checked: 0 };
  const turns = g.agent_turns_checked;
  els.guards.innerHTML = g.events.map((e) => `<li class="caught" data-k="${esc(e.at + e.code + e.text)}">${esc(e.text)}</li>`).join("")
    + (turns || g.events.length
      ? `<li class="count">${turns} agent ${turns === 1 ? "reply" : "replies"} checked for promises about coverage, payment, or fault${g.events.length ? "" : ". Nothing caught."}</li>`
      : "");
  els.guards.querySelectorAll("li.caught").forEach((li) => { if (!seenFlags.has(li.dataset.k)) flash(li); seenFlags.add(li.dataset.k); });

  setField("brief", c.adjuster_brief || "");

  const lines = JSON.stringify({ claim_number: c.claim_number, status: c.status, line: c.line, fields: c.fields, routing: c.routing, dot_testing: c.dot_testing }, null, 2).split("\n");
  els.json.innerHTML = lines.map((l) => (prevJsonLines.size && !prevJsonLines.has(l) ? `<span class="new">${esc(l)}</span>` : esc(l))).join("\n");
  prevJsonLines = new Set(lines);
}

async function callTool(name, args) {
  const r = await fetch(`/api/tools/${name}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ session_id: sessionId, args }),
  });
  if (!r.ok) return { tool_result: { error: `backend ${r.status}` }, claim: null };
  return r.json();
}

async function freshSnapshot() {
  render(await (await fetch(`/api/claim/${sessionId}`)).json());
}

// ------------------------------------------------------------------ landing: personas and views

const PERSONAS = [
  { id: "truck", scenario: "trucking_crash", name: "Marcus Hill, truck driver", gist: "rear-ended a car on I-80",
    ref: "XYZ Freight, policy CT-20417",
    story: "The other driver went to the hospital. Both vehicles were towed and the trooper gave you a ticket.",
    opener: "I need to report an accident.", watch: "the DOT drug and alcohol test deadlines it gives you" },
  { id: "wire", scenario: "wire_fraud", name: "Dan Wu, CFO", gist: "wired $186,000 to a scammer",
    ref: "ABC Labs, policy TS-88120",
    story: "A vendor's bank details were changed by email. The payment went out yesterday morning and you feel awful about it.",
    opener: "I think we wired money to a scammer.", watch: "how it responds to you being upset, and the 72-hour recall deadline" },
  { id: "ransom", scenario: "ransomware", name: "Priya Shah, CEO", gist: "hit by ransomware",
    ref: "ABC Labs, policy TS-88120",
    story: "Your build servers are encrypted and there's a note demanding 4 bitcoin. Ask whether you should just pay.",
    opener: "We've been hit with ransomware.", watch: "what it tells you about paying" },
  { id: "status", scenario: "claim_status", name: "Marcus Hill, following up", gist: "checking on an open cargo claim",
    ref: "XYZ Freight, claim CLM-260902-4417",
    story: "Ask where the claim stands, then tell them the receiver found two of the missing pallets.",
    opener: "I'm calling to check on a claim.", watch: "the status it reads back and the note it adds" },
  { id: "free", scenario: "edge_no_coverage", name: "Anyone you like", gist: "trying to break it",
    ref: "Any policy above, or none",
    story: "Invent a loss, change your story halfway through, ask it to approve the claim, or ask for a person.",
    opener: "Say whatever you want.", watch: "whether it stays honest" },
];
let persona = PERSONAS[0];
let liveConfig = { access_code_required: false, max_call_minutes: 6, filler_ms: 500 };

function setView(view) {
  document.body.classList.toggle("view-welcome", view === "welcome");
  document.body.classList.toggle("view-call", view === "call");
  window.scrollTo(0, 0);
}

function renderPersonas() {
  els.personas.innerHTML = PERSONAS.map((p) => `
    <label class="caller-opt">
      <input type="radio" name="caller" value="${p.id}"${p.id === persona.id ? " checked" : ""} />
      <span><span class="name">${esc(p.name)}</span> <span class="gist">${esc(p.gist)}</span></span>
      <div class="more">
        <p class="typed">${esc(p.ref)}</p>
        <p>${esc(p.story)}</p>
        <p>Start with "${esc(p.opener)}" Listen for ${esc(p.watch)}.</p>
      </div>
    </label>`).join("");
  els.personas.querySelectorAll("input").forEach((r) =>
    r.addEventListener("change", () => { persona = PERSONAS.find((p) => p.id === r.value); }));
}

function renderRole(mode, rec) {
  els.role.innerHTML = mode === "replay"
    ? `<strong>Recorded call: ${esc(rec.title)}.</strong>
       <p>Caller: ${esc(rec.who)}. The script is fixed, but every step runs through the real claims engine, so the form is filled in live.</p>`
    : `<strong>You're ${esc(persona.name)}, ${esc(persona.gist)}.</strong>
       <p>${esc(persona.ref)}. ${esc(persona.story)}</p>
       <p>Start with "${esc(persona.opener)}" Then answer whatever it asks.</p>`;
}

function playSample() {
  // The landing page's one moment: the example form types itself in, once.
  const lines = [...document.querySelectorAll(".sheet-sample .sample-line")];
  if (reduceMotion) return;
  const texts = lines.map((el) => el.textContent);
  lines.forEach((el) => { el.textContent = ""; });
  lines.forEach((el, i) => setTimeout(() => { el.textContent = texts[i]; flash(el); }, 500 + i * 420));
}

function showStartError(msg) {
  els.startError.textContent = msg;
  els.startError.classList.toggle("hidden", !msg);
}

async function loadConfig() {
  try { liveConfig = await (await fetch("/api/config")).json(); } catch {}
  els.maxMin.textContent = liveConfig.max_call_minutes;
  let code = params.get("code") || "";
  try { code = code || sessionStorage.getItem("claimline_code") || ""; } catch {}
  els.accessCode.value = code;
  // Shared links can carry ?code=..., so testers never see the field.
  els.codeField.classList.toggle("hidden", !liveConfig.access_code_required || !!params.get("code"));
}

// ------------------------------------------------------------------ replay

// Who is on each recorded call, as shown in the list.
const RECORDED_CALLERS = {
  trucking_crash: "Marcus Hill, truck driver at XYZ Freight",
  ransomware: "Priya Shah, ABC Labs",
  wire_fraud: "Dan Wu, CFO at ABC Labs",
  eo_ai_demand: "Priya Shah, ABC Labs",
  third_party: "Rosa Delgado, the other driver",
  edge_no_coverage: "Leo Park, founder of PQR AI",
  claim_status: "Marcus Hill, following up on a claim",
  coi_request: "Priya Shah, ABC Labs",
  guardrails_check: "Jen Okafor, safety manager at XYZ Freight",
};
let scenarioList = [];

function setRecordedDisabled(off) {
  els.recorded.querySelectorAll("button").forEach((b) => { b.disabled = off; });
}

function callLength(id) {
  const lines = Object.values(replayManifest?.scenarios?.[id]?.lines || {});
  if (!lines.length) return "";
  const s = Math.round(lines.reduce((t, l) => t + l.ms + 300, 0) / 1000);
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}

function renderRecorded() {
  els.recorded.innerHTML = scenarioList.map((sc) => `
    <li><button class="rec" data-id="${esc(sc.id)}">
      <span class="rec-title">${esc(sc.title)}</span>
      <span class="rec-len">${esc(callLength(sc.id))}</span>
      <span class="rec-who">${esc(RECORDED_CALLERS[sc.id] || "")}</span>
    </button></li>`).join("");
  els.recorded.querySelectorAll("button").forEach((b) =>
    b.addEventListener("click", () => { els.scenario.value = b.dataset.id; replay(); }));
}

async function loadManifest() {
  if (replayManifest !== null) return;
  try {
    const r = await fetch("/static/replay/manifest.json", { cache: "no-cache" });
    replayManifest = r.ok ? await r.json() : {};
  } catch { replayManifest = {}; }
}

async function loadScenarios() {
  const [list] = await Promise.all([(await fetch("/api/scenarios")).json(), loadManifest()]);
  scenarioList = list;
  els.scenario.innerHTML = list.map((s) => `<option value="${esc(s.id)}">${esc(s.title)}</option>`).join("");
  renderRecorded();
  const auto = params.get("replay"); // ?replay=trucking_crash starts that scenario on load
  if (auto && list.some((s) => s.id === auto)) {
    els.scenario.value = auto;
    replay();
  }
}

// Pre-recorded voices for replays (scripts/make_replay_audio.py). Missing audio falls back to silent timing.
var replayManifest = null;
let replayAudio = null;

async function loadReplayClips(scenarioId, steps) {
  await loadManifest();
  const lines = replayManifest.scenarios?.[scenarioId]?.lines || {};
  const clips = {};
  await Promise.all(steps.map((step, i) => {
    const line = lines[String(i)];
    const text = step.caller || step.agent;
    if (!line || line.text !== text) return null; // script changed since recording
    const audio = new Audio(`/static/replay/${scenarioId}/${line.file}`);
    audio.preload = "auto";
    clips[i] = audio;
    return new Promise((ok) => {
      audio.addEventListener("canplaythrough", ok, { once: true });
      audio.addEventListener("error", () => { delete clips[i]; ok(); }, { once: true });
      setTimeout(ok, 4000);
    });
  }));
  return clips;
}

function playClip(audio) {
  // Resolves when the clip ends or the replay is stopped; false if the browser refused to play.
  return new Promise((done) => {
    replayAudio = audio;
    audio.playbackRate = Math.min(2, SPEED);
    const finish = (ok) => { audio.onended = audio.onpause = null; replayAudio = null; done(ok); };
    audio.onended = () => finish(true);
    audio.onpause = () => finish(true);
    audio.play().catch(() => finish(false));
  });
}

async function replay() {
  if (replaying) return;
  const r = await fetch(`/api/demo/${els.scenario.value}`, { method: "POST" });
  const demo = await r.json();
  const clips = await loadReplayClips(els.scenario.value, demo.steps);
  clearAll();
  setView("call");
  const sc = scenarioList.find((x) => x.id === els.scenario.value) || { title: "" };
  renderRole("replay", { title: sc.title, who: RECORDED_CALLERS[sc.id] || "" });
  sessionId = demo.session_id;
  await freshSnapshot();
  replaying = true;
  els.end.disabled = false;
  els.end.textContent = "Stop";
  setCallState("Playing a recorded call", "");
  for (const [i, step] of demo.steps.entries()) {
    if (!replaying) break;
    if (step.caller || step.agent) {
      const who = step.caller ? "caller" : "agent";
      const text = step.caller || step.agent;
      addLine(who, text);
      if (who === "agent") checkAgentTurn(text);
      const played = clips[i] && SPEED <= 2 ? await playClip(clips[i]) : false;
      await sleep(played ? 300 : Math.min(3200, Math.max(900, text.length * 28)));
    } else if (step.tool) {
      const { tool_result, claim } = await callTool(step.tool, step.args);
      logTool(step.tool, step.args, tool_result);
      if (claim) render(claim);
      await sleep(450);
    }
  }
  replaying = false;
  els.end.disabled = true;
  els.end.textContent = "End call";
  setCallState("Recorded call finished", "");
}

// ------------------------------------------------------------------ live call

function send(event) {
  if (dc && dc.readyState === "open") dc.send(JSON.stringify(event));
}

// Output guardrail: every finished agent turn is checked server-side. If it promised coverage, payment, or
// fault (or claimed to be human), the agent is told to correct itself on its very next turn.
let responseActive = false;
let pendingCorrection = null;

function correctionItem(text) {
  return { type: "conversation.item.create", item: { type: "message", role: "system", content: [{ type: "input_text", text }] } };
}

async function checkAgentTurn(text) {
  if (!sessionId || !text || !text.trim()) return;
  try {
    const r = await fetch("/api/guardrails/agent_turn", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ session_id: sessionId, text }),
    });
    if (!r.ok) return;
    const out = await r.json();
    if (out.claim) render(out.claim);
    if (out.correction && dc && dc.readyState === "open") {
      logTool("guardrail", { agent_said: text }, { violations: out.violations.map((v) => v.code) });
      if (responseActive) pendingCorrection = out.correction;
      else { send(correctionItem(out.correction)); send({ type: "response.create" }); }
    }
  } catch { /* a failed check never breaks the call */ }
}

async function handleFunctionCalls(outputs) {
  const calls = outputs.filter((o) => o.type === "function_call" && !handledCalls.has(o.call_id));
  if (!calls.length) return;
  for (const c of calls) {
    handledCalls.add(c.call_id);
    let args = {};
    try { args = JSON.parse(c.arguments || "{}"); } catch { args = {}; }
    const { tool_result, claim } = await callTool(c.name, args);
    logTool(c.name, args, tool_result);
    if (claim) render(claim);
    send({ type: "conversation.item.create", item: { type: "function_call_output", call_id: c.call_id, output: JSON.stringify(tool_result) } });
  }
  if (pendingCorrection) { send(correctionItem(pendingCorrection)); pendingCorrection = null; }
  send({ type: "response.create" });
  if (filler) filler.toolRoundTrip(calls.some((c) => LOOKUP_TOOLS.has(c.name)) ? "lookup" : "ack");
}

const AGENT_AUDIO_EVENTS = new Set([
  "output_audio_buffer.started", "response.output_audio.delta", "response.audio.delta",
  "response.output_audio_transcript.delta", "response.audio_transcript.delta",
]);

function handleEvent(ev) {
  if (filler) {
    if (ev.type === "input_audio_buffer.speech_stopped") filler.userStoppedSpeaking();
    else if (ev.type === "input_audio_buffer.speech_started") filler.userStartedSpeaking();
    else if (AGENT_AUDIO_EVENTS.has(ev.type)) filler.agentAudioStarted();
  }
  switch (ev.type) {
    case "conversation.item.input_audio_transcription.completed":
      addLine("caller", ev.transcript);
      break;
    case "response.output_audio_transcript.done": // GA name
    case "response.audio_transcript.done": // beta name
      addLine("agent", ev.transcript);
      checkAgentTurn(ev.transcript);
      break;
    case "response.created":
      responseActive = true;
      break;
    case "response.done": {
      responseActive = false;
      const out = (ev.response && ev.response.output) || [];
      if (out.some((o) => o.type === "function_call")) handleFunctionCalls(out);
      else if (pendingCorrection) {
        send(correctionItem(pendingCorrection)); pendingCorrection = null; send({ type: "response.create" });
      }
      break;
    }
    case "error":
      console.error("realtime error", ev);
      logTool("realtime_error", {}, { error: ev.error ? ev.error.message : "unknown" });
      break;
  }
}

async function startCall() {
  showStartError("");
  const code = els.accessCode.value.trim();
  if (liveConfig.access_code_required && !code) {
    els.codeField.classList.remove("hidden");
    els.accessCode.focus();
    showStartError("Enter the access code from the message you received.");
    return;
  }
  els.start.disabled = true;
  setRecordedDisabled(true);

  // Ask for the mic before creating a paid session, so a denied mic costs nothing.
  try {
    micStream = await navigator.mediaDevices.getUserMedia({ audio: true });
  } catch {
    showStartError("The microphone is blocked. Allow it from the icon in the address bar and press Start again, or play a recorded call.");
    els.start.disabled = false; setRecordedDisabled(false);
    return;
  }

  let s;
  try {
    const r = await fetch("/session", { method: "POST", headers: { "X-Access-Code": code } });
    s = await r.json();
    if (!r.ok) {
      if (r.status === 401) { try { sessionStorage.removeItem("claimline_code"); } catch {} els.codeField.classList.remove("hidden"); }
      throw new Error(r.status === 401 ? "That access code doesn't match. Check the message you received." : r.status === 429 ? s.detail : `The call didn't start: ${s.detail || r.status}.`);
    }
    try { sessionStorage.setItem("claimline_code", code); } catch {}
  } catch (err) {
    micStream.getTracks().forEach((t) => t.stop()); micStream = null;
    showStartError(err.message);
    els.start.disabled = false; setRecordedDisabled(false);
    return;
  }

  setView("call");
  renderRole("live");
  clearAll();
  sessionId = s.session_id;
  setCallState("Connecting", "");
  els.end.disabled = false;
  els.end.textContent = "End call";
  const maxMin = s.max_call_minutes || liveConfig.max_call_minutes || 6;
  latencies = []; fillerCount = 0; renderLatency();
  filler = newFillerController(Number(params.get("filler_ms") ?? liveConfig.filler_ms ?? 500));
  callTimer = setTimeout(() => { addLine("note", `Demo calls end after ${maxMin} minutes. Thanks for trying it.`); endCall(); }, maxMin * 60000);
  await freshSnapshot();

  try {
    pc = new RTCPeerConnection();
    const audio = document.createElement("audio");
    audio.autoplay = true;
    pc.ontrack = (e) => { audio.srcObject = e.streams[0]; };
    pc.addTrack(micStream.getTracks()[0], micStream);

    dc = pc.createDataChannel("oai-events");
    dc.onmessage = (e) => handleEvent(JSON.parse(e.data));
    dc.onopen = () => {
      setCallState("", "live");
      els.textInput.disabled = false;
      els.textBtn.disabled = false;
      send({ type: "response.create" }); // agent opens the call
    };

    const offer = await pc.createOffer();
    await pc.setLocalDescription(offer);
    const sdpResp = await fetch("https://api.openai.com/v1/realtime/calls", {
      method: "POST",
      body: offer.sdp,
      headers: { Authorization: `Bearer ${s.client_secret}`, "Content-Type": "application/sdp" },
    });
    if (!sdpResp.ok) throw new Error(`SDP exchange failed: ${sdpResp.status} ${await sdpResp.text()}`);
    await pc.setRemoteDescription({ type: "answer", sdp: await sdpResp.text() });
  } catch (err) {
    console.error(err);
    logTool("start_call", {}, { error: err.message });
    addLine("note", "The call didn't connect. Press New call and try again.");
    endCall();
  }
}

function endCall() {
  if (replaying) { replaying = false; }
  if (replayAudio) { replayAudio.pause(); replayAudio = null; }
  if (callTimer) { clearTimeout(callTimer); callTimer = null; }
  if (filler) { filler.reset(); filler = null; }
  if (dc) dc.close();
  if (pc) pc.close();
  if (micStream) micStream.getTracks().forEach((t) => t.stop());
  dc = pc = micStream = null;
  setCallState("Call ended", "");
  els.start.disabled = false;
  setRecordedDisabled(false);
  els.end.disabled = true;
  els.end.textContent = "End call";
  els.textInput.disabled = true;
  els.textBtn.disabled = true;
}

function newCall() {
  endCall();
  showStartError("");
  setView("welcome");
}

els.start.addEventListener("click", startCall);
els.end.addEventListener("click", endCall);
els.newCall.addEventListener("click", newCall);
els.textForm.addEventListener("submit", (e) => {
  e.preventDefault();
  const text = els.textInput.value.trim();
  if (!text) return;
  addLine("caller", text);
  send({ type: "conversation.item.create", item: { type: "message", role: "user", content: [{ type: "input_text", text }] } });
  send({ type: "response.create" });
  els.textInput.value = "";
});

renderPersonas();
playSample();
loadConfig();
loadScenarios();
loadFillers();
