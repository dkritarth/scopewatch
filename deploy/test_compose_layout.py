"""Hosted-demo deployment regression checks.

Check the public ingress, private gateway/runner networks, project isolation,
and executable operator procedures without a Docker daemon. Compose config
is the authoritative parser check and is run separately when available.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess

import pytest

DEPLOY_DIR = Path(__file__).resolve().parent
COMPOSE = DEPLOY_DIR / "compose.yaml"
RUNNER_FRAGMENT = DEPLOY_DIR / "executor-runner" / "compose.executor-runner.yaml"
RUNBOOK = DEPLOY_DIR / "RUNBOOK.md"

# The executor-runner fragment's own network. It is `internal: true` (no route
# off the host) because the container holds the Docker socket.
RUNNER_NETWORK = "executor-runner"

# Services that are allowed to reach the host network, and the host ports each
# is allowed to bind. Anything not listed here must publish nothing.
EXPECTED_PUBLISHED_PORTS = {
    "caddy": ['"80:80"', '"443:443"', '"443:443/udp"'],
    "gateway": [],
}


def top_level_section(text: str, key: str) -> str:
    """Return the body of a top-level ``key:`` block, comments included."""
    start = re.search(rf"^{re.escape(key)}:\s*$", text, re.MULTILINE)
    assert start, f"no {key}: block"
    rest = text[start.end():]
    end = re.search(r"^\S", rest, re.MULTILINE)
    return rest[: end.start()] if end else rest


def declared_networks(text: str) -> dict[str, dict[str, str]]:
    """Map every top-level network name to its attributes, by indentation."""
    section = top_level_section(text, "networks")
    networks: dict[str, dict[str, str]] = {}
    current: str | None = None
    for line in section.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        entry = re.match(r"^  ([A-Za-z0-9._-]+):\s*(.*)$", line)
        if entry:
            current = entry.group(1)
            networks[current] = {}
            inline = entry.group(2).strip()
            if inline.startswith("{") and inline.endswith("}"):
                # Inline mapping form, e.g. `internal: {external: true}`.
                for part in inline[1:-1].split(","):
                    if ":" in part:
                        name, value = part.split(":", 1)
                        networks[current][name.strip()] = value.strip()
            continue
        attribute = re.match(r"^    ([A-Za-z0-9_-]+):\s*(\S+)\s*$", line)
        if attribute and current is not None:
            networks[current][attribute.group(1)] = attribute.group(2)
    return networks


def service_networks(text: str, name: str) -> list[str]:
    """Return the network names a service's ``networks:`` list attaches to."""
    block = service_block(text, name)
    match = re.search(r"^    networks:\s*$", block, re.MULTILINE)
    assert match, f"service {name!r} declares no networks:"
    names: list[str] = []
    for line in block[match.end():].splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        entry = re.match(r"^      - (\S+)", line)
        if not entry:
            break  # left the networks list
        names.append(entry.group(1))
    return names


def services_section(compose: str) -> str:
    """Return only the `services:` body, up to the next top-level key.

    Splitting on the bare word "networks:" would truncate at each service's own
    `networks:` key, so anchor on a top-level (column 0) key instead.
    """
    start = re.search(r"^services:\s*$", compose, re.MULTILINE)
    assert start, f"no services: block in {COMPOSE.name}"
    rest = compose[start.end():]
    end = re.search(r"^\S", rest, re.MULTILINE)
    return rest[: end.start()] if end else rest


def service_block(text: str, name: str) -> str:
    """Return the `services.<name>:` block of a compose file, by indentation."""
    match = re.search(rf"^  {re.escape(name)}:\s*$", text, re.MULTILINE)
    assert match, f"service {name!r} not found in {COMPOSE.name}"
    rest = text[match.end():]
    # A sibling service starts at the same two-space indent.
    end = re.search(r"^  \S", rest, re.MULTILINE)
    return rest[: end.start()] if end else rest


