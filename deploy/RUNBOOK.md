# Scopewatch hosted demo runbook (issue #42)

Deploy, operate, and retire the public demo on ONE Linux VM. Follow top to
bottom on a clean Ubuntu 24.04 VM; every step was written to work by copy
and paste. Time estimate: ~30 minutes plus DNS propagation.

> Scope honesty: this stack mediates only actions submitted through the
> Scopewatch gateway. It does not intercept arbitrary host or agent
> operations, and the dashboard labels what is observed vs inferred. Never
> claim guaranteed detection or prevention outside the gateway.

## 0. Architecture (what you are about to run)

```
internet --443/80--> caddy (automatic TLS, Caddyfile)
                        |
                        v  (compose `internal` network, no published ports)
                    gateway :8000 (FastAPI + reviewer dashboard)
                        |  demo token, rate limit, concurrency and turn caps,
                        |  daily request budgets, per-profile token metering
                        |  local executor by default; optional remote runner
                        |  on the separate egress-less `executor-runner` network
                        v
              volumes: scopewatch-data (/data/sqlite),
                       scopewatch-workspace (synthetic scenario fixture),
                       scopewatch-run-workspaces (/runs, one directory per run)
              gateway egress: model provider calls
```

Key properties:

- The gateway has no Docker socket, Docker CLI, or published ports. Its only
  public ingress is `caddy -> gateway`. Caddy replaces incoming untrusted
  `X-Forwarded-For` headers; the gateway uses that header for per-IP limits.
- Reads of health, the dashboard, runs, approvals, and events are public.
  Every mutating `/api/*` request requires `X-Demo-Token`, otherwise `401`.
  Compose requires a non-empty `DEMO_TOKEN`, so a missing token cannot
  accidentally enable the gateway's local-development mode. The container
  entrypoint also rejects whitespace-only tokens before seeding or serving.
- Demo guard refusals use `{"error": "<code>", "message": "<sentence>"}`.
  Malformed numeric controls refuse new runs/actions with `503` and
  `demo_guard_misconfigured`. On a fresh volume, invalid limits can also
  prevent offline scenario seeding and stop startup, which fails closed.
- Run one gateway replica. Rate windows, daily request counts, and turn
  counts are in memory and reset on restart. Concurrency uses real SQLite
  run rows. Per-profile model token metering is separate from request counts;
  see [demo guards](../docs/operations/demo-guards.md) for configuration.
- Every run owns a workspace copy under `/runs`. The scenario workspace is
  the copy source, so concurrent runs cannot overwrite each other's files.

### Enforcement decision, issue #155

Use the gateway's native guards as the single enforcement layer. They have
real run state and model token usage, so a second HTTP counting policy adds
no reliable information. Caddy handles TLS, compression, and proxying only.
All limits and refusal codes come from `backend/scopewatch/demo_guards.py`
and the gateway token ledger. Existing VM deployments should run
`docker compose up -d --build --remove-orphans` when updating to this stack
so obsolete containers stop too. No volume names change.

## 1. Provision the VM

Requirements: 1 VM, Ubuntu 24.04, 2 vCPU / 4 GB RAM / 25 GB disk, public IP,
ports 80+443 reachable, a DNS A (or AAAA) record for your demo domain.

```bash
# 1. Point DNS at the VM, e.g.:
#    demo.example.com.  300  IN  A  203.0.113.10
#    (Caddy will not get a certificate until DNS resolves + ports are open.)

# 2. On the VM as a sudo user:
sudo apt-get update && sudo apt-get install -y ca-certificates curl git
sudo install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg | \
  sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
  https://download.docker.com/linux/ubuntu $(lsb_release -cs) stable" | \
  sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
sudo apt-get update && sudo apt-get install -y \
  docker-ce docker-ce-cli containerd.io docker-compose-plugin
sudo usermod -aG docker "$USER"   # log out and back in afterwards

# 3. Check out the repo (public clone is fine; it contains no secrets):
git clone https://github.com/dkritarth/scopewatch.git
cd scopewatch/deploy
```

Verify Docker works: `docker compose version` should print v2.x.

