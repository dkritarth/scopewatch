# Provider profiles (M1: #26, #27, #28)

Single place documenting how the agent loop and the reasoning auditor pick
their models. Config lives in `backend/config/providers.toml` (committed, no
secrets). Core code never hard-codes a model ID — use `profile.model` or
`get_mock_model_name()`.

## Profiles

| Profile | Endpoint | Model | Key env | Use |
| --- | --- | --- | --- | --- |
| `mock` | `mock://localhost` | `mock-rules-auditor` | none | Tests and CI. Deterministic queues, no network. |
| `openrouter-dev` | `https://openrouter.ai/api/v1` | `nvidia/llama-3.1-nemotron-70b-instruct` | `OPENROUTER_API_KEY` | Dev runs via OpenRouter. |
| `nebius-demo` | `https://api.tokenfactory.nebius.com/v1` | `nvidia/llama-3.1-nemotron-70b-instruct` | `NEBIUS_API_KEY` | Demo runs via Nebius Token Factory. Model availability is pending a keyed catalog check. |

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

## Live verification (PENDING, #101)

No live calls were made from machines without keys. Live per-profile calls
(one call per profile, recording date/model/latency) remain PENDING and must
use synthetic content only. CI uses `mock` / `httpx.MockTransport` with no
network.
