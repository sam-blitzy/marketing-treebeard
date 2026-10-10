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
#                       empty means the default. e2e passes it to Playwright as E2E_COMPOSE
#   APP_URL             origin of the web service that up/e2e poll and Playwright targets
#   HEALTH_TIMEOUT      seconds up/e2e wait for /api/v1/healthz to answer 200
#   TEST_DB_PORT        127.0.0.1 port of the disposable test database (default 55432); 0 lets
#                       Docker choose a free port. make test points TEST_DATABASE_URL at it
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

# The disposable database of `make test`. Every run creates its own container, named
# TEST_DB_NAME-<random token> and labelled TEST_DB_LABEL=<token>, and removes, with its anonymous
# volume, only what carries that token: it never adopts or deletes a container it did not create,
# so concurrent runs cannot share or remove each other's database. It is published on
# 127.0.0.1:TEST_DB_PORT only (0 lets Docker choose a free port; a port in use fails the run).
# The recipe exports TEST_DATABASE_URL for the port Docker actually bound, replacing any
# inherited value, because the tests drop and recreate schema public in that database; the URL
# is never written into a command line. TEST_DB_PORT is exported and read by the recipe as data,
# never pasted into its shell source. TEST_DB_READY_TIMEOUT is wall-clock seconds.
TEST_DB_IMAGE := postgres:18.6
TEST_DB_PORT ?= 55432
export TEST_DB_PORT
TEST_DB_NAME := ads-engine-testdb
TEST_DB_LABEL := com.ads-engine.make-test
TEST_DB_READY_TIMEOUT := 60

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
test: ## Backend tests on a disposable postgres:18.6 (removed on exit; an unconfirmed removal fails the target), then the web unit tests
	@test_db_port=''
	case "$${TEST_DB_PORT:-}" in
		'' | *[!0-9]*) ;;
		*)
			# Leading zeros are dropped first, so no digit string can overflow bash arithmetic
			# or be read as octal.
			port_digits="$${TEST_DB_PORT#"$${TEST_DB_PORT%%[!0]*}"}"
			if [ "$${#port_digits}" -le 5 ] && [ "$$(( $${port_digits:-0} ))" -le 65535 ]; then
				test_db_port="$$(( $${port_digits:-0} ))"
			fi
			;;
	esac
	if [ -z "$$test_db_port" ]; then
		echo "TEST_DB_PORT must be a port number from 0 to 65535 (0 lets Docker choose a free port), not '$${TEST_DB_PORT:-}'" >&2
		exit 1
	fi
	if [ "$$test_db_port" -eq 0 ]; then
		requested_endpoint='127.0.0.1 (a free port Docker chooses)'
	else
		requested_endpoint="127.0.0.1:$$test_db_port"
	fi
	# A random token per run names and labels the container, so neither a concurrent run nor a
	# container something else called $(TEST_DB_NAME) can be taken for this run's database.
	token="$$(od -An -tx1 -N8 /dev/urandom | tr -d ' \n')"
	case "$$token" in
		*[!0-9a-f]*) token='' ;;
	esac
	if [ "$${#token}" -ne 16 ]; then
		echo "Could not read 8 random bytes from /dev/urandom for the test database name" >&2
		exit 1
	fi
	db_name='$(TEST_DB_NAME)'"-$$token"
	db_label='$(TEST_DB_LABEL)'"=$$token"
	# The cleanup state is set and the traps are installed before anything is created: from
	# here on every exit, Ctrl-C included, removes what this run created and nothing else.
	db_id=''
	db_volumes=''
	create_attempted=0
	# Adds the anonymous volumes of container $$1 to db_volumes, once each.
	record_volumes() {
		volume_format='{{range .Mounts}}{{if eq .Type "volume"}}{{println .Name}}{{end}}{{end}}'
		mounts="$$(docker container inspect --format "$$volume_format" "$$1")" || return 1
		for vol in $$mounts; do
			case " $$db_volumes " in
				*" $$vol "*) ;;
				*) db_volumes="$${db_volumes:+$$db_volumes }$$vol" ;;
			esac
		done
	}
	# Succeeds only when Docker reports the object absent: $$1 is container or volume, $$2 its
	# ID or name. An existing object, or any other error such as an unreachable daemon, leaves
	# the removal unconfirmed; absent_error then says why.
	is_absent() {
		if absent_error="$$(docker "$$1" inspect "$$2" 2>&1 >/dev/null)"; then
			absent_error='it still exists'
			return 1
		fi
		case "$$(printf '%s' "$$absent_error" | tr '[:upper:]' '[:lower:]')" in
			*"no such $$1"*) return 0 ;;
			*) return 1 ;;
		esac
	}
	# Runs the removal command that follows $$1 (container or volume) and $$2 (its ID or name),
	# then succeeds once Docker reports the object absent. The container is created with --rm,
	# so once it stops the daemon removes it itself: a second removal is refused as already in
	# progress, and the anonymous volume goes only after the container. An object still there
	# is therefore re-checked, and its removal retried, once a second until removal_deadline,
	# a wall-clock time as in the readiness wait. It fails when the object still exists or is
	# still unconfirmed (an unreachable daemon, say) at the deadline, with removal_error
	# holding the last attempt's error. A first check that confirms the removal ends it at once.
	remove_until_absent() {
		removal_kind="$$1"
		removal_name="$$2"
		shift 2
		removal_error="$$("$$@" 2>&1 >/dev/null)" || true
		until is_absent "$$removal_kind" "$$removal_name"; do
			# A clock that cannot be read counts as no time left, so the wait always ends.
			if ! [ "$$(date +%s)" -lt "$$removal_deadline" ] 2>/dev/null; then
				return 1
			fi
			sleep 1
			removal_error="$$("$$@" 2>&1 >/dev/null)" || true
		done
	}
	# Removes only this run's containers: the ID docker create printed and any container with
	# this run's label, which covers one created just before a Ctrl-C stopped its ID being
	# recorded. An owned container already gone counts as removed; anything left, or not
	# confirmed gone, when the removal wait ends is reported with the command that removes it
	# and fails the run, keeping a failing test status. Each docker command's failure is
	# handled where it runs (if or ||), so errexit cannot cut the cleanup short.
	remove_test_db() {
		status=$$?
		if [ "$$create_attempted" -eq 0 ]; then
			exit "$$status"
		fi
		cleanup_failed=0
		owned_ids="$$db_id"
		if labelled_ids="$$(docker container ls --all --quiet --no-trunc --filter "label=$$db_label")"; then
			for id in $$labelled_ids; do
				case " $$owned_ids " in
					*" $$id "*) ;;
					*) owned_ids="$${owned_ids:+$$owned_ids }$$id" ;;
				esac
			done
		else
			echo "Could not list the containers labelled $$db_label; one may remain" >&2
			echo "  find them with: docker container ls --all --filter label=$$db_label" >&2
			cleanup_failed=1
		fi
		# One 10-second window covers every container and volume, so the cleanup ends about
		# 10 s from here whatever remains; a clock that cannot be read leaves no wait.
		removal_deadline=$$(( $$(date +%s) + 10 )) || removal_deadline=0
		for id in $$owned_ids; do
			record_volumes "$$id" 2>/dev/null || true
			if ! remove_until_absent container "$$id" docker rm --force --volumes "$$id"; then
				echo "Could not confirm the removal of the test database container $$db_name ($$id): $$absent_error" >&2
				if [ -n "$$removal_error" ]; then echo "  docker rm: $$removal_error" >&2; fi
				echo "  remove it with: docker rm --force --volumes $$id" >&2
				cleanup_failed=1
			fi
		done
		for vol in $$db_volumes; do
			if is_absent volume "$$vol"; then continue; fi
			if ! remove_until_absent volume "$$vol" docker volume rm "$$vol"; then
				echo "Could not confirm the removal of the test database volume $$vol: $$absent_error" >&2
				if [ -n "$$removal_error" ]; then echo "  docker volume rm: $$removal_error" >&2; fi
				echo "  remove it with: docker volume rm $$vol" >&2
				cleanup_failed=1
			fi
		done
		if [ "$$cleanup_failed" -ne 0 ]; then
			if [ "$$status" -eq 0 ]; then status=1; fi
		elif [ -n "$$owned_ids" ]; then
			case "$$db_volumes" in
				'') removed_volumes='' ;;
				*' '*) removed_volumes=' and its volumes' ;;
				*) removed_volumes=' and its volume' ;;
			esac
			echo "Removed the test database container $$db_name$$removed_volumes"
		fi
		exit "$$status"
	}
	trap remove_test_db EXIT
	trap 'exit 130' INT
	trap 'exit 143' TERM
	db_args=(create --rm --name "$$db_name" --label "$$db_label")
	db_args+=(--env POSTGRES_USER=ads --env POSTGRES_PASSWORD=ads --env POSTGRES_DB=ads_engine_test)
	db_args+=(--publish "127.0.0.1:$$test_db_port:5432" '$(TEST_DB_IMAGE)')
	echo "Creating $$db_name ($(TEST_DB_IMAGE)) on $$requested_endpoint"
	create_attempted=1
	if ! db_id="$$(docker "$${db_args[@]}")"; then
		db_id=''
		echo "Could not create $$db_name from $(TEST_DB_IMAGE)" >&2
		exit 1
	fi
	case "$$db_id" in
		'' | *[!0-9a-f]*)
			echo "docker create printed '$$db_id' instead of the ID of $$db_name" >&2
			db_id=''
			exit 1
			;;
	esac
	if ! record_volumes "$$db_id"; then
		echo "Could not read the volumes of $$db_name" >&2
		exit 1
	fi
	if ! docker start "$$db_id" >/dev/null; then
		echo "Could not start $$db_name on $$requested_endpoint; if the port is in use, set TEST_DB_PORT to a free port, or TEST_DB_PORT=0 to let Docker choose one" >&2
		exit 1
	fi
	# Docker reports the address it bound; with TEST_DB_PORT=0 that is the only way to learn it.
	if ! db_bindings="$$(docker port "$$db_id" 5432/tcp)"; then
		echo "Could not read the port Docker published for $$db_name" >&2
		exit 1
	fi
	db_port=''
	for binding in $$db_bindings; do
		case "$$binding" in
			127.0.0.1:*) db_port="$${binding#127.0.0.1:}" ;;
		esac
	done
	case "$$db_port" in
		*[!0-9]*) db_port='' ;;
	esac
	if [ -z "$$db_port" ] || { [ "$$test_db_port" -ne 0 ] && [ "$$db_port" -ne "$$test_db_port" ]; }; then
		echo "$$db_name is not published on $$requested_endpoint (docker port reported: $${db_bindings:-nothing})" >&2
		exit 1
	fi
	# The tests drop and recreate schema public, so they must reach this container and nothing
	# else: the URL replaces any inherited TEST_DATABASE_URL, and it names 127.0.0.1, where the
	# port is published, rather than localhost, which may resolve to ::1 and another server.
	export TEST_DATABASE_URL="postgresql+psycopg://ads:ads@127.0.0.1:$$db_port/ads_engine_test"
	echo "Backend tests use $$db_name on 127.0.0.1:$$db_port"
	# TCP readiness (-h 127.0.0.1): the image's first-run initialisation server listens on the
	# socket only, so a socket probe could report ready before the final server starts.
	# The deadline is wall-clock time, as in STACK_UP, so slow probes count against it: each
	# probe's connection timeout (-t) and each pause are capped by the time that remains, and
	# the timeout never reaches 0, which pg_isready reads as "wait forever".
	echo "Waiting up to $(TEST_DB_READY_TIMEOUT) s for $$db_name to accept connections"
	deadline=$$(( $$(date +%s) + $(TEST_DB_READY_TIMEOUT) ))
	while :; do
		remaining=$$(( deadline - $$(date +%s) ))
		if [ "$$remaining" -le 0 ]; then
			echo "$$db_name did not accept connections within $(TEST_DB_READY_TIMEOUT) s" >&2
			docker logs --tail 50 "$$db_id" >&2 || true
			exit 1
		fi
		probe_timeout=$$(( remaining < 3 ? remaining : 3 ))
		if docker exec "$$db_id" pg_isready -q -t "$$probe_timeout" -h 127.0.0.1 -U ads -d ads_engine_test; then
			break
		fi
		if [ "$$(( deadline - $$(date +%s) ))" -gt 0 ]; then
			sleep 1
		fi
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

# actions.spec.ts undoes its snooze through this stack's db service, so the specs reach the stack
# with the same Compose command, -p/-f flags included. COMPOSE reaches Playwright as the
# environment variable E2E_COMPOSE, never spliced into the recipe as quoted make text, so quotes
# inside it (a -f path with spaces) arrive intact. The spec runs it through sh -c from the
# repository root, which parses it as the recipes' own $(COMPOSE) commands are parsed. override
# keeps an E2E_COMPOSE given to make from pointing the specs at another stack.
e2e: override export E2E_COMPOSE = $(COMPOSE)
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
