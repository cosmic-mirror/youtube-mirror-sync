#!/usr/bin/env zsh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV_DIR="${SCRIPT_DIR}/.venv"
VENV_PYTHON="${VENV_DIR}/bin/python3"

echo "\033[1;34m[INFO]\033[0m Updating yt-mirror-sync dependencies..."

if [[ ! -d "$VENV_DIR" ]]; then
    echo "Environment not found. Please run 'run.command' first."
    exit 1
fi

# Update system dependencies via Homebrew if available
if command -v brew >/dev/null 2>&1; then
    echo "Checking for Homebrew updates (ffmpeg, node)..."
    brew upgrade ffmpeg node || true
fi

# Update Python dependencies securely using the absolute path
echo "Updating pip, yt-dlp, and mutagen..."
"$VENV_PYTHON" -m pip install --upgrade pip yt-dlp mutagen

echo "\033[1;32m[SUCCESS]\033[0m Updates complete!"

if [[ "$(ps -o comm= -p $PPID)" == *Terminal* || "$TERM_PROGRAM" == "Apple_Terminal" ]]; then
    echo
    read -s -k '?Press any key to close...'
fi