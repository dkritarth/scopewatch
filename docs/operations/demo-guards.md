# Gateway-native demo guards + token metering (issues #76, #77)

The gateway enforces public-demo controls itself, so they hold for every
deployment. All values below are synthetic placeholders — never commit real
tokens or keys.

> Hosted-stack note: the current `deploy/compose.yaml` keeps the `deploy/gate`
> sidecar for edge rate and count limits. The gate validates mutating requests
> and forwards `X-Demo-Token` over the private Compose network. The gateway
> validates the same token again and applies its native guards and token
> metering. Other deployments may omit the sidecar and use these gateway
> controls directly. Since #107 both layers refuse in the same flat
> `{"error": "<code>", "message": "..."}` envelope; their numerics still
> differ, so treat them as one policy only for the envelope. The gate is
> published on `127.0.0.1:8080` for the operator's own runbook checks.

## Demo mode (#76)

Demo guards engage only when `DEMO_TOKEN` is set. Without it the gateway
behaves exactly as before (local development and tests).

| Variable | Default | Enforced as |
| --- | --- | --- |
| `DEMO_TOKEN` | _(unset = guards off)_ | Shared secret; mutating `/api/*` calls send it as `X-Demo-Token`, else `401 {"error": "demo_token_required"}`. Health, dashboard reads, and static assets stay public. |
| `DEMO_RATE_LIMIT_RUNS_PER_MIN_PER_IP` (alias `DEMO_RATE_LIMIT_PER_MIN`) | `6` | Per-IP sliding window on `POST /api/v1/runs`; `429 {"error": "rate_limited"}` + `Retry-After`. |
| `DEMO_MAX_CONCURRENT_RUNS` | `5` | Runs in `ACTIVE`/`WAITING_FOR_APPROVAL` count; `429 {"error": "concurrent_cap"}`. Completing/failing a run frees capacity. |
| `DEMO_MAX_TURNS_PER_RUN` | `40` | Action submissions per run (attempts counted); `429 {"error": "turn_cap"}`. |
| `DEMO_MAX_RUNS_PER_DAY` (alias `DEMO_DAILY_RUN_BUDGET`) | `200` | Run creations per UTC day; `429 {"error": "budget_exhausted"}`. |
| `DEMO_MAX_ACTIONS_PER_DAY` (alias `DEMO_DAILY_ACTION_BUDGET`) | `5000` | Action submissions per UTC day; `429 {"error": "budget_exhausted"}`. |

Fail-closed: a malformed `DEMO_*` numeric refuses new runs/actions with
`503 {"error": "demo_guard_misconfigured"}` while health and reads stay up.
Guard denials never echo tokens, keys, prompts, traces, or provider bodies.

### Discovery and the reviewer dashboard

`GET /api/v1/health` is a public read and reports `demo_mode: true` when
`DEMO_TOKEN` is set. It never carries the token or anything derived from it.
The reviewer dashboard uses that flag to offer its **Reviewer access** panel
only where a token is actually required (#115): the panel appears when
`demo_mode` is true, and again after the first `401`/`503` on a mutation, in
case the flag is missing from an older deployment. A local gateway without
`DEMO_TOKEN` reports `demo_mode: false` and shows no panel. The dashboard
holds the pasted token in memory for the page load only, writes it to no
browser storage, and sends it as `X-Demo-Token` on mutating `/api/*` requests
only. See `frontend/README.md` for the client-side contract.

### Curl examples

```bash
BASE=http://127.0.0.1:8000
TOKEN=change-me-to-a-long-random-value  # synthetic placeholder

# Public: health and reads need no token.
curl -s $BASE/api/v1/health
curl -s $BASE/api/v1/runs

# Mutating without the token -> 401 {"error": "demo_token_required"}.
curl -s -X POST $BASE/api/v1/runs -H 'Content-Type: application/json' -d '{}' -w '\n%{http_code}\n'

# Mutating with the token (minimal run body; see demo/scenarios for full scopes).
curl -s -X POST $BASE/api/v1/runs -H "X-Demo-Token: $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"name":"demo","task_scope":{"task_description":"t","created_at":"2026-09-28T00:00:00Z"}}'

# Rate-limited creation -> 429 {"error": "rate_limited"} with Retry-After.
# Over-capacity creation -> 429 {"error": "concurrent_cap"}.
# Spent daily budget   -> 429 {"error": "budget_exhausted"}.
```

## Per-profile daily token metering (#77)

Model tokens are metered per provider profile per UTC day (agent + auditor
calls). Mock/scripted usage records 0 tokens and is never refused.

| Variable | Default | Meaning |
| --- | --- | --- |
| `SCOPEWATCH_DAILY_TOKEN_BUDGET_<PROFILE>` | _(unset = unlimited)_ | Daily cap for one profile, e.g. `SCOPEWATCH_DAILY_TOKEN_BUDGET_NEBIUS_DEMO`. Profile name upper-cased, non-alphanumerics become `_`. Malformed values fail closed to `0`. |
| `SCOPEWATCH_TOKEN_BUDGET_DEFAULT` | _(unset = unlimited)_ | Fallback cap for any live profile without its own budget. |
| `SCOPEWATCH_TOKEN_RESERVE_PER_AUDIT` | `8000` | Estimate reserved before each auditor call (check-then-reserve); settled to actuals afterwards. |
| `SCOPEWATCH_TOKEN_BUDGET_STORE` | _(unset = memory only)_ | Optional JSON file for the counters, shared by co-located processes. Counts only. |

Refusal: `429 {"error": "token_budget_exhausted"}` naming the profile and the
UTC-midnight rollover, raised at run creation (agent profile) and before each
auditor spend (auditor profile). Token counts (only) are reported in
`RUN_CREATED` (`details.token_budget`) and reasoning-audit events
(`details.tokens_used` + `details.profile`).

### Live-profile spend expectations

With `mock` profiles no model calls happen, so scripted scenarios always
work. With live profiles (e.g. `nebius-demo`, NVIDIA Nemotron via Nebius),
every agent turn costs one agent completion and every audited action costs
one auditor completion; size `SCOPEWATCH_DAILY_TOKEN_BUDGET_<PROFILE>` from
expected turns × per-call usage, and keep a margin for the per-audit
reservation. Budgets reset at UTC midnight.
