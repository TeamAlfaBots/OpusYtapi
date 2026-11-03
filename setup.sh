#!/usr/bin/env bash
set -uo pipefail

REPO_URL="PUT_YOUR_GITHUB_REPO_URL_HERE"  # e.g. https://github.com/yourname/OpusApi.git
REPO_DIR="OpusApi"
APP_NAME="opus-api"
APP_MODULE="OpusApi.main:app"
PORT=8080
TMUX_SESSION="opus_api"
VENV_DIR="yvenv"
LOG_FILE="opus_api.log"
TEST_VIDEO_ID="dQw4w9WgXcQ"
MIN_PY_MAJOR=3
MIN_PY_MINOR=9

log() { echo "[*] $1"; }
err() { echo "[!] $1" >&2; }

sudo_if_needed() {
    if [ "$EUID" -eq 0 ]; then
        "$@"
    else
        sudo "$@"
    fi
}

check_python() {
    log "Checking Python version..."
    if ! command -v python3 &>/dev/null; then
        err "python3 not found. Install Python ${MIN_PY_MAJOR}.${MIN_PY_MINOR}+ first."
        exit 1
    fi
    PY_VER=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
    PY_MAJOR=$(echo "$PY_VER" | cut -d. -f1)
    PY_MINOR=$(echo "$PY_VER" | cut -d. -f2)
    if [ "$PY_MAJOR" -lt "$MIN_PY_MAJOR" ] || { [ "$PY_MAJOR" -eq "$MIN_PY_MAJOR" ] && [ "$PY_MINOR" -lt "$MIN_PY_MINOR" ]; }; then
        err "Python $PY_VER found, need >= ${MIN_PY_MAJOR}.${MIN_PY_MINOR}"
        exit 1
    fi
    log "Python $PY_VER OK"
}

ensure_pkg() {
    # $1 = command to check, $2 = apt package name
    local cmd="$1" pkg="$2"
    log "Checking ${cmd}..."
    if command -v "$cmd" &>/dev/null; then
        log "${cmd} found"
        return
    fi
    if ! command -v apt-get &>/dev/null; then
        err "${cmd} missing and apt-get not available. Install ${pkg} manually."
        exit 1
    fi
    log "${cmd} not found, installing ${pkg}..."
    sudo_if_needed apt-get update -qq
    sudo_if_needed apt-get install -y "$pkg" -qq
    if ! command -v "$cmd" &>/dev/null; then
        err "Could not install ${pkg} automatically. Run manually: sudo apt install ${pkg}"
        exit 1
    fi
    log "${pkg} installed"
}

clone_or_update_repo() {
    log "Setting up repo ${REPO_DIR}..."
    if [ -d "$REPO_DIR/.git" ]; then
        log "Repo already exists, pulling latest..."
        (cd "$REPO_DIR" && git pull --ff-only) || err "git pull failed, continuing with existing copy"
    else
        if [ -d "$REPO_DIR" ]; then
            err "${REPO_DIR} exists but is not a git repo, removing and re-cloning..."
            rm -rf "$REPO_DIR"
        fi
        log "Cloning ${REPO_URL}..."
        git clone "$REPO_URL" "$REPO_DIR"
    fi
    cd "$REPO_DIR" || { err "Could not cd into ${REPO_DIR}"; exit 1; }
    log "Now working inside $(pwd)"
}

ensure_venv_support() {
    log "Checking venv/ensurepip support..."
    if python3 -c "import ensurepip" &>/dev/null; then
        log "ensurepip OK"
        return
    fi
    if ! command -v apt-get &>/dev/null; then
        err "ensurepip missing and apt-get not available. Install venv support manually for Python ${PY_VER}."
        exit 1
    fi
    log "ensurepip missing, installing python${PY_VER}-venv..."
    sudo_if_needed apt-get update -qq
    if ! sudo_if_needed apt-get install -y "python${PY_VER}-venv" -qq; then
        log "Versioned package not found, trying generic python3-venv..."
        sudo_if_needed apt-get install -y python3-venv -qq
    fi
    if ! python3 -c "import ensurepip" &>/dev/null; then
        err "Could not install venv support automatically. Run manually: sudo apt install python${PY_VER}-venv"
        exit 1
    fi
    log "venv support installed"
}

setup_venv() {
    if [ -d "$VENV_DIR" ] && [ ! -f "$VENV_DIR/bin/activate" ]; then
        log "Found broken/incomplete ${VENV_DIR} from a previous failed run, removing it..."
        rm -rf "$VENV_DIR"
    fi
    if [ ! -d "$VENV_DIR" ]; then
        log "Creating ${VENV_DIR}..."
        python3 -m venv "$VENV_DIR"
        if [ ! -f "$VENV_DIR/bin/activate" ]; then
            err "venv creation failed. Cleaning up broken ${VENV_DIR} directory..."
            rm -rf "$VENV_DIR"
            exit 1
        fi
    else
        log "${VENV_DIR} already exists, skipping"
    fi
    # shellcheck disable=SC1091
    source "$VENV_DIR/bin/activate"
}

