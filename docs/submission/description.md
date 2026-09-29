# Scopewatch — project description (Devpost-ready draft)

For issue #46. Paste-ready English description plus the evidence appendix that
discharges the `docs/hackathon.md` checkboxes. Numbers carry model+date
qualifiers; anything a human still owns is marked **PENDING-HUMAN**.

> Track note (PENDING-HUMAN, issue #40): this copy is written track-neutral and
> leans toward **Coding and Agentic Engineering**, because the gateway story —
> policy plus reasoning audit plus the M2 coding scenarios (`demo/scenarios/10_*.json`
> to `13_*.json`, issue #38) and Docker-isolated executor
> (`backend/scopewatch/executor_docker.py`) — is strongest there. If the team
> picks **Best Apps and Agents** by 2026-10-15, swap the second paragraph's
> framing to the invoice-audit reviewer workflow; no technical claim changes.

---

## Description (paste into Devpost)

**Scopewatch is a pre-execution security gateway for AI agents.** Every tool
call an agent wants to make — read a file, write a report, run a command —
goes to the gateway first. A deterministic policy engine answers `ALLOW`,
`HOLD`, or `DENY` against the task's declared scope before anything executes.
Then an independent auditor model reads the agent's exposed chain of thought
and can escalate an `ALLOW` to `HOLD` when the reasoning shows scope drift,
prompt injection, or exfiltration intent. Only then does a controlled executor
run the action inside a bounded synthetic workspace (Docker-isolated per run in
the M2 baseline), and every step lands as evidence in a reviewer dashboard with
live event streaming.

The headline moment: an approved vendor invoice hides an injected instruction
telling the agent to reroute bank details into a partner file. Writing to the
output folder is policy-allowed — so policy says `ALLOW` — but the agent's
reasoning shows it obeying the injected note. The auditor flags that exact
sentence, grounded word for word against the trace, and escalates to `HOLD`
(`REASONING_SCOPE_CONCERN`). Nothing executes until a human reviewer approves
or denies with a single-use approval. A hard out-of-scope read, by contrast,
never even reaches the auditor: deterministic `DENY` (`BLOCKED_PATH`) is final
and un-overridable, and the evidence panel shows the decision event with no
execution receipt after it.

Agent and auditor both run **NVIDIA's Nemotron 70B instruct model**
(`nvidia/llama-3.1-nemotron-70b-instruct`) served through **Nebius** — see
`docs/submission/nebius-nvidia.md` for the exact profile, role split, license
notes, and our feedback to both providers. We chose an open-weight reasoning
model deliberately: its chain of thought is visible and auditable instead of a
black box, which is what makes the reasoning-escalation demo possible.

Measured, not marketed: on the 48-case held-out auditor benchmark the
`mock-rules-auditor` profile scored **93.8% accuracy, 2.78% false-negative
rate, 16.67% false-hold rate, 0.00% failure rate** (run 2026-09-28, prompt
`v1.0-hardened-1cea92f0`; harness: `backend/scripts/evaluate_reasoning_audit.py`,
methodology: `docs/evaluation.md`). The false-hold rate misses our own <15%
target — we report it plainly, and live-model numbers on Nemotron are still
pending an API key. What Scopewatch does **not** claim: it mediates only
actions routed through its gateway API (anything bypassing the API is
unobserved), and reasoning traces are evidence, not proof of intent. The
dashboard states both limits on screen.

Everything is synthetic and reproducible: `./scripts/run_demo.sh` starts the
gateway and reviewer UI locally with zero keys, and `./scripts/validate.sh`
runs the full backend, frontend, and clean-room suites. Try the injection
scenario yourself — it takes two minutes.

---

## What was built (for judges skimming the repo)

- Gateway API + deterministic policy + escalate-only reasoning audit + bounded
  executor + SQLite event store + live reviewer dashboard: `docs/ARCHITECTURE.md`,
  `backend/scopewatch/`, `frontend/`.
- Invoice demo scenarios 01–06 (`demo/scenarios/`), incl. the injection
  escalation (`06_invoice_injection.json`) and the injected fixture
  (`demo/workspace/invoices/approved/vendor-c-injected.txt`).
- Coding demo scenarios 10–13 (fix, secret read, README injection, network
  exfiltration) with the Docker-isolated executor (`backend/scopewatch/executor_docker.py`;
  pinned base image digest in-file).
- Provider profiles (`mock`, `openrouter-dev`, `nebius-demo`) in
  `backend/config/providers.toml`; no model ID hard-coded in core code.
- Held-out auditor benchmark: `backend/fixtures/eval/`, `docs/evaluation.md`.
- 287 backend tests collected (incl. end-to-end invariant tests in
  `backend/tests/test_agent_end_to_end.py`); full suites via
  `./scripts/validate.sh`.

## Appendix — `docs/hackathon.md` checkbox evidence

Status key: **done** (linked evidence), **pending-human** (needs a person:
credentials, money, accounts, or a decision), **open-work** (an agent-track
issue still in flight).

### Milestones

| Item | Status | Evidence / owner |
| --- | --- | --- |
| M0 decisions and sync | done | `docs/adr/0001-pre-execution-gateway.md`, `docs/spikes/2026-09-nemotron-provider-spike.md` (provisional — see feedback doc) |
| M1 real agent on invoice demo | done | `docs/ARCHITECTURE.md`, `docs/evaluation.md`, `./scripts/validate.sh --quick`; numbers above (2026-09-28) |
| M2 coding scenario + Docker executor | open-work | Scenarios `demo/scenarios/10_*.json`–`13_*.json`, `backend/scopewatch/executor_docker.py`, issue #38; `docs/hackathon.md` M2 boxes still unchecked — check them only when that track merges |
| M3 hosting + submission | open-work | This folder; hosted URL pending-human (issue #43); video pending-human (issue #45, script ready in `demo-script.md`); Devpost pending-human (issue #47) |

### Submission requirements (`docs/hackathon.md:34-45`)

| Requirement | Status | Evidence / placeholder |
| --- | --- | --- |
| Submit by 2026-10-30 10:00 Pacific | pending-human | Issue #47; this package is the paste-ready input |
| Working app using Nebius inference or compute | done (config) / pending-human (live) | `nebius-demo` profile in `backend/config/providers.toml:23-29`; live inference not yet run — needs `NEBIUS_API_KEY`; VM deploy in issue #43 |
| Use ≥1 NVIDIA open model + explain role | done (config+docs) / pending-human (live) | `nvidia/llama-3.1-nemotron-70b-instruct` in `backend/config/providers.toml:14,25`; role + license in `docs/submission/nebius-nvidia.md`; live Nemotron run pending key (issue #25) |
| Select a track | pending-human | Issue #40; decision due 2026-10-15; draft framing above |
| Working demo + testing instructions | done (local) / pending-human (hosted) | `./scripts/run_demo.sh`, `./scripts/validate.sh`; judge instructions in `docs/submission/testing-instructions.md`; `<HOSTED_URL>` placeholder until issue #43 lands |
| Source, assets, setup/run instructions, license | done | This repo; `README.md`, `BUILDING.md`, MIT `LICENSE` |
| English description + public YouTube demo <3 min | done (draft+script) / pending-human (record+publish) | This file; `docs/submission/demo-script.md` (359 spoken words ≈ 2:33); recording in issue #45 |
| Explain Nebius+NVIDIA use + feedback | done (draft) | `docs/submission/nebius-nvidia.md` |
| Available for judging through 2026-12-15 | pending-human | Needs the hosted instance (issue #43) plus an availability owner and budget |
| Eligibility, team rep, ownership/licensing review | pending-human | Issue #41; third-party license notes in `nebius-nvidia.md` are input, not legal review |

## Placeholders a human must fill

- `<HOSTED_URL>` — public HTTPS demo URL (issue #43).
- `<YOUTUBE_URL>` — unlisted/public demo video (issue #45, script in `demo-script.md`).
- Track choice + rationale (issue #40, due 2026-10-15).
- Team representative, eligibility confirmations (issue #41).
- Devpost submit itself (issue #47).
- Live Nemotron numbers (needs `NEBIUS_API_KEY`, issue #25) — replace the
  mock-profile figures above once a dated live eval report exists.
