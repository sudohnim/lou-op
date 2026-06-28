#!/usr/bin/env bash
# bin/lint.sh — run black, isort, flake8, and mypy
# Usage:
#   ./bin/lint.sh          # check only (no changes)
#   ./bin/lint.sh --fix    # auto-fix black + isort, then check
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
cd "$PROJECT_DIR"

FIX=false
if [[ "${1:-}" == "--fix" ]]; then
  FIX=true
fi

BOLD='\033[1m'
GREEN='\033[0;32m'
RED='\033[0;31m'
NC='\033[0m'

FAILED=0

run_check() {
  local name="$1"
  shift
  echo -e "\n${BOLD}── $name${NC}"
  if "$@"; then
    echo -e "${GREEN}✓ $name passed${NC}"
  else
    echo -e "${RED}✗ $name failed${NC}"
    FAILED=$((FAILED + 1))
  fi
}

if $FIX; then
  echo -e "${BOLD}Auto-fixing with black and isort...${NC}"
  black lou_op/ tests/
  isort lou_op/ tests/
  echo ""
fi

run_check "black"  black --check lou_op/ tests/
run_check "isort"  isort --check-only lou_op/ tests/
run_check "flake8" flake8 lou_op/
run_check "mypy"   mypy lou_op/

echo ""
if [[ $FAILED -eq 0 ]]; then
  echo -e "${GREEN}${BOLD}All checks passed.${NC}"
else
  echo -e "${RED}${BOLD}$FAILED check(s) failed.${NC}"
  exit 1
fi
