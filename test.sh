#!/usr/bin/env bash
set -euo pipefail

# Run the test suite from the repository's development environment.
# Usage:
#   ./test.sh
#   ./test.sh -v
#   ./test.sh -k "config"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Activate the repository-local development environment.
if [[ ! -d ".venv" ]]; then
    echo "Error: Virtual environment not found."
    echo "Run ./install.sh first to set up the environment."
    exit 1
fi

# This source is intentionally dynamic, so ShellCheck cannot resolve it.
# shellcheck disable=SC1091
source .venv/bin/activate

# Bootstrap both test packages only when pytest itself is absent.
if ! python3 -c "import pytest" 2>/dev/null; then
    echo "Installing pytest and pytest-qt..."
    pip install --quiet pytest pytest-qt
fi

# Report Qt/capture dependency problems before pytest begins collection.
if ! python3 -c "from PyQt6 import QtCore, QtWidgets; import cv2" 2>/dev/null; then
    echo "Warning: Some core imports failed. Tests may not run correctly."
    echo "Ensure PyQt6 and OpenCV are installed."
fi

echo "Running tests..."
echo "========================================"

python3 -m pytest tests/ "$@"

exit_code=$?

echo "========================================"

if [[ $exit_code -eq 0 ]]; then
    echo "All tests passed!"
else
    echo "Some tests failed. Exit code: $exit_code"
fi

exit $exit_code
