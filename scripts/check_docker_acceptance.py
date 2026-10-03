#!/usr/bin/env python3
"""Static M2 acceptance verifier: Docker executor + run_command (issues #35/#36).

Checks structural acceptance properties without a Docker daemon:
pinned image digest, hardening flags, mount hygiene (no socket/home/creds),
run_command docker-only gating, helper re-validation, fail-closed strings,
single entry point, gateway/runner agreement on job construction (issue #106),
dedicated CI workflow, and skip-cleanly fixtures.

Since #106 the image pin, hardening flag list, helper source, staging, and
copy-back walk live in ``backend/scopewatch/docker_job.py``, which the gateway
and the executor-runner sidecar both import; those checks read that file and
additionally assert neither caller re-declares the flag list.

Usage: python3 scripts/check_docker_acceptance.py [--quiet]
Exit 0 when every check passes, 1 otherwise.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
EXEC_DOCKER = REPO / "backend" / "scopewatch" / "executor_docker.py"
# Shared Docker job construction, imported by both the gateway and the
# executor-runner sidecar (issue #106). The image pin, hardening flag list,
# helper source, staging, and copy-back walk are checked here.
DOCKER_JOB = REPO / "backend" / "scopewatch" / "docker_job.py"
RUNNER = REPO / "deploy" / "executor-runner" / "runner.py"
RUNNER_DOCKERFILE = REPO / "deploy" / "executor-runner" / "Dockerfile"
EXECUTOR = REPO / "backend" / "scopewatch" / "executor.py"
POLICY = REPO / "backend" / "scopewatch" / "policy.py"
DOCKERFILE = REPO / "backend" / "executor" / "Dockerfile"
WORKFLOW = REPO / ".github" / "workflows" / "docker-executor.yml"
CONFTEST = REPO / "backend" / "tests" / "conftest.py"

CHECKS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    CHECKS.append((name, ok, detail))


def main() -> int:
    quiet = "--quiet" in sys.argv[1:]
    try:
        docker_src = EXEC_DOCKER.read_text(encoding="utf-8")
        job_src = DOCKER_JOB.read_text(encoding="utf-8")
        runner_src = RUNNER.read_text(encoding="utf-8")
        runner_dockerfile_src = RUNNER_DOCKERFILE.read_text(encoding="utf-8")
        executor_src = EXECUTOR.read_text(encoding="utf-8")
        policy_src = POLICY.read_text(encoding="utf-8")
        dockerfile_src = DOCKERFILE.read_text(encoding="utf-8")
        workflow_src = WORKFLOW.read_text(encoding="utf-8")
        conftest_src = CONFTEST.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        print(f"MISSING FILE: {exc.filename}")
        return 1

    # Job construction (flag list, helper, image pin, staging, copy-back) is
    # shared between the gateway and the runner sidecar (issue #106), so these
    # static checks read backend/scopewatch/docker_job.py. The gates that
    # decide whether to dispatch stay in executor_docker.py.
    m = re.search(r'DOCKER_IMAGE = \(\s*"([^"]+)"\s*"([^"]+)"', job_src)
    image = "".join(m.groups()) if m else ""
    check("image-has-digest-pin", "@sha256:" in image, image[:60])
    digest = image.split("@sha256:")[1] if "@sha256:" in image else ""
    check("image-digest-64hex", len(digest) == 64
          and all(c in "0123456789abcdef" for c in digest), digest[:16])
    check("image-slim-python-base", image.startswith("python:3.12-slim"), image[:40])
    check("dockerfile-matches-pin", digest != "" and digest in dockerfile_src,
          "CI image FROM digest")
    check("no-floating-latest", ":latest" not in image, image[:40])
    # The runner must not restate the pin; it takes it from the shared module.
    check("runner-shares-the-pin", digest != "" and digest not in runner_src,
          "runner imports the pin instead of repeating it")
    check("runner-imports-the-pin", "DOCKER_IMAGE as RUNNER_DOCKER_IMAGE" in runner_src,
          "runner aliases the shared pin")

    # --- 2. Hardening flags in build_docker_command ---
    for flag in ["--rm", "--network", "none", "--read-only", "--tmpfs", "/tmp",
                 "--user", "65534:65534", "--cap-drop", "ALL",
                 "--security-opt", "no-new-privileges", "--pids-limit", "64",
                 "--memory", "256m", "--memory-swap", "--cpus",
                 "--workdir", "/workspace"]:
        check(f"flag:{flag}", f'"{flag}"' in job_src, "build_docker_command")
    check("no-privileged", "--privileged" not in job_src, "never privileged")
    # The flag list must exist once, not twice (issue #106).
    for caller, src in (("executor_docker", docker_src), ("runner", runner_src)):
        check(f"flag-list-single-source:{caller}",
              '"--cap-drop"' not in src,
              "flag list must live only in docker_job.py")

    # --- 3. Mount hygiene: only the workspace copy ---
    check("mount-workspace-rw",
          f"{{workspace_copy}}:{{CONTAINER_WORKSPACE}}:rw" in job_src
          and 'CONTAINER_WORKSPACE = "/workspace"' in job_src,
          "single :rw mount onto the container workspace")
    for bad, label in [("docker.sock", "no-docker-socket"),
                       ("Path.home()", "no-home-mount"),
                       (".ssh", "no-ssh-mount")]:
        check(label, bad not in job_src, label)
    for token in ["AWS_", "NEBIUS_", "SSH_AUTH_SOCK", "GITHUB_TOKEN"]:
        check(f"no-cred-{token}", token not in job_src, "no credential leak")

    # --- 4. run_command docker-only ---
    check("policy-docker-gate", "RULE_RUN_COMMAND_REQUIRES_DOCKER" in policy_src,
          "policy gate rule")
    check("policy-unsupported-op", "run_command' requires SCOPEWATCH_EXECUTOR=docker"
          in policy_src, "policy deny message")
    check("executor-refuses-local-run-command",
          "requires the Docker executor" in executor_src, "local refusal")
    check("shell-metacharacter-code", "SHELL_METACHARACTER" in policy_src,
          "reason code present")
    check("command-not-allowed-code", "COMMAND_NOT_ALLOWED" in policy_src,
          "reason code present")
    for marker in [";", '"|"', '"`"', '"$("']:
        check(f"metachar-{marker}", marker in policy_src, "pre-parse rejection")

    # --- 5. Helper re-validates /workspace ---
    for token, label in [("relative_to", "helper-containment"),
                         ("shell=False", "helper-no-shell"),
                         ("TimeoutExpired", "helper-timeout"),
                         ("...[truncated", "helper-truncation-marker"),
                         ("SCOPEWATCH_WORKSPACE", "helper-workspace-env"),
                         ("/workspace", "helper-workspace-path")]:
        check(label, token in job_src, "in-container helper")

    # --- 6. Fail-closed, single entry ---
    check("fail-closed-message", "failing closed" in docker_src.lower(),
          "daemon-unavailable message")
    check("no-silent-fallback", "never fall" in executor_src.lower()
          or "never falls back" in executor_src, "no silent local fallback")
    check("single-entry-dispatch",
          ('get_executor_backend() == "docker"' in executor_src)
          or ('get_executor_backend()' in executor_src and 'backend == "docker"' in executor_src),
          "execute_action dispatch")
    check("stored-decision-required", "Direct execution without policy evidence"
          in docker_src, "docker stored-decision gate")
    check("per-run-container-name", "scopewatch-{uuid" in docker_src
          or 'f"scopewatch-{uuid' in docker_src, "unique container names")
    check("best-effort-remove", "_best_effort_remove" in docker_src,
          "container cleanup")
    check("run-label", "scopewatch.run" in job_src, "per-run label")

    # --- 6b. The runner imports the shared module and nothing else (#106) ---
    check("runner-imports-shared-job-module",
          "from scopewatch.docker_job import" in runner_src,
          "runner uses the shared job module")
    check("runner-ships-shared-module",
          "backend/scopewatch/docker_job.py" in runner_dockerfile_src,
          "runner image COPYs the shared module")

    # --- 7. CI workflow + skip-cleanly ---
    check("workflow-exists", WORKFLOW.exists(), str(WORKFLOW))
    for token, label in [("SCOPEWATCH_EXECUTOR=docker", "workflow-selects-docker"),
                         ("-k docker", "workflow-runs-docker-tests"),
                         ("ubuntu-latest", "workflow-ubuntu"),
                         ("docker build", "workflow-builds-image")]:
        check(label, token in workflow_src, ".github/workflows/docker-executor.yml")
    check("skip-cleanly-fixture", "pytest.skip" in conftest_src
          and "requires_docker" in conftest_src, "daemon tests skip")

    # --- 8. New acceptance artefacts present ---
    check("m2-docker-tests", (REPO / "backend" / "tests"
          / "test_m2_docker_acceptance.py").exists(), "new #35 tests")
    check("m2-run-command-tests", (REPO / "backend" / "tests"
          / "test_m2_run_command_acceptance.py").exists(), "new #36 tests")
    check("operator-doc", (REPO / "docs" / "operations"
          / "docker-executor.md").exists(), "operator doc")

    failed = [c for c in CHECKS if not c[1]]
    if not quiet:
        print(f"M2 Docker acceptance static checks: "
              f"{len(CHECKS) - len(failed)}/{len(CHECKS)} passed")
        for name, ok, detail in CHECKS:
            print(f"  {'PASS' if ok else 'FAIL'}  {name}"
                  + (f"  ({detail})" if detail and not ok else ""))
    else:
        for name, ok, detail in CHECKS:
            if not ok:
                print(f"FAIL {name} ({detail})")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
