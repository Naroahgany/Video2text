#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."
PROJECT_ROOT="$(pwd)"
RUNTIME_DIR="$PROJECT_ROOT/runtime"
DOWNLOADS_DIR="$RUNTIME_DIR/downloads"
TOOLS_DIR="$RUNTIME_DIR/tools"
BROWSER_CACHE_DIR="$RUNTIME_DIR/browser-cache"
DATA_DIR="$PROJECT_ROOT/data"
LOGS_DIR="$PROJECT_ROOT/logs"
TEMP_DIR_PATH="$DATA_DIR/temp"
VENV_DIR="$RUNTIME_DIR/.venv"
DEPENDENCY_STAMP="$RUNTIME_DIR/.dependencies-installed"
REQUIREMENTS_FILE="$PROJECT_ROOT/backend/requirements.txt"
PYTHON_PKG="$DOWNLOADS_DIR/python-3.12.8-macos11.pkg"
PYTHON_URL="${VIDEO2TEXT_PYTHON_URL:-https://mirrors.tuna.tsinghua.edu.cn/python/3.12.8/python-3.12.8-macos11.pkg}"
DEFAULT_PIP_INDEX_URL="https://pypi.tuna.tsinghua.edu.cn/simple"
DEFAULT_PLAYWRIGHT_DOWNLOAD_HOST="https://npmmirror.com/mirrors/playwright"

info() {
  printf '[INFO] %s\n' "$1"
}

fail() {
  printf '[ERROR] %s\n' "$1" >&2
  printf '[NEXT] %s\n' "$2" >&2
}

download_notice() {
  local name="$1"
  local url="$2"
  local target="$3"
  printf '\n'
  printf '【环境准备】未检测到可用的 %s，启动脚本将自动下载并准备它。\n' "$name"
  printf '下载来源：%s\n' "$url"
  printf '保存位置：%s\n' "$target"
  printf '请保持网络连接并等待下载完成，后续再次启动会优先复用已安装的本地文件。\n'
  printf '\n'
}

ensure_dirs() {
  mkdir -p "$RUNTIME_DIR" "$DOWNLOADS_DIR" "$TOOLS_DIR" "$DATA_DIR" "$LOGS_DIR" "$TEMP_DIR_PATH"
}

use_domestic_install_defaults() {
  if [ -z "${PIP_INDEX_URL:-}" ]; then
    export PIP_INDEX_URL="$DEFAULT_PIP_INDEX_URL"
    info "Using default PyPI mirror for mainland direct network: $DEFAULT_PIP_INDEX_URL"
  else
    info "Using user configured PyPI index: $PIP_INDEX_URL"
  fi

  if [ -z "${PLAYWRIGHT_BROWSERS_PATH:-}" ]; then
    export PLAYWRIGHT_BROWSERS_PATH="$BROWSER_CACHE_DIR"
    info "Using project Playwright browser cache: $BROWSER_CACHE_DIR"
  else
    info "Using user configured Playwright browser cache: $PLAYWRIGHT_BROWSERS_PATH"
  fi

  if [ -z "${PLAYWRIGHT_DOWNLOAD_HOST:-}" ]; then
    export PLAYWRIGHT_DOWNLOAD_HOST="$DEFAULT_PLAYWRIGHT_DOWNLOAD_HOST"
    info "Using default Playwright mirror for mainland direct network: $DEFAULT_PLAYWRIGHT_DOWNLOAD_HOST"
  else
    info "Using user configured Playwright browser mirror: $PLAYWRIGHT_DOWNLOAD_HOST"
  fi
}

python_is_usable() {
  "$1" - <<'PY' >/dev/null 2>&1
import sys
raise SystemExit(0 if sys.version_info >= (3, 10) else 1)
PY
}

