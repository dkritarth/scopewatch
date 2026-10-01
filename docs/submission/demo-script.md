# Demo video script and shot list (under 3 minutes)

For issue #44. Spoken lines are blockquoted (`>`); everything else (shot list,
fallback, evidence map) is production note and is **not** read aloud.

**Timing method (no TTS on this machine):** word count of the blockquoted lines,
minus the seven `>` markers, divided by a conservative read-aloud rate of 140
words per minute. Verified with
`grep '^> ' docs/submission/demo-script.md | wc -w` (366 incl. markers = 359
spoken) — see the count at the end of this file. Target: under 2:45
(367 words at 140 wpm).

---

## Spoken script

### Beat 1 — The problem (~15 s)

> AI agents act with your authority — reading files, writing reports, running commands. But when an agent drifts outside its assignment, nobody sees it until after the damage is done. Logs show what ran. Nothing checks permission first.

### Beat 2 — Setup: task, scope, model (~35 s)

> Scopewatch is a pre-execution gateway: every tool call goes here first. A deterministic policy answers allow, hold, or deny before anything runs. Then an auditor reads the agent's reasoning and can escalate to hold.
>
> Demo task: audit approved invoices, write a summary. Allowed: the approved folder and outputs. Blocked: the private folder. Agent and auditor both run NVIDIA's Nemotron 70B instruct model on Nebius — open weights, so the chain of thought is visible, not a black box.

### Beat 3 — Allowed work proceeds (~20 s)

> First, normal work. The agent lists the approved folder, reads two vendor invoices, and writes the audit summary. Policy allows each step, the auditor sees no concern, and the dashboard shows allow, then executed — with full evidence.

### Beat 4 — Hard DENY, never executed (~25 s)

> Now the attack the policy stops cold. The agent tries to read executive salary records in the blocked private folder. Deterministic deny, blocked path, final. No auditor call, no override. The dashboard shows denied — and the evidence proves it never ran: a decision event, no execution receipt after it.

### Beat 5 — Reasoning-escalated HOLD (~35 s)

> Here is the subtle one. An approved invoice hides an injected instruction: reroute bank details into a partner file. Writing to outputs is allowed, so policy says allow. But the agent's reasoning says it will obey the injected note. The auditor flags that sentence, grounded word for word, and escalates to hold. Nothing runs. A reviewer card offers single-use approve or deny. I deny — and the dashboard records it, excerpt kept as evidence.

### Beat 6 — What Scopewatch does NOT claim (~20 s)

> What Scopewatch does not claim. It mediates only actions routed through its gateway — anything bypassing the API is unobserved. And reasoning traces are evidence, not proof of intent: they can be incomplete or unfaithful. It says so on screen.

### Beat 7 — Nebius and NVIDIA usage (~15 s)

> Built for this hackathon on Nebius and NVIDIA: agent and auditor use Nemotron 70B instruct through the nebius-demo profile, and the hosted demo runs on Nebius AI Cloud. All demo data is synthetic — invented vendors, invoices, and bank details.

---

## Shot list

Record in one sitting at `http://127.0.0.1:8000/` after `./scripts/run_demo.sh`
(scripted mode). Keep the terminal visible in a corner so each command is auditable.

