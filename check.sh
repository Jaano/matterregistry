#!/usr/bin/env bash
# check.sh - lint, format-check, typecheck, migration-diff, and unit tests
# Usage: ./check.sh [--fix]  (--fix applies ruff auto-fixes and format)
set -euo pipefail

FIX=0
for arg in "$@"; do
  [[ "$arg" == "--fix" ]] && FIX=1
done

VENV_PYTHON=".venv/bin/python"
VENV_RUFF=".venv/bin/ruff"
VENV_MYPY=".venv/bin/mypy"
VENV_VULTURE=".venv/bin/vulture"
VENV_ALEMBIC=".venv/bin/alembic"
VENV_PYTEST=".venv/bin/pytest"

# Verify dev tooling is installed
if [[ ! -x "$VENV_RUFF" || ! -x "$VENV_MYPY" || ! -x "$VENV_VULTURE" ]]; then
  echo "dev tools not found - run: pip install -e '.[dev]'"
  exit 1
fi

PASS=0
FAIL=0

run_step() {
  local label="$1"; shift
  echo ""
  echo "── $label ──"
  if "$@"; then
    echo "✓ $label passed"
    PASS=$((PASS + 1))
  else
    echo "✗ $label FAILED"
    FAIL=$((FAIL + 1))
  fi
}

# ── ruff lint ────────────────────────────────────────────────────────────────
if [[ $FIX -eq 1 ]]; then
  run_step "ruff lint (--fix)" "$VENV_RUFF" check --fix app/ tests/ migrations/ scripts/
else
  run_step "ruff lint" "$VENV_RUFF" check app/ tests/ migrations/ scripts/
fi

# ── ruff format ──────────────────────────────────────────────────────────────
if [[ $FIX -eq 1 ]]; then
  run_step "ruff format (--fix)" "$VENV_RUFF" format app/ tests/ migrations/ scripts/
else
  run_step "ruff format check" "$VENV_RUFF" format --check app/ tests/ migrations/ scripts/
fi

# ── mypy typecheck ───────────────────────────────────────────────────────────
run_step "mypy" "$VENV_MYPY" app/

# ── vulture dead code ────────────────────────────────────────────────────────
run_step "vulture" "$VENV_VULTURE" app/ --min-confidence 61

# ── alembic migration diff ───────────────────────────────────────────────────
# Checks that all model changes are reflected in a migration file.
# Spins up a temp SQLite DB, runs upgrade head, then checks for outstanding
# autogenerate diffs.
if [[ -x "$VENV_ALEMBIC" ]]; then
  echo ""
  echo "── alembic migration diff ──"
  TMPDB=$(mktemp /tmp/matterregistry_check_XXXXXX)
  ALEMBIC_DATABASE_URL="sqlite:///$TMPDB" "$VENV_ALEMBIC" upgrade head 2>/dev/null
  DIFF_OUTPUT=$(ALEMBIC_DATABASE_URL="sqlite:///$TMPDB" "$VENV_ALEMBIC" check 2>&1) || true
  rm -f "$TMPDB"
  if echo "$DIFF_OUTPUT" | grep -q "No new upgrade operations detected"; then
    echo "✓ alembic migration diff passed"
    PASS=$((PASS + 1))
  else
    echo "✗ alembic migration diff FAILED - model changes not covered by a migration:"
    echo "$DIFF_OUTPUT"
    FAIL=$((FAIL + 1))
  fi
else
  echo ""
  echo "── alembic migration diff - skipped (alembic not in .venv) ──"
fi

# ── unit tests ───────────────────────────────────────────────────────────────
if [[ -x "$VENV_PYTEST" ]]; then
  run_step "unit tests" "$VENV_PYTEST" tests/unit/ -q --tb=short
else
  echo ""
  echo "── unit tests - skipped (pytest not in .venv) ──"
fi

# ── summary ──────────────────────────────────────────────────────────────────
echo ""
echo "══════════════════════════════════════"
echo "  Results: $PASS passed, $FAIL failed"
echo "══════════════════════════════════════"
[[ $FAIL -eq 0 ]]
