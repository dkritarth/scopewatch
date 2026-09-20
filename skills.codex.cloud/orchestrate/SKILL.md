---
name: orchestrate
version: 1.0.0
description: Routes subagent workflows across provider-neutral reasoning tiers and frontier models from Anthropic (Opus 5, Sonnet 5, Fable 5.1) and OpenAI (GPT-6 Astra, GPT-5.6 Sol, GPT-5.6 Luna). Matches tasks to calibrated reasoning budgets to eliminate token waste and guarantee execution reliability.
allowed-tools: [Agent, Read, Grep, Glob, Bash]
disable-model-invocation: true
---

# Orchestrate

Task decomposition and reasoning-budget routing for multi-agent workflows.

Rather than running every subagent on maximum frontier compute or defaulting blindly to session inheritance, this skill breaks complex tasks into delegable sub-tasks, classifies each by required reasoning depth, and routes to appropriate models with calibrated thinking levels.

## The five reasoning tiers

Classify every delegable unit of work into one tier before spawning an agent:

| Tier | Purpose | Target scope and characteristics | Reasoning depth |
|------|---------|----------------------------------|-----------------|
| 0 | Orchestration & Synthesis | Task decomposition, drafting subagent specs, parallel planning, synthesizing conflicting diffs, final integration. Always runs in the primary session. | Extended / Maximum |
| 1 | Deep Reasoning & Architecture | System boundary design, multi-process concurrency, cryptographic protocols, complex optimization, subtle state-machine bugs, security audits. | Maximum / High |
| 2 | Standard Implementation | Bounded feature development against defined specs, localized refactoring, writing unit and integration tests, code reviews against documented conventions. | Medium / Balanced |
| 3 | Mechanical Execution | Type annotations, lint fixes, renaming symbols, boilerplate replication, applying established patterns across known files. | Low / Fast |
| 4 | Bulk Retrieval & Extraction | Tree grepping, call-graph tracing, structured JSON extraction from documentation, log filtering, wide repository scans. | Minimal / Raw Speed |

## Provider and model routing

### OpenAI mapping

Routes across the GPT-6 (`gpt-6-astra`) and GPT-5.6 (`gpt-5.6-sol`, `gpt-5.6-luna`) generations using calibrated `reasoning_effort`:

| Tier | Model | Reasoning effort | Guidance & operational role |
|------|-------|------------------|-----------------------------|
| 0 | Astra (`gpt-6-astra`) | `xhigh` / `high` | Master orchestrator. Manages overall context, coordinates multi-hour workflows, evaluates subagent outputs, and maintains end-to-end task coherence. |
| 1 | Astra / Sol (`gpt-5.6-sol`) | `high` | Frontier reasoning engine for complex mathematical logic, deep architectural refactors, and elusive race conditions. |
| 2 | Sol (`gpt-5.6-sol`) | `medium` | The core software engineering workhorse. Implements multi-file features, writes regression suites, and refactors components against specifications. |
| 3 | Luna (`gpt-5.6-luna`) | `low` | High-speed, lightweight execution. Applies localized fixes, handles repetitive boilerplate, and transforms established code patterns quickly. |
| 4 | Luna (`gpt-5.6-luna`) | `minimal` | Instantaneous batch processing, fan-out file searching, tabular data extraction, and log parsing without speculative overhead. |

### Anthropic mapping

Routes across Claude 5 models (Fable 5.1, Opus 5, Sonnet 5) with explicit adaptive thinking budgets:

| Tier | Model | Thinking budget | Guidance & operational role |
|------|-------|-----------------|-----------------------------|
| 0 | Fable 5.1 / Opus 5 | Extended (32k+ tokens) | Primary session orchestrator. Directs workflow execution, reconciles disparate subagent findings, and owns top-level architectural integrity. |
| 1 | Opus 5 | Extended (16k to 32k tokens) | Frontier reasoning specialist. Solves root-cause debugging, formal specifications, and security audits before generating edits. |
| 2 | Sonnet 5 | Medium (4k to 8k tokens) | Standard engineering workhorse. Delivers near-frontier code generation and test authoring with fast turnaround and efficient token spend. |
| 3 | Sonnet 5 | Low (1k to 2k tokens) | Quick mechanical refactoring, pattern replication across files, and localized changes against rigid prompts. |
| 4 | Sonnet 5 | Minimal / Off (0 to 1k tokens) | Maximum throughput for tree walks, syntax audits, and rapid summary generation. |

## Dispatch rules

1. **Keep subagent prompts focused**: Never pass the entire workspace context. Provide only the relevant file paths, target interfaces, expected outputs, and explicit verification criteria.
2. **Serial vs parallel fan-out**:
   - Tiers 3 and 4 (Luna, Sonnet 5 low/minimal) support parallel fan-out (such as analyzing separate modules concurrently across multiple workers).
   - Tier 1 (Astra, Opus 5) must run serially when architectural foundations dictate subsequent tasks.
3. **Escalation protocol**:
   - If a Tier 2 task fails two successive test validation passes due to unforeseen design ambiguities, escalate immediately to Tier 1 with full failure logs.
   - If a Tier 3 mechanical edit reveals hidden dependencies or structural variance, promote it to Tier 2 instead of looping.
4. **Maintain provider consistency**: Stick to one provider family within an active task graph to avoid tool signature discrepancies and formatting divergence.
