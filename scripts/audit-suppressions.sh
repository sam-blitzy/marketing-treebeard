#!/usr/bin/env bash
#
# audit-suppressions.sh — Gate 2 suppression audit (AAP 0.11.2 Gate 2, 0.13.2 suppression policy).
#
# Scans every tracked text file of the repository for inline comments that silence a quality
# gate instead of fixing the finding: directives that disable or reconfigure ESLint rules or
# disable Stylelint rules, that make the TypeScript compiler skip a line or a file, that make
# mypy, ruff or pyright ignore a line or a file (including inline overrides of their settings),
# that exempt code from the ruff formatter or import sorter, and that exclude code from coverage
# measurement. Findings are fixed at their cause, never suppressed; where a third-party type is
# missing, a typed wrapper is written instead.
#
# Usage:   scripts/audit-suppressions.sh [-h|--help]
# Runs from any working directory: paths are resolved from this script's own location, and the
# report is identical wherever it is invoked from. It reads files only; it never modifies the
# tree and needs no network access. Portable to bash 3.2 (macOS) and GNU/Linux.
#
# Exit codes:
#   0  no suppression comment found
#   1  one or more suppression comments found (each listed as path:line: text on stderr)
#   2  usage error, not inside a git work tree, or git failed
#
# Scope: files tracked by git (untracked files are not scanned), binary files skipped. Excluded:
#   - this script, which necessarily contains the patterns it searches for;
#   - lockfiles (package-lock.json, uv.lock), which are generated and hold no authored code;
#   - node_modules/, which holds third-party code;
#   - Markdown (*.md). This goes beyond excluding only the script, lockfiles and node_modules:
#     README.md and docs/operations.md document this policy and name the markers literally,
#     and a comment in Markdown cannot suppress any tool, so scanning Markdown would report
#     only false positives. Every other tracked text file (source, config, YAML workflows, SQL,
#     JSON, CSS, templates, .cjs/.mjs) is scanned.
#
# Consumer: the `suppressions` job in .github/workflows/ci.yml, a required branch-protection check.

set -euo pipefail

SCRIPT_NAME="$(basename "${BASH_SOURCE[0]}")"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
# Path of this script relative to ROOT, used to exclude it from its own scan.
SELF_PATH="$(basename "${SCRIPT_DIR}")/${SCRIPT_NAME}"

# Longest excerpt of a matching line printed in the report.
MAX_TEXT_CHARS=200

usage() {
  cat <<EOF
Usage: ${SCRIPT_NAME} [-h|--help]

Fails when any tracked file holds a lint, type-check or coverage suppression comment
(AAP 0.11.2 Gate 2; suppression policy AAP 0.13.2).

Exit codes:
  0  no suppression comment found
  1  suppression comments found (listed as path:line: text on stderr)
  2  usage error, not inside a git work tree, or git failed

Scanned: every file tracked by git under ${ROOT}; binary files are skipped.
Excluded: ${SELF_PATH}, lockfiles (package-lock.json, uv.lock), node_modules/,
and Markdown (*.md), which documents the policy and cannot suppress any tool.
EOF
}

case "$#" in
  0) ;;
  1)
    case "$1" in
      -h | --help)
        usage
        exit 0
        ;;
      *)
        printf '%s: unexpected argument: %s\n\n' "${SCRIPT_NAME}" "$1" >&2
        usage >&2
        exit 2
        ;;
    esac
    ;;
  *)
    printf '%s: expected at most one argument, got %s\n\n' "${SCRIPT_NAME}" "$#" >&2
    usage >&2
    exit 2
    ;;
esac

if ! command -v git >/dev/null 2>&1; then
  printf '%s: git is required but was not found on PATH\n' "${SCRIPT_NAME}" >&2
  exit 2
fi

# rev-parse prints "false" with status 0 inside a .git directory, so the answer is compared too.
inside_status=0
inside="$(git -C "${ROOT}" rev-parse --is-inside-work-tree 2>&1)" || inside_status=$?
if [[ "${inside_status}" -ne 0 || "${inside}" != "true" ]]; then
  printf '%s: %s is not inside a git work tree\n' "${SCRIPT_NAME}" "${ROOT}" >&2
  if [[ -n "${inside}" && "${inside}" != "false" ]]; then
    printf '%s\n' "${inside}" >&2
  fi
  exit 2
