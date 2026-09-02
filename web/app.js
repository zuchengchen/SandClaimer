"use strict";

// 与 Python 侧 Api 通过 window.pywebview.api 通信；批量领取用并发池并行驱动，逐行更新状态。

let accounts = [];
const rowState = {}; // id -> { outcome/unlocked/percent/detail/team/... }
const selected = new Set(); // 勾选的账号 id；为空表示对全部生效
let busy = false;
let currentAccountId = null;

const $ = (id) => document.getElementById(id);

function api() {
  return window.pywebview && window.pywebview.api;
}

function toast(msg) {
  const el = $("toast");
  el.textContent = msg;
  el.hidden = false;
  clearTimeout(toast._t);
  toast._t = setTimeout(() => (el.hidden = true), 2200);
}

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])
  );
}

function setCurrentAccount(info) {
  const hint = $("currentAccountHint");
  if (!info || !info.ok) {
    currentAccountId = null;
    if (hint) {
      hint.className = "current-account muted";
      hint.textContent = "当前 Cursor：未登录或无法读取";
    }
    return;
  }
  currentAccountId = info.id || null;
  const matched = currentAccountId && accounts.some((a) => a.id === currentAccountId);
  if (hint) {
    hint.className = info.warning ? "current-account warning" : "current-account";
    const detail = info.warning
      ? `（${info.warning}）`
      : matched
        ? "（额度看这一行）"
        : "（该账号尚未加入列表）";
    hint.textContent = `当前 Cursor：${info.email || info.id || "本机账号"}${detail}`;
  }
}

async function refreshCurrentAccount(showToast = false) {
  try {
    const info = await api().current_local_account();
    setCurrentAccount(info);
    if (showToast) {
      toast(info && info.ok ? `当前 Cursor 账号：${info.email || info.id}` : ((info && info.error) || "未读取到本机账号"));
    }
  } catch (e) {
    setCurrentAccount(null);
    if (showToast) toast("读取本机账号失败：" + String(e));
  }
  render();
}

function fmtPercent(p) {
  if (p == null || isNaN(p)) return "";
  const v = Math.max(0, Number(p));
  return (v < 1 && v > 0 ? v.toFixed(2) : v.toFixed(1)) + "%";
}

function fmtUsd(v) {
  if (v == null || isNaN(v)) return "";
  const n = Number(v);
  return "$" + (n >= 100 ? n.toFixed(0) : n.toFixed(2));
}

function fmtReset(v) {
  if (v == null || v === "") return "";
  let d;
  if (typeof v === "number" || /^\d+$/.test(String(v))) {
    let n = Number(v);
    if (n < 1e12) n *= 1000; // 秒 -> 毫秒
    d = new Date(n);
  } else {
    d = new Date(v);
  }
  if (isNaN(d.getTime())) return "";
  const p2 = (x) => String(x).padStart(2, "0");
  return `${d.getFullYear()}-${p2(d.getMonth() + 1)}-${p2(d.getDate())} ${p2(d.getHours())}:${p2(d.getMinutes())}`;
}

// 账号到期时间：来自 token（JWT）的 exp 声明，秒级时间戳。添加账号时即解析。
function expMs(exp) {
  if (exp == null || exp === "") return NaN;
  let n = Number(exp);
  if (isNaN(n)) return NaN;
  if (n < 1e12) n *= 1000; // 秒 -> 毫秒
  return n;
}

// 精确到分：YYYY-MM-DD HH:MM（本地时区）。
function fmtTs(ms) {
  if (ms == null || isNaN(ms)) return "";
  const d = new Date(ms);
  if (isNaN(d.getTime())) return "";
  const p2 = (x) => String(x).padStart(2, "0");
  return `${d.getFullYear()}-${p2(d.getMonth() + 1)}-${p2(d.getDate())} ${p2(d.getHours())}:${p2(d.getMinutes())}`;
}

// token（登录凭证）到期：来自 JWT 的 exp（秒级），约 60 天有效——这不是订阅到期。
function fmtExpiry(exp) {
  return fmtTs(expMs(exp));
}

function relRemain(ms) {
  if (ms <= 0) return "已到期";
  const days = Math.floor(ms / 86400000);
  const hours = Math.floor((ms % 86400000) / 3600000);
  const mins = Math.floor((ms % 3600000) / 60000);
  if (days > 0) return `剩 ${days}天${hours}小时`;
  if (hours > 0) return `剩 ${hours}小时${mins}分`;
  return `剩 ${mins}分`;
}

// 真实订阅到期/续费日：优先“待取消日”（真会停），否则本计费周期结束（续费日）。ISO 字符串。
function subEndMs(st) {
  if (!st) return NaN;
  const v = st.pendingCancellationDate || st.billingCycleEnd;
  if (!v) return NaN;
  const t = Date.parse(v);
  return isNaN(t) ? NaN : t;
}

