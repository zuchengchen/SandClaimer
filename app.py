"""Sand 资格领取器：pywebview（Windows 用 Edge WebView2）+ 玻璃风 Web UI。

- UI 在 web/ 下（HTML/CSS/JS，iOS 玻璃浅蓝风）。
- Python 提供导入/领取能力，通过 window.pywebview.api 暴露给前端。
- 批量领取由前端逐个调用 claim_one 驱动，实时更新每行状态。
"""

import datetime
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

import webview

import resolve
import sand_api
import sand_patch
import browser_login
import local_cursor
from accounts import AccountStore
from accounts import app_data_dir
from accounts import data_file_path
from accounts import format_export_line
from sand_api import claim as claim_token
from sand_api import get_status
from sand_api import parse_token


_STATE_DIR = app_data_dir()
_STAGED_RESOURCE_DIRS: dict[tuple[str, str], tempfile.TemporaryDirectory] = {}


def _strip_auth_prefix(value: object) -> str:
    text = str(value or "").strip()
    return text.split("|", 1)[-1] if "|" in text else text


def _local_identity(acct: dict, store: AccountStore | None = None) -> dict:
    """以 access token 的 user id 为准解析本机账号，识别过期的邮箱缓存。"""
    user_id, _jwt, claims = parse_token(acct["token"])
    claim_email = str(claims.get("email") or "").strip()
    cached_email = str(acct.get("email") or "").strip()
    cached_ids = {
        _strip_auth_prefix(acct.get(key))
        for key in ("user_id", "cached_user_id", "auth_id")
        if acct.get(key)
    }
    saved_email = ""
    if store is not None:
        saved = store.get(user_id)
        label = str(saved.get("label") or "") if saved else ""
        if "@" in label:
            saved_email = label

    # JWT claim or the matching saved row wins. A cached email from a different
    # user id is stale and must not be presented as the active account.
    if claim_email and "@" in claim_email:
        email = claim_email
        source = "token"
    elif saved_email:
        email = saved_email
        source = "saved"
    elif cached_email and (not cached_ids or user_id in cached_ids):
        email = cached_email
        source = "cursor-cache"
    else:
        email = user_id
        source = "token-id"

    mismatch = bool(cached_email and cached_ids and user_id not in cached_ids)
    return {
        "id": user_id,
        "email": email,
        "membership": acct.get("membership"),
        "emailMismatch": mismatch,
        "cachedEmail": cached_email if mismatch else None,
        "identitySource": source,
    }


def _read_json(name: str, default):
    try:
        with open(data_file_path(name), "r", encoding="utf-8") as handle:
            return json.load(handle)
    except Exception:
        return default


