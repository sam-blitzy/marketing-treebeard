# Ads Action Engine: developer entry point.
#
# Targets (`make help` lists them):
#   check-tools     Docker Compose >=5.5.1,<6 and Docker Engine >=29 (bounds below, nowhere else)
#   init            first start: copy .env.example to .env (never over an existing one) and write
#                   a random SESSION_SECRET and the argon2id APP_ADMIN_PASSWORD_HASH
#   up, down        build and start the Compose stack and wait for /api/v1/healthz; stop it
#   migrate         the one-shot migrate service: ads-engine migrate && ads-engine ensure-admin
#   seed-simulated  seed simulated datasets for every source and compute the default-window run
#   test            backend pytest on a disposable postgres:18.6, then the web unit tests
#   lint            ruff format/check and mypy --strict; eslint, stylelint and tsc
#   e2e             Playwright against the stack in DATA_MODE=simulated
#   secret-scan     gitleaks (pinned image) over every commit with .gitleaks.toml
#
# First start:
#   make init ADMIN_PASSWORD=<value>
#   docker compose up -d --build        (or: make up)
#   open http://localhost:8080 and sign in as admin@example.com with that password
#
# Overridable variables (command line or environment):
#   COMPOSE             the Compose command (default: docker compose); add -p/-f flags here;
#                       empty means the default
#   APP_URL             origin of the web service that up/e2e poll and Playwright targets
#   HEALTH_TIMEOUT      seconds up/e2e wait for /api/v1/healthz to answer 200
#   TEST_DB_PORT        host port of the disposable test database (127.0.0.1 only)
#   TEST_DATABASE_URL   database the backend tests use (read by backend/tests/conftest.py)
#   PYTEST_ARGS         extra pytest arguments for `make test`, such as tests/unit
#   PLAYWRIGHT_INSTALL  browser install step of `make e2e`; empty skips it
#   ADMIN_EMAIL         e2e login, the APP_ADMIN_EMAIL of .env
#   ADMIN_PASSWORD      init: the admin password to hash; e2e: the password given to init
#
# Secrets: ADMIN_PASSWORD, SESSION_SECRET and the hash are never printed, never placed in a
# command line (the password reaches `ads-engine hash-password` on stdin, the generated values
# reach awk through its environment) and only ever written to .env, mode 600, which is
# gitignored. Recipes print "written" instead of a value.
#
# Requirements: GNU make 4.0 or newer and bash; POSIX awk, od and mktemp; curl or
# wget for the health poll. Every recipe runs as one bash script (.ONESHELL) with -eu and
# pipefail, so any failing command stops it. Inside recipes no line may begin with `-`, `+` or
# `@` once indented: GNU make strips those prefix characters from every .ONESHELL line, so long
# docker invocations are assembled in bash arrays instead.

ifneq ($(filter 0.% 1.% 2.% 3.%,$(MAKE_VERSION)),)
$(error GNU make 4.0 or newer is required (found $(MAKE_VERSION)); on macOS run `brew install make` and use gmake)
endif

SHELL := /bin/bash
.SHELLFLAGS := -eu -o pipefail -c
.ONESHELL:
.DEFAULT_GOAL := help

.PHONY: help check-tools init up down migrate seed-simulated test lint e2e secret-scan

# -----------------------------------------------------------------------------------------------
# Variables
# -----------------------------------------------------------------------------------------------

# The environment and the command line override COMPOSE (the command line wins); an empty or
# blank value means the default.
COMPOSE ?= docker compose
ifeq ($(strip $(COMPOSE)),)
override COMPOSE := docker compose
endif

# Tool bounds: Compose >=COMPOSE_MIN (a release MAJOR.MINOR[.PATCH]) and of a major version below
# COMPOSE_MAX; Engine major version >=ENGINE_MIN. check-tools compares numerically and accepts
# release versions only. CI installs Compose at exactly COMPOSE_MIN and runs `make check-tools`.
COMPOSE_MIN := 5.5.1
COMPOSE_MAX := 6
ENGINE_MIN := 29

# Pinned by tag, never `latest`.
GITLEAKS_IMAGE := zricethezav/gitleaks:v8.30.1

