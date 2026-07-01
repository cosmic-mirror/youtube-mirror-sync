#!/usr/bin/env zsh
set -euo pipefail
export LC_ALL=en_US.UTF-8

# -------------------- CONFIG --------------------
ENV_NAME="yt-mirror-env"
PY_VERSION="3.10"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PYTHON_SCRIPT="${SCRIPT_DIR}/yt_mirror_sync.py"
# ------------------------------------------------

info()  { echo "[INFO]  $*"; }
error() { echo "[ERROR] $*" >&2; exit 1; }

initialize_conda() {
    if command -v conda >/dev/null 2>&1; then return 0; fi
    for f in "${HOME}/.zprofile" "${HOME}/.zshenv" "${HOME}/.zshrc"; do
        if [[ -f "$f" ]]; then source "$f" >/dev/null 2>&1 || true; fi
    done
    if command -v conda >/dev/null 2>&1; then return 0; fi
    local candidates=("${HOME}/miniconda3/etc/profile.d/conda.sh" "${HOME}/anaconda3/etc/profile.d/conda.sh")
    for c in "${candidates[@]}"; do
        if [[ -f "$c" ]]; then source "$c" || true; return 0; fi
    done
    return 1
}

info "Checking environment and dependencies..."

if ! initialize_conda; then
    error "Conda not found. Please install Miniconda or Anaconda."
fi

source "$(conda info --base)/etc/profile.d/conda.sh"

manager="conda"
if command -v mamba >/dev/null 2>&1; then manager="mamba"; fi

# 1. Create the env if it doesn't exist
if ! conda info --envs | grep -q "^${ENV_NAME}\s"; then
    info "Creating environment '${ENV_NAME}'..."
    "$manager" create -y -n "${ENV_NAME}" "python=${PY_VERSION}" -c conda-forge pip
fi

conda activate "${ENV_NAME}"

# -------------------------------------------------------
# ASK FOR UPDATES (Default: No)
# -------------------------------------------------------
echo
echo -n "Check for updates (ffmpeg, nodejs, pip packages)? [y/N] "
read -r response

if [[ "$response" =~ ^[yY] ]]; then
    # 2. Ensure core dependencies are installed (This fixes the JS/FFmpeg issues)
    info "Ensuring system dependencies (FFmpeg, Node.js) are present..."
    "$manager" install --yes --quiet -c conda-forge ffmpeg nodejs

    # 3. Update python packages
    info "Updating Python packages..."
    python -m pip install --upgrade --quiet pip yt-dlp mutagen
else
    info "Skipping updates."
fi
# -------------------------------------------------------

info "Environment ready. Starting sync..."
echo "--------------------------------------------------"
python "$PYTHON_SCRIPT"
echo "--------------------------------------------------"

if [[ "$(ps -o comm= -p $PPID)" == *Terminal* || "$TERM_PROGRAM" == "Apple_Terminal" ]]; then
    echo
    read -s -k '?Press any key to close...'
fi
