// Replays decisions recorded by the real gateway (assets/demo-data.json).
// Nothing here evaluates policy: every outcome comes from the recorded data.
// All recorded text is rendered with textContent, never innerHTML.

const STAGES = ["Agent request", "Deterministic policy", "Reasoning auditor", "Human approval", "Executor"];
const STEP_MS = 700;

const PROVENANCE_NOTES = {
  PROVIDER_EXPOSED_TRACE: "Reasoning returned in a provider response field and captured in-process with the capture credential.",
  AGENT_AUTHORED_SUMMARY: "A summary the agent wrote about itself. Not a raw trace.",
  SYNTHETIC_FIXTURE: "A hand-written demo trace stored with the scenario. Not captured from a model.",
  CALLER_ASSERTED_SUMMARY: "The caller claimed this was an agent summary but sent no capture credential, so the claim is unverified.",
  CALLER_ASSERTED_PROVIDER_TRACE: "The caller claimed this was a provider trace but sent no capture credential, so the claim is unverified.",
  UNAVAILABLE: "No reasoning was exposed for this action.",
};

const els = {
  list: document.getElementById("scenario-list"),
  title: document.getElementById("scn-title"),
  desc: document.getElementById("scn-desc"),
  scope: document.getElementById("scn-scope"),
  track: document.getElementById("track"),
  review: document.getElementById("review-bar"),
  timeline: document.getElementById("timeline"),
  evidence: document.getElementById("evidence"),
  play: document.getElementById("btn-play"),
  step: document.getElementById("btn-step"),
  reset: document.getElementById("btn-reset"),
};

const state = {
  runs: [],
  scenario: 0,
  action: 0, // index of the action in focus
  stage: -1, // last revealed stage of the focused action (-1 = not started)
  viewing: null, // action index the user clicked to inspect, or null to follow playback
  resolutions: new Map(), // action index -> "APPROVED" | "DENIED"
  timer: null,
  pausedForReview: false, // playback stopped at a held action; resume after Approve/Deny
};

