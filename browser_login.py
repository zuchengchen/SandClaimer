"""用账号 token 打开一个已登录的浏览器（Chrome/Edge），落到 Sand 领取页，供手动完成绑卡/领取。

原理：WorkosCursorSessionToken 是 HttpOnly cookie，命令行/URL 都带不进普通浏览器；
只能用 CDP（DevTools 协议）：启动带调试端口 + 独立 profile 的浏览器 → Network.setCookie
注入会话 cookie → Page.navigate 到目标页 → 浏览器留给用户手动操作。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

import websocket  # websocket-client

CURSOR_ONBOARDING = "https://cursor.com/bot/onboarding?product=grok-bot"
CURSOR_DASHBOARD = "https://cursor.com/dashboard"


def _free_port() -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def _find_browser():
    """返回 (exe 路径, 浏览器名)。优先 Chrome/Chromium，回退 Edge。"""
    if sys.platform == "darwin":
        macs = [
            ("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome", "chrome"),
            (os.path.expanduser("~/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"), "chrome"),
            ("/Applications/Chromium.app/Contents/MacOS/Chromium", "chrome"),
            ("/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge", "edge"),
        ]
        for path, name in macs:
            if path and os.path.isfile(path):
                return path, name
        return None, None
    if sys.platform.startswith("linux"):
        commands = [
            ("google-chrome", "chrome"),
            ("google-chrome-stable", "chrome"),
            ("chromium", "chromium"),
            ("chromium-browser", "chromium"),
            ("microsoft-edge", "edge"),
            ("microsoft-edge-stable", "edge"),
            ("brave-browser", "brave"),
            ("brave-browser-stable", "brave"),
        ]
        for command, name in commands:
            path = shutil.which(command)
            if path:
                return path, name
        extra = [
            ("/opt/google/chrome/chrome", "chrome"),
            ("/snap/bin/chromium", "chromium"),
            ("/opt/microsoft/msedge/msedge", "edge"),
            ("/opt/brave.com/brave/brave-browser", "brave"),
        ]
        for path, name in extra:
            if os.path.isfile(path) and os.access(path, os.X_OK):
                return path, name
        return None, None
    pf = os.environ.get("ProgramFiles", r"C:\Program Files")
    pfx = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    local = os.environ.get("LOCALAPPDATA", "")
    chrome = [
        os.path.join(pf, "Google", "Chrome", "Application", "chrome.exe"),
        os.path.join(pfx, "Google", "Chrome", "Application", "chrome.exe"),
        os.path.join(local, "Google", "Chrome", "Application", "chrome.exe") if local else "",
    ]
    edge = [
        os.path.join(pfx, "Microsoft", "Edge", "Application", "msedge.exe"),
        os.path.join(pf, "Microsoft", "Edge", "Application", "msedge.exe"),
    ]
    for path in chrome:
        if path and os.path.isfile(path):
            return path, "chrome"
    for path in edge:
        if path and os.path.isfile(path):
            return path, "edge"
    return None, None


def _profile_dir(user_id: str) -> str:
    """按 user_id 隔离 profile：不同账号各自会话，不污染用户日常浏览器。"""
    safe = re.sub(r"[^A-Za-z0-9_]", "_", user_id) or "default"
    legacy_base = None
    if sys.platform == "darwin":
        base = os.path.expanduser("~/Library/Application Support")
    elif sys.platform.startswith("linux"):
        configured = os.path.expanduser(os.environ.get("XDG_DATA_HOME", ""))
        base = configured if configured and os.path.isabs(configured) else os.path.expanduser("~/.local/share")
        # 旧 Linux 版默认使用 tempfile.gettempdir()；保留已有 profile/cookie。
        legacy_base = os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()
    else:
        base = os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()
    path = os.path.join(base, "SandClaimer", "browser-profiles", safe)
    if legacy_base:
        legacy = os.path.join(legacy_base, "SandClaimer", "browser-profiles", safe)
        if os.path.abspath(legacy) != os.path.abspath(path) and os.path.isdir(legacy) and not os.path.exists(path):
            try:
                if sys.platform.startswith("linux"):
                    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
                else:
                    os.makedirs(os.path.dirname(path), exist_ok=True)
                shutil.copytree(legacy, path, dirs_exist_ok=True)
            except OSError:
                # 只读或受限环境下继续使用旧目录，保证原有 CDP 会话仍可打开。
                path = legacy
    try:
        if sys.platform.startswith("linux"):
            os.makedirs(path, mode=0o700, exist_ok=True)
        else:
            os.makedirs(path, exist_ok=True)
    except OSError:
        if legacy_base:
            legacy = os.path.join(legacy_base, "SandClaimer", "browser-profiles", safe)
            if os.path.isdir(legacy):
                path = legacy
            else:
                raise
    if sys.platform.startswith("linux"):
        try:
            os.chmod(os.path.dirname(path), 0o700)
            os.chmod(path, 0o700)
        except OSError:
            pass
    return path


def _http_json(port: int, path: str):
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=5) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _wait_page_target(port: int, timeout: float = 15.0):
    """等浏览器 CDP 就绪并返回一个 page target 的 webSocketDebuggerUrl。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            targets = _http_json(port, "/json")
            for t in targets:
                if t.get("type") == "page" and t.get("webSocketDebuggerUrl"):
                    return t["webSocketDebuggerUrl"]
        except Exception:
            pass
        time.sleep(0.25)
    return None


