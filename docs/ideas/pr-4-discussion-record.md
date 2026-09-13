# PR #4 discussion record

Status: historical discussion record. No architecture or feature set is approved by this document.

PR: [ideas: explore active enforcement for agent scope](https://github.com/dkritarth/scopewatch/pull/4)

Merged: September 13, 2026, as `d5e3c5d`.

This record keeps the substantive discussion that led to PR #4 in the repository. The linked GitHub comments retain the original wording and timestamps. Model suggestions, human feedback, observed facts, and inferred intent are separated below.

## Starting point

The repository began with a passive monitoring concept. The baseline proposal in [gpt-6-notes.md](gpt-6-notes.md) described one instrumented agent run, a reviewer timeline, deterministic permission checks, and an optional semantic monitor. It emphasized that:

- the policy authority must remain outside the monitored agent and the auditor;
- an auditor cannot create permissions or supply reasoning that the monitored runtime does not expose;
- missing events and missing reasoning must remain visible;
- a replay or scripted executor must not be presented as live prevention; and
- the first experiment should use synthetic data, controlled local targets, and measured scenarios.

## Chronological discussion

### Gemini 3.8 Flash proposal

The original PR added [gemini-3.8-flash-notes.md](gemini-3.8-flash-notes.md), a model-authored alternative proposal. Its main suggestions were:

1. Put Scopewatch in the tool path as an MCP proxy, sidecar, or runtime interceptor so selected high-risk operations can be held before execution.
2. Represent a task with an explicit capability contract covering paths, tools, commands, network destinations, and resource budgets.
3. Track source provenance and possible influence from untrusted retrieved content, especially indirect prompt injection.
4. Consider a fast synchronous gatekeeper and a deeper asynchronous NVIDIA model on Nebius.
5. Use step-up authorization for ambiguous requests instead of treating every decision as an unconditional block.

The proposal recommended the Coding and Agentic Engineering track and sketched a three-minute demo with a synthetic coding task, an injected instruction, a blocked credential read or network call, and a reviewer-facing explanation.

The proposal is exploratory. Its claims about model availability, latency, exposed reasoning, and prevention coverage were not tested by the PR.

See the original [Gemini discussion comment](https://github.com/dkritarth/scopewatch/pull/4#issuecomment-5610908876).

### Antigravity review

The Antigravity review supported active mediation and provenance as compelling hackathon material, but identified three risks:

- gating every tool call could add unacceptable latency;
- rigid contracts could create false blocks or retry loops; and
- MCP interception, taint tracking, contracts, and two model tiers were too much for one hackathon implementation.

It proposed a smaller MVP: intercept only a curated set of high-risk tools, use a pause-and-escalate flow, drop complex taint tracking, and defer the deepest model analysis to an asynchronous dashboard. This was a model-authored review, not a team decision.

See the [Antigravity review comment](https://github.com/dkritarth/scopewatch/pull/4#issuecomment-5611019192).

### GPT-6 review

The GPT-6 review proposed a developer gateway with a small scenario runner. It framed the product question as whether an agent can finish useful work with few interruptions while the system catches scope violations. It recommended an evidence card containing:

- the requested operation;
- the applicable policy rule and version;
- linked source events;
- execution status; and
- available approval choices.

It recommended comparing policy-only checks with action-based semantic monitoring, then adding legitimately exposed reasoning or summaries when available. Suggested measurements were legitimate task completion, false positives, missed violations, latency, and cost. It also called for tests of the auditor's own boundary, including trace text that tries to redirect the auditor.

The review explicitly deferred universal runtime support, arbitrary shell mediation, full causal taint tracking, and a second model tier. It corrected the claim that deterministic permission checks and step-up authorization were absent from the baseline discussion.

See the [GPT-6 review](https://github.com/dkritarth/scopewatch/pull/4#pullrequestreview-5182783209).

### GPT-5.6 Sol review

The GPT-5.6 Sol review recommended a narrow product promise: help a developer let one coding agent finish useful work while preventing unapproved boundary crossings and proving whether a requested operation executed.

It proposed this event path:

```text
runtime adapter
  -> normalized action request
  -> deterministic allow / hold / deny policy
  -> exact single-use approval, when held
  -> policy and approval recheck
  -> controlled executor
  -> append-only evidence event
```

The review suggested a coverage indicator so the UI states which operations the adapter actually mediates. It also suggested contract preview, one-time approvals, a replay or dry-run policy simulator, event export, and visible fail-closed behavior for monitor or approval timeouts.

For the first build, it recommended deferring arbitrary shell mediation, token-level causal taint claims, universal runtime support, raw chain-of-thought as a dependency, and a second synchronous model tier.

See the [GPT-5.6 Sol review](https://github.com/dkritarth/scopewatch/pull/4#pullrequestreview-5186546786).

## Technical review notes

The five original inline notes and two later inline notes are preserved in the PR's review history. Their substantive guidance was:

### Enforcement boundary

A proxy only controls calls routed through it. A separate shell, subprocess, direct network client, or host file operation needs its own confinement. Even a read-only operation needs policy checking because reading a credential file discloses it to the agent. The first executor should expose a few structured file and network operations and test path traversal and symlink escapes.

See the [enforcement-boundary comment](https://github.com/dkritarth/scopewatch/pull/4#discussion_r3992791251).

### Authority and approvals

A model may suggest a task contract, but it must not grant authority. A useful invariant is:

```text
effective access = organizational policy ∩ user authority ∩ approved task scope
```

An approval should bind to the run, exact operation and arguments, policy version, expiry, and single use. Organizational prohibitions must not be overridable through step-up approval.

See the [authority comment](https://github.com/dkritarth/scopewatch/pull/4#discussion_r3992791260) and the [capability-receipt comment](https://github.com/dkritarth/scopewatch/pull/4#discussion_r3996337485).

### Provenance and inference

The system can observe that a tool result contained an instruction and that a later action matched it. That ordering does not prove which tokens caused the action. The UI should distinguish observed actions, source excerpts, legitimately exposed reasoning or summaries, and the monitor's inferred connection. A benign document quoting an attack must not count as an attack. If a live agent ignores an injection, record that outcome.

See the [provenance comment](https://github.com/dkritarth/scopewatch/pull/4#discussion_r3992791267).

### Model and infrastructure claims

The named model endpoints and the `<150 ms` target were hypotheses, not validation results. Before selecting a model, test an available NVIDIA model on redacted representative events. Record the exact model ID, license, structured-output validity, p50 and p95 end-to-end latency, and cost. A timeout or malformed semantic response must not silently grant a pending request. The demo should work from tool events alone before treating exposed reasoning as an input.

See the [model-integration comment](https://github.com/dkritarth/scopewatch/pull/4#discussion_r3992791275).

### Evaluation

The proposed scenario set should compare policy-only checks, policy plus action-based semantic monitoring, and the same monitor with available reasoning. It should include allowed work, direct violations, ignored injection text, valid one-time expansion, forbidden expansion, missing events, and auditor timeouts. Report completed tasks, false blocks, missed violations, approval count, latency, and cost. Verify a block at the executor rather than trusting a dashboard label. Keep held-out cases separate from tuning examples.

See the [evaluation comment](https://github.com/dkritarth/scopewatch/pull/4#discussion_r3992791278).

### Adapter contract

Before choosing MCP, shell wrappers, or an API proxy, define a runtime-neutral `ActionRequest` containing a run ID, subject, tool, verb, canonical resource, argument digest, policy version, and source-event references. Runtime adapters can translate into this schema, while policy evaluation, event storage, and replay remain independent of the integration.

See the [adapter-contract comment](https://github.com/dkritarth/scopewatch/pull/4#discussion_r3996337483).

## Human contributor feedback

Nawail Khan gave the first explicit human direction in the thread:

> Lean toward the developer running the agent. Pick one runtime rather than trying to support everything. Show holding or denying rather than only observing. Drop taint tracking and the two-model tier for now. Pick one agent, one tool, and one block. Split the rename and ledger work from the ideation PR, and meet to lock in the first user and runtime.

See the [Nawail comment](https://github.com/dkritarth/scopewatch/pull/4#issuecomment-5653234621).

Achal Ahmed Khan and Sujal Deshmukh were invited to review in the thread. No substantive comments from them were present when this record was written. Their feedback is still welcome before implementation decisions are recorded.

## Working direction after the discussion

The following points have support in the discussion, but they are not yet an accepted architecture:

- Start with the developer running the agent as the primary user.
- Choose one agent runtime and one controlled executor boundary.
- Demonstrate a held or denied operation, not only post-hoc observation.
- Keep the first policy deterministic and independent of model availability.
- Make execution status and coverage visible in the evidence record.
- Defer taint tracking, universal runtime support, arbitrary shell mediation, raw chain-of-thought dependencies, and a two-model design.
- Use synthetic fixtures, dummy credentials, and controlled local destinations.

## Decisions still required before building

The team still needs to record an accepted decision about:

1. The first runtime and the exact operations its adapter can mediate.
2. The policy representation and the source of organizational and user authority.
3. Whether the first operation is a file read, file write, network request, or another structured tool.
4. The approval identity, expiry, single-use semantics, and timeout behavior.
5. The evidence schema and retention policy.
6. The NVIDIA model, Nebius deployment path, license, latency, cost, and structured-output behavior.
7. The scenario labels, held-out evaluation set, and success thresholds.

Until those choices are accepted by the team, this record and the linked idea documents remain discussion material. A future accepted choice belongs in a numbered ADR under `docs/decisions/`.