get_python() {
  info "Checking Python." >&2
  for cmd in python3 python; do
    if command -v "$cmd" >/dev/null 2>&1 && python_is_usable "$cmd"; then
      info "Using existing Python: $cmd" >&2
      printf '%s\n' "$cmd"
      return 0
    fi
  done

  download_notice "Python 3.12" "$PYTHON_URL" "$PYTHON_PKG" >&2
  if ! command -v curl >/dev/null 2>&1; then
    fail "curl is not available, so Python cannot be downloaded automatically." "Install Python 3.12 manually and run start-mac.command again."
    return 1
  fi
  if [ ! -f "$PYTHON_PKG" ]; then
    if ! curl --fail --location --retry 2 "$PYTHON_URL" -o "$PYTHON_PKG"; then
      rm -f "$PYTHON_PKG"
      fail "Python download failed from the configured domestic mirror." "Check network access to the Qinghua Python mirror, then run start-mac.command again."
      return 1
    fi
  else
    info "Reusing downloaded Python installer: $PYTHON_PKG" >&2
  fi
  info "Opening the Python installer downloaded from the domestic mirror. Complete the installer, then this script will continue." >&2
  open "$PYTHON_PKG"
  read -r -p "Press Enter after the Python installer finishes... " _

  for cmd in python3 python; do
    if command -v "$cmd" >/dev/null 2>&1 && python_is_usable "$cmd"; then
      info "Using installed Python: $cmd" >&2
      printf '%s\n' "$cmd"
      return 0
    fi
  done

  fail "Python is still not available after installation." "Close and reopen Terminal, or install Python 3.12 manually, then run start-mac.command again."
  return 1
}

ensure_ffmpeg() {
  local venv_python="$1"
  info "Checking FFmpeg."
  if command -v ffmpeg >/dev/null 2>&1; then
    info "Using system FFmpeg."
    return 0
  fi

  local local_ffmpeg
  local_ffmpeg="$(find "$TOOLS_DIR/ffmpeg" -type f -name ffmpeg 2>/dev/null | head -n 1 || true)"
  if [ -n "$local_ffmpeg" ]; then
    chmod +x "$local_ffmpeg" 2>/dev/null || true
    export PATH="$(dirname "$local_ffmpeg"):$PATH"
    info "Using local FFmpeg: $local_ffmpeg"
    return 0
  fi

  info "Preparing FFmpeg from imageio-ffmpeg via the configured PyPI mirror."
  if "$venv_python" -m pip install 'imageio-ffmpeg>=0.5.1,<1.0.0'; then
    local_ffmpeg="$("$venv_python" -c 'import imageio_ffmpeg; print(imageio_ffmpeg.get_ffmpeg_exe())' 2>/dev/null || true)"
  fi
  if [ -n "$local_ffmpeg" ] && [ -f "$local_ffmpeg" ]; then
    chmod +x "$local_ffmpeg" 2>/dev/null || true
    export PATH="$(dirname "$local_ffmpeg"):$PATH"
    info "Using imageio-ffmpeg binary: $local_ffmpeg"
    return 0
  fi

  fail "Unable to prepare FFmpeg automatically." "Check access to the configured PyPI domestic mirror, or install FFmpeg manually, then run start-mac.command again."
  return 1
}

ensure_venv() {
  local python_cmd="$1"
  info "Creating or reusing local virtual environment." >&2
  if [ ! -x "$VENV_DIR/bin/python" ]; then
    "$python_cmd" -m venv "$VENV_DIR"
  fi
  printf '%s\n' "$VENV_DIR/bin/python"
}

backend_dependencies_available() {
  "$1" -c 'import fastapi, uvicorn, yt_dlp, httpx, curl_cffi, playwright.sync_api, imageio_ffmpeg' >/dev/null 2>&1
}

