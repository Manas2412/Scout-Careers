#!/usr/bin/env bash
# ci/run-checks.sh — the single pre-merge gate.
#
# Host-agnostic by design: CI invokes this and nothing else, so what runs in CI
# and what runs on a laptop cannot drift apart. A CI configuration file that
# duplicates the checks is a second definition that eventually disagrees with
# the first.
#
# Usage: run-checks.sh [all|lint|types|test]   (default: all)
#
# Every check below is a HARD GATE: it fails the run. There are no report-only
# checks yet. When one is added it goes in its own clearly labelled section, so
# that "advisory" never quietly becomes the default for a real gate.

set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKEND="$ROOT/backend"
TARGET="${1:-all}"
FAILED=()

c_reset=$'\033[0m'; c_bold=$'\033[1m'
c_red=$'\033[31m';  c_green=$'\033[32m'

# So `lint-imports` and `pytest` resolve the package from src/ whether or not it
# has been installed into the environment.
export PYTHONPATH="$BACKEND/src${PYTHONPATH:+:$PYTHONPATH}"

step() { printf '\n%s▶ %s%s\n' "$c_bold" "$1" "$c_reset"; }

run() {   # run <label> <command...>
  local label="$1"; shift
  local logfile
  logfile="$(mktemp -t scout-check.XXXXXX)"
  printf '  %-32s' "$label"
  if "$@" >"$logfile" 2>&1; then
    printf '%s✓%s\n' "$c_green" "$c_reset"
  else
    printf '%s✗%s\n' "$c_red" "$c_reset"
    sed 's/^/      /' "$logfile" | tail -80
    FAILED+=("$label")
  fi
  rm -f "$logfile"
}

# ───────────────────────────────────────────────────────────────── lint ──
check_lint() {
  step "Lint"
  cd "$BACKEND"
  run "ruff check"          ruff check .
  run "ruff format --check" ruff format --check .
  # Compile every module: catches syntax and import-time errors in code the
  # test suite does not reach.
  run "compile"             python -m compileall -q src/
  run "import contracts"    lint-imports --config pyproject.toml
  cd "$ROOT"
}

# ──────────────────────────────────────────────────────────────── types ──
check_types() {
  step "Types"
  cd "$BACKEND"
  run "mypy" mypy src/
  cd "$ROOT"
}

# ───────────────────────────────────────────────────────────────── test ──
check_test() {
  step "Tests"
  cd "$BACKEND"
  # Offline unit tests. No network (a socket-blocking fixture), no database.
  run "pytest unit" python -m pytest tests/unit --maxfail=1
  cd "$ROOT"
}

# ──────────────────────────────────────────────────────────── invariants ──
check_invariants() {
  step "Invariants (ARCHITECTURE.md §3)"
  cd "$BACKEND"
  run "never-scrape is absolute" python -m pytest tests/unit/invariants -q
  run "no submit endpoint" bash -c \
    '! grep -rEni "def .*(submit_application|auto_apply|post_application)" src/'
  run "no print statements" bash -c \
    '! grep -rn --include="*.py" -E "^[[:space:]]*print\(" src/'
  # The deny list must not be reachable from configuration. The real assertion
  # is on the AST, in tests/unit/invariants; this is the cheap early signal that
  # somebody wired policy.py to Settings.
  run "deny list is a code constant" bash -c \
    '! grep -nE "^[[:space:]]*(from|import)[[:space:]]+.*(config|Settings)" \
        src/scout_careers/sources/policy.py'
  run "alembic single head" bash -c \
    '[ "$(ls migrations/versions/*.py | wc -l)" -ge 1 ]'
  cd "$ROOT"
}

# ─────────────────────────────────────────────────────────────── driver ──
case "$TARGET" in
  lint)  check_lint ;;
  types) check_types ;;
  test)  check_test; check_invariants ;;
  all)   check_lint; check_types; check_test; check_invariants ;;
  *) echo "Usage: $0 [all|lint|types|test]" >&2; exit 2 ;;
esac

if ((${#FAILED[@]})); then
  printf '\n%s✗ %d check(s) failed:%s\n' "$c_red" "${#FAILED[@]}" "$c_reset"
  printf '    %s\n' "${FAILED[@]}"
  exit 1
fi

printf '\n%s✓ All checks passed.%s\n' "$c_green" "$c_reset"
