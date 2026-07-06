#!/usr/bin/env bash
# Run the test suite. Unit tests run locally; integration tests hit TEST_TARGET.
# Point TEST_TARGET at your running container (default http://localhost:5591).
# Override with: TEST_TARGET=http://host:5591 ./test.sh

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")" && pwd)"
ENV_FILE="$REPO_ROOT/.env"

if [[ -f "$ENV_FILE" ]]; then
  set -o allexport
  source "$ENV_FILE"
  set +o allexport
fi

export TEST_TARGET="${TEST_TARGET:-http://localhost:5591}"

echo "==> Target: $TEST_TARGET"
echo "==> Running tests..."
cd "$REPO_ROOT"
pytest -v tests/integration/
