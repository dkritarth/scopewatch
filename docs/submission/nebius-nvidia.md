# Nebius and NVIDIA usage (Devpost-ready draft)

For issue #46. Exact profiles, model IDs, roles, licenses, and our feedback to
both providers. Nothing here claims a live call that has not happened: each
live-dependent statement carries its qualifier.

## Nebius — what we use

### 1. Nebius inference: `nebius-demo` provider profile (configured, live run pending)

- **Profile:** `nebius-demo` in `backend/config/providers.toml:23-29`.
- **Base URL (as configured):** `https://api.tokenfactory.nebius.com/v1`
  (OpenAI-compatible `/chat/completions`).
- **Model:** `nvidia/llama-3.1-nemotron-70b-instruct`.
- **Key handling:** read at runtime from the `NEBIUS_API_KEY` environment
  variable (`api_key_env`, never committed; see `.env.example`). No key exists
  in this repository.
- **Roles served through this profile:** both the Scopewatch agent loop
  (`backend/scopewatch/agent/`, reasoning trace + tool calls) and the
  reasoning auditor (`backend/scopewatch/reasoning_audit.py`, JSON structured
  output verdicts). Routing is centralized in `backend/scopewatch/providers/`
  so core code never hard-codes a model ID.
- **Status qualifier:** the profile is wired end to end and exercised in CI via
  equivalent paths, but **no live inference call against Nebius has been run
  from this repo yet — it needs `NEBIUS_API_KEY`** (tracked under issues
  #101/#31/#32). Until a dated live report exists, every accuracy number we
  publish is labelled with the profile that produced it (currently `mock`).
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

- `openrouter-dev` (`backend/config/providers.toml:12-21`) serves the same
  model ID (`nvidia/llama-3.1-nemotron-70b-instruct`) via OpenRouter for local
  development; `mock` (`mock-rules-auditor`, no network, no key) drives CI and
  all reported numbers to date.

## NVIDIA — what we use

- **Model:** `nvidia/llama-3.1-nemotron-70b-instruct` — a fine-tune of Meta
  Llama 3.1 70B Instruct with NVIDIA RLHF/DPO, used for **both** roles:
  (a) the agent, whose provider-exposed reasoning trace is the evidence the
  auditor reads; (b) the auditor, which returns a bounded JSON verdict with
  verbatim flagged excerpts (`backend/scopewatch/reasoning_audit.py`).
- **Why this model:** it is open-weight with visible chain-of-thought output,
  so the gateway can observe reasoning instead of trusting a black box. That
  observability is load-bearing for the headline demo (scenario 06): without
  an exposed trace there is nothing to ground excerpts against, and the audit
  correctly records `UNAVAILABLE` instead of escalating
  (`frontend/scripts/app.js`, provenance labels).
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

1. **Nemotron 70B Instruct is the right shape for agent-safety work:**
   instruction-following plus inspectable reasoning is exactly what a
   pre-execution auditor consumes. Keep publishing the RLHF/DPO lineage notes —
   they informed our model choice.
2. **Please keep one stable instruct ID across catalogs:** the same
   `nvidia/llama-3.1-nemotron-70b-instruct` string resolving on both Nebius and
   OpenRouter is what makes our swappable-profile design (`nebius-demo` vs
   `openrouter-dev`) one line apart. Don't fragment it per host.
3. **Ask:** a short official note on reasoning-trace fidelity (when emitted
   traces can be truncated or suppressed, e.g. on tool-call turns) would help
   every team building evidence-grade auditing on open models.

## Pre-submit re-verification list (human)

- [ ] Confirm the live Nebius base URL and that `NEBIUS_API_KEY` inference
      returns reasoning on tool-call turns; record model ID + date in the issue.
- [ ] Run `python backend/scripts/evaluate_reasoning_audit.py --profile
      nebius-demo --split heldout` and replace the mock figures in
      `description.md` with the dated live report (or report the divergence honestly).
- [ ] Re-confirm the Nemotron license text + team eligibility (issue #41).
- [ ] Provision the VM, set the billing alert, fill in `<HOSTED_URL>` (issue #43).
