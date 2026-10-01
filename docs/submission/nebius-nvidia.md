# Nebius and NVIDIA usage (Devpost-ready draft)

For issue #46. Exact profiles, model IDs, roles, licenses, and our feedback to
both providers. Nothing here claims a live call that has not happened: each
live-dependent statement carries its qualifier.

## Nebius — what we use

### 1. Nebius inference: `nebius-demo` provider profile

- **Profile:** `nebius-demo` in `backend/config/providers.toml:23-29`.
- **Base URL (as configured):** `https://api.tokenfactory.nebius.com/v1`
  (OpenAI-compatible `/chat/completions`).
- **Model:** `nvidia/Nemotron-3_5-Lightning`.
- **Key handling:** read at runtime from the `NEBIUS_API_KEY` environment
  variable (`api_key_env`, never committed; see `.env.example`). No key exists
  in this repository.
- **Roles served through this profile:** both the Scopewatch agent loop
  (`backend/scopewatch/agent/`, reasoning trace + tool calls) and the
  reasoning auditor (`backend/scopewatch/reasoning_audit.py`, JSON structured
  output verdicts). Routing is centralized in `backend/scopewatch/providers/`
  so core code never hard-codes a model ID.
- **Observed 2026-10-01:** the authenticated catalog and four bounded synthetic
  calls succeeded. Tool calling returned `read_file` with `finish_reason` set
  to `tool_calls`. Basic and tool-call responses exposed no dedicated reasoning
  field. Two JSON-mode auditor calls exhausted 256 and 512 completion tokens
  on hidden reasoning and returned no valid JSON. Live accuracy is not claimed.
- **URL note:** Nebius Token Factory documentation currently specifies
  `https://api.tokenfactory.nebius.com/v1`. The profile now matches that
  documented endpoint. A keyed request is still required to confirm the model
  catalog and inference access for this account (issue #101).

### 2. Nebius AI Cloud VM: hosted demo (PENDING-HUMAN, issue #43)

- **Plan:** a small CPU VM serves the gateway + reviewer dashboard at
  `<HOSTED_URL>` over HTTPS through 2026-12-15, with a billing alert and a
  named availability owner.
- **Status:** not provisioned. Deployment steps belong to the runbook track
  (issue #42); there is no `deploy/` directory on `main` yet. Judge
  instructions fall back to the local run until the URL exists — see
  `docs/submission/testing-instructions.md`.
- **What runs on the VM (once provisioned):** exactly this repo —
  `./scripts/run_demo.sh` for the scripted + agent demos, `./scripts/validate.sh`
  for the clean-room check. No separate hosted codebase.

### Developer fallback profile (context for judges)

- `openrouter-dev` uses the current catalog ID
  `nvidia/nemotron-3.5-lightning`. The supplied key returned HTTP 401 on the
  first inference attempt, so no OpenRouter behavior is claimed. `mock`
  (`mock-rules-auditor`, no network, no key) drives CI accuracy numbers.

## NVIDIA — what we use

- **Model:** `nvidia/Nemotron-3_5-Lightning`, used for both configured roles:
  (a) the agent, whose provider-exposed reasoning trace is the evidence the
  auditor reads; (b) the auditor, which returns a bounded JSON verdict with
  verbatim flagged excerpts (`backend/scopewatch/reasoning_audit.py`).
- **Observed limitation:** Nebius did not expose a reasoning field in these
  calls. Scopewatch therefore records reasoning as `UNAVAILABLE`; the live
  profile cannot support the reasoning-escalation demo as currently served.
- **License (to be re-verified before submission):** per the provisional spike,
  dual-governed by the **NVIDIA Open Model License Agreement** and the **Meta
  Llama 3.1 Community License**, permissive for academic/commercial/hackathon
  use within Meta's active-user threshold. This is second-hand from
  `docs/spikes/2026-09-nemotron-provider-spike.md` — a human must confirm the
  current license text and the team's eligibility (issue #41) before Devpost.
  Our own code is MIT (`LICENSE`).

## Feedback for Nebius

1. **Product migration confused us:** older AI Studio examples use a different
   hostname, while current Token Factory documentation uses
   `api.tokenfactory.nebius.com`. A migration notice beside old examples would
   make the active endpoint clear.
2. **Reasoning-trace exposure needs a contract:** our gateway depends on raw
   reasoning arriving alongside tool calls in a dedicated provider response
   field. Documenting exactly which fields survive on tool-call turns
   — per model — would let guardrail builders like us commit to Nebius-only
   inference with confidence. (This is the open question behind our
   OpenRouter fallback profile.)
3. **What worked:** OpenAI-compatible `/chat/completions` + `response_format:
   json_object` meant our provider client needed no Nebius-specific code path
   (`backend/scopewatch/providers/client.py`).

## Feedback for NVIDIA

1. **Nemotron 3.5 Lightning followed the tool schema correctly:** the bounded
   synthetic tool request returned the requested `read_file` call.
2. **Provider-specific IDs add configuration risk:** Nebius and OpenRouter use
   different spelling for the same model family. Profiles must pin each ID.
3. **Ask:** a short official note on reasoning-trace fidelity (when emitted
   traces can be truncated or suppressed, e.g. on tool-call turns) would help
   every team building evidence-grade auditing on open models.

## Pre-submit re-verification list (human)

- [x] Confirm the live Nebius base URL, model ID, tool-call behavior, and
      reasoning-field absence; recorded 2026-10-01 under issue #101.
- [ ] Run `python backend/scripts/evaluate_reasoning_audit.py --profile
      nebius-demo --split heldout` and replace the mock figures in
      `description.md` with the dated live report (or report the divergence honestly).
- [ ] Re-confirm the Nemotron license text + team eligibility (issue #41).
- [ ] Provision the VM, set the billing alert, fill in `<HOSTED_URL>` (issue #43).