fi

# Every git call runs from ROOT, so "." below is the repository root whatever the caller's
# working directory, and reported paths are relative to ROOT. grep.column is forced off
# because a caller's git config enabling it would add a column field to every hit.
GIT_AT_ROOT=(git -C "${ROOT}" -c grep.column=false)

# Pathspec exclusions shared by the scan and the file count.
EXCLUDES=(
  ":(exclude)${SELF_PATH}"
  ":(exclude,glob)**/package-lock.json"
  ":(exclude,glob)**/uv.lock"
  ":(exclude,glob)**/node_modules/**"
  ":(exclude,glob)**/*.md"
)

# One git grep over tracked files: -I skips binary files (fonts, images), -i matches every
# case and spacing variant the tools accept, -E enables the alternations below.
# The patterns are the Gate 2 list (the ESLint, TypeScript, Stylelint, mypy, ruff and pyright
# directives) plus every other spelling the same Gate 2 tools honour for the same effect, so a
# suppression cannot pass by switching spelling:
#   - ESLint inline rule configuration (a block comment starting with the tool name and a space);
#   - the ruff file-level forms, including the flake8 spelling ruff also honours;
#   - the ruff formatter and import-sorter skips, which silence `ruff format --check` and the
#     import-order rule;
#   - any mypy inline configuration comment, which overrides the strict pyproject settings just
#     as any pyright comment does;
#   - the coverage exclusion and partial-branch pragmas in every spelling coverage.py's default
#     patterns accept.
# Directives of tools outside the Gate 2 toolchain are not part of this list.
status=0
output="$(
  "${GIT_AT_ROOT[@]}" grep -n -I -i -E --no-color \
    -e 'eslint-disable' \
    -e '/\*[[:space:]]*eslint[[:space:]]' \
    -e '@ts-(ignore|expect-error|nocheck)' \
    -e 'stylelint-disable' \
    -e '#[[:space:]]*type:[[:space:]]*ignore' \
    -e '#[[:space:]]*noqa' \
    -e '#[[:space:]]*(ruff|flake8):[[:space:]]*noqa' \
    -e '#[[:space:]]*(ruff:[[:space:]]*)?(fmt|isort):[[:space:]]*(off|skip)' \
    -e '#[[:space:]]*yapf:[[:space:]]*disable' \
    -e '#[[:space:]]*pyright:' \
    -e '#[[:space:]]*mypy:' \
    -e '#[[:space:]]*pragma(:|[[:space:]])?[[:space:]]*no[[:space:]]*(cover|branch)' \
    -- . "${EXCLUDES[@]}"
)" || status=$?

case "${status}" in
  0)
    printf 'Suppression comments are not permitted (AAP 0.13.2, Gate 2):\n' >&2
    count=0
    # Each hit is "path:line:text". Tracked paths hold no ':' (such paths cannot be checked out
    # on Windows), so the path ends at the first ':' and the line number at the next one.
    while IFS= read -r hit; do
      [[ -n "${hit}" ]] || continue
      path="${hit%%:*}"
      rest="${hit#*:}"
      line_no="${rest%%:*}"
      text="${rest#*:}"
      # Trim leading and trailing whitespace (including a CR from CRLF files).
      text="${text#"${text%%[![:space:]]*}"}"
      text="${text%"${text##*[![:space:]]}"}"
      if [[ "${#text}" -gt "${MAX_TEXT_CHARS}" ]]; then
        text="${text:0:$((MAX_TEXT_CHARS - 3))}..."
      fi
      printf '%s:%s: %s\n' "${path}" "${line_no}" "${text}" >&2
      count=$((count + 1))
    done <<<"${output}"
    printf '%s suppression comment(s) found\n' "${count}" >&2
    exit 1
    ;;
  1)
    if ! file_count="$("${GIT_AT_ROOT[@]}" ls-files -- . "${EXCLUDES[@]}" | wc -l | tr -d '[:space:]')"; then
      printf 'git ls-files failed\n' >&2
      exit 2
    fi
    printf 'No suppression comments found in %s tracked files.\n' "${file_count}"
    exit 0
    ;;
  *)
    printf 'git grep failed (status %s)\n' "${status}" >&2
    exit 2
    ;;
esac
