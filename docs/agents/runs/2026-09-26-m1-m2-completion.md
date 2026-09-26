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
