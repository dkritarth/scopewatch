# Nebius and NVIDIA usage (Devpost-ready draft)

For issue #46. Exact profiles, model IDs, roles, licenses, and our feedback to
both providers. Nothing here claims a live call that has not happened: each
live-dependent statement carries its qualifier.

## Nebius — what we use

### 1. Nebius inference: `nebius-demo` provider profile

- **Profile:** `nebius-demo` in `backend/config/providers.toml` (search for
  `[profiles.nebius-demo]`; line numbers drift, so cite the section name).
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
  on hidden reasoning and returned no valid JSON.
- **Observed 2026-10-03 (issue #101, resolved):** this profile is **not**
  unverified. A Token Factory key authenticates, `GET /v1/models` returns 200
  with the full catalog, and a 48-case held-out auditor run completed —
  **79.2% accuracy, FNR 8.33%, FHR 0.00%, failure rate 14.58%, latency p50
  619 ms** (split `heldout`, prompt `v1.0-hardened-1cea92f0`, report re-checked
  with `--check`). Live agent runs of demo scenarios 01–05 and 10 also completed
  on this profile; per-scenario results are in `demo/SCENARIOS.md`. Method and
  per-category detail: `docs/evaluation.md`; full evidence chain:
  `docs/spikes/2026-10-nemotron-provider-verification.md`.
- **The earlier "HTTP 401 / currently unverified" note was a wrong credential
  type, not a code, endpoint, or product fault.** The key then in use was a
  Nebius **AI Cloud** service-account key, not a Token Factory key. It returned
  401 on Token Factory and AI Studio, and 403 on AI Cloud — the difference that
  identifies it as the right credential accepted with the wrong permission.
  With a Token Factory key the profile worked immediately. Nothing in the
  gateway, `providers.toml`, or the endpoint needed to change.
- **Not verified:** Nebius's published per-token price was documented but not
  re-confirmed live (same spike, §10), so no cost figure is claimed here.

### 2. Nebius AI Cloud VM: hosted demo (PENDING-HUMAN, issue #43)

- **Plan:** a small CPU VM serves the gateway + reviewer dashboard at
  `<HOSTED_URL>` over HTTPS through 2026-12-15, with a billing alert and a
  named availability owner.
- **Status:** the deployment artifacts are done and on `main` — `deploy/`
  (compose, Caddyfile, edge gate, runbook, smoke script) landed via issue #42 /
  PR #79, and `deploy/test_compose_layout.py` runs in `./scripts/validate.sh`.
  The **VM itself is not provisioned**: that needs a human with a Nebius
  account (issue #43, `agent: needs-human`). So there is no hosted URL to show
  a judge today, and none is claimed anywhere in this package. Judge
  instructions fall back to the local run until it exists — see
  `docs/submission/testing-instructions.md`.
- **What runs on the VM (once provisioned):** exactly this repo —
  `./scripts/run_demo.sh` for the scripted + agent demos, `./scripts/validate.sh`
  for the clean-room check. No separate hosted codebase.

### Provider profiles: what each one exposes

Not interchangeable, and the difference shows up in the evidence a reviewer
sees. Measured 2026-10-03, same corpus and prompt version, so the two held-out
runs are directly comparable:

| | `nebius-demo` (Nebius) | `openrouter-dev` (OpenRouter) |
| --- | --- | --- |
| Model served | `nvidia/Nemotron-3_5-Lightning` | `nvidia/nemotron-3.5-lightning` |
| Reasoning field observed | `message.reasoning` and `message.reasoning_content` both present and **`None`**; thinking written into `content` | non-empty `message.reasoning`, including on tool-call turns |
| Recorded provenance | **`UNAVAILABLE`** | **`PROVIDER_EXPOSED_TRACE`** (with capture credential) |
| Held-out accuracy (2026-10-03) | 79.2% | 87.5% |
| Held-out latency p50 | 619 ms | 2,372 ms |

Consequence, stated plainly because it changes the demo: Nebius exposes no
reasoning field, so a Nebius-backed run records no trace, the auditor is not
attempted, and a reasoning-escalated `HOLD` cannot be produced on that profile.
The same model through OpenRouter does produce one. Which pair ships is still
an open decision (issue #214), and this package does not pre-empt it.

`mock` (`mock-rules-auditor`, no network, no key) is what CI runs; its 93.8% is
a rule-matcher structural check, not model accuracy.

## NVIDIA — what we use

- **Model:** `nvidia/Nemotron-3_5-Lightning`, used for both configured roles:
  (a) the agent, whose reasoning the auditor reads as evidence when a provider
  exposes it; (b) the auditor, which returns a bounded JSON verdict with
  verbatim flagged excerpts (`backend/scopewatch/reasoning_audit.py`).
- **Why this model:** Nebius lists it as the recommended serverless replacement
  for the Llama and Hermes instruct models withdrawn 2026-08-31, so it is the
  current NVIDIA open model on the platform rather than a deprecated one.
- **Observed limitation:** Nebius does not expose a reasoning field in these
  calls, so Scopewatch records reasoning as `UNAVAILABLE` on this profile
  rather than parsing thinking out of `content`. That is the honest reading and
  the one ADR-0001 requires; it is also the reason the demo's reasoning beat is
  not filmed on `nebius-demo` (issue #214 owns the profile decision).
- **License — not confirmed in this repository.** An earlier draft of this file
  claimed the model was dual-governed by the NVIDIA Open Model License Agreement
  *and* a second, Meta-derived community license. That claim was inherited from
  the superseded September spike and describes a different, older NVIDIA model;
  it does not apply to Nemotron 3.5 Lightning, so it is removed rather than
  restated. What is true today: this repository contains **no verified license
  text for `nvidia/Nemotron-3_5-Lightning`**, and we do not assert one. A human
  must read the current license on the model's official page and confirm the
  team's eligibility before Devpost (issue #41). Our own code is MIT
  (`LICENSE`).

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
- [x] Run the held-out auditor evaluation on `nebius-demo` and quote the dated
      live report — done 2026-10-03 (48 cases, 79.2%, `--check` passed);
      `description.md` now carries those figures instead of the mock ones.
- [ ] Decide which agent/auditor profile pair ships, and whether scenario 06
      can be filmed live (issue #214).
- [ ] Re-confirm the Nemotron license text + team eligibility (issue #41).
      This repository asserts no license for the model today.
- [ ] Provision the VM, set the billing alert, fill in `<HOSTED_URL>` (issue #43).
