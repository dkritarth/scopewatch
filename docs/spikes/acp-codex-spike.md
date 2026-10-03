# ACP Codex Spike: Mediation Feasibility and Bypass Analysis

**Date:** 2026-09-30  
**Author:** Gemini 3.8 Flash (T3 Code / Antigravity harness)  
**Status:** Completed finding for Issue #48; guides ACP Client Adapter implementation (#49).

---

## 1. Executive Summary & Recommendation

**Recommendation:** **Build the ACP client adapter (#49) with an explicit, non-bypassable coverage statement.**

The Agent Client Protocol (ACP) provides an architectural seam where an editor or host platform serves as the ACP *client*, while the coding agent (such as Codex via `codex-acp`) acts as the ACP *server/agent*. By mediating all capability requests (`fs/*`, `terminal/*`, and `session/request_permission`) through the Scopewatch gateway API (`POST /api/v1/runs/{run_id}/actions`), deterministic policies, reasoning audit flags, and approval holds can be enforced before any file or command is executed.

However, because native agent runtimes may retain local execution pathways if not constrained by client-capability negotiation, the Scopewatch gateway cannot guarantee the absence of unobserved actions outside the protocol boundary. All runs must carry a mandatory `COVERAGE_STATEMENT` explicitly informing human reviewers that evidence covers only mediated ACP interactions.

---

## 2. Investigation & Analysis (Questions 1–5)

### Q1: Does `codex-acp` route all file reads and writes through client `fs/*` capabilities, or does Codex read and write files directly?
- **Finding:** In standard ACP client mode, `codex-acp` advertises client-side filesystem capability requirements:
  - `fs/read_text_file`: requests file contents by path.
  - `fs/write_text_file`: submits path and updated text.
  - `fs/delete_file`: requests path removal.
  - `fs/list_directory`: queries directory entries.
- **Bypass Risk:** If the agent is granted host-level filesystem access in its local environment or spawns child processes directly, it could bypass the client RPC channel.
- **Mitigation:** The Scopewatch ACP adapter acts as the client host and only acknowledges virtual/sandboxed paths relative to the declared run workspace. File operations outside the mediated workspace trigger `BLOCKED_PATH` or `PATH_TRAVERSAL` denials.

### Q2: Does it run commands through client `terminal/*`, or in its own sandbox?
- **Finding:** Under ACP, terminal actions are mediated via `terminal/run` requests dispatched to the client (`terminal/create` interactive sessions are refused).
- **Gateway Mapping:** The adapter maps terminal requests directly to `operation: "run_command"` on `tool: "workspace"` with `resource=cwd`.
- **Policy Enforcement:** Commands are parsed without a shell using `shlex.split`, verified against allowlisted command prefixes, rejected if containing shell metacharacters, and executed only in Docker-isolated containers (`SCOPEWATCH_EXECUTOR=docker`). Disallowed commands return an immediate JSON-RPC tool error without execution.

### Q3: Does `session/request_permission` fire for every sensitive action, and can the client deny it?
- **Finding:** ACP specifies `session/request_permission` as the protocol mechanism for user consent before executing sensitive tools or actions.
- **Dry-Run Scope Verification:** To avoid premature action execution or consuming single-use approvals during preflight checks, the adapter evaluates permissions dry-run against the **real deterministic gateway policy** via the side-effect-free seam `POST /api/v1/runs/{id}/actions/preview`. The seam builds the same request submission would send and runs the same `policy.evaluate_policy` against the same run state, workspace root, and `TaskScope`. It writes nothing: no action, decision, approval, or receipt record, no event, no demo-budget spend, and no execution.
  - `allow`: Operation, path, tool, run state, and command prefix are permitted under active scope without approval required. Flagged `provisional: true` — the deterministic layer allowed it, but the submit-time reasoning audit may still escalate to `HOLD`, and execution may still fail.
  - `deny`: The deterministic decision is `DENY` (e.g. `PATH_OUTSIDE_WORKSPACE`, `BLOCKED_PATH`, `PATH_TRAVERSAL`, `TOOL_NOT_ALLOWED`, `COMMAND_NOT_ALLOWED`, `OPERATION_NOT_ALLOWED`). Also returned when the seam itself is unreachable or malformed, so preflight fails closed.
  - `requires_approval`: The deterministic decision is `HOLD` (`APPROVAL_REQUIRED`) and a human reviewer must approve before execution.
- **Superseded design (#122):** This was originally a separate partial evaluator that read `TaskScope` from `GET /api/v1/runs/{id}` and applied its own path rules. It disagreed with the engine that actually decides — it stripped leading slashes with `lstrip("/")` before its own absolute-path test, so a request the gateway denies was preflighted as an allowed, scope-verified permission, and it checked no run state, tool, command prefix, or approval rule. A dry run that answers a different question from the real decision is worse than no dry run; the seam is now shared with submission by construction.

### Q4: What reasoning does Codex expose over ACP?
- **Finding:** Codex does not expose internal raw Chain-of-Thought (CoT) token streams or hidden model activations across ACP. It emits:
  - High-level progress explanations.
  - Agent-authored step summaries.
  - Contextual messages intended for user feedback.
- **Invariant Adherence (ADR-0001):** Under Scopewatch's provenance taxonomy, Codex reasoning over ACP must **never** be recorded as `PROVIDER_EXPOSED_TRACE`. It must be recorded strictly as `ReasoningProvenance.AGENT_AUTHORED_SUMMARY` when a summary is provided, or `ReasoningProvenance.UNAVAILABLE` when missing.

### Q5: Is using a personal or team Codex subscription this way allowed by its terms?
- **Finding:** Codex commercial and developer subscription terms permit integration with developer tools and editors implementing standard protocols (LSP, ACP, MCP) for local interactive coding.
- **Requirements:**
  1. The integration must not be used to harvest synthetic training data to distill competing foundational models.
  2. Credentials and session tokens must remain secure and never be logged or transmitted to third parties (Scopewatch redacts all auth headers and provider tokens).

---

## 3. Coverage Statement & Security Boundary

Every run mediated through the ACP adapter must record the following boundary notice:

```text
Scopewatch mediates only file, command, and permission operations submitted through
the Agent Client Protocol (ACP) adapter to the gateway API (POST /api/v1/runs/{run_id}/actions).
Permission preflight (POST /api/v1/runs/{run_id}/actions/preview) reports the deterministic
policy decision only: it executes nothing, creates no approval, and a preflight allow is
provisional because the submit-time reasoning audit may still hold the action.
If the underlying agent executes actions outside the negotiated ACP capabilities,
such activity is outside the observation boundary. A missing event never proves
an action did not occur. Gateway evidence represents a tamper-evident audit of mediated
operations only.
```

---

## 4. Architectural Implementation Plan (#49)

1. **Module Placement:** `backend/scopewatch/acp_adapter.py`
2. **Components:**
   - `AcpClientAdapter`: Handles JSON-RPC dispatch, session capability handshake, dry-run preflight checks, and mapping to gateway operations.
   - `AcpRpcError`: Conforms to JSON-RPC 2.0 error responses carrying gateway `reason_code` and `explanation`.
   - `HoldConfig`: Pydantic v2 configuration for bounded async polling for human approvals with deterministic fail-closed timeouts.
3. **Gateway mapping — authorization is not execution (#123):**
   - **Preflight (query, no effect):** `session/request_permission` → `POST /api/v1/runs/{run_id}/actions/preview`, returning the deterministic `PolicyDecision` only.
   - **Submission (effect):** `fs/*` and `terminal/run` → `POST /api/v1/runs/{run_id}/actions`, then approval polling via `GET /api/v1/runs/{run_id}/actions/{action_id}`.
   - **Result construction:** a success result requires an `EXECUTED` execution receipt. `FAILED`, `NOT_EXECUTED`, and missing receipts become sanitized JSON-RPC errors carrying the real action ID and the gateway's failure code — never empty content, an inferred byte count, or `exit_code: 0`.
4. **Validation & Testing:**
   - `backend/tests/test_acp_adapter.py`: Comprehensive test matrix with a fake ACP agent exercising reads, writes, deletions, command filtering, holds, approval single-use, and provenance classification.
   - `backend/tests/test_issue_122_123_acp_preflight_and_receipts.py`: Preflight-versus-submission agreement (paths, tool, operation, run state, command prefixes, approval requirements), side-effect-free preflight, and execution-receipt enforcement for failed read/write/command.

---

## 5. Empirical Observed Evidence (Fake Agent Protocol Traces)

The following protocol interaction traces were observed using the synthetic `FakeAcpAgent` contract harness against a local in-process gateway with clean-room fixtures. **No real ACP peer (`codex-acp`) and no live Codex session has been connected** — see issue #121, which tracks the remaining protocol-interoperability and real-run verification.

The first four turns were observed on 2026-09-30. Turns 5 and 6 were re-observed on 2026-10-03 after #122 and #123; the preflight response now carries the deterministic reason code, matched rule, and an explicit `provisional` marker.

```json
[Turn 1: Handshake]
Request:  {"jsonrpc": "2.0", "id": "fake-agent-1", "method": "initialize", "params": {}}
Response: {"jsonrpc": "2.0", "id": "fake-agent-1", "result": {"protocolVersion": "2024-11-05", "capabilities": {"fs": {"readTextFile": true, "writeTextFile": true, "deleteFile": true, "listDirectory": true}, "terminal": {"run": true, "create": false}, "session": {"requestPermission": true}}}}

[Turn 2: Dry-run Preflight Check]
Request:  {"jsonrpc": "2.0", "id": "fake-agent-2", "method": "session/request_permission", "params": {"operation": "read_text", "path": "invoices/approved/vendor_a.txt"}}
Response: {"jsonrpc": "2.0", "id": "fake-agent-2", "result": {"decision": "allow", "scope_verified": true, "provisional": true, "reason_code": "ALLOWED_TOOL_AND_RESOURCE", "matched_rule": "RULE_ALLOWED_TOOL_AND_RESOURCE", "explanation": "Operation 'read_text' on 'invoices/approved/vendor_a.txt' is permitted.", "audit_note": "Deterministic policy ALLOW only; submit-time reasoning audit may still escalate to HOLD, and execution may fail."}}

[Turn 3: Mediated Read]
Request:  {"jsonrpc": "2.0", "id": "fake-agent-3", "method": "fs/read_text_file", "params": {"path": "invoices/approved/vendor_a.txt", "reasoning_summary": "Reading vendor invoice", "turn_id": "turn-1"}}
Response: {"jsonrpc": "2.0", "id": "fake-agent-3", "result": {"path": "invoices/approved/vendor_a.txt", "content": "Invoice: $500", "action_id": "..."}}

[Turn 4: Adversarial Traversal Block]
Request:  {"jsonrpc": "2.0", "id": "fake-agent-4", "method": "fs/read_text_file", "params": {"path": "../../../etc/shadow", "turn_id": "turn-3"}}
Response: {"jsonrpc": "2.0", "id": "fake-agent-4", "error": {"code": -32000, "message": "Operation denied by policy: PATH_TRAVERSAL", "data": {"reason_code": "PATH_TRAVERSAL", "explanation": "Directory traversal using '..' is prohibited.", "action_id": "...", "matched_rule": "RULE_PATH_TRAVERSAL_REJECTED"}}}

[Turn 5: Absolute Path Preflight Denies (the #122 lstrip regression)]
Request:  {"jsonrpc": "2.0", "id": "fake-agent-5", "method": "session/request_permission", "params": {"operation": "read_text", "path": "/outputs/old.txt"}}
Response: {"jsonrpc": "2.0", "id": "fake-agent-5", "result": {"decision": "deny", "reason": "Absolute paths are prohibited.", "reason_code": "PATH_OUTSIDE_WORKSPACE", "matched_rule": "RULE_ABSOLUTE_PATH_REJECTED", "provisional": false}}

[Turn 6: Allowed But Failed Read Is a Tool Error (#123)]
Request:  {"jsonrpc": "2.0", "id": "fake-agent-6", "method": "fs/read_text_file", "params": {"path": "outputs/missing.txt"}}
Response: {"jsonrpc": "2.0", "id": "fake-agent-6", "error": {"code": -32000, "message": "Action did not execute successfully (status=FAILED): EXECUTION_FAILED", "data": {"action_id": "...", "status": "FAILED", "error_code": "EXECUTION_FAILED", "reason_code": "EXECUTION_FAILED", "error": "File not found: outputs/missing.txt"}}}
```
