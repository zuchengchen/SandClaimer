#!/usr/bin/env bash
# Install an isolated Linux runtime while reusing distribution GTK bindings.
set -Eeuo pipefail

PROJECT_DIR="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="${SANDCLAIMER_VENV:-$PROJECT_DIR/.venv-linux}"
REQUIREMENTS="$PROJECT_DIR/requirements-linux.txt"

info() { printf '[SandClaimer] %s\n' "$*"; }
warn() { printf '[SandClaimer] 警告: %s\n' "$*" >&2; }
die()  { printf '[SandClaimer] 错误: %s\n' "$*" >&2; exit 1; }

if [[ "$(uname -s)" != "Linux" ]]; then
  die "此脚本仅用于 Linux。macOS 请使用 install-mac.command。"
fi

ensure_web_resources() {
  local name canonical legacy
  mkdir -p "$PROJECT_DIR/web"
  for name in index.html style.css app.js; do
    canonical="$PROJECT_DIR/web/$name"
    legacy="$PROJECT_DIR/web\\$name"
    if [[ ! -s "$canonical" && -f "$legacy" ]]; then
      cp -p -- "$legacy" "$canonical"
      info "已修复 Web 资源路径: web/$name"
    fi
    [[ -s "$canonical" ]] || die "缺少 Web 资源: web/$name"
  done
}

python_is_supported() {
  "$1" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' \
    >/dev/null 2>&1
}

choose_python() {
  local candidate
  if [[ -n "${SANDCLAIMER_PYTHON:-}" ]]; then
    python_is_supported "$SANDCLAIMER_PYTHON" || \
      die "SANDCLAIMER_PYTHON 不可用或低于 Python 3.10: $SANDCLAIMER_PYTHON"
    printf '%s\n' "$SANDCLAIMER_PYTHON"
    return
  fi

  # Distribution PyGObject is compiled for /usr/bin/python3. Prefer it over a
  # Conda Python so the venv can reuse GTK/WebKitGTK through system-site-packages.
  for candidate in /usr/bin/python3 "$(command -v python3 2>/dev/null || true)"; do
    [[ -n "$candidate" && -x "$candidate" ]] || continue
    if python_is_supported "$candidate"; then
      printf '%s\n' "$candidate"
      return
    fi
  done
  die "未找到 Python 3.10 或更高版本。"
}

