#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

VENV_DIR="$SCRIPT_DIR/venv"
VENV_PYTHON=""
VENV_PIP=""

setup_venv() {
    echo "==> Checking Python virtual environment..."

    if [ ! -d "$VENV_DIR" ]; then
        echo "--> No venv found at $VENV_DIR. Creating..."
        python3 -m venv "$VENV_DIR" || python -m venv "$VENV_DIR"
        echo "--> Venv created."
    else
        echo "--> Existing venv found."
    fi

    # Detect the python binary inside the venv
    if [ -f "$VENV_DIR/bin/python" ]; then
        VENV_PYTHON="$VENV_DIR/bin/python"
        VENV_PIP="$VENV_DIR/bin/pip"
    elif [ -f "$VENV_DIR/Scripts/python.exe" ]; then
        VENV_PYTHON="$VENV_DIR/Scripts/python.exe"
        VENV_PIP="$VENV_DIR/Scripts/pip.exe"
    else
        echo "ERROR: Could not find python binary in venv."
        exit 1
    fi
}

install_deps() {
    echo "==> Installing/updating requirements from requirements.txt..."
    "$VENV_PIP" install --upgrade pip --quiet
    "$VENV_PIP" install -r requirements.txt --quiet
    "$VENV_PIP" install pytest --quiet
    echo "--> Dependencies installed."
}

run_tests() {
    echo "==> Running tests with pytest..."
    mkdir -p "$SCRIPT_DIR/test-reports"
    "$VENV_PYTHON" -m pytest tests/ -v --junitxml="$SCRIPT_DIR/test-reports/results.xml" "$@"
    echo "--> Tests complete."
}

run_frontend_checks() {
    if ! command -v npm >/dev/null 2>&1; then
        echo "==> Skipping frontend checks: npm not found on this machine."
        return
    fi
    if [ ! -f "$SCRIPT_DIR/web/package.json" ]; then
        echo "==> Skipping frontend checks: web/package.json not found."
        return
    fi
    echo "==> Running frontend typecheck (web/)..."
    (cd "$SCRIPT_DIR/web" && npm install --silent && npm run typecheck)
    echo "--> Frontend typecheck complete."
}

main() {
    setup_venv
    install_deps
    run_tests "$@"
    run_frontend_checks
}

main "$@"
