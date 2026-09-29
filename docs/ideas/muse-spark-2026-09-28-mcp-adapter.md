# MCP adapter prototype: findings (issue #51)

Author: Muse Spark (implementer/researcher thread), 2026-09-28.
Status: prototype evidence for `Advances #51`. Issue #51 stays
`status: needs-discussion` — the open questions below are recorded, not
resolved unilaterally.

Related: #48 (ACP/Codex spike, read-only context), #49 (ACP client adapter),
#29 (escalate-only decision merge, the blocker #51 names), #61 (coverage
statement pattern this prototype follows).

## What was built

`poc/mcp-gateway/src/mcp_adapter.py` — a stdlib + `httpx`-only
`GatewayMCPAdapter` whose only effect channel is the gateway HTTP API.
Verified by 26 prototype tests (`poc/mcp-gateway/tests/`, fake MCP client
over `httpx.MockTransport`, no network) plus 8 contract tests
(`backend/tests/test_mcp_adapter_contract.py`) that pin the same adapter
against the real gateway inside the backend suite. Full log in the PR;
`scripts/validate.sh --quick` green.

## Protocol mapping table

| MCP side (this prototype) | Gateway side (`SubmitActionRequest`) | Decision handling |
| --- | --- | --- |
| `list_directory {path}` | `tool: workspace, operation: list_directory, resource: path` | ALLOW result / DENY tool error / HOLD poll |
| `read_text {path}` | `operation: read_text, resource: path` | same |
| `write_text {path, content}` | `operation: write_text, resource: path, arguments: {content}` | same |
| `delete_path {path}` | `operation: delete_path, resource: path` | usually HOLD → approval |
| `run_command {command \| argv, cwd?}` | `operation: run_command, resource: cwd ?? "."`, `arguments: {command}` or `{argv}` | DENY on local executor (`RULE_RUN_COMMAND_REQUIRES_DOCKER`); allowlisted + Docker-gated otherwise |
| unknown tool name | *(no gateway call)* | client-side `UNKNOWN_TOOL` error |
| non-object arguments | *(no gateway call)* | client-side `MALFORMED_ARGUMENTS` error |
| missing `path` | payload omits `resource` → gateway HTTP 422 | `GATEWAY_REJECTED (422)`, nothing executed |
| HOLD, approval granted | poll `GET /runs/{id}/actions/{action_id}` → receipt EXECUTED + approval APPROVED/CONSUMED | `HOLD_THEN_EXECUTED` result |
| HOLD, approval denied/expired | same poll | `APPROVAL_DENIED` / `APPROVAL_EXPIRED` tool error |
| HOLD, no resolution in time | caller-side deadline | `HOLD_TIMEOUT` tool error, fail closed (never reported as executed) |
| ALLOW policy, executor FAILED (e.g. missing file) | receipt `FAILED` | `EXECUTION_FAILED` tool error (observed: policy ALLOW ≠ execution success) |
| `reasoning_summary` present / absent | `reasoning_provenance: AGENT_AUTHORED_SUMMARY / UNAVAILABLE` | provider trace never forwarded (no such parameter exists) |

## Permission / session translation

- **Permissions:** MCP has no permission primitive in this prototype; every
  check stays gateway-side. Scope allowlists (`allowed_tools`,
  `allowed_operations`, `allowed/blocked_paths`), the deterministic policy
  order, and `requires_approval → HOLD` apply unchanged. The adapter adds no
  policy of its own (unknown-tool and non-object-args guards aside) so there
  is exactly one authority for allow/deny.
- **Sessions:** one adapter instance is bound to one gateway `run_id`;
  `requested_by` defaults to `mcp-adapter-agent` and is configurable per
  deployment. Multi-turn MCP sessions map to sequential actions under the
  same run; there is no cross-run state in the adapter.
- **Approvals:** each MCP call creates exactly one action and at most one
  approval. Approval tokens stay exact and single-use gateway-side (reuse →
  HTTP 409 `APPROVAL_ALREADY_RESOLVED`, verified); a retry after
  `HOLD_TIMEOUT` is a *new* action with a *new* approval, never a replay.