check_gtk() {
  "$1" - <<'PY' >/dev/null 2>&1
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

gtk_description() {
  "$1" - <<'PY' 2>/dev/null
import gi
gi.require_version("Gtk", "3.0")
try:
    gi.require_version("WebKit2", "4.1")
except ValueError:
    gi.require_version("WebKit2", "4.0")
from gi.repository import Gtk, WebKit2
print(f"GTK {Gtk.get_major_version()}.{Gtk.get_minor_version()} / "
      f"WebKitGTK {WebKit2.get_major_version()}.{WebKit2.get_minor_version()}")
PY
}

detect_qt_api() {
  "$1" - <<'PY' 2>/dev/null
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

as_root() {
  if (( EUID == 0 )); then
    "$@"
  elif command -v sudo >/dev/null 2>&1; then
    sudo "$@"
  else
    warn "系统缺少 sudo，无法自动安装 GTK/WebKitGTK。"
    return 1
  fi
}

install_system_gtk() {
  if [[ "${SANDCLAIMER_NO_SYSTEM_PACKAGES:-0}" == "1" ]]; then
    return 1
  fi

  info "未检测到可用的 GTK/WebKitGTK，尝试安装系统图形依赖…"
  if command -v pacman >/dev/null 2>&1; then
    as_root pacman -S --needed --noconfirm python-gobject gtk3 webkit2gtk-4.1
  elif command -v apt-get >/dev/null 2>&1; then
    local webkit_pkg="gir1.2-webkit2-4.1"
    if command -v apt-cache >/dev/null 2>&1 && ! apt-cache show "$webkit_pkg" >/dev/null 2>&1; then
      webkit_pkg="gir1.2-webkit2-4.0"
    fi
    as_root apt-get update && \
      as_root apt-get install -y python3-venv python3-pip python3-gi python3-gi-cairo \
        gir1.2-gtk-3.0 "$webkit_pkg"
  elif command -v dnf >/dev/null 2>&1; then
    as_root dnf install -y python3-gobject python3-cairo gtk3 webkit2gtk4.1
  elif command -v zypper >/dev/null 2>&1; then
    as_root zypper --non-interactive install python3-gobject python3-cairo gtk3 \
      typelib-1_0-Gtk-3_0 typelib-1_0-WebKit2-4_1
  elif command -v apk >/dev/null 2>&1; then
    as_root apk add python3 py3-pip py3-gobject3 py3-cairo gtk+3.0 webkit2gtk-4.1
  else
    warn "未识别系统包管理器，将改用 Qt WebEngine 后端。"
    return 1
  fi
}

ensure_web_resources
BASE_PYTHON="$(choose_python)"
info "使用 $BASE_PYTHON ($("$BASE_PYTHON" --version 2>&1))"

[[ -f "$REQUIREMENTS" ]] || die "缺少 $REQUIREMENTS"
info "创建/更新虚拟环境: $VENV_DIR"
"$BASE_PYTHON" -m venv --system-site-packages "$VENV_DIR" || \
  die "无法创建 venv；请先安装发行版的 python-venv/python3-venv 包。"
VENV_PYTHON="$VENV_DIR/bin/python"

info "安装 Python 运行依赖…"
PIP_DISABLE_PIP_VERSION_CHECK=1 "$VENV_PYTHON" -m pip install --upgrade pip setuptools wheel
PIP_DISABLE_PIP_VERSION_CHECK=1 "$VENV_PYTHON" -m pip install -r "$REQUIREMENTS"
"$VENV_PYTHON" -c 'import webview, requests, websocket' >/dev/null 2>&1 || \
  die "Python 依赖验证失败。"

REQUESTED_BACKEND="${SANDCLAIMER_BACKEND:-auto}"
QT_API_FOUND=""
case "$REQUESTED_BACKEND" in
  auto|"")
    if check_gtk "$VENV_PYTHON"; then
      info "图形后端就绪: $(gtk_description "$VENV_PYTHON")"
    else
      install_system_gtk || true
      if check_gtk "$VENV_PYTHON"; then
        info "图形后端就绪: $(gtk_description "$VENV_PYTHON")"
      else
        QT_API_FOUND="$(detect_qt_api "$VENV_PYTHON" || true)"
        if [[ -z "$QT_API_FOUND" ]]; then
          info "安装 Qt 6 WebEngine 备用后端（下载较大）…"
          PIP_DISABLE_PIP_VERSION_CHECK=1 "$VENV_PYTHON" -m pip install \
            'PyQt6>=6.6,<7' 'PyQt6-WebEngine>=6.6,<7'
          QT_API_FOUND="$(detect_qt_api "$VENV_PYTHON" || true)"
        fi
        [[ -n "$QT_API_FOUND" ]] || die "GTK 和 Qt WebEngine 均不可用。"
        info "图形后端就绪: Qt WebEngine ($QT_API_FOUND)"
      fi
    fi
    ;;
  gtk)
    if ! check_gtk "$VENV_PYTHON"; then
      install_system_gtk || true
    fi
    check_gtk "$VENV_PYTHON" || \
      die "指定了 GTK，但 GTK 3/WebKitGTK 不可用。"
    info "图形后端就绪: $(gtk_description "$VENV_PYTHON")"
    ;;
  qt)
    QT_API_FOUND="$(detect_qt_api "$VENV_PYTHON" || true)"
    if [[ -z "$QT_API_FOUND" ]]; then
      info "安装 Qt 6 WebEngine 备用后端（下载较大）…"
      PIP_DISABLE_PIP_VERSION_CHECK=1 "$VENV_PYTHON" -m pip install \
        'PyQt6>=6.6,<7' 'PyQt6-WebEngine>=6.6,<7'
      QT_API_FOUND="$(detect_qt_api "$VENV_PYTHON" || true)"
    fi
    [[ -n "$QT_API_FOUND" ]] || die "指定了 Qt，但 Qt WebEngine 不可用。"
    info "图形后端就绪: Qt WebEngine ($QT_API_FOUND)"
    ;;
  *)
    die "不支持的后端 '$REQUESTED_BACKEND'，请使用 auto、gtk 或 qt。"
    ;;
esac

info "安装完成。运行 ./start-linux.sh 启动。"
