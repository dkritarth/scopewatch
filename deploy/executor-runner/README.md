# executor-runner sidecar (issues #78, #80-part1)

Dedicated Docker-execution sidecar. It is the **only** process that mounts
the Docker socket (or talks to a rootless daemon). The gateway runs with
`SCOPEWATCH_EXECUTOR=remote` and dispatches policy-approved actions to
`POST /execute` on the internal network.

## Layout

| File | Purpose |
| --- | --- |
| `runner.py` | Stdlib-only HTTP service (`POST /execute`, `GET /healthz`) |
| `Dockerfile` | Minimal image: pinned `python:3.12-slim-bookworm` + Docker CLI only |
| `compose.executor-runner.yaml` | Fragment merged with the #42 deploy bundle |

## Gateway configuration (environment only, never committed)

```bash
SCOPEWATCH_EXECUTOR=remote
EXECUTOR_RUNNER_URL=http://executor-runner:8091   # internal DNS, no public route
EXECUTOR_RUNNER_TOKEN=<pre-shared bearer secret>  # same value as the runner's
```

The gateway client is `backend/scopewatch/executor_remote.py`. It enforces
the same pre-network policy gates as the local/Docker backends (stored
decision required, `DENY` never dispatches, `HOLD` needs an `APPROVED`
approval bound to the exact action, `network_request` refused), then sends
one JSON dispatch per approved action with a fresh single-use
`dispatch_token` bound to the SHA-256 `action_digest` of that action. The
payload and its digest are produced together by `prepare_dispatch`, so the
digest always describes the arguments the runner receives. Any refusal,
error, timeout, or malformed runner output fails closed to
`FAILED`/`EXECUTION_FAILED` with no fallback to local execution and no
secret in the error.

## Runner contract

`POST /execute` with `Authorization: Bearer <token>` and body:

```json
{
  "dispatch_token": "<single-use random token>",
  "action_digest": "<sha256 of canonical action + decision>",
  "action": {"id": "...", "run_id": "...", "operation": "write_text|read_text|list_directory|delete_path|run_command", "resource": "relative/path", "arguments": {}},
  "policy_decision": {"id": "...", "outcome": "ALLOW|HOLD"},
  "approval": {"id": "...", "action_request_id": "...", "status": "APPROVED"} 
}
```

Validation order (all fail closed with static messages):

1. Bearer via constant-time compare → `401` otherwise.
2. Dispatch token unseen and unexpired (60s TTL, one-shot) → `409` on replay.
3. Digest recomputed with the gateway's canonicalization → `409` on mismatch.
4. Policy outcome must be `ALLOW` or `HOLD` → `403` otherwise. The
   canonicalization is public, so a token holder can forge a matching
   digest; this check is what stops a forged `DENY` from executing.
5. Operation allowlisted, resource relative and inside `/workspace`
   (symlink escapes refused) → `400`/`403`.
6. Hardened `docker run` (no network, read-only rootfs, `nobody`,
   cap-drop, pids/mem caps, workspace-only mount). Container output caps
   (64 KiB/stream) and per-command timeouts (1–300s, default 60s) match the
   gateway Docker backend. Copy-back after `write_text`/`run_command` is
   symlink-tolerant (issue #80: existing links are replaced, never
   followed; only additions/overwrites propagate).

## Run it (synthetic only)

```bash
# never commit a real value; generate per deployment
export EXECUTOR_RUNNER_TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
python3 deploy/executor-runner/runner.py  # binds 127.0.0.1:8091 by default
curl -s http://127.0.0.1:8091/healthz     # {"status": "ok"}
```

With the #42 bundle: merge `compose.executor-runner.yaml` into
`deploy/compose.yaml` (same `internal` network + shared workspace volume),
set `EXECUTOR_RUNNER_TOKEN` in the deploy `.env` (gitignored), and set the
gateway's `SCOPEWATCH_EXECUTOR=remote`,
`EXECUTOR_RUNNER_URL=http://executor-runner:8091`.

## Risk section

- **Socket isolation.** `/var/run/docker.sock` is root-equivalent: any
  writer can start privileged containers and escape to the host. Only the
  runner mounts it (verified by the socket-grep test:
  `backend/tests/test_executor_remote.py::test_no_docker_socket_outside_runner`,
  which fails the suite if `docker.sock` appears in `backend/scopewatch`,
  `frontend`, `scripts`, or `deploy` outside `deploy/executor-runner/`).
  The single exemption is an explicit verifier allowlist
  (`scripts/check_docker_acceptance.py`, which only asserts the socket is
  absent from the executor image); those files are still scanned for
  mount/dial patterns and must mention the socket in an absence-asserting
  context. The gateway, gate, and caddy images and services must never gain
  a socket mount; review any deploy change that touches `volumes:` for this.
- **Token theft window.** The bearer token is a shared secret: anyone who
  can read it (deploy `.env`, process env, internal network capture) can
  submit dispatches — but each dispatch still needs a fresh single-use
  token bound to a policy-approved action digest, and replays are refused.
  Theft impact is limited to submitting *new* dispatches until the token is
  rotated, not to reading other runs' data. Rotate by changing the value in
  both services and restarting. The 60s dispatch-token TTL bounds replay
  capture to one minute, and one-shot consumption makes even that fail.
- **Network scope.** The runner binds loopback by default and the compose
  fragment publishes no host ports on an `internal: true` network, so only
  containers on that network (the gateway) can reach it. Do not add
  `ports:`, do not attach it to a public network, and do not put it behind
  the public reverse proxy. A rootless daemon socket
  (`/run/user/1000/docker.sock`) reduces host impact further and is a
  drop-in replacement for the mount source.
- **What this does not prove.** The sidecar moves Docker access off the
  gateway; it does not sandbox the container workloads themselves beyond
  the documented `docker run` flags, and it does not authenticate the
  *agent* — policy approval still happens in the gateway before dispatch.

## Follow-ups

- Service-layer threading of store/scope handles (issue #80-part2) waits
  for PR #67 to merge; the remote path accepts the same future handles
  without changing this contract. Verified follow-up to file after merge.
- Live end-to-end against a real daemon (no daemon in CI for this path;
  tests use mocked transports and a mocked Docker CLI).
