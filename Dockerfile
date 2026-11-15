# syntax=docker/dockerfile:1

# The image is built in two stages. The builder installs the package
# into a virtualenv, from the lock file of the repository; the runtime
# stage copies that virtualenv and runs the service from it, so the
# image carries the built package and not the sources.
FROM python:3.15-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

# The tool that installs the dependencies is pinned by its digest: a
# tag can be moved, and a build must not depend on what the tag points
# to today.
COPY --from=ghcr.io/astral-sh/uv@sha256:5cc1805e2426484abca4ad46ea783bab281fdb225fd82d8ce6d66a81254bac85 /uv /usr/local/bin/uv

WORKDIR /build
ENV PATH="/opt/venv/bin:${PATH}"

# The lock file is copied with the manifest: the versions of the
# dependencies come from it and are not resolved again on every build.
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable


FROM python:3.15-slim AS runtime

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

# Only the virtualenv is copied: the package is installed in it, so the
# image carries the sources once and not twice.
COPY --from=builder /opt/venv /opt/venv

WORKDIR /app
COPY --chown=foresight:foresight config.example.toml ./config.example.toml

USER foresight

EXPOSE 8000

# The probe asks /healthz and not /api/health: the API sits behind the
# bearer token, so a probe without a token would be answered with a 401
# and the container would be reported unhealthy forever. The route
# answers without a token, and it says nothing about the dump. The port
# is the one the API really listens on, so a deployment that moved it
# is probed on the right port.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["foresight-healthcheck"]

ENTRYPOINT ["foresight"]