function expiryCell(a, st) {
  const subMs = subEndMs(st);
  if (!isNaN(subMs)) {
    const s = fmtTs(subMs);
    const ms = subMs - Date.now();
    const willCancel = !!(st && st.pendingCancellationDate);
    const active = st && st.subscriptionStatus === "active";
    let tag;
    if (willCancel) tag = `<span class="pill bad">到期不续</span>`;
    else if (active) tag = `<span class="pill ok">自动续费</span>`;
    else tag = `<span class="pill warn">${esc(st.subscriptionStatus || "未续费")}</span>`;
    const yr = st.isYearlyPlan ? "年付" : "月付";
    const tokenExp = (st && st.tokenExp) || (a && a.exp);
    const tokenHint = tokenExp ? `<div class="hint mono">登录至 ${esc(fmtExpiry(tokenExp))}</div>` : "";
    return `<div class="mono qmain">${esc(s)}</div><div class="hint">${tag} ${esc(relRemain(ms))} · ${yr}</div>${tokenHint}`;
  }
  // 还没刷新到订阅信息：退回显示 token 登录有效期，并提示刷新。
  const s = fmtExpiry(a && a.exp);
  if (!s) return `<span class="hint">—</span>`;
  return `<div class="mono qmain">${esc(s)}</div><div class="hint">登录token有效期 · 刷新看订阅</div>`;
}

// bot 额度是周期性重置的：给出距下次重置的倒计时。
function fmtResetRelative(v) {
  if (v == null || v === "") return "";
  let d;
  if (typeof v === "number" || /^\d+$/.test(String(v))) {
    let n = Number(v);
    if (n < 1e12) n *= 1000;
    d = new Date(n);
  } else {
    d = new Date(v);
  }
  if (isNaN(d.getTime())) return "";
  const ms = d.getTime() - Date.now();
  if (ms <= 0) return "可重置";
  const totalHours = Math.floor(ms / 3600000);
  const days = Math.floor(totalHours / 24);
  const hours = totalHours % 24;
  if (days > 0) return `还剩 ${days}天${hours}小时`;
  const mins = Math.floor((ms % 3600000) / 60000);
  return `还剩 ${hours}小时${mins}分`;
}

// 本周期起始在 24 小时内 → 视为「今天刚重置」。
function isJustReset(v) {
  if (v == null || v === "") return false;
  let d;
  if (typeof v === "number" || /^\d+$/.test(String(v))) {
    let n = Number(v);
    if (n < 1e12) n *= 1000;
    d = new Date(n);
  } else {
    d = new Date(v);
  }
  if (isNaN(d.getTime())) return false;
  const ms = Date.now() - d.getTime();
  return ms >= 0 && ms <= 24 * 3600000;
}

// 领取/刷新拿到真实邮箱后回写行 label（并持久化到 Python 侧）。
function applyEmail(id, email) {
  if (!email || !String(email).includes("@")) return;
  const a = accounts.find((x) => x.id === id);
  if (a && a.label !== email) {
    a.label = email;
    const bridge = api();
    if (bridge && bridge.set_label) bridge.set_label(id, email);
  }
}

// 只记忆有意义的稳定状态；run/bad 不落盘，避免重开显示"处理中/失败"。
const PERSIST_KINDS = new Set(["ok", "idle", "card"]);
let persistTimer = null;
function schedulePersist() {
  clearTimeout(persistTimer);
  persistTimer = setTimeout(() => {
    const clean = {};
    for (const [id, st] of Object.entries(rowState)) {
      if (st && PERSIST_KINDS.has(st.kind)) clean[id] = st;
    }
    const bridge = api();
    if (bridge && bridge.save_status) bridge.save_status(clean);
  }, 400);
}

function statusCell(st) {
  if (!st) return `<span class="pill idle">待领取</span>`;
  switch (st.kind) {
    case "run":
      return `<span class="pill run">处理中…</span>`;
    case "ok":
      return `<span class="pill ok">${esc(st.label || "已开通")}</span>`;
    case "card":
      return `<span class="pill warn">需绑卡</span>`;
    case "bad":
      return `<span class="pill bad" title="${esc(st.detail || "")}">${esc(st.label || "失败")}</span>`;
    case "idle":
      return `<span class="pill idle">${esc(st.label || "未开通")}</span>`;
    default:
      return `<span class="pill idle">待领取</span>`;
  }
}

