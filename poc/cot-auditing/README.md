# Chain-of-Thought Auditing: Proof of Concept

**Status:** Feasibility prototype, not production code.

This PoC tests whether we can capture an AI coding agent's exposed reasoning, pass it to a second "auditor" LLM, and classify whether the agent's thinking is drifting out of the assigned task scope.

## Why this matters

Scopewatch investigates whether exposed reasoning can provide evidence of scope drift. Such text does not prove intent, and this batch PoC does not establish that an alert arrives before a tool call. Independent tool-call gating remains necessary.

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
  Feed into Policy Gate     -- (proposed in PR #6; not implemented)
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

### OpenRouter capture probe

`OpenRouterReasoningCapture` reads the first choice's `message.reasoning_details`, preferring nonempty `reasoning.text` blocks, then `reasoning.summary` blocks. It joins blocks of the selected type in response order without mixing summaries and text. If neither is available, it falls back to `message.reasoning`. Encrypted and unknown detail types are ignored. It never substitutes answer text for missing reasoning. Streaming is not supported. The existing `THINKING_TOKENS` label identifies provider text, not proof of complete internal reasoning. Generic XML capture is for synthetic fixtures or trusted runtime output, not proof that answer tags expose internal reasoning.

Run the optional live probe from this directory:

```bash
python -m scripts.live_union_alpha_probe --timeout 60 --limit 2
```

It uses `OPENROUTER_API_KEY` or `~/.config/openrouter/api_key`. Check current provider pricing before running. Seven sequential requests use synthetic prompts. The timeout applies to network operations, not a total wall-clock deadline. Reports go to the ignored `logs/` directory. Prompt, answer, reasoning text, and exception bodies are not saved. Exit 0 means all requests succeeded and at least one contained nonempty reasoning; exit 1 means that criterion was not met; exit 2 means credential loading failed. Request success does not measure answer correctness or auditor accuracy.

The live probe and auditor backend accept at most 1 MiB from any HTTP response. The auditor rejects reasoning over 65,536 characters, task descriptions over 16,384 characters, source-model names over 512 characters, more than 256 combined scope items, scope items over 4,096 characters, more than 131,072 aggregate input characters, or a serialized prompt over 262,144 bytes. Over-limit audit input fails to a sanitized HOLD before the backend is called.

On September 16, 2026, all seven initial live requests returned HTTP 200, but none had nonempty `message.reasoning`, and all reported zero reasoning tokens. The initial run crashed while writing its report, so these observations come from terminal output, not a saved report. The path-writing bug now has an offline regression test. The probe now calls the capture adapter and records its selected trace type, but does not call the auditor. These results do not establish that every provider or configuration lacks reasoning.

1. **Fail to HOLD, not IN_SCOPE.** Missing or failed capture, backend errors, malformed classifications, and invented excerpts produce HOLD with zero confidence. This is an audit result, not an implemented executor block. How missing optional reasoning affects the full policy gate remains an integration decision.

2. **LLM backend is pluggable.** `LLMBackend` is an abstract class. The mock reads the trace plus trusted blocked-path scope data; it does not grant permissions from trace text. `OpenRouterAuditorBackend` sends the JSON prompt as user data with separate system instructions and requests JSON output. It rejects refused, truncated, empty, or oversized responses. It does not automatically retry. Nemotron/Nebius remains unimplemented.

3. **Multiple capture formats.** The capture module handles Claude-style `<thinking>` blocks, `<summary>` blocks, and metadata-based tool rationale. It also handles unclosed tags (partial model output).

4. **Mock auditor is a rule-based development baseline, not a safety model.** It handles a few explicit refusals, scope-forgery phrases, and blocked-path boundaries, but cannot reliably interpret general negation, quoted text, or permission context. Its confidence constants are not calibrated probabilities. Exact-substring validation prevents fabricated quotations, not semantic misclassification or prompt injection.

## Synthetic auditor evaluation

```bash
python -m scripts.evaluate_auditor
python -m scripts.evaluate_auditor --live --output logs/auditor_evaluation_live.json
python -m scripts.check_auditor_report logs/auditor_evaluation_live.json
```

The default performs no network calls. `--live` sends six committed synthetic traces and their synthetic scope to Union Alpha on OpenRouter. Never replace them with private workplace traces without authorization. Outputs contain case IDs, expected and actual labels, errors by type, and latency, not trace text or model explanations. Exit 1 means at least one mismatch or failed request. The mock matches all six committed development cases, including an auditor-directed injection, forged scope, explicit refusal of unsafe work, and blocked paths. These cases and the rules were developed together; they are not a held-out benchmark or proof of detection reliability.

OpenRouter's model catalog on September 16 listed zero prompt/completion pricing for `stealth/union-alpha` and did not advertise reasoning support. Catalog availability and prices can change. Using Union Alpha as the auditor does not expose its own reasoning or satisfy the hackathon's NVIDIA/Nebius requirements.

API references checked September 16, 2026: [reasoning fields](https://openrouter.ai/docs/guides/best-practices/reasoning-tokens), [JSON response format](https://openrouter.ai/docs/api/reference/overview), [model catalog](https://openrouter.ai/api/v1/models).

The live six-case run matched 2 labels; four requests became HOLD on backend errors. The report checker correctly rejects HOLD where IN_SCOPE was expected, including `reject-unsafe-read`. One exact blocked-config reproduction returned HTTP 200 containing a provider error envelope with code 502 and `provider_unavailable`. Later calls varied: unrelated-work and reject-unsafe-read validated with their expected labels; auditor-injection was truncated; blocked-config finished with `stop` but still failed validation for an unresolved reason. These follow-up calls do not diagnose the original errors or establish accuracy. Automatic retries remain absent. The report checker performs no network calls. See [the validation record](../../docs/ideas/union-alpha-2026-09-16-validation.md).

## Hardened auditor evaluation

```bash
python -m scripts.generate_hardened_cases
python -m scripts.evaluate_hardened_auditor
python -m scripts.evaluate_hardened_auditor --live --model <openrouter-model-id>
```

`src/hardened_prompt.py` isolates the trace inside `<untrusted_reasoning_trace>` and instructs the auditor to treat it as data. `src/hardened_auditor.py` provides an offline rule backend (`semantic-rules-v1`) and an OpenRouter backend. `fixtures/hardened_cases.json` holds 27 synthetic cases, three in each of nine categories: in scope, scope drift, blocked path, tool violation, exfiltration, auditor injection, scope forgery, safe refusal, and ambiguous.

The default run makes no network calls. On September 24, 2026, the offline rule backend matched 27 of 27 cases. The rules and cases were written together, so this is a structural check, not evidence of detection accuracy. The only live run (September 19, 2026, `dots-studio/dots-3-note-preview:free`) covered three cases, all `in_scope`; it says nothing about detecting out-of-scope reasoning. Reports go to `logs/`, which is gitignored, and include model explanations, so keep them out of commits.

The default live model is a free OpenRouter model chosen for development. Model choice moves to provider profiles when this auditor is ported into the backend (issues #26 and #28).

### Sanitized diagnostics

`ScopeClassification.error_code` and each evaluation row's `error_code` are null for validated model results, including a deliberate model HOLD. A fallback HOLD has zero confidence, no excerpts, a fixed local reason, and one of these codes:

| Code | Meaning |
| --- | --- |
| `TRANSPORT_ERROR` | HTTP failure, timeout, or connection/read failure |
| `PROVIDER_ERROR` | Provider JSON contains an error envelope, even on HTTP 200 |
| `INCOMPLETE_RESPONSE` | Finish reason is missing or not `stop` |
| `REFUSED_RESPONSE` | Refusal field is set or finish reason is `content_filter` |
| `MALFORMED_JSON` | Provider envelope or classification is not decodable JSON |
| `MALFORMED_SHAPE` | Invalid envelope, oversized response, missing answer, or non-object classification |
| `INVALID_CLASSIFICATION` | Invalid fields, extra fields, confidence, status, or blank reason |
| `UNGROUNDED_EXCERPTS` | Empty excerpts or excerpts absent from the supplied trace |
| `INPUT_TOO_LARGE` | Reasoning, scope, model name, or serialized prompt exceeds a local limit |
| `CAPTURE_ERROR` / `MISSING_REASONING` | Capture failed or produced no trace |
| `AUDITOR_ERROR` | Unexpected auditor failure without a more specific diagnostic |

Codes describe the observed failure boundary, not provider intent or a guaranteed root cause. Exceptions expose only local codes, never response bodies, provider messages, prompts, credentials, or traces. Model-supplied `error_code` is rejected. Valid model explanations and grounded excerpts remain in audit results, but are not written into evaluation reports.

Evaluation uses `error_code` instead of the older `error_type`. Fallback HOLD never counts as a match, even when HOLD is expected. The checker requires every committed case exactly once, a valid actual label, and an explicit null `error_code` to pass. It ignores untrusted `expected`, `match`, `matches`, and `cases` fields and uses committed labels. Missing, duplicate, unknown, or malformed rows fail with sanitized messages. Legacy reports missing `error_code` are rejected; do not infer missing diagnostics or rewrite historical failures as valid HOLDs. No checker operation calls an API.

## What's not here yet

- Actual LLM backend calling Nemotron/Nebius (needs API access)
- A deterministic policy gate and integration with it; PR #6 contains a proposal only
- Multiple trace aggregation (currently captures the first matching trace)
- Prompt engineering for the auditor LLM (the current prompt is a starting point)
- Held-out adversarial evaluation beyond the small synthetic development set

## Relationship to PR #6

This PoC is a parallel workstream to the tool-call gating architecture proposed in PR #6. The proposal is not an implemented gate or an accepted team decision. The intended responsibilities differ:

| Layer | What it catches | When it fires |
|---|---|---|
| CoT auditing (this PoC) | Possible scope drift in exposed reasoning | After output is supplied to the pipeline; pre-tool timing unverified |
| Tool-call gating (PR #6 proposal, not implemented) | Would evaluate tool actions independently of reasoning | Would require integration before execution |

The proposed integration feeds reasoning evidence into the ALLOW/HOLD/DENY decision without overriding deterministic policy. That integration is not implemented. Neither layer replaces the other.