## 2. Configure (secrets live ONLY in this step)

```bash
cd scopewatch/deploy
cp .env.example .env
chmod 600 .env
nano .env   # or vi
```

Set at minimum:

| Variable | Value |
| --- | --- |
| `DOMAIN` | your DNS name, e.g. `demo.example.com` (`http://localhost` = local dry-run, plain HTTP) |
| `DEMO_TOKEN` | shared demo secret for demo mutating requests |
| `REVIEWER_TOKEN` | dedicated credential for human reviewers/judges resolving holds (issue #185) |
| `API_KEY` | dedicated agent API key for action submissions |
| `SCOPEWATCH_ALLOW_RESET` | set to `true` to enable `/demo/reset` (default `false`) |
| `SCOPEWATCH_AGENT_PROFILE` / `SCOPEWATCH_AUDITOR_PROFILE` | `mock` for a zero-cost scripted demo, or `nebius-demo` for live NVIDIA Nemotron via Nebius |
| `NEBIUS_API_KEY` | only when using `nebius-demo`; paste the key into `.env` on the VM, never into git/chat |
| abuse/cost caps | defaults are sane; lower `DEMO_MAX_RUNS_PER_DAY` for a tight judging window |

Rules:

- The filled-in `.env` is **gitignored** (`deploy/.env`) and has `0600`
  permissions. Double-check with `git status --short` — `.env` must never
  appear.
- Never bake keys into images (`docker compose build` reads `.env` only for
  non-secret build knobs like `DOMAIN`; keys are injected at container
  start via `env_file`).
- Never paste real keys into issues, PRs, or logs.

## 3. Deploy

```bash
cd scopewatch/deploy
docker compose up -d --build
docker compose ps            # gateway healthy, caddy running
```

Check the public path using the configured domain:

```bash
curl -fsS https://<DOMAIN>/api/v1/health
# Local dry-run only, configured with DOMAIN=http://localhost:
curl -fsS http://localhost:80/api/v1/health
```

Caddy publishes ports 80 and 443. The gateway publishes no host port.
The HTTPS check needs working DNS and TLS; the local check uses Caddy's
explicit HTTP site. Health is public and needs no token.

The gateway-direct check, spelled out:

```bash
docker compose exec gateway python -c "import sys,urllib.request,json;d=json.load(urllib.request.urlopen('http://127.0.0.1:8000/api/v1/health',timeout=10));print(d);sys.exit(0 if d.get('status')=='ok' else 1)"
```

First start seeds the synthetic workspace + scripted scenarios once per data
volume (marker `/data/.seeded-scripted-v1`); seeding makes **no model
calls**, but it does create one run, so it needs the per-run workspace volume
to be writable — see below. Open `https://<DOMAIN>/` — the reviewer dashboard
loads with no login
and no token; runs, events, and evidence are public reads. Open
`https://<DOMAIN>/api/v1/health` — `{"status":"ok",...}`.

For the judge's mutating controls (submit a simulated action, resolve an
approval) the dashboard asks for the reviewer token itself: it reveals a
**Reviewer access** panel whenever `GET /api/v1/health` reports
`"demo_mode": true`, and again on the first `401`. The token is held in memory
for that page load only, is never written to browser storage, and is sent only
as `X-Demo-Token` on mutating requests. The shell equivalent is the
`curl -H "X-Demo-Token: $TOKEN"` form in section 4.

Both services use `restart: unless-stopped`. Caddy waits for a healthy
gateway at startup. Compose dependency checks govern startup only; they do
not remove an upstream that becomes unhealthy later.

### Per-run workspaces (issue #117)

Each run gets its own copy of the scenario workspace in the
`scopewatch-run-workspaces` volume, mounted at `/runs`. The
`scopewatch-workspace` volume is the copy **source** and is never written. That
is what stops run A from reading run B's output and stops container copy-back
from polluting the baseline.

This is a hard deployment requirement, not a nicety. The gateway service runs
`read_only: true` with volumes for `/data`, `/workspace` and `/runs` only, so
`SCOPEWATCH_RUN_WORKSPACES_DIR` **must** point inside one of those. If it points
anywhere else the path can neither exist nor be created, and because
`docker-entrypoint.sh` is `set -euo pipefail` and seeds on first start, the
container aborts rather than starting degraded.

