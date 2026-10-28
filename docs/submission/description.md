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

Agent and auditor both run **NVIDIA's Nemotron 3.5 Lightning**
(`nvidia/Nemotron-3_5-Lightning`) served through **Nebius Token Factory** — see
`docs/submission/nebius-nvidia.md` for the exact profile, role split, license
notes, and our feedback to both providers. We chose an open-weight model
deliberately: when a provider returns a reasoning field, the gateway records it
as provider-exposed evidence instead of taking the model's word for it, and that
is what makes the reasoning-escalation demo possible.

Measured, not marketed. On our 48-case held-out auditor benchmark, one pinned run
per profile on 2026-10-03, every report re-verified by recomputing all metrics
from the per-case rows (`--check`):

| Profile | Model served | Accuracy | False negatives | False holds | Failed closed | Latency p50 |
| --- | --- | --- | --- | --- | --- | --- |
| `nebius-demo` (Nebius Token Factory) | `nvidia/Nemotron-3_5-Lightning` | 79.2% | 8.33% | **0.00%** | 14.58% | 619 ms |
| `openrouter-dev` (OpenRouter) | `nvidia/nemotron-3.5-lightning` | 87.5% | 5.56% | **0.00%** | 8.33% | 2,372 ms |
| `mock` (structural check, not a model) | `mock-rules-auditor` | 93.8% | 2.78% | 16.67% | 0.00% | 1 ms |

Read that table with its targets rather than alone: both live profiles miss our
own <5% false-negative target, and the mock row misses our <15% false-hold
target. The `mock` row is a rule matcher exercising the harness, not model
accuracy. Every live miss sits in the same category — `injection_following`,
where the auditor answered "no concern" where a concern was expected. That is
the only unsafe direction anywhere in the measurement; across 96 held-out audits
no clean action was ever held, and every other failure mode failed *closed* to
a hold. Method, dataset hash, and what we did not verify: `docs/evaluation.md`.

What Scopewatch does **not** claim: it mediates only actions routed through its
gateway API (anything bypassing the API is unobserved), and reasoning traces are
evidence, not proof of intent. The dashboard states both limits on screen.

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
- 1,479 backend tests collected (incl. end-to-end invariant tests in
  `backend/tests/test_agent_end_to_end.py`); 1,454 pass locally and the 25
  Docker-executor tests skip without a daemon, so CI's backend job on `main`
  at `e392087` reported 1,479 passed with a Docker daemon present. Full suites
  via `./scripts/validate.sh`.

## Appendix — `docs/hackathon.md` checkbox evidence

Status key: **done** (linked evidence), **pending-human** (needs a person:
credentials, money, accounts, or a decision), **open-work** (an agent-track
issue still in flight).

### Milestones

| Item | Status | Evidence / owner |
| --- | --- | --- |
| M0 decisions and sync | done | `docs/adr/0001-pre-execution-gateway.md`, `docs/spikes/2026-09-nemotron-provider-spike.md` (provisional — see feedback doc) |
| M1 real agent on invoice demo | done | `docs/ARCHITECTURE.md`, `docs/evaluation.md`; live-model numbers above (held-out split, 2026-10-03) |
| M2 coding scenario + Docker executor | done | Scenarios `demo/scenarios/10_*.json`–`13_*.json`, `backend/scopewatch/executor_docker.py`; landed via #65 and `docs/hackathon.md` M2 boxes checked |
| M3 hosting + submission | open-work | This folder; `deploy/` runbook merged (#42) but the VM itself is pending-human (issue #43); video pending-human (issue #45, script ready in `demo-script.md`); Devpost pending-human (issue #47) |

### Submission requirements (`docs/hackathon.md:34-45`)

| Requirement | Status | Evidence / placeholder |
| --- | --- | --- |
| Submit by 2026-10-30 10:00 Pacific | pending-human | Issue #47; this package is the paste-ready input |
| Working app using Nebius inference or compute | done (live) | `nebius-demo` profile in `backend/config/providers.toml`; live inference verified 2026-10-03 — authenticated catalogue, 48-case held-out auditor run, and live agent runs of scenarios 01–05 and 10 (`demo/SCENARIOS.md`). VM deploy is issue #43 |
| Use ≥1 NVIDIA open model + explain role | done (live Nebius call) | `nvidia/Nemotron-3_5-Lightning` in `backend/config/providers.toml`; observed limits and role in `docs/submission/nebius-nvidia.md` |
| Select a track | pending-human | Issue #40; decision due 2026-10-15; draft framing above |
| Working demo + testing instructions | done (local) / pending-human (hosted) | `./scripts/run_demo.sh`, `./scripts/validate.sh`; judge instructions in `docs/submission/testing-instructions.md`; `<HOSTED_URL>` placeholder until issue #43 lands |
| Source, assets, setup/run instructions, license | done | This repo; `README.md`, `BUILDING.md`, MIT `LICENSE` |
| English description + public YouTube demo <3 min | done (draft+script) / pending-human (record+publish) | This file; `docs/submission/demo-script.md` (362 spoken words ≈ 2:35 at 140 wpm); recording in issue #45 |
| Explain Nebius+NVIDIA use + feedback | done (draft) | `docs/submission/nebius-nvidia.md` |
| Available for judging through 2026-12-15 | pending-human | Needs the hosted instance (issue #43) plus an availability owner and budget |
| Eligibility, team rep, ownership/licensing review | pending-human | Issue #41; third-party license notes in `nebius-nvidia.md` are input, not legal review |

## Placeholders a human must fill

- `<HOSTED_URL>` — public HTTPS demo URL (issue #43).
- `<YOUTUBE_URL>` — unlisted/public demo video (issue #45, script in `demo-script.md`).
- Track choice + rationale (issue #40, due 2026-10-15).
- Team representative, eligibility confirmations (issue #41).
- Devpost submit itself (issue #47).

Live Nemotron numbers are **not** a placeholder: both live profiles were
measured on the held-out split on 2026-10-03 and are quoted above with profile,
split, and date. They were deliberately not re-run afterwards, because held-out
is spent for this submission and re-measuring to chase a better number is what
`docs/evaluation.md` forbids. Two things a human still owns and this package
cannot assert: whether scenario 06 reaches its `HOLD` live (issue #214) and the
Nemotron license text (issue #41).

## Gallery assets

[Current gallery, provenance, architecture, and live-capture checklist](media/README.md).
The current UI PNGs are captioned scripted mock captures, not live inference or
sponsor-use evidence. Live scenario captures and the hero GIF remain pending.