function quotaCell(st) {
  if (!st) return `<span class="hint">—</span>`;
  let html = "";
  // 主：Bot 周用量（会周期性重置，这才是 bot 额度）——百分比 + 重置时间/倒计时/刚重置。
  if (st.percent != null) {
    const v = Math.min(100, Math.max(0, Number(st.percent)));
    const fresh = isJustReset(st.periodStart) ? ` <span class="tag-reset">刚重置</span>` : "";
    html += `<div class="mono qmain">Bot 周用量 ${esc(fmtPercent(st.percent))}${fresh}</div><div class="bar"><i style="width:${v}%"></i></div>`;
    const reset = fmtReset(st.nextReset);
    if (reset) {
      const rel = fmtResetRelative(st.nextReset);
      html += `<div class="hint">重置 ${esc(reset)}${rel ? "（" + esc(rel) + "）" : ""}</div>`;
    }
    if (st.periodSpendUsd != null) {
      html += `<div class="hint mono">本周期消费 ${esc(fmtUsd(st.periodSpendUsd))}（全模型）</div>`;
    }
  }
  // 次：月账单口径（get-team-spend），明确标注「本月」以免和 bot 周额度混淆。
  if (st.spendUsd != null || st.teamPercent != null) {
    const pct = st.teamPercent != null ? st.teamPercent : st.totalPercent;
    const bits = [];
    if (st.spendUsd != null) bits.push(`本月消费 ${esc(fmtUsd(st.spendUsd))}`);
    if (pct != null) bits.push(`月用量 ${esc(fmtPercent(pct))}`);
    if (bits.length) html += `<div class="hint">${bits.join(" · ")}（账单月）</div>`;
  }
  if (st.spendUsd == null && st.teamPercent == null && st.percent == null && st.totalPercent != null) {
    html += `<div class="hint mono">总用量 ${esc(fmtPercent(st.totalPercent))}</div>`;
  }
  return html || `<span class="hint">—</span>`;
}

function membershipLabel(m) {
  const map = { free: "Free", pro: "Pro", pro_plus: "Pro+", "pro-plus": "Pro+", ultra: "Ultra", enterprise: "企业", team: "Team", business: "Business" };
  return map[String(m).toLowerCase()] || String(m);
}

function planCell(st) {
  if (!st) return `<span class="hint">—</span>`;
  const parts = [];
  if (st.unlimited) parts.push(`<span class="pill ok">无限</span>`);
  else if (st.membership) parts.push(`<span class="pill info">${esc(membershipLabel(st.membership))}</span>`);
  if (st.tierLabel) parts.push(`<span class="pill amount" title="Cursor 档位标签（非美元金额）">档 ${esc(st.tierLabel)}</span>`);
  if (st.teamId) parts.push(`<span class="pill idle">团队</span>`);
  return parts.length ? parts.join(" ") : `<span class="hint">—</span>`;
}

// 列表排序键：优先按订阅到期（最快到期在前）；无订阅数据时退回 token 到期；都没有排最后。
function accountSortKey(a) {
  const st = rowState[a.id];
  const sub = subEndMs(st);
  if (!isNaN(sub)) return sub;
  const t = expMs(a.exp);
  return isNaN(t) ? Infinity : t;
}

function render() {
  const tbody = $("rows");
  const ordered = accounts
    .map((a, i) => ({ a, i }))
    .sort((x, y) => {
      const kx = accountSortKey(x.a);
      const ky = accountSortKey(y.a);
      return kx !== ky ? kx - ky : x.i - y.i;
    })
    .map((o) => o.a);
  tbody.innerHTML = ordered
    .map((a) => {
      const st = rowState[a.id];
      const mail = a.label && a.label.includes("@") ? a.label : a.label || a.id;
      const checked = selected.has(a.id) ? " checked" : "";
      const webTok = String(a.tokenType || "").toLowerCase() === "web";
      const currentTag = currentAccountId === a.id
        ? `<span class="pill current" title="Cursor 当前使用此账号，Bot 请求会计入这一账号">当前 Cursor</span>`
        : "";
      const tokTag = webTok
        ? `<div class="hint"><span class="pill warn" title="网站会话：切号时会自动换成客户端登录票，稍慢几秒">网站会话·切号自动换</span></div>`
        : "";
      return `<tr data-id="${esc(a.id)}">
        <td class="col-chk"><input type="checkbox" class="rowchk" data-id="${esc(a.id)}"${checked}${busy ? " disabled" : ""} /></td>
        <td><div class="mail">${esc(mail)} ${currentTag}</div><div class="uid">${esc(a.id)}</div>${tokTag}</td>
        <td>${planCell(st)}</td>
        <td>${expiryCell(a, st)}</td>
        <td>${statusCell(st)}</td>
        <td>${quotaCell(st)}</td>
        <td class="col-act"><div class="act-wrap">
          <button class="btn tiny primary" data-act="claim" data-id="${esc(a.id)}"${busy ? " disabled" : ""}>领取</button>
          <button class="btn tiny" data-act="switch" data-id="${esc(a.id)}"${busy ? " disabled" : ""} title="${webTok ? "网站会话：切号时自动换客户端登录票（稍慢几秒）" : "切到本机 Cursor"}">切号</button>
          <button class="btn tiny" data-act="browser" data-id="${esc(a.id)}"${busy ? " disabled" : ""}>网页领取</button>
          <button class="btn tiny" data-act="copy" data-id="${esc(a.id)}"${busy ? " disabled" : ""} title="复制：邮箱----user_id::token">复制</button>
          <button class="btn tiny danger" data-act="remove" data-id="${esc(a.id)}"${busy ? " disabled" : ""}>移除</button>
        </div></td>
      </tr>`;
    })
    .join("");
  $("emptyHint").hidden = accounts.length > 0;
  $("countPill").textContent = accounts.length + " 个";
  syncSelectAll();
  updateStats();
}

