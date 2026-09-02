#!/bin/bash
# Sand 资格领取器 · macOS 启动（源码模式，先运行一次 install-mac.command 装依赖）
# 双击若提示“无法打开/未验证的开发者”，右键本文件 → 打开。
cd "$(dirname "$0")" || exit 1

if ! command -v python3 >/dev/null 2>&1; then
  echo "没找到 python3，请先双击「install-mac.command」。"
  read -r -p "按回车退出..." _
  exit 1
fi

# 依赖缺失时自动补装
if ! python3 -c "import webview, requests, websocket" >/dev/null 2>&1; then
  echo "[首次运行] 正在安装依赖..."
  python3 -m pip install -r requirements-mac.txt || {
    echo "依赖安装失败，请检查网络。"
    read -r -p "按回车退出..." _
    exit 1
  }
fi

echo "启动中...（关闭窗口即退出）"
exec python3 app.py