# The disposable database of `make test`: published on 127.0.0.1 only and removed, with its
# anonymous volume, whenever the recipe ends. TEST_DATABASE_URL is exported, never written into
# a command line, so a URL carrying a real password stays out of process listings.
TEST_DB_IMAGE := postgres:18.6
TEST_DB_PORT ?= 55432
TEST_DB_NAME := ads-engine-testdb
TEST_DATABASE_URL ?= postgresql+psycopg://ads:ads@localhost:$(TEST_DB_PORT)/ads_engine_test
export TEST_DATABASE_URL

ADMIN_EMAIL ?= admin@example.com
APP_URL ?= http://localhost:8080
HEALTH_TIMEOUT ?= 180
PYTEST_ARGS ?=
PLAYWRIGHT_INSTALL ?= npx playwright install --with-deps chromium

# A command-line value would be expanded by make before it reaches the recipe environment, so
# `make init ADMIN_PASSWORD='pa$word'` would hash "paord". Re-binding it as a simple variable
# from its unexpanded text passes the password through byte for byte; an environment value is
# passed through unchanged already.
ifeq ($(origin ADMIN_PASSWORD),command line)
override ADMIN_PASSWORD := $(value ADMIN_PASSWORD)
endif
export ADMIN_PASSWORD

# -----------------------------------------------------------------------------------------------
# Shared recipe fragments (expanded inside .ONESHELL recipes; `$$` reaches bash as `$`)
# -----------------------------------------------------------------------------------------------

define REQUIRE_ENV_FILE
if [ ! -f .env ]; then
	echo ".env not found: run make init ADMIN_PASSWORD=<value> first" >&2
	exit 1
fi
endef

# Shows why the stack is not serving: every container's state and the latest log lines of the
# services on the request path.
define DEFINE_STACK_DIAGNOSTICS
stack_diagnostics() {
	echo "Stack state and recent logs:" >&2
	$(COMPOSE) ps --all >&2 || true
	$(COMPOSE) logs --no-color --tail=50 migrate api web >&2 || true
}
endef

# Builds and starts the stack, then polls the API through nginx until it answers 200. The poll
# replaces `up --wait`, which fails on the one-shot migrate service that exits by design.
define STACK_UP
$(DEFINE_STACK_DIAGNOSTICS)
health_url='$(APP_URL)/api/v1/healthz'
if command -v curl >/dev/null 2>&1; then
	probe_health() {
		[ "$$(curl --silent --output /dev/null --write-out '%{http_code}' --max-time 5 "$$health_url" || true)" = 200 ]
	}
elif command -v wget >/dev/null 2>&1; then
	probe_health() {
		wget -q -O /dev/null -T 5 "$$health_url" >/dev/null 2>&1
	}
else
	echo "Neither curl nor wget is installed; one is needed to poll $$health_url" >&2
	exit 1
fi
echo "Building and starting the stack ($(COMPOSE) up -d --build)"
if ! $(COMPOSE) up -d --build; then
	echo "$(COMPOSE) up failed" >&2
	stack_diagnostics
	exit 1
fi
echo "Waiting up to $(HEALTH_TIMEOUT) s for $$health_url to answer 200"
deadline=$$(( $$(date +%s) + $(HEALTH_TIMEOUT) ))
until probe_health; do
	if [ "$$(date +%s)" -ge "$$deadline" ]; then
		echo "$$health_url did not answer 200 within $(HEALTH_TIMEOUT) s" >&2
		stack_diagnostics
		exit 1
	fi
	sleep 3
done
echo "API healthy at $$health_url"
endef

# -----------------------------------------------------------------------------------------------
# Targets
# -----------------------------------------------------------------------------------------------

help: ## List the targets
	@echo "Ads Action Engine make targets:"
	awk 'BEGIN { FS = ":[^#]*## " } /^[a-z][a-z0-9-]*:[^#]*## / { printf "  %-15s %s\n", $$1, $$2 }' $(firstword $(MAKEFILE_LIST))
	echo
	echo "First start: make init ADMIN_PASSWORD=<value>, then docker compose up -d --build (or make up)."

