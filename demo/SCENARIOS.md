# Demo scenarios: scripted vs mock-agent (replay) vs live

All scenario files live in `demo/scenarios/*.json`. Each file defines a
`task_scope` and an ordered `actions` list with synthetic reasoning fields.
Scenario files carry **no per-scenario mode key**. The global `--mode`
(`scripts/run_demo.sh`, `scripts/seed_demo.py`) is the sole control.

## The three ways a scenario runs

1. **Scripted** (`--mode scripted`, the default).
   The seeder submits each entry in `actions` directly to the gateway via
   `ScopewatchService.submit_action`. No model is involved.

2. **Mock-agent** (`--mode agent` with the default `mock` provider).
   **Deterministic replay, not model choice.** `build_scenario_mock_provider`
   (`backend/scopewatch/agent/__main__.py`) enqueues exactly one tool call
   per entry in the scenario's `actions` list, verbatim (operation, resource,
   arguments, and reasoning fields, in order), then a final completion turn.
   The agent loop dispatches those calls through the gateway. The mock
   provider never invents, reorders, or skips actions, so mock-agent runs
   prove gateway mediation (policy/audit/executor evidence), not model
   behaviour.

3. **Live-agent** (`--mode agent --profile <live-profile>`).
   A real model chooses actions via the system prompt
   (`backend/scopewatch/agent/prompt.py`) and the gateway-mediated tools
   (`backend/scopewatch/agent/tools.py`, including the allowlisted,
   shell-free `run_command`). Only live runs exercise model choice; they
   need keys and are never run in CI.

## Which sets exist

- Invoice set `01_`-`06_` (default seed): safe audit, denials, approval,
  and the invoice reasoning-injection escalation.
- Coding set `10_`-`13_` (issue #38, `--coding`): auth fix, secret read,
  README injection, network exfiltration. The workspace is seeded from the
  synthetic `demo/coding-workspace` fixture (off-by-one `auth.py`, pytest
  suite, contributor notes with a reserved `.invalid` injection string, and
  a fake-credential `.env`). `run_demo.sh --coding` copies the fixture into
  an isolated runtime workspace so the fix never dirties the repo.

## Decision record (issue #73)

Five scenario files used to carry a top-level `"mode": "scripted"` key that
no code read; the other five never had it. We deleted the key (option b)
rather than honouring per-scenario overrides, because honouring would add
mixed-mode branching for no demo need: every scenario is designed to pass
in both scripted and mock-agent modes, and the existing tests prove it.
