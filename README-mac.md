# Sand 资格领取器 · macOS 使用说明

Windows 用户：双击 `启动.bat` 即可，无需看本文件。

## 运行（源码模式，推荐）

1. 装 Python 3（若未装）：https://www.python.org/downloads/
2. 双击 `install-mac.command`（首次装依赖，需联网）。
   - 若提示“无法打开、来自身份不明的开发者”：右键该文件 → 打开 → 打开。
3. 以后双击 `start-mac.command` 启动。

命令行等价：

```bash
python3 -m pip install -r requirements-mac.txt
python3 app.py
```

## 打补丁 / 切号需要的权限

- 「切号」「打补丁」要写入 Cursor 安装目录与登录库。首次可能弹出权限请求，允许即可。
- 若失败，用终端 `sudo python3 app.py` 再试。

## 打包成 .app（可选，需在 Mac 上做）

Windows 上无法编译出 Mac 程序，必须在 Mac 上用 Nuitka 编译：

```bash
python3 -m pip install nuitka
python3 -m nuitka --standalone --macos-create-app-bundle \
  --enable-plugin=pywebview \
  --include-data-dir=web=web \
  --output-filename=SandClaimer app.py
```

产物是 `app.dist/` 里的 `.app`。首次运行同样可能需右键 → 打开绕过 Gatekeeper。

## 隐私

- 账号 token 只存在本机（macOS 下 `~/Library/Application Support/SandClaimer/`），不上传第三方。
- 分享本工具时，请勿附带上述目录或任何导出的账号文件。
