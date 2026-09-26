# Hackathon submission checklist

Checked September 9, 2026 against the [official rules](https://nebiusglobalaihackathon.devpost.com/rules). The direct page challenged automated access; its search-indexed rules were readable. Recheck the live rules before submitting.

## Milestone progress

- [x] **Milestone M0: Decisions and sync**
  - [x] ADR-0001: Accept the pre-execution gateway with an open-weight reasoning agent (`docs/adr/0001-pre-execution-gateway.md`)
  - [x] Make CI workflows required status checks on main (`.github/branch-protection.json`, `docs/repository-governance.md`)
  - [x] Refresh PR #12 gap audit against current main (`docs/ideas/gpt-5-2026-09-19-gap-audit.md`)
  - [x] Nemotron provider spike on Nebius Token Factory and OpenRouter (`docs/spikes/2026-09-nemotron-provider-spike.md` — provisional/unverified, see defect 12 note)

- [x] **Milestone M1: Real agent on invoice demo** (verified via `./scripts/validate.sh --quick`: 148 backend, 27 frontend unit, 6 scenarios; see `docs/ARCHITECTURE.md` and `docs/evaluation.md`)
  - [x] Provider profile layer (`backend/scopewatch/providers/`, `backend/config/providers.toml:5-29`, `.env.example`)
  - [x] Scopewatch agent loop (`backend/scopewatch/agent/` - `loop.py`, `prompt.py`, `tools.py`, `__main__.py`; isolation in `backend/tests/test_agent_loop.py`)
  - [x] Port hardened reasoning auditor into backend (`backend/scopewatch/reasoning_audit.py:890-1062`)
  - [x] Escalate-only decision merge in gateway with per-turn reasoning audit (`backend/scopewatch/service.py:461-484`; `backend/tests/test_agent_end_to_end.py::test_invariant_4_*,test_invariant_5_*`)
  - [x] Reviewer dashboard provenance, audit verdicts, and highlighted trace excerpts (`frontend/scripts/app.js`, `frontend/styles/reviewer.css`, `frontend/index.html:40-42`)
  - [x] Real-agent invoice scenarios + reasoning-injection escalation (`demo/scenarios/06_invoice_injection.json`; 6 scenarios in `demo/scenarios/`)
  - [x] Held-out evaluation set and harness for reasoning auditor (`backend/fixtures/eval/`, `backend/scripts/evaluate_reasoning_audit.py:217-308`, `docs/evaluation.md:42-72`; mock held-out 93.8% accuracy, FHR 16.67% misses <15% target — live run needs key)
  - [x] End-to-end tests for all 8 invariants (`backend/tests/test_agent_end_to_end.py`)
  - [x] Updated documentation and architecture guide (`README.md`, `docs/ARCHITECTURE.md:48-63`)

- [ ] **Milestone M2: Coding scenario and Docker executor**
  - [ ] Docker container executor per run (`--network none`, read-only root)
  - [ ] Command allowlist with `shlex` parsing
  - [ ] Coding benchmark scenario

- [ ] **Milestone M3: Hosting and submission**
  - [ ] Nebius AI Cloud VM deployment
  - [ ] 3-minute video walkthrough
  - [ ] Devpost submission package

## Submission requirements

- [ ] Submit by October 30, 2026, at 10:00 a.m. Pacific Time.
- [x] Build a working application using a Nebius Token Factory runtime inference call or Nebius AI Cloud compute (profile `nebius-demo` configured in `backend/config/providers.toml:23-29`; live inference call not yet run — needs `NEBIUS_API_KEY`, tracked in #32/#31).
- [x] Use at least one NVIDIA open source model and explain its role (`nvidia/llama-3.1-nemotron-70b-instruct` for agent reasoning trace and audit; see `docs/spikes/2026-09-nemotron-provider-spike.md` (provisional) and `backend/config/providers.toml:14,25`).
- [ ] Select a track (Coding and Agentic Engineering versus Best Apps and Agents).
- [x] Provide a working demo, hosted application, or test build with testing instructions (`./scripts/run_demo.sh`, `./scripts/validate.sh`).
- [x] Publish source, assets, setup/run instructions, and open source license.
- [ ] Provide an English description and a public YouTube demonstration under three minutes.
- [ ] Explain use of Nebius and NVIDIA tools and provide feedback.
- [ ] Keep the project available for judging through December 15, 2026.
- [ ] Confirm every member's eligibility, designate the team representative, and review ownership and third-party licensing requirements.
