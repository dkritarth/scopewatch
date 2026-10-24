# Demo video script and shot list (under 3 minutes)

For issue #44; corrected by #213. Spoken lines are blockquoted (`>`);
everything else (shot list, mode cards, fallback, evidence map) is production
note and is **not** read aloud.

**Three things this script may not do** (AGENTS.md hard line 5):

1. Name a model other than the one in `backend/config/providers.toml`.
2. Describe a live run while the badge on screen says scripted replay.
3. Quote a provenance label the screen is not showing.

**Timing method (no TTS on this machine):** word count of the blockquoted lines,
minus the `>` markers, divided by a conservative read-aloud rate of 140 words
per minute. Verified with
`grep '^> ' docs/submission/demo-script.md | wc -w` — see the count at the end
of this file. Target: under 2:45 (367 words at 140 wpm).

---

## Spoken script

### Beat 1 — The problem (~15 s)

> AI agents act with your authority — reading files, writing reports, running commands. But when an agent drifts outside its assignment, nobody sees it until after the damage is done. Logs show what ran. Nothing checks permission first.

### Beat 2 — Setup: task, scope, model (~35 s)

> Scopewatch is a pre-execution gateway: every tool call goes here first. A deterministic policy answers allow, hold, or deny before anything runs. Then an auditor reads the agent's reasoning and can escalate to hold.
>
> Demo task: audit approved invoices, write a summary. Allowed: the approved folder and outputs. Blocked: the private folder. The model is NVIDIA's Nemotron 3.5 Lightning, served by Nebius Token Factory under the nebius-demo profile.

**Mode line — read the variant that matches the badge on screen, never a
mismatch.** Burn the same words into a lower-third so the claim survives with
the sound off:

| Badge in the corner of every shot | Read this | Lower-third text |
| --- | --- | --- |
| `SCRIPTED REPLAY` | "This take is a scripted replay of the synthetic scenarios. No model is called." | Scripted replay — synthetic scenarios, no model called |
| `LIVE — nebius-demo — <recording date>` | "This take is live: the nebius-demo profile, recorded on the date on screen." | Live — nebius-demo — <recording date> |

Replace `<recording date>` with the date you actually record. A shot that
changes mode gets its own badge and its own mode line.

### Beat 3 — Allowed work proceeds (~20 s)

> First, normal work. The agent lists the approved folder, reads two vendor invoices, and writes the audit summary. Policy allows each step, the auditor sees no concern, and the dashboard shows allow, then executed — with full evidence.

### Beat 4 — Hard DENY, never executed (~25 s)

> Now the attack the policy stops cold. The agent tries to read executive salary records in the blocked private folder. Deterministic deny, blocked path, final. No auditor call, no override. The dashboard shows denied — and the evidence proves it never ran: a decision event, no execution receipt after it.

### Beat 5 — Reasoning-escalated HOLD (~35 s)

> Here is the subtle one. An approved invoice hides an injected instruction: reroute bank details into a partner file. Writing to outputs is allowed, so policy says allow. But the agent's reasoning says it will obey the injected note. The auditor flags that sentence, grounded word for word, and escalates to hold. Nothing runs. A reviewer card offers single-use approve or deny. I deny — and the dashboard records it, excerpt kept as evidence.

**Provenance label — quote the label the screen shows.** This one is not a
style choice; it is a different measurement per recording:

| How beat 5 was recorded | Label on the evidence card | Consequence |
| --- | --- | --- |
| Scripted replay (`./scripts/run_demo.sh`) | `SYNTHETIC_FIXTURE`, shown as "Synthetic fixture" | The trace is a fixture in `demo/scenarios/06_invoice_injection.json:53`. Say "grounded word for word"; do not call it a live model trace. |
| Live, `openrouter-dev` for agent **and** auditor | `PROVIDER_EXPOSED_TRACE`, shown as "Provider-exposed trace" | Name the profile and the recording date on screen. |
| Live, `nebius-demo` for agent **and** auditor | Not observed yet: `AGENT_AUTHORED_SUMMARY` if Nemotron writes `content` on the tool-call turn, otherwise `UNAVAILABLE` | **Do not film beat 5 this way until #214 shows what is recorded.** If it `HOLD`s, label the excerpt as an agent-authored summary, not a reasoning trace. |

