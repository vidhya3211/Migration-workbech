# AI Workbench API, job worker and migration job - one image, three commands. Non-root, no build tools at runtime.
#   api:     python -m uvicorn api.main:app ...      (default CMD)
#   worker:  python cli.py worker                    (scaled on Service Bus queue length by KEDA)
#   migrate: python cli.py migrate                   (release job, runs once before new revisions start)
FROM python:3.12-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app

FROM base AS deps
COPY . /src
RUN pip install --upgrade pip && pip install --target /deps "/src[headroom,mcp,azure]"

FROM base AS runtime
RUN useradd --uid 10001 --create-home aiwb && mkdir -p /tmp/aiwb && chown aiwb /tmp/aiwb
COPY --from=deps /deps /usr/local/lib/python3.12/site-packages
COPY --chown=aiwb:aiwb . /app
USER aiwb
# Production defaults; everything else (database host, blob account, Service Bus namespace, Entra ids) is set
# per environment by the Container App. Secrets (AIWB_*_KEY, AIWB_ADMIN_PASSWORD_HASH) come from Key Vault.
ENV AIWB__ENVIRONMENT=production AIWB__STORAGE__OBJECT_STORE=azure_blob AIWB__STORAGE__ENTRA_AUTH=true \
    AIWB__STORAGE__AUTO_MIGRATE=false AIWB__JOBS__BACKEND=service_bus AIWB__JOBS__EMBEDDED_WORKER=false \
    AIWB__OBSERVABILITY__OTEL_ENABLED=true TMPDIR=/tmp/aiwb
EXPOSE 8000
HEALTHCHECK CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/v1/health')" || exit 1
CMD ["python", "-m", "uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", \
     "--timeout-keep-alive", "75"]