## Coverage: what the gateway mediates vs cannot see

Mediated: every action the adapter submits (`POST /actions`), including its
policy decision, approval lifecycle, execution receipt, and evidence events —
identical to any other gateway client.

Cannot see (follows the #61 pattern; `COVERAGE_STATEMENT` in the adapter):
the MCP client's own native file/shell tools, any out-of-band process or
network egress, and anything that never reaches the gateway API. A missing
event never proves nothing happened. This is the same voluntariness caveat
#51 already records, and the reason ACP (#48) was preferred first: under ACP
the client owns the file/terminal capabilities, while under MCP the agent
usually keeps native tools that differ per host.

## Bypass analysis (what a malicious MCP client could try)

| Attempt | What stops it (observed) |
| --- | --- |
| Call native file/shell tools directly, skipping the adapter | Nothing — outside the gateway, invisible. Must be stated, not fixed here (disable native tools per-host; differs per agent). |
| Tamper with `resource` to escape scope (`../`, blocked dir) | Deterministic policy DENY (`BLOCKED_PATH` / traversal rules); receipt `NOT_EXECUTED` (tested). |
| Send malformed payloads (missing `resource`, extra fields, wrong types) | Gateway schema validation HTTP 422 before any action row or event exists (tested: `ACTION_REQUESTED` count unchanged). |
| Reuse an approval token for a second action | Single-use consumption; replay → 409 (tested). |
| Approve a DENY | No approval object is created for DENY; `resolve_approval` refuses to override DENY (`DENIED_ACTION_CANNOT_BE_APPROVED`). |
| Claim a timed-out HOLD executed anyway | Adapter reports `HOLD_TIMEOUT`; evidence shows approval still PENDING and no EXECUTED receipt (tested). |
| Smuggle a full reasoning trace through the adapter | No parameter or code path forwards one; stored provenance stays SUMMARY/UNAVAILABLE (tested). |
| Talk to the executor directly | Adapter has no such code path (source-asserted); executor itself requires policy evidence / valid approval. |
| Audit-failure or auditor-timeout escalation | Fail-closed HOLD via the existing decision-merge path (unchanged by this prototype; not re-tested here). |

What the prototype does **not** claim: containment of the MCP server process
itself, authentication of the MCP client, or any detection guarantee outside
the gateway (per AGENTS.md evidence-honesty rules).

## Open questions for #51 (needs-discussion — not resolved here)

1. Real MCP transport: stdio vs Streamable HTTP vs SSE, and which SDK
   (`mcp` python package?) the team will depend on. This prototype tests the
   mapping, not SDK interop.
2. Tool surface: exact five tools, or also `network_request` (currently
   gateway-denied baseline-wide) and richer `run_command` forms?
3. HOLD UX: naive polling vs gateway SSE (`/events/stream`) push, and how
   the caller-side timeout should relate to server-side approval expiry.
4. `requested_by` / session identity: one run per MCP session, or per task?
   Who mints runs and with what auth?
5. Native-tool lockdown per supported MCP host (the core voluntariness
   caveat) — documented where, enforced how, and who verifies it per release?
6. Relationship to #49 (ACP adapter): shared mapping library, or two
   independent adapters? #48's finding should decide.
7. `run_command` Docker-path coverage: untested here (no Docker); needs the
   executor-docker environment before any production claim.

## Effort estimate for a production adapter

- SDK transport + session management + error taxonomy: ~1–2 days.
- SSE-based HOLD resolution + timeout/expiry alignment: ~1 day.
- AuthN/Z for MCP clients, run provisioning, per-host native-tool lockdown
  docs: ~2–3 days (mostly decisions + docs, see questions 4–5).
- Hardening + second-pass review + Docker-path `run_command` tests: ~2 days.
- Total rough order: **one focused week** for a reviewable production
  adapter, *after* #51's open questions (especially 1, 4, 5) are decided and
  #29/#48 land. The mapping and contract tests here should carry over
  unchanged.