def raw_published_ports(block: str) -> list[str]:
    """Return the host-side mapping strings from a service's `ports:` list.

    Exactly as written in the file: ``${VAR:-default}`` substitutions are kept,
    so a caller can assert that a port is *overridable* rather than merely
    what it defaults to.
    """
    match = re.search(r"^    ports:\s*$", block, re.MULTILINE)
    if not match:
        return []
    entries: list[str] = []
    for line in block[match.end():].splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue  # blank line or a comment inside the list
        entry = re.match(r"^      - (\S+?)\s*(?:#.*)?$", line)
        if not entry:
            break  # left the ports list (next key at 4-space indent)
        entries.append(entry.group(1))
    return entries


def default_of(mapping: str) -> str:
    """Resolve ``${VAR:-default}`` substitutions to their shipped defaults."""
    return re.sub(r"\$\{[A-Za-z_][A-Za-z0-9_]*:-([^}]*)\}", r"\1", mapping)


def published_ports(block: str) -> list[str]:
    """Host-side mappings with ``${VAR:-default}`` resolved to the default.

    A parameterised port is still checked against the value it ships with, so
    the expectations below describe the live stack that operators run today.
    """
    return [default_of(mapping) for mapping in raw_published_ports(block)]


def host_port(mapping: str) -> int:
    """Host-side port of a compose mapping: ``80:80`` and ``127.0.0.1:8080:8080``.

    Raises for anything that is not a plain port mapping, so a structurally
    different compose file fails loudly instead of silently publishing nothing.
    """
    parts = mapping.strip('"').split(":")
    host_port_str = parts[1] if len(parts) >= 3 else parts[0]
    host_port_str = host_port_str.split("/")[0]  # drop a /udp or /tcp suffix
    assert host_port_str.isdigit(), f"unrecognised port mapping {mapping!r}"
    return int(host_port_str)


def published_by_service() -> dict[str, list[str]]:
    """Map every service name in compose.yaml to its published host mappings."""
    section = services_section(COMPOSE.read_text(encoding="utf-8"))
    names = re.findall(r"^  ([a-z][a-z0-9_-]*):\s*$", section, re.MULTILINE)
    return {name: published_ports(service_block(section, name)) for name in names}


def test_only_expected_services_publish_ports() -> None:
    published = published_by_service()
    for name, expected in EXPECTED_PUBLISHED_PORTS.items():
        assert published.get(name) == expected, (
            f"{name} must publish exactly {expected or 'nothing'}, "
            f"found {published.get(name)}"
        )
    # A new service must not publish a port without a decision recorded here.
    unexpected = sorted(set(published) - set(EXPECTED_PUBLISHED_PORTS))
    assert not unexpected, (
        f"new services {unexpected} need an EXPECTED_PUBLISHED_PORTS entry first"
    )


def test_the_gateway_stays_unpublished() -> None:
    """The gateway must never bind a host port: Caddy is its only public way in."""
    assert published_by_service().get("gateway") == [], (
        "gateway must publish no host ports"
    )
    block = service_block(COMPOSE.read_text(encoding="utf-8"), "gateway")
    assert not re.search(r"^\s*network_mode:\s*host", block, re.MULTILINE)


def test_every_loopback_curl_port_in_the_runbook_is_published() -> None:
    """The #107 acceptance criterion, as a regression guard.

    Every loopback URL the runbook tells an operator to curl must use a port
    some service actually publishes. `docker compose exec` URLs are excluded:
    they run inside a container's network namespace, not on the host.
    """
    published = set()
    for ports in published_by_service().values():
        for mapping in ports:
            published.add(host_port(mapping))
    assert published, "compose publishes nothing, so no runbook command could work"

    host_side = "\n".join(
        line
        for line in RUNBOOK.read_text(encoding="utf-8").splitlines()
        if "docker compose exec" not in line
    )
    referenced = {int(p) for p in re.findall(r"http://(?:127\.0\.0\.1|localhost):(\d+)", host_side)}
    assert referenced, "runbook no longer curls any loopback port; update this test"
    unpublished = sorted(referenced - published)
    assert not unpublished, (
        f"runbook curls loopback ports {unpublished} that compose never publishes"
    )


