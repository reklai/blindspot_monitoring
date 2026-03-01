#!/usr/bin/env bash
set -euo pipefail

# Camera Dashboard Test Runner
# Run all tests with pytest
#
# Usage:
#   ./test.sh              # Run all tests
#   ./test.sh -v           # Run with verbose output
#   ./test.sh -k "config"  # Run only tests matching "config"
#   ./test.sh --cov        # Run with coverage report

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# ---------- Check virtual environment ----------

if [[ ! -d ".venv" ]]; then
    echo "Error: Virtual environment not found."
    echo "Run ./install.sh first to set up the environment."
    exit 1
fi

# Activate virtual environment
# shellcheck disable=SC1091
source .venv/bin/activate

# ---------- Check pytest is installed ----------

if ! python3 -c "import pytest" 2>/dev/null; then
    echo "Installing pytest and pytest-qt..."
    pip install --quiet pytest pytest-qt
fi

# ---------- Verify core imports ----------

if ! python3 -c "from PyQt6 import QtCore, QtWidgets; import cv2" 2>/dev/null; then
    echo "Warning: Some core imports failed. Tests may not run correctly."
    echo "Ensure PyQt6 and OpenCV are installed."
fi

# ---------- Run tests ----------

echo "Running tests..."
echo "========================================"

# Pass any arguments to pytest
python3 -m pytest tests/ "$@"

exit_code=$?

echo "========================================"

if [[ $exit_code -eq 0 ]]; then
    echo "All tests passed!"
else
    echo "Some tests failed. Exit code: $exit_code"
fi

exit $exit_code