function syncSelectAll() {
  const all = $("chkAll");
  if (!all) return;
  const n = accounts.length;
  const sel = accounts.filter((a) => selected.has(a.id)).length;
  all.checked = n > 0 && sel === n;
  all.indeterminate = sel > 0 && sel < n;
}

function updateStats() {
  let done = 0;
  let card = 0;
  for (const a of accounts) {
    const st = rowState[a.id];
    if (!st) continue;
    if (st.kind === "ok") done += 1;
    else if (st.kind === "card") card += 1;
  }
  $("statTotal").textContent = accounts.length;
  $("statDone").textContent = done;
  $("statCard").textContent = card;
}

function applyClaim(id, res) {
  if (!res) {
    rowState[id] = { kind: "bad", label: "无响应" };
    return;
  }
  applyEmail(id, res.email);
  if (res.outcome === "already") {
    rowState[id] = { kind: "ok", label: "已开通", percent: res.percent, teamId: res.teamId };
  } else if (res.outcome === "activated") {
    rowState[id] = { kind: "ok", label: "已开通" };
  } else if (res.outcome === "team_ok") {
    rowState[id] = { kind: "ok", label: "团队已开通", teamId: res.teamId };
  } else if (res.outcome === "card_required") {
    rowState[id] = { kind: "card", detail: res.detail || "需验证信用卡", url: res.url || "" };
  } else {
    rowState[id] = { kind: "bad", label: "失败", detail: res.detail || "未知原因" };
  }
  schedulePersist();
}

function applyStatus(id, res) {
  if (!res || res.error) {
    rowState[id] = { kind: "bad", label: "查询失败", detail: (res && res.error) || "" };
    return;
  }
  applyEmail(id, res.email);
  rowState[id] = {
    kind: res.unlocked ? "ok" : "idle",
    label: res.unlocked ? "已开通" : "未开通",
    percent: res.percent,
    nextReset: res.nextReset,
    periodStart: res.periodStart,
    periodSpendUsd: res.periodSpendUsd,
    teamId: res.teamId,
    membership: res.membership,
    totalPercent: res.totalPercent,
    unlimited: res.unlimited,
    billingCycleStart: res.billingCycleStart,
    billingCycleEnd: res.billingCycleEnd,
    subscriptionStatus: res.subscriptionStatus,
    pendingCancellationDate: res.pendingCancellationDate,
    isYearlyPlan: res.isYearlyPlan,
    tokenExp: res.tokenExp,
    tierLabel: res.tierLabel,
    spendUsd: res.spendUsd,
    teamPercent: res.teamPercent,
    autoPercent: res.autoPercent,
    apiPercent: res.apiPercent,
  };
  schedulePersist();
}

// 自动刷新用的静默版：查询失败不覆盖已有数据；未开通但原先是「需绑卡」的行保留绑卡提示。
function applyStatusQuiet(id, res) {
  if (!res || res.error) return false;
  const prev = rowState[id];
  applyStatus(id, res);
  if (prev && prev.kind === "card" && rowState[id].kind !== "ok") {
    rowState[id] = { ...rowState[id], kind: "card", detail: prev.detail, url: prev.url };
    schedulePersist();
  }
  return true;
}

async function claimThenRefresh(id) {
  const res = await api().claim_one(id);
  applyClaim(id, res);
  try {
    const st = await api().status_one(id);
    if (st && !st.error) {
      applyStatus(id, st);
      // 刷新后仍未开通、且领取结果是需绑卡：保留「需绑卡」，同时留下套餐/订阅等字段。
      if (res && res.outcome === "card_required" && rowState[id] && rowState[id].kind !== "ok") {
        rowState[id] = {
          ...rowState[id],
          kind: "card",
          detail: res.detail || "需验证信用卡",
          url: res.url || "",
        };
        schedulePersist();
      }
    }
  } catch (e) {
    if (!rowState[id] || rowState[id].kind === "run") {
      rowState[id] = { kind: "bad", label: "异常", detail: String(e) };
    }
  }
  return res;
}

async function claimOne(id) {
  rowState[id] = { kind: "run" };
  render();
  const res = await claimThenRefresh(id);
  render();
  return res;
}

async function statusOne(id) {
  rowState[id] = { kind: "run" };
  render();
  try {
    applyStatus(id, await api().status_one(id));
  } catch (e) {
    rowState[id] = { kind: "bad", label: "异常", detail: String(e) };
  }
  render();
}

