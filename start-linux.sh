#!/usr/bin/env bash
# SandClaimer Linux launcher. It self-installs missing runtime dependencies.
set -Eeuo pipefail

PROJECT_DIR="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="${SANDCLAIMER_VENV:-$PROJECT_DIR/.venv-linux}"
VENV_PYTHON="$VENV_DIR/bin/python"
INSTALLER="$PROJECT_DIR/install-linux.sh"

info() { printf '[SandClaimer] %s\n' "$*"; }
warn() { printf '[SandClaimer] 警告: %s\n' "$*" >&2; }
die()  { printf '[SandClaimer] 错误: %s\n' "$*" >&2; exit 1; }

check_python_runtime() {
  [[ -x "$VENV_PYTHON" ]] && \
    "$VENV_PYTHON" -c 'import webview, requests, websocket' >/dev/null 2>&1
}

check_web_resources() {
  [[ -s "$PROJECT_DIR/web/index.html" && \
     -s "$PROJECT_DIR/web/style.css" && \
     -s "$PROJECT_DIR/web/app.js" ]]
}

check_gtk() {
  "$VENV_PYTHON" - <<'PY' >/dev/null 2>&1
import gi
gi.require_version("Gtk", "3.0")
try:
    gi.require_version("WebKit2", "4.1")
    gi.require_version("Soup", "3.0")
except ValueError:
    gi.require_version("WebKit2", "4.0")
    gi.require_version("Soup", "2.4")
from gi.repository import Gtk, WebKit2  # noqa: F401
PY
}

detect_qt_api() {
  "$VENV_PYTHON" - <<'PY' 2>/dev/null
import importlib
import os

for api, binding in (
    ("pyqt6", "PyQt6"),
    ("pyside6", "PySide6"),
    ("pyqt5", "PyQt5"),
    ("pyside2", "PySide2"),
):
    try:
        os.environ["QT_API"] = api
        importlib.import_module(f"{binding}.QtWebEngineWidgets")
        importlib.import_module(f"{binding}.QtWebChannel")
        importlib.import_module("qtpy")
    except (ImportError, OSError):
        continue
    print(api)
    raise SystemExit(0)
raise SystemExit(1)
PY
}

run_installer() {
  [[ -x "$INSTALLER" ]] || die "缺少可执行的 install-linux.sh"
  info "首次运行或依赖不完整，正在自动安装…"
  "$INSTALLER"
}

[[ "$(uname -s)" == "Linux" ]] || die "此启动器仅用于 Linux。"
if ! check_python_runtime || ! check_web_resources; then
  run_installer
fi
check_python_runtime || die "虚拟环境不完整，请重试 ./install-linux.sh。"
check_web_resources || die "缺少 web/index.html、web/style.css 或 web/app.js。"

REQUESTED_BACKEND="${SANDCLAIMER_BACKEND:-${PYWEBVIEW_GUI:-auto}}"
QT_API_FOUND=""
case "$REQUESTED_BACKEND" in
  auto|"")
    if check_gtk; then
      export PYWEBVIEW_GUI=gtk
      info "使用 GTK/WebKitGTK 后端。"
    else
      QT_API_FOUND="$(detect_qt_api || true)"
      if [[ -n "$QT_API_FOUND" ]]; then
        export PYWEBVIEW_GUI=qt QT_API="$QT_API_FOUND"
        info "使用 Qt WebEngine ($QT_API_FOUND) 后端。"
      else
        run_installer
        if check_gtk; then
          export PYWEBVIEW_GUI=gtk
          info "使用 GTK/WebKitGTK 后端。"
        else
          QT_API_FOUND="$(detect_qt_api || true)"
          [[ -n "$QT_API_FOUND" ]] || die "未找到 GTK 或 Qt WebEngine 后端。"
          export PYWEBVIEW_GUI=qt QT_API="$QT_API_FOUND"
          info "使用 Qt WebEngine ($QT_API_FOUND) 后端。"
        fi
      fi
    fi
    ;;
  gtk)
    if ! check_gtk; then
      SANDCLAIMER_BACKEND=gtk run_installer
    fi
    check_gtk || die "指定了 GTK，但 GTK 3/WebKitGTK 不可用。"
    export PYWEBVIEW_GUI=gtk
    info "使用 GTK/WebKitGTK 后端。"
    ;;
  qt)
    QT_API_FOUND="$(detect_qt_api || true)"
    if [[ -z "$QT_API_FOUND" ]]; then
      SANDCLAIMER_BACKEND=qt run_installer
      QT_API_FOUND="$(detect_qt_api || true)"
    fi
    [[ -n "$QT_API_FOUND" ]] || die "指定了 Qt，但 Qt WebEngine 不可用。"
    export PYWEBVIEW_GUI=qt QT_API="$QT_API_FOUND"
    info "使用 Qt WebEngine ($QT_API_FOUND) 后端。"
    ;;
  *)
    die "不支持的后端 '$REQUESTED_BACKEND'，请使用 auto、gtk 或 qt。"
    ;;
esac

if [[ -z "${DISPLAY:-}" && -z "${WAYLAND_DISPLAY:-}" ]]; then
  die "未检测到图形会话（DISPLAY/WAYLAND_DISPLAY 为空）。请在 Linux 桌面会话中启动。"
fi

if ! command -v chromium >/dev/null 2>&1 && \
   ! command -v chromium-browser >/dev/null 2>&1 && \
   ! command -v google-chrome >/dev/null 2>&1 && \
   ! command -v google-chrome-stable >/dev/null 2>&1 && \
   ! command -v microsoft-edge >/dev/null 2>&1; then
  warn "未找到 Chromium/Chrome/Edge；主程序可运行，但“网页领取”功能需要其中一个浏览器。"
fi

cd "$PROJECT_DIR"
export PYTHONUNBUFFERED=1
info "正在启动 Sand 资格领取器…"
exec "$VENV_PYTHON" "$PROJECT_DIR/app.py" "$@"
