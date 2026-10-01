# Provider profiles (M1: #26, #27, #28)

Single place documenting how the agent loop and the reasoning auditor pick
their models. Config lives in `backend/config/providers.toml` (committed, no
secrets). Core code never hard-codes a model ID — use `profile.model` or
`get_mock_model_name()`.

## Profiles

| Profile | Endpoint | Model | Key env | Use |
| --- | --- | --- | --- | --- |
| `mock` | `mock://localhost` | `mock-rules-auditor` | none | Tests and CI. Deterministic queues, no network. |
| `openrouter-dev` | `https://openrouter.ai/api/v1` | `nvidia/nemotron-3.5-lightning` | `OPENROUTER_API_KEY` | Current catalog ID; inference is blocked by a 401 from the supplied key. |
| `nebius-demo` | `https://api.tokenfactory.nebius.com/v1` | `nvidia/Nemotron-3_5-Lightning` | `NEBIUS_API_KEY` | Auditor profile; thinking is disabled so bounded JSON output completes. |

Model IDs appear only in `providers.toml` and tests. `mock-rules-auditor`
is the single mock label resolved via `get_mock_model_name()`.

## Selection

Agent and auditor resolve independently and may differ simultaneously:

```bash
SCOPEWATCH_AGENT_PROFILE=mock          # agent loop (default: mock)
SCOPEWATCH_AUDITOR_PROFILE=nebius-demo # reasoning auditor (default: mock)
SCOPEWATCH_PROVIDERS_CONFIG=/tmp/custom-providers.toml  # tests only
```

`get_agent_profile()` / `get_auditor_profile()` read those vars.
`get_profile(name)` raises `PROFILE_NOT_FOUND` for unknown names.
`get_api_key_for_profile()` raises `MISSING_API_KEY` when the named env var
is unset or blank, and returns `None` when the profile needs no key.

`.env.example` lists all four vars with empty values. Never commit keys.

## Reasoning extraction

`extract_reasoning()` checks provider fields (`reasoning_content`,
`reasoning`, `reasoning_details` text/summary blocks) and sets provenance to
`PROVIDER_EXPOSED_TRACE` only when a provider field supplied raw reasoning.
Absent or blank reasoning -> `UNAVAILABLE`. Content `<thinking>` blocks are
never mined. `ChatResult.model` is the model the provider says it served.

Retries use bounded exponential backoff on 429/5xx only — never on 400.
Errors are sanitized (`ProviderError` carries code + profile + status only).

## Mock backends (structural-only)

`MockProviderClient` (chat) and `MockAuditorProvider` (audit) are
deterministic offline fixtures for unit tests. They verify wiring,
grounding, fail-closed paths, truncation, and timeout handling. They are
structural-only and NOT accurate detectors — never present mock verdicts as
model judgments.

## Auditor hardening (#28)

- `<untrusted_reasoning_trace>` isolation with tag-boundary escaping so a
  trace cannot close its own trust boundary or forge `<task_scope>`.
- Grounded-excerpt validation: every `flagged_excerpts` entry must be an
  exact substring of the (escaped, bounded) trace or the audit is `FAILED`
  with `UNGROUNDED_EXCERPT`.
- Fail closed: exception, timeout, schema violation, or ungrounded excerpt
  -> `FAILED` (never guess). No trace or provider body text in exceptions.
- Trace cap 16,000 chars with explicit truncation marker; recent actions
  capped at 10.

## Live verification (#101)

On 2026-10-01, Nebius served the configured model and completed tool calls.
With thinking enabled, reasoning tokens consumed bounded output budgets but no
non-empty dedicated reasoning field was returned. With
`chat_template_kwargs.enable_thinking=false`, the synthetic auditor probe
returned valid JSON in 84 total tokens (56 prompt, 28 completion) and reported
zero reasoning tokens. The committed `nebius-demo` profile therefore disables
thinking for reliable bounded auditor output.

OpenRouter inference remains unverified because the supplied key returned HTTP
401. CI continues to use `mock` / `httpx.MockTransport` with no network.
