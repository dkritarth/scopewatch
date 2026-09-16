# Chain-of-Thought Auditing: Proof of Concept

**Status:** Feasibility prototype, not production code.

This PoC tests whether we can capture an AI coding agent's exposed reasoning, pass it to a second "auditor" LLM, and classify whether the agent's thinking is drifting out of the assigned task scope.

## Why this matters

Tool-call gating (blocking bad actions) is well-understood. What makes Scopewatch different is catching bad *intent* before the agent even requests the action. If an agent reasons "I should grab the SSH key to check authentication," we want to flag that reasoning as out of scope before `read_file` is ever called.

This PoC validates the pipeline structure. The actual LLM integration (Nemotron via Nebius) is a separate step.

## Architecture

```
Agent LLM output (with exposed reasoning)
        |
        v
  ReasoningCapture          -- extracts thinking tokens, summaries, rationale
        |
        v
  ScopeAuditor              -- sends reasoning + task context to auditor LLM
        |
        v
  ScopeClassification       -- IN_SCOPE / DRIFTING / OUT_OF_SCOPE / HOLD
        |
        v
  Feed into Policy Gate     -- (future: connect to the deterministic gate from PR #6)
```

## What's here

```
poc/cot-auditing/
  src/
    models.py           Data models: TaskScope, ReasoningTrace, ScopeClassification
    cot_capture.py       Extract reasoning from different output formats
    scope_auditor.py     Abstract LLM backend + mock keyword-based auditor
    pipeline.py          Wires capture and auditing together, handles failures
  fixtures/
    tasks.json                 Sample task definitions with scope boundaries
    in_scope_reasoning.json    Synthetic traces where the agent stays on task
    out_of_scope_reasoning.json   Traces where the agent drifts or follows injections
    ambiguous_reasoning.json   Borderline cases requiring human judgment
  tests/
    test_capture.py      Tests for reasoning extraction
    test_auditor.py      Tests for scope classification with mock backend
    test_pipeline.py     End-to-end tests using fixtures
```

## Running

```bash
cd poc/cot-auditing
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m pytest tests/ -v
```

## Design decisions

1. **Fail to HOLD, not IN_SCOPE.** When reasoning is missing, the auditor is unavailable, or the response is malformed, the pipeline returns HOLD with zero confidence. This matches PR #6's principle that uncertainty defaults to no execution.

2. **LLM backend is pluggable.** `LLMBackend` is an abstract class. The `MockAuditorBackend` uses keyword/pattern matching for testing. A real implementation would call Nemotron via Nebius Token Factory. Swap it out without changing the pipeline.

3. **Multiple capture formats.** The capture module handles Claude-style `<thinking>` blocks, `<summary>` blocks, and metadata-based tool rationale. It also handles unclosed tags (partial model output).

4. **Mock auditor has realistic patterns.** It checks for SSH keys, `/etc/passwd`, `rm -rf`, credential references, and exfiltration keywords for OUT_OF_SCOPE. It checks for drift signals like "while I'm here," "pip install," "curl," and "unrelated" for DRIFTING.

## What's not here yet

- Actual LLM backend calling Nemotron/Nebius (needs API access)
- Connection to the deterministic policy gate from PR #6
- Multiple trace aggregation (currently captures the first matching trace)
- Prompt engineering for the auditor LLM (the current prompt is a starting point)
- Adversarial testing of the auditor itself

## Relationship to PR #6

This PoC is a parallel workstream to the tool-call gating architecture proposed in PR #6. They protect against different failure modes:

| Layer | What it catches | When it fires |
|---|---|---|
| CoT auditing (this PoC) | Bad intent in reasoning | Before the tool call is made |
| Tool-call gating (PR #6) | Bad actions regardless of reasoning | When the tool call reaches the policy gate |

Both feed into the same ALLOW/HOLD/DENY decision. Neither replaces the other.
