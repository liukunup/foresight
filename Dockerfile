# syntax=docker/dockerfile:1

# The image is built in two stages. The builder resolves the
# dependencies and installs the package into a virtualenv; the runtime
# stage copies that virtualenv and runs the service from it, so the
# image carries the built package and not only the sources.
FROM python:3.13-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /build
RUN uv venv /opt/venv
ENV PATH="/opt/venv/bin:${PATH}"

COPY pyproject.toml README.md ./
COPY src ./src
RUN uv pip install --python /opt/venv/bin/python /build


FROM python:3.13-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:${PATH}" \
    FORESIGHT_CONFIG=/app/config.toml

# The service never runs as root. The user owns /app and the data
# directory, because the compose file mounts ./data there: the database
# and the downloaded media have to be writable by the service.
RUN groupadd --gid 10001 foresight \
    && useradd --uid 10001 --gid 10001 --create-home foresight \
    && mkdir -p /app/data \
    && chown -R foresight:foresight /app

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app
COPY --chown=foresight:foresight src ./src
COPY --chown=foresight:foresight config.example.toml ./config.example.toml

USER foresight

EXPOSE 8000

# The probe asks /metrics and not /api/health: the API sits behind the
# bearer token, so a probe without a token would be answered with a 401
# and the container would be reported unhealthy forever. The metrics
# endpoint is mounted outside /api and answers without a token — unless
# api.metrics_token is set, in which case the probe needs that token.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; \
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/metrics', timeout=4).status == 200 else 1)"

ENTRYPOINT ["foresight"]
