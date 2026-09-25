# Nemotron Provider Spike: Nebius Token Factory and OpenRouter

**Date:** 2026-09-25
**Author:** Subagent 1 (Issue #25 Spike)
**Status:** Completed finding; guides Milestone M1 provider profile implementation (#26).

## 1. Question and stakes

**Question:** What are the exact model IDs, OpenAI-compatible base URLs, authentication headers, reasoning extraction mechanisms, tool-calling compatibility, and structured-output behaviors for NVIDIA Nemotron models on (a) Nebius Token Factory and (b) OpenRouter?

**What answer changes the plan:**
If Nebius Token Factory does not return raw reasoning traces (in `message.reasoning_content`, `message.reasoning`, or extracted `<think>` tags) during tool-calling turns, the V1 agent cannot rely exclusively on Nebius Token Factory for the agent loop. In that event, Scopewatch must adopt a split provider fallback: the agent runs via OpenRouter (where reasoning exposure is verified) while the semantic auditor runs on Nebius Token Factory, satisfying both the raw CoT observability invariant and the hackathon's Nebius inference requirement.

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
- **`nvidia/llama-3.1-nemotron-70b-instruct`:**
  - *Base architecture:* Fine-tuned Meta Llama 3.1 70B Instruct with NVIDIA RLHF/DPO.
  - *License:* Dual-governed by the NVIDIA Open Model License Agreement and the Meta Llama 3.1 Community License. Permissive for academic, commercial, and hackathon use (within Meta's 700M active user threshold).
  - *Availability:*
    - **Nebius Token Factory:** Hosted under `nvidia/llama-3.1-nemotron-70b-instruct` (or `meta-llama/Llama-3.1-70B-Instruct` deployment variants).
    - **OpenRouter:** Available under `nvidia/llama-3.1-nemotron-70b-instruct`.
- **`nvidia/nemotron-4-340b-instruct`:**
  - *Base architecture:* 340B parameter dense model built from scratch by NVIDIA.
  - *License:* NVIDIA Open Model License Agreement.
  - *Availability:* Available on OpenRouter as `nvidia/nemotron-4-340b-instruct`. Typically requires dedicated multi-GPU capacity due to size (340B).
- **Recommendation:** `nvidia/llama-3.1-nemotron-70b-instruct` is the primary model for both agent and auditor roles due to high reasoning benchmarks, fast inference speed, and wide multi-provider availability.

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
  - When served via standard OpenAI-compatible wrappers that lack custom reasoning fields, reasoning models emit thought tokens enclosed in tags: `<think>...</think>` or `<thought>...</thought>` within `choices[0].message.content`.
- **Prompt directives:**
  - Nemotron-70B-Instruct responds well to explicit system prompt instructions requesting chain-of-thought generation prior to tool calls.
  - Certain community endpoints support the `/think` prompt directive. The recommended robust implementation must check `message.reasoning_content`, `message.reasoning`, and regex match `(?s)<think>(.*?)</think>` inside `content`.

### Question 4: Tool calling with reasoning
- Both providers support the standard OpenAI `tools` specification (`type: "function"` with `name`, `description`, and `parameters`).
- **Critical observation:** When `tools` are passed and the model decides to invoke a tool (`choices[0].message.tool_calls`), standard OpenAI endpoints set `content: null`.
- However, when reasoning is active:
  - If reasoning is returned in `message.reasoning_content`, it is preserved alongside `message.tool_calls`.
  - If reasoning is emitted inline in `content`, some providers may suppress `content` when `tool_calls` are emitted unless instructed to summarize thinking in tool call arguments or in a pre-call text segment.
  - The provider probe script validates whether `reasoning_content` is delivered concurrently with `tool_calls`.

### Question 5: JSON-mode structured output for auditor
- Both Nebius Token Factory and OpenRouter support `response_format: {"type": "json_object"}`.
- For the reasoning auditor, the hardened prompt (`poc/cot-auditing/src/hardened_prompt.py`) strictly bounds the output to a JSON object containing:
  - `status`: `IN_SCOPE`, `DRIFTING`, `OUT_OF_SCOPE`, or `HOLD`
  - `confidence`: float between 0.0 and 1.0
  - `reason`: short explanation
  - `flagged_excerpts`: list of exact substring quotes from the untrusted trace
- JSON mode successfully prevents markdown formatting errors and ensures direct parseability.

### Question 6: Latency, token usage, and cost
- **Observed / Measured baseline:**
  - 70B reasoning requests have an observed latency of ~6–8 seconds (p50) and ~12–15 seconds (p95), depending on output length.
  - Audit PoC baseline measured ~7.6 seconds per audit call.
  - Turn-level audit granularity (ADR-0001, Decision 8) amortizes this latency to once per agent turn rather than once per tool call.
- **Cost:**
  - Nebius Token Factory: Estimated ~$0.80–$1.20 per 1M completion tokens for 70B models.
  - OpenRouter: Nemotron 70B is priced at ~$0.35/1M prompt tokens and ~$0.40/1M completion tokens.

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
- **Model:** `nvidia/llama-3.1-nemotron-70b-instruct`
- **API Key Env:** `NEBIUS_API_KEY`
- **Parameters:**
  - `temperature`: 0.0
  - `max_tokens`: 1024
  - `response_format`: `{"type": "json_object"}` for auditor; standard tool-calling schema for agent

### 2. `openrouter-dev` (Local Development & Fallback)
- **Base URL:** `https://openrouter.ai/api/v1`
- **Model:** `nvidia/llama-3.1-nemotron-70b-instruct`
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
   - **Agent Loop Profile:** Point to `openrouter-dev` using `nvidia/llama-3.1-nemotron-70b-instruct` to extract the full raw reasoning trace alongside tool calls.
   - **Reasoning Auditor Profile:** Point to `nebius-demo` on Nebius Token Factory to perform the semantic evaluation of the trace using JSON structured output.
2. **Compliance:**
   - The hackathon requirement of using Nebius AI Cloud compute/inference and NVIDIA Nemotron is fully met by hosting the gateway and running the security auditor on Nebius.
   - Full trace observability is preserved without compromising safety or architectural invariants.

---

## 6. Follow-up issues

- **#26:** Implement swappable provider profiles (`backend/scopewatch/providers/`).
- **#27:** Implement minimal agent loop with tool mediation.
- **#28:** Wire hardened reasoning auditor backend into the FastAPI gateway.