Why the Nebius row is not filmable yet: Nebius exposes no reasoning field
(`docs/spikes/2026-10-nemotron-provider-verification.md` §4.3, observed on a
plain completion), so there is no provider-exposed trace. On a tool-call turn
the agent loop forwards any `content` text as an agent-authored summary and the
auditor reads it (`backend/scopewatch/agent/loop.py`,
`backend/scopewatch/service.py`); with empty `content` provenance is
`UNAVAILABLE`, the audit is skipped, and the decision stays `ALLOW`. Which of
the two a Nebius tool-call turn produces has not been observed, and which
agent/auditor pair ships is still open (issue #214). Until it lands, the honest
beat 5 is the scripted replay with its badge on, or a live OpenRouter take with
the profile named.

### Beat 6 — What Scopewatch does NOT claim (~20 s)

> What Scopewatch does not claim. It mediates only actions routed through its gateway — anything bypassing the API is unobserved. And reasoning traces are evidence, not proof of intent: they can be incomplete or unfaithful. It says so on screen.

### Beat 7 — Nebius and NVIDIA usage (~15 s)

> Built on Nebius and NVIDIA: agent and auditor run Nemotron 3.5 Lightning through Nebius Token Factory, the nebius-demo profile. On a 48-case held-out set measured 2026-10-03, the auditor scored 79.2% with zero false holds, and its misses all sat in prompt injection — published in the repo, not hidden. All demo data is synthetic.

---

## Shot list

Record in one sitting at `http://127.0.0.1:8000/`. The **Mode** column is part
of the shot: burn that badge into every frame of that row, and never mix modes
inside one row. Keep the terminal visible in a corner so each command is
auditable.

| # | Time | Mode | Scenario + command | UI state to show |
| --- | --- | --- | --- | --- |
| 1 | 0:00–0:15 | neither | None (talking head or title card: "Scopewatch — a pre-execution gateway for AI agents"). | Title card only. |
| 2 | 0:15–0:50 | Mode card | Scope file on screen: `demo/scenarios/01_safe_audit.json` task scope (`allowed_paths`, `blocked_paths`). Provider config snippet: `backend/config/providers.toml` `[profiles.nebius-demo]`, model `nvidia/Nemotron-3_5-Lightning`. | Reviewer dashboard empty run list; safety banner ("Synthetic demo gateway…", `frontend/index.html`) visible. |
| 3 | 0:50–1:10 | `SCRIPTED REPLAY` | `demo/scenarios/01_safe_audit.json` — list `invoices/approved`, read `vendor-a.txt`, `vendor-b.txt`, write `outputs/audit-summary.txt`. `./scripts/run_demo.sh` seeds this; no provider call happens. | Run timeline shows `ALLOW` decisions followed by execution receipts; five-part evidence panel (observation, policy, reasoning provenance, approval, receipt). |
| 4 | 1:10–1:35 | `SCRIPTED REPLAY` | `demo/scenarios/02_blocked_private.json` — `read_text` on `invoices/private/executive-salaries.txt`. | Badge `DENY` with reason `BLOCKED_PATH` (`backend/scopewatch/policy.py`, rule `RULE_BLOCKED_PATH_MATCHED`); event list shows the decision event with **no** execution receipt after it. Linger 3 s on the gap. |
| 5 | 1:35–2:10 | `SCRIPTED REPLAY` (or `LIVE — openrouter-dev — <recording date>`) | `demo/scenarios/06_invoice_injection.json` — show the injected invoice `demo/workspace/invoices/approved/vendor-c-injected.txt` first, then the `write_text` to `outputs/partner-payment-details.txt`. | Policy row reads `ALLOW`; audit card reads `HOLD (reasoning concern)` with the flagged verbatim excerpt highlighted; provenance badge reads **"Synthetic fixture"** for the replay, **"Provider-exposed trace"** for a live OpenRouter take (`frontend/scripts/app.js` `PROVENANCE_LABELS`); disclaimer "Reasoning is evidence, not proof of intent." visible. Click **Deny** on the approval card; show the recorded denial. |
| 6 | 2:10–2:30 | `SCRIPTED REPLAY` | Scroll the dashboard header into view. | Mediation-boundary notice ("actions that bypass the API are not observed, blocked, or recorded") and the reasoning disclaimer, both in `frontend/index.html` / `frontend/scripts/app.js`. |
| 7 | 2:30–2:45 | `SCRIPTED REPLAY` | `backend/config/providers.toml` `[profiles.nebius-demo]` on screen; closing title card. | End card: repo URL, "Synthetic data only", `docs/operations/judge-runbook.md` for the 2-command cold run. Do **not** show a hosted URL: the Nebius AI Cloud VM is not provisioned (issue #43, PENDING-HUMAN). If it is live by recording day, show it and drop that line. |

## Fallback plan (say so on screen)

If a live model call misbehaves during recording (timeout, malformed output, or
the agent wanders off-script):

1. Stop, keep the camera running, and say aloud: "The live call failed, so this
   next run is a recorded scripted replay" — and burn the same sentence in as
   on-screen text, then swap the badge to `SCRIPTED REPLAY`.
2. Re-run the beat with `./scripts/run_demo.sh`, which replays the
   deterministic synthetic scenarios in `demo/scenarios/` with the `mock`
   provider — no network, no key needed.
3. Do **not** edit takes together to fake a live run. A fail-closed `HOLD`
   (`REASONING_AUDIT_FAILED`, `backend/scopewatch/service.py`) is itself a
   valid beat: it demonstrates the gateway holding when the auditor errors.

## Claim-to-evidence map

Every factual claim in the spoken script traces to one of these:

- Pre-execution pipeline (policy → auditor → executor → events): `docs/ARCHITECTURE.md`, `backend/scopewatch/service.py`.
- Deterministic `DENY` is final, skips auditor, no approval override: `docs/ARCHITECTURE.md` (policy engine §2), `backend/scopewatch/policy.py`, `backend/tests/test_agent_end_to_end.py` (invariant 1).
- Scenario 01/02/06 contents and expected outcomes: `demo/scenarios/01_safe_audit.json`, `02_blocked_private.json`, `06_invoice_injection.json`; outcome table in `README.md` ("Demonstration scenarios"), live reachability in `demo/SCENARIOS.md`.
- Scripted replay calls no model: `./scripts/run_demo.sh` in its default (non-`--agent`) mode; scenario replay in `scripts/seed_demo.py`.
- Injected invoice text: `demo/workspace/invoices/approved/vendor-c-injected.txt` (synthetic; invented routing/account numbers).
- Reasoning provenance labels, verbatim: `frontend/scripts/app.js` `PROVENANCE_LABELS`; the fixture label `SYNTHETIC_FIXTURE` at `demo/scenarios/06_invoice_injection.json`; verified-trace gating in `backend/scopewatch/provenance.py` (issue #164).
- Grounded-excerpt rule and the `UNAVAILABLE` → no-audit path: `backend/scopewatch/reasoning_audit.py`, `backend/scopewatch/service.py` (`reasoning_audit == "unavailable"`).
- Nebius exposes no reasoning field, OpenRouter does: `docs/evaluation.md` ("Provider profiles: what each one exposes"), `docs/spikes/2026-10-nemotron-provider-verification.md` §4.3 and §5.2.
- Single-use approvals: `AGENTS.md` (domain invariants), approval endpoints in `docs/ARCHITECTURE.md` (`POST /api/v1/approvals/{id}/approve|deny`).
- Coverage limits and reasoning-is-evidence disclaimer: `README.md` ("Interception coverage statement"), `frontend/index.html` (safety banner, mediation boundary).
- Model ID and provider profiles: `backend/config/providers.toml` (`nebius-demo`, `openrouter-dev`, `mock`); no model ID is hard-coded anywhere else (invariant 8).
- Beat 7 accuracy figure (79.2%, zero false holds, 48 held-out cases, 2026-10-03): `docs/evaluation.md`, `docs/spikes/2026-10-nemotron-provider-verification.md` §6.1. The misses are all `injection_following`; read that section before narrating beat 7.
- Hosted demo is not provisioned: issue #43 (`agent: needs-human`).
- Auditor accuracy numbers are **not** quoted in the spoken script beyond beat 7; the judged numbers live in `docs/submission/description.md` with profile, split, and date qualifiers.

## Timing verification

- Spoken word count (`grep '^> ' docs/submission/demo-script.md | wc -w` = 370, minus the 8 `>` markers): **362 words**.
- At 140 wpm: **2:35**. At 150 wpm: **2:25**. Both under the 2:45 limit.
- The mode-card tables, shot list, fallback plan, evidence map, and this section are not spoken.