// ---------- small DOM helpers ----------
function el(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (value === undefined || value === null) continue;
    if (key === "class") node.className = value;
    else if (key === "text") node.textContent = value;
    else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else node.setAttribute(key, value);
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

// Let long reason codes wrap at underscores instead of mid-word.
function breakable(text) {
  return String(text ?? "").replace(/_/g, "_\u200b");
}

function pill(text, kind, extra = "") {
  return el("span", { class: `pill ${kind} ${extra}`.trim(), text });
}

function outcomeKind(outcome) {
  return { ALLOW: "allow", DENY: "deny", HOLD: "hold" }[outcome] || "muted";
}

function scenarioNumber(run) {
  return run.file.slice(0, 2);
}

// ---------- derived state ----------
function currentRun() {
  return state.runs[state.scenario];
}

function isPendingHold(action) {
  return action.gateway_decision.outcome === "HOLD" && !action.execution;
}

function stageReached(index) {
  if (index < state.action) return 4;
  if (index === state.action) return state.stage;
  return -1;
}

function nodesFor(action, index) {
  const det = action.deterministic_policy;
  const gw = action.gateway_decision;
  const audit = action.audit;
  const resolution = state.resolutions.get(index);

  const nodes = [
    { kind: "info", s: action.operation, x: action.resource },
    { kind: outcomeKind(det.outcome), s: det.outcome, x: det.reason_code },
  ];

  if (det.outcome === "DENY") {
    nodes.push({ kind: "skip", s: "NOT REACHED", x: "policy DENY is final" });
  } else if (!audit) {
    nodes.push({ kind: "skip", s: "NO AUDIT", x: "none recorded" });
  } else if (audit.verdict === "CONCERN") {
    nodes.push({ kind: "hold", s: "CONCERN", x: `${audit.concern_type}, escalates ALLOW → HOLD` });
  } else if (audit.verdict === "NO_CONCERN") {
    nodes.push({ kind: "allow", s: "NO CONCERN", x: "no escalation" });
  } else {
    nodes.push({ kind: "hold", s: audit.verdict, x: audit.error_code || "fails closed to HOLD" });
  }

  if (gw.outcome === "HOLD") {
    if (resolution === "APPROVED") nodes.push({ kind: "allow", s: "APPROVED", x: "by you (single-use)" });
    else if (resolution === "DENIED") nodes.push({ kind: "deny", s: "DENIED", x: "by you" });
    else nodes.push({ kind: "hold", s: "PENDING", x: gw.reason_code });
  } else if (gw.outcome === "DENY") {
    nodes.push({ kind: "skip", s: "NOT ALLOWED", x: "approval can't override DENY" });
  } else {
    nodes.push({ kind: "skip", s: "NOT NEEDED", x: "decision was ALLOW" });
  }

  const exec = action.execution;
  if (exec && exec.status === "EXECUTED") nodes.push({ kind: "allow", s: "EXECUTED", x: exec.executor });
  else if (exec) nodes.push({ kind: "deny", s: "NOT RUN", x: exec.error_code || "refused: no allowing decision" });
  else if (resolution === "APPROVED") nodes.push({ kind: "info", s: "RUNS ONCE", x: "approval consumed (not replayed here)" });
  else if (resolution === "DENIED") nodes.push({ kind: "deny", s: "NOT RUN", x: "denied by reviewer" });
  else nodes.push({ kind: "hold", s: "WAITING", x: "no decision, no execution" });

  return nodes;
}

function finalPill(action, index) {
  const gw = action.gateway_decision;
  const resolution = state.resolutions.get(index);
  if (gw.outcome === "HOLD" && resolution) {
    return pill(`HOLD → ${resolution}`, resolution === "APPROVED" ? "allow" : "deny");
  }
  return pill(`${gw.outcome} · ${gw.reason_code}`, outcomeKind(gw.outcome));
}

// ---------- rendering ----------
function renderScenarioList() {
  els.list.replaceChildren(
    ...state.runs.map((run, i) => {
      const outcomes = run.actions.map((a) => a.gateway_decision.outcome);
      const worst = outcomes.includes("HOLD") ? "HOLD" : outcomes.includes("DENY") ? "DENY" : "ALLOW";
      return el(
        "button",
        {
          class: "scn",
          type: "button",
          "aria-pressed": String(i === state.scenario),
          onclick: () => selectScenario(i, true),
        },
        el("span", { class: "top" }, el("span", { class: "no", text: `Scenario ${scenarioNumber(run)}` }), pill(worst, outcomeKind(worst))),
        el("span", { class: "nm", text: run.name }),
      );
    }),
  );
}

function chips(values, cls = "") {
  if (!values || values.length === 0) return el("div", { class: "chips" }, el("span", { class: "chip none", text: "none" }));
  return el("div", { class: "chips" }, values.map((v) => el("span", { class: `chip ${cls}`.trim(), text: v })));
}

function renderHeader() {
  const run = currentRun();
  els.title.textContent = `${scenarioNumber(run)} · ${run.name}`;
  els.desc.textContent = run.description;
  const scope = run.task_scope;
  els.scope.replaceChildren(
    el("div", {}, el("div", { class: "lbl", text: "Task" }), el("p", { class: "muted", style: "font-size:14px", text: scope.task_description })),
    el("div", {}, el("div", { class: "lbl", text: "Allowed paths" }), chips(scope.allowed_paths)),
    el("div", {}, el("div", { class: "lbl", text: "Blocked paths" }), chips(scope.blocked_paths, "bad")),
    el("div", {}, el("div", { class: "lbl", text: "Allowed operations" }), chips(scope.allowed_operations)),
    el("div", {}, el("div", { class: "lbl", text: "Needs approval" }), chips(scope.requires_approval)),
    el("div", {}, el("div", { class: "lbl", text: "Network" }), chips(scope.allowed_network_destinations)),
  );
}

function focusedIndex() {
  return state.viewing ?? state.action;
}

function renderTrack() {
  const run = currentRun();
  const index = focusedIndex();
  const action = run.actions[index];
  const reached = stageReached(index);
  const nodes = nodesFor(action, index);
  els.track.replaceChildren(
    ...nodes.map((node, i) => {
      let cls = "node";
      if (i <= reached) cls += node.kind === "skip" ? " skip done" : ` ${node.kind} done`;
      if (i === reached && reached < 4 && state.timer) cls += " active";
      return el(
        "div",
        { class: cls },
        el("div", { class: "h" }, el("span", { class: "t", text: STAGES[i] })),
        el("div", { class: "s", text: i <= reached ? breakable(node.s) : "…" }),
        el("div", { class: "x", text: i <= reached ? breakable(node.x) : "" }),
      );
    }),
  );
  renderReviewBar(action, index, reached);
}

function approvalButtons(index) {
  return el(
    "div",
    { class: "actions" },
    el("button", { class: "btn btn-sm btn-approve", type: "button", onclick: () => resolve(index, "APPROVED") }, "Approve"),
    el("button", { class: "btn btn-sm btn-deny", type: "button", onclick: () => resolve(index, "DENIED") }, "Deny"),
  );
}

// Inline prompt under the pipeline so a held action can be decided without scrolling.
function renderReviewBar(action, index, reached) {
  const awaiting = reached >= 3 && isPendingHold(action) && !state.resolutions.has(index);
  els.review.hidden = !awaiting;
  if (!awaiting) {
    els.review.replaceChildren();
    return;
  }
  els.review.replaceChildren(
    el(
      "div",
      {},
      el("strong", { text: "Held for you. " }),
      el("span", { text: `${action.operation} on ${action.resource} needs a reviewer (${action.gateway_decision.reason_code}). Read the evidence below, then decide.` }),
    ),
    approvalButtons(index),
  );
}

function renderTimeline() {
  const run = currentRun();
  const focus = focusedIndex();
  els.timeline.replaceChildren(
    ...run.actions.map((action, i) => {
      const reached = stageReached(i);
      const started = reached >= 0;
      return el(
        "li",
        {
          class: `tl ${started ? "" : "pending"}`.trim(),
          "aria-current": String(i === focus),
          tabindex: "0",
          onclick: () => inspect(i),
          onkeydown: (event) => {
            if (event.key === "Enter" || event.key === " ") {
              event.preventDefault();
              inspect(i);
            }
          },
        },
        el("span", { class: "i", text: String(i + 1) }),
        el("div", {}, el("div", { class: "op", text: `${action.tool}.${action.operation}` }), el("div", { class: "rs", text: action.resource })),
        reached >= 4
          ? finalPill(action, i)
          : reached >= 3 && isPendingHold(action) && !state.resolutions.has(i)
            ? pill("HOLD · awaiting reviewer", "hold")
            : started
              ? pill("deciding…", "info")
              : pill("queued", "muted", "no-dot"),
      );
    }),
  );
}

function kv(key, value, missingText) {
  const missing = value === null || value === undefined || value === "";
  return el("div", { class: "kv" }, el("div", { class: "k", text: key }), el("div", { class: `v ${missing ? "missing" : ""}`.trim(), text: missing ? missingText || "Not recorded" : value }));
}

function highlighted(text, excerpts) {
  const node = el("div", { class: "quote" });
  const match = (excerpts || []).filter((e) => e && text.includes(e)).sort((a, b) => b.length - a.length)[0];
  if (!match) {
    node.textContent = text;
    return node;
  }
  const parts = text.split(match);
  parts.forEach((part, i) => {
    node.append(document.createTextNode(part));
    if (i < parts.length - 1) node.append(el("mark", { class: "warn", text: match }));
  });
  return node;
}

function section(title, badge, ...body) {
  return el("div", { class: "ev" }, el("div", { class: "ev-h" }, el("span", { class: "t", text: title }), badge), el("div", { class: "ev-b" }, ...body));
}

function whatNext(action, resolution) {
  if (resolution === "DENIED") {
    return "The gateway closes the request without running it. The executor never sees it, and the agent gets a denial back.";
  }
  if (action.operation === "delete_path") {
    return "The gateway consumes your single-use approval and the executor runs delete_path once. In this baseline, delete is simulated: the receipt reports it but the file is not removed.";
  }
  return `The gateway consumes your single-use approval and the executor runs ${action.operation} on ${action.resource} exactly once. Re-sending the same approval would be refused. This is why the reviewer reads the evidence above first.`;
}

function renderEvidence() {
  const run = currentRun();
  const index = focusedIndex();
  const action = run.actions[index];
  const reached = stageReached(index);
  if (reached < 0) {
    els.evidence.replaceChildren(el("p", { class: "empty", text: "This action hasn't been submitted yet. Press Play or Next step." }));
    return;
  }

  const blocks = [];
  const traceExcerpts = action.audit ? action.audit.flagged_excerpts : [];

  blocks.push(
    section(
      "1 · Observation",
      pill("ACTION_REQUESTED", "info", "no-dot"),
      kv("Tool", action.tool),
      kv("Operation", action.operation),
      kv("Resource", action.resource),
      kv("Arguments", JSON.stringify(action.arguments ?? {}, null, 1)),
    ),
  );

  const provenance = action.reasoning_provenance || "UNAVAILABLE";
  blocks.push(
    section(
      "Reasoning provenance",
      pill(provenance, provenance === "PROVIDER_EXPOSED_TRACE" ? "allow" : provenance.startsWith("CALLER") ? "hold" : "info", "no-dot"),
      el("p", { class: "sim-note", text: PROVENANCE_NOTES[provenance] || "" }),
      kv("Summary", action.reasoning_summary, "No summary supplied"),
      action.exposed_reasoning_trace
        ? el("div", {}, el("div", { class: "kv" }, el("div", { class: "k", text: "Exposed trace" })), highlighted(action.exposed_reasoning_trace, reached >= 2 ? traceExcerpts : []))
        : kv("Exposed trace", null, "Unavailable. No trace was exposed for this action."),
    ),
  );

  if (reached >= 1) {
    const det = action.deterministic_policy;
    const gw = action.gateway_decision;
    blocks.push(
      section(
        "2 · Policy decision",
        pill(gw.outcome, outcomeKind(gw.outcome)),
        kv("Deterministic", `${det.outcome} [${det.reason_code}]`),
        kv("Final decision", `${gw.outcome} [${gw.reason_code}]`),
        kv("Matched rule", gw.matched_rule),
        kv("Explanation", gw.explanation),
      ),
    );
  }

  if (reached >= 2) {
    const audit = action.audit;
    if (!audit) {
      blocks.push(section("Reasoning audit", pill("NOT REACHED", "muted", "no-dot"), el("p", { class: "muted", style: "font-size:14px", text: "Policy denied this action, so it never reached the auditor. A deterministic DENY is final and no model output can change it." })));
    } else {
      blocks.push(
        section(
          "Reasoning audit",
          pill(audit.verdict, audit.verdict === "NO_CONCERN" ? "allow" : "hold"),
          kv("Concern type", audit.concern_type, "None"),
          kv("Explanation", audit.explanation),
          kv("Auditor", `${audit.model} (profile: ${audit.profile})`),
          kv("Flagged excerpts", (audit.flagged_excerpts || []).length ? audit.flagged_excerpts.map((e) => `“${e}”`).join("\n") : null, "None"),
        ),
      );
    }
  }

  if (reached >= 3) {
    const gw = action.gateway_decision;
    const resolution = state.resolutions.get(index);
    if (gw.outcome === "HOLD" && isPendingHold(action)) {
      const box = el(
        "div",
        { class: "approval-box" },
        el("div", {}, el("strong", { text: `Approval required: ${action.operation}` }), el("div", { class: "sim-note", text: `Resource: ${action.resource} · Reason: ${gw.reason_code}` })),
      );
      if (resolution) {
        box.append(el("div", { class: "what-next", text: whatNext(action, resolution) }));
      } else {
        box.append(
          approvalButtons(index),
          el("p", { class: "sim-note", text: "The recorded gateway state is PENDING. Your choice is shown here only and isn't sent anywhere." }),
        );
      }
      blocks.push(section("3 · Human approval", pill(resolution || "PENDING", resolution === "APPROVED" ? "allow" : resolution === "DENIED" ? "deny" : "hold"), box));
    } else {
      blocks.push(section("3 · Human approval", pill("NOT REQUIRED", "muted", "no-dot"), el("p", { class: "muted", style: "font-size:14px", text: gw.outcome === "DENY" ? "Approvals cannot override a policy DENY, so none was offered." : "The decision was ALLOW, so no approval was needed." })));
    }
  }

  if (reached >= 4) {
    const exec = action.execution;
    if (exec) {
      const result = exec.result || {};
      const preview = result.preview ?? (result.items ? result.items.map((i) => `${i.type === "dir" ? "📁" : "📄"} ${i.name}`).join("\n") : null);
      blocks.push(
        section(
          "4 · Execution receipt",
          pill(exec.status, exec.status === "EXECUTED" ? "allow" : "deny"),
          kv("Executor", exec.executor),
          kv("Error code", exec.error_code, "None"),
          preview ? el("div", {}, el("div", { class: "kv" }, el("div", { class: "k", text: "Output" })), highlighted(preview, [])) : kv("Output", JSON.stringify(result, null, 1)),
        ),
      );
    } else {
      blocks.push(section("4 · Execution receipt", pill("NONE YET", "hold", "no-dot"), el("p", { class: "muted", style: "font-size:14px", text: "No receipt. The executor refuses anything without an allowing decision or a valid approval." })));
    }
  }

  els.evidence.replaceChildren(...blocks);
}

function render() {
  renderTrack();
  renderTimeline();
  renderEvidence();
  els.play.textContent = state.timer ? "❚❚ Pause" : isFinished() ? "↻ Replay" : "▶ Play";
}

// ---------- playback ----------
function isFinished() {
  const run = currentRun();
  return state.action === run.actions.length - 1 && state.stage >= 4;
}

function waitingOnReviewer() {
  const action = currentRun().actions[state.action];
  return state.stage === 3 && isPendingHold(action) && !state.resolutions.has(state.action);
}

function advance() {
  const run = currentRun();
  state.viewing = null;
  if (waitingOnReviewer()) return false;
  if (state.stage < 4) state.stage += 1;
  else if (state.action < run.actions.length - 1) {
    state.action += 1;
    state.stage = 0;
  } else return false;
  return true;
}

function stop() {
  clearInterval(state.timer);
  state.timer = null;
}

function play() {
  if (state.timer) {
    stop();
    render();
    return;
  }
  if (isFinished()) resetRun();
  const tick = () => {
    const moved = advance();
    if (waitingOnReviewer()) {
      state.pausedForReview = true;
      stop();
    } else if (!moved) stop();
  };
  state.timer = setInterval(() => {
    tick();
    render();
  }, STEP_MS);
  tick();
  render();
}

function resolve(index, decision) {
  state.resolutions.set(index, decision);
  const resume = state.pausedForReview && index === state.action;
  state.pausedForReview = false;
  render();
  if (resume && !isFinished()) setTimeout(play, STEP_MS);
}

function inspect(index) {
  stop();
  state.viewing = stageReached(index) >= 0 ? index : null;
  if (state.viewing === null) {
    // Jump playback forward so the clicked action is fully revealed.
    while (state.action < index || state.stage < 4) {
      if (!advance()) break;
    }
  }
  render();
}

function resetRun() {
  stop();
  state.action = 0;
  state.stage = -1;
  state.viewing = null;
  state.pausedForReview = false;
  state.resolutions = new Map();
}

function selectScenario(i, updateHash) {
  state.scenario = i;
  resetRun();
  renderScenarioList();
  renderHeader();
  render();
  if (updateHash) history.replaceState(null, "", `#${scenarioNumber(currentRun())}`);
}

function scenarioFromHash() {
  const wanted = location.hash.replace("#", "");
  const found = state.runs.findIndex((run) => scenarioNumber(run) === wanted);
  return found >= 0 ? found : state.runs.findIndex((run) => scenarioNumber(run) === "06");
}

els.play.addEventListener("click", play);
els.step.addEventListener("click", () => {
  stop();
  if (isFinished()) resetRun();
  advance();
  render();
});
els.reset.addEventListener("click", () => {
  resetRun();
  render();
});
window.addEventListener("hashchange", () => selectScenario(Math.max(0, scenarioFromHash()), false));

try {
  const response = await fetch("assets/demo-data.json");
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  const data = await response.json();
  state.runs = data.runs;
  selectScenario(Math.max(0, scenarioFromHash()), false);
} catch (error) {
  els.title.textContent = "Could not load the recorded scenarios";
  els.desc.textContent = `assets/demo-data.json failed to load (${error.message}). If you opened this file directly, serve the folder over HTTP instead.`;
}
