#!/bin/bash

# Get the directory where this script is located (resolving symlinks)
SCRIPT_PATH="$(readlink -f "${BASH_SOURCE[0]}")"
SCRIPT_DIR="$( cd "$( dirname "$SCRIPT_PATH" )" &> /dev/null && pwd )"

# Navigate to the script directory to ensure relative paths work correctly
cd "$SCRIPT_DIR"

# Define the python executable to use
PYTHON_EXE="python3"
if [ -d "$SCRIPT_DIR/venv/bin" ]; then
    PYTHON_EXE="$SCRIPT_DIR/venv/bin/python3"
elif [ -d "$SCRIPT_DIR/.venv/bin" ]; then
    PYTHON_EXE="$SCRIPT_DIR/.venv/bin/python3"
elif [ -d "$SCRIPT_DIR/scripts/venv/bin" ]; then
    PYTHON_EXE="$SCRIPT_DIR/scripts/venv/bin/python3"
fi

# Execute the main script with all passed arguments
exec "$PYTHON_EXE" "$SCRIPT_DIR/main.py" "$@"
