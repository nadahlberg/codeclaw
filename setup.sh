#!/bin/bash
set -euo pipefail

# setup.sh — Bootstrap script for CodeClaw
# Checks Python environment and installs dependencies.

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_FILE="$PROJECT_ROOT/logs/setup.log"

mkdir -p "$PROJECT_ROOT/logs"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] [bootstrap] $*" >> "$LOG_FILE"; }

# --- Platform detection ---

detect_platform() {
  local uname_s
  uname_s=$(uname -s)
  case "$uname_s" in
    Darwin*) PLATFORM="macos" ;;
    Linux*)  PLATFORM="linux" ;;
    *)       PLATFORM="unknown" ;;
  esac

  IS_WSL="false"
  if [ "$PLATFORM" = "linux" ] && [ -f /proc/version ]; then
    if grep -qi 'microsoft\|wsl' /proc/version 2>/dev/null; then
      IS_WSL="true"
    fi
  fi

  IS_ROOT="false"
  if [ "$(id -u)" -eq 0 ]; then
    IS_ROOT="true"
  fi

  log "Platform: $PLATFORM, WSL: $IS_WSL, Root: $IS_ROOT"
}

# --- Python check ---

check_python() {
  PYTHON_OK="false"
  PYTHON_VERSION="not_found"
  PYTHON_PATH_FOUND=""

  # Try python3 first, then python
  local py_cmd=""
  if command -v python3 >/dev/null 2>&1; then
    py_cmd="python3"
  elif command -v python >/dev/null 2>&1; then
    py_cmd="python"
  fi

  if [ -n "$py_cmd" ]; then
    PYTHON_VERSION=$($py_cmd --version 2>/dev/null | sed 's/^Python //')
    PYTHON_PATH_FOUND=$(command -v "$py_cmd")
    local major minor
    major=$(echo "$PYTHON_VERSION" | cut -d. -f1)
    minor=$(echo "$PYTHON_VERSION" | cut -d. -f2)
    if [ "$major" -ge 3 ] && [ "$minor" -ge 11 ] 2>/dev/null; then
      PYTHON_OK="true"
    fi
    log "Python $PYTHON_VERSION at $PYTHON_PATH_FOUND (major=$major, minor=$minor, ok=$PYTHON_OK)"
  else
    log "Python not found"
  fi
}

# --- pip install ---

install_deps() {
  DEPS_OK="false"

  if [ "$PYTHON_OK" = "false" ]; then
    log "Skipping pip install — Python not available"
    return
  fi

  cd "$PROJECT_ROOT"

  log "Running pip install -e .[dev]"
  if pip install -e ".[dev]" >> "$LOG_FILE" 2>&1; then
    DEPS_OK="true"
    log "pip install succeeded"
  else
    log "pip install failed"
    return
  fi
}

# --- Main ---

log "=== Bootstrap started ==="

detect_platform
check_python
install_deps

# Emit status block
STATUS="success"
if [ "$PYTHON_OK" = "false" ]; then
  STATUS="python_missing"
elif [ "$DEPS_OK" = "false" ]; then
  STATUS="deps_failed"
fi

cat <<EOF
=== CODECLAW SETUP: BOOTSTRAP ===
PLATFORM: $PLATFORM
IS_WSL: $IS_WSL
IS_ROOT: $IS_ROOT
PYTHON_VERSION: $PYTHON_VERSION
PYTHON_OK: $PYTHON_OK
PYTHON_PATH: ${PYTHON_PATH_FOUND:-not_found}
DEPS_OK: $DEPS_OK
STATUS: $STATUS
LOG: logs/setup.log
=== END ===
EOF

log "=== Bootstrap completed: $STATUS ==="

if [ "$PYTHON_OK" = "false" ]; then
  exit 2
fi
if [ "$DEPS_OK" = "false" ]; then
  exit 1
fi