install_dependencies() {
  local venv_python="$1"
  local requirements_stamp
  local current_stamp=""
  requirements_stamp="$($venv_python - <<'PY' "$REQUIREMENTS_FILE"
import hashlib
import sys
from pathlib import Path

path = Path(sys.argv[1])
print(hashlib.sha256(path.read_bytes()).hexdigest())
PY
)"
  if [ -f "$DEPENDENCY_STAMP" ]; then
    current_stamp="$(cat "$DEPENDENCY_STAMP")"
  fi

  if [ "$current_stamp" = "$requirements_stamp" ] && [ -n "$requirements_stamp" ] && backend_dependencies_available "$venv_python"; then
    info "Backend dependencies are already installed; skipping pip install."
    return 0
  fi

  info "Installing backend dependencies. First launch or dependency changes may download packages."
  "$venv_python" -m pip install --upgrade pip
  "$venv_python" -m pip install -r "$REQUIREMENTS_FILE"
  printf '%s' "$requirements_stamp" > "$DEPENDENCY_STAMP"
}

playwright_chromium_executable() {
  local venv_python="$1"
  "$venv_python" - <<'PY'
from playwright.sync_api import sync_playwright

with sync_playwright() as playwright:
    print(playwright.chromium.executable_path)
PY
}

ensure_playwright_chromium() {
  local venv_python="$1"
  local executable=""
  info "Checking Playwright Chromium."
  executable="$(playwright_chromium_executable "$venv_python" 2>/dev/null || true)"
  if [ -n "$executable" ] && [ -x "$executable" ]; then
    info "Using Playwright Chromium: $executable"
    return 0
  fi

  local download_source="$PLAYWRIGHT_DOWNLOAD_HOST"
  download_notice "Playwright Chromium" "$download_source" "$PLAYWRIGHT_BROWSERS_PATH"
  "$venv_python" -m playwright install chromium

  executable="$(playwright_chromium_executable "$venv_python" 2>/dev/null || true)"
  if [ -z "$executable" ] || [ ! -x "$executable" ]; then
    fail "Playwright Chromium installation finished but the browser executable was not found." "Check network access or PLAYWRIGHT_DOWNLOAD_HOST, then run start-mac.command again."
    return 1
  fi
  info "Playwright Chromium is ready: $executable"
}

port_available() {
  local port="$1"
  "$PYTHON_FOR_PORT" - "$port" <<'PY'
import socket
import sys

port = int(sys.argv[1])
sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
try:
    sock.bind(("127.0.0.1", port))
except OSError:
    raise SystemExit(1)
finally:
    sock.close()
PY
}

get_available_port() {
  for port in $(seq 8000 8099); do
    if port_available "$port"; then
      printf '%s\n' "$port"
      return 0
    fi
  done
  fail "No available port found between 8000 and 8099." "Close another local service or restart your computer, then run start-mac.command again."
  return 1
}

open_browser() {
  local url="$1"
  info "Opening browser: $url"
  open "$url" >/dev/null 2>&1 || true
}

open_browser_after_health_check() {
  local url="$1"
  local health_url="$url/api/health"
  (
    for _ in $(seq 1 240); do
      if curl -fsS --max-time 1 "$health_url" >/dev/null 2>&1; then
        open_browser "$url"
        return 0
      fi
      sleep 0.5
    done
  ) &
}

main() {
  echo "Bilibili Video to Text - local desktop startup"
  ensure_dirs
  use_domestic_install_defaults
  PYTHON_CMD="$(get_python)"
  VENV_PYTHON="$(ensure_venv "$PYTHON_CMD")"
  install_dependencies "$VENV_PYTHON"
  ensure_playwright_chromium "$VENV_PYTHON"
  ensure_ffmpeg "$VENV_PYTHON"
  PYTHON_FOR_PORT="$VENV_PYTHON"
  PORT="$(get_available_port)"
  URL="http://127.0.0.1:$PORT"
  if [ "$PORT" != "8000" ]; then
    info "Port 8000 is busy. Using port $PORT instead."
  fi

  export PORT
  export TEMP_DIR="$TEMP_DIR_PATH"
  export GLOBAL_TASK_CONCURRENCY=1
  export AUDIO_REQUEST_CONCURRENCY=2

  info "Starting FastAPI service. Keep this window open while using the app."
  info "Browser will open after local health check passes: $URL"
  open_browser_after_health_check "$URL"
  "$VENV_PYTHON" -m uvicorn backend.app.main:app --host 127.0.0.1 --port "$PORT"
}

main "$@"
