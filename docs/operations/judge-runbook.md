# Judge runbook — cold run in ~5 minutes (no keys)

For issue #46. One page a judge follows with zero repo knowledge. All fixtures are synthetic (invented vendors, invoices, bank details). No Docker, no keys, no network. Verified 2026-10-15 on `origin/main` at `e392087` (`./scripts/validate.sh --quick` green, `python3 scripts/check_docs_links.py` PASS).

## 0. Prerequisites (1 min)

Python 3.12+, Node 22+. Check:

```bash
python3 --version   # want 3.12.x
node --version      # want v22+
```

## 1. Clone + install (2 min)

```bash
git clone https://github.com/dkritarth/scopewatch.git && cd scopewatch
python3 -m venv .venv && . .venv/bin/activate && pip install --require-hashes -r backend/requirements.lock
npm ci --prefix frontend
```

The dashboard itself has no runtime npm dependencies, so `npm ci --prefix frontend` is only needed for the frontend tests.

Pinned/reproducible variant (same bytes every machine): `pip install --require-hashes -r backend/requirements.lock`. Env names (only for live-model runs): `SCOPEWATCH_AGENT_PROFILE`, `SCOPEWATCH_AUDITOR_PROFILE`, `OPENROUTER_API_KEY`, `NEBIUS_API_KEY` (`.env.example`, gitignored `.env`). Mock (default) needs none of them.

## 2. Run the demo (1 min)

```bash
./scripts/run_demo.sh
# prints: dashboard http://127.0.0.1:8000/ | health /api/v1/health | runs /api/v1/runs
```

Open `http://127.0.0.1:8000/` → pick run `Reasoning-Injection Escalation` → policy `ALLOW`, audit `HOLD (reasoning concern)` with highlighted excerpt → **Deny** → denial recorded. Then open `Blocked Confidential Directory Access` → `DENY (BLOCKED_PATH)` with no execution receipt after the decision. Variants: `--agent` (offline mock), `--coding` (M2 scenarios 10–13), `PORT=8001 ./scripts/run_demo.sh` (override).

Approvals are single-use and expire after `SCOPEWATCH_APPROVAL_TTL` seconds (default `300`, from `DEFAULT_EXPIRY_SECONDS` in `backend/scopewatch/config.py`). Reviewer access for approving or denying approvals arrives with #258.

## 3. Verify (1 min, optional)

```bash
./scripts/validate.sh --quick   # backend + PoC + frontend unit + clean-room (no browser)
PYTHONPATH=backend python3 backend/scripts/evaluate_reasoning_audit.py --profile mock --split heldout
python3 scripts/check_docs_links.py
```

Expected from the offline run (2026-09-28, `mock-rules-auditor`, prompt `v1.0-hardened-1cea92f0`, 48 held-out cases): accuracy 93.8%, FNR 2.78%, FHR 16.67% (misses our <15% target — reported plainly), failure 0.00%. Rule-matcher structural check, not model accuracy.

Live accuracy evaluations exist for both providers (issue #101, resolved). Held-out split, one pinned run each, 2026-10-03, integrity-checked:

| Profile | Accuracy | FNR | FHR | Failure | Latency p50 |
| --- | --- | --- | --- | --- | --- |
| `openrouter-dev` | 87.5% | 5.56% | **0.00%** | 8.33% | 2,372 ms |
| `nebius-demo` | 79.2% | 8.33% | **0.00%** | 14.58% | 619 ms |

Both miss the <5% FNR target, and both miss it in the same place: `injection_following`, where the auditor returns `NO_CONCERN` where a concern was expected. No clean action was ever held on either profile. Full record, including the measured latency spread and what was *not* verified, is in `docs/spikes/2026-10-nemotron-provider-verification.md`.

## Running a live agent scenario

```bash
set -o allexport; source .env; set +o allexport
export SCOPEWATCH_AGENT_PROFILE=nebius-demo SCOPEWATCH_AUDITOR_PROFILE=nebius-demo
PYTHONPATH=backend python3 -m scopewatch.agent \
  --scenario demo/scenarios/01_safe_audit.json \
  --profile nebius-demo --max-turns 12 --db-path "$(mktemp -d)/run.db"
```

If a run ends `FAILED` with "Wall clock timeout reached", that is the per-run budget, **not** a policy refusal. The budget follows the model: 120s for scripted replay, 600s for a live profile. Override with `--wall-clock <seconds>` or `SCOPEWATCH_AGENT_WALL_CLOCK_S`. Reaching it fails the run — it never reports `COMPLETED`. "Maximum turns limit reached" is the other budget: `--max-turns` (default 20), also reported as `FAILED`, also not a policy outcome.

Two caveats before reading a live run as a result. Live models are non-deterministic, so a single run is an anecdote; per-scenario live evidence (three runs each, both budgets, what was *not* reachable) is recorded in [demo/SCENARIOS.md](../../demo/SCENARIOS.md) for issue #136. And a Nebius-backed run has **no provider-exposed reasoning trace**, because Nebius exposes no reasoning field. If Nemotron writes text into `content` on a tool-call turn, the gateway records it as `AGENT_AUTHORED_SUMMARY` and the auditor reads it; if not, it records `UNAVAILABLE` and skips the audit. Which one you will see has not been observed yet (issue #214). Use `openrouter-dev` if you want a provider trace visible in the evidence panel.

## Scope honesty (read before judging)

Mediates only actions routed through its gateway API — bypassing the API is unobserved (dashboard states this). Reasoning traces are evidence, not proof of intent. Approvals are single-use and cannot override a policy `DENY`. Hosted demo (`<HOSTED_URL>`, issue #43) and video (`<YOUTUBE_URL>`, issue #45) are PENDING-HUMAN; this local run is the complete verifiable baseline.
