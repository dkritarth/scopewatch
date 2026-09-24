# Agent playbook

How to spin up agent threads on Scopewatch, run them in parallel, keep them from colliding, and let them run for a long time. Rules and invariants are in [AGENTS.md](../../AGENTS.md); this page is about operating the threads.

## Mental model

- **One thread, one role, one target.** A thread is a single agent session (or subagent) doing one role from [`.agents/roles/`](../../.agents/roles/) on one issue, PR, or area.
- **One implementer thread per worktree.** Parallel implementers never share a checkout. `scripts/agents/worktree.sh` makes a worktree per issue. Read-only roles (reviewer, change-monitor) can share one.
- **GitHub is the shared memory.** Threads coordinate through issue comments, labels, PR reviews, and blocking links, not through chat. A thread that dies leaves its state in GitHub for the next one.
- **An orchestrator is optional.** You can run threads by hand, or start one `orchestrator` thread that launches the others.

## Thread recipes

Each recipe names the role, how to start it in Claude Code, and the equivalent prompt for any other tool.

### Build one issue

```text
Claude Code:  /pickup 26
Any tool:     Act as the implementer role. Read AGENTS.md and .agents/roles/implementer.md, then implement issue #26.
```

Leave the issue number out to take the next ready issue from `scripts/agents/next_issues.py`.

### Write tests first

Start this before or alongside an implementer when an issue has detailed acceptance criteria (for example #29, #33, #36, #39).

```text
Claude Code:  /write-tests 29
Any tool:     Act as the test-writer role (.agents/roles/test-writer.md). Write failing tests for issue #29's acceptance criteria on branch feature/29-<slug>.
```

The implementer then makes those tests pass. For a module rather than an issue, use `/write-tests backend/scopewatch/policy.py`.

### Check that things work

```text
Claude Code:  /verify 57        (PR number)   or   /verify main
Any tool:     Act as the verifier role (.agents/roles/verifier.md) on PR #57.
```

Runs the real app, walks every acceptance criterion, pokes at edge cases, and posts a pass/fail table with screenshots.

### Run and triage the tests

```text
Claude Code:  /run-tests               (origin/main)   or   /run-tests 57
Any tool:     Act as the test-runner role (.agents/roles/test-runner.md) on origin/main.
```

Runs `./scripts/validate.sh`, reads CI, and turns every failure into a fix, a bug issue, or an explained environment problem.

### Monitor other changes

```text
Claude Code:  /loop 20m /watch
Any tool:     Every 20 minutes: act as the change-monitor role (.agents/roles/change-monitor.md) and do one pass.
```

Keep one running whenever two or more PRs are open. It flags PRs that touch the same code, invariant breaks, weakened tests, secrets, and docs drift. It comments; it does not push.

### Review a PR

```text
Claude Code:  /review-pr 57
Any tool:     Act as the reviewer role (.agents/roles/reviewer.md) on PR #57. You did not write it.
```

Required before merging issues labelled `review: second-pass`. Use a different thread (ideally a different model) from the one that wrote the PR.

### Investigate or try an idea

```text
Claude Code:  /spike 25        or   /spike "Can SSE carry reasoning audit progress?"
Any tool:     Act as the researcher role (.agents/roles/researcher.md) on issue #25.
```

### Fix the docs

```text
Claude Code:  /sync-docs
```

### Drive a whole milestone

```text
Claude Code:  /run-milestone M1
Any tool:     Act as the orchestrator role (.agents/roles/orchestrator.md) for milestone "M1: Real agent on invoice demo".
```

## Example: a long M1 session

A good shape for a multi-hour push, with an orchestrator and about five threads live at once:

1. **Orchestrator** runs `next_issues.py`. Suppose #26 (provider profiles) is ready.
2. It starts a **test-writer** and an **implementer** on #26 in the same worktree. The test-writer lands failing tests first, then the implementer makes them pass.
3. It starts a **change-monitor** loop.
4. When #26 merges, #27 (agent loop) and #28 (auditor port) both unblock. They touch different packages, so the orchestrator runs two implementers in parallel worktrees.
5. #29 (decision merge) is `review: second-pass`. After its implementer goes green, the orchestrator starts a **reviewer** (different model) and a **verifier**.
6. A **test-runner** runs on `main` after each merge.
7. At the end of the session the orchestrator writes a run log in `docs/agents/runs/` listing what merged, what broke, and what is next.

## Parallelism rules

- Before starting parallel implementers, check which files each issue will touch. `backend/scopewatch/schemas.py`, `models.py` (enums), and `service.py` are hot spots; serialize issues that change them, or agree in the issue comments which PR adds the shared field first.
- After any merge to `main`, open branches rebase (`git fetch origin && git rebase origin/main`). Resolve conflicts with the `resolving-merge-conflicts` skill.
- If two threads claimed the same issue, the older claim wins; the other thread stops and picks something else.

## Tools and folders

| Tool | Reads instructions from | Roles | Commands | Skills |
| --- | --- | --- | --- | --- |
| Claude Code | `CLAUDE.md` → `AGENTS.md` | `.claude/agents/` (subagents) | `.claude/commands/` | `.claude/skills/` |
| Codex | `AGENTS.md` | `.agents/roles/` (read by prompt) | prompt text above | `.agents/skills/` |
| Gemini CLI | `GEMINI.md` → `AGENTS.md` | `.agents/roles/` (read by prompt) | prompt text above | `.gemini/skills/` |

`.claude/agents`, `.claude/commands`, `.claude/skills`, `.codex/skills`, and `.gemini/skills` are symlinks into `.agents/`. Edit the files in `.agents/` only. On Windows, enable symlinks in Git (`core.symlinks=true`) or read `.agents/` directly.

To add a role: create `.agents/roles/<name>.md` with `name` and `description` front matter, add a launcher in `.agents/commands/<name>.md`, and add a row to the role table in `AGENTS.md`.

## Helpers

| Script | Does |
| --- | --- |
| `scripts/agents/next_issues.py [--all] [--milestone M1] [--json]` | Lists ready, unblocked, unclaimed issues in milestone order |
| `scripts/agents/worktree.sh <issue> <slug> [prefix]` | Creates `../scopewatch-worktrees/<issue>-<slug>` on a new branch from `origin/main` |
| `scripts/validate.sh [--quick]` | Full local validation; `--quick` skips the browser suite |

## Run logs

Long sessions leave a short log in `docs/agents/runs/<yyyy-mm-dd>-<topic>.md`: threads started, PRs merged, failures, decisions made on the fly, and what to pick up next. The next orchestrator reads the latest log first.
