# Executor-runner sidecar: the ONLY image that mounts the Docker socket.
# Pinned base matches the gateway Docker backend pin (Debian bookworm slim).
FROM python:3.12-slim-bookworm@sha256:782412e85d0f0984994c290652577d4018aff08145c85b262bb63dc0c7522254

WORKDIR /app

# Build context is the repo root (see compose.executor-runner.yaml) so the two
# shared modules can be copied in from their single authored locations rather
# than vendored here: the Docker job module (issue #106) and the pre-dispatch
# gate (issue #105). Only these files ship; the sidecar deliberately holds no
# other gateway code.
COPY backend/scopewatch/__init__.py /app/scopewatch/__init__.py
COPY backend/scopewatch/docker_job.py /app/scopewatch/docker_job.py
COPY backend/scopewatch/dispatch_gate.py /app/scopewatch/dispatch_gate.py
COPY deploy/executor-runner/runner.py /app/runner.py

# The runner needs the Docker CLI to talk to the mounted socket (or a
# rootless daemon via DOCKER_HOST). Install the CLI only — no daemon here.
RUN apt-get update \
    && apt-get install -y --no-install-recommends docker-cli \
    && rm -rf /var/lib/apt/lists/*

# Import-constraint check (#106, #105): both shared modules must stay
# stdlib-only, because this sidecar is stdlib-only. They are COPYed as package
# members so runner.py can import them by module path; nothing else from the
# gateway package is shipped. If either ever needs a third-party import the
# image build fails here instead of at runtime on the runner host.
RUN python3 -c "import sys; sys.path.insert(0, '/app'); \
    from scopewatch.docker_job import helper_code, build_docker_command; \
    from scopewatch.dispatch_gate import authorize_dispatch; \
    assert 'import json, os, subprocess, sys' in helper_code(); \
    assert authorize_dispatch(None, None).code == 'NO_DECISION'"

# Byte-compile ahead of time so a syntax error in a shared module fails the
# build rather than the first dispatch.
RUN python3 -m compileall -q /app/runner.py /app/scopewatch/docker_job.py \
    /app/scopewatch/dispatch_gate.py

# Internal-only port; the compose fragment publishes nothing to the host.
EXPOSE 8091

ENV EXECUTOR_RUNNER_HOST=0.0.0.0 \
    EXECUTOR_RUNNER_PORT=8091 \
    EXECUTOR_RUNNER_WORKSPACE=/workspace

# Runs as root inside this minimal container because the Docker socket is
# root-owned by default; see README.md risk section. The container has no
# other mounts, no public ports, and no credentials baked in.
CMD ["python3", "/app/runner.py"]