class _CDP:
    def __init__(self, ws_url: str):
        self._ws = websocket.create_connection(ws_url, timeout=10)
        self._id = 0

    def call(self, method: str, params: dict | None = None) -> dict:
        self._id += 1
        mid = self._id
        self._ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
        deadline = time.time() + 10
        while time.time() < deadline:
            msg = json.loads(self._ws.recv())
            if msg.get("id") == mid:
                return msg
        raise TimeoutError(f"CDP 无响应：{method}")

    def close(self):
        try:
            self._ws.close()
        except Exception:
            pass


def open_with_token(user_id: str, jwt: str, url: str = CURSOR_ONBOARDING) -> str:
    """启动浏览器、注入会话 cookie、跳转到目标页。返回浏览器名。抛异常表示失败。"""
    exe, name = _find_browser()
    if not exe:
        raise RuntimeError("未找到 Chrome、Chromium 或 Edge 浏览器")

    port = _free_port()
    profile = _profile_dir(user_id)
    args = [
        exe,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={profile}",
        "--remote-allow-origins=*",
        "--no-first-run",
        "--no-default-browser-check",
        "--new-window",
        "about:blank",
    ]
    # Chromium refuses to start as root unless its sandbox is disabled. Keep
    # the safer default for normal desktop users; this branch only helps an
    # explicitly privileged launch used for Cursor installation repairs.
    if sys.platform.startswith("linux") and hasattr(os, "geteuid") and os.geteuid() == 0:
        args.insert(1, "--no-sandbox")
    popen_kwargs = dict(
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
    )
    if sys.platform == "win32":
        popen_kwargs["creationflags"] = 0x00000200  # CREATE_NEW_PROCESS_GROUP
    else:
        popen_kwargs["start_new_session"] = True  # 父进程退出不带走浏览器
    subprocess.Popen(args, **popen_kwargs)

    ws_url = _wait_page_target(port)
    if not ws_url:
        raise RuntimeError("浏览器调试端口未就绪（可能被安全软件拦截）")

    cdp = _CDP(ws_url)
    try:
        cdp.call("Network.enable")
        # 值与后端 cookie 完全一致：user_id%3A%3Ajwt（URL 编码的 ::）
        cookie = {
            "name": "WorkosCursorSessionToken",
            "value": f"{user_id}%3A%3A{jwt}",
            "domain": ".cursor.com",
            "path": "/",
            "secure": True,
            "httpOnly": True,
            "sameSite": "Lax",
        }
        res = cdp.call("Network.setCookie", cookie)
        if res.get("error"):
            raise RuntimeError(f"注入 cookie 失败：{res['error']}")
        cdp.call("Page.enable")
        cdp.call("Page.navigate", {"url": url})
    finally:
        cdp.close()
    return name
