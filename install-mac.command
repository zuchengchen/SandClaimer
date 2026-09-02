#!/bin/bash
# Sand 资格领取器 · macOS 一键安装依赖
# 双击若提示“无法打开/未验证的开发者”，右键本文件 → 打开。
cd "$(dirname "$0")" || exit 1

if ! command -v python3 >/dev/null 2>&1; then
  echo "[错误] 没找到 python3。请先从 https://www.python.org/downloads/ 安装 Python 3。"
  read -r -p "按回车退出..." _
  exit 1
fi

echo "正在安装依赖（需要联网，约 1-3 分钟）..."
python3 -m pip install --upgrade pip
python3 -m pip install -r requirements-mac.txt || {
  echo "[错误] 依赖安装失败，请检查网络后重试。"
  read -r -p "按回车退出..." _
  exit 1
}
echo "依赖安装完成。以后双击「start-mac.command」即可启动。"
read -r -p "按回车退出..." _
