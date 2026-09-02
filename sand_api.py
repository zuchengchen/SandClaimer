"""Cursor Sand（Grok Bot）资格查询与领取。

所有端点均为真机实测确认：
  - 额度：POST https://api2.cursor.sh/aiserver.v1.DashboardService/GetSandUsageStatus（Bearer accessToken）
  - 资格：POST https://cursor.com/api/dashboard/get-sand-access-status（会话 cookie）
  - teamId：POST https://cursor.com/api/dashboard/get-me（会话 cookie）
  - 领取：个人 POST /api/dashboard/start-sand-trial；团队 POST /api/dashboard/request-sand-team-access（body 带 teamId）
鉴权：api2 用 Bearer 明文 accessToken；cursor.com 用会话 cookie（userId::jwt），写操作再加 Origin 过 CSRF。
"""

import base64
import datetime
import hashlib
import json
import re
import secrets
import time
import uuid

import requests

SAND_USAGE_URL = "https://api2.cursor.sh/aiserver.v1.DashboardService/GetSandUsageStatus"
AGG_USAGE_URL = "https://api2.cursor.sh/aiserver.v1.DashboardService/GetAggregatedUsageEvents"
ACCESS_STATUS_URL = "https://cursor.com/api/dashboard/get-sand-access-status"
START_TRIAL_URL = "https://cursor.com/api/dashboard/start-sand-trial"
TEAM_ACCESS_URL = "https://cursor.com/api/dashboard/request-sand-team-access"
TEAM_ONBOARD_URL = "https://cursor.com/api/dashboard/update-team-sand-onboarding-completed"
GET_ME_URL = "https://cursor.com/api/dashboard/get-me"
USAGE_SUMMARY_URL = "https://cursor.com/api/usage-summary"
TEAM_SPEND_URL = "https://cursor.com/api/dashboard/get-team-spend"
STRIPE_URL = "https://cursor.com/api/auth/stripe"
ORIGIN = "https://cursor.com"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
TIMEOUT = 20


def _b64url_json(segment: str) -> dict:
    segment = segment.replace("-", "+").replace("_", "/")
    segment += "=" * (-len(segment) % 4)
    return json.loads(base64.b64decode(segment).decode("utf-8", "replace"))


def parse_token(raw: str):
    """把用户粘贴的 token 解析成 (user_id, access_token_jwt, claims)。

    支持两种格式：
      1) 纯 access_token（JWT，形如 eyJ...）；user id 从 JWT 的 sub 里取。
      2) ws token：user_01XXX::eyJ...（WorkosCursorSessionToken，:: 可为 %3A%3A）。
    """
    text = (raw or "").strip()
    if not text:
        raise ValueError("空 token")
    text = re.sub(r"^WorkosCursorSessionToken=", "", text, flags=re.I).strip()
    sep = "::" if "::" in text else ("%3A%3A" if "%3A%3A" in text else None)
    pasted_uid = None
    jwt = text
    if sep:
        left, _, right = text.partition(sep)
        pasted_uid = left.strip()
        jwt = right.strip()
    claims: dict = {}
    try:
        claims = _b64url_json(jwt.split(".")[1])
    except Exception:
        claims = {}
    sub = str(claims.get("sub", ""))
    from_sub = sub.split("|")[-1] if sub else ""
    user_id = from_sub if from_sub.startswith("user_") else (pasted_uid or "")
    if not user_id.startswith("user_"):
        raise ValueError("无法解析 user id（既不是 ws token，JWT 里也没有 sub）")
    return user_id, jwt, claims


LOGIN_DEEP_URL = "https://cursor.com/api/auth/loginDeepCallbackControl"
AUTH_POLL_URL = "https://api2.cursor.sh/auth/poll"
CHROME_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
)


def token_type(token: str) -> str | None:
    """返回 JWT payload 里的 type（web / session / …）；解析失败返回 None。"""
    try:
        _uid, jwt, claims = parse_token(token)
        return claims.get("type")
    except Exception:
        return None


