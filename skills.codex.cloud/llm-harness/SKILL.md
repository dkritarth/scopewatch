---
name: llm-harness
version: 0.2.0
description: Constructs an empirical evaluation suite for LLM components (prompts, tool chains, subagent workflows). Defines measurable assertions, test cases, and scoring metrics to benchmark accuracy.
allowed-tools: [Read, Grep, Glob, Bash, Write, Edit]
disable-model-invocation: true
---

# LLM-Harness

Empirical benchmark and evaluation harness construction for LLM prompts and pipelines.

Replaces vague subjective assessments with reproducible test suites, golden sets, adversarial cases, and quantitative scoring metrics.

## When to use

- Measuring accuracy differences before and after modifying a prompt or system instruction.
- Establishing test fixtures for agents and tool-calling functions.
- Quantifying regression rates across model upgrades.

Do not use if the project already has an existing evaluation suite (such as Promptfoo or LangSmith); extend the existing harness instead.

## Process

### 1. Infer system intent and constraints
Read target prompts, tool definitions, and pipeline code. Document three core parameters:
- **Task specification**: Exact expected output for the step.
- **Variable inputs**: Dynamic variables passed into the prompt across invocations.
- **Failure modes**: Concrete errors to defend against (hallucinated JSON keys, dropped constraints, unhandled edge cases).

### 2. Select grading strategies
Choose evaluation mechanisms based on output types:

| Output format | Verification mode | Evaluation implementation |
|---------------|-------------------|---------------------------|
| Categorical or key-value | Exact or normalized match | String comparison, JSON schema validation |
| Code or query | Deterministic assertion | Syntax parsing, test suite execution, dry-run compile |
| Structured prose | Rubric matching | Criteria checklists, semantic embeddings, bounded assertion tests |

### 3. Generate the test dataset
Build a JSONL or CSV test fixture containing at least:
- 10 standard baseline inputs representing normal operation.
- 5 boundary or edge cases (empty fields, max token lengths, special characters).
- 5 adversarial cases specifically targeting known failure modes.

### 4. Run baseline and report metrics
Execute the harness against current model prompts, record per-case passes and failures, and generate a baseline accuracy summary table before making revisions.
