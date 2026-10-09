# syntax=docker/dockerfile:1
#
# Ads Action Engine backend: the one image behind the `migrate`, `api` and `worker` services.
#
# Build context: ./backend (root docker-compose.yml builds it once). The Compose `command:`
# selects the process; the default CMD below is the API:
#   migrate  sh -c "ads-engine migrate && ads-engine ensure-admin"
#   api      uvicorn ads_engine.api.app:create_app --factory --host 0.0.0.0 --port 8000
#            --proxy-headers (proxy headers trusted only from FORWARDED_ALLOW_IPS; see the CMD)
#   worker   python -m ads_engine.worker.main
#   init     `make init` pipes the admin password into `ads-engine hash-password` in this
#            image with stdin attached (`docker compose run --rm -T ...` or `docker run -i`)
#
# Runtime layout (WORKDIR /app)
#   .venv/             runtime dependencies and ads_engine, installed as a wheel (no dev group)
#   alembic.ini        read from the working directory by `ads-engine migrate`
#   migrations/        Alembic environment and revisions
#   src/               the project source the wheel was built from
#   tests/contracts/   the external API contracts the simulator reads at runtime, the same
#                      files the contract tests read; nothing else from tests/ is copied.
#                      Root-owned like every file, except each {source}/captured/: the
#                      runtime user owns those, so `ads-engine capture-fixtures` can write
#                      its redacted live captures there (bind-mount the checkout's own
#                      captured/ directory over one to keep them)
#   config/            NOT in the image: it lies outside the build context, and Compose
#                      bind-mounts the repository's config/ read-only here
#
# Secrets: none. No ARG or ENV carries a secret and no .env* file is copied; Compose injects
# .env into the migrate, api and worker containers at run time only.
#
# Health: no HEALTHCHECK, because the same image runs `worker` and `migrate`, which serve no
# HTTP. Compose probes the api service with the image's own Python (urllib) on
# /api/v1/healthz; the slim base has no curl, and none is added.

# Exact tag, so a rebuild cannot change the runtime silently.
FROM python:3.14.8-slim

# uv pinned exactly; backend/pyproject.toml declares `required-version = "==0.12.21"`, so any
# other uv refuses to sync.
COPY --from=ghcr.io/astral-sh/uv:0.12.21 /uv /uvx /bin/

# UV_COMPILE_BYTECODE: precompile site-packages at build time, so the read-only runtime never
#   needs to write bytecode for them.
# UV_LINK_MODE=copy: the uv cache is a build-time cache mount, so files are copied, not linked.
# UV_PYTHON_DOWNLOADS=never, UV_PYTHON=python3.14: use the base image's interpreter only.
# UV_PROJECT_ENVIRONMENT: the virtual environment every process runs from.
# PYTHONUNBUFFERED: structlog JSON reaches stdout immediately.
# PYTHONDONTWRITEBYTECODE: the image filesystem is read-only to the runtime user (outside
#   tests/contracts/*/captured/), so Python does not attempt to write __pycache__ for modules
#   outside site-packages (migrations/).
ENV UV_COMPILE_BYTECODE=1 \
  UV_LINK_MODE=copy \
  UV_PYTHON_DOWNLOADS=never \
  UV_PYTHON=python3.14 \
  UV_PROJECT_ENVIRONMENT=/app/.venv \
  PYTHONUNBUFFERED=1 \
  PYTHONDONTWRITEBYTECODE=1

# Fixed non-root identity with no home and no login shell. Created before the project layers
# so source changes do not invalidate it; the switch to it happens at the end of the build.
# The uid sits above SYS_UID_MAX on purpose (a fixed id outside every distribution range), so
# useradd's "greater than SYS_UID_MAX" build warning is expected.
RUN groupadd --system --gid 10001 app \
  && useradd --system --uid 10001 --gid app --no-create-home --home-dir /nonexistent \
    --shell /usr/sbin/nologin app

WORKDIR /app

# Dependency layer, cached separately from the source: it is rebuilt only when the manifest or
# the lockfile changes. `--locked` asserts that uv.lock matches pyproject.toml and fails the
# build when it does not, instead of installing a stale lock; the lockfile is never rewritten
# in the image. (`--frozen` would install whatever the lock holds without that check.)
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
  uv sync --locked --no-dev --no-install-project

# Project layer. Explicit COPYs only, never `COPY . .`: a local .venv, caches, coverage files
# or a .env in backend/ can never reach the image, with no .dockerignore to maintain.
# `--exclude` drops the __pycache__ directories that local test runs leave in the source
# tree, so an image built from a working checkout matches one built from a clean clone.
COPY --exclude=**/__pycache__ src ./src
COPY alembic.ini ./
COPY --exclude=**/__pycache__ migrations ./migrations
COPY tests/contracts ./tests/contracts
# The capture directories alone, not their contents and not the contracts, go to the runtime
# user, so `ads-engine capture-fixtures` can write into them; a glob that matches nothing fails
# the build.
RUN chown 10001:10001 tests/contracts/*/captured

# Installs ads_engine into /app/.venv as a regular (non-editable) wheel built by uv_build,
# which also creates the `ads-engine` console script.
RUN --mount=type=cache,target=/root/.cache/uv \
  uv sync --locked --no-dev --no-editable

ENV PATH="/app/.venv/bin:${PATH}"

# Every file stays root-owned and read-only to the runtime user, with one exception: the
# tests/contracts/*/captured/ directories it owns, where `ads-engine capture-fixtures` writes
# its redacted live captures. The api, worker and migrate processes write nothing to the image
# filesystem.
USER 10001:10001

EXPOSE 8000

# Proxy-header trust for the API. uvicorn takes the client address and scheme from
# X-Forwarded-For and X-Forwarded-Proto only when the connecting peer is listed in
# FORWARDED_ALLOW_IPS (comma-separated addresses or CIDR networks); from any other peer it
# ignores both headers, and from a listed peer it takes the rightmost X-Forwarded-For entry
# that is not itself listed. The login rate limiter keys on that address. The default trusts
# loopback only, so a client reaching a published port 8000 directly is always keyed on its own
# address, whatever header it sends. A deployment behind a reverse proxy sets
# FORWARDED_ALLOW_IPS to the proxy's address or network at run time (`docker run -e`, a Compose
# `environment:` entry), and that proxy must overwrite X-Forwarded-For with the address of the
# peer connected to it (nginx: `proxy_set_header X-Forwarded-For $remote_addr;`) rather than
# append to the client's value. Never set `*` where clients can reach the API port: uvicorn
# then trusts every peer and takes the leftmost entry, which the client writes, so each attempt
# could claim a fresh address and escape the per-IP login limit.
ENV FORWARDED_ALLOW_IPS=127.0.0.1,::1

# No ENTRYPOINT, so a Compose `command:` replaces this CMD directly. `--forwarded-allow-ips` is
# deliberately absent: uvicorn reads FORWARDED_ALLOW_IPS (above) instead, so the trusted proxies
# are set at run time without replacing the command.
CMD ["uvicorn", "ads_engine.api.app:create_app", "--factory", \
  "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
