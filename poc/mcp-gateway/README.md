# MCP gateway adapter prototype (issue #51)

- **Question:** can an MCP client use *only* gateway-mediated tools — with the
  same policy / audit / approval / evidence rules as any other agent — when
  the adapter's only effect channel is `POST /api/v1/runs/{id}/actions`?
- **Status:** active prototype (2026-09-28). Not production code; no gateway,
  frontend, or existing-test files were touched.
- **Placement decision:** this lives in `poc/mcp-gateway/` rather than
  `backend/scopewatch/mcp/` because the gateway is security-critical and a
  prototype must not risk its behaviour or review surface. The adapter is
  stdlib + `httpx` only and imports nothing from `scopewatch`, so it can run
  wherever an MCP server runs. The one exception is
  `backend/tests/test_mcp_adapter_contract.py` (new file, allowed by the task
  brief), which loads this adapter and pins its contract against the real
  gateway inside the normal backend suite.

## How to run

From the repo root (no network, synthetic fixtures only):

```bash
python3 -m pytest poc/mcp-gateway -q
PYTHONPATH=backend python3 -m pytest backend/tests/test_mcp_adapter_contract.py -q
```

## What the adapter does

`src/mcp_adapter.py` (`GatewayMCPAdapter`) translates five MCP-style tools —
`list_directory`, `read_text`, `write_text`, `delete_path`, `run_command` —
into `SubmitActionRequest` bodies (`tool: "workspace"`, mirrored operation /
resource / arguments, see `docs/ideas/muse-spark-2026-09-28-mcp-adapter.md`
for the mapping table) and:

- returns ALLOW + EXECUTED results to the caller;
- surfaces DENY as a tool error carrying the gateway's `reason_code` and
  `explanation` (a DENY is final; there is no path to execution);
- suspends on HOLD and polls `GET /api/v1/runs/{id}/actions/{action_id}`
  until the approval resolves; on timeout it fails closed with
  `HOLD_TIMEOUT` and never claims execution;
- records `AGENT_AUTHORED_SUMMARY` when given a `reasoning_summary`,
  `UNAVAILABLE` otherwise, and never forwards a provider-exposed trace
  (there is no parameter for one);
- never touches the filesystem or the executor directly (asserted by tests
  that inspect its own source).

## Result (2026-09-28, Muse Spark)

- `poc/mcp-gateway`: 26 tests pass (fake MCP client over
  `httpx.MockTransport` against the real gateway app).
- `backend/tests/test_mcp_adapter_contract.py`: 8 tests pass.
- `scripts/validate.sh --quick`: green (see PR body for the pasted log).
- Coverage statement (`COVERAGE_STATEMENT` in the adapter): the gateway
  mediates only actions submitted through the adapter. MCP clients keep
  their own native file/shell tools, so mediation is voluntary — anything
  done natively is not observed, not blocked, not recorded. Same caveat as
  #51 / #48: ACP was preferred first precisely because the client owns the
  file and terminal capabilities there.

## Limitations (honest)

- No real MCP SDK / JSON-RPC transport is wired; the "MCP client" is the
  `call_tool` method plus tool definitions. The protocol mapping is the
  claim under test, not SDK interop (open question in the findings note).
- HOLD polling is naive (fixed interval, caller-side timeout); production
  would want SSE (`/events/stream`) and server-side expiry alignment.
- `run_command` maps onto the gateway contract but stays DENY-gated on the
  local executor (`RULE_RUN_COMMAND_REQUIRES_DOCKER`); Docker-path execution
  was not exercised here (no Docker in this environment).