def token_exp(token: str) -> int | None:
    """返回 JWT 的过期时间（exp，epoch 秒）；无 exp 或解析失败返回 None。"""
    try:
        _uid, _jwt, claims = parse_token(token)
        exp = claims.get("exp")
        return int(exp) if exp is not None else None
    except Exception:
        return None


def probe_token_alive(token: str) -> str:
    """切号前的「探活闸」：用会话 cookie 打 cursor.com/api/auth/me，判断这个号还能不能用。

    借鉴 kc-cursor 的 verify_account 闸①——切进一个死号会「设置里有号、一发消息就重登」
    （表现就是「切了等于没切」），所以切之前先探一下。
    返回 'alive'（200，能用）/ 'dead'（401/403，已失效或被限）/ 'unknown'（网络等无法判定）。
    """
    try:
        user_id, jwt, _claims = parse_token(token)
    except Exception:
        return "unknown"
    headers = {"cookie": _cookie(user_id, jwt), "accept": "application/json", "user-agent": UA}
    status, _text = _get("https://cursor.com/api/auth/me", headers)
    if status == 200:
        return "alive"
    if status in (401, 403):
        return "dead"
    return "unknown"


def exchange_web_to_session(token: str, timeout: int = 30):
    """把 type=web 的会话票换成 Cursor 客户端认的 type=session 的 (accessToken, refreshToken)。

    背景：网页登录得到的是 type=web 的 WorkosCursorSessionToken，能过 cursor.com 接口，但写进
    客户端 cursorAuth/accessToken 后，设置里能显示账号、一发消息就要求重新登录。这里按官方深度
    登录（PKCE）走一遍：用仍有效的 web Cookie 自动确认授权，从 api2.cursor.sh/auth/poll 换回真正
    的 session accessToken + refreshToken。

    仅对 web 票有意义；session 票或换取失败返回 (None, None)，由调用方回退原样写。
    """
    try:
        user_id, jwt, claims = parse_token(token)
    except Exception:
        return None, None
    if str(claims.get("type") or "").lower() != "web":
        return None, None

    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode().rstrip("=")
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).decode().rstrip("=")
    handshake = str(uuid.uuid4())
    cookie_val = f"{user_id}::{jwt}"

    session = requests.Session()
    session.trust_env = False
    session.headers.update({"user-agent": CHROME_UA, "accept": "application/json, text/plain, */*"})
    try:
        resp = session.post(
            LOGIN_DEEP_URL,
            json={"uuid": handshake, "challenge": challenge},
            headers={
                "cookie": f"WorkosCursorSessionToken={cookie_val}",
                "content-type": "application/json",
                "origin": ORIGIN,
            },
            timeout=timeout,
        )
        if not (200 <= resp.status_code < 300):
            return None, None
    except Exception:
        return None, None

    # 轮询换 token（最多 20 次 × 1s）；poll 走 api2，不带 Cookie。
    for _ in range(20):
        time.sleep(1.0)
        try:
            poll = session.get(
                f"{AUTH_POLL_URL}?uuid={handshake}&verifier={verifier}",
                headers={"accept": "*/*"},
                timeout=timeout,
            )
        except Exception:
            continue
        if poll.status_code == 200 and (poll.text or "").strip():
            try:
                data = poll.json()
            except Exception:
                continue
            access = (data.get("accessToken") or "").strip()
            if access:
                refresh = (data.get("refreshToken") or "").strip() or access
                return access, refresh
    return None, None


def _cookie(user_id: str, jwt: str) -> str:
    return f"WorkosCursorSessionToken={user_id}%3A%3A{jwt}"


def _bearer_headers(jwt: str) -> dict:
    return {
        "authorization": f"Bearer {jwt}",
        "content-type": "application/json",
        "connect-protocol-version": "1",
        "user-agent": UA,
    }


def _cookie_headers(user_id: str, jwt: str, origin: bool = False) -> dict:
    headers = {
        "cookie": _cookie(user_id, jwt),
        "content-type": "application/json",
        "accept": "application/json",
        "user-agent": UA,
    }
    if origin:
        headers["origin"] = ORIGIN
    return headers