# ---------------------------------------------------------------------------
# Throwaway budget-check stack (issue #201)
#
# The runbook told the operator to bring up a second project with
# `-p budgetcheck`, but every volume and network carried a fixed top-level
# `name:` — which Compose does NOT scope to the project — and every host port
# was fixed. With the live stack up, the throwaway stack therefore:
#
# * could not bind 80/443 (caddy) or 127.0.0.1:8080 (gate);
# * pointed the smoke script at the LIVE gate, spending the judging budget and
#   failing the 429 expectation the check exists to prove;
# * mounted the live SQLite evidence volume, so two containers answered as
#   `gateway` on one shared network; and
# * risked `down -v` removing the live named volumes, i.e. the judged evidence.
#
# `docker compose -p budgetcheck config` on a real daemon is the authoritative
# check and was not run for this change (no Docker here); these are the
# dependency-free regression guards.
# ---------------------------------------------------------------------------

PROJECT_SCOPED_NAME = "${COMPOSE_PROJECT_NAME:-scopewatch-demo}"

# Host port knobs a second project must be able to override, and the live
# defaults that must stay unchanged for the shipped stack.
PORT_OVERRIDES = {
    "caddy": ["HTTP_HOST_PORT", "HTTPS_HOST_PORT"],
}


def _top_level_block(text: str, key: str) -> str:
    """Return the body of a top-level ``key:`` block."""
    start = re.search(rf"^{re.escape(key)}:\s*$", text, re.MULTILINE)
    assert start, f"no top-level {key}: block"
    rest = text[start.end():]
    end = re.search(r"^\S", rest, re.MULTILINE)
    return rest[: end.start()] if end else rest


def _declared_resource_names(text: str, key: str) -> dict[str, str]:
    """Map each top-level network/volume entry to its explicit ``name:``.

    The name is returned exactly as written, so a caller can assert both that it
    is project-scoped and what it resolves to.
    """
    names: dict[str, str] = {}
    current: str | None = None
    for line in _top_level_block(text, key).splitlines():
        entry = re.match(r"^  ([A-Za-z0-9._-]+):\s*(.*)$", line)
        if entry:
            current = entry.group(1)
            continue
        override = re.match(r"^\s+name:\s*(\S+)\s*$", line)
        if override and current:
            names[current] = override.group(1).strip('"')
    return names


def test_every_resource_name_is_project_scoped() -> None:
    """A fixed top-level ``name:`` is shared by every project on the host."""
    for compose in (COMPOSE, RUNNER_FRAGMENT):
        text = compose.read_text(encoding="utf-8")
        for key in ("networks", "volumes"):
            declared = _declared_resource_names(text, key)
            for entry, name in declared.items():
                assert name.startswith(PROJECT_SCOPED_NAME + "-"), (
                    f"{compose.name}: {key}.{entry} is named {name!r}, which "
                    "every project on the host shares. Scope it with "
                    "${COMPOSE_PROJECT_NAME:-scopewatch-demo}-... (issue #201)"
                )
    # The run-workspace volume is the one both files must agree on (#117), so
    # it must be project-scoped on both sides rather than only one.
    for compose in (COMPOSE, RUNNER_FRAGMENT):
        declared = _declared_resource_names(
            compose.read_text(encoding="utf-8"), "volumes"
        )
        assert "scopewatch-run-workspaces" in declared, (
            f"{compose.name} must name the shared run-workspace volume"
        )
        assert declared["scopewatch-run-workspaces"].startswith(
            PROJECT_SCOPED_NAME + "-"
        ), f"{compose.name}: the shared volume is not project-scoped"


def test_the_live_resource_names_are_unchanged_by_the_default() -> None:
    """Parameterising must not rename what the shipped deployment owns.

    An existing demo's volumes are ``scopewatch-demo-*``; a default that
    resolved to anything else would orphan the live evidence on the next
    `up -d --build`.
    """
    expected = {
        "networks": {
            "public": "scopewatch-demo-public",
            "internal": "scopewatch-demo-internal",
        },
        "volumes": {
            "scopewatch-data": "scopewatch-demo-data",
            "scopewatch-workspace": "scopewatch-demo-workspace",
            "scopewatch-run-workspaces": "scopewatch-demo-run-workspaces",
            "caddy-data": "scopewatch-demo-caddy-data",
            "caddy-config": "scopewatch-demo-caddy-config",
        },
    }
    text = COMPOSE.read_text(encoding="utf-8")
    for key, wanted in expected.items():
        declared = _declared_resource_names(text, key)
        resolved = {entry: default_of(name) for entry, name in declared.items()}
        assert resolved == wanted, (
            f"{key} names must resolve to the shipped values when "
            f"COMPOSE_PROJECT_NAME is unset; found {resolved}"
        )


