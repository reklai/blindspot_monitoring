#!/usr/bin/env bash
#
# Run the test suite inside the project's virtual environment.
#
#   ./test.sh              # all tests
#   ./test.sh -k config    # any pytest arguments pass through
#
# Expects a .venv created with system site packages so the distro's PyQt6
# and OpenCV are visible (the installer does not create it):
#
#   python3 -m venv --system-site-packages .venv
#
# pytest and pytest-qt are installed into the venv on first run if missing.
# Tests run headless (QT_QPA_PLATFORM=offscreen) unless that is already set.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

if [[ ! -d ".venv" ]]; then
  cat >&2 <<'EOF'
Error: .venv not found. Create it with:
  python3 -m venv --system-site-packages .venv
EOF
  exit 1
fi

# shellcheck disable=SC1091
source .venv/bin/activate

if ! python3 -c "import pytest, pytestqt" 2>/dev/null; then
  echo "Installing pytest and pytest-qt..."
  pip install --quiet pytest pytest-qt
fi

if ! python3 -c "from PyQt6 import QtCore, QtWidgets; import cv2" 2>/dev/null; then
  echo "Warning: PyQt6 or OpenCV is not importable; widget and camera tests will fail." >&2
  echo "Ensure the venv was created with --system-site-packages, or pip install them." >&2
fi

export QT_QPA_PLATFORM="${QT_QPA_PLATFORM:-offscreen}"

echo "Running tests..."
echo "========================================"

# Capture pytest's exit code explicitly: under `set -e` a failing pytest
# would otherwise end the script before the summary below.
exit_code=0
python3 -m pytest tests/ "$@" || exit_code=$?

echo "========================================"
if [[ $exit_code -eq 0 ]]; then
  echo "All tests passed!"
else
  echo "Some tests failed. Exit code: $exit_code"
fi
exit "$exit_code"
