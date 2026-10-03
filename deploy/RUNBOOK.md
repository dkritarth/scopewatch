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
                        v  (compose `public` network)
                      gate :8080  (deploy/gate/gate.py, stdlib only)
                        |  enforces: demo token (401), per-IP rate limit,
                        |  concurrent-run cap, per-run turn cap, daily budget
                        v  (compose `internal` network, no published ports)
                    gateway :8000 (FastAPI + static reviewer dashboard)
                        |  SCOPEWATCH_EXECUTOR=local (no Docker socket anywhere)
                        v
              volumes: scopewatch-data (/data/sqlite), scopewatch-workspace
              egress: HTTPS to the model provider (Nebius/OpenRouter) only
```

Key properties:

- The gateway has **no Docker socket, no docker CLI, no published ports**.
  The only inbound path from the internet is `caddy -> gate -> gateway`. The
  gate additionally binds `127.0.0.1:8080` on the VM host for the operator's
  own verify commands; that binding is loopback-only.
- Public reads (`GET /api/v1/health`, dashboard, run/approval/event reads)
  need **no token** so judges can browse. Every mutating `/api/*` call needs
  the `X-Demo-Token` header (otherwise **401**). The gate validates this
  header, then forwards it over the private Compose network so the gateway's
  native demo guard can validate the same request independently.
- Both layers refuse in the same envelope,
  `{"error": "<code>", "message": "<sentence>"}`. Their *policies* still
  differ — see "Gate vs gateway: what is still duplicated" below.
- Gate budgets/caps are in-memory in a **single** gate replica. Restarting
  the gate resets counters (documented, accepted for a demo).
- Budget accounting is a **conservative proxy** (run/action counts per UTC
  day), not true token metering — the gate cannot see model tokens from
  HTTP alone. Native per-profile token metering is a filed follow-up (see
  "Known limits and follow-ups"). With `mock` profiles no model calls happen
  at all, so scripted scenarios always work.

### Gate vs gateway: what is still duplicated

The gate and `backend/scopewatch/demo_guards.py` are two implementations of the
same idea, kept deliberately separate in this PR because removing the gate is a
design decision, not a task (#107). Recorded honestly:

| Aspect | Gate (`deploy/gate/gate.py`) | Gateway (`backend/scopewatch/demo_guards.py`) |
| --- | --- | --- |
| Refusal envelope | flat `{"error", "message"}` | flat `{"error", "message"}` — **aligned in #107** |
| Misconfigured numerics | falls back to the default value (**fail open**) | refuses new runs/actions with `503` (**fail closed**) |
| Misconfiguration code | `gate_misconfigured` | `demo_guard_misconfigured` |
| Budget basis | HTTP call counts, plus one `GET /api/v1/runs` poll | real run state from SQLite, plus per-profile model tokens |
| Token validation | first line of defence, forwards the header | re-validates every forwarded request |

Consequence: the gate can let traffic through with a cap that the gateway would
have refused, and it cannot see actual token spend. A client must treat the two
as one policy only for the envelope, not for the limits. Unifying them is
tracked as a follow-up, not fixed here.

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
| `DOMAIN` | your DNS name, e.g. `demo.example.com` (`localhost` = local dry-run, plain HTTP) |
| `DEMO_TOKEN` | long random string for judges: `python3 -c "import secrets;print(secrets.token_urlsafe(32))"` |
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
docker compose ps            # all three services "running (healthy)"
```

Then check each layer. Which address answers depends on what you are allowed
to reach, so pick the column that matches your situation:

| Check | On the VM host | From any machine |
| --- | --- | --- |
| Gate liveness | `curl -fsS http://127.0.0.1:8080/healthz` | `curl -fsS https://<DOMAIN>/healthz` |
| Gateway health through the gate | `curl -fsS http://127.0.0.1:8080/api/v1/health` | `curl -fsS https://<DOMAIN>/api/v1/health` |
| Gateway, bypassing the gate | `docker compose exec gateway python -c "..."` (full command below) | not reachable, by design |

The right-hand column needs DNS already pointing at the VM. Health is a public
read, so none of these need the token.

Why the VM-host commands work (this is what #107 fixed):

- The gate publishes **one** port, `127.0.0.1:8080:8080`, in `compose.yaml`.
  The `127.0.0.1:` prefix is load-bearing: the binding is on the loopback
  interface, so the gate answers on the VM and nothing else can reach it from
  the internet. The public path is still `caddy -> gate -> gateway`.
- The gateway publishes **no** ports at all, on purpose: the gate is its only
  inbound path. Check it from inside its own container with
  `docker compose exec`. The runbook used to tell you to curl
  `127.0.0.1:8000`, which nothing ever bound.
- `curl http://127.0.0.1:8080/api/v1/health` reaches the *gateway's* health
  endpoint through the gate. Reads are public, so it needs no token, and it
  proves the gate-to-gateway path rather than just a live gate process.
- Never widen that mapping to `"8080:8080"`. It would expose the gate directly
  and let a client skip Caddy's TLS and `X-Forwarded-For` handling, which the
  gate trusts for per-IP rate limiting.

The gateway-direct check, spelled out:

```bash
docker compose exec gateway python -c "import sys,urllib.request,json;d=json.load(urllib.request.urlopen('http://127.0.0.1:8000/api/v1/health',timeout=10));print(d);sys.exit(0 if d.get('status')=='ok' else 1)"
```

First start seeds the synthetic workspace + scripted scenarios once per data
volume (marker `/data/.seeded-scripted-v1`); seeding makes **no model
calls**. Open `https://<DOMAIN>/` — the reviewer dashboard loads with no login
and no token; runs, events, and evidence are public reads. Open
`https://<DOMAIN>/api/v1/health` — `{"status":"ok",...}`.

For the judge's mutating controls (submit a simulated action, resolve an
approval) the dashboard asks for the reviewer token itself: it reveals a
**Reviewer access** panel whenever `GET /api/v1/health` reports
`"demo_mode": true`, and again on the first `401`. The token is held in memory
for that page load only, is never written to browser storage, and is sent only
as `X-Demo-Token` on mutating requests. The shell equivalent is the
`curl -H "X-Demo-Token: $TOKEN"` form in section 4.

Auto-restart: every service has `restart: unless-stopped` plus `healthcheck`s,
so the stack survives VM reboots (with Docker set to start on boot, which the
`docker-ce` package does by default) and crashed containers are restarted
automatically. `caddy` depends on a healthy `gate`, which depends on a healthy
`gateway` — unhealthy dependencies are not routed to.

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

Both enforcement layers answer in the same envelope,
`{"error": "<code>", "message": "<plain sentence>"}`, so one parser handles a
refusal whether the gate or the gateway's own demo guard produced it. That is
the only place the two layers are aligned: their numerics still differ (see
"Known limits and follow-ups").

On the VM host, `./scripts/smoke-demo.sh http://127.0.0.1:8080 "$TOKEN"` hits
the loopback-published gate. `./scripts/smoke-demo.sh` with no arguments does
exactly that, so the script's default now works.

Budget-exhaustion check (proves the cost guard refuses with a clear message).
Do this against a THROWAWAY stack so judging budgets are untouched:

```bash
# On the VM, in a scratch copy:
cp -r scopewatch /tmp/budget-check && cd /tmp/budget-check/deploy
printf '\nDEMO_MAX_RUNS_PER_DAY=1\n' >> .env   # keep the same DEMO_TOKEN
DOMAIN=localhost docker compose -p budgetcheck up -d --build
./scripts/smoke-demo.sh --exhaust-budget http://127.0.0.1:8080 "$TOKEN"
# want: ... 201 for the first run, then 429 + budget_exhausted + clear message
docker compose -p budgetcheck down -v   # throwaway volumes deleted
```

Rate-limit check (optional): create runs in a tight loop from one IP with the
token; after `DEMO_RATE_LIMIT_RUNS_PER_MIN_PER_IP` creations you get `429`
+ `Retry-After`. Turn-cap check: submit more than `DEMO_MAX_TURNS_PER_RUN`
actions to one run; the excess gets `429 turn_cap`.

## 5. Operate

```bash
cd scopewatch/deploy
docker compose ps                       # health + restart status
docker compose logs -f gate gateway     # live logs (tokens are never logged)
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
docker compose up -d gateway gate   # both validators load the new token
# Old token stops working immediately; update the testing instructions.
```

Rotate a provider key: same flow — edit `.env`, then
`docker compose up -d gateway gate` (gateway recreates; in-flight runs live
in the persistent volume, gate budgets reset).

## 6. Executor isolation decision and residual risk

Requirement (issue #42): the gateway must start executor containers WITHOUT
mounting the Docker socket into anything the agent can reach.

Decision for this stack:

- **Pin `SCOPEWATCH_EXECUTOR=local` and mount no socket anywhere.**
  `deploy/compose.yaml`, both Dockerfiles, and the Caddyfile contain zero
  host daemon-control-socket mounts. This runbook spells that filename as
  `docker[.]sock` rather than embedding the literal token (verify:
  `grep -R "docker[.]sock" deploy/compose.yaml deploy/Dockerfile deploy/gate/ deploy/Caddyfile`
  returns nothing — the only mentions in `deploy/` are these explanatory
  paragraphs), and the gateway image
  has no docker CLI. The agent's actions execute in-process in the
  unprivileged gateway container, confined to the synthetic `/workspace`
  volume with workspace-containment checks in code, under `read_only: true`,
  `cap_drop: ALL`, `no-new-privileges`, a 256-process limit, and 1 GB RAM.
- **Why not mount the socket into the gateway?** The socket is root on the
  host; anything the agent's HTTP traffic can reach (the gateway) must never
  hold it — a policy or approval bug would become a host takeover.
- **Path to full container isolation:** a dedicated `executor-runner`
  sidecar that ALONE holds the socket (or a rootless daemon) on the internal
  network, with the gateway calling it only with policy-approved digests.
  That needs a gateway-side remote-executor hook, which is a filed follow-up
  (see below) owned by the executor thread — it is deliberately NOT
  half-built here.

Residual risks you accept by running this demo:

1. `local` execution shares the gateway container's kernel/userns; a kernel
   or Python-sandbox escape is contained only by the container boundary
   (no-new-privs, dropped caps, read-only rootfs) — not by a second
   container. Mitigation: synthetic workspace only, no host mounts, no
   credentials in the container except provider keys in env.
2. Gate budgets are count-based proxies, not token metering; a pathological
   run could burn more tokens than its action count suggests. Mitigation:
   low daily caps + turn cap + provider-side spend alerts (set these in the
   Nebius console).
3. Reads are public (dashboard browsing without a token) — acceptable because
   all demo content is synthetic; do not paste real data into the demo.
4. Single gate replica holds budgets in memory; do not `--scale gate`.
5. The gate binds `127.0.0.1:8080` on the host so the runbook verify commands
   work. Anything running **on the VM** can reach the gate directly and set
   `X-Forwarded-For`, so per-IP rate limiting does not apply to host-local
   traffic. Acceptable: that traffic already has host access. Keep the
   `127.0.0.1:` prefix; widening it exposes the gate to the internet.

## 7. Cost guard recap

- Daily caps: `DEMO_MAX_RUNS_PER_DAY` / `DEMO_MAX_ACTIONS_PER_DAY` (UTC day),
  refused as `429 budget_exhausted` with a plain-language message.
- `mock` profiles make zero model calls — scripted judging never spends.
- Live judging (`nebius-demo`) spends per agent/auditor call; keep caps low
  and set a Nebius budget alert. True per-profile token metering is a
  follow-up (below).

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

## Known limits and follow-ups (filed from #42)

- Gateway-native demo-token auth (401/429 in `backend/scopewatch/app.py`).
- Gateway-native per-profile daily TOKEN metering + run refusal with a clear
  message (replacing the gate's count proxy).
- Remote-executor hook + socket-holding `executor-runner` sidecar
  (unlocks `SCOPEWATCH_EXECUTOR=docker` without a socket in the gateway).
- Optional: shared-state (Redis) gate for multi-replica budgets.
- The gate and the gateway demo guards still implement the same controls twice,
  with different numerics and different fail-open/fail-closed behaviour. Decide
  whether to delete the gate or to make it a thin token-checking proxy; tracked
  as a follow-up from #107, not fixed here.

## What was NOT verified by the author

- No Nebius VM was available: `docker compose up`, real TLS issuance, and
  the Caddyfile were reviewed but not executed end to end.
- No live provider keys: live-model spend was never exercised; only `mock`.
- Browser check of the dashboard through Caddy was not performed.
- #107's port and envelope changes were checked two ways: a real gate process
  and a real demo-mode gateway were run locally and the whole
  `smoke-demo.sh` script passed against them, and
  `deploy/test_compose_layout.py` asserts that no runbook command curls a port
  compose does not publish. `docker compose config` was NOT run (no Docker
  daemon on the machine that made the change), so the loopback bind itself and
  these commands are still unproven against a real VM.
If you hit anything, file an issue with the failing command + output.
