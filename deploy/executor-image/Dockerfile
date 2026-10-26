# Scopewatch executor image for `run_command` coding scenarios (issue #302).
#
# Same pinned base digest as DOCKER_IMAGE in backend/scopewatch/docker_job.py,
# plus a hashed pytest install so `python -m pytest` works inside the
# container (the slim base ships no test runner). Built locally with
# scripts/build_executor_image.sh and selected via SCOPEWATCH_EXECUTOR_IMAGE.
FROM python:3.12-slim-bookworm@sha256:782412e85d0f0984994c290652577d4018aff08145c85b262bb63dc0c7522254
COPY deploy/executor-image/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir --require-hashes -r /tmp/requirements.txt
USER 65534:65534