check-tools: ## Verify Docker Compose >=5.5.1,<6 and Docker Engine >=29
	@compose_range='>=$(COMPOSE_MIN),<$(COMPOSE_MAX)'
	engine_range='>=$(ENGINE_MIN)'
	# Release versions only: [v]MAJOR.MINOR[.PATCH][+BUILD[~BACKPORT]], a missing patch counting
	# as 0. Each component is 0 or up to nine digits without a leading zero, so bash arithmetic
	# never reads octal or overflows. +BUILD is SemVer 2.0.0 build metadata: nonempty
	# dot-separated identifiers of [0-9A-Za-z-]. ~BACKPORT is a Debian/Ubuntu backport tail of
	# nonempty dot-separated alphanumeric identifiers. Together they admit two distribution builds
	# of a release: Debian docker.io (28.5.2+dfsg4) and Ubuntu docker-compose-v2
	# (2.40.3+ds1-0ubuntu1~24.04.1). Refused: prereleases (5.5.1-rc.1, which SemVer ranks below
	# 5.5.1), a bare Debian revision (2.40.3-3, indistinguishable from a prerelease), development
	# builds (v5.6.0-24-g4e1cd3f4e) and any other text. parse_release sets release_major,
	# release_minor and release_patch.
	release_re='^v?(0|[1-9][0-9]{0,8})\.(0|[1-9][0-9]{0,8})(\.(0|[1-9][0-9]{0,8}))?(\+[0-9A-Za-z-]+(\.[0-9A-Za-z-]+)*(~[0-9A-Za-z]+(\.[0-9A-Za-z]+)*)?)?$$'
	major_re='^(0|[1-9][0-9]{0,8})$$'
	parse_release() {
		[[ $$1 =~ $$release_re ]] || return 1
		release_major="$${BASH_REMATCH[1]}"
		release_minor="$${BASH_REMATCH[2]}"
		release_patch="$${BASH_REMATCH[4]:-0}"
	}
	if ! parse_release '$(COMPOSE_MIN)' || ! [[ '$(COMPOSE_MAX)' =~ $$major_re ]] || ! [[ '$(ENGINE_MIN)' =~ $$major_re ]]; then
		echo "Malformed tool bounds in the Makefile: COMPOSE_MIN must be a release version MAJOR.MINOR[.PATCH], COMPOSE_MAX and ENGINE_MIN major versions; found $$compose_range, Engine $$engine_range" >&2
		exit 1
	fi
	min_major="$$release_major"
	min_minor="$$release_minor"
	min_patch="$$release_patch"
	if ! command -v docker >/dev/null 2>&1; then
		echo "docker not found on PATH; required: Docker Engine $$engine_range with the Compose plugin $$compose_range" >&2
		exit 1
	fi
	if ! compose_version="$$($(COMPOSE) version --short 2>/dev/null)" || [ -z "$$compose_version" ]; then
		echo "Docker Compose not found ('$(COMPOSE) version --short' failed); required $$compose_range" >&2
		exit 1
	fi
	if ! parse_release "$$compose_version"; then
		echo "Docker Compose $$compose_version is not a release version (prereleases and development builds are not accepted); required $$compose_range" >&2
		exit 1
	fi
	compose_version="$${compose_version#v}"
	if (( release_major < min_major
		|| (release_major == min_major && release_minor < min_minor)
		|| (release_major == min_major && release_minor == min_minor && release_patch < min_patch)
		|| release_major >= $(COMPOSE_MAX) )); then
		echo "Docker Compose $$compose_version found; required $$compose_range" >&2
		exit 1
	fi
	if ! engine_version="$$(docker version --format '{{.Server.Version}}' 2>/dev/null)" || [ -z "$$engine_version" ]; then
		echo "Docker Engine not reachable (is the daemon running, and may this user use it?); required $$engine_range" >&2
		exit 1
	fi
	if ! parse_release "$$engine_version"; then
		echo "Docker Engine $$engine_version is not a release version (prereleases and development builds are not accepted); required $$engine_range" >&2
		exit 1
	fi
	if (( release_major < $(ENGINE_MIN) )); then
		echo "Docker Engine $$engine_version found; required $$engine_range" >&2
		exit 1
	fi
	echo "Docker Compose $$compose_version ($$compose_range) and Docker Engine $$engine_version ($$engine_range): ok"

