# Judge runbook — cold run in ~5 minutes (no keys)

For issue #46. One page a judge follows with zero repo knowledge. All fixtures are synthetic (invented vendors, invoices, bank details). No Docker, no keys, no network. Verified 2026-09-28 on `origin/main` (`./scripts/validate.sh --quick` green, `python3 scripts/check_docs_links.py` PASS).

## 0. Prerequisites (1 min)

Python 3.12+, Node 22+. Check:

```bash
python3 --version   # want 3.12.x
node --version      # want v22+
```

## 1. Clone + install (2 min)

```bash
git clone <REPO_URL> && cd scopewatch
pip install -r backend/requirements.txt
npm ci --prefix frontend
```

Pinned/reproducible variant (same bytes every machine): `pip install --require-hashes -r backend/requirements.lock`. Env names (only for live-model runs): `SCOPEWATCH_AGENT_PROFILE`, `SCOPEWATCH_AUDITOR_PROFILE`, `OPENROUTER_API_KEY`, `NEBIUS_API_KEY` (`.env.example`, gitignored `.env`). Mock (default) needs none of them.

## 2. Run the demo (1 min)

```bash
./scripts/run_demo.sh
# prints: dashboard http://127.0.0.1:8000/ | health /api/v1/health | runs /api/v1/runs
```

Open `http://127.0.0.1:8000/` → pick run `scenario-06-invoice-injection` → policy `ALLOW`, audit `HOLD (reasoning concern)` with highlighted excerpt → **Deny** → denial recorded. Then open `scenario-02-blocked-private` → `DENY (BLOCKED_PATH)` with no execution receipt after the decision. Variants: `--agent` (offline mock), `--coding` (M2 scenarios 10–13), `PORT=8001 ./scripts/run_demo.sh` (override).

## 3. Verify (1 min, optional)

```bash
./scripts/validate.sh --quick   # backend + PoC + frontend unit + clean-room (no browser)
PYTHONPATH=backend python3 backend/scripts/evaluate_reasoning_audit.py --profile mock --split heldout
python3 scripts/check_docs_links.py
```

Expected (2026-09-28, `mock-rules-auditor`, prompt `v1.0-hardened-1cea92f0`, 48 held-out cases): accuracy 93.8%, FNR 2.78%, FHR 16.67% (misses our <15% target — reported plainly), failure 0.00%. Live Nemotron numbers pending a key (issue #25).

## Scope honesty (read before judging)

Mediates only actions routed through its gateway API — bypassing the API is unobserved (dashboard states this). Reasoning traces are evidence, not proof of intent. Approvals are single-use and cannot override a policy `DENY`. Hosted demo (`<HOSTED_URL>`, issue #43) and video (`<YOUTUBE_URL>`, issue #45) are PENDING-HUMAN; this local run is the complete verifiable baseline.