async function detectLocal() {
  toast("正在读取本机 Cursor 登录账号…");
  try {
    const res = await api().detect_local_account();
    if (!res || !res.ok) {
      toast("探测失败：" + ((res && res.error) || "未知原因"));
      return;
    }
    accounts = res.accounts || [];
    setCurrentAccount(res);
    render();
    toast("已探测本机账号：" + (res.email || res.id || "本机"));
    if (res.id) statusOne(res.id);
  } catch (e) {
    toast("探测失败：" + String(e));
  }
}

async function switchAccount(id) {
  const resetMid = !!($("resetMidChk") && $("resetMidChk").checked);
  const a = accounts.find((x) => x.id === id);
  const webTok = a && String(a.tokenType || "").toLowerCase() === "web";
  toast(
    (webTok ? "网站会话切号：正在换客户端登录票（约几秒）… " : "") +
      (resetMid ? "正在切号并重置机器码，Cursor 将自动重启…" : "正在切号，Cursor 将自动重启…")
  );
  try {
    const res = await api().switch_account(id, resetMid);
    if (res && res.ok) {
      setCurrentAccount({ ok: true, id, email: res.email || (a && a.label) || id });
      render();
      toast(
        `已切换到 ${res.email || id}${res.resetMachineId ? "（已重置机器码）" : ""}` +
          `${res.exchanged ? "（网站会话已换客户端票）" : ""}，Cursor 正在重启` +
          (res.warning ? "　⚠ " + res.warning : "")
      );
    } else {
      toast("切号失败：" + ((res && res.error) || "未知原因"));
    }
  } catch (e) {
    toast("切号失败：" + String(e));
  }
}

async function openLogin(id) {
  toast("正在打开浏览器并注入登录，请稍候…");
  try {
    const res = await api().open_login(id);
    if (res && res.ok) {
      const browserName = { edge: "Edge", chromium: "Chromium", chrome: "Chrome" }[res.browser] || "浏览器";
      toast(`已在 ${browserName} 打开领取页，请在浏览器里手动完成`);
    } else {
      toast("打开失败：" + ((res && res.error) || "未知原因"));
    }
  } catch (e) {
    toast("打开失败：" + String(e));
  }
}

function targetIds() {
  // 有勾选就只处理勾选的，否则处理全部。
  return selected.size ? accounts.filter((a) => selected.has(a.id)).map((a) => a.id) : accounts.map((a) => a.id);
}

function readConcurrency() {
  const el = $("concInput");
  let n = parseInt(el && el.value, 10);
  if (isNaN(n)) n = 3;
  return Math.min(10, Math.max(1, n));
}

async function runBatch(kind) {
  if (busy || accounts.length === 0) return;
  const ids = targetIds();
  if (ids.length === 0) {
    toast("没有可处理的账号");
    return;
  }
  const conc = readConcurrency();
  busy = true;
  render();
  const wrap = $("progressWrap");
  const bar = $("progressBar");
  const text = $("progressText");
  wrap.hidden = false;
  const total = ids.length;
  const label = kind === "claim" ? "领取" : "刷新";
  let done = 0;
  let next = 0;
  bar.style.width = "0%";
  text.textContent = `${label} 0/${total}（并发 ${conc}）`;

  async function worker() {
    while (next < ids.length) {
      const id = ids[next++];
      rowState[id] = { kind: "run" };
      render();
      try {
        if (kind === "claim") await claimThenRefresh(id);
        else applyStatus(id, await api().status_one(id));
      } catch (e) {
        rowState[id] = { kind: "bad", label: "异常", detail: String(e) };
      }
      done += 1;
      bar.style.width = ((done / total) * 100).toFixed(1) + "%";
      text.textContent = `${label} ${done}/${total}（并发 ${conc}）`;
      render();
    }
  }

  const workers = [];
  for (let i = 0; i < Math.min(conc, total); i++) workers.push(worker());
  await Promise.all(workers);

  bar.style.width = "100%";
  text.textContent = `完成 ${done}/${total}`;
  busy = false;
  render();
  setTimeout(() => (wrap.hidden = true), 1500);
  toast(`${label}完成：${total} 个${selected.size ? "（仅选中）" : ""}`);
}

// ---- 每隔 1 分钟自动刷新 Bot 周用量 ----
// 与手动「刷新状态」不同：不把行置为「处理中」、不显示进度条、不弹 toast，失败静默保留旧数据。
const AUTO_REFRESH_MS = 60 * 1000;
let autoRefreshTimer = null;
let autoRefreshRunning = false;

function fmtClock(d) {
  const p2 = (x) => String(x).padStart(2, "0");
  return `${p2(d.getHours())}:${p2(d.getMinutes())}:${p2(d.getSeconds())}`;
}

function setAutoRefreshHint(text) {
  const el = $("autoRefreshHint");
  if (el) el.textContent = text || "";
}

