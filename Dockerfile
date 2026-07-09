# syntax=docker/dockerfile:1

# --- Build stage -------------------------------------------------------------------
FROM python:3.12-slim AS builder

WORKDIR /build

RUN pip install --no-cache-dir --upgrade pip

COPY pyproject.toml ./
COPY when2leave ./when2leave
COPY README.md ./

RUN pip install --no-cache-dir --prefix=/install .

# --- Runtime stage -------------------------------------------------------------------
FROM python:3.12-slim AS runtime

LABEL org.opencontainers.image.title="when2leave" \
      org.opencontainers.image.description="Watches your Nextcloud calendar and tells you when to leave, based on real-time location and live travel time." \
      org.opencontainers.image.source="https://github.com/arnyminerz/calendar-notifier" \
      org.opencontainers.image.licenses="MIT"

# tzdata: TZ env var support; curl: container HEALTHCHECK.
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata curl \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd --gid 1000 when2leave \
    && useradd --uid 1000 --gid when2leave --shell /usr/sbin/nologin --create-home when2leave

COPY --from=builder /install /usr/local

WORKDIR /app

RUN mkdir -p /data && chown when2leave:when2leave /data

USER when2leave

ENV DATABASE_PATH=/data/when2leave.db \
    HTTP_HOST=0.0.0.0 \
    HTTP_PORT=8080 \
    PYTHONUNBUFFERED=1

VOLUME ["/data"]
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl -fsS "http://127.0.0.1:${HTTP_PORT}/health" || exit 1

ENTRYPOINT ["when2leave"]