# init writes the two generated values into the copied template:
# - APP_ADMIN_PASSWORD_HASH is single-quoted, because Compose interpolates dollar signs in
#   unquoted and double-quoted .env values and an argon2 PHC string is full of them;
# - both values reach awk through ENVIRON, which applies no escape processing (awk -v would),
#   and never through a command line;
# - the file is rebuilt through a temporary file and mv rather than sed -i, whose syntax
#   differs between GNU and BSD, and every other line of the template is kept as it is, so
#   APP_ADMIN_EMAIL stays admin@example.com and every other variable keeps its default.
init: export CHECKOUT_DIR := $(CURDIR)
init: check-tools ## First start: write .env with SESSION_SECRET and the admin hash (make init ADMIN_PASSWORD=<value>)
	@if [ -z "$${ADMIN_PASSWORD:-}" ]; then
		echo "usage: make init ADMIN_PASSWORD=<value>" >&2
		exit 1
	fi
	if [ -e .env ] || [ -L .env ]; then
		echo "refusing to overwrite existing .env; move it aside first to initialise again" >&2
		exit 1
	fi
	if [ ! -f .env.example ]; then
		echo ".env.example not found in $$CHECKOUT_DIR" >&2
		exit 1
	fi
	# Once the reservation below has created .env, a failure, or Ctrl-C, removes it, so a re-run
	# starts clean; an .env created by anything else is never touched.
	created=0
	tmp_env=''
	cleanup_init() {
		status=$$?
		if [ -n "$$tmp_env" ]; then rm -f "$$tmp_env"; fi
		if [ "$$status" -ne 0 ] && [ "$$created" -eq 1 ]; then
			rm -f .env
			echo "make init failed; removed the partial .env" >&2
		fi
		exit "$$status"
	}
	trap cleanup_init EXIT
	trap 'exit 130' INT
	trap 'exit 143' TERM
	umask 077
	# Reserve, own, then copy. With noclobber the empty reservation is created exclusively, so it
	# refuses, untouched, an .env (a symlink included) that appeared after the check above; the
	# regular-file test refuses one it could still open, such as a link to a device. The file is
	# this run's from the reservation on, so a failed or interrupted copy is removed, not left.
	if ! (set -o noclobber; : > .env) || [ -L .env ] || [ ! -f .env ]; then
		echo "make init did not create .env; an existing .env is never overwritten" >&2
		exit 1
	fi
	created=1
	cat .env.example > .env
	echo "Copied .env.example to .env"
	session_secret="$$(od -An -tx1 -N32 /dev/urandom | tr -d ' \n')"
	case "$$session_secret" in
		*[!0-9a-f]*) session_secret='' ;;
	esac
	if [ "$${#session_secret}" -ne 64 ]; then
		echo "Could not read 32 random bytes from /dev/urandom for SESSION_SECRET" >&2
		exit 1
	fi
	echo "Building the backend image ($(COMPOSE) build migrate)"
	$(COMPOSE) build migrate
	echo "Hashing the admin password with ads-engine hash-password in the backend image"
	admin_hash="$$(printf '%s' "$$ADMIN_PASSWORD" | $(COMPOSE) run --rm -T --no-deps migrate ads-engine hash-password)"
	if ! printf '%s' "$$admin_hash" | grep -Eq '^[$$]argon2id[$$]v=[0-9]+[$$]m=[0-9]+,t=[0-9]+,p=[0-9]+[$$][A-Za-z0-9+/]+[$$][A-Za-z0-9+/]+$$'; then
		echo "ads-engine hash-password did not print an argon2id hash" >&2
		exit 1
	fi
	tmp_env="$$(mktemp .env.XXXXXX)"
	SESSION_SECRET_VALUE="$$session_secret" ADMIN_HASH_VALUE="$$admin_hash" awk '
		BEGIN { q = sprintf("%c", 39) }
		/^SESSION_SECRET=/ { if (!s) print "SESSION_SECRET=" ENVIRON["SESSION_SECRET_VALUE"]; s = 1; next }
		/^APP_ADMIN_PASSWORD_HASH=/ { if (!h) print "APP_ADMIN_PASSWORD_HASH=" q ENVIRON["ADMIN_HASH_VALUE"] q; h = 1; next }
		{ print }
		END {
			if (!s) print "SESSION_SECRET=" ENVIRON["SESSION_SECRET_VALUE"]
			if (!h) print "APP_ADMIN_PASSWORD_HASH=" q ENVIRON["ADMIN_HASH_VALUE"] q
		}
	' .env > "$$tmp_env"
	chmod 600 "$$tmp_env"
	mv -f "$$tmp_env" .env
	tmp_env=''
	chmod 600 .env
	echo "SESSION_SECRET written"
	echo "APP_ADMIN_PASSWORD_HASH written"
	admin_email="$$(awk 'index($$0, "APP_ADMIN_EMAIL=") == 1 { v = substr($$0, 17) } END { print v }' .env)"
	echo ".env is ready (mode 600). Next:"
	echo "  docker compose up -d --build    (or: make up)"
	echo "  open $(APP_URL) and sign in as $${admin_email:-admin@example.com} with ADMIN_PASSWORD"

