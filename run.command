#!/usr/bin/env zsh
set -euo pipefail
export LC_ALL=en_US.UTF-8

# -------------------- CONFIG --------------------
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV_DIR="${SCRIPT_DIR}/.venv"
VENV_PYTHON="${VENV_DIR}/bin/python3"
PYTHON_SCRIPT="${SCRIPT_DIR}/yt_mirror_sync.py"
# ------------------------------------------------

info()  { echo "\033[1;34m[INFO]\033[0m  $*"; }
error() { echo "\033[1;31m[ERROR]\033[0m $*" >&2; exit 1; }

# 1. Check for system dependencies (macOS standard is Homebrew)
for cmd in python3 ffmpeg node; do
    if ! command -v "$cmd" >/dev/null 2>&1; then
        error "Missing system requirement: '$cmd'.\nPlease install Homebrew (https://brew.sh) and run:\n  brew install python ffmpeg node"
    fi
done

# 2. Bootstrap standard Python venv if it doesn't exist
if [[ ! -d "$VENV_DIR" ]]; then
    info "First run detected. Setting up isolated Python environment..."
    
    # Create the venv using the system's python3
    python3 -m venv "$VENV_DIR"
    
    info "Installing Python dependencies..."
    # BULLETPROOFING: Call the venv's python directly via absolute path.
    # This completely bypasses Conda/Homebrew and prevents PEP-668 externally-managed errors.
    "$VENV_PYTHON" -m pip install --upgrade --quiet pip
    "$VENV_PYTHON" -m pip install --quiet yt-dlp mutagen
    
    info "Setup complete!"
fi

# 3. Run the application using the isolated Python
echo "--------------------------------------------------"
"$VENV_PYTHON" "$PYTHON_SCRIPT"
echo "--------------------------------------------------"

# Keep terminal open if double-clicked from Finder
if [[ "$(ps -o comm= -p $PPID)" == *Terminal* || "$TERM_PROGRAM" == "Apple_Terminal" ]]; then
    echo
    read -s -k '?Press any key to close...'
fi