```bash
# Where run workspaces actually live, and that the fixture volume stayed pristine:
docker compose exec gateway ls -1 /runs | head
docker compose exec gateway python -c "import os;print(os.environ['SCOPEWATCH_RUN_WORKSPACES_DIR'])"
docker compose exec gateway ls -A /workspace      # no per-run directories here
```

Two symptoms worth recognising:

| Symptom | Cause | Fix |
| --- | --- | --- |
| Gateway exits on first start, log ends `Could not initialize an isolated workspace for this run` | `SCOPEWATCH_RUN_WORKSPACES_DIR` is not a writable mounted path | point it at a mounted volume (see below) |
| Every `POST /api/v1/runs` returns **503** `RUN_WORKSPACES_ROOT_UNAVAILABLE` | same, after seeding was skipped by an existing data volume | fix the path, then `docker compose down -v && docker compose up -d` |
| Every remote dispatch returns **500** `run workspace unavailable` | gateway and `executor-runner` disagree on the shared path | see "Shared path contract" below |

The values are a two-sided contract across a container boundary, so they are
**not** operator knobs — they are not in `deploy/.env.example` as settings, only
documented there. To relocate them, change all three together:

| Where | Key | Shipped value |
| --- | --- | --- |
| `deploy/compose.yaml` (gateway) | `SCOPEWATCH_RUN_WORKSPACES_DIR` | `/runs` |
| `deploy/executor-runner/compose.executor-runner.yaml` | `EXECUTOR_RUNNER_WORKSPACE` | `/runs` |
| both compose files | volume | `scopewatch-run-workspaces:/runs` |

#### Shared path contract with executor-runner

The sidecar is a separate container, so a shared path is only shared through a
shared **mount**. The gateway announces `run_workspace` as a bare per-run
directory name; the runner resolves it under `EXECUTOR_RUNNER_WORKSPACE` and
refuses to fall back to any other root. If the two services resolve that name
to different directories, every remote dispatch fails closed with 500 — correct
fail-closed behaviour pointed at the wrong path.

The sidecar deliberately does **not** mount `scopewatch-workspace`. It holds the
Docker socket and runs as root; giving it write access to the pristine fixture
buys nothing and reopens what the per-run copies exist to prevent.

