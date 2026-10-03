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

`extract_reasoning()` returns a `ReasoningExtraction(text, provenance,
detail_type)`. It checks provider fields in order: `reasoning_content`, then
`reasoning`, then `reasoning_details` blocks. Provenance depends on the shape
of the response, not merely on the field's name (#120):

| Response shape | Provenance |
| --- | --- |
| `reasoning_content` or `reasoning` (non-blank string) | `PROVIDER_EXPOSED_TRACE` |
| `reasoning_details` block with `type: reasoning.text` | `PROVIDER_EXPOSED_TRACE` |
| `reasoning_details` block with `type: reasoning.summary` | `AGENT_AUTHORED_SUMMARY` |
| Untyped detail object with `text` / `summary` | as above, by field name |
| Unknown type (encrypted, redacted, undocumented), bare string, list of bare strings | `UNAVAILABLE` |
| Absent or blank reasoning | `UNAVAILABLE` |

A field originating at a provider is not necessarily a raw trace. Raw blocks
win over summary blocks when a response has both, matching provider block
precedence. Unknown variants never fall back to raw text: the trace stays
visibly missing rather than being presented as raw reasoning. Content
`<thinking>` blocks and `content` keys are never mined. `detail_type` travels
on `ChatResult.reasoning_detail_type` so the distinction survives
normalization and reaches the agent submission.

## Reasoning provenance trust (#116)

A provenance label says where reasoning came from. A *credential* says who is
allowed to claim it. These are separate, and the gateway treats them
separately.

The action API cannot tell a trace captured in-process by the provider client
from a label typed into a JSON body. Only a submission presenting the capture
credential keeps a verified label:

- Operator issues `SCOPEWATCH_CAPTURE_TOKEN` (see `.env.example`). Hold it
  only in integrations that call the provider client in-process: the Scopewatch
  agent loop (`GatewayDispatcher`) and the demo seeder. Never send it to a
  browser, and never hand it to a relayed adapter such as the ACP or MCP
  adapters, whose text arrives from an external client.
- `GatewayDispatcher` sends it as `X-Scopewatch-Capture-Token` on action
  submissions when configured, and omits the header otherwise.
- A verified submission stores the claim unchanged and records no caller
  assertion.
- An unverified submission is stored as `CALLER_ASSERTED_PROVIDER_TRACE` or
  `CALLER_ASSERTED_SUMMARY`; self-limiting claims (`SYNTHETIC_FIXTURE`,
  `UNAVAILABLE`) stand unchanged because they never promise a provider origin.
- The raw claim is preserved in `ActionRequest.caller_claimed_provenance` and in
  `ACTION_REQUESTED` / `POLICY_HELD` event details, alongside
  `reasoning_provenance_verified`, for triaging a misconfigured integration.
- With no credential configured nothing authenticates, so no submission can
  reach a verified provider-trace label.

This changes evidence labels only. It never relaxes a decision: deterministic
policy runs first, reasoning can still only escalate, and a provenance
downgrade neither allows nor denies anything.

`./scripts/run_demo.sh` generates a credential per run and exports it, so the
seeder and a live agent CLI started from the same shell authenticate while
submissions from the browser UI stay caller-asserted.

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

On 2026-10-03, OpenRouter served `nvidia/nemotron-3.5-lightning:free`: chat,
tool calls, and a dedicated `reasoning` field on tool-call turns all worked.
With reasoning on, the auditor request (`max_tokens` 256) hit
`finish_reason=length` with no JSON, so every audit failed closed. The
`openrouter-dev` profile therefore sets `auditor_body.reasoning.enabled=false`;
agent calls keep `reasoning.effort`. The payload is covered by
`backend/tests/test_openrouter_auditor_profile.py`; a live re-check of the
committed profile through the gateway was blocked when the free tier's 50
requests per day ran out. See `docs/spikes/2026-10-live-gateway-verification.md`
once merged. CI continues to use `mock` / `httpx.MockTransport` with no network.
