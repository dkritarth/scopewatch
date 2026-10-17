"""Structural checks on the hosted-demo compose file and runbook (#107).

The bug this guards against: `deploy/RUNBOOK.md` told an operator to curl
`http://127.0.0.1:8080/healthz` and `http://127.0.0.1:8000/api/v1/health`, but
`deploy/compose.yaml` published neither port, so every section-3 verify command
failed with connection-refused on a freshly provisioned VM.

These tests read the YAML as text rather than importing a YAML parser: the
locked dependency set (`backend/requirements.txt`) has no YAML library, and
adding one for three assertions would mean regenerating `requirements.lock`
under `--generate-hashes`. The assertions below are deliberately narrow and
fail loudly if the file's shape changes, so a silent false pass is not possible.

`docker compose config` on a real Docker CLI is the authoritative check and was
NOT run for this change (no Docker daemon available); these tests are the
dependency-free regression guard for the specific regression above.
"""

from __future__ import annotations

import re
from pathlib import Path

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
    "gate": ['"127.0.0.1:8080:8080"'],
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


def published_ports(block: str) -> list[str]:
    """Return the host-side mapping strings from a service's `ports:` list."""
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
    """The gateway must never bind a host port: the gate is its only way in."""
    assert published_by_service().get("gateway") == [], (
        "gateway must publish no host ports"
    )
    block = service_block(COMPOSE.read_text(encoding="utf-8"), "gateway")
    assert not re.search(r"^\s*network_mode:\s*host", block, re.MULTILINE)


def test_the_gate_binds_loopback_only() -> None:
    """`127.0.0.1:` is load-bearing: a bare `8080:8080` would expose the gate."""
    ports = published_by_service().get("gate") or []
    assert ports, "the gate must publish a port so the runbook can verify it"
    for mapping in ports:
        host_part = mapping.split(":", 1)[0].strip('"')
        assert host_part == "127.0.0.1", (
            f"gate mapping {mapping!r} is not loopback-bound; the public path is caddy -> gate"
        )


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
    referenced = {int(p) for p in re.findall(r"http://127\.0\.0\.1:(\d+)", host_side)}
    assert referenced, "runbook no longer curls any loopback port; update this test"
    unpublished = sorted(referenced - published)
    assert not unpublished, (
        f"runbook curls loopback ports {unpublished} that compose never publishes"
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
    for service in ("gate", "caddy"):
        attached = service_networks(base, service)
        assert RUNNER_NETWORK not in attached, (
            f"{service} must not join the runner-only network"
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
