#!/usr/bin/env bash

set -Eeuo pipefail

dirpath=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)

pause() {
    if [ -t 0 ] && [ "${1:-}" != "--nopause" ]; then
        read -rp "Press any key to continue..."
    fi
}

# Check if git is installed
if ! command -v git &> /dev/null; then
    echo
    echo "No git executable found in PATH!"
    echo
    pause "${1:-}"
    exit 1
fi

# Check if the virtual environment exists
if [ ! -d "$dirpath/env" ]; then
    echo
    echo "Creating the env folder..."
    if ! python3 -m venv "$dirpath/env"; then
        echo
        echo "No python executable found in PATH or failed to create virtual environment!"
        echo
        pause "${1:-}"
        exit 1
    fi
fi

# Activate the virtual environment and install requirements
echo
echo "Installing requirements.txt..."
if ! (
    "$dirpath/env/bin/python" -m pip install -U pip && \
    "$dirpath/env/bin/pip" install wheel && \
    "$dirpath/env/bin/pip" install -r "$dirpath/requirements.txt"
); then
    echo
    echo "Failed to install requirements."
    echo
    pause "${1:-}"
    exit 1
fi

echo
echo "Environment setup completed successfully."
echo
pause "${1:-}"