async function autoRefreshTick() {
  // 批量领取/刷新进行中，或上一轮尚未跑完：跳过本轮，避免请求叠加。
  if (busy || autoRefreshRunning || accounts.length === 0) return;
  autoRefreshRunning = true;
  setAutoRefreshHint("刷新中…");
  const ids = accounts.map((a) => a.id);
  const conc = readConcurrency();
  let next = 0;
  let okCount = 0;
  let failCount = 0;

  async function worker() {
    while (next < ids.length) {
      const id = ids[next++];
      // 期间可能被手动操作接管（处理中）或被移除：不动它。
      if (!accounts.some((a) => a.id === id)) continue;
      if (rowState[id] && rowState[id].kind === "run") continue;
      try {
        if (applyStatusQuiet(id, await api().status_one(id))) okCount += 1;
        else failCount += 1;
      } catch (e) {
        failCount += 1;
      }
    }
  }

  try {
    const workers = [];
    for (let i = 0; i < Math.min(conc, ids.length); i++) workers.push(worker());
    await Promise.all(workers);
    if (!busy) render();
    setAutoRefreshHint(`上次 ${fmtClock(new Date())}${failCount ? `（${failCount} 个失败）` : ""}`);
  } finally {
    autoRefreshRunning = false;
  }
}

function startAutoRefresh() {
  if (autoRefreshTimer) return;
  autoRefreshTimer = setInterval(autoRefreshTick, AUTO_REFRESH_MS);
  setAutoRefreshHint("每分钟");
}

function stopAutoRefresh() {
  if (autoRefreshTimer) clearInterval(autoRefreshTimer);
  autoRefreshTimer = null;
  setAutoRefreshHint("");
}

function applyAutoRefreshSetting(enabled, persist) {
  const chk = $("autoRefreshChk");
  if (chk) chk.checked = !!enabled;
  if (enabled) startAutoRefresh();
  else stopAutoRefresh();
  if (persist) {
    const bridge = api();
    if (bridge && bridge.set_settings) bridge.set_settings({ autoRefresh: !!enabled });
  }
}

async function importFiles() {
  const res = await api().import_files();
  accounts = res.accounts || [];
  render();
  toast(res.added ? `导入 ${res.added} 个账号` : "未识别到账号");
}

async function addText() {
  const text = $("tokenInput").value.trim();
  if (!text) {
    toast("先粘贴 token 或 JSON");
    return;
  }
  const res = await api().import_text(text);
  accounts = res.accounts || [];
  $("tokenInput").value = "";
  render();
  toast(res.added ? `添加 ${res.added} 个账号` : "未识别到有效 token");
}

async function clearAll() {
  accounts = await api().clear_accounts();
  for (const k of Object.keys(rowState)) delete rowState[k];
  selected.clear();
  api().save_status({});
  render();
  toast("已清空");
}