def _post(url: str, headers: dict, body: str = "{}"):
    try:
        resp = requests.post(url, headers=headers, data=body.encode("utf-8"), timeout=TIMEOUT)
        return resp.status_code, resp.text
    except Exception as exc:
        return 0, str(exc)


def _get(url: str, headers: dict):
    try:
        resp = requests.get(url, headers=headers, timeout=TIMEOUT)
        return resp.status_code, resp.text
    except Exception as exc:
        return 0, str(exc)


def fetch_general_usage(user_id: str, jwt: str):
    """查账号总额度（GET usage-summary，会话 cookie，GET 无需 Origin）。返回套餐与总用量百分比。"""
    headers = {"cookie": _cookie(user_id, jwt), "accept": "application/json", "user-agent": UA}
    status, text = _get(USAGE_SUMMARY_URL, headers)
    if status != 200:
        return None
    try:
        body = json.loads(text)
    except Exception:
        return None
    plan = (body.get("individualUsage") or {}).get("plan") or {}
    return {
        "membership": body.get("membershipType"),
        "totalPercent": plan.get("totalPercentUsed"),
        "unlimited": body.get("isUnlimited"),
        # 计费周期：billingCycleEnd 就是本期订阅结束/续费日（真正的“订阅到期”，非 token 有效期）。
        "billingCycleStart": body.get("billingCycleStart"),
        "billingCycleEnd": body.get("billingCycleEnd"),
    }


def fetch_subscription(user_id: str, jwt: str):
    """查订阅状态（GET auth/stripe，会话 cookie）。返回是否在续费、是否待取消、月付/年付。"""
    headers = {"cookie": _cookie(user_id, jwt), "accept": "application/json", "user-agent": UA, "origin": ORIGIN}
    status, text = _get(STRIPE_URL, headers)
    if status != 200:
        return None
    try:
        body = json.loads(text)
    except Exception:
        return None
    return {
        "membership": body.get("membershipType"),
        "subscriptionStatus": body.get("subscriptionStatus"),
        "pendingCancellationDate": body.get("pendingCancellationDate"),
        "isYearlyPlan": body.get("isYearlyPlan"),
    }


def _iso_to_ms(iso):
    """ISO8601（如 2026-08-26T17:22:03.913Z）转毫秒时间戳；失败返回 None。"""
    if not iso:
        return None
    try:
        dt = datetime.datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        return int(dt.timestamp() * 1000)
    except Exception:
        return None


def fetch_period_spend(jwt: str, period_start_iso):
    """汇总 bot 当前周期(periodStart→now)的用量事件美元消费（全模型）。返回美元 float 或 None。

    GetAggregatedUsageEvents 支持 startDate/endDate（毫秒字符串）范围过滤；按 bot 周期起点到现在
    汇总各模型 totalCents。注意：这是该账号「本周期内全部模型」的消费，接口无法单独拆出纯 bot。
    """
    start_ms = _iso_to_ms(period_start_iso)
    if start_ms is None:
        return None
    body = json.dumps({"startDate": str(start_ms), "endDate": str(int(time.time() * 1000))})
    status, text = _post(AGG_USAGE_URL, _bearer_headers(jwt), body)
    if status != 200:
        return None
    try:
        aggs = json.loads(text).get("aggregations", [])
    except Exception:
        return None
    if not isinstance(aggs, list):
        return None
    total_cents = sum(float(a.get("totalCents") or 0) for a in aggs if isinstance(a, dict))
    return total_cents / 100.0


def fetch_usage(jwt: str):
    """查 Sand 额度。unlocked=已开通（有非零额度）。"""
    status, text = _post(SAND_USAGE_URL, _bearer_headers(jwt))
    if status != 200:
        return None
    try:
        body = json.loads(text)
    except Exception:
        return None
    unlocked = (body.get("includedLimitZero") is not True) and (
        body.get("hasNonZeroIncludedLimit") is True
    )
    return {
        "unlocked": unlocked,
        "percent": body.get("usagePercent"),
        "nextReset": body.get("nextResetTimestampUtc"),
        "periodStart": body.get("currentPeriodStart"),
        "plan": body.get("grokPlanLabel"),
    }


