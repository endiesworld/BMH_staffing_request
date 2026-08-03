# syntax=docker/dockerfile:1

# ---------------------------------------------------------------------------
# Stage 1 — builder: resolve and install dependencies into a virtualenv.
#
# Split from the runtime stage so that build tooling (uv, caches, any compiler
# a wheel might need) never reaches the shipped image. Only the finished .venv
# is copied forward.
# ---------------------------------------------------------------------------
FROM python:3.12-slim-bookworm AS builder

# Pinned by digest-bearing tag rather than :latest -- a reproducible build must
# not silently change because upstream released. Renovate bumps this.
COPY --from=ghcr.io/astral-sh/uv:0.12.0 /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Dependencies first, source second. Docker caches layers by content, so as
# long as pyproject.toml and uv.lock are unchanged this whole install is reused
# and editing a view costs a second instead of a full reinstall.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

# --frozen: install exactly what uv.lock pins and fail if it disagrees with
# pyproject.toml. Never re-resolve during a build -- the image must contain the
# versions that were tested, not whatever is newest at build time.


# ---------------------------------------------------------------------------
# Stage 2 — runtime
# ---------------------------------------------------------------------------
FROM python:3.12-slim-bookworm AS runtime

# PYTHONUNBUFFERED: without it Python block-buffers stdout when it is a pipe
# rather than a terminal, so `kubectl logs` shows nothing until the buffer
# fills -- during an incident it looks exactly like a hung process.
# PYTHONDONTWRITEBYTECODE: no .pyc litter; bytecode was compiled in the builder.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/app/.venv/bin:$PATH"

# Run as a non-root user. Containers run as root by default, which means a
# process escaping the container arrives as root on the node. Nothing here needs
# privilege: the app never writes to disk (static files are baked in at build,
# uploads are not implemented).
RUN groupadd --gid 1001 app \
 && useradd --uid 1001 --gid app --create-home --shell /usr/sbin/nologin app

WORKDIR /app

# The virtualenv from stage 1. psycopg[binary] ships prebuilt wheels, so there
# is no libpq or C toolchain to install here -- that choice in Unit 2 is what
# keeps this stage to a plain slim base.
COPY --from=builder --chown=app:app /app/.venv /app/.venv

COPY --chown=app:app manage.py docker-entrypoint.sh ./
COPY --chown=app:app config/ ./config/
COPY --chown=app:app accounts/ ./accounts/
COPY --chown=app:app servicing/ ./servicing/
COPY --chown=app:app templates/ ./templates/
COPY --chown=app:app static/ ./static/

RUN chmod +x docker-entrypoint.sh

# Build the static assets into the image.
#
# Two build-time-only environment variables, and the reason is not obvious:
# collectstatic imports settings, and settings *require* SECRET_KEY (Unit 1 --
# deliberately no default). Without a placeholder the build simply fails. This
# value is never used to sign anything: no request is served during a build, and
# the real key arrives from a Secret at runtime.
#
# DEBUG=False is what selects CompressedManifestStaticFilesStorage, so the
# manifest and hashed filenames are produced here, at build time. That also
# means a typo'd {% static %} path fails THIS LINE rather than a user's page.
RUN SECRET_KEY=build-time-placeholder-never-used-to-sign-anything \
    DEBUG=False \
    python manage.py collectstatic --noinput --clear

USER app

EXPOSE 8000

# No HEALTHCHECK instruction: Kubernetes runs its own liveness and readiness
# probes against /healthz/*, and a container-level healthcheck would duplicate
# them while being invisible to the kubelet's restart logic.

ENTRYPOINT ["./docker-entrypoint.sh"]
CMD ["web"]