up: check-tools ## Build and start the stack, then wait for /api/v1/healthz to answer 200
	@$(REQUIRE_ENV_FILE)
	$(STACK_UP)
	echo "Open $(APP_URL) and sign in with the admin of .env; stop the stack with make down"

# The pgdata volume is kept; `docker compose down -v` also deletes the database for a full reset.
down: ## Stop and remove the stack's containers (the database volume is kept)
	@$(COMPOSE) down

migrate: ## Run the one-shot migrate service: ads-engine migrate && ads-engine ensure-admin
	@$(REQUIRE_ENV_FILE)
	$(COMPOSE) run --rm migrate

# `ads-engine migrate` runs first: it is idempotent, and seeding needs the schema, which a fresh
# pgdata volume does not have until the stack has been started once.
seed-simulated: ## Seed simulated datasets for every source and compute the default-window run
	@$(REQUIRE_ENV_FILE)
	$(COMPOSE) run --rm migrate sh -c 'ads-engine migrate && ads-engine seed-simulated'

# Coverage gates live in backend/pyproject.toml and web/vite.config.ts, not here. Backend tools
# run with `uv run --frozen` here and in lint, so a run never re-resolves or rewrites uv.lock:
# the lock is used exactly as committed, as `uv sync --frozen` installed it.
test: ## Backend tests on a disposable postgres:18.6 (always removed), then the web unit tests
	@remove_test_db() {
		status=$$?
		docker rm --force --volumes '$(TEST_DB_NAME)' >/dev/null 2>&1 || true
		echo "Removed the test database container $(TEST_DB_NAME)"
		exit "$$status"
	}
	trap remove_test_db EXIT
	trap 'exit 130' INT
	trap 'exit 143' TERM
	db_state="$$(docker container inspect --format '{{.State.Running}}' '$(TEST_DB_NAME)' 2>/dev/null || true)"
	if [ "$$db_state" = true ]; then
		echo "Reusing the running $(TEST_DB_NAME); it is removed when the tests finish"
	else
		if [ -n "$$db_state" ]; then docker rm --force --volumes '$(TEST_DB_NAME)' >/dev/null; fi
		db_args=(run --detach --rm --name '$(TEST_DB_NAME)')
		db_args+=(--env POSTGRES_USER=ads --env POSTGRES_PASSWORD=ads --env POSTGRES_DB=ads_engine_test)
		db_args+=(--publish '127.0.0.1:$(TEST_DB_PORT):5432' '$(TEST_DB_IMAGE)')
		echo "Starting $(TEST_DB_NAME) ($(TEST_DB_IMAGE)) on 127.0.0.1:$(TEST_DB_PORT)"
		docker "$${db_args[@]}" >/dev/null
	fi
	# TCP readiness (-h 127.0.0.1): the image's first-run initialisation server listens on the
	# socket only, so a socket probe could report ready before the final server starts.
	waited=0
	until docker exec '$(TEST_DB_NAME)' pg_isready -q -h 127.0.0.1 -U ads -d ads_engine_test; do
		if [ "$$waited" -ge 60 ]; then
			echo "$(TEST_DB_NAME) did not accept connections within 60 s" >&2
			docker logs --tail 50 '$(TEST_DB_NAME)' >&2 || true
			exit 1
		fi
		sleep 1
		waited=$$((waited + 1))
	done
	echo "Backend: uv sync --frozen, then uv run --frozen pytest$(if $(strip $(PYTEST_ARGS)), $(PYTEST_ARGS))"
	(cd backend && uv sync --frozen && uv run --frozen pytest $(PYTEST_ARGS))
	echo "Web: npm ci, then npm test"
	(cd web && npm ci && npm test)

