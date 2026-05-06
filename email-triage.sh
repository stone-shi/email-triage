#!/bin/bash

# Get the directory where this script is located
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )"

# Navigate to the script directory to ensure relative paths work correctly
cd "$SCRIPT_DIR"

# Define the python executable to use
PYTHON_EXE="python3"
if [ -d "$SCRIPT_DIR/venv/bin" ]; then
    PYTHON_EXE="$SCRIPT_DIR/venv/bin/python3"
elif [ -d "$SCRIPT_DIR/.venv/bin" ]; then
    PYTHON_EXE="$SCRIPT_DIR/.venv/bin/python3"
fi

# Execute the main script with all passed arguments
exec "$PYTHON_EXE" "$SCRIPT_DIR/main.py" "$@"
