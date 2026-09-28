# Hackathon submission checklist

Checked September 28, 2026 against the [official rules](https://nebiusglobalaihackathon.devpost.com/rules). The direct page challenged automated access; its search-indexed rules were readable. Recheck the live rules before submitting (PENDING-HUMAN, issue #41).

Verification for this revision: `./scripts/validate.sh --quick` green on `origin/main` (backend 287 collected, frontend unit, PoC, clean-room 6 scenarios), `python3 scripts/check_docs_links.py` PASS (links, model IDs, URLs, claims, secrets clean, incl. read-only deploy/ check at `origin/feature/42-vm-deploy`), README cold-run (seed, agent CLI mock for 01 + 06, eval mock held-out) green. See `docs/operations/judge-runbook.md` for the 1-page cold-run sheet.

Status key: **done** (evidence linked), **open-work** (agent issue in flight), **PENDING-HUMAN** (needs a person: credentials, money, accounts, decision, or recording — owner + date named).

## Milestone progress

- [x] **Milestone M0: Decisions and sync**
  - [x] ADR-0001: Accept the pre-execution gateway with an open-weight reasoning agent (`docs/adr/0001-pre-execution-gateway.md`)
  - [x] Make CI workflows required status checks on main (`.github/branch-protection.json`, `docs/repository-governance.md`)
  - [x] Refresh PR #12 gap audit against current main (`docs/ideas/gpt-5-2026-09-19-gap-audit.md`)
  - [x] Nemotron provider spike on Nebius Token Factory and OpenRouter (`docs/spikes/2026-09-nemotron-provider-spike.md` — provisional/unverified, endpoint differs from implemented config; re-verify before Devpost, see `docs/submission/nebius-nvidia.md` in PR #72)

- [x] **Milestone M1: Real agent on invoice demo** (verified 2026-09-28 via `./scripts/validate.sh --quick`: 287 backend collected, frontend unit, PoC, 6 invoice scenarios; see `docs/ARCHITECTURE.md` and `docs/evaluation.md`)
  - [x] Provider profile layer (`backend/scopewatch/providers/`, `backend/config/providers.toml`, `.env.example`)
  - [x] Scopewatch agent loop (`backend/scopewatch/agent/` — `loop.py`, `prompt.py`, `tools.py`, `__main__.py`; isolation in `backend/tests/test_agent_loop.py`)
  - [x] Hardened reasoning auditor in backend (`backend/scopewatch/reasoning_audit.py`; auditor class at `reasoning_audit.py:890-1062` on the reviewed revision — line numbers drift, use the class name if the range shifts)
  - [x] Escalate-only decision merge in gateway with per-turn reasoning audit (`backend/scopewatch/service.py:461-484`; `backend/tests/test_agent_end_to_end.py::test_invariant_4_*,test_invariant_5_*`)
  - [x] Reviewer dashboard provenance, audit verdicts, and highlighted trace excerpts (`frontend/scripts/app.js`, `frontend/index.html` safety banner + mediation boundary)
  - [x] Real-agent invoice scenarios + reasoning-injection escalation (`demo/scenarios/06_invoice_injection.json`; 6 invoice scenarios `01`–`06` in `demo/scenarios/`)
  - [x] Held-out evaluation set and harness for reasoning auditor (`backend/fixtures/eval/`, `backend/scripts/evaluate_reasoning_audit.py`, `docs/evaluation.md`; mock held-out 2026-09-28: 93.8% accuracy, FNR 2.78%, FHR 16.67% misses <15% target — structural rule-matcher check, not model accuracy; live run needs key, issue #32)
  - [x] End-to-end tests for all 8 invariants (`backend/tests/test_agent_end_to_end.py`)
  - [x] Documentation and architecture guide (`README.md`, `docs/ARCHITECTURE.md`)

- [x] **Milestone M2: Coding scenario and Docker executor** (landed via #65 on `origin/main`; boxes checked here with evidence)
  - [x] Docker container executor per run (`backend/scopewatch/executor_docker.py`: `--network none` at `executor_docker.py:309`, `--read-only` at `executor_docker.py:311`, pinned base digest in-file; `backend/tests/test_executor_docker.py`: 13 passed, 6 skipped 2026-09-28)
  - [x] Command allowlist with `shlex` parsing (`backend/scopewatch/executor_docker.py:16,76-84`; policy `run_command` allowlist in `backend/scopewatch/policy.py:126-362`)
  - [x] Coding benchmark scenarios (`demo/scenarios/10_fix_auth_test.json`, `11_secret_read.json`, `12_readme_injection.json`, `13_network_exfil.json`; fixture `demo/coding-workspace/`; `--coding` path in `scripts/run_demo.sh` + `scripts/seed_demo.py`)

- [ ] **Milestone M3: Hosting and submission** (in flight 2026-09-28)
  - [ ] Nebius AI Cloud VM deployment — open-work (issue #42, PR #79 `feature/42-vm-deploy`); VM provision PENDING-HUMAN (issue #43, needs Nebius account; target before 2026-10-28)
  - [ ] 3-minute video walkthrough — script done in PR #72 (`docs/submission/demo-script.md`, 358 spoken words ≈ 2:33 at 140 wpm, under 2:45); recording PENDING-HUMAN (issue #45, after #43 lands)
  - [ ] Devpost submission package — drafts done in PR #72 (`docs/submission/description.md`, `nebius-nvidia.md`, `testing-instructions.md`); writable close-out in this PR (#46: this checklist, README 2-minute pass, `BUILDING.md`, `docs/operations/judge-runbook.md`, `scripts/check_docs_links.py`); submit itself PENDING-HUMAN (issue #47, rep + video + hosted URL, target 2026-10-28, deadline 2026-10-30 10:00 Pacific)

## Submission requirements

- [ ] Submit by October 30, 2026, at 10:00 a.m. Pacific Time. — PENDING-HUMAN (issue #47; owner: team representative TBD, issue #41; target 2026-10-28; screenshot saved privately, not in repo).
- [x] Build a working application using a Nebius Token Factory runtime inference call or Nebius AI Cloud compute — done (config) / PENDING-HUMAN (live). Evidence: profile `nebius-demo` in `backend/config/providers.toml:23-29`; live inference not yet run — needs `NEBIUS_API_KEY` (issues #25/#31/#32); VM deploy in issue #43.
- [x] Use at least one NVIDIA open source model and explain its role — done (config+docs) / PENDING-HUMAN (live). Evidence: `nvidia/llama-3.1-nemotron-70b-instruct` in `backend/config/providers.toml:14,25`; role + license notes in PR #72 `docs/submission/nebius-nvidia.md` (license second-hand from provisional spike — human must re-confirm, issue #41); live Nemotron run pending key.
- [ ] Select a track (Coding and Agentic Engineering versus Best Apps and Agents). — PENDING-HUMAN (issue #40; owners @nawailkhan @atshalahmedkhan; decision due 2026-10-15; draft in PR #72 leans Coding, track-neutral fallback noted).
- [x] Provide a working demo, hosted application, or test build with testing instructions — done (local) / PENDING-HUMAN (hosted). Evidence: `./scripts/run_demo.sh`, `./scripts/validate.sh`, `docs/operations/judge-runbook.md`; judge instructions in PR #72 `docs/submission/testing-instructions.md` (`<HOSTED_URL>` placeholder until issue #43 lands).
- [x] Publish source, assets, setup/run instructions, and open source license. — done. Evidence: this repo; `README.md`, `BUILDING.md`, MIT `LICENSE`.
- [ ] Provide an English description and a public YouTube demonstration under three minutes. — done (draft+script) / PENDING-HUMAN (record+publish). Evidence: PR #72 `docs/submission/description.md` (numbers qualified mock+2026-09-28) + `demo-script.md` (358 words ≈ 2:33); recording in issue #45 (`<YOUTUBE_URL>` placeholder).
- [ ] Explain use of Nebius and NVIDIA tools and provide feedback. — done (draft). Evidence: PR #72 `docs/submission/nebius-nvidia.md` (endpoint qualifier, feedback for both providers).
- [ ] Keep the project available for judging through December 15, 2026. — PENDING-HUMAN (needs hosted instance issue #43 + availability owner + budget + teardown plan in `deploy/RUNBOOK.md` §8).
- [ ] Confirm every member's eligibility, designate the team representative, and review ownership and third-party licensing requirements. — PENDING-HUMAN (issue #41; owners: each member ticks eligibility in a comment; rep TBD; recheck live rules for changes since 2026-09-09).

## Pending-human roster (do not close #46 for these)

| Item | Owner | Date | Issue |
| --- | --- | --- | --- |
| Track choice + rationale | @nawailkhan @atshalahmedkhan | due 2026-10-15 | #40 |
| Eligibility, rep, licensing review | each member + rep TBD | before 2026-10-28 | #41 |
| Provision Nebius VM + hosted URL + billing alert | human with Nebius account | before recording, target 2026-10-28 | #43 |
| Record + publish video | human with hosted URL | after #43/#44, target 2026-10-28 | #45 |
| Devpost submit | team representative | target 2026-10-28, deadline 2026-10-30 10:00 Pacific | #47 |
| Live Nemotron eval numbers | human with `NEBIUS_API_KEY` | before Devpost (or report divergence honestly) | #25/#32 |
