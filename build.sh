#!/usr/bin/env bash

set -Eeuo pipefail

dirpath=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)

pause() {
    if [ -t 0 ] && [ "${1:-}" != "--nopause" ]; then
        read -rp "Press any key to continue..."
    fi
}

# Check if the virtual environment exists
if [ ! -d "$dirpath/env" ]; then
    echo
    echo "No virtual environment found! Run setup_env.sh to set it up first."
    echo
    pause "${1:-}"
    exit 1
fi

# Check if PyInstaller is installed in the virtual environment
if [ ! -f "$dirpath/env/bin/pyinstaller" ]; then
    echo
    echo "Installing PyInstaller..."
    if ! "$dirpath/env/bin/pip" install pyinstaller; then
        echo
        echo "Failed to install PyInstaller."
        echo
        pause "${1:-}"
        exit 1
    fi
fi

# Run PyInstaller with the specified build spec file
echo
echo "Building..."
if ! "$dirpath/env/bin/pyinstaller" "$dirpath/build.spec"; then
    echo
    echo "PyInstaller build failed."
    echo
    pause "${1:-}"
    exit 1
fi

echo
echo "Build completed successfully."
echo
pause "${1:-}"

