# Agent guide for Scopewatch

This file is the single source of instructions for every coding agent (Claude Code, Codex, Gemini CLI, or anything else) and for humans working like one. `CLAUDE.md` and `GEMINI.md` only point here.

## What we are building

Scopewatch is a pre-execution gateway for AI agents. Every tool call an agent makes goes to the gateway first. A deterministic policy engine decides `ALLOW`, `HOLD`, or `DENY`. An auditor model reads the agent's exposed reasoning and can escalate an allowed action to `HOLD`. Only then does a controlled executor run the action, and every step is recorded as evidence in a reviewer dashboard.

We are a student team building this for the Nebius x NVIDIA Global AI Hackathon. **Submission deadline: October 30, 2026, 10:00 a.m. Pacific.** The submission needs Nebius inference or compute and an NVIDIA open model (Nemotron).

Decisions behind the current plan: [planning record](docs/ideas/claude-opus-5.5-2026-09-23-planning-session.md) and ADR-0001 (issue #21). Architecture: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) (what exists) and [the proposal](docs/architecture/proposed-scopewatch-architecture.md) (where we are going).

## How we work

**Be ambitious.** This is a student hackathon project, not production software. Try things, run long sessions, prototype ideas the backlog did not think of, and fix forward when something breaks. A merged mistake costs a revert, not an outage. If you see a better approach than an issue describes, build it and explain why in the PR.

**Move fast, but stay honest.** The hard lines below are few, and they exist because they protect people or the credibility of the project, not because we are cautious:

1. **No secrets or private data in the repository, issues, PRs, or logs.** Keys come from environment variables or a local, gitignored `.env`. All fixtures are synthetic.
2. **Only synthetic content goes to external model providers.** Free and stealth endpoints may log prompts.
3. **Never push to `main` directly.** Everything lands through a PR (branch protection enforces this).
4. **Report what actually happened.** Say which tests ran, which passed, and what you did not verify. Never delete or weaken a test just to make it pass.
5. **Keep the evidence honest.** Observed actions, provider-exposed reasoning traces, agent-written summaries, and inferred intent are different things. Label them separately. Missing reasoning stays visibly missing. Never claim guaranteed detection or prevention outside the gateway.

Everything else is a default you may break with a stated reason.

## Domain invariants

These are the ideas Scopewatch is about. Code that breaks one is a bug, even if its tests pass. The `reviewer` and `change-monitor` roles check them on every PR.

- **Deterministic policy first.** It runs before any model call and before execution. A policy `DENY` is final.
- **Reasoning can only escalate.** An auditor concern turns `ALLOW` into `HOLD`. Model output never produces `ALLOW` on its own authority, never produces `DENY`, and never relaxes a decision.
- **Fail closed.** An audit that was attempted and failed, timed out, or returned malformed output yields `HOLD`, not `ALLOW`.
- **No decision, no execution.** The executor refuses anything without a stored decision or a valid single-use approval.
- **Approvals are exact and single-use.** They are bound to one action, cannot be reused, and cannot override a policy `DENY`.
- **Evidence before effect.** Decision events are recorded before execution events. The dashboard shows attempted, allowed, held, denied, approved, executed, and failed as distinct states.
- **Models are swappable.** Agent and auditor models come from provider profiles. Core code never hard-codes a model ID.
- **The agent never touches the executor or filesystem directly.** Everything goes through the gateway API.

## Repository map

| Path | What lives there |
| --- | --- |
| `backend/scopewatch/` | FastAPI gateway: schemas, policy engine, executor, SQLite repository, events, service layer |
| `backend/tests/` | Backend pytest suite |
| `frontend/` | Dependency-free HTML, CSS, and JavaScript dashboard, with unit and Playwright tests |
| `demo/` | Synthetic workspaces and scenario files |
| `poc/` | Experiments and prototypes. Each gets its own folder with a README (see [poc/README.md](poc/README.md)) |
| `scripts/` | `run_demo.sh`, `validate.sh`, `seed_demo.py`; `scripts/agents/` holds backlog and worktree helpers |
| `docs/` | Architecture, decisions (`adr/`), ideas, spikes, agent playbook, hackathon checklist ([index](docs/README.md)) |
| `.agents/roles/` | Role specs for agent threads (see below) |
| `.agents/commands/` | Slash commands that launch each role |
| `.agents/skills/` | Shared skills (TDD, debugging, code review, PR writing, and more) |
| `.claude/`, `.codex/`, `.gemini/` | Tool-specific entry points. They symlink to `.agents/` so every tool sees the same roles and skills |

## Agent roles

Spin up focused threads instead of one agent doing everything. Each role has a spec in `.agents/roles/` and a launcher in `.agents/commands/`. The [agent playbook](docs/agents/README.md) explains how to run them in parallel, in loops, and across tools.

| Role | Use it to | Claude Code |
| --- | --- | --- |
| `orchestrator` | Drive a whole milestone with parallel threads | `/run-milestone` |
| `implementer` | Take one issue from claim to merged PR | `/pickup [issue]` |
| `test-writer` | Write failing tests from acceptance criteria or for a module | `/write-tests <target>` |
| `test-runner` | Run every suite, read CI, triage failures | `/run-tests [target]` |
| `verifier` | Run the real app and check behaviour against acceptance criteria | `/verify <target>` |
| `change-monitor` | Watch new commits and open PRs for collisions, invariant breaks, test weakening, secrets, docs drift | `/loop 20m /watch` |
| `reviewer` | Independent second pass on one PR | `/review-pr <n>` |
| `researcher` | Spikes and experiments with written findings | `/spike <issue or question>` |
| `docs-keeper` | Make docs match the code | `/sync-docs` |

In Codex, Gemini CLI, or any other tool: "Act as the `<role>` role. Read `AGENTS.md` and `.agents/roles/<role>.md`, then work on `<target>`."

## Backlog workflow

The backlog lives in GitHub issues, grouped into milestones M0 to M3 plus "Future and stretch". Each issue has an outcome, design guidance, acceptance criteria, validation steps, and native "blocked by" links.

1. **Find work:** `python3 scripts/agents/next_issues.py` lists open `agent: ready` issues whose blockers are closed and that nobody has claimed, in milestone order.
2. **Claim:** comment `Claimed by <tool/model>` and add the `agent: in-progress` label. A claim with no PR after 24 hours may be taken over.
3. **Isolate:** `scripts/agents/worktree.sh <issue> <slug>` creates a separate worktree and branch (`feature/<issue>-<slug>`) from `origin/main`. Parallel threads never share a checkout.
4. **Build and validate:** see "Validation" below. Commit in small steps using conventional messages (`feat:`, `fix:`, `docs:`, `test:`, `chore:`).
5. **PR:** one PR per issue, using the template, with `Closes #<issue>`.
6. **Merge:** squash-merge yourself once CI passes and review conversations are resolved. If the issue is labelled `review: second-pass`, first get an approving review from a separate `reviewer` thread or a human.
7. **Clean up:** remove `agent: in-progress`, delete the worktree, and file follow-up issues for anything you found.

Labels: `agent: ready` (agents may take it), `agent: needs-human` (credentials, money, repository settings, recordings, or submission steps; skip it), `agent: in-progress` (claimed), `review: second-pass` (security-critical; independent review before merge), `status: blocked` (explain why in a comment).

New ideas that are not in the backlog: open an issue with the `agent-task` or `idea` template, or just prototype it in `poc/<name>/` and write it up.

## Validation

```bash
./scripts/validate.sh           # full: compile, backend, PoC, frontend unit, browser, clean-room scenario check
./scripts/validate.sh --quick   # same without the Playwright browser suite
```

Narrower loops while iterating:

```bash
PYTHONPATH=backend python3 -m pytest backend/tests -q
python3 -m pytest poc/cot-auditing -q
npm test --prefix frontend
npm run test:browser --prefix frontend
./scripts/run_demo.sh           # dashboard and API on http://127.0.0.1:8000
```

Setup: Python 3.12 with `pip install -r backend/requirements.txt`, Node 22 or newer with `npm ci --prefix frontend` and `npx --prefix frontend playwright install chromium`. CI runs the same suites in `.github/workflows/`.

In the PR, paste the commands you ran and their results, and list what you could not verify (for example "no Docker on this machine", "no Nebius key", "browser suite skipped").

## Conventions

- **Python:** 3.12, type hints, Pydantic v2 models with `extra="forbid"` for API schemas, FastAPI for HTTP, `httpx` for outbound calls. Errors returned to clients are sanitized; never echo provider response bodies or stack traces.
- **Frontend:** plain ES modules, no build step, no framework. Render untrusted text with `textContent`, never `innerHTML`. Colour is never the only signal.
- **Tests:** pytest for Python, `node --test` for frontend units, Playwright for the browser. No network in tests; use the `mock` provider and `httpx.MockTransport`.
- **Model and provider config:** in provider profiles, never in code.
- **Docs:** describe what exists now. Plans go in issues, ADRs (`docs/adr/`), or `docs/ideas/<model>-<date>-<topic>.md`. Model-authored proposals name the model and date.
- **Commits and PRs:** conventional commit prefixes; PR body explains problem, change, validation, and what remains unverified.
