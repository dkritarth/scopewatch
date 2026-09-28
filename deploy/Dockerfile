# Scopewatch demo gateway image (issue #42).
#
# Serves the FastAPI gateway AND the static reviewer dashboard from one
# image (the gateway mounts ../frontend itself; see backend/scopewatch/app.py).
# No secrets are baked in: provider keys and the demo token arrive only via
# environment files on the VM (compose `env_file`). No demo fixture in this
# repo contains real credentials.
FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    SCOPEWATCH_DB_PATH=/data/scopewatch.db \
    SCOPEWATCH_WORKSPACE_ROOT=/workspace \
    PYTHONPATH=/app/backend

WORKDIR /app

# System deps kept minimal on purpose: no docker CLI, no curl, no sudo.
# (The gateway image must NOT be able to start sibling containers; the
# executor runs in-process in `local` mode. See RUNBOOK.md "Executor
# isolation" and compose.yaml comments.)
RUN useradd -r -u 10001 -m -s /usr/sbin/nologin appuser

COPY backend/requirements.txt ./backend/requirements.txt
RUN pip install --no-cache-dir -r ./backend/requirements.txt

COPY backend/ ./backend/
COPY frontend/ ./frontend/
COPY demo/scenarios/ ./demo/scenarios/
COPY scripts/seed_demo.py ./scripts/seed_demo.py
COPY deploy/docker-entrypoint.sh ./deploy/docker-entrypoint.sh

RUN chmod +x ./deploy/docker-entrypoint.sh \
    && mkdir -p /data /workspace \
    && chown -R appuser:appuser /app /data /workspace

USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import sys,urllib.request,json; \
d=json.load(urllib.request.urlopen('http://127.0.0.1:8000/api/v1/health',timeout=4)); \
sys.exit(0 if d.get('status')=='ok' else 1)"

ENTRYPOINT ["./deploy/docker-entrypoint.sh"]
CMD ["python", "-m", "uvicorn", "scopewatch.app:app", "--host", "0.0.0.0", "--port", "8000"]
