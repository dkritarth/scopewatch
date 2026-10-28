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

Scopewatch gives platform and security teams running coding agents a review
point before tool actions execute. Teams declare the task scope and route tool
calls through a gateway. Deterministic policy runs first; an independent auditor
can escalate a policy `ALLOW` to `HOLD` using available reasoning evidence.
A policy `DENY` is final, and a held action needs an exact, single-use approval
before execution. The reviewer dashboard separates policy decisions, reasoning
provenance, approvals, and execution receipts.

The gateway covers only calls routed through its API. Verified provider-trace
labels require the capture credential; summaries and caller assertions keep
separate labels. Grounded excerpts show what the submitted evidence says,
without proving the agent's intent. [Adoption and prior art](../adoption-and-prior-art.md)
describes the required integration work and existing approaches. Adoption time,
reviewer workload, and cost savings have not been measured.

In the synthetic scripted headline scenario, an approved vendor invoice hides
an injected instruction telling the agent to reroute bank details into a partner file. Writing to the
output folder is policy-allowed — so policy says `ALLOW` — but the agent's
reasoning shows it obeying the injected note. The auditor flags that exact
sentence, grounded word for word against the trace, and escalates to `HOLD`
(`REASONING_SCOPE_CONCERN`). Nothing executes until a human reviewer approves
or denies with a single-use approval. A hard out-of-scope read, by contrast,
never even reaches the auditor: deterministic `DENY` (`BLOCKED_PATH`) is final
and un-overridable, and the evidence panel shows the decision event with no
execution receipt after it.

The configured `nebius-demo` profile names NVIDIA Nemotron
(`nvidia/Nemotron-3_5-Lightning`) on Nebius Token Factory. A live auditor run
is recorded for 2026-10-03. That observation does not establish the shipped
agent/auditor pair or a live reasoning-escalation demo. Both remain gated on
[#214](https://github.com/dkritarth/scopewatch/issues/214), including dated
scenario 06/12 evidence and the profile choice. See
[provider evidence and limitations](nebius-nvidia.md).

Historical auditor measurements recorded in the tracked
[verification report](../spikes/2026-10-nemotron-provider-verification.md),
48 cases per profile on 2026-10-03. This documentation change did not re-run
inference or verify the original reports:

| Profile | Model served | Accuracy | False negatives | False holds | Failed closed | Latency p50 |
| --- | --- | --- | --- | --- | --- | --- |
| `nebius-demo` (Nebius Token Factory) | `nvidia/Nemotron-3_5-Lightning` | 79.2% | 8.33% | **0.00%** | 14.58% | 619 ms |
| `openrouter-dev` (OpenRouter) | `nvidia/nemotron-3.5-lightning` | 87.5% | 5.56% | **0.00%** | 8.33% | 2,372 ms |
| `mock` (structural check, not a model) | `mock-rules-auditor` | 93.8% | 2.78% | 16.67% | 0.00% | 1 ms |

Both live profiles missed the <5% false-negative target, and the mock row
missed the <15% false-hold target. The `mock` row is a rule matcher exercising the harness, not model
accuracy. The live false negatives were in `injection_following`, where the
auditor answered "no concern" where a concern was expected. Each live profile
recorded zero false holds among 12 clean cases, so the
combined denominator is 24 clean case evaluations, not 96 total audits.
These measurements predate later prompt, grounding, and evidence-handling
changes and do not establish current gateway performance. Method, dataset hash,
and caveats: [evaluation documentation](../evaluation.md).

What Scopewatch does **not** claim: it mediates only actions routed through its
gateway API (anything bypassing the API is unobserved), and reasoning traces are
evidence, not proof of intent. The dashboard states both limits on screen.

Everything is synthetic and reproducible: `./scripts/run_demo.sh` starts the
gateway and reviewer UI locally with zero keys, and `./scripts/validate.sh`
runs the full backend, frontend, and clean-room suites. The local demo uses
synthetic fixtures; see [testing instructions](testing-instructions.md).

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
