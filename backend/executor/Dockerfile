# Scopewatch executor image for `run_command` scenarios.
#
# Pinned base digest (identical to DOCKER_IMAGE in
# backend/scopewatch/executor_docker.py) plus the full hashed backend lock,
# so `python -m pytest` and friends work inside the container. The slim base
# ships no test runner, which scenario 10 (`python -m pytest`) needs.
#
# Built in CI only:
#   docker build -f backend/executor/Dockerfile -t scopewatch-executor:ci .
# Selected at runtime with SCOPEWATCH_EXECUTOR_IMAGE; the daemon-less suites
# never need it. Rebuild after any backend/requirements.lock bump.
FROM python:3.12-slim-bookworm@sha256:782412e85d0f0984994c290652577d4018aff08145c85b262bb63dc0c7522254
COPY backend/requirements.lock /tmp/backend-requirements.lock
RUN pip install --no-cache-dir --require-hashes -r /tmp/backend-requirements.lock