def fetch_access(user_id: str, jwt: str):
    # cursor.com 的 dashboard POST 端点即使是读也要 Origin 过 CSRF，否则 403。
    status, text = _post(ACCESS_STATUS_URL, _cookie_headers(user_id, jwt, origin=True))
    if status != 200:
        return None
    try:
        body = json.loads(text)
    except Exception:
        return None
    return {
        "granted": body.get("state") == "SAND_ACCESS_STATE_GRANTED",
        "state": body.get("state"),
        "blockReason": body.get("blockReason"),
    }


def fetch_team_id(user_id: str, jwt: str):
    """返回 (team_id, email)。非团队账号 team_id 为 None。"""
    status, text = _post(GET_ME_URL, _cookie_headers(user_id, jwt, origin=True))
    if status != 200:
        return None, None
    try:
        body = json.loads(text)
    except Exception:
        return None, None
    team_id = body.get("teamId")
    email = body.get("email")
    return (team_id if isinstance(team_id, int) and team_id > 0 else None), email


def _tier_label(billing_tier):
    """把 TEAM_MEMBER_BILLING_TIER_TIER_2000 归一成短档位标签「T2000」。

    注意：这个数字是 Cursor 的档位/信用点口径，**不是美元金额**（usage-summary 里
    同值出现在 plan.limit，与 bonus/total 同单位的信用点）。真正的美元只有 includedSpendCents。
    """
    raw = str(billing_tier or "")
    match = re.search(r"(\d+)\s*$", raw)
    if match:
        return "T" + match.group(1)
    short = raw.replace("TEAM_MEMBER_BILLING_TIER_", "").replace("TIER_", "").strip()
    return short or None


def fetch_team_spend(user_id: str, jwt: str, team_id: int):
    """查团队每个成员的绝对额度：档位($)/已用($)/用量%。返回成员列表；失败返回 None。

    合并后 Grok Bot 用量走 cursor.com 团队账单，body 必须带 teamId，否则 401「Team ID is required」。
    """
    body = json.dumps({"teamId": team_id})
    status, text = _post(TEAM_SPEND_URL, _cookie_headers(user_id, jwt, origin=True), body)
    if status != 200:
        return None
    try:
        rows = json.loads(text).get("teamMemberSpend")
    except Exception:
        return None
    return rows if isinstance(rows, list) else None


def _spend_row_for(rows, email, user_id):
    """在 team-spend 列表里按邮箱（优先）或数字 userId 匹配当前账号那一行。"""
    if not rows:
        return None
    want = (email or "").strip().lower()
    for row in rows:
        if want and str(row.get("email", "")).strip().lower() == want:
            return row
    return None


def _spend_fields(row) -> dict:
    """把一行 team-spend 归一成给 UI 用的绝对额度字段。"""
    if not row:
        return {}
    cents = row.get("includedSpendCents")
    return {
        "billingTier": row.get("billingTier"),
        "tierLabel": _tier_label(row.get("billingTier")),
        "spendUsd": (cents / 100.0) if isinstance(cents, (int, float)) else None,
        "teamPercent": row.get("totalPercentUsed"),
        "autoPercent": row.get("autoPercentUsed"),
        "apiPercent": row.get("apiPercentUsed"),
        "role": row.get("role"),
    }


def start_trial(user_id: str, jwt: str):
    status, text = _post(START_TRIAL_URL, _cookie_headers(user_id, jwt, origin=True))
    if status != 200:
        return "failed", f"HTTP {status}: {text[:160]}"
    low = text.lower()
    if "cardverificationrequired" in low or "card_verification" in low:
        match = re.search(r'"(https://[^"]*(?:checkout|stripe)[^"]*)"', text)
        return "card_required", (match.group(1) if match else "")
    return "activated", ""


def request_team(user_id: str, jwt: str, team_id: int):
    body = json.dumps({"teamId": team_id})
    status, text = _post(TEAM_ACCESS_URL, _cookie_headers(user_id, jwt, origin=True), body)
    if status != 200:
        return "failed", f"HTTP {status}: {text[:160]}"
    # 标记团队 onboarding 完成是幂等辅助调用，失败不影响领取结果。
    _post(TEAM_ONBOARD_URL, _cookie_headers(user_id, jwt, origin=True), body)
    return "team_ok", ""