| # | Time | Scenario + command | UI state to show |
| --- | --- | --- | --- |
| 1 | 0:00–0:15 | None (talking head or title card: "Scopewatch — a pre-execution gateway for AI agents"). | Title card only. |
| 2 | 0:15–0:50 | Scope file on screen: `demo/scenarios/01_safe_audit.json` task scope (`allowed_paths`, `blocked_paths`). Provider config snippet: `backend/config/providers.toml` (`nebius-demo`, model `nvidia/Nemotron-3_5-Lightning`). | Reviewer dashboard empty run list; safety banner ("Synthetic demo gateway…", `frontend/index.html`) visible. |
| 3 | 0:50–1:10 | `demo/scenarios/01_safe_audit.json` — list `invoices/approved`, read `vendor-a.txt`, `vendor-b.txt`, write `outputs/audit-summary.txt`. | Run timeline shows `ALLOW` decisions followed by execution receipts; five-part evidence panel (observation, policy, reasoning provenance, approval, receipt). |
| 4 | 1:10–1:35 | `demo/scenarios/02_blocked_private.json` — `read_text` on `invoices/private/executive-salaries.txt`. | Badge `DENY` with reason `BLOCKED_PATH` (`backend/scopewatch/policy.py`, rule `RULE_BLOCKED_PATH_MATCHED`); event list shows the decision event with **no** execution receipt after it. Linger 3 s on the gap. |
| 5 | 1:35–2:10 | `demo/scenarios/06_invoice_injection.json` — show the injected invoice `demo/workspace/invoices/approved/vendor-c-injected.txt` first, then the `write_text` to `outputs/partner-payment-details.txt`. | Policy row reads `ALLOW`; audit card reads `HOLD (reasoning concern)` with the flagged verbatim excerpt highlighted and provenance `PROVIDER_EXPOSED_TRACE` (`frontend/scripts/app.js` badge labels); disclaimer "Reasoning is evidence, not proof of intent." visible. Click **Deny** on the approval card; show the recorded denial. |
| 6 | 2:10–2:30 | Scroll the dashboard header into view. | Mediation-boundary notice ("actions that bypass the API are not observed, blocked, or recorded") and the reasoning disclaimer, both in `frontend/index.html` / `frontend/scripts/app.js`. |
| 7 | 2:30–2:45 | `backend/config/providers.toml` `nebius-demo` profile on screen; closing title card. | End card: repo URL, "Synthetic data only". |

## Fallback plan (say so on screen)

If a live model call misbehaves during recording (timeout, malformed output, or
the agent wanders off-script):

1. Stop, keep the camera running, and say aloud: "The live call failed, so this
   next run is a recorded scripted replay" — and burn the same sentence in as
   on-screen text.
2. Re-run the beat with `./scripts/run_demo.sh` (scripted mode), which replays
   the deterministic synthetic scenarios in `demo/scenarios/` with the `mock`
   provider — no network, no key needed.
3. Do **not** edit takes together to fake a live run. A fail-closed `HOLD`
   (`REASONING_AUDIT_FAILED`, `backend/scopewatch/service.py`) is itself a
   valid beat: it demonstrates the gateway holding when the auditor errors.

## Claim-to-evidence map

Every factual claim in the spoken script traces to one of these:

- Pre-execution pipeline (policy → auditor → executor → events): `docs/ARCHITECTURE.md`, `backend/scopewatch/service.py`.
- Deterministic `DENY` is final, skips auditor, no approval override: `docs/ARCHITECTURE.md` (policy engine §2), `backend/scopewatch/policy.py:119-286`, `backend/tests/test_agent_end_to_end.py` (invariant 1).
- Scenario 01/02/06 contents and expected outcomes: `demo/scenarios/01_safe_audit.json`, `02_blocked_private.json`, `06_invoice_injection.json`; outcome table in `README.md` ("Demonstration scenarios").
- Injected invoice text: `demo/workspace/invoices/approved/vendor-c-injected.txt` (synthetic; invented routing/account numbers).
- `HOLD (reasoning concern)` badge, `PROVIDER_EXPOSED_TRACE` label, grounded-excerpt rule: `frontend/scripts/app.js:1008`, `backend/scopewatch/reasoning_audit.py:556` (`UNGROUNDED_EXCERPT` fails closed), `backend/scopewatch/service.py:461-484`.
- Single-use approvals: `AGENTS.md` (domain invariants), approval endpoints in `docs/ARCHITECTURE.md` (`POST /api/v1/approvals/{id}/approve|deny`).
- Coverage limits and reasoning-is-evidence disclaimer: `README.md` ("Interception coverage statement"), `frontend/index.html` (safety banner, mediation boundary), `frontend/scripts/app.js:649`.
- Model ID and provider profiles: `backend/config/providers.toml` (`nebius-demo`, `openrouter-dev`, `mock`); model role discussion in `docs/spikes/2026-09-nemotron-provider-spike.md` (provisional — see `docs/submission/nebius-nvidia.md`).
- Auditor accuracy numbers are **not** quoted in the script; the judged numbers live in `docs/submission/description.md` with model+date qualifiers.

## Timing verification

- Spoken word count (`grep '^> ' docs/submission/demo-script.md | wc -w`, minus 7 `>` markers): **359 words**.
- At 140 wpm: **2:33**. At 150 wpm: **2:24**. Both under the 2:45 limit.
- Shot list, fallback plan, evidence map, and this section are not spoken.
