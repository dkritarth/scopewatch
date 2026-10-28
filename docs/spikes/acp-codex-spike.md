# ACP Codex mediation spike and verification status

Original research: Gemini 3.8 Flash, 2026-09-30.
Protocol correction and client implementation record: GPT-6.1 Sol, 2026-10-28.
Issue #121 remains open. The manual real Codex acceptance inherited from #49 is pending.

## What was verified before #121

`backend/scopewatch/acp_adapter.py` and its `FakeAcpAgent` tests exercised a
local dictionary dispatcher against the gateway. They verified policy decisions,
HOLD polling, receipt enforcement, provenance labels, and relay bounds. They did
not connect an ACP peer, start a stdio transport, or negotiate an ACP session.

The old document's Q1-Q5 findings were documentation inferences, not observations
of Codex. In particular, its date-string protocol version, reversed initialize
exchange, `terminal/run`, `fs/delete_file`, `fs/list_directory`, and custom
permission `{operation, path}` / `{decision}` exchange are legacy synthetic
contracts. They do not establish ACP v1 interoperability. The legacy dispatcher
remains for existing callers and tests; the new client does not advertise those
methods. The previous subscription-terms claim had no cited verification and is
withdrawn. No subscription or bridge terms review was performed for this change.

## Protocol source and supported subset

