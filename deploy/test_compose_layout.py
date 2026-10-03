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
RUNBOOK = DEPLOY_DIR / "RUNBOOK.md"

# Services that are allowed to reach the host network, and the host ports each
# is allowed to bind. Anything not listed here must publish nothing.
EXPECTED_PUBLISHED_PORTS = {
    "gate": ['"127.0.0.1:8080:8080"'],
    "caddy": ['"80:80"', '"443:443"', '"443:443/udp"'],
    "gateway": [],
}


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