lint: ## ruff format --check, ruff check, mypy --strict; eslint, stylelint and tsc --noEmit
	@echo "Backend: ruff format --check, ruff check, mypy --strict src"
	(cd backend && uv sync --frozen && uv run --frozen ruff format --check . && uv run --frozen ruff check . && uv run --frozen mypy --strict src)
	echo "Web: npm run lint (eslint, stylelint), npm run typecheck (tsc --noEmit)"
	(cd web && npm ci && npm run lint && npm run typecheck)

e2e: check-tools ## Playwright against the stack in DATA_MODE=simulated (make e2e ADMIN_PASSWORD=<value>)
	@$(REQUIRE_ENV_FILE)
	if [ -z "$${ADMIN_PASSWORD:-}" ]; then
		echo "usage: make e2e ADMIN_PASSWORD=<value>   (the password given to make init)" >&2
		exit 1
	fi
	# Compose interpolates DATA_MODE into migrate, api and worker, overriding .env.
	export DATA_MODE=simulated
	$(STACK_UP)
	leave_stack_running() {
		status=$$?
		echo "The stack is left running for inspection; stop it with make down"
		exit "$$status"
	}
	trap leave_stack_running EXIT
	cd web
	npm ci
	$(if $(strip $(PLAYWRIGHT_INSTALL)),$(PLAYWRIGHT_INSTALL),echo "PLAYWRIGHT_INSTALL is empty; using the installed Playwright browsers")
	E2E_BASE_URL='$(APP_URL)' E2E_ADMIN_EMAIL='$(ADMIN_EMAIL)' E2E_ADMIN_PASSWORD="$$ADMIN_PASSWORD" npm run e2e

# `gitleaks git` scans every commit: the committed history is the gate. The image's git refuses
# a mounted repository owned by another uid ("dubious ownership"), so /repo is declared safe
# first. A linked worktree keeps its git data in the main checkout, which is mounted read-only
# at its own path so the worktree's .git file still resolves inside the container. The checkout
# directory reaches bash as the environment variable CHECKOUT_DIR and is expanded in double
# quotes, never spliced into the script as make text, so a path with spaces, quotes or dollar
# signs is passed through as data.
secret-scan: export CHECKOUT_DIR := $(CURDIR)
secret-scan: ## gitleaks v8.30.1 (pinned image) over every commit with .gitleaks.toml; a finding fails
	@scan_args=(run --rm --volume "$$CHECKOUT_DIR:/repo:ro" --workdir /repo --entrypoint sh)
	git_common_dir="$$(git -C "$$CHECKOUT_DIR" rev-parse --path-format=absolute --git-common-dir 2>/dev/null || true)"
	if [ -n "$$git_common_dir" ] && [ "$$git_common_dir" != "$$CHECKOUT_DIR/.git" ]; then
		scan_args+=(--volume "$$git_common_dir:$$git_common_dir:ro")
	fi
	scan_args+=('$(GITLEAKS_IMAGE)' -c 'git config --global --add safe.directory /repo && exec gitleaks git /repo --config /repo/.gitleaks.toml --redact --exit-code 1 --verbose')
	echo "Scanning every commit with $(GITLEAKS_IMAGE) and .gitleaks.toml"
	docker "$${scan_args[@]}"
	echo "No secrets found"
