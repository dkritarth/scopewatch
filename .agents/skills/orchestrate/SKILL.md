---
name: orchestrate
version: 2.1.0
description: Route T3 Code agent workflows using Kritarth's account priority, model limits, and reasoning preferences. Use for delegation or complex workflow coordination; routine tasks need no Opus consultation.
allowed-tools: [Agent, Read, Grep, Glob, Bash, "mcp__t3_code__*"]
disable-model-invocation: true
---

# Orchestrate for T3 Code

Account-aware T3 delegation replaces PLAN.md's generic model tiers.

## When to use

Apply when orchestrating or delegating work in T3 Code.

## When not to use

Outside T3 Code, follow the current environment's delegation rules.

## Routing policy

Conversation instructions override this policy, including earlier model-specific high-reasoning requests. Propagate overrides to children.

```yaml
routes: # Verified against T3 Code orchestrator_capabilities, 2026-11-24.
  orchestrator:
    providerInstanceId: claudeAgent
    model: claude-opus-5-5
    options: {effort: medium}
  implementation:
    providerInstanceId: codex
    model: gpt-6.2-sol
    options: {reasoningEffort: medium} # High (or higher) is also permitted when useful.
  routine:
    providerInstanceIds: [antigravity, antigravity_2]
    model: gemini-4-hydrogen
    options: {thought_level: high} # Always high.
  research:
    providerInstanceIds: [antigravity, antigravity_2]
    model: gemini-4-hydrogen
    options: {thought_level: high} # Always high.
```

- Opus coordinates complex workflows, instructs workers, and resolves exceptionally difficult questions. Existing Opus parents coordinate directly; other parents delegate one Opus coordinator when needed. Routine work goes straight to workers. The parent model cannot be changed by this skill.
- Sol edits codebases, implements, debugs, and validates. Opus delegates execution to Sol.
- Gemini 4 Hydrogen (high) handles charts, reports, datasets, authorized GitHub updates/pushes, repetitive workflows, monitoring, internet lookup, source verification, and research. Prefer the first available Antigravity instance. Route codebase implementation to Sol.
- Before Gemini 4 Hydrogen (high) monitors HPCC, load `msu-dminer-fleet` and its `references/hpcc.md`; use verified hosts and job IDs.
- Long monitoring (HPCC jobs, gates, arrays) runs in a separate top-level monitor thread, never in the lead thread. The lead commits a brief (jobs, acceptance checks, pre-decided rules, approved submissions, file ownership, lead thread id) and starts the thread with `t3_thread_launch` (Gemini 4 Hydrogen, `thought_level: high`). The monitor thread owns its `schedule_task` bound to itself, messages the lead once with `t3_thread_send` (`mode: "queue"`) when everything ends, and earlier only for blocking events. The lead never messages it; to change the plan, delete its schedule and launch a new thread.
- Only these models are allowed by default. Claude uses only Opus 5.5; OpenCode and other providers require a user override.
- If multiple Codex or Antigravity instances appear in `orchestrator_capabilities`, fall back across available instances on quota/access failure without duplicating active tasks. Report unavailable routes; do not substitute unlisted models.

## Execution

1. Call `orchestrator_capabilities`; verify accounts, availability, models, and options. Set model and effort explicitly (`effort` for Claude, `reasoningEffort` for Codex, `thought_level` for Antigravity).
2. Use T3 `delegate_task` for account-specific GPT and cross-provider work. Supply the goal, paths, edit scope, policy/overrides, deliverables, and checks. Parallelize independent work with separate file ownership; serialize dependencies.
3. Verify artifacts and checks before integrating. For task tracking, review rounds, or missing MCP tools, read [references/t3-protocol.md](references/t3-protocol.md).

Keep task IDs in workflow context; this skill stores no persistent state.