def get_status(token: str) -> dict:
    """查询单个账号的 Sand 状态（只读），供 UI 展示。"""
    user_id, jwt, claims = parse_token(token)
    usage = fetch_usage(jwt)
    team_id, email = fetch_team_id(user_id, jwt)
    general = fetch_general_usage(user_id, jwt)
    sub = fetch_subscription(user_id, jwt)
    resolved_email = email or claims.get("email") or user_id
    # 团队账号：从 team-spend 拿绝对额度（档位$/已用$/用量%）；个人号无 teamId 跳过。
    spend = {}
    if team_id is not None:
        rows = fetch_team_spend(user_id, jwt, team_id)
        spend = _spend_fields(_spend_row_for(rows, resolved_email, user_id))
    # bot 当前周期（会重置）内的美元消费（全模型），供「本周期消费」展示。
    period_spend = fetch_period_spend(jwt, usage.get("periodStart")) if usage else None
    return {
        "email": resolved_email,
        "teamId": team_id,
        "unlocked": usage.get("unlocked") if usage else None,
        "percent": usage.get("percent") if usage else None,
        "nextReset": usage.get("nextReset") if usage else None,
        "periodStart": usage.get("periodStart") if usage else None,
        "periodSpendUsd": period_spend,
        "plan": usage.get("plan") if usage else None,
        "membership": (general.get("membership") if general else None) or (sub.get("membership") if sub else None),
        "totalPercent": general.get("totalPercent") if general else None,
        "unlimited": general.get("unlimited") if general else None,
        # 真实订阅：本期结束/续费日 + 续费状态；tokenExp 是 token 60 天有效期（区别于订阅）。
        "billingCycleStart": general.get("billingCycleStart") if general else None,
        "billingCycleEnd": general.get("billingCycleEnd") if general else None,
        "subscriptionStatus": sub.get("subscriptionStatus") if sub else None,
        "pendingCancellationDate": sub.get("pendingCancellationDate") if sub else None,
        "isYearlyPlan": sub.get("isYearlyPlan") if sub else None,
        "tokenExp": claims.get("exp"),
        "billingTier": spend.get("billingTier"),
        "tierLabel": spend.get("tierLabel"),
        "spendUsd": spend.get("spendUsd"),
        "teamPercent": spend.get("teamPercent"),
        "autoPercent": spend.get("autoPercent"),
        "apiPercent": spend.get("apiPercent"),
    }


def claim(token: str) -> dict:
    """领取 Sand：已开通短路；能读到 teamId 走团队通道（带 teamId）；否则个人试用；免费号返回需绑卡。"""
    user_id, jwt, claims = parse_token(token)
    # 提前取 teamId + 真实 email（get-me），让每个返回分支都能带上邮箱。
    team_id, me_email = fetch_team_id(user_id, jwt)
    email = me_email or claims.get("email") or user_id
    usage = fetch_usage(jwt)
    if usage and usage.get("unlocked"):
        return {"outcome": "already", "email": email, "teamId": team_id, "percent": usage.get("percent"), "detail": "已开通"}
    access = fetch_access(user_id, jwt)
    if access and access.get("granted"):
        return {"outcome": "already", "email": email, "teamId": team_id, "detail": "已授予资格"}
    if team_id is not None:
        outcome, detail = request_team(user_id, jwt, team_id)
        return {
            "outcome": "team_ok" if outcome == "team_ok" else "failed",
            "email": email,
            "teamId": team_id,
            "detail": detail or "团队已请求/开通",
        }
    outcome, detail = start_trial(user_id, jwt)
    if outcome == "activated":
        return {"outcome": "activated", "email": email, "detail": "个人已开通"}
    if outcome == "card_required":
        return {"outcome": "card_required", "email": email, "detail": "免费账号需先验证信用卡", "url": detail}
    return {"outcome": "failed", "email": email, "detail": detail}
