# Demo access gate image (issue #42). Stdlib only: no pip install step,
# minimal attack surface, runs as a non-root user.
FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN useradd -r -u 10002 -m -s /usr/sbin/nologin gateuser

WORKDIR /srv/gate
COPY deploy/gate/gate.py ./gate.py
RUN chown -R gateuser:gateuser /srv/gate

USER gateuser

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import sys,urllib.request,json; \
d=json.load(urllib.request.urlopen('http://127.0.0.1:8080/healthz',timeout=4)); \
sys.exit(0 if d.get('status')=='ok' else 1)"

CMD ["python", "gate.py"]
