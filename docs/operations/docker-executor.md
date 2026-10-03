# Docker executor — operator guide (M2 #35 / #36)

How to run, isolate, and troubleshoot the Docker-isolated executor and the
allowlisted `run_command` operation. Describes what exists on `main` now;
plans live in issues, not here.

## Backend selection

`execute_action` (`backend/scopewatch/executor.py`) is the single entry
point. It dispatches on `SCOPEWATCH_EXECUTOR`:

| Value | Backend | Behaviour |
| --- | --- | --- |
| unset / empty / `local` | `LocalWorkspaceExecutor` | Current synthetic behaviour; default for tests. |
| `docker` | `DockerExecutor` (`executor_docker.py`) | Per-run hardened container. |
| anything else | local | Falls through to the local backend (no third path). |

The variable is stripped and lower-cased, so `DOCKER` and `  docker  `
both select the container backend.

## Container hardening (per run)

Each dispatch builds one `docker run` (`build_docker_command`) with:

- `--rm`, plus `--label scopewatch.executor=docker` and
  `--label scopewatch.run=<run_id>`, and a unique
  `--name scopewatch-<12 hex chars>` per dispatch;
- `--network none`, `--read-only`, `--tmpfs /tmp`,
  `--user 65534:65534` (Debian `nobody`, non-root),
  `--cap-drop ALL`, `--security-opt no-new-privileges`,
  `--pids-limit 64`, `--memory 256m` (+ `--memory-swap 256m`),
  `--cpus 1.0`, `--workdir /workspace`;
- exactly one mount: a staged copy of the run's own workspace at
  `/workspace:rw`. Host home, the Docker socket, SSH material, and cloud
  credential variables are never mounted or referenced.

There are two copies in play (issue #117). `workspace_root` handed to the
executor is already the **run's own workspace**, a copy of the synthetic
scenario fixture owned by that run (`scopewatch/workspaces.py`). Staging
(`_stage_workspace_copy`) then makes a throwaway per-dispatch copy of *that*
directory for the container. The container never mounts the shared fixture,
and never mounts another run's workspace.

The staged copy (`_stage_workspace_copy`) is a fresh `mkdtemp`
`scopewatch-run-*` directory per dispatch, opened to UID 65534 with world
bits so the non-root container can write; the source tree keeps its own
permissions. The staging root is removed in a `finally` block and the
container is removed best-effort (`docker rm -f`), including on timeouts.
Successful `write_text`/`run_command` results sync back to the **run's**
workspace; reads, listings, and simulated deletes do not. Copy-back never
touches the shared fixture or a sibling run's directory.

## Image pin

`DOCKER_IMAGE` in `docker_job.py` is a digest-pinned
`python:3.12-slim-bookworm@sha256:<64 hex>` base, and is re-exported from
`executor_docker.py` for existing callers. The executor-runner sidecar imports
the same constant, so there is one pin to bump (issue #106). CI builds
`backend/executor/Dockerfile` (same base digest + the hashed backend
lock, so `python -m pytest` exists inside) and selects it at runtime with
`SCOPEWATCH_EXECUTOR_IMAGE=scopewatch-executor:ci`. Constructor arguments
beat the env var; the env var beats the pin. Rebuild the CI image after
any `backend/requirements.lock` bump.

## Stored decision first

Both backends enforce the stored-decision gate on the host *before* the
daemon is touched: missing decisions raise `ExecutionSecurityError`,
`DENY` returns `NOT_EXECUTED` with the policy reason code, `HOLD` needs an
`APPROVED`/`CONSUMED` approval for the same action, and `network_request`
is always refused. The in-container helper re-validates that every path
resolves inside `/workspace` (defence in depth).

## Docker unavailable: fail closed

If no daemon is reachable (`docker info` fails) or the CLI is missing,
dispatch returns `FAILED` / `EXECUTION_FAILED` from `docker-executor` and
**never falls back to local execution** — the host workspace is left
untouched. A hung `docker run` is removed and reported the same way.

## `run_command` allowlist (#36)

- The agent submits `arguments["command"]` (string, parsed gateway-side
  with `shlex.split`) or `arguments["argv"]` (list); execution always uses
  the argv list with `shell=False`. String form wins when both are present.
- Before parsing, the raw string (or each argv item) is rejected with
  `SHELL_METACHARACTER` if it contains `; & | > < ` `` ` `` `$(`,
  newline, carriage return, or NUL.
- `argv` must start with a per-scope `allowed_commands` prefix
  (e.g. `["python", "-m", "pytest"]`); otherwise `COMMAND_NOT_ALLOWED`.
- Post-prefix arguments honour the path rules (absolute, `..`, NUL,
  symlink escape, blocked paths). `cwd` is the `resource` field:
  `/workspace` (`""` or `"."`) or a validated subdirectory.
- `run_command` is docker-only: any other backend yields
  `UNSUPPORTED_OPERATION` in policy, and the local executor refuses it too.
- Scopes may gate commands with `requires_approval` (whole operation) or
  `commands_requiring_approval` (per-prefix) → `HOLD` for reviewer sign-off.
- Limits: default 60 s timeout (clamped to 1–300 s; garbage/NaN/Inf fall
  back to 60 s), 64 KiB stdout/stderr caps each with a `...[truncated N
  bytes]` marker, exit code in the receipt (`EXECUTED` on 0,
  `FAILED`/`NONZERO_EXIT` otherwise — never a policy denial).

## Running the suites

```bash
PYTHONPATH=backend python3 -m pytest backend/tests -q            # all backends
SCOPEWATCH_EXECUTOR=docker python3 -m pytest backend/tests -k docker -q
python3 scripts/check_docker_acceptance.py                       # static checks
./scripts/validate.sh --quick
```

Daemon tests use the `requires_docker` fixture and skip cleanly without a
daemon; the dedicated CI workflow `.github/workflows/docker-executor.yml`
(pre-pull pin → build CI image → `pytest -k docker`) runs them on
`ubuntu-latest`.

## Troubleshooting

| Symptom | Meaning |
| --- | --- |
| `FAILED` / `EXECUTION_FAILED`, executor `docker-executor` | Daemon unreachable, CLI missing, timeout, or malformed helper output. Failing closed is the intended behaviour — check `docker info`. |
| `NOT_EXECUTED` + policy code | Policy denied/held; not an executor fault. Resolve via approvals. |
| `ExecutionSecurityError` | Missing decision, unapproved HOLD, network op, or local `run_command` — caller bug, fix the dispatch. |
| `COMMAND_TIMEOUT` / `NONZERO_EXIT` | The command itself timed out or exited non-zero inside the container; the isolation worked. |
| Leftover `scopewatch-run-*` in temp | Staging cleanup only runs after dispatch; a killed host process can orphan one — safe to delete. |

Evidence honesty: the dashboard shows attempted, allowed, held, denied,
approved, executed, and failed as distinct states; decision events precede
execution events. Missing reasoning stays visibly missing. Never claim
guaranteed containment outside the gateway.
