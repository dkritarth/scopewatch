# T3 task protocol

- Discover the live provider/model catalog through `orchestrator_capabilities`, including custom accounts. Tool names may have an MCP prefix such as `mcp__t3_code__`.
- Native same-provider subagents are suitable only when they can preserve the chosen account, model, and options. Otherwise use T3 `delegate_task`. Cross-provider work uses T3, not another provider's CLI.
- Prefer `mode: async`. Retain `taskId` and a stable `clientRequestId` across retries. Async completion wakes the parent; finish useful independent work, then yield instead of polling. Use `task_status` when the result is needed mid-turn and `task_cancel` to stop work.
- With `mode: wait`, `waitTimedOut` ends the parent's wait only. The child continues; retain its task ID instead of spawning a replacement. A completed turn with pending nested work is not a completed task.
- Every delegated review round is a new `delegate_task`, with a distinct request ID and the original brief, findings, responses, and unresolved objections. `childThreadId` is backing storage; do not continue reviews through `t3_thread_send`.
- `t3_thread_launch` and `create_threads` are for explicitly requested separate top-level conversations. Use delegated children for ordinary subagent work. Scheduling requires the user's requested recurring work.
- If discovery lists no T3 tools, try `orchestrator_capabilities` directly once. If tools remain absent and `T3_ACP_MCP_NODE` exists, use the supported bridge:

```bash
ELECTRON_RUN_AS_NODE=1 "$T3_ACP_MCP_NODE" ${T3_ACP_MCP_ENTRYPOINT:+"$T3_ACP_MCP_ENTRYPOINT"} acp-mcp-call orchestrator_capabilities '{}'
```

Use the same bridge with `delegate_task` and a JSON argument containing `task`, `target`, `mode`, and `clientRequestId`. If neither transport works, report that limitation; complete suitable work locally without claiming delegation occurred.
