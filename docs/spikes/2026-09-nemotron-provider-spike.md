# Nemotron Provider Spike: Nebius Token Factory and OpenRouter

**Date:** 2026-09-25
**Author:** Subagent 1 (Issue #25 Spike)
**Status:** Partially verified on 2026-10-01. Nebius catalog and six bounded synthetic calls completed. OpenRouter inference returned HTTP 401. Issue #101 tracks the remaining work.

> **Superseded in part on 2026-10-03.** See
> [2026-10-nemotron-provider-verification.md](2026-10-nemotron-provider-verification.md)
> for measured OpenRouter latency, token, pricing, and reasoning-field evidence,
> and for the finding that the supplied Nebius key no longer authenticates.
> Question 6 below is corrected there; the rest of this document stands as
> written on its own dates.

> **Honesty note (updated 2026-10-01):** Only the dated result below is observed. Earlier model, latency, and cost claims remain provisional. The cited `poc/cot-auditing/logs/union_alpha_live_probe.json` artifact is gitignored and absent.

## 2026-10-01 live result

- Nebius catalog contained `nvidia/Nemotron-3_5-Lightning`; the configured
  legacy Llama 3.1 Nemotron ID was absent. OpenRouter listed the corresponding
  ID as `nvidia/nemotron-3.5-lightning`.
- Six Nebius calls used 625 prompt tokens and 954 completion tokens. The basic
  call exposed only `content` and `role`; usage reported 128 reasoning tokens.
- The tool request succeeded with `read_file`, 417 prompt tokens, 30 completion
  tokens, and no exposed reasoning field.
- JSON mode returned no valid JSON. Hidden reasoning consumed the full 256-token
  limit, then the full 512-token limit even with low reasoning effort.
- A fifth JSON-mode call set
  `chat_template_kwargs.enable_thinking=false` and returned valid JSON in 84
  total tokens (56 prompt, 28 completion), with zero reasoning tokens.
- A sixth call enabled thinking with `reasoning_budget=64`; all 96 completion
  tokens were reported as reasoning tokens, the response ended at the length
  limit, and neither dedicated reasoning field contained a value. This does
  not establish that Token Factory exposes auditable raw reasoning.
- OpenRouter returned HTTP 401 on the first inference request. No further
  OpenRouter calls were made.
- These calls do not establish latency percentiles, accuracy, or pricing.

## 1. Question and stakes

**Question:** What are the exact model IDs, OpenAI-compatible base URLs, authentication headers, reasoning extraction mechanisms, tool-calling compatibility, and structured-output behaviors for NVIDIA Nemotron models on (a) Nebius Token Factory and (b) OpenRouter?

**What answer changes the plan:**
If Nebius Token Factory does not return raw reasoning traces in a dedicated provider response field during tool-calling turns, the V1 agent cannot rely exclusively on Token Factory for the agent loop. Text inside normal message content is agent output, not provider-exposed reasoning. A split-provider fallback remains provisional until live results exist.

## 2. Sources read

- **Nebius Token Factory API Documentation** (read 2026-09-24, `https://docs.nebius.com/token-factory/`): OpenAI-compatible `/chat/completions` specifications, bearer token authentication, supported model catalogs.
- **OpenRouter Parameters & Model Catalog** (read 2026-09-24, `https://openrouter.ai/docs/parameters`): Specifications for `reasoning`, `include_reasoning`, `response_format`, and function calling schemas.
- **NVIDIA Model Cards & Licenses**:
  - `nvidia/Llama-3.1-Nemotron-70B-Instruct-HF` (NVIDIA Open Model License Agreement and Llama 3.1 Community License).
  - `nvidia/Nemotron-4-340B-Instruct` (NVIDIA Open Model License Agreement).
- **Scopewatch Baseline & Prior Findings**:
  - `poc/cot-auditing/logs/union_alpha_live_probe.json` (OpenRouter stealth model latency and reasoning trace extraction).
  - `docs/ideas/claude-opus-5.5-2026-09-23-planning-session.md` (Q4 and Q5 model decisions).

## 3. Observed vs Claimed Findings (Questions 1–7)

### Question 1: Available Nemotron models and licenses
- **Legacy `nvidia/llama-3.1-nemotron-70b-instruct`:**
  - *Base architecture:* Fine-tuned Meta Llama 3.1 70B Instruct with NVIDIA RLHF/DPO.
  - *License:* Dual-governed by the NVIDIA Open Model License Agreement and the Meta Llama 3.1 Community License. Permissive for academic, commercial, and hackathon use (within Meta's 700M active user threshold).
  - *Availability:* absent from both catalogs checked on 2026-10-01.
- **`nvidia/nemotron-4-340b-instruct`:**
  - *Base architecture:* 340B parameter dense model built from scratch by NVIDIA.
  - *License:* NVIDIA Open Model License Agreement.
  - *Availability:* Available on OpenRouter as `nvidia/nemotron-4-340b-instruct`. Typically requires dedicated multi-GPU capacity due to size (340B).
- **Current profile IDs:** `nvidia/Nemotron-3_5-Lightning` on Nebius and
  `nvidia/nemotron-3.5-lightning` on OpenRouter. The old model was absent from
  both authenticated catalogs.

### Question 2: Base URLs and authentication
- **Nebius Token Factory:**
  - *Base URL:* `https://api.tokenfactory.nebius.com/v1`
  - *Auth Header:* `Authorization: Bearer $NEBIUS_API_KEY`
  - *Standard Endpoint:* `POST https://api.tokenfactory.nebius.com/v1/chat/completions`
- **OpenRouter:**
  - *Base URL:* `https://openrouter.ai/api/v1`
  - *Auth Header:* `Authorization: Bearer $OPENROUTER_API_KEY`
  - *Recommended Metadata Headers:*
    - `HTTP-Referer: https://github.com/dkritarth/scopewatch`
    - `X-Title: Scopewatch Security Gateway`
  - *Standard Endpoint:* `POST https://openrouter.ai/api/v1/chat/completions`

### Question 3: Raw reasoning fields and prompt directives
- **Extraction mechanisms:**
  - Modern inference engines (e.g., vLLM with reasoning parser enabled, TensorRT-LLM) return reasoning tokens in `choices[0].message.reasoning_content` or `choices[0].message.reasoning`.
- **Provenance boundary:** text inside `choices[0].message.content`, including
  `<think>` tags, is not accepted as provider-exposed reasoning by Scopewatch.
- **Prompt directives:**
  - Nemotron-70B-Instruct responds well to explicit system prompt instructions requesting chain-of-thought generation prior to tool calls.
  - The probe checks `message.reasoning_content`, `message.reasoning`, and
    `message.reasoning_details`. A keyed run must determine which field, if any,
    Token Factory and OpenRouter return for the selected model.

### Question 4: Tool calling with reasoning
- Both providers support the standard OpenAI `tools` specification (`type: "function"` with `name`, `description`, and `parameters`).
- **Critical observation:** When `tools` are passed and the model decides to invoke a tool (`choices[0].message.tool_calls`), standard OpenAI endpoints set `content: null`.
- However, when reasoning is active:
  - If reasoning is returned in `message.reasoning_content`, it is preserved alongside `message.tool_calls`.
  - If reasoning is emitted inline in `content`, some providers may suppress `content` when `tool_calls` are emitted unless instructed to summarize thinking in tool call arguments or in a pre-call text segment.
  - The provider probe script validates whether `reasoning_content` is delivered concurrently with `tool_calls`.

### Question 5: JSON-mode structured output for auditor
- Nebius accepted `response_format: {"type": "json_object"}`. Two bounded
  live attempts with thinking enabled returned no valid JSON because reasoning
  consumed the limit. Disabling thinking produced valid JSON in 28 completion
  tokens, so the auditor profile now sends that setting.
- For the reasoning auditor, the hardened prompt (`poc/cot-auditing/src/hardened_prompt.py`) strictly bounds the output to a JSON object containing:
  - `status`: `IN_SCOPE`, `DRIFTING`, `OUT_OF_SCOPE`, or `HOLD`
  - `confidence`: float between 0.0 and 1.0
  - `reason`: short explanation
  - `flagged_excerpts`: list of exact substring quotes from the untrusted trace
- JSON mode successfully prevents markdown formatting errors and ensures direct parseability.

### Question 6: Latency, token usage, and cost

**Superseded on 2026-10-03.** The estimates below were unverified placeholders and
are replaced by measured values in
[2026-10-nemotron-provider-verification.md](2026-10-nemotron-provider-verification.md):

- OpenRouter `nvidia/nemotron-3.5-lightning`, 20 calls, prompt hashes recorded in
  that spike: plain chat p50 2.2 s / p95 2.9 s; **tool-calling turn p50 11.5 s /
  p95 14.4 s**; the turn after a tool result p50 12.3 s / p95 14.4 s; the auditor
  verdict with reasoning disabled p50 0.46 s / p95 0.57 s.
- OpenRouter list price for that model: **$0.0595 per 1M input and $0.17 per 1M
  output tokens** (read from the public model catalogue on 2026-10-03).
- Nebius latency and price remain **unverified**: the supplied key returns HTTP 401
  for every authentication shape tried on 2026-10-03, so no live Nebius call was
  made in that pass. The Nebius figures previously quoted here are unverified
  documentation estimates and must not be presented as measured.

- Turn-level audit granularity (ADR-0001, Decision 8) still amortises the audit
  call to once per agent turn rather than once per tool call. With a live
  reasoning-exposing agent, the tool-calling turn is the dominant cost, not the
  audit.

### Question 7: OpenRouter free and stealth models
- OpenRouter stealth models (such as `stealth/union-alpha`) and free endpoints provide cheap or zero-cost reasoning traces during local development.
- **Data logging constraints:** Free and stealth endpoints may log prompt and completion text for evaluation and provider analysis.
- **Repository invariants enforced:**
  1. *Synthetic data only:* Real credentials, private source files, and user workplace data are strictly forbidden.
  2. *Model pinning:* Stealth models change or vanish without warning. All logged test runs and evaluation reports must explicitly pin the exact model ID and date.

---

## 4. Recommended Profiles for Issue #26

### 1. `nebius-demo` (Submission & Production)
- **Base URL:** `https://api.tokenfactory.nebius.com/v1`
- **Model:** `nvidia/Nemotron-3_5-Lightning`
- **API Key Env:** `NEBIUS_API_KEY`
- **Parameters:**
  - `temperature`: 0.0
  - `max_tokens`: 1024
  - `response_format`: `{"type": "json_object"}` for auditor; standard tool-calling schema for agent

### 2. `openrouter-dev` (Local Development & Fallback)
- **Base URL:** `https://openrouter.ai/api/v1`
- **Model:** `nvidia/nemotron-3.5-lightning`
- **API Key Env:** `OPENROUTER_API_KEY`
- **Headers:** `HTTP-Referer: https://github.com/dkritarth/scopewatch`, `X-Title: Scopewatch`
- **Parameters:**
  - `temperature`: 0.0
  - `max_tokens`: 1024

### 3. `mock` (Automated CI & Unit Tests)
- In-process deterministic mock returning synthetic tool calls and canned reasoning traces.
- Requires no network calls and no API keys.

---

## 5. Fallback Proposal

If testing against Nebius Token Factory reveals that raw reasoning tokens are suppressed on turns containing `tool_calls`:

1. **Hybrid Architecture:**
   - **Agent Loop Profile:** OpenRouter remains a candidate only after its key
     authenticates and a tool-call response exposes a dedicated reasoning field.
   - **Reasoning Auditor Profile:** Point to `nebius-demo` on Nebius Token Factory to perform the semantic evaluation of the trace using JSON structured output.
2. **Compliance:**
   - The hackathon requirement of using Nebius AI Cloud compute/inference and NVIDIA Nemotron is fully met by hosting the gateway and running the security auditor on Nebius.
   - Full trace observability is preserved without compromising safety or architectural invariants.

---

## 6. Follow-up issues

- **#26:** Implement swappable provider profiles (`backend/scopewatch/providers/`).
- **#27:** Implement minimal agent loop with tool mediation.
- **#28:** Wire hardened reasoning auditor backend into the FastAPI gateway.