The new `backend/scopewatch/acp_client.py` implements ACP major version **1**.
Official sources were retrieved on 2026-10-28 from
[agentclientprotocol/agent-client-protocol revision d6f00fd0ce5323be07f64fca09f59f26a3a5d77a](https://github.com/agentclientprotocol/agent-client-protocol/tree/d6f00fd0ce5323be07f64fca09f59f26a3a5d77a).
This pins the source snapshot, not a Codex bridge release. The repository also
contains v2; this client deliberately negotiates only integer `protocolVersion: 1`.

The reference is
[`schema/v1/schema.json`](https://github.com/agentclientprotocol/agent-client-protocol/blob/d6f00fd0ce5323be07f64fca09f59f26a3a5d77a/schema/v1/schema.json).
The full source artifact SHA-256 is
`1c57095acf9c0844054dd1fdc3624c121b3fdc24209ecc987fd6748c705fdef5`.
The offline test fixture copies the definitions used by the exchange and their
transitive references without modifying them. Tests validate recorded synthetic
stdio messages against these definitions with pinned `jsonschema==4.26.0`.

Relevant official v1 documentation:

- [Initialization](https://agentclientprotocol.com/protocol/v1/initialization): the client sends `initialize`, integer version, `clientCapabilities`, and `clientInfo`. The agent replies with its version and optional capabilities/info. Unsupported version or malformed consumed fields close the connection.
- [Transports](https://agentclientprotocol.com/protocol/v1/transports): the client launches the agent and exchanges UTF-8 JSON-RPC messages delimited by newlines. Stderr is ignored here, never printed or stored.
- [Session setup](https://agentclientprotocol.com/protocol/v1/session-setup): `session/new` carries absolute `cwd` and `mcpServers: []`, then the client uses the returned `sessionId`.
- [Prompt turn](https://agentclientprotocol.com/protocol/v1/prompt-turn): `session/prompt` contains text content blocks. `session/update` is a notification and receives no response. `agent_thought_chunk` text is an `AGENT_AUTHORED_SUMMARY`, never a provider-exposed trace. Missing summaries remain `UNAVAILABLE`.
- [File system](https://agentclientprotocol.com/protocol/v1/file-system): only `fs/read_text_file` and `fs/write_text_file` are advertised. Requests carry absolute paths and the active session ID. Read line/limit selections are applied to a successful bounded whole-file receipt. Oversized, preview-only, or partial receipts are refused. `line: 0` is rejected
  because the prose specifies 1-based lines even though the schema allows zero.
- [Tool calls](https://agentclientprotocol.com/protocol/v1/tool-calls): `session/request_permission` receives `{outcome: {outcome: "cancelled"}}`. No option is selected. Agent tool descriptions/locations are insufficient to bind an exact gateway action, and a deterministic preview cannot replace submit-time auditing or single-use approval. Agents that require a permission grant may therefore stop without performing their task.
- [Terminals](https://agentclientprotocol.com/protocol/v1/terminals): capability `terminal: false`. All terminal methods, including create/output/wait/kill/release and legacy `terminal/run`, are refused. There is no command support.
- [Cancellation](https://agentclientprotocol.com/protocol/v1/cancellation): timeout or interruption sends best-effort `session/cancel`, cancels pending permission requests during a brief grace period, then terminates and reaps the process group. Cancellation does not revoke a stored gateway HOLD; a reviewer may still resolve it separately.

No SDK or bridge dependency was installed. This is a bounded hand-written client
for one text prompt, not an implementation of every optional ACP feature.
Authentication handshakes, reconnect/load/resume, MCP servers, arbitrary prompt
content, multiple concurrent sessions, and remote workspace mapping are unsupported.

## Filesystem and execution boundary

An operator supplies an existing active synthetic run and one absolute local
session root. The operator must bind that root to the intended gateway run
workspace. The public API intentionally does not disclose the gateway's host
workspace path. The client does not discover, create, populate, or read guarded
files. `Path.resolve()` checks filesystem metadata for confinement; gateway HTTP
requests are the only file-effect channel.

This driver supports POSIX local paths only. Windows/UNC/device syntax is
unsupported. Only canonical absolute path syntax below the root is accepted. Relative paths,
`..`, `.`, doubled separators, backslashes, control characters, root-only paths,
and symlink escapes fail before submission. Contained symlink aliases retain
their submitted relative name so the gateway checks both alias and canonical
target against policy. A gateway-host workspace with different symlink metadata
is not a supported remote mapping. The gateway independently owns confinement,
policy, auditing, exact approval, and execution.

Receipt/HOLD handling reuses the reviewed legacy adapter through a bounded HTTP
wrapper. `GatewayClient` provides the synchronous facade over cancellable async
HTTP. It applies the absolute remaining deadline across connection, headers and
body reads, then closes the request client before returning. Numeric HTTP
loopback destinations avoid resolver threads; literal `localhost` maps directly
to `127.0.0.1`. Use explicit `[::1]` for an IPv6-only gateway. Other hostnames
and cross-origin request targets are refused before network I/O. Custom transports
and hooks are trusted application/test code, not peer-controlled callbacks.
Only `EXECUTED` earns success. Missing, failed, or not-executed receipts
are tool errors. Held responses must retain the original action ID and exact
submitted arguments. Error responses expose stable local messages and bounded join
identifiers/status codes, withholding receipt details and upstream bodies.

Limits per session: 256 KiB per stdio frame, 2 MiB combined incoming/outgoing
stdio bytes, 512 frames, 512 KiB per gateway response, 4,000 summary characters,
16,000 task characters, 4,096 characters per opaque ACP session ID, and the existing 65,536-character file relay/write bound.
The session deadline is configurable up to 300 seconds. HTTP calls and HOLD
polling are bounded by the remaining session time. Callbacks are processed
serially; a HOLD delays later callbacks and EOF detection until that bounded
wait ends. There are no unbounded pending callback tasks. Cancellation is
best effort and does not claim a confirmed peer `cancelled` response. Stderr
goes to `/dev/null`.
JSON batches, duplicate keys, invalid UTF-8, non-JSON constants, unsolicited
responses, effect notifications, and wrong-session requests fail closed.

Launching an ACP agent is **not a sandbox**. The child can perform native
filesystem/process/network operations allowed by its OS account. No claim of
prevention or observation outside gateway-submitted requests follows from this
client. It must only be used with synthetic data in an appropriately isolated
agent environment. Whether a real `codex-acp` honors the advertised capabilities
is unverified.

## Runnable session

Use an already running loopback gateway with an existing synthetic run. Choose
an already installed, independently pinned ACP agent executable. This command
does not install a bridge, start infrastructure, or invoke a provider by itself:

```bash
python3 scripts/run_acp_client.py \
  --gateway-url http://127.0.0.1:8000 \
  --run-id SYNTHETIC_RUN_ID \
  --workspace /absolute/local/session-root \
  --synthetic --synthetic-task 'Summarize the synthetic invoice into outputs/summary.txt' \
  -- /absolute/path/to/pinned-acp-agent
```

Gateway credentials come only from `SCOPEWATCH_API_KEY` and/or `DEMO_TOKEN`.
They are not passed to the agent. If the installed agent needs a credential,
set it outside the command and pass its environment variable **name** with
`--agent-env-name NAME`. Only PATH/locale/temp variables and explicitly named
variables reach the child; HOME and credential files are not loaded by this CLI.
Do not put secrets in command arguments. No reviewer or capture credential is used.

The CLI prints a coverage record before launching the peer, then a bounded
completion report with run/session IDs, protocol/source revision, stop reason,
and the coverage statement below. Redirect stdout to a private JSONL artifact
to persist these records. The existing server run coverage field is not modified;
there is no coverage update endpoint. It never prints agent
messages, raw transcripts, provider output, credentials, or stack traces. Existing
run evidence stores mediated actions and their summary provenance. No new server
endpoints or coverage mutation API were added.

## Verification levels and pending acceptance

| Level | Evidence | Claim |
| --- | --- | --- |
| Legacy fake only | Existing adapter tests | Gateway contract behavior; no ACP connection |
| Real stdio, synthetic peer | `backend/tests/test_acp_client.py` | A Python subprocess exchanges actual JSON-RPC frames; no LLM or Codex |
| Actual localhost gateway | Same suite and CLI subprocess test | Synthetic peer + loopback HTTP gateway + isolated SQLite/run workspace; read/write, DENY, HOLD timeout, approved failed receipt, aliases, no unauthorized file effect |
| Real Codex bridge | **Pending** | No bridge version pinned/installed, no Codex session, no external inference |

The synthetic transcript schema test validates initialize/new/prompt/update,
filesystem requests/responses, and cancelled permission shapes against the
pinned official v1 definitions. Adverse peers cover malformed/oversized frames,
aggregate output, timeout, EOF, unsupported methods, wrong sessions, invalid paths,
and bounded cleanup. Existing legacy tests are retained unchanged.

Validation on 2026-10-28 with Python 3.12.3 and Node 24.19.0:

- `PYTHONPATH=backend .venv/bin/python -m pytest backend/tests/test_acp_client.py -q`: 58 passed.
- `./scripts/validate.sh`: passed. Backend 1,983 passed and 25 existing Docker-gated skips; reasoning-audit PoC 96 passed plus 79 subtests; MCP PoC 26 passed; deploy 140 passed; frontend unit 77 passed; browser 18 passed; six clean-room scenarios passed.
- The first full attempt stopped at browser tests because Playwright was missing in this checkout. `npm ci --prefix frontend` installed the locked dependencies, and the final full attempt above passed. No browser infrastructure configuration was changed.
- The backend lockfile passed CI's `uv pip compile` regeneration/comparison command. The 112 vendored schema definitions matched the pinned official source exactly.
- Docker execution remains unverified on this machine. No real bridge was installed or run, and no external inference occurred.

A future manual acceptance must record a pinned bridge version and Codex version,
a synthetic task/run, a redacted stdio transcript, joined gateway action/decision/
approval/receipt evidence, and this coverage statement. It must separately test
whether the bridge respects `terminal: false` and routes filesystem requests as
expected. Merely finding a `codex` executable does not satisfy this criterion.
Issue #121 stays open until that evidence exists. No real bridge compatibility
or successful Codex task is claimed by this PR.

Persisted in each CLI completion/error report:

```text
ACP v1 client mediates only advertised fs/read_text_file and fs/write_text_file
requests through the gateway. Commands, terminal lifecycle, MCP servers, and
permission grants are unsupported. All permission requests are cancelled.
Agent thought chunks are agent-authored summaries, never provider-exposed traces.
Native agent filesystem, process, and network activity is outside this boundary.
Missing gateway evidence does not prove no action occurred. Synthetic stdio peer
verification is not real Codex bridge verification; that manual acceptance is pending.
```
