"""读取本机 Cursor 客户端的登录态（token/邮箱/套餐），用于「以本机账号探测」。

Cursor 把登录信息存在 SQLite 库 state.vscdb 的 ItemTable 键值表里：
  - cursorAuth/accessToken        当前登录的 access_token（JWT）
  - cursorAuth/cachedEmail        缓存邮箱
  - cursorAuth/stripeMembershipType 套餐（free/pro/enterprise…）
只读打开（mode=ro&immutable=1）：Cursor 正在运行时库被占用也能读，且绝不写盘。
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import uuid

_KEYS = {
    "token": "cursorAuth/accessToken",
    "email": "cursorAuth/cachedEmail",
    "membership": "cursorAuth/stripeMembershipType",
    # 这些是显示缓存/身份缓存，不代替 token；用于发现 Cursor 登录状态不一致。
    "user_id": "cursorAuth/userId",
    "cached_user_id": "cursorAuth/cachedUserId",
    "auth_id": "cursorAuth/authId",
}


def _cursor_root() -> str:
    """本机 Cursor 数据根目录（Windows / macOS / Linux）。"""
    if sys.platform == "darwin":
        base = os.path.expanduser("~/Library/Application Support")
    elif os.name == "nt":
        base = os.environ.get("APPDATA") or os.path.expanduser("~/AppData/Roaming")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, "Cursor")


def state_db_path() -> str:
    """本机 Cursor state.vscdb 路径。"""
    return os.path.join(_cursor_root(), "User", "globalStorage", "state.vscdb")


def storage_json_path() -> str:
    """本机 Cursor storage.json 路径（机器码 telemetry.* 存这里）。"""
    return os.path.join(_cursor_root(), "User", "globalStorage", "storage.json")


def machineid_path() -> str:
    """本机 Cursor machineid 文件路径（= storage.serviceMachineId）。"""
    return os.path.join(_cursor_root(), "machineid")


def read_local_account() -> dict | None:
    """返回 {token, email, membership}；未登录 / 读不到 token 时返回 None。"""
    path = state_db_path()
    if not os.path.isfile(path):
        return None
    uri = "file:{}?mode=ro&immutable=1".format(path.replace("\\", "/"))
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=5)
    except Exception:
        return None
    try:
        out = {}
        for field, key in _KEYS.items():
            try:
                row = conn.execute(
                    "SELECT value FROM ItemTable WHERE key=?", (key,)
                ).fetchone()
            except Exception:
                row = None
            out[field] = row[0] if row else None
    finally:
        conn.close()
    if not out.get("token"):
        return None
    return out


def write_local_account(
    access_token: str,
    email: str,
    refresh_token: str | None = None,
    membership: str | None = None,
    user_id: str | None = None,
) -> None:
    """把账号写入本机 Cursor 登录态（切号）。必须在 Cursor 已关闭时调用，否则库被锁。

    写入完整登录态键（accessToken/refreshToken/userId/authId/登录标志），并清理旧号的
    付费缓存，避免 Cursor 用旧值校验判定 session 异常而掉线。未提供 refresh_token 时删除旧
    refreshToken，避免用旧号的 refresh 刷回上一个账号。
    """
    path = state_db_path()
    if not os.path.isfile(path):
        raise RuntimeError("未找到本机 Cursor state.vscdb（可能未安装或未登录过）")
    conn = sqlite3.connect(path, timeout=8)
    try:
        cur = conn.cursor()

        def put(key: str, value: str) -> None:
            cur.execute(
                "INSERT OR REPLACE INTO ItemTable (key, value) VALUES (?, ?)",
                (key, value),
            )

        def delete(key: str) -> None:
            cur.execute("DELETE FROM ItemTable WHERE key=?", (key,))

        put("cursorAuth/accessToken", access_token)
        put("cursorAuth/cachedEmail", email or "")
        put("cursorAuth/email", email or "")
        put("cursor.accessToken", access_token)
        put("cursor.email", email or "")
        if refresh_token:
            put("cursorAuth/refreshToken", refresh_token)
        else:
            delete("cursorAuth/refreshToken")
        if user_id:
            auth_id = user_id if user_id.startswith("auth0|") else ("auth0|" + user_id)
            put("cursorAuth/userId", auth_id)
            put("cursorAuth/cachedUserId", auth_id)
            put("cursorAuth/authId", auth_id)
        put("cursorAuth/isAuthenticated", "true")
        put("cursorAuth/isAuthorized", "true")
        put("cursorAuth/isLoggedIn", "true")
        put("cursorAuth/cachedSignUpType", "Auth_0")
        if membership:
            put("cursorAuth/stripeMembershipType", membership)
        else:
            # 清掉旧号的付费缓存，否则 Cursor 用旧值校验 -> 判 session 异常 -> 掉线。
            delete("cursorAuth/stripeMembershipType")
            delete("cursorAuth/stripeSubscriptionStatus")
        # 清掉旧号残留的引导日期与自带 API Key（BYOK），避免带进新号的会话。
        # （对齐 kc-cursor cursor-manager 的切号清理项。）
        delete("cursorAuth/onboardingDate")
        delete("cursorAuth/openAIKey")
        delete("cursorAuth/claudeKey")
        delete("cursorAuth/googleKey")
        conn.commit()
    finally:
        conn.close()


def _rand_hex64() -> str:
    return os.urandom(32).hex()


def _atomic_write(path: str, data: bytes) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "wb") as handle:
        handle.write(data)
    os.replace(tmp, path)


def reset_machine_ids() -> dict:
    """重置本机机器码，防止多个小号被 Cursor 关联。必须在 Cursor 已关闭时调用。

    覆盖三处（均实测确认）：
      - storage.json：telemetry.machineId / macMachineId（64位hex）、devDeviceId（UUID）、sqmId（{大写GUID}）
      - state.vscdb：storage.serviceMachineId（UUID）
      - machineid 文件（= serviceMachineId）
    """
    service_id = str(uuid.uuid4())
    ids = {
        "telemetry.machineId": _rand_hex64(),
        "telemetry.macMachineId": _rand_hex64(),
        "telemetry.devDeviceId": str(uuid.uuid4()),
        "telemetry.sqmId": "{" + str(uuid.uuid4()).upper() + "}",
    }

    sj = storage_json_path()
    try:
        data = json.load(open(sj, "r", encoding="utf-8")) if os.path.isfile(sj) else {}
    except Exception:
        data = {}
    if not isinstance(data, dict):
        data = {}
    data.update(ids)
    _atomic_write(sj, json.dumps(data, ensure_ascii=False, indent=4).encode("utf-8"))

    db = state_db_path()
    if os.path.isfile(db):
        conn = sqlite3.connect(db, timeout=8)
        try:
            conn.execute(
                "INSERT OR REPLACE INTO ItemTable (key, value) VALUES (?, ?)",
                ("storage.serviceMachineId", service_id),
            )
            conn.commit()
        finally:
            conn.close()

    try:
        _atomic_write(machineid_path(), service_id.encode("utf-8"))
    except Exception:
        pass

    return {
        "machineId": ids["telemetry.machineId"],
        "devDeviceId": ids["telemetry.devDeviceId"],
        "serviceMachineId": service_id,
    }