`SCOPEWATCH_EXECUTOR` is an operator choice, defaulted to `local` in
`deploy/compose.yaml` and overridable in the deploy `.env`. Selecting `remote`
means merging the sidecar fragment (`deploy/executor-runner/README.md`), which
puts the runner on its own `internal: true` `executor-runner` network that the
gateway also joins — that network is why the gateway keeps its provider egress
(issue #202). With `local` (the shipped default) the sidecar is not part of the
demo stack today (section 6); the contract above matters for the day the
fragment is merged.

`backend/tests/test_run_workspaces_deployment.py` asserts this layout from the
compose files and exercises it against a simulated read-only container root. It
is a static check; `docker compose config` on a real daemon was not run for it.

#### May the scenario fixture be mounted read-only? (issue #173)

**The run-workspace side: yes, unconditionally.** A run workspace's permissions
are set by `workspaces.py` (`RUN_WORKSPACE_DIR_MODE` / `RUN_WORKSPACE_FILE_MODE`)
rather than inherited from the copy source, so a baseline with no write bit at
all still produces a workspace the gateway can write into. That is what used to
break: `shutil.copytree` ends each directory it copies with `copystat`, so a
read-only fixture handed every run a read-only workspace and every
`create_run` failed with `Could not initialize an isolated workspace for this
run` — a message that blames the run rather than the source tree's mode.

Note the trigger is read-only **permission bits**, not a read-only **mount**. A
`:ro` mount changes no inode's mode, so `:ro` on its own would never have
tripped this; `chmod -R a-w` on the host before the volume is first populated
does, and that is the step you would reach for while hardening the baseline. If
you do it, expect run workspaces to come out `0700` regardless.

**The seeding side: not yet, and the blocker is unrelated to #173.**
`scopewatch-workspace` is *not* `:ro` in the shipped compose file, and flipping
it today would break the demo. `deploy/Dockerfile` creates an empty `/workspace`
and ships no fixture in the image; `docker-entrypoint.sh` runs
`scripts/seed_demo.py` on first start, and `seed_workspace_files()` `mkdir`s and
`write_text`s every scenario file into that volume. Under `:ro` those writes
fail with `EROFS`, and because the entrypoint is `set -euo pipefail` the gateway
container aborts on first start — the same failure shape as a bad
`SCOPEWATCH_RUN_WORKSPACES_DIR`.

Making `:ro` viable is therefore a deployment change, not a code change: bake
the fixture into the image (a `COPY` seeding `/workspace`, so the named volume
is populated with content and seeding becomes a no-op), or move seeding to
`/data`. Either way it needs a live `docker compose config` and a first
`docker compose up -d` to confirm, neither of which was available where this
was written, so **nothing in the compose file was changed**. After seeding the
gateway itself writes nothing to `/workspace` — every write goes to `/runs`.

## 3b. Credential Separation & Reviewer Flow (issue #185)

Scopewatch strictly separates **agent** and **reviewer** roles:
- **Agent credentials** (`X-API-Key` or `X-Demo-Token`) can submit actions and runs, but CANNOT resolve holds. Any attempt by an agent credential to call `/api/v1/approvals/{id}/approve` or `deny` is refused with HTTP 403 (`agent credentials are not permitted`).
- **Reviewer credentials** (`X-Reviewer-Token` / `REVIEWER_TOKEN`) are required to approve or deny held actions when authentication is configured.
- **Judge / Reviewer UI**: Judges enter their `REVIEWER_TOKEN` in the **Reviewer Access** panel on the dashboard. The credential is held only in memory for the active browser session and never stored in cookies or localStorage.
- **Strict Content-Type**: Approval endpoints require `Content-Type: application/json` and reject invalid content types with HTTP 415.
- **Demo Reset**: `/demo/reset` is disabled by default (HTTP 403). It requires `SCOPEWATCH_ALLOW_RESET=true` plus a valid reviewer credential, and clears `reasoning_audits` in addition to runs and events.

## 4. Verify the demo (acceptance checks)

From any machine (token needed only where noted):

```bash
export BASE=https://demo.example.com TOKEN='<paste DEMO_TOKEN>'

# Health + dashboard load WITHOUT a token:
curl -fsS $BASE/api/v1/health
curl -fsS $BASE/ | head -c 200

# Run creation WITHOUT a token -> 401 with a clear message:
curl -sS -X POST $BASE/api/v1/runs -H 'Content-Type: application/json' \
  -d '{"name":"nope","task_scope":{"task_description":"x","created_at":"2026-01-01T00:00:00Z"}}' \
  -w '\nHTTP %{http_code}\n'
# want: HTTP 401 + {"error":"demo_token_required","message":"..."}

# Or run everything at once (needs the stack reachable + token in $TOKEN):
./scripts/smoke-demo.sh $BASE "$TOKEN"
# PASS lines for: health, dashboard, 401, authed 201 + cleanup.
```

Demo guard refusals use one envelope,
`{"error": "<code>", "message": "<plain sentence>"}`.
The smoke script defaults to `http://localhost:80`, Caddy's published HTTP
port, for a local dry-run with `DOMAIN=http://localhost`. For a TLS deployment,
pass its public HTTPS URL explicitly, as above. An overridden `HTTP_HOST_PORT`
also changes the script's default port.

Budget-exhaustion check. Use a throwaway project so the live judging budgets,
evidence volume, and ports are untouched. Run from `scopewatch/deploy`.
Export both `COMPOSE_PROJECT_NAME` and `-p` to keep named resources isolated.
Caddy's HTTP port is 18000 and its HTTPS port is 18443 in this project.
The explicit `http://localhost` site requests no certificate.

```bash
# From scopewatch/deploy, with TOKEN set as in section 4:
(
  set -euo pipefail
  export COMPOSE_PROJECT_NAME=budgetcheck DOMAIN=http://localhost
  export DEMO_TOKEN="$TOKEN"
  export HTTP_HOST_PORT=18000 HTTPS_HOST_PORT=18443
  export DEMO_MAX_RUNS_PER_DAY=200              # allow offline first-start seeding
  export SCOPEWATCH_AGENT_PROFILE=mock SCOPEWATCH_AUDITOR_PROFILE=mock
  docker compose -p budgetcheck config --quiet
  trap 'docker compose -p budgetcheck down -v' EXIT
  docker compose -p budgetcheck up -d --build --wait gateway caddy
  # Recreate after seeding. The marker prevents reseeding; the new gateway
  # starts with empty in-memory counters and this one-run budget.
  export DEMO_MAX_RUNS_PER_DAY=1
  docker compose -p budgetcheck up -d --wait gateway
  ./scripts/smoke-demo.sh --exhaust-budget "http://localhost:${HTTP_HOST_PORT}" "$TOKEN"
  # want: 201 for the first run, then 429 + budget_exhausted + clear message
)
```

The smoke test reaches only the throwaway Caddy on port 18000. Inspect the
isolated names and ports before starting if needed:

```bash
COMPOSE_PROJECT_NAME=budgetcheck DOMAIN=http://localhost HTTP_HOST_PORT=18000 \
  HTTPS_HOST_PORT=18443 docker compose -p budgetcheck config | grep -E 'name:|published:'
# want: only budgetcheck-* volumes and networks, and ports 18000/18443
```

Inspect the live project without changing it:

```bash
docker compose -p scopewatch-demo config | grep -E 'name:|published:'
```

Use a project name that is not already in use. Cleanup always names
`-p budgetcheck`; omitting it could remove the live data volume.

Rate-limit check (optional): create runs in a tight loop from one IP with the
token; after `DEMO_RATE_LIMIT_RUNS_PER_MIN_PER_IP` creations you get `429`
+ `Retry-After`. Turn-cap check: submit more than `DEMO_MAX_TURNS_PER_RUN`
actions to one run; the excess gets `429 turn_cap`.

## 5. Operate

```bash
cd scopewatch/deploy
docker compose ps                       # health + restart status
docker compose logs -f gateway     # live logs (tokens are never logged)
docker compose logs --since 1h caddy    # TLS/access issues
```

Update to a new release (downtime ~1 min):

```bash
cd scopewatch
git pull --ff-only
cd deploy
docker compose up -d --build
docker compose ps   # all healthy again
```

Data volumes persist across updates; the seed marker prevents reseeding.

Reset demo data (full wipe + reseed from scratch):

```bash
./scripts/reset-demo.sh     # asks for confirmation, then down -v + up --build
```

Rotate the demo token (e.g. it leaked into a screenshot):

```bash
cd scopewatch/deploy
python3 -c "import secrets;print(secrets.token_urlsafe(32))"  # new value
nano .env            # replace DEMO_TOKEN
docker compose up -d gateway   # the validator loads the new token
# Old token stops working immediately; update the testing instructions.
```

Rotate a provider key: same flow — edit `.env`, then
`docker compose up -d gateway`. The gateway recreates, runs stay in the
persistent volume, and in-memory request counters reset.

## 6. Executor isolation decision and residual risk

Requirement (issue #42): the gateway must start executor containers WITHOUT
mounting the Docker socket into anything the agent can reach.

Decision for this stack:

- **Default to `SCOPEWATCH_EXECUTOR=local` and mount no socket anywhere.**
  `deploy/compose.yaml`, the gateway Dockerfile, and the Caddyfile contain zero
  host daemon-control-socket mounts. This runbook spells that filename as
  `docker[.]sock` rather than embedding the literal token (verify:
  `grep -R "docker[.]sock" deploy/compose.yaml deploy/Dockerfile deploy/Caddyfile`
  returns nothing — the only mentions in `deploy/` are these explanatory
  paragraphs), and the gateway image
  has no docker CLI. The agent's actions execute in-process in the
  unprivileged gateway container, confined to its synthetic `/runs` workspace
  copy with workspace-containment checks in code, under `read_only: true`,
  `cap_drop: ALL`, `no-new-privileges`, a 256-process limit, and 1 GB RAM.
  `remote` is selectable from the deploy `.env` (it is no longer pinned in
  `environment:`, which used to override the env file), but only with the
  sidecar fragment merged in — see below.
- **Why not mount the socket into the gateway?** The socket is root on the
  host; anything the agent's HTTP traffic can reach (the gateway) must never
  hold it — a policy or approval bug would become a host takeover.
- **Path to full container isolation:** a dedicated `executor-runner`
  sidecar that ALONE holds the socket (or a rootless daemon) on its own
  egress-less `executor-runner` network, with the gateway calling it only
  with policy-approved digests. The fragment exists
  (`deploy/executor-runner/compose.executor-runner.yaml`);
  the demo stack ships without it, so the shipped deployment stays on `local`.

Residual risks you accept by running this demo:

1. `local` execution shares the gateway container's kernel/userns; a kernel
   or Python-sandbox escape is contained only by the container boundary
   (no-new-privs, dropped caps, read-only rootfs) — not by a second
   container. Mitigation: synthetic workspace only, no host mounts, no
   credentials in the container except provider keys in env.
2. Run/action caps do not measure model spend. Configure the gateway
   per-profile token budgets too, plus provider-side spend alerts in the
   Nebius console.
3. Reads are public (dashboard browsing without a token) — acceptable because
   all demo content is synthetic; do not paste real data into the demo.
4. The single gateway replica holds request counters in memory. Restarting
   resets these counters; do not scale the gateway without shared accounting.

## 7. Cost guard recap

- Daily caps: `DEMO_MAX_RUNS_PER_DAY` / `DEMO_MAX_ACTIONS_PER_DAY` (UTC day),
  refused as `429 budget_exhausted` with a plain-language message.
- `mock` profiles make zero model calls — scripted judging never spends.
- Live judging (`nebius-demo`) spends per agent/auditor call; keep caps low
  and set a Nebius budget alert. Configure
  `SCOPEWATCH_DAILY_TOKEN_BUDGET_NEBIUS_DEMO` for native token metering.

## 8. Teardown after 2026-12-15

The demo must be offline after December 15, 2026 (hackathon judging ends;
no reason to pay for the VM or expose the demo).

```bash
cd scopewatch/deploy
docker compose down -v                      # stop + delete demo data volumes
docker system prune -af --volumes           # remove images/build cache
cd ~ && rm -rf scopewatch                   # remove repo + any local .env
# Then in your cloud console: delete the VM (stops billing), release the IP,
# revoke the NEBIUS_API_KEY used for the demo, and remove the DNS record.
```

Verify: `https://<DOMAIN>/` no longer resolves/loads; provider key revoked
in the Nebius console.

## Known limits

- Request limits use in-memory state. Restarting resets them, and multiple
  gateway replicas need shared accounting before they can enforce one cap.
- Token metering needs explicit per-profile budgets. Its default is unlimited;
  use `SCOPEWATCH_TOKEN_BUDGET_STORE` for persistence across restarts.
- The optional executor-runner deployment requires its own credentials and
  Compose fragment. See [executor operations](../docs/operations/docker-executor.md).

## Deployment verification limits

Structural tests check the service graph, published ports, and the documented
throwaway budget-check procedure. A real VM is still needed to verify binding,
TLS issuance, the dashboard through Caddy, and live-provider token spend.
All automated provider fixtures are synthetic and use mock transports.

### Safe scripted seed lifecycle (audit #289)

Fresh volumes seed six invoice scenarios without autoapproval. The four runs with
no pending requests are completed; the two HOLD runs remain reviewable and count
against the normal concurrency cap. Runtime guards remain enabled while seeding.
The v2 marker makes restarts idempotent. A legacy v1 marker means the old seed may
already have executed the injection: startup refuses automatic migration. Inspect
and explicitly migrate/reset that demo volume using the documented reset procedure;
the entrypoint never deletes or rewrites old evidence automatically.