def _write_json(name: str, data) -> None:
    try:
        os.makedirs(_STATE_DIR, exist_ok=True)
        path = data_file_path(name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path + ".tmp", "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False)
        os.replace(path + ".tmp", path)
    except Exception:
        pass


def _stage_backslash_resources(base: str, top_dir: str) -> str | None:
    """将 POSIX 上误解压为 ``web\\index.html`` 的资源还原成正常目录。"""
    key = (base, top_dir)
    existing = _STAGED_RESOURCE_DIRS.get(key)
    if existing is not None:
        return os.path.join(existing.name, top_dir)

    try:
        names = os.listdir(base)
    except OSError:
        return None
    prefix = top_dir + "\\"
    legacy_names = [name for name in names if name.startswith(prefix)]
    if not legacy_names:
        return None

    holder = tempfile.TemporaryDirectory(prefix="sandclaimer-resources-")
    staged_root = os.path.join(holder.name, top_dir)
    try:
        for name in legacy_names:
            source = os.path.join(base, name)
            if not os.path.isfile(source):
                continue
            suffix = name[len(prefix) :]
            parts = [part for part in re.split(r"[\\\\/]", suffix) if part not in ("", ".", "..")]
            if not parts:
                continue
            destination = os.path.join(staged_root, *parts)
            os.makedirs(os.path.dirname(destination), exist_ok=True)
            shutil.copy2(source, destination)
    except OSError:
        holder.cleanup()
        return None
    _STAGED_RESOURCE_DIRS[key] = holder
    return staged_root


def resource_path(rel: str) -> str:
    """兼容 onefile 解包目录与 POSIX 上带反斜杠文件名的旧分发包。"""
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    parts = [part for part in re.split(r"[\\\\/]", rel) if part not in ("", ".")]
    normal = os.path.join(base, *parts)
    if os.path.exists(normal) or not parts:
        return normal

    staged_root = _stage_backslash_resources(base, parts[0])
    if staged_root:
        staged = os.path.join(staged_root, *parts[1:])
        if os.path.exists(staged):
            return staged

    # 单文件的最后兼容路径；目录资源正常时不会走到这里。
    legacy = os.path.join(base, "\\".join(parts))
    return legacy if os.path.exists(legacy) else normal


class Api:
    # 属性必须以下划线开头：pywebview 生成 JS 桥接时会递归遍历 js_api 的公开属性
    # （webview/util.py get_functions），遍历到 Window 对象会与建窗线程互等而死锁。
    def __init__(self) -> None:
        self._store = AccountStore()
        self._window: webview.Window | None = None

    def import_files(self) -> dict:
        """弹原生文件选择框，导入 JSON/文本账号文件。"""
        paths = None
        try:
            if self._window is not None:
                paths = self._window.create_file_dialog(
                    webview.OPEN_DIALOG,
                    allow_multiple=True,
                    file_types=("JSON 文件 (*.json)", "文本文件 (*.txt)", "所有文件 (*.*)"),
                )
        except Exception:
            paths = None
        if not paths:
            return {"added": 0, "accounts": self._store.list()}
        added = self._store.add_json_files(list(paths))
        return {"added": len(added), "accounts": self._store.list()}

    def import_text(self, text: str) -> dict:
        added = self._store.add_text(text or "")
        return {"added": len(added), "accounts": self._store.list()}

    def current_local_account(self) -> dict:
        """读取 Cursor 当前登录账号（只返回可展示的标识，不返回 token）。"""
        acct = local_cursor.read_local_account()
        if not acct or not acct.get("token"):
            return {"ok": False, "error": "未检测到本机 Cursor 登录"}
        try:
            identity = _local_identity(acct, self._store)
        except Exception as exc:
            return {"ok": False, "error": f"本机登录票无法解析：{exc}"}
        warning = None
        if identity["emailMismatch"]:
            warning = "Cursor 邮箱缓存与 access token 不一致，已按 access token 判定"
        return {
            "ok": True,
            **identity,
            "warning": warning,
        }

    def detect_local_account(self) -> dict:
        """以本机账号探测：读本机 Cursor 登录 token，自动加入列表（回写真实邮箱）。"""
        acct = local_cursor.read_local_account()
        if not acct or not acct.get("token"):
            return {"ok": False, "error": "未检测到本机 Cursor 登录（请先在本机 Cursor 登录账号）"}
        touched = self._store.add_text(acct["token"])
        account_id = touched[0]["id"] if touched else None
        try:
            identity = _local_identity(acct, self._store)
        except Exception as exc:
            return {"ok": False, "error": f"本机登录票无法解析：{exc}"}
        # Only promote a Cursor cached email when its cached user id agrees with
        # the token. A mismatched cache is commonly left behind after a manual login.
        email = identity["email"]
        if account_id and email and "@" in email and not identity["emailMismatch"]:
            self._store.set_label(account_id, email)
        return {
            "ok": True,
            **identity,
            "id": account_id or identity["id"],
            "warning": (
                "Cursor 邮箱缓存与 access token 不一致，已按 access token 判定"
                if identity["emailMismatch"]
                else None
            ),
            "accounts": self._store.list(),
        }

    def list_accounts(self) -> list:
        return self._store.list()

    def remove_account(self, account_id: str) -> list:
        self._store.remove(account_id)
        return self._store.list()

    def set_label(self, account_id: str, label: str) -> bool:
        """把查到的真实邮箱回写到账号，刷新/领取后行内显示邮箱。"""
        self._store.set_label(account_id, label)
        return True

    def clear_accounts(self) -> list:
        self._store.clear()
        return self._store.list()

    def export_accounts(self, payload) -> dict:
        """导出 txt：可按分段写入（# 标题），账号行仍是 邮箱----user_id::jwt，一个号只出现一次。"""
        sections = []
        if isinstance(payload, dict):
            sections = list(payload.get("sections") or [])
        elif isinstance(payload, list):
            sections = [{"title": "", "ids": payload}]

        seen: set[str] = set()
        blocks: list[str] = []
        count = 0
        for sec in sections:
            if not isinstance(sec, dict):
                continue
            title = str(sec.get("title") or "").strip()
            ids = [str(x) for x in (sec.get("ids") or [])]
            lines = []
            for account_id in ids:
                if account_id in seen:
                    continue
                item = self._store.get(account_id)
                if not item or not item.get("token"):
                    continue
                try:
                    lines.append(format_export_line(item))
                except Exception:
                    continue
                seen.add(account_id)
            if not lines:
                continue
            if title:
                blocks.append("# ===== %s %s =====" % (title, len(lines)))
            blocks.extend(lines)
            blocks.append("")
            count += len(lines)

        if count == 0:
            return {"ok": False, "error": "没有可导出的账号"}
        text = "\n".join(blocks).rstrip() + "\n"

        path = None
        try:
            if self._window is not None:
                fname = "sand_export_%s.txt" % datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
                result = self._window.create_file_dialog(
                    webview.SAVE_DIALOG,
                    save_filename=fname,
                    file_types=("文本文件 (*.txt)", "所有文件 (*.*)"),
                )
                if isinstance(result, (list, tuple)):
                    path = result[0] if result else None
                else:
                    path = result
        except Exception as exc:
            return {"ok": False, "error": f"打开保存框失败：{exc}"}
        if not path:
            return {"ok": False, "error": "已取消", "count": 0}

        try:
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(text)
        except Exception as exc:
            return {"ok": False, "error": f"写入文件失败：{exc}"}
        return {"ok": True, "count": count, "path": str(path), "text": text}

    def account_export_text(self, account_id: str) -> dict:
        """单条复制：邮箱----user_id::jwt，与导入行格式一致。"""
        item = self._store.get(account_id)
        if not item or not item.get("token"):
            return {"ok": False, "error": "账号不存在"}
        try:
            text = format_export_line(item)
        except Exception as exc:
            return {"ok": False, "error": f"导出失败：{exc}"}
        label = item.get("label") or ""
        email = label if "@" in label else (item.get("id") or "")
        return {"ok": True, "text": text, "email": email}

    def clip_set(self, text: str) -> dict:
        """浏览器剪贴板不可用时，用当前平台的系统命令兜底。"""
        payload = (text or "").encode("utf-8")
        if os.name == "nt":
            commands = [["clip"]]
        elif sys.platform == "darwin":
            commands = [["pbcopy"]]
        else:
            wayland = [["wl-copy"]]
            x11 = [["xclip", "-selection", "clipboard"], ["xsel", "--clipboard", "--input"]]
            commands = wayland + x11 if os.environ.get("WAYLAND_DISPLAY") else x11 + wayland

        errors = []
        for command in commands:
            # Windows clip.exe and macOS pbcopy may be shell/system commands;
            # preserve the old direct invocation instead of relying on PATH lookup.
            if sys.platform.startswith("linux") and shutil.which(command[0]) is None:
                continue
            try:
                kwargs = {"input": payload, "timeout": 5, "stdout": subprocess.DEVNULL, "stderr": subprocess.PIPE}
                if os.name == "nt":
                    kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW，避免闪黑框
                proc = subprocess.run(command, **kwargs)
                if proc.returncode == 0:
                    return {"ok": True}
                detail = (proc.stderr or b"").decode("utf-8", "replace").strip()
                errors.append(f"{command[0]}: {detail or 'exit ' + str(proc.returncode)}")
            except Exception as exc:
                errors.append(f"{command[0]}: {exc}")

        if errors:
            return {"ok": False, "error": "; ".join(errors)}
        if sys.platform.startswith("linux"):
            return {"ok": False, "error": "未找到 wl-copy、xclip 或 xsel 剪贴板工具"}
        return {"ok": False, "error": f"未找到系统剪贴板命令：{commands[0][0]}"}

    def claim_one(self, account_id: str) -> dict:
        item = self._store.get(account_id)
        if not item:
            return {"outcome": "failed", "detail": "账号不存在"}
        try:
            return claim_token(item["token"])
        except Exception as exc:
            return {"outcome": "failed", "detail": str(exc)}

    def status_one(self, account_id: str) -> dict:
        item = self._store.get(account_id)
        if not item:
            return {"error": "账号不存在"}
        try:
            return get_status(item["token"])
        except Exception as exc:
            return {"error": str(exc)}

    def open_login(self, account_id: str) -> dict:
        """用该账号 token 打开一个已登录浏览器并跳到 Sand 领取页，供手动完成（免费号绑卡等）。"""
        item = self._store.get(account_id)
        if not item:
            return {"ok": False, "error": "账号不存在"}
        try:
            user_id, jwt, _claims = parse_token(item["token"])
        except Exception as exc:
            return {"ok": False, "error": f"token 解析失败：{exc}"}
        try:
            name = browser_login.open_with_token(user_id, jwt)
            return {"ok": True, "browser": name}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def switch_account(self, account_id: str, reset_machine_id: bool = False) -> dict:
        """一键切号：关闭本机 Cursor → 写入所选账号登录态（可选重置机器码）→ 重开 Cursor。"""
        item = self._store.get(account_id)
        if not item:
            return {"ok": False, "error": "账号不存在"}
        try:
            user_id, jwt, claims = parse_token(item["token"])
        except Exception as exc:
            return {"ok": False, "error": f"token 解析失败：{exc}"}
        label = item.get("label") or ""
        email = label if "@" in label else (claims.get("email") or user_id)
        # 闸①（借鉴 kc-cursor cursor-manager）：切进一个死号 = 设置里有号、一发就重登（等于没切）。
        # 先本地看 exp（离线、即时），再联网探活；只在明确失效（401/403）时拦，网络问题不拦。
        exp = sand_api.token_exp(item["token"])
        if exp is not None and exp <= int(time.time()):
            return {
                "ok": False,
                "error": "该账号登录票已过期：切了也登不上（设置里会有号、一发消息就要重登，等于没切）。"
                "请重新领取/导入该号的新 token 再切。",
            }
        if sand_api.probe_token_alive(item["token"]) == "dead":
            return {
                "ok": False,
                "error": "该账号已失效或被限（服务端 401/403）：切了也登不上（等于没切）。"
                "请换一个有效号，或重新领取该号。",
            }
        # 网站会话票（type=web）直接写进客户端对话层不认（能显示账号、一发消息就要重登）。
        # 先按官方深度登录换成客户端 session 票；换不到再回退原样写（至少不比以前差）。
        refresh_jwt = None
        exchanged = False
        if str(claims.get("type") or "").lower() == "web":
            try:
                access, refresh = sand_api.exchange_web_to_session(item["token"])
            except Exception:
                access, refresh = None, None
            if access:
                jwt = access
                refresh_jwt = refresh or access
                exchanged = True
            else:
                return {
                    "ok": False,
                    "error": "这是网站会话（type=web），换取客户端登录票失败："
                    "多为该 token 已过期/被限流或网络问题。可重试，或用「网页领取」在浏览器里用。",
                }
        try:
            layout = sand_patch.resolve_cursor_layout()
        except sand_patch.SandToolError as exc:
            return {"ok": False, "error": f"未找到本机 Cursor：{exc}"}
        try:
            sand_patch.close_cursor(layout)
            local_cursor.write_local_account(jwt, email, refresh_token=refresh_jwt, user_id=user_id)
            if reset_machine_id:
                local_cursor.reset_machine_ids()
            sand_patch.start_cursor(layout)
            return {
                "ok": True,
                "email": email,
                "resetMachineId": bool(reset_machine_id),
                "exchanged": exchanged,
            }
        except PermissionError as exc:
            return {"ok": False, "error": f"没有写入权限，请用管理员身份运行本工具：{exc}"}
        except sand_patch.SandToolError as exc:
            return {"ok": False, "error": str(exc)}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    # ---- 状态记忆 / 设置持久化 ----

    def load_status(self) -> dict:
        """读取上次每个账号的 Sand 状态（套餐/额度/是否开通），重开时还原到列表。"""
        data = _read_json("status.json", {})
        return data if isinstance(data, dict) else {}

    def save_status(self, data: dict) -> bool:
        _write_json("status.json", data or {})
        return True

    def get_settings(self) -> dict:
        data = _read_json("settings.json", {})
        return data if isinstance(data, dict) else {}

    def set_settings(self, data: dict) -> bool:
        # 合并写入：前端各处只传自己关心的键（hideHelp / autoRefresh…），不能互相覆盖。
        merged = self.get_settings()
        merged.update(data or {})
        _write_json("settings.json", merged)
        return True

    # ---- 本机 Cursor Sand 补丁（复用 sand_patch，即原安装工具的成熟逻辑）----

    def set_cursor_path(self, path: str) -> dict:
        """设置自定义 Cursor 路径（传空或 auto 恢复自动检测），随后返回最新补丁状态。"""
        value = (path or "").strip() or "auto"
        try:
            sand_patch.save_cursor_path(value)
        except sand_patch.SandToolError as exc:
            return {"ok": False, "error": str(exc)}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        return self.patch_status()

    def patch_status(self) -> dict:
        try:
            layout = sand_patch.resolve_cursor_layout()
        except sand_patch.SandToolError as exc:
            return {"ok": False, "error": str(exc)}
        try:
            st = sand_patch.inspect_status(layout)
            return {
                "ok": True,
                "version": layout.version,
                "path": str(layout.install_root),
                "remoteServer": bool(layout.is_remote_server),
                "installed": bool(st.installed),
                "streamMode": bool(st.stream_mode_installed),
                "streamCapable": bool(st.stream_capable),
                "subagent": bool(st.subagent_installed and st.subagent_wake_installed),
                "subagentLegacy": bool(st.legacy_subagent_markers),
                # ≤1.1.9 强制 move_exec 的旧 marker；当前版本保留 ON 以提供完整工具执行器。
                "moveExecLegacy": bool(st.legacy_move_exec_forced),
                "foreignDirectStream": int(st.foreign_direct_stream_markers),
                "client": st.client_markers + st.legacy_client_markers,
                "eligibility": st.eligibility_markers + st.legacy_eligibility_markers,
            }
        except sand_patch.SandToolError as exc:
            return {"ok": False, "error": str(exc), "version": layout.version, "path": str(layout.install_root)}

    def apply_patch(self) -> dict:
        try:
            layout = sand_patch.resolve_cursor_layout()
            sand_patch.install(layout)
            return {"ok": True}
        except sand_patch.SandToolError as exc:
            return {"ok": False, "error": str(exc)}
        except PermissionError as exc:
            return {"ok": False, "error": f"没有写入权限，请用管理员身份运行本工具：{exc}"}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def restore_patch(self) -> dict:
        try:
            layout = sand_patch.resolve_cursor_layout()
            sand_patch.uninstall(layout)
            return {"ok": True}
        except sand_patch.SandToolError as exc:
            return {"ok": False, "error": str(exc)}
        except PermissionError as exc:
            return {"ok": False, "error": f"没有写入权限，请用管理员身份运行本工具：{exc}"}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}


def main() -> None:
    resolve.install()
    api = Api()
    window = webview.create_window(
        "Sand 资格领取器",
        resource_path(os.path.join("web", "index.html")),
        js_api=api,
        width=1000,
        height=730,
        min_size=(840, 600),
        background_color="#EAF2FF",
    )
    api._window = window
    if sys.platform.startswith("linux"):
        # Linux 发行版常同时安装残缺的 Qt 绑定；明确使用已安装的 GTK/WebKit。
        gui = (os.environ.get("PYWEBVIEW_GUI") or "gtk").strip().lower()
        # start-linux.sh resolves "auto" before launching, but direct `python app.py`
        # should be equally forgiving.
        webview.start(gui="gtk" if gui in {"", "auto"} else gui)
    else:
        webview.start()


if __name__ == "__main__":
    main()