check_deno() {
    log "Checking deno..."
    if command -v deno &>/dev/null; then
        log "deno found: $(deno --version | head -1)"
    else
        log "deno not found, installing..."
        curl -fsSL https://deno.land/install.sh | sh
        export PATH="$HOME/.deno/bin:$PATH"
        if ! command -v deno &>/dev/null; then
            err "deno install failed. Add \$HOME/.deno/bin to PATH manually and re-run."
            exit 1
        fi
        log "deno installed: $(deno --version | head -1)"
    fi
}

install_requirements() {
    if [ -f "requirements.txt" ]; then
        log "Installing requirements..."
        pip install --upgrade pip -q
        pip install -r requirements.txt -q
    else
        err "requirements.txt not found in ${REPO_DIR}, skipping"
    fi
}

port_in_use() {
    local port=$1
    if command -v ss &>/dev/null; then
        ss -tuln | grep -q ":${port} "
    elif command -v lsof &>/dev/null; then
        lsof -i ":${port}" &>/dev/null
    else
        (echo > "/dev/tcp/127.0.0.1/${port}") &>/dev/null
    fi
}

get_port_pid() {
    local port=$1
    if command -v lsof &>/dev/null; then
        lsof -ti ":${port}"
    else
        fuser "${port}/tcp" 2>/dev/null
    fi
}

check_port_and_zombies() {
    log "Checking port ${PORT}..."
    if port_in_use "$PORT"; then
        PID=$(get_port_pid "$PORT")
        CMD=$(ps -p "$PID" -o cmd= 2>/dev/null || true)
        if echo "$CMD" | grep -q "$APP_MODULE"; then
            log "Port ${PORT} busy with an old/zombie instance of ${APP_NAME} (PID ${PID}). Killing it..."
            kill -9 "$PID" 2>/dev/null
            sleep 1
        else
            err "Port ${PORT} is busy with an unrelated process (PID ${PID}): ${CMD}"
            err "Free the port manually or change PORT in this script."
            exit 1
        fi
    else
        log "Port ${PORT} is free"
    fi

    log "Checking for any leftover duplicate processes..."
    PIDS=$(pgrep -f "$APP_MODULE" || true)
    if [ -n "$PIDS" ]; then
        log "Found duplicate process(es), killing: $PIDS"
        kill -9 $PIDS 2>/dev/null
        sleep 1
    fi
    tmux kill-session -t "$TMUX_SESSION" 2>/dev/null || true
}

start_tmux() {
    log "Starting ${APP_NAME} in tmux session '${TMUX_SESSION}'..."
    tmux new-session -d -s "$TMUX_SESSION" \
        "cd $(pwd) && source ${VENV_DIR}/bin/activate && uvicorn ${APP_MODULE} --host 0.0.0.0 --port ${PORT} 2>&1 | tee ${LOG_FILE}"
    sleep 5
    if ! tmux has-session -t "$TMUX_SESSION" 2>/dev/null; then
        err "tmux session died immediately. Check ${LOG_FILE} in ${REPO_DIR}."
        exit 1
    fi
}

verify_running() {
    log "Verifying server is up..."
    for i in $(seq 1 10); do
        CODE=$(curl -s -o /dev/null -w "%{http_code}" "http://127.0.0.1:${PORT}/")
        if [ "$CODE" == "200" ]; then
            log "Server responded 200 OK"
            return 0
        fi
        sleep 2
    done
    err "Server did not respond after 20s. Recent log:"
    tmux capture-pane -t "$TMUX_SESSION" -p | tail -30
    exit 1
}

test_download() {
    log "Testing a real song download (video: ${TEST_VIDEO_ID})..."
    if [ -z "${TEST_API_KEY:-}" ]; then
        log "TEST_API_KEY not set, skipping the download test (the API now needs a key)"
        return 0
    fi
    RESP=$(curl -s -H "X-API-Key: ${TEST_API_KEY}" "http://127.0.0.1:${PORT}/v1/download?url=${TEST_VIDEO_ID}&type=audio")
    TOKEN=$(echo "$RESP" | grep -o '"download_token":"[^"]*"' | cut -d'"' -f4)
    if [ -z "$TOKEN" ]; then
        err "Failed to get download token. Response: $RESP"
        exit 1
    fi
    log "Token received, requesting stream..."
    HTTP_CODE=$(curl -s -o /tmp/test_song.m4a -w "%{http_code}" "http://127.0.0.1:${PORT}/v1/stream/${TEST_VIDEO_ID}?token=${TOKEN}&type=audio")
    if [ "$HTTP_CODE" == "200" ] && [ -s /tmp/test_song.m4a ]; then
        SIZE=$(du -h /tmp/test_song.m4a | cut -f1)
        log "SUCCESS - test song downloaded (${SIZE}). API is fully working."
        rm -f /tmp/test_song.m4a
    else
        err "Stream test failed (HTTP ${HTTP_CODE})"
        exit 1
    fi
}

main() {
    check_python
    ensure_pkg git git
    ensure_pkg tmux tmux
    ensure_pkg ffmpeg ffmpeg
    ensure_pkg unzip unzip
    clone_or_update_repo
    ensure_venv_support
    setup_venv
    check_deno
    install_requirements
    check_port_and_zombies
    start_tmux
    verify_running
    test_download
    log "All done. Attach anytime with: tmux attach -t ${TMUX_SESSION}"
    log "Project directory: $(pwd)"
}

main