// 写剪贴板：优先浏览器 API，失败退回隐藏 textarea + execCommand，再失败交给 Python 的 clip.exe。
async function copyText(text) {
  try {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch (e) {}
  try {
    const ta = document.createElement("textarea");
    ta.value = text;
    ta.style.position = "fixed";
    ta.style.top = "-1000px";
    ta.style.opacity = "0";
    document.body.appendChild(ta);
    ta.focus();
    ta.select();
    const ok = document.execCommand("copy");
    document.body.removeChild(ta);
    if (ok) return true;
  } catch (e) {}
  try {
    const bridge = api();
    if (bridge && bridge.clip_set) {
      const r = await bridge.clip_set(text);
      return !!(r && r.ok);
    }
  } catch (e) {}
  return false;
}

// 单条记录：复制该账号 + token（JSON，和导入格式一致，可直接粘回「添加到列表」重新导入）。
async function copyAccount(id) {
  let res;
  try {
    res = await api().account_export_text(id);
  } catch (e) {
    res = null;
  }
  if (!res || !res.ok) {
    toast("复制失败：" + ((res && res.error) || "账号不存在"));
    return;
  }
  const ok = await copyText(res.text);
  toast(ok ? `已复制 ${res.email || id}（邮箱----user::token）` : "复制失败：请重试或手动复制");
}

// 是否用过 bot：Sand 本周用量 > 0（就是列表里「Bot 周用量」那个百分比）。
function usedBotQuota(st) {
  const p = Number(st && st.percent);
  return !isNaN(p) && p > 0;
}

// 互斥分类：一个号只进第一段命中的。用过 Bot 的最高优先，单独隔离；付费档位再拆开。
function exportBucket(a) {
  const st = rowState[a.id];
  if (!st || st.kind === "run") return "other";
  const m = String(st.membership || "").toLowerCase().replace(/-/g, "_");
  const renewing = st.subscriptionStatus === "active" && !st.pendingCancellationDate;
  const hasBot = st.kind === "ok";
  // 用过 bot 额度的先单独拎出来，避免和「干净未用」的号混在一起（也不会重复出现在档位段）。
  if (hasBot && usedBotQuota(st)) return "used_bot";
  if (renewing) {
    if (m === "pro") return "pro";
    if (m === "pro_plus" || m === "proplus") return "proplus";
    if (m === "ultra") return "ultra";
    if (m === "enterprise" || m === "team" || m === "business") return "paid_other";
  }
  if (hasBot) return "bot_no_renew";
  if (st.kind === "bad" || st.kind === "card" || st.kind === "idle") return "no_bot";
  return "other";
}

const EXPORT_SECTIONS = [
  { key: "used_bot", title: "0. 已使用 Bot 额度（本周用量>0，慎用）" },
  { key: "pro", title: "1. Pro 自动续费（未用 Bot）" },
  { key: "proplus", title: "2. Pro+ 自动续费（未用 Bot）" },
  { key: "ultra", title: "3. Ultra 自动续费（未用 Bot）" },
  { key: "paid_other", title: "4. 其它付费自动续费（未用 Bot）" },
  { key: "bot_no_renew", title: "5. 未续费但仍有 Bot（未用）" },
  { key: "no_bot", title: "6. 领取失败 / 无 Bot" },
  { key: "other", title: "7. 未刷新 / 查询失败" },
];

async function exportAllClassified() {
  if (accounts.length === 0) {
    toast("列表为空，没有可导出的账号");
    return;
  }
  const buckets = {};
  for (const spec of EXPORT_SECTIONS) buckets[spec.key] = [];
  const seen = new Set();
  for (const a of accounts) {
    if (seen.has(a.id)) continue;
    seen.add(a.id);
    const key = exportBucket(a);
    (buckets[key] || buckets.other).push(a.id);
  }
  const sections = EXPORT_SECTIONS
    .filter((spec) => buckets[spec.key].length)
    .map((spec) => ({ title: spec.title, ids: buckets[spec.key] }));
  const unrefreshed = buckets.other.length;
  try {
    const res = await api().export_accounts({ sections });
    if (res && res.ok) {
      if (res.text) await copyText(res.text);
      toast(
        `已导出 ${res.count} 个账号（分段 txt，已复制）` +
          (unrefreshed ? `；其中 ${unrefreshed} 个未刷新` : "")
      );
    } else if (res && res.error === "已取消") {
      toast("已取消导出");
    } else {
      toast("导出失败：" + ((res && res.error) || "未知原因"));
    }
  } catch (e) {
    toast("导出失败：" + String(e));
  }
}

function onTableClick(e) {
  const btn = e.target.closest("button[data-act]");
  if (!btn || busy) return;
  const id = btn.getAttribute("data-id");
  const act = btn.getAttribute("data-act");
  if (act === "remove") {
    api()
      .remove_account(id)
      .then((list) => {
        accounts = list || [];
        delete rowState[id];
        selected.delete(id);
        schedulePersist();
        render();
      });
  } else if (act === "browser") {
    openLogin(id);
  } else if (act === "switch") {
    switchAccount(id);
  } else if (act === "copy") {
    copyAccount(id);
  } else if (act === "claim") {
    claimOne(id);
  }
}

function onTableChange(e) {
  const chk = e.target.closest("input.rowchk");
  if (!chk) return;
  const id = chk.getAttribute("data-id");
  if (chk.checked) selected.add(id);
  else selected.delete(id);
  syncSelectAll();
}

function onSelectAll(e) {
  if (e.target.checked) accounts.forEach((a) => selected.add(a.id));
  else selected.clear();
  render();
}

async function refreshPatch() {
  const pill = $("patchPill");
  const info = $("patchInfo");
  pill.className = "pill idle";
  pill.textContent = "检测中…";
  const res = await api().patch_status();
  if (!res || !res.ok) {
    pill.className = "pill bad";
    pill.textContent = "未检测到 Cursor";
    info.textContent = (res && res.error) || "未找到本机 Cursor 安装。";
    return;
  }
  const versionMismatch = !res.streamMode && !res.streamCapable;
  if (res.streamMode) {
    pill.className = "pill ok";
    pill.textContent = "Stream 模式";
  } else if (versionMismatch) {
    pill.className = "pill bad";
    pill.textContent = "版本不符";
  } else if (res.installed) {
    pill.className = "pill idle";
    pill.textContent = "已打补丁";
  } else {
    pill.className = "pill idle";
    pill.textContent = "未打补丁";
  }
  if (versionMismatch) {
    // 版本不对：打补丁不会生效，Sand 工具用不了。醒目提示，别让人白打。
    info.textContent =
      `⚠ 需 Cursor 3.18.9 才能打补丁：当前 ${res.version || "?"} 不含 agent-host 锚点，` +
      `打了也不生效（Sand 工具仍用不了）。请先装 3.18.9 并关自动更新。 · ${res.path || ""}`;
  } else {
    let streamHint = res.streamMode
      ? "Stream 回路已启用"
      : res.streamCapable
        ? "可打 Stream 补丁，尚未完整启用"
        : "";
    if (res.streamMode) {
      streamHint += res.subagent
        ? " · 子代理/Multitask 已启用"
        : res.subagentLegacy
          ? " · ⚠ 子代理补丁为旧版（子代理会报 Unknown model ID），重打可修复"
          : " · 子代理未启用，重打可补齐";
      if (res.moveExecLegacy) {
        // ≤1.1.9 强制 move_exec：每条消息首 token 固定多等 ~10 秒，Rules 也不生效。
        streamHint += " · ⚠ 旧版 move_exec 强制仍在（每条消息多等约 10 秒），重打可去掉";
      }
    }
    const kind = res.remoteServer ? "（Remote SSH 服务端）" : "";
    info.textContent = `Cursor ${res.version || "?"}${kind} · ${res.path || ""} · ${streamHint}`;
  }
}

async function doPatch() {
  // 版本不对就先拦一下：3.18.9 之外没有 agent-host 锚点，打了也白打。
  try {
    const st = await api().patch_status();
    if (st && st.ok && st.streamCapable === false) {
      const msg =
        `当前 Cursor ${st.version || ""} 没有 3.18.9 的 agent-host 锚点：\n` +
        `打补丁不会生效，Sand 工具依然用不了。\n\n` +
        `请先安装 Cursor 3.18.9（并关闭自动更新）再打补丁。\n\n仍要继续尝试吗？`;
      if (window.confirm(msg) === false) {
        toast("已取消：请先安装 Cursor 3.18.9 再打补丁");
        return;
      }
    }
  } catch (e) {}
  $("btnPatch").disabled = true;
  $("btnRestore").disabled = true;
  toast("正在打补丁，随后会自动重启 Cursor…");
  const res = await api().apply_patch();
  $("btnPatch").disabled = false;
  $("btnRestore").disabled = false;
  toast(res && res.ok ? "补丁完成，Cursor 将自动重启" : "打补丁失败：" + ((res && res.error) || ""));
  refreshPatch();
}

async function doRestore() {
  $("btnPatch").disabled = true;
  $("btnRestore").disabled = true;
  toast("正在回退，随后会自动重启 Cursor…");
  const res = await api().restore_patch();
  $("btnPatch").disabled = false;
  $("btnRestore").disabled = false;
  toast(res && res.ok ? "已回退，Cursor 将自动重启" : "回退失败：" + ((res && res.error) || ""));
  refreshPatch();
}

function showHelp() {
  $("helpMask").hidden = false;
}

function hideHelp() {
  $("helpMask").hidden = true;
  if ($("helpHide").checked) {
    const bridge = api();
    if (bridge && bridge.set_settings) bridge.set_settings({ hideHelp: true });
  }
}

async function doSetPath() {
  const path = $("cursorPathInput").value.trim();
  toast("正在设置 Cursor 路径…");
  try {
    const res = await api().set_cursor_path(path);
    toast(res && res.ok ? (path ? "已设置路径" : "已恢复自动检测") : "设置失败：" + ((res && res.error) || "路径无效"));
  } catch (e) {
    toast("设置失败：" + String(e));
  }
  refreshPatch();
}

async function boot() {
  $("btnDetectLocal").addEventListener("click", detectLocal);
  $("btnImportFile").addEventListener("click", importFiles);
  $("btnAddText").addEventListener("click", addText);
  $("btnClear").addEventListener("click", clearAll);
  $("btnClaimAll").addEventListener("click", () => runBatch("claim"));
  $("btnRefresh").addEventListener("click", () => runBatch("status"));
  $("btnExport").addEventListener("click", exportAllClassified);
  $("btnPatch").addEventListener("click", doPatch);
  $("btnRestore").addEventListener("click", doRestore);
  $("btnSetPath").addEventListener("click", doSetPath);
  $("rows").addEventListener("click", onTableClick);
  $("rows").addEventListener("change", onTableChange);
  $("chkAll").addEventListener("change", onSelectAll);
  $("helpOk").addEventListener("click", hideHelp);
  $("autoRefreshChk").addEventListener("change", (e) => {
    applyAutoRefreshSetting(e.target.checked, true);
    toast(e.target.checked ? "已开启：每隔 1 分钟自动刷新 Bot 周用量" : "已关闭自动刷新");
  });
  refreshPatch();

  try {
    const [list, status] = await Promise.all([api().list_accounts(), api().load_status()]);
    accounts = list || [];
    if (status) {
      const ids = new Set(accounts.map((a) => a.id));
      for (const [id, st] of Object.entries(status)) {
        if (ids.has(id)) rowState[id] = st;
      }
    }
    render();
  } catch (e) {
    render();
  }

  await refreshCurrentAccount();

  let settings = null;
  try {
    settings = await api().get_settings();
  } catch (e) {
    settings = null;
  }
  if (!settings || !settings.hideHelp) showHelp();
  // 默认开启；只有用户明确关过（autoRefresh === false）才不启动。
  applyAutoRefreshSetting(!(settings && settings.autoRefresh === false), false);
}

if (window.pywebview && window.pywebview.api) {
  boot();
} else {
  window.addEventListener("pywebviewready", boot);
}
