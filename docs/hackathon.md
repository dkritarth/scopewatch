# Hackathon submission checklist

Checked September 28, 2026 against the [official rules](https://nebiusglobalaihackathon.devpost.com/rules). The direct page challenged automated access; its search-indexed rules were readable. Recheck the live rules before submitting (PENDING-HUMAN, issue #41).

Verification for this revision (2026-10-15, `origin/main` at `e392087`): `./scripts/validate.sh --quick` green — backend 1,479 collected (1,454 passed, 25 Docker-executor tests skipped on a machine with no Docker daemon; CI's backend job on the same commit reported 1,479 passed with a daemon), PoC 96 passed, deploy 24 passed, frontend unit, clean-room 6 scenarios with per-run workspace isolation. `python3 scripts/check_docs_links.py` PASS (links, model IDs, URLs, claims, secrets clean, incl. the read-only `deploy/` secret check). README cold-run (seed, agent CLI mock for 01 + 06, eval mock held-out) green. See `docs/operations/judge-runbook.md` for the 1-page cold-run sheet.

Status key: **done** (evidence linked), **open-work** (agent issue in flight), **PENDING-HUMAN** (needs a person: credentials, money, accounts, decision, or recording — owner + date named).

## Milestone progress

- [x] **Milestone M0: Decisions and sync**
  - [x] ADR-0001: Accept the pre-execution gateway with an open-weight reasoning agent (`docs/adr/0001-pre-execution-gateway.md`)
  - [x] Make CI workflows required status checks on main (`.github/branch-protection.json`, `docs/repository-governance.md`)
  - [x] Refresh PR #12 gap audit against current main (`docs/ideas/gpt-5-2026-09-19-gap-audit.md`)
  - [x] Nemotron provider spike on Nebius Token Factory and OpenRouter (`docs/spikes/2026-09-nemotron-provider-spike.md` — provisional, superseded for live facts by `docs/spikes/2026-10-nemotron-provider-verification.md`, which measured both profiles against a working key on 2026-10-03)

- [x] **Milestone M1: Real agent on invoice demo** (verified 2026-09-28 via `./scripts/validate.sh --quick`: 6 invoice scenarios, frontend unit, PoC; re-verified 2026-10-15 at 1,479 backend collected; see `docs/ARCHITECTURE.md` and `docs/evaluation.md`)
  - [x] Provider profile layer (`backend/scopewatch/providers/`, `backend/config/providers.toml`, `.env.example`)
  - [x] Scopewatch agent loop (`backend/scopewatch/agent/` — `loop.py`, `prompt.py`, `tools.py`, `__main__.py`; isolation in `backend/tests/test_agent_loop.py`)
  - [x] Hardened reasoning auditor in backend (`backend/scopewatch/reasoning_audit.py`; auditor class at `reasoning_audit.py:890-1062` on the reviewed revision — line numbers drift, use the class name if the range shifts)
  - [x] Escalate-only decision merge in gateway with per-turn reasoning audit (`backend/scopewatch/service.py:461-484`; `backend/tests/test_agent_end_to_end.py::test_invariant_4_*,test_invariant_5_*`)
  - [x] Reviewer dashboard provenance, audit verdicts, and highlighted trace excerpts (`frontend/scripts/app.js`, `frontend/index.html` safety banner + mediation boundary)
  - [x] Real-agent invoice scenarios + reasoning-injection escalation (`demo/scenarios/06_invoice_injection.json`; 6 invoice scenarios `01`–`06` in `demo/scenarios/`)
  - [x] Held-out evaluation set and harness for reasoning auditor (`backend/fixtures/eval/`, `backend/scripts/evaluate_reasoning_audit.py`, `docs/evaluation.md`; mock held-out 2026-09-28: 93.8% accuracy, FNR 2.78%, FHR 16.67% misses <15% target — structural rule-matcher check, not model accuracy. **Live runs now exist:** one pinned held-out run per profile, 2026-10-03 — `nebius-demo` 79.2% / FNR 8.33% / FHR 0.00%, `openrouter-dev` 87.5% / FNR 5.56% / FHR 0.00%, both `--check` clean, both missing the <5% FNR target with every miss in `injection_following`)
  - [x] End-to-end tests for all 8 invariants (`backend/tests/test_agent_end_to_end.py`)
  - [x] Documentation and architecture guide (`README.md`, `docs/ARCHITECTURE.md`)

- [x] **Milestone M2: Coding scenario and Docker executor** (landed via #65 on `origin/main`; boxes checked here with evidence)
  - [x] Docker container executor per run (`backend/scopewatch/executor_docker.py`: `--network none` at `executor_docker.py:309`, `--read-only` at `executor_docker.py:311`, pinned base digest in-file; `backend/tests/test_executor_docker.py`: 13 passed, 6 skipped 2026-09-28)
  - [x] Command allowlist with `shlex` parsing (`backend/scopewatch/executor_docker.py:16,76-84`; policy `run_command` allowlist in `backend/scopewatch/policy.py:126-362`)
  - [x] Coding benchmark scenarios (`demo/scenarios/10_fix_auth_test.json`, `11_secret_read.json`, `12_readme_injection.json`, `13_network_exfil.json`; fixture `demo/coding-workspace/`; `--coding` path in `scripts/run_demo.sh` + `scripts/seed_demo.py`)

- [ ] **Milestone M3: Hosting and submission** (in flight 2026-10-15)
  - [x] Deployment artifacts — done (issue #42, PR #79): `deploy/` with compose, Caddyfile, edge gate, `RUNBOOK.md`, smoke script, and `deploy/test_compose_layout.py` wired into `./scripts/validate.sh`
  - [ ] Nebius AI Cloud VM provision — PENDING-HUMAN (issue #43, needs a Nebius account and money; target before 2026-10-28). No VM is provisioned, so no hosted URL exists yet and none is claimed in the submission package
  - [ ] 3-minute video walkthrough — script done (`docs/submission/demo-script.md`, 362 spoken words ≈ 2:35 at 140 wpm, under 2:45; corrected by #213 so every spoken claim matches the badge and provenance label on screen); recording PENDING-HUMAN (issue #45)
  - [ ] Devpost submission package — drafts done (`docs/submission/description.md`, `nebius-nvidia.md`, `testing-instructions.md`; rewritten by #215 against current evidence), close-out done (this checklist, README 2-minute pass, `BUILDING.md`, `docs/operations/judge-runbook.md`, `scripts/check_docs_links.py`); submit itself PENDING-HUMAN (issue #47, rep + video + hosted URL, target 2026-10-28, deadline 2026-10-30 10:00 Pacific)

## Submission requirements

- [ ] Submit by October 30, 2026, at 10:00 a.m. Pacific Time. — PENDING-HUMAN (issue #47; owner: team representative TBD, issue #41; target 2026-10-28; screenshot saved privately, not in repo).
- [x] Build a working application using a Nebius Token Factory runtime inference call or Nebius AI Cloud compute — **done (live inference).** Evidence: `nebius-demo` profile in `backend/config/providers.toml`; authenticated Token Factory catalog and a 48-case held-out auditor run on 2026-10-03 (79.2% accuracy, `--check` passed, `docs/evaluation.md`); live agent runs of scenarios 01–05 and 10 (`demo/SCENARIOS.md`); probe and diagnosis chain in `docs/spikes/2026-10-nemotron-provider-verification.md`. The earlier "HTTP 401, live inference not yet run" note was a wrong credential type (an AI Cloud service-account key used against Token Factory), diagnosed and resolved 2026-10-03 — no code change was needed. **VM compute is still PENDING-HUMAN** (issue #43): `deploy/` is on `main`, but no VM is provisioned.
- [x] Use at least one NVIDIA open source model and explain its role — done. `nvidia/Nemotron-3_5-Lightning`, live on Nebius Token Factory; Nebius lists it as the recommended serverless replacement for the Llama and Hermes instruct models withdrawn 2026-08-31. Role and observed limits (no reasoning field; bounded-JSON behaviour) in `docs/submission/nebius-nvidia.md`. **License text is not asserted anywhere in this repo** — a human must confirm it against the model's official page (issue #41).
- [ ] Select a track (Coding and Agentic Engineering versus Best Apps and Agents). — PENDING-HUMAN (issue #40; owners @nawailkhan @atshalahmedkhan; decision due 2026-10-15; draft in PR #72 leans Coding, track-neutral fallback noted).
- [x] Provide a working demo, hosted application, or test build with testing instructions — done (local) / PENDING-HUMAN (hosted). Evidence: `./scripts/run_demo.sh`, `./scripts/validate.sh`, `docs/operations/judge-runbook.md`; judge instructions in PR #72 `docs/submission/testing-instructions.md` (`<HOSTED_URL>` placeholder until issue #43 lands).
- [x] Publish source, assets, setup/run instructions, and open source license. — done. Evidence: this repo; `README.md`, `BUILDING.md`, MIT `LICENSE`.
- [ ] Provide an English description and a public YouTube demonstration under three minutes. — done (draft+script) / PENDING-HUMAN (record+publish). Evidence: `docs/submission/description.md` (live numbers qualified with profile + split + date, rewritten by #215) + `demo-script.md` (362 words ≈ 2:35; spoken claims matched to on-screen badges and provenance labels by #213); recording in issue #45 (`<YOUTUBE_URL>` placeholder).
- [ ] Explain use of Nebius and NVIDIA tools and provide feedback. — done (draft). Evidence: `docs/submission/nebius-nvidia.md` (exact profiles, observed per-provider behaviour, license explicitly unasserted, feedback for both providers).
- [ ] Keep the project available for judging through December 15, 2026. — PENDING-HUMAN (needs hosted instance issue #43 + availability owner + budget + teardown plan in `deploy/RUNBOOK.md` §8).
- [ ] Confirm every member's eligibility, designate the team representative, and review ownership and third-party licensing requirements. — PENDING-HUMAN (issue #41; owners: each member ticks eligibility in a comment; rep TBD; recheck live rules for changes since 2026-09-09).

## Pending-human roster (do not close #46 for these)

| Item | Owner | Date | Issue |
| --- | --- | --- | --- |
| Track choice + rationale | @nawailkhan @atshalahmedkhan | due 2026-10-15 | #40 |
| Eligibility, rep, licensing review | each member + rep TBD | before 2026-10-28 | #41 |
| Provision Nebius VM + hosted URL + billing alert | human with Nebius account | before recording, target 2026-10-28 | #43 |
| Record + publish video | human with the demo running | target 2026-10-28 | #45 |
| Devpost submit | team representative | target 2026-10-28, deadline 2026-10-30 10:00 Pacific | #47 |
| Shipped agent/auditor profile pair; can scenario 06 be filmed live? | human with provider keys | before recording | #214 |
| Nemotron license text + team eligibility | team members + rep | before 2026-10-28 | #41 |
