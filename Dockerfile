FROM python:3.12-slim-bookworm

WORKDIR /app

# Matches Ubuntu 24 runtime deps (ssh client, certs)
RUN apt-get update -qq \
    && apt-get install -y -qq --no-install-recommends \
        openssh-client ca-certificates bash curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt pyproject.toml README.md ./
COPY skills ./skills
COPY config ./config
COPY bin ./bin
COPY infrastructure ./infrastructure

RUN pip install --no-cache-dir -r requirements.txt \
    && pip install --no-cache-dir -e . \
    && chmod +x /app/bin/*.sh || true \
    && mkdir -p /app/.azom-data /app/logs

# Production-oriented defaults (override via compose/.env)
ENV AZOM_CONFIG_DIR=/app/config \
    AZOM_DATA_DIR=/app/.azom-data \
    AZOM_USE_MOCK=0 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/skills \
    DASHBOARD_HOST=0.0.0.0 \
    DASHBOARD_PORT=8080

EXPOSE 8080

# Run as non-root when image is used without host mounts that need root
RUN useradd --system --uid 10001 --home-dir /app --shell /usr/sbin/nologin azom \
    && chown -R azom:azom /app
USER azom

CMD ["python", "-m", "ecom_ops", "--help"]
