# Executor-runner sidecar: the ONLY image that mounts the Docker socket.
# Pinned base matches the gateway Docker backend pin (Debian bookworm slim).
FROM python:3.12-slim-bookworm@sha256:782412e85d0f0984994c290652577d4018aff08145c85b262bb63dc0c7522254

WORKDIR /app
COPY runner.py /app/runner.py

# The runner needs the Docker CLI to talk to the mounted socket (or a
# rootless daemon via DOCKER_HOST). Install the CLI only — no daemon here.
RUN apt-get update \
    && apt-get install -y --no-install-recommends docker-cli \
    && rm -rf /var/lib/apt/lists/*

# Internal-only port; the compose fragment publishes nothing to the host.
EXPOSE 8091

ENV EXECUTOR_RUNNER_HOST=0.0.0.0 \
    EXECUTOR_RUNNER_PORT=8091 \
    EXECUTOR_RUNNER_WORKSPACE=/workspace

# Runs as root inside this minimal container because the Docker socket is
# root-owned by default; see README.md risk section. The container has no
# other mounts, no public ports, and no credentials baked in.
CMD ["python3", "/app/runner.py"]
