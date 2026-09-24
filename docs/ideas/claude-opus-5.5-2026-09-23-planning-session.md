# Planning session record, September 23, 2026

**Participants:** @dkritarth (decisions) and Claude Opus 5.5 via Claude Code (questions, recommendations, and this record).
**Status:** Model-authored record awaiting team review. The decisions below were accepted by @dkritarth during the session. They become team decisions only after [ADR-0001 (#21)](https://github.com/dkritarth/scopewatch/issues/21) is merged. @nawailkhan and @atshalahmedkhan are asked to comment on #21 by September 26, 2026.

## Purpose

Turn the repository's open questions into a backlog that coding agents can work through with little supervision: milestones with due dates, issues with acceptance criteria and validation steps, and explicit blocking links.

## Repository state observed at the start

- The local checkout was 7 commits behind `origin/main`. PRs #13–#19 had already merged a FastAPI and SQLite gateway, a 15-step deterministic policy engine, a synthetic in-process workspace executor, single-use approvals, server-sent events, a live dashboard with an action simulator, five invoice-domain scenarios, and `scripts/validate.sh`.
- `ActionRequest` already carried `exposed_reasoning_trace` and a `reasoning_provenance` enum that includes `PROVIDER_EXPOSED_TRACE`.
- There was no real agent loop, no model provider layer, no NVIDIA Nemotron or Nebius integration, no command execution, no container isolation, and no hosting.
- Hardened auditor work (hardened prompt, offline rule backend, 564-line synthetic case file, evaluator, tests) existed only as a local commit on a stale `main`. It was moved to branch `feature/hardened-auditor`, where `python3 -m pytest poc/cot-auditing -q` gave 96 passed and 79 subtests passed. Its only live run covered 3 `in_scope` cases, so it provides no evidence about detection.
- PR #12 (GPT-5 gap audit, September 19) was open and partly outdated by PRs #16–#19.
- Branch protection required PRs but no status checks.

## Decisions

Each entry lists the question, the accepted answer, the reasoning, and the options not chosen.

### Q1. Product shape

**Accepted:** Scopewatch V1 is a pre-execution gateway, as proposed in `docs/architecture/proposed-scopewatch-architecture.md`. Deterministic policy runs first and returns `ALLOW`, `HOLD`, or `DENY`. The reasoning auditor is an evidence source feeding that gateway.

**Why:** Only this framing can demonstrate an action being stopped. Model failure cannot cause unsafe execution, and the NVIDIA model has a visible role.

**Not chosen:** A post-hoc monitor centered on the reasoning auditor. Cheaper, but it cannot claim prevention and makes a weaker demo.

### Q2. Stack

**Accepted:** Python 3.12, FastAPI, and SQLite for the backend. The existing dependency-free vanilla-JS reviewer UI becomes the dashboard. Next.js is dropped.

**Why:** Five weeks remain. The existing UI already covers the timeline, filters, evidence panel, missing-trace labelling, and accessibility smoke tests. One language for all security-critical code is easier to review. PRs #16–#19 had already built this stack.

**Not chosen:** Next.js and TypeScript as proposed in the architecture document.

### Q3. Agent runtime

**Accepted:** Scopewatch runs its own minimal agent loop, with its own system prompt and a tool surface it fully controls. Free and stealth models on OpenRouter are used for development. Support for Codex through the Agent Client Protocol (ACP), using an existing Codex subscription, is a stretch feature.

**Why (user's reasoning):** A deployed system should call models through the API with a purpose-built system prompt, not reuse the system prompts of tools such as Claude Code or t3code. Owning the tool surface also means there is no bypass path by construction.

**Caveats recorded:**

- OpenRouter free and stealth models do not satisfy the hackathon requirement for Nebius inference and an NVIDIA open model. A config-driven OpenAI-compatible client with separate profiles solves this.
- Stealth models appear and disappear, so evaluation results must pin model ID and date.
- Free endpoints may log or train on prompts, so only synthetic data may be sent. This is already a repository rule.
- ACP fits better than MCP because the ACP client provides file and terminal capabilities to the agent. It is still unverified whether `codex-acp` routes all file and shell work through those capabilities. A spike (#48) comes before any build (#49).

**Not chosen for V1:** An MCP server adapter (agents keep their native tools, so mediation is voluntary), or wrapping an existing open source agent (deep integration, inherited tool surface).

### Q4. Model roles

**Accepted:**

- Both the agent model and the auditor model are swappable through provider profiles. Core code never names a specific model.
- The agent must be an open-weight reasoning model whose provider returns raw reasoning. NVIDIA Nemotron is the default.

**Why (user's reasoning):** Open-weight models such as Nemotron expose their reasoning. Closed models such as GPT and Claude expose only summaries, which gives the monitor less to inspect.

**Caveats recorded:**

- Open weights do not guarantee visible reasoning. The provider must return it, and some Nemotron releases need a directive to enable thinking. The provider spike (#25) must verify this on Nebius Token Factory and OpenRouter. It blocks all of M1.
- Visible reasoning can be unfaithful or incomplete. It is evidence, never proof.
- Reasoning is also an injection channel, because the agent reads untrusted content. The hardened prompt's untrusted-trace isolation exists for this reason.
- Closed-model agents remain allowed in summary-only mode, with evidence labelled as agent-authored summaries.
- This reverses the architecture proposal's line "avoid making raw chain-of-thought a dependency" for the agent role. ADR-0001 records the change.

**Not chosen:** Pinning Nemotron in code, or restricting the evaluator to Nemotron only.

### Q5. Authority of reasoning in decisions

**Accepted:** Escalate-only. A reasoning concern can turn a deterministic `ALLOW` into `HOLD`. It can never produce `ALLOW`, never produce `DENY`, and never relax a decision. An attempted audit that fails also yields `HOLD`. Absent reasoning does not escalate and is shown as missing. The audit runs once per agent turn, not once per action, because measured PoC latency was about 7.6 seconds per call.

**Headline demo moment:** The agent reads an allowed file whose content contains an injected instruction. Its reasoning adopts that instruction. The next action is allowed by policy but is held, and the flagged reasoning excerpt is shown as evidence.

**Not chosen:** Display-only reasoning (no runtime effect), or reasoning that can `DENY` (would make model output a hard boundary).

**Future idea from the user (#50):** Graded flag severity levels:

- **Low:** dashboard warning only.
- **Medium:** may hold.
- **High:** automatic block.
- **Extreme:** automatic block.

Session notes on this idea:

- Automatic blocking by model output conflicts with the rule that model output cannot produce `DENY`. It would need corroboration by a deterministic signal, or a new ADR.
- High and Extreme currently have the same effect. One proposal: Extreme also terminates the run and revokes pending approvals.
- Severity would need calibrated evidence from the held-out evaluation first.

### Q6. Executor

**Accepted:** A Docker container per run (`--network none`, non-root, read-only root filesystem, only the run workspace mounted, no host home). The hosted demo runs on one Nebius AI Cloud VM. `run_command` accepts only allowlisted argv prefixes parsed with `shlex`, with no shell metacharacters. The user asked that collaborators be invited to comment on this in the issues.

**Why:** Real isolation. Even if policy failed, the secret file does not exist inside the container. The Nebius VM adds a second Nebius touchpoint.

**Not chosen:** Plain subprocess in a temporary directory (no real isolation), or an unverified Nebius-managed sandbox.

### Q7. Demo domain

**Accepted:** Both, in order. The existing invoice scenarios are connected to the real agent first (M1), giving a submittable demo early. A coding scenario with the Docker executor follows (M2). The track is chosen once M2 status is known, around October 15.

**Not chosen:** Coding only (more model and build risk before a working demo exists), or invoice only (weaker fit with the coding-agent story).

### Q8. Milestones

**Accepted:**

| Milestone | Due | Summary |
| --- | --- | --- |
| M0: Decisions and sync | September 27 | ADR-0001, this record, land hardened auditor, resolve PR #12, required checks, provider spike |
| M1: Real agent on invoice demo | October 9 | Provider profiles, agent loop, per-turn reasoning audit, escalate-only merge, dashboard evidence, held-out evaluation, end-to-end tests |
| M2: Coding scenario and Docker executor | October 20 | Docker executor, `run_command`, coding workspace, coding scenarios, bypass tests |
| M3: Hosting and submission | October 28 | Track decision, Nebius VM deployment, demo video, submission package |
| Future and stretch | none | ACP spike and adapter, severity levels, MCP adapter, tamper-evident evidence |

Two days of buffer remain before the October 30, 10:00 a.m. Pacific deadline. The provider spike is the main risk and blocks all of M1.

### Q9. Agent autonomy

**Accepted:** Agents pick up `agent: ready` issues whose blocking issues are closed, claim them in a comment, work on a topic branch, and open one PR per issue. Agents may self-merge ordinary issues after required checks pass. Issues labelled `review: human-required` (policy, executor, decision merge, approvals, Docker isolation, deployment, and adapters) need a human merge. Issues labelled `agent: needs-human` (decisions, credentials, billing, repository settings, recording, submission) are skipped by agents. Required status checks on `main` (#22) are a prerequisite for self-merge.

**Not chosen:** Human merge for every PR (bottleneck for about 30 PRs), or unconditional self-merge (no enforced gate).

## Backlog created

Milestones 1–5 and issues #20–#52 on GitHub, with native blocking links and the same links written in each issue body. New labels: `agent: ready`, `agent: needs-human`, `review: human-required`, `type: spike`, `type: decision`, `area: agent`, `area: submission`.

## Open items not decided in this session

- Exact Nemotron model IDs, endpoints, and reasoning parameters (#25).
- Whether PR #12 is refreshed or closed (#24).
- Track (#40), due by October 15.
- Approach for starting executor containers from the hosted gateway without exposing the Docker socket (#42).
