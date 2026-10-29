# Project status and direction as of 2026-10-28

Submission deadline: 2026-10-30, 10:00 Pacific. This page says what the code does now, what is not done or not verified, and where the project goes after the hackathon. The checklist with evidence links is [hackathon.md](hackathon.md).

## What works and is tested

All of this runs offline with the `mock` provider. Tests use synthetic fixtures and mocked transports.

- **Gateway and policy.** `POST /api/v1/runs/{id}/actions` evaluates a deterministic policy (paths, tools, operations, allowlisted `run_command`) before any model call. `DENY` is final. Network operations are always denied. Request bodies are bounded (256 KiB default, `SCOPEWATCH_MAX_BODY_BYTES`). Path matching checks the requested name and the resolved target, and folds case on case-insensitive volumes. Docker copy-back refuses destination directory links.
- **Escalate-only auditor.** A concern or a failed audit turns `ALLOW` into `HOLD`. The model never produces `ALLOW` or `DENY`. Optional two-tier routing (`SCOPEWATCH_AUDITOR_ROUTING=two-tier`) runs a fast screen and a strong review. It has been exercised with synthetic responses only.
- **Approvals.** Exact, single-use, expiring (`SCOPEWATCH_APPROVAL_TTL`), bound to one action, and unable to override `DENY`. A shared dispatch gate refuses execution without a stored decision.
- **Evidence.** Decisions are stored before execution. Provenance labels separate provider-exposed traces, agent-authored summaries, caller assertions, and missing reasoning. An exportable hash chain (`scripts/verify_evidence_chain.py`) detects later edits to exported evidence. It does not prevent unsafe execution.
- **Executors.** In-process workspace executor, plus a Docker executor and a remote runner sidecar for `run_command`. `delete_path` is simulated in the in-process executor.
- **Reviewer dashboard.** Plain HTML/JS, SSE stream, five-part evidence panel, approval expiry display, operator reset and reseed of scenario 06, bundled fonts served by the gateway.
- **Agent loop and scenarios.** Six invoice scenarios (`01` to `06`) and four coding scenarios (`10` to `13`). Mock-agent runs replay scripted actions through the gateway; they test mediation, not model behaviour.
- **Operations.** Workspace retention CLI (`scripts/reap_workspaces.py`, dry run by default), docs checker inside `validate.sh`, injectable clocks in the agent loop.
- **Evaluation tooling.** Held-out auditor harness with split audits and reproducibility checks ([evaluation.md](evaluation.md)), and a monitor ablation replay prototype ([poc/monitor-ablation](../poc/monitor-ablation/README.md)). The ablation has produced no live results.
- **Integrations.** A bounded ACP v1 stdio client (`backend/scopewatch/acp_client.py`, `scripts/run_acp_client.py`) tested against a synthetic peer and a local gateway. An MCP adapter prototype in `poc/mcp-gateway/`.
- **Measured auditor results.** One pinned held-out run per profile on 2026-10-03, auditor only. Both live profiles missed the 5% false-negative target. These numbers do not describe the current gateway; later changes are unmeasured. See [README](../README.md#historical-auditor-results-held-out-split-one-pinned-run-per-profile-2026-10-03).

## Not done or not verified

| Item | State | Issue |
| --- | --- | --- |
| Live Nebius/Nemotron run of the headline scenario, and the shipped agent/auditor profile pair | Not observed. `nebius-demo` exposes no reasoning field, so scenario 06 may `HOLD` on an agent-written summary or stay `ALLOW`. Unknown until run. | #214 |
| Real Codex mediation over ACP | Not demonstrated. The client is tested against a synthetic peer only. Native Codex file, process, and network activity is outside the gateway. | #121 |
| Nebius VM deployment and hosted URL | Not provisioned. Needs a Nebius account and budget. | #43 |
| Demo video | A 2:42 silent, captioned scripted-replay cut is recorded ([MP4](https://dkritarth.github.io/scopewatch/assets/video/scopewatch-walkthrough.mp4), source `site/assets/video/`). It still needs a YouTube or Vimeo upload for Devpost. An optional voiceover can follow the captions. | #45 |
| Devpost submission | Not submitted. Needs the team representative. | #47 |
| Track choice, team eligibility, Nemotron license confirmation | Open, owned by people. No license text is asserted in the repo. | #40, #41 |
| Full suite on the merged tree | Run locally 2026-10-28 on the integrated tree: all suites passed except four root-only `PermissionError` tests that pass as non-root ([hackathon.md](hackathon.md)). No Docker daemon, so 25 Docker tests were skipped. | |
| Native Docker and casefold platforms | Docker tests were skipped and only Linux ext4 case handling was exercised. macOS and Windows volumes are untested. | |
| Two-tier auditor, completion caps, token budgets | Synthetic responses only. Provider enforcement, latency, cost, and detection quality are unmeasured. | |
| Claims of detection or prevention | None made. Scopewatch mediates only actions routed through its API. A concealed or obfuscated plan can pass. | |

## Direction after the hackathon

None of this is built. The target design is in the [proposed architecture](architecture/proposed-scopewatch-architecture.md).

- **Real agent runtimes.** Finish Codex acceptance over ACP (#121) and move the MCP adapter (#51) from prototype to supported.
- **Severity levels.** Show graded severity over reason codes and auditor signals for triage, without changing decisions (#50, prototype in `poc/severity-levels/`).
- **Tamper-evident evidence.** Harden and operate the evidence hash chain (#52, [ADR-0002](adr/0002-evidence-hash-chain.md)).
- **Bounded networking.** Add a separately reviewed, structured network operation. Today all network operations are denied (#306).
- **Measurement.** Run the ablation and a fresh benchmark on unspent cases before making any quality claim.
- **Deferred in the proposal.** Multi-use or time-bound approvals, enterprise identity, production event stores, taint tracking, SIEM routing.