def test_every_host_port_is_overridable() -> None:
    """A second project cannot share a host port with the live listeners."""
    text = COMPOSE.read_text(encoding="utf-8")
    for service, knobs in PORT_OVERRIDES.items():
        block = service_block(text, service)
        for mapping in raw_published_ports(block):
            overridden = re.findall(r"\$\{([A-Za-z_][A-Za-z0-9_]*):-", mapping)
            assert overridden, (
                f"{service} publishes {mapping!r} as a fixed host port, so a "
                "second project on this VM cannot bind it (issue #201). Use "
                "${SOME_PORT:-<live default>}."
            )
            for knob in overridden:
                assert knob in knobs, (
                    f"{service} uses unknown port knob {knob!r}; expected one of {knobs}"
                )


def _budget_check_step() -> str:
    """Return the documented budget-exhaustion shell step as text."""
    runbook = RUNBOOK.read_text(encoding="utf-8")
    section = runbook.split("Budget-exhaustion check", 1)[1]
    blocks = re.findall(r"```bash\n(.*?)```", section, re.S)
    assert blocks, "the budget-exhaustion step lost its bash block"
    return blocks[0]


@pytest.mark.parametrize("smoke_fails", [False, True], ids=["clean", "failure-cleans-up"])
def test_the_budget_check_step_runs_isolated_and_always_cleans_up(
    tmp_path: Path, smoke_fails: bool
) -> None:
    """Execute the documented step against fake binaries (no Docker needed).

    A fake `docker` and a fake `./scripts/smoke-demo.sh` record their argv and
    the knobs the runbook claims to set, so the step is checked as a program
    rather than as prose: right project, right ports, and `down -v` on failure
    too.
    """
    step = _budget_check_step()

    log = tmp_path / "calls.jsonl"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    docker = bin_dir / "docker"
    docker.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        "with open(os.environ['CALL_LOG'], 'a') as fh:\n"
        "    fh.write(json.dumps({'tool': 'docker', 'argv': sys.argv[1:],\n"
        "        'project': os.environ.get('COMPOSE_PROJECT_NAME'),\n"
        "        'http_port': os.environ.get('HTTP_HOST_PORT'), 'domain': os.environ.get('DOMAIN'), 'runs_per_day': os.environ.get('DEMO_MAX_RUNS_PER_DAY')}) + '\\n')\n",
        encoding="utf-8",
    )
    docker.chmod(0o755)

    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()
    smoke = scripts_dir / "smoke-demo.sh"
    smoke.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        "with open(os.environ['CALL_LOG'], 'a') as fh:\n"
        "    fh.write(json.dumps({'tool': 'smoke', 'argv': sys.argv[1:],\n"
        "        'project': os.environ.get('COMPOSE_PROJECT_NAME'),\n"
        "        'runs_per_day': os.environ.get('DEMO_MAX_RUNS_PER_DAY')}) + '\\n')\n"
        "sys.exit(1 if os.environ.get('SMOKE_FAILS') else 0)\n",
        encoding="utf-8",
    )
    smoke.chmod(0o755)

    env = {
        "PATH": f"{bin_dir}:{os.environ.get('PATH', '')}",
        "HOME": os.environ.get("HOME", ""),
        "TOKEN": "synthetic-demo-token",
        "CALL_LOG": str(log),
        "SMOKE_FAILS": "1" if smoke_fails else "",
    }
    proc = subprocess.run(
        ["bash", "-c", step],
        cwd=str(tmp_path),
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == (1 if smoke_fails else 0), proc.stderr

    calls = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    kinds = [call["tool"] for call in calls]
    # Config, initial start, budget recreation, smoke, and cleanup on either outcome.
    assert kinds == ["docker", "docker", "docker", "smoke", "docker"], kinds

    config, up, recreate, smoke_call, down = calls
    assert config["argv"][:3] == ["compose", "-p", "budgetcheck"]
    assert "config" in config["argv"]
    assert up["argv"][:3] == ["compose", "-p", "budgetcheck"]
    assert "up" in up["argv"]
    assert "gateway" in up["argv"] and "caddy" in up["argv"]
    assert up["domain"] == "http://localhost"  # explicit HTTP, no certificate
    assert up["runs_per_day"] == "200"  # offline seeding can finish
    assert recreate["argv"][:3] == ["compose", "-p", "budgetcheck"]
    assert recreate["argv"][-1] == "gateway"
    assert recreate["runs_per_day"] == "1"
    assert down["argv"][-2:] == ["down", "-v"]
    assert down["argv"][:3] == ["compose", "-p", "budgetcheck"]

    for call in calls:
        assert call["project"] == "budgetcheck", (
            "every compose call must name the throwaway project, or `down -v` "
            "can remove the live stack's volumes"
        )
    assert smoke_call["runs_per_day"] == "1", (
        "the throwaway budget cap must be set for the smoke test"
    )
    assert smoke_call["argv"][0] == "--exhaust-budget"
    assert smoke_call["argv"][1] == "http://localhost:18000", (
        f"the smoke test must hit the throwaway Caddy, not the live one: {smoke_call['argv']}"
    )


# ---------------------------------------------------------------------------
# Executor-runner overlay (issue #202)
#
# Two defects met here. The fragment redeclared the gateway's shared
# `internal` network with `internal: true`, which Compose applies to every
# service on it: the gateway lost its HTTPS egress to the model provider, so
# every live audit failed closed to HOLD and the live agent could not reach
# Nebius. And `compose.yaml` pinned `SCOPEWATCH_EXECUTOR: local` under
# `environment:`, which overrides `env_file:`, so setting
# `SCOPEWATCH_EXECUTOR=remote` in the VM `.env` did nothing at all.
#
# `docker compose config` on a real daemon is the authoritative check and was
# not run for this change (no Docker here); these are the dependency-free
# regression guards.
# ---------------------------------------------------------------------------


def test_the_runner_fragment_does_not_cut_gateway_egress() -> None:
    """No shared network may be marked internal by the merged fragment."""
    fragment = RUNNER_FRAGMENT.read_text(encoding="utf-8")
    fragment_networks = declared_networks(fragment)
    assert fragment_networks, "the fragment declares no networks"

    for name, attributes in fragment_networks.items():
        if name == RUNNER_NETWORK:
            continue
        assert attributes.get("internal") != "true", (
            f"the fragment marks shared network {name!r} internal: true, which "
            "removes egress from every service on it, including the gateway's "
            "route to the model provider (issue #202)"
        )

    base = COMPOSE.read_text(encoding="utf-8")
    assert declared_networks(base).get("internal", {}).get("internal") != "true", (
        "the gateway's provider-facing `internal` network must stay routable"
    )


def test_the_runner_network_is_egress_less_and_shared_with_the_gateway() -> None:
    """The socket-holding sidecar gets its own no-egress network, and the
    gateway is on it so remote dispatch can still reach the runner."""
    fragment = RUNNER_FRAGMENT.read_text(encoding="utf-8")
    base = COMPOSE.read_text(encoding="utf-8")

    assert declared_networks(fragment)[RUNNER_NETWORK].get("internal") == "true", (
        "the runner's own network must be internal: true (no route off the host)"
    )
    # Declared in the base file too, so the gateway service stays valid when the
    # fragment is not merged, and so both files resolve to one network.
    assert RUNNER_NETWORK in declared_networks(base), (
        f"{COMPOSE.name} must declare {RUNNER_NETWORK!r} for the gateway to join"
    )
    assert RUNNER_NETWORK in service_networks(base, "gateway"), (
        "the gateway must attach to the runner network or remote dispatch cannot reach it"
    )
    assert RUNNER_NETWORK in service_networks(fragment, "executor-runner")
    # Least privilege: nothing else joins it.
    assert RUNNER_NETWORK not in service_networks(base, "caddy"), (
        "Caddy must not join the runner-only network"
    )


def test_the_gateway_executor_backend_is_selectable_from_the_env_file() -> None:
    """`environment:` must not pin the executor over `env_file:` (#202)."""
    base = COMPOSE.read_text(encoding="utf-8")
    gateway = service_block(base, "gateway")
    match = re.search(
        r"^\s+SCOPEWATCH_EXECUTOR:\s*(\S+)\s*$", gateway, re.MULTILINE
    )
    assert match, "the gateway service must set SCOPEWATCH_EXECUTOR"
    value = match.group(1).strip('"')
    assert value == "${SCOPEWATCH_EXECUTOR:-local}", (
        "SCOPEWATCH_EXECUTOR must default to local through variable "
        f"interpolation so .env can select remote; found {value!r}"
    )


def test_caddy_routes_directly_to_the_only_demo_enforcement_layer() -> None:
    """The public path must reach native guards without a second policy."""
    base = COMPOSE.read_text(encoding="utf-8")
    assert set(published_by_service()) == {"caddy", "gateway"}
    assert "reverse_proxy gateway:8000" in (DEPLOY_DIR / "Caddyfile").read_text()
    assert set(service_networks(base, "caddy")) & set(service_networks(base, "gateway"))
    assert re.search(r"gateway:\s*\n\s+condition: service_healthy", service_block(base, "caddy"))
    assert not (DEPLOY_DIR / "gate").exists()


def test_hosted_demo_cannot_start_with_guards_disabled() -> None:
    gateway = service_block(COMPOSE.read_text(), "gateway")
    assert re.search(r"^      DEMO_TOKEN: \$\{DEMO_TOKEN:\?", gateway, re.MULTILINE)


@pytest.mark.parametrize("name,default", [
    ("DEMO_MAX_CONCURRENT_RUNS", 5),
    ("DEMO_MAX_TURNS_PER_RUN", 40),
    ("DEMO_RATE_LIMIT_RUNS_PER_MIN_PER_IP", 6),
    ("DEMO_MAX_RUNS_PER_DAY", 200),
    ("DEMO_MAX_ACTIONS_PER_DAY", 5000),
])
def test_demo_numeric_overrides_reach_the_enforcing_gateway(name: str, default: int) -> None:
    gateway = service_block(COMPOSE.read_text(), "gateway")
    aliases = {
        "DEMO_RATE_LIMIT_RUNS_PER_MIN_PER_IP": "DEMO_RATE_LIMIT_PER_MIN",
        "DEMO_MAX_RUNS_PER_DAY": "DEMO_DAILY_RUN_BUDGET",
        "DEMO_MAX_ACTIONS_PER_DAY": "DEMO_DAILY_ACTION_BUDGET",
    }
    fallback = f"${{{aliases[name]}:-{default}}}" if name in aliases else str(default)
    assert f"{name}: ${{{name}:-{fallback}}}" in gateway


def test_smoke_default_targets_caddys_published_http_port() -> None:
    script = (DEPLOY_DIR / "scripts/smoke-demo.sh").read_text()
    assert 'BASE="${1:-http://localhost:${HTTP_HOST_PORT:-80}}"' in script
    assert '"${HTTP_HOST_PORT:-80}:80"' in raw_published_ports(
        service_block(COMPOSE.read_text(), "caddy")
    )


@pytest.mark.parametrize("canonical,fallback", [
    ("SCOPEWATCH_API_KEY", "API_KEY"),
    ("SCOPEWATCH_REVIEWER_TOKEN", "REVIEWER_TOKEN"),
    ("SCOPEWATCH_REVIEWER_ID", "DEFAULT_REVIEWER_IDENTITY"),
    ("SCOPEWATCH_ALLOW_RESET", "ALLOW_DEMO_RESET"),
    ("SCOPEWATCH_CORS_ALLOW_ORIGINS", "CORS_ALLOW_ORIGINS"),
    ("SCOPEWATCH_CORS_ALLOW_CREDENTIALS", "CORS_ALLOW_CREDENTIALS"),
])
def test_compose_gateway_passes_through_canonical_auth_and_cors_env(canonical: str, fallback: str) -> None:
    gateway = service_block(COMPOSE.read_text(encoding="utf-8"), "gateway")
    assert canonical in gateway, f"gateway service must contain {canonical}"
    assert fallback in gateway, f"gateway service must contain {fallback} fallback"
