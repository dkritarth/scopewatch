# M1/M2 completion run — September 26, 2026

**Orchestrator:** Space Bunny Free, ideated and run by Kritarth Dandapat ([@dkritarth](https://github.com/dkritarth)).
**Scope:** Close M1 (Real agent on invoice demo) and M2 (Coding scenario and Docker executor) per the post-M1 audit in `docs/ideas/space-bunny-free-2026-09-25-post-m1-audit-and-m2-plan.md`.
**Source plan:** that document's six-PR sequence (auditor-integrity → gateway-correctness → reviewer-ui-live → truth-pass → m2-workspace+executor → m2-command+scenarios), plus #59 lock files and #24 gap-audit refresh folded into truth-pass.

## Reconciliation (done before this run)

- PR #57 merged Sep 25 as `0f06705`. It closed #21 and #33 (correct) but its `Closes #21, #22, #24 … #34` line only closed #21; #22 was already closed by required-checks work, #23 by #54.
- #24, #26–#32, #34 correctly stayed OPEN: four verifier threads posted per-issue "Verification after PR #57" comments (authored `dkritarth`, signed Space Bunny Free) with criterion-by-criterion evidence and the remaining gaps. No stale open/close to fix.
- #61, #62 (coverage statement, mutation gaps) filed Sep 25 by Space Bunny Free — these ARE the M1 hardening backlog.
- M0 remainder: #24 (agent-ready, docs) + #25 (needs-human spike). M3 + #40–#52 are needs-human / future, out of scope.
- `./scripts/validate.sh --quick` green on `0f06705` at run start (6 clean-room scenarios pass).

## Waves (file-collision aware)

| Wave | Threads (parallel) | Files owned | Issues advanced |
|------|--------------------|-------------|-----------------|
| 1A | auditor+gateway integrity | `backend/scopewatch/reasoning_audit.py`, `service.py`, `schemas.py`, `models.py`, `repository.py`, `db.py`, `backend/tests/test_decision_merge.py`, `test_reasoning_audit.py`, `test_agent_end_to_end.py` | #28, #29 (defects 1, 2, 14, 15, 16, 17, 21) |
| 1B | reviewer-ui-live + coverage | `frontend/*` only | #30, #61, #62 items 5–6 |
| 1C | coding workspace fixture | `demo/coding-workspace/` only (new) | #37 |
| 1D | lock files | `backend/requirements*`, `poc/cot-auditing/requirements*`, `BUILDING.md`, CI install lines | #59 |
| 2 (after 1A) | truth-pass + harness | `docs/*`, `README.md`, `backend/scripts/evaluate_reasoning_audit.py`, `backend/fixtures/eval/`, `scripts/` mutation helper | #24, #34, #32 remainder, #62 items (mutation script, duplicate fixtures) |
| 2 (after 1A/1C) | docker executor | `backend/scopewatch/executor*.py`, `backend/tests/*docker*`, `.github/workflows/docker-executor.yml` | #35 (second-pass: needs reviewer thread before merge) |
| 3 (after docker) | run_command policy | `backend/scopewatch/policy.py`, executor command path, tests | #36 (second-pass) |
| 4 (after run_command + fixture) | coding scenarios + bypass suite | `demo/scenarios/10-13*`, `demo/coding-workspace/` (scenarios only), `docs/security/bypass-tests.md`, agent tools/prompt | #38, #39 (second-pass on #39) |

Order constraints honored: #35 → #36 → (#38, #39); #36 + #37 → #38; #29 → #30 → (#34, #61).

## Working rules for every thread

- Act as the `implementer` role (`.agents/roles/implementer.md`). Follow skills `implement`, `tdd` (red→green, vertical slices, seams at public interfaces), `pr` (Summary/Evidence/Merge-Danger body), `code-review` before opening the PR.
- Claim with `gh issue comment <n> --body "Claimed by Space Bunny Free, ideated and run by Kritarth Dandapat"` + `agent: in-progress` label. Own worktree via `scripts/agents/worktree.sh <n> <slug>`. One PR per issue with `Closes #<n>`.
- Synthetic fixtures only; no keys; `logs/` never committed. Evidence labels kept distinct (observed vs provider-exposed trace vs summary vs inferred). No guaranteed-detection claims.
- Validate with `./scripts/validate.sh --quick` while iterating, full `./scripts/validate.sh` before PR. Paste commands + results in PR, state what stayed unverified (no Docker / no Nebius key / browser suite skipped as applicable).
- `review: second-pass` issues (#29 already merged but touched, #35, #36, #39): request a reviewer pass before merge.
- Never push to `main` directly; squash-merge own PR once CI green + conversations resolved.

## Log

- Sep 26: run opened. Wave 1 (A–D) dispatched in parallel. orchestrator session `t3code/finish-milestone-issues`.
- Sep 26: Wave 1 merged (auditor+gateway integrity, reviewer-ui-live, fixture #37, locks #59). `./scripts/validate.sh --quick` green.
- Sep 26: Wave 2 merged (truth-pass #24/#34/#32/#62-rest, docker executor #35). Quick green.
- Sep 26: Wave 3 merged (run_command #36). Quick green.
- Sep 26: Wave 4 merged (coding scenarios #38, bypass suite #39). Full evidence: backend 259 passed/24 skipped (docker-daemon skips), PoC 96 passed + 79 subtests, frontend unit 27/27, browser 5/5, mutation_check 12/12 caught, fixture exactly-1-failure, `./scripts/validate.sh` full green with clean-room checks, `git status` clean.
- Follow-ups filed: #63 (symlink-to-blocked served), #64 (approve-after-completed executes) — both type:bug, priority:high, review:second-pass, must fix before M3 per #39.
- Live runs NOT done (no Nebius/OpenRouter keys): #32 live held-out, #31/#38 live scenario runs, #25 spike probe. Docker-daemon probes (6+4) skip here; must go green in docker-executor CI.
- M1 remainder after merge: #60 (needs-human decision), live-run portions of #31/#32. M2 remainder: #63/#64 fixes + docker CI green + live runs. M0 remainder: #25 (needs-human).
