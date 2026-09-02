#!/bin/bash
# =============================================================================
#  Sand 资格领取器 · 一键打包成 Mac App(.dmg)
#  给【有 Mac 的群友】用：双击本文件，等几分钟，同目录会生成「Sand资格领取器.dmg」。
#  之后把 .dmg 发群，其他 Mac 用户下载→打开→拖进“应用程序”→双击使用。
#
#  ⚠️ 只能在 macOS 上运行（.dmg 依赖 hdiutil/codesign，Windows 无法生成）。
#  首次双击若提示“无法打开/未验证的开发者”：右键点本文件 → 打开；
#  或先在“终端”执行一次：  chmod +x *.command
# =============================================================================
set -e
cd "$(dirname "$0")" || exit 1

APP_NAME="Sand资格领取器"
BUNDLE_ID="com.sand.claimer"
PIP_MIRROR="https://pypi.tuna.tsinghua.edu.cn/simple"

echo "==================================================="
echo "   打包 $APP_NAME → Mac App(.dmg)"
echo "==================================================="

if ! command -v python3 >/dev/null 2>&1; then
  echo "[X] 没找到 python3。请先安装 Python 3：https://www.python.org/downloads/macos/"
  read -r -p "按回车退出..." _
  exit 1
fi
echo "使用 Python：$(python3 --version 2>&1)"

VENV=".build_venv"
echo "[1/5] 建立打包用虚拟环境 $VENV ..."
python3 -m venv "$VENV"
# shellcheck disable=SC1090
source "$VENV/bin/activate"
python -m pip install -U pip -i "$PIP_MIRROR"

echo "[2/5] 安装依赖 + pyinstaller ..."
python -m pip install -U \
  requests websocket-client pywebview \
  pyobjc-core pyobjc-framework-Cocoa pyobjc-framework-WebKit \
  pyinstaller pyinstaller-hooks-contrib \
  -i "$PIP_MIRROR"

echo "[3/5] 用 pyinstaller 打包 .app（较慢，请耐心等待）..."
rm -rf build dist
pyinstaller --noconfirm --clean --windowed \
  --name "$APP_NAME" \
  --osx-bundle-identifier "$BUNDLE_ID" \
  --add-data "web:web" \
  --collect-all webview \
  --hidden-import sand_api \
  --hidden-import accounts \
  --hidden-import sand_patch \
  --hidden-import resolve \
  --hidden-import local_cursor \
  --hidden-import browser_login \
  --hidden-import websocket \
  --hidden-import webview.platforms.cocoa \
  --hidden-import objc \
  --hidden-import Foundation \
  --hidden-import AppKit \
  --hidden-import WebKit \
  app.py

APP_PATH="dist/$APP_NAME.app"
if [ ! -d "$APP_PATH" ]; then
  echo "[X] 打包失败：没生成 $APP_PATH。请把上面的报错发我。"
  deactivate || true
  read -r -p "按回车退出..." _
  exit 1
fi

echo "[4/5] 写入权限说明 + 临时(ad-hoc)签名 ..."
PLIST="$APP_PATH/Contents/Info.plist"
PB=/usr/libexec/PlistBuddy
# 切号需要自动退出/重启 Cursor（自动化权限）
$PB -c "Add :NSAppleEventsUsageDescription string '切号时需要自动退出并重启 Cursor'" "$PLIST" 2>/dev/null || \
  $PB -c "Set :NSAppleEventsUsageDescription '切号时需要自动退出并重启 Cursor'" "$PLIST" 2>/dev/null || true
$PB -c "Add :NSHighResolutionCapable bool true" "$PLIST" 2>/dev/null || true
# 改过 plist 会让签名失效 → 重新 ad-hoc 签名（Apple 芯片必须签名才能运行）
codesign --force --deep --sign - "$APP_PATH" 2>/dev/null || true
xattr -cr "$APP_PATH" 2>/dev/null || true

echo "[5/5] 生成 .dmg ..."
DMG="$APP_NAME.dmg"
rm -f "$DMG"
hdiutil create -volname "$APP_NAME" -srcfolder "$APP_PATH" -ov -format UDZO "$DMG"

deactivate || true

echo
echo "==================================================="
if [ -f "$DMG" ]; then
  echo "   打包完成： $(pwd)/$DMG"
  echo "   把这个 .dmg 发群即可。Mac 用户：下载→打开→拖进“应用程序”→双击运行。"
  echo "   （首次运行请右键图标→打开）"
else
  echo "   [!] 没找到 .dmg，App 已在 $APP_PATH，可用“磁盘工具”手动打包或直接压缩 .app。"
fi
echo "==================================================="
read -r -p "按回车退出..." _
