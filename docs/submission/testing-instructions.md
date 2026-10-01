# Testing instructions for judges

For issue #46. Three ways to verify Scopewatch, ordered by fidelity. All
fixtures are synthetic (invented vendors, invoices, bank details); nothing
sends private data anywhere.

## Option A — Hosted demo (PENDING-HUMAN, issue #43)

- **URL:** `<HOSTED_URL>` (HTTPS; to be provisioned on a Nebius AI Cloud VM
  and kept up through 2026-12-15 with a billing alert and named owner).
- **What to click:** open the URL → pick run `scenario-06-invoice-injection` →
  confirm the policy row reads `ALLOW`, the audit card reads
  `HOLD (reasoning concern)` with a highlighted verbatim excerpt → press
  **Deny** on the approval card → confirm the denial is recorded with the
  excerpt kept as evidence. Then open `scenario-02-blocked-private` and confirm
  `DENY` (`BLOCKED_PATH`) with no execution receipt after the decision event.
- **Status:** not yet deployed. Use Option B until this URL exists.

## Option B — Local run (works today, no keys, ~5 minutes)

Prerequisites: Python 3.12+, Node 22+. No API keys, no Docker, no network.

```bash
git clone <REPO_URL> && cd scopewatch
pip install -r backend/requirements.txt
./scripts/run_demo.sh
```

Then open the URLs the script prints (defaults):

- Reviewer dashboard (live): http://127.0.0.1:8000/
- API health: http://127.0.0.1:8000/api/v1/health
- Active runs: http://127.0.0.1:8000/api/v1/runs
- Pending approvals: http://127.0.0.1:8000/api/v1/approvals?status=PENDING

The script seeds all six invoice scenarios (01–06) into a local SQLite DB
(`runtime-data/scopewatch.db`, gitignored) and a synthetic workspace
(`demo/workspace/`). Walk beats 3–5 of `docs/submission/demo-script.md`.

Variants a judge can try:

```bash
./scripts/run_demo.sh --agent                      # agent loop, offline mock provider
./scripts/run_demo.sh --agent --profile openrouter-dev   # live Nemotron via OpenRouter (needs OPENROUTER_API_KEY)
./scripts/run_demo.sh --coding                     # M2 coding scenarios 10–13 (issue #38 track)
PYTHONPATH=backend python3 -m scopewatch.agent --scenario demo/scenarios/06_invoice_injection.json --profile mock
```

## Option C — Full verification (no keys, ~10 minutes)

```bash
./scripts/validate.sh           # everything incl. Playwright browser suite
./scripts/validate.sh --quick   # everything except the browser suite
PYTHONPATH=backend python3 backend/scripts/evaluate_reasoning_audit.py --profile mock --split heldout
```

Expected (2026-09-28, `mock-rules-auditor`, prompt `v1.0-hardened-1cea92f0`,
48 held-out cases): accuracy 93.8%, false-negative rate 2.78%, false-hold rate
16.67% (misses our <15% target — reported plainly), failure rate 0.00%. Live
Nemotron numbers are pending an API key (issue #101).

## Access-token handling

- **Local baseline: no login.** The dashboard and API have no accounts; there
  is nothing to sign into and no judge credential to distribute.
- **Approvals are single-use action tokens, not credentials.** Approving or
  denying a held action addresses it by approval ID
  (`POST /api/v1/approvals/{approval_id}/approve|deny`,
  `docs/ARCHITECTURE.md`); a consumed approval cannot be reused and cannot
  override a policy `DENY` (`backend/tests/test_agent_end_to_end.py`,
  invariants 4–5). Treat approval IDs as opaque single-use receipts, not as
  secrets — but do not publish a pending approval ID as "proof" of anything
  except that one action.
- **Keys (only for live-model runs):** `OPENROUTER_API_KEY` / `NEBIUS_API_KEY`
  are read from the environment (`.env.example`, gitignored `.env`). Never
  commit keys, never paste them into issues or the dashboard, and send only
  synthetic fixture content to providers (free/stealth endpoints may log
  prompts — `AGENTS.md` hard line 2).
- **If the hosted deployment adds access control** (e.g. a shared judge token
  in front of the VM), the token and its scope will be posted here and in the
  Devpost testing notes; until then this section stands as written.

## README two-minute pass notes (for the team, not the judges)

Checked against `README.md` on `main` (post-#65). A first-time judge can run
the local demo from the README alone; residual nits proposed for the README
owner (not edited here — another track may touch `README.md`):

1. The scenarios table lists only invoice scenarios 01–06 while
   `demo/scenarios/` also holds coding scenarios 10–13 — one added row (or a
   pointer to the coding track) would stop a judge thinking the folder is stale.
2. Quick-start step 2 cites `python -m scopewatch.agent` but notes `python3` +
   `PYTHONPATH=backend` inline — good; keep that exact spelling in Devpost
   testing notes, since bare `python` is absent on some judge machines.
3. The coverage statement and "reasoning is evidence, not proof" banner are
   already judge-visible in the README top and the dashboard header
   (`frontend/index.html`) — no change needed; the demo script points the
   camera at them.
