"""Cursor Sand 客户端模式补丁工具。

规则分组：
  - 客户端身份 / 资格 / 会员伪装（渲染层与扩展宿主）
  - Stream 回路：managed-local 路由 + 本地 runtime + agent-host sand 身份
    （move_exec 门控保持官方值；1.1.9 及更早版本强制开启的写法会在 install 时还原）
  - 子代理：Task 工具、resume/summarize/后台完成动作放行、Multitask 等模式放行（agent-host）
    与后台子代理完成唤醒（渲染层）
支持桌面版与 Remote SSH 服务端（~/.cursor-server/bin/<os>/<commit>，无渲染层）两种布局。

交互式运行：
    python "Sand客户端模式安装工具.py"

命令行运行：
    python "Sand客户端模式安装工具.py" install
    python "Sand客户端模式安装工具.py" uninstall
    python "Sand客户端模式安装工具.py" set-path <Cursor路径|auto>
    SAND_CURSOR_INSTALL_DIR=~/.cursor-server/bin/linux-x64/<commit> python3 sand_patch.py install
"""

from __future__ import annotations

import argparse
import base64
import ctypes
import hashlib
import json
import os
import re
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple, Union


TOOL_VERSION = "1.1.11"
CONFIG_VERSION = 1

SAND_CLIENT_MARKER = "/*SAND_CLIENT_MODE_V1*/"
SAND_CLIENT_EXISTING_MARKER = "/*SAND_CLIENT_EXISTING_V1*/"
SAND_ELIGIBILITY_MARKER = "/*SAND_ELIGIBILITY_MODE_V1*/"
SAND_MODEL_UNLOCK_MARKER = "/*SAND_MODEL_UNLOCK_V1*/"
SAND_MEM_PRO_MARKER = "/*SAND_MEM_PRO_V1*/"
SAND_MAXMODE_MARKER = "/*SAND_MAXMODE_V1*/"
SAND_GLASSFIX_MARKER = "/*SAND_GLASSFIX_V1*/"
SAND_HDRFIX_MARKER = "/*SAND_HDRFIX_V1*/"
SAND_HDRFIX_V2_MARKER = "/*SAND_HDRFIX_V2*/"
# Agent Run 必须出 ide：Connect 拦截器会在 prepareAgentRunRequest 之后再次
# applyRequestHeaders，只在返回前改 ide 会被这次调用改回 sand。
SAND_HDRFIX_V2_FN = (
    '(function(r){try{var u=String((r&&r.url)||""),s=String((r&&r.service&&r.service.typeName)||"");'
    'if(/AgentService|\\/agent\\.v1\\./.test(u+s))return"ide"}catch(x){}return"sand"})'
)
HEADER_SET_SIMPLE_RE = re.compile(
    r"([A-Za-z_$][\w$]*)\.header\.set\(\s*([\"'])x-cursor-client-type\2\s*,\s*"
    r"((?:[A-Za-z_$][\w$]*\s*\?\?\s*)?([\"'])(ide|sand|glass)\4)"
    r"((?:/\*SAND[A-Z0-9_]*_V1\*/)*)"
    r"\)"
)
# 智能分流函数永远返回非空串，`||(原实参)` 是永不求值的死代码，只为让卸载能逐字节还原
# `g??"ide"` 这类带 fallback 变量的原文（旧写法直接丢弃了原实参，卸载后只剩 "ide"）。
HDRFIX_V2_REMOVE_RE = re.compile(
    re.escape(SAND_HDRFIX_V2_FN)
    + r"\([A-Za-z_$][\w$]*\)"
    + re.escape(SAND_HDRFIX_V2_MARKER)
    + r"(?:\|\|\(((?:[A-Za-z_$][\w$]*\s*\?\?\s*)?[\"'](?:ide|sand|glass)[\"'])\))?"
)
SAND_MEMBERSHIP_MARKER = "/*SAND_MEMBERSHIP_SPOOF_V1*/"
SAND_MANAGED_LOCAL_ROUTE_MARKER = "/*SAND_MANAGED_LOCAL_ROUTE_V1*/"
SAND_DIRECT_STREAM_MARKER = "/*SAND_DIRECT_INFERENCE_STREAM_V1*/"
SAND_AGENT_HOST_ENABLEMENT_MARKER = "/*SAND_AGENT_HOST_ENABLEMENT_V1*/"
SAND_LOCAL_RUNTIME_LOAD_MARKER = "/*SAND_LOCAL_RUNTIME_LOAD_V1*/"
SAND_AGENT_HOST_IDENTITY_MARKER = "/*SAND_AGENT_HOST_IDENTITY_V1*/"
SAND_AGENTEXEC_KEEP_MARKER = "/*SAND_AGENTEXEC_KEEP_V1*/"
SAND_AGENT_IDE_MARKER = "/*SAND_AGENT_IDE_V1*/"
SAND_STREAM_HOOK_MARKER = "/*SAND_STREAM_HOOK_V1*/"
# 1.1.9 及更早版本把 cursor_agent_host_move_exec 门控强制为真（marker 见下）。1.1.10 起不再
# 强制：这两个 marker 都视为「旧版残留」，install / uninstall 时按 marker 还原为官方原文。
# 原因见下文 4) 的说明（强制 move_exec 会让每条消息首 token 固定多等 10 秒）。
SAND_MOVE_EXEC_MARKER = "/*SAND_MOVE_EXEC_V1*/"
# 1.1.8 之前的 Stream 安装器使用过这个标记名，且直接丢弃了原门控表达式。
LEGACY_MOVE_EXEC_MARKERS: Tuple[str, ...] = (
    "/*SAND_AGENT_HOST_MOVE_EXEC_V1*/",
)
MOVE_EXEC_MARKERS: Tuple[str, ...] = (SAND_MOVE_EXEC_MARKER,) + LEGACY_MOVE_EXEC_MARKERS
# 子代理（Task 工具）组：让 managed-local 运行时也能派生/恢复子代理。
# 官方 managed-local 门控默认把 subagent 相关 run options、resume/summarize/
# backgroundTaskCompletion 动作全部踢回 connect（服务端 agent），并把 taskToolProps 置空。
SAND_SUBAGENT_RESUME_MODE_MARKER = "/*SAND_SUBAGENT_RESUME_AGENT_MODE_V1*/"
SAND_SUBAGENT_ROUTE_MARKER = "/*SAND_MANAGED_SUBAGENT_ROUTE_V1*/"
# V1 只放行 AGENT 模式且拒绝 simulated 消息；Multitask（「Start Multitasking」/「Build in
# Parallel」）走的是 mode=MULTITASK + isSimulatedMsg 的用户消息，V1 会被踢回 connect。
# V2 把 mode / simulated 两个判定改为死代码（保留原表达式便于字节级回退），本地运行时
# 对 AGENT/ASK/PLAN/DEBUG/TRIAGE/PROJECT/MULTITASK 都有对应的 system reminder 生成器。
SAND_ACTION_ROUTE_MARKER = "/*SAND_MANAGED_ACTION_ROUTE_V2*/"
SAND_ACTION_ROUTE_V1_MARKER = "/*SAND_MANAGED_ACTION_ROUTE_V1*/"
SAND_SUBAGENT_SESSION_MARKER = "/*SAND_MANAGED_SUBAGENT_SESSION_V1*/"
# V3（1.1.11）：子代理模型改为沿用父请求的 requestedModel.modelId。V2 及更早把 createAgentConfig
# 里解析后的复合 slug（形如 claude-fable-5-1-thinking-max，thinking / max 已拼进名字）当成
# 子代理的 requestedModel.modelId，而服务端只认基础 ID（claude-fable-5-1，thinking / effort /
# max 走 parameters 与 maxMode），子代理一启动就 ERROR_BAD_MODEL_NAME「Unknown model ID」。
SAND_TASK_TOOL_MARKER = "/*SAND_MANAGED_TASK_TOOL_V3*/"
# 1.1.x ≤ 1.1.10 与 Sand Stream Toolkit 1.2.6 写入的 V2 形态：升级时识别并替换为 V3。
SAND_TASK_TOOL_V2_MARKER = "/*SAND_MANAGED_TASK_TOOL_V2*/"
# Sand Stream Toolkit 1.2.4/1.2.5 写入的旧 taskToolProps 形态（无 subagentTypeName 短路、
# 空 modelsBySlug），升级时识别并替换，卸载时同样还原。
SAND_TASK_TOOL_V1_MARKER = "/*SAND_MANAGED_TASK_TOOL_V1*/"
# 其他 Sand 工具对同一 taskToolProps 锚点的注入（如 Toolkit 的 SAND_SUBAGENT_TASK_PROPS_V2，
# 形如 `taskToolProps:SAND_CHILD?void 0:/*SAND_SUBAGENT_TASK_PROPS_V2*/{...}`）。它已经用
# requestedModel.modelId，本工具遇到时不再叠加注入，并把它视为 Task 工具槽位已满足；
# 卸载时也不触碰它。`[^{}]` 保证只在锚点到对象开头之间查找，不会越过官方原文 `void 0}`。
FOREIGN_TASK_TOOL_PROPS_RE = re.compile(
    r"taskToolProps:[^{}]{0,120}?/\*SAND_(?!MANAGED_TASK_TOOL_V\d)[A-Z_0-9]+\*/\{"
)
# 渲染层（workbench desktop/glass）：后台子代理完成后唤醒父对话。官方只在
# long_running_jobs 门控开启时才派发 source==="subagent" 的完成事件，Sand 号门控关着，
# run_in_background 的子代理做完了父代理也收不到通知。Remote SSH 服务端没有渲染层，
# 由本机客户端那份 workbench 提供。
SAND_SUBAGENT_WAKE_MARKER = "/*SAND_SUBAGENT_COMPLETION_WAKE_V1*/"
# agent-host 侧子代理 marker（五类齐全才算子代理链路完整）。
SUBAGENT_MARKERS: Tuple[str, ...] = (
    SAND_SUBAGENT_RESUME_MODE_MARKER,
    SAND_SUBAGENT_ROUTE_MARKER,
    SAND_ACTION_ROUTE_MARKER,
    SAND_SUBAGENT_SESSION_MARKER,
    SAND_TASK_TOOL_MARKER,
)
# 旧版子代理 marker：状态里单独计数，提示用户重跑 install 升级。
LEGACY_SUBAGENT_MARKERS: Tuple[str, ...] = (
    SAND_ACTION_ROUTE_V1_MARKER,
    SAND_TASK_TOOL_V1_MARKER,
    SAND_TASK_TOOL_V2_MARKER,
)
# 子代理运行链就绪锚点：这些是 Cursor 本地运行时里稳定的日志/工厂字面量，缺任何一个都说明
# 当前构建没有对应代码路径，此时注入 taskToolProps 只会得到一个永远失败的 Task 工具。
# 只在 cursor-agent-host 扩展目录内统计。
# execRuntimeReady：move_exec 门控为官方值（OFF）时 host 走的分支——从 cursor-agent-exec 取共享
# 运行时并激活它（该运行时同时负责向 workbench 推送 rules / skills / 子代理），工具执行器由它提供。
SUBAGENT_READY_ANCHORS: Tuple[Tuple[str, "re.Pattern[str]"], ...] = (
    (
        "execRuntimeReady",
        re.compile(
            re.escape("Acquired shared agent runtime from cursor-agent-exec")
        ),
    ),
    ("taskRunnerReady", re.compile(re.escape("Creating subagent and starting execution"))),
    ("taskFactoryReady", re.compile(r"void 0===\w+\.taskToolProps\?\w+\.fromTools\(\[\]\)")),
)
SAND_RPC_REWRITE_MARKER = "/*SAND_RPC_REWRITE_V1*/"
SAND_RPC_REWRITE_END = "/*SAND_RPC_REWRITE_END*/"
SAND_STREAM_WRAP_MARKER = "/*SAND_STREAM_WRAP_V1*/"
SAND_TRANSPORT_HOST_MARKER = "/*SAND_TRANSPORT_HOST_V1*/"
OLD_RPC_PATH = "agent.v1.AgentService/Run"
NEW_RPC_PATH = "aiserver.v1.InferenceService/Stream"

# 会员伪装 + 模型列表解锁注入：拦截 renderer 里的 fetch，
#   - 会员/用量/Stripe 类响应把 membershipType 等改成 pro（注意：full_stripe_profile 是 text/plain + 数组！）
#   - AvailableModels 响应把每个模型设 defaultOn:true
# 用 .text()+JSON.parse 兜住 text/plain；数组逐元素改。全程 try/catch，出错原样返回。语法已 node --check 校验。
SAND_MEMBERSHIP_SNIPPET = (
    SAND_MEMBERSHIP_MARKER
    + '(function(){try{var G=(typeof globalThis!=="undefined")?globalThis:(typeof self!=="undefined"?self:this);'
    + 'if(!G||G.__sandMemPatch)return;G.__sandMemPatch=1;'
    + 'var MEM={membershipType:"enterprise",membership_type:"enterprise",isTeamMember:true,teamId:28945905,teamMembershipType:"SELF_SERVE",subscriptionStatus:"active",subscription_status:"active"};'
    + 'function dm(a,b){if(a===null||typeof a!=="object")return a;for(var k in b){var v=b[k];'
    + 'if(v&&typeof v==="object"&&!Array.isArray(v)){a[k]=dm(typeof a[k]==="object"&&a[k]?a[k]:{},v);}else{a[k]=v;}}return a;}'
    + 'function isMem(u){try{return /membership|usage-summary|dashboard\\/get-me|auth\\/(me|full_stripe|stripe_profile)|GetUserInfo|getUserPrivilege|hard-limit/i.test(u);}catch(e){return false;}}'
    + 'function isModels(u){try{return /AvailableModels|available-models/i.test(u);}catch(e){return false;}}'
    + 'function pmod(b){try{var arr=(b&&b.models)||(b&&b.data&&b.data.models);if(Array.isArray(arr)){'
    + 'for(var i=0;i<arr.length;i++){var m=arr[i];if(m&&typeof m==="object"){m.defaultOn=true;m.default_on=true;}}}}catch(e){}return b;}'
    + 'function patchBody(b,mem,mod){if(mem){if(Array.isArray(b)){for(var i=0;i<b.length;i++){if(b[i]&&typeof b[i]==="object"){dm(b[i],MEM);}}}else if(b&&typeof b==="object"){dm(b,MEM);}}if(mod){b=pmod(b);}return b;}'
    + 'var OF=G.fetch;if(typeof OF==="function"){G.fetch=function(){var a=arguments;'
    + 'return OF.apply(this,a).then(function(r){try{var u=(a[0]&&a[0].url)?a[0].url:a[0];'
    + 'var mem=isMem(u),mod=isModels(u);if(!mem&&!mod){return r;}'
    + 'return r.clone().text().then(function(txt){var b;try{b=JSON.parse(txt);}catch(e){return r;}'
    + 'try{b=patchBody(b,mem,mod);}catch(e){}'
    + 'try{return new Response(JSON.stringify(b),{status:r.status,statusText:r.statusText,headers:r.headers});}catch(e){return r;}},'
    + 'function(){return r;});}catch(e){return r;}});};}}catch(e){}})();'
)

# Stream 核心：managed-local + 本地 runtime + agent-host sand 身份
# + 强制开启 agent host。move_exec 门控保持官方值（见下文 4)）。
# 1.1.1–1.1.3 曾把 hre() 短路成 Joe(raw client)，工具 get(CP._As) 落到 undefined.execute。
# 1.1.3 曾删掉强制 host 后的 return（AGENTEXEC_KEEP），workbench 继续等 agent-exec。
# 实测 14:11 日志：agent-exec 注册 30s+ 连超时，界面就是「超长时间」，
# 最终 ERROR_EXTENSION_HOST_TIMEOUT。1.1.5 打补丁时剥掉残留 KEEP。
# 1.1.4–1.1.9 仿 ThankCat 1.0.8 强制 move_exec 走同包 createAgentHostExec；1.1.10 发现它正是
# 「每条消息首 token 多等 10 秒」的根因，改回官方门控。

# 只往这两个 renderer 包注入会员伪装（有 fetch/window）。
MEMBERSHIP_TARGET_NAMES = ("workbench.desktop.main.js", "workbench.glass.main.js")

# 通用匹配「任意版本」的 membership 注入片段（marker 到第一个 IIFE 结尾 })(); ），用于刷新/删除旧片段。
MEMBERSHIP_SNIPPET_RE = re.compile(re.escape(SAND_MEMBERSHIP_MARKER) + r"[\s\S]*?\}\)\(\);")
RPC_SNIPPET_RE = re.compile(
    re.escape(SAND_RPC_REWRITE_MARKER) + r"[\s\S]*?" + re.escape(SAND_RPC_REWRITE_END)
)
RPC_SNIPPET_RE_LEGACY = re.compile(
    re.escape(SAND_RPC_REWRITE_MARKER) + r"[\s\S]*?\}\)\(\);"
)


def _strip_rpc_snippets(content: str) -> tuple[str, int]:
    next_content, n1 = RPC_SNIPPET_RE.subn("", content)
    n2 = 0
    if SAND_RPC_REWRITE_MARKER in next_content:
        next_content, n2 = RPC_SNIPPET_RE_LEGACY.subn("", next_content)
    return next_content, n1 + n2
STREAM_WRAP_RESTORE_RE = re.compile(
    r'(throw new Error\("INVARIANT VIOLATION: Transport is undefined for service: "\+\w+\.typeName\);return )'
    r'\(typeof globalThis\.__sandRewriteStream==="function"\?globalThis\.__sandRewriteStream\((\w+)\.transport,'
    r'([^)]+)\):\2\.transport\.stream\(\3\)\)'
    + re.escape(SAND_STREAM_WRAP_MARKER)
)
# Stream 必须打到 api2（_backendTransport），不能跟 Agent Run 一起走 agentn.global.api5。
# 1.0.3 只改了 RPC 路径，主机仍是 agent 后端，结果 HTTP 404，界面像「连接不上」。
_TRANSPORT_HOST_SWAPS: Tuple[Tuple[str, str], ...] = (
    (
        "this._overrideServiceNameToTransportMapLowerPriorityThanMethodOverrides[kt.typeName]=s.agentBidiTransport",
        "this._overrideServiceNameToTransportMapLowerPriorityThanMethodOverrides[kt.typeName]=this._backendTransport"
        + SAND_TRANSPORT_HOST_MARKER,
    ),
    (
        "this._overrideMethodNameToTransportMap[kt.methods.run.name]=s.agentBidiTransport",
        "this._overrideMethodNameToTransportMap[kt.methods.run.name]=this._backendTransport"
        + SAND_TRANSPORT_HOST_MARKER,
    ),
    (
        "this._overrideServiceNameToTransportMapLowerPriorityThanMethodOverrides[l.AgentService.typeName]=e.agentBidiTransport",
        "this._overrideServiceNameToTransportMapLowerPriorityThanMethodOverrides[l.AgentService.typeName]=this._backendTransport"
        + SAND_TRANSPORT_HOST_MARKER,
    ),
    (
        "this._overrideMethodNameToTransportMap[l.AgentService.methods.run.name]=e.agentBidiTransport",
        "this._overrideMethodNameToTransportMap[l.AgentService.methods.run.name]=this._backendTransport"
        + SAND_TRANSPORT_HOST_MARKER,
    ),
)
LEGACY_SAND_CLIENT_MARKER = "/*K" + "C_SAND_CLIENT_V1*/"
LEGACY_SAND_ELIGIBILITY_MARKER = "/*K" + "C_SAND_ELIGIBILITY_V1*/"
CLIENT_MARKER_PATTERN = re.escape(SAND_CLIENT_MARKER)
CLIENT_EXISTING_MARKER_PATTERN = re.escape(SAND_CLIENT_EXISTING_MARKER)
ELIGIBILITY_MARKER_PATTERN = re.escape(SAND_ELIGIBILITY_MARKER)
LEGACY_CLIENT_MARKER_PATTERN = re.escape(LEGACY_SAND_CLIENT_MARKER)
LEGACY_ELIGIBILITY_MARKER_PATTERN = re.escape(LEGACY_SAND_ELIGIBILITY_MARKER)
CLIENT_MARKER_GUARD_PATTERN = r"/\*[A-Z0-9_]*SAND_CLIENT(?:_(?:MODE|EXISTING))?_V1\*/"
ELIGIBILITY_MARKER_GUARD_PATTERN = r"/\*[A-Z0-9_]*SAND_ELIGIBILITY(?:_MODE)?_V1\*/"
SAND_ONBOARDING_URL = "https://cursor.com/bot/onboarding?product=grok-bot"

ANSI_RESET = "\033[0m"
ANSI_BOLD = "\033[1m"
ANSI_RED = "\033[31m"
ANSI_GREEN = "\033[32m"
ANSI_YELLOW = "\033[33m"
ANSI_BLUE = "\033[36m"

_COLOR_ENABLED = True


TARGET_SPECS: Tuple[Tuple[str, Optional[str]], ...] = (
    ("out/main.js", None),
    ("out/vs/workbench/api/worker/extensionHostWorkerMain.js", None),
    ("out/vs/workbench/api/node/extensionHostProcess.js", None),
    ("out/vs/workbench/workbench.glass.main.js", None),
    ("out/vs/workbench/workbench.desktop.main.js", None),
    (
        "out/vs/code/electron-utility/alwaysLocalSingleton/alwaysLocalSingletonMain.js",
        None,
    ),
    ("extensions/cursor-always-local/dist/main.js", "cursor-always-local"),
    (
        "extensions/cursor-local-agent-runtime/dist/main.js",
        "cursor-local-agent-runtime",
    ),
    ("extensions/cursor-agent-host/dist/main.js", "cursor-agent-host"),
    ("extensions/cursor-agent-exec/dist/main.js", "cursor-agent-exec"),
)

# agent-host dist 里承载 managed-local 路由 / Stream 逻辑的 chunk 文件名（657 / 675 / …）
# 随每次构建变化，不同 commit 的同版本号会拆到不同编号。写死编号会漏掉路由锚点所在 chunk，
# 直接表现为「版本对却切不过去」。改为运行时扫描整个 dist 目录（排除 main.js 与 *-worker.js）。
AGENT_HOST_DIST_REL = "extensions/cursor-agent-host/dist"

EXT_HOST_REL = "out/vs/workbench/api/node/extensionHostProcess.js"

ELIGIBILITY_PREFIXES: Tuple[str, ...] = (
    "function r4g(e){const{adminSettingsService:t",
    "function Vj_(t){const{adminSettingsService:e",
    "function inf(e){const{adminSettingsService:t",
    "function HSy(t){const{adminSettingsService:e",
    "function Q_f(e){const{adminSettingsService:t",
    "function BpS(t){const{adminSettingsService:e",
)


class SandToolError(RuntimeError):
    pass


@dataclass(frozen=True)
class CursorLayout:
    install_root: Path
    app_root: Path
    product_json: Path
    executable: Path
    target_paths: Tuple[Path, ...]
    ext_host_path: Optional[Path]
    version: str
    # Remote SSH 服务端（~/.cursor-server/bin/<os>/<commit>）：没有 workbench 渲染层，
    # 只承载扩展宿主侧的 agent-host / agent-exec / always-local 等目标。
    is_remote_server: bool = False


@dataclass(frozen=True)
class PlannedFile:
    original: bytes
    next_bytes: bytes
    mode: int


@dataclass
class PatchStats:
    is_glass: int = 0
    object_header: int = 0
    set_header: int = 0
    eligibility: int = 0
    adopted_sand: int = 0
    migrated_client: int = 0
    migrated_eligibility: int = 0
    model_unlock: int = 0
    rpc_rewrite: int = 0
    managed_local_route: int = 0
    local_runtime_load: int = 0
    direct_stream: int = 0
    agent_host_enablement: int = 0
    agent_host_identity: int = 0
    move_exec: int = 0
    subagent: int = 0
    subagent_wake: int = 0

    @property
    def total(self) -> int:
        return (
            self.is_glass
            + self.object_header
            + self.set_header
            + self.eligibility
            + self.model_unlock
            + self.migrated_client
            + self.migrated_eligibility
            + self.rpc_rewrite
            + self.managed_local_route
            + self.local_runtime_load
            + self.direct_stream
            + self.agent_host_enablement
            + self.agent_host_identity
            + self.move_exec
            + self.subagent
            + self.subagent_wake
        )


@dataclass
class RemoveStats:
    client_type: int = 0
    eligibility: int = 0
    rpc_rewrite: int = 0
    managed_local_route: int = 0
    local_runtime_load: int = 0
    direct_stream: int = 0
    agent_host_enablement: int = 0
    agent_host_identity: int = 0
    move_exec: int = 0
    subagent: int = 0
    subagent_wake: int = 0

    @property
    def total(self) -> int:
        return (
            self.client_type
            + self.eligibility
            + self.rpc_rewrite
            + self.managed_local_route
            + self.local_runtime_load
            + self.direct_stream
            + self.agent_host_enablement
            + self.agent_host_identity
            + self.move_exec
            + self.subagent
            + self.subagent_wake
        )


@dataclass(frozen=True)
class PatchStatus:
    client_markers: int
    eligibility_markers: int
    ide_matches: int
    external_sand_matches: int
    external_marker_count: int
    legacy_client_markers: int
    legacy_eligibility_markers: int
    patched_files: Tuple[Path, ...]
    managed_local_route_markers: int = 0
    local_runtime_load_markers: int = 0
    direct_stream_markers: int = 0
    agent_host_enablement_markers: int = 0
    agent_host_identity_markers: int = 0
    move_exec_markers: int = 0
    stream_capable: bool = False
    remote_server: bool = False
    subagent_markers: int = 0
    legacy_subagent_markers: int = 0
    subagent_wake_markers: int = 0
    # 渲染层里存在多少处唤醒锚点（原始或已打补丁）；服务端布局为 0。
    subagent_wake_anchors: int = 0
    # 本工具的 Task 工具注入数（已含在 subagent_markers 里）与其他 Sand 工具的 Task 工具注入数。
    task_tool_markers: int = 0
    foreign_task_tool_markers: int = 0

    @property
    def installed(self) -> bool:
        return (
            self.client_markers
            + self.eligibility_markers
            + self.legacy_client_markers
            + self.legacy_eligibility_markers
            + self.managed_local_route_markers
            + self.local_runtime_load_markers
            + self.direct_stream_markers
            + self.agent_host_enablement_markers
            + self.agent_host_identity_markers
            + self.move_exec_markers
            + self.subagent_markers
            + self.legacy_subagent_markers
            + self.subagent_wake_markers
            > 0
        )

    @property
    def task_tool_from_foreign(self) -> bool:
        """Task 工具配置由其他 Sand 工具提供（本工具不注入、不卸载）。"""
        return self.task_tool_markers == 0 and self.foreign_task_tool_markers > 0

    @property
    def subagent_installed(self) -> bool:
        # Task 工具槽位可由本工具 V3 或其他 Sand 工具的注入满足。
        effective = self.subagent_markers + (1 if self.task_tool_from_foreign else 0)
        return effective >= len(SUBAGENT_MARKERS)

    @property
    def subagent_wake_installed(self) -> bool:
        """渲染层唤醒补丁齐全（没有渲染层的服务端布局视为满足）。"""
        return self.subagent_wake_markers >= self.subagent_wake_anchors

    @property
    def stream_mode_installed(self) -> bool:
        # 1.1.3 短路 createPromptSession 会让工具 get(CP._As) 落到 undefined.execute，
        # 所以 direct_stream 必须为 0。move_exec 门控 1.1.10 起保持官方值，不再是必需锚点；
        # 残留的强制写法由 legacy_move_exec_forced 单独提示。
        # agent-host enablement 锚点在 workbench 渲染层；Remote SSH 服务端没有该文件，
        # 由本机客户端那份 workbench 提供，服务端只校验扩展宿主侧三类锚点。
        return (
            self.managed_local_route_markers > 0
            and self.local_runtime_load_markers > 0
            and self.direct_stream_markers == 0
            and (self.remote_server or self.agent_host_enablement_markers > 0)
            and self.agent_host_identity_markers > 0
        )

    @property
    def legacy_move_exec_forced(self) -> bool:
        """1.1.9 及更早版本强制开启 move_exec 的残留：每条消息首 token 多等约 10 秒。"""
        return self.move_exec_markers > 0


def _compile_client_rules() -> Tuple[Tuple[str, re.Pattern[str]], ...]:
    marker_guard = rf"(?!{CLIENT_MARKER_GUARD_PATTERN})"
    return (
        (
            "is_glass",
            re.compile(
                rf"(isGlass\s*\?\s*[\"']glass[\"']\s*:\s*)([\"'])(ide|sand)\2{marker_guard}"
            ),
        ),
        (
            "object_header",
            re.compile(
                rf"([\"']x-cursor-client-type[\"']\s*:\s*)([\"'])(ide|sand)\2{marker_guard}"
            ),
        ),
        (
            "set_header",
            re.compile(
                rf"(header\.set\(\s*[\"']x-cursor-client-type[\"']\s*,\s*"
                rf"[A-Za-z_$][A-Za-z0-9_$.]*\s*(?:\?\?|\|\|)\s*)"
                rf"([\"'])(ide|sand)\2{marker_guard}"
            ),
        ),
    )


CLIENT_RULES = _compile_client_rules()

# 以下五处是 3.18.9 Stream 回路的结构锚点。minified 变量名（对象 / 网关常量 / 循环变量 /
# catch 变量）随每次构建变化——同一版本号的不同 commit（About 里的 ...130 与官方下载的 ...137）
# 变量名并不相同，写死字面量会导致「Cursor 版本对，却一个锚点都命中不了 → 切不过去」。
# 因此这里用 \w+ 泛化易变部分，只锚定 checkFeatureGate / runtime:"managed-local" /
# agent_host_local_loop / createAgentHost 等稳定语义字面量；打补丁时保留原始片段（短路 +
# 死代码 / 注入语句），卸载按 marker 精确回退，保证字节级可还原（product 校验和据此通过）。

# 1) managed-local 路由：无条件返回 managed-local，原三元判定保留为死代码，便于精确回退。
MANAGED_LOCAL_ROUTE_RE = re.compile(
    r'try\{return(\(yield \w+\.checkFeatureGate\(\w+\)\)\?'
    r'\{runtime:"managed-local",reason:"eligible"\}:'
    r'\{runtime:"connect",reason:"gate-off"\})\}catch\((\w+)\)'
)
MANAGED_LOCAL_ROUTE_RESTORE_RE = re.compile(
    r'try\{return\{runtime:"managed-local",reason:"sand-client"\}'
    + re.escape(SAND_MANAGED_LOCAL_ROUTE_MARKER)
    + r";"
)


def _managed_local_route_sub(match: "re.Match[str]") -> str:
    original_ternary = match.group(1)
    catch_var = match.group(2)
    return (
        'try{return{runtime:"managed-local",reason:"sand-client"}'
        + SAND_MANAGED_LOCAL_ROUTE_MARKER
        + ";"
        + original_ternary
        + "}catch("
        + catch_var
        + ")"
    )


# 2) 本地 loop runtime：在 if(!t) 判定前注入 t=!0 强制加载，原 gate 判定与 catch 原样保留。
#    锚定稳定串 agent_host_local_loop，避免依赖 minified 的 gate 常量名。
LOCAL_RUNTIME_LOAD_RE = re.compile(
    r"(let (\w+)=!1;try\{\2=await \w+\.cursor\.checkFeatureGate\(\w+\)\}"
    r"catch\(\w+\)\{[^{}]*agent_host_local_loop[^{}]*\})"
    r"(if\(!\2\))"
)
LOCAL_RUNTIME_LOAD_RESTORE_RE = re.compile(
    re.escape(SAND_LOCAL_RUNTIME_LOAD_MARKER) + r"\w+=!0;"
)


def _local_runtime_load_sub(match: "re.Match[str]") -> str:
    head = match.group(1)
    var = match.group(2)
    tail_if = match.group(3)
    return head + SAND_LOCAL_RUNTIME_LOAD_MARKER + var + "=!0;" + tail_if


# 3) agent-host 身份：clientType ide→sand。此处无 minified 变量，字面量替换天然跨构建。
AGENT_HOST_IDENTITY_ORIGINAL = 'clientIdentity:{clientType:"ide"}'
AGENT_HOST_IDENTITY_PATCHED = (
    'clientIdentity:{clientType:"sand"'
    + SAND_AGENT_HOST_IDENTITY_MARKER
    + "}"
)
DIRECT_STREAM_ANCHOR = (
    "function hre(e){return t=>{return n=this,o=void 0,s=function*(){"
)
AGENT_HOST_ENABLEMENT_RE = re.compile(
    r"(this\._agentHostEnabled=)([A-Za-z_$][A-Za-z0-9_$]*)(,)"
)
AGENT_HOST_ENABLEMENT_PATCH_RE = re.compile(
    rf"([A-Za-z_$][A-Za-z0-9_$]*)=!0;"
    rf"{re.escape(SAND_AGENT_HOST_ENABLEMENT_MARKER)}"
    rf"(this\._agentHostEnabled=)\1(,)"
)
# 强制 agent-host 后，官方是 waitFor(host); return，不再等 agent-exec。
# 1.1.3 曾删掉 return（AGENTEXEC_KEEP）：host 开了但 agent-exec 永不注册，
# 卡 30s+ 后 ERROR_EXTENSION_HOST_TIMEOUT。move_exec ON 时执行器由
# 同包 createAgentHostExec 提供，必须保留这个 return。
AGENTEXEC_SKIP_ORIGINAL = (
    "waitForProviderRegistration(r.ctx.signal);return}await this._agentExecProviderService.waitForProviderRegistration"
)
AGENTEXEC_SKIP_PATCHED = (
    "waitForProviderRegistration(r.ctx.signal);"
    + SAND_AGENTEXEC_KEEP_MARKER
    + "}await this._agentExecProviderService.waitForProviderRegistration"
)
AGENT_IDE_INJECT_RE = re.compile(
    r"(?<!" + re.escape(SAND_AGENT_IDE_MARKER) + r"\);)"
    r"return\{headers:([A-Za-z_$][\w$]*),credentialFingerprint:"
)
AGENT_IDE_REMOVE_RE = re.compile(
    r'[A-Za-z_$][\w$]*\.set\("x-cursor-client-type","ide"'
    + re.escape(SAND_AGENT_IDE_MARKER)
    + r'\);'
)
STREAM_HOOK_REMOVE_RE = re.compile(
    r'if\(t&&t\.typeName==="agent\.v1\.AgentService"&&n&&n\.name==="Run"'
    r'&&typeof globalThis\.__sandStream==="function"\)'
    r'return globalThis\.__sandStream\(t,n,r,s,o,i,a\)'
    + re.escape(SAND_STREAM_HOOK_MARKER)
    + r';'
)
# 4) move_exec 网关（cursor_agent_host_move_exec）：保持官方值，不再强制为真。
#    1.1.4–1.1.9 把它强制为 !0，让 host 用同包 createAgentHostExec 提供工具执行器。但 move_exec
#    ON 时 host 不再激活 cursor-agent-exec 的运行时（日志：move_exec ON ... no cursor-agent-exec
#    runtime），而只有该运行时会向 workbench 推送 Cursor Rules / Agent Skills / 自定义子代理
#    （$updateCursorRules 等）。workbench 的 WorkbenchRequestContextExecutor.buildFromPushedData
#    每条消息都要 await 推送来的 rules，peek 不到就等满 10s 超时——requestTraces 实测
#    buildFromPushedData=10006ms、client.ttft≈10.7s，而 grok-4.6 真正的网络+模型首 token 只有
#    1~2s；同时 rules/skills 从不进 prompt。门控为 OFF 时 host 走官方默认路径：
#    createLiveExecRuntime 从 cursor-agent-exec 取共享运行时并激活，rules 正常推送，
#    工具执行器由该运行时提供。
#    这里只负责把旧版强制写法还原为官方原文（install 与 uninstall 共用）。
#    用稳定的 createAgentHost), 前缀锁定到唯一一处，绝不误伤同文件里另外两个同构 gate
#    （native cloud subagent ownership / subagent interaction policy）。
MOVE_EXEC_GATE_RE = re.compile(
    r"(createAgentHost\),)(\w+)=await Promise\.resolve\("
    r"(\w+\.cursor\.checkFeatureGate\(\w+\))\)\.catch\(\(\)=>!1\)"
)
# 1.1.8–1.1.9 写法：原 gate 读取以 !0||await... 死代码保留，可直接精确还原。
MOVE_EXEC_GATE_RESTORE_RE = re.compile(
    r"(createAgentHost\),)(\w+)=!0"
    + re.escape(SAND_MOVE_EXEC_MARKER)
    + r"\|\|await Promise\.resolve\("
    r"(\w+\.cursor\.checkFeatureGate\(\w+\))\)\.catch\(\(\)=>!1\)"
)
# 1.1.8 之前的写法丢弃了原表达式，只剩 `p=!0/*SAND_AGENT_HOST_MOVE_EXEC_V1*/`。原文形如
# `p=await Promise.resolve(r.cursor.checkFeatureGate(Us)).catch(()=>!1)`：vscode 别名 r 从同一
# 语句里紧随其后的 native-cloud-subagent 门控读取取回，门控常量名 Us 按其字面量
# "cursor_agent_host_move_exec" 在文件内反查，据此重建。
MOVE_EXEC_GATE_LEGACY_RE = re.compile(
    r"(createAgentHost\),)([\w$]+)=!0(?:"
    + "|".join(re.escape(marker) for marker in LEGACY_MOVE_EXEC_MARKERS)
    + r")(?=,[\w$]+=await Promise\.resolve\(([\w$]+)\.cursor\.checkFeatureGate\()"
)
MOVE_EXEC_GATE_NAME_RE = re.compile(r'([\w$]+)="cursor_agent_host_move_exec"')


def _move_exec_gate_restore(match: "re.Match[str]") -> str:
    prefix = match.group(1)
    var = match.group(2)
    gate = match.group(3)
    return prefix + var + "=await Promise.resolve(" + gate + ").catch(()=>!1)"


def _restore_move_exec_gates(content: str) -> Tuple[str, int]:
    """把所有旧版强制 move_exec 的写法还原为官方门控读取。返回 (新内容, 还原数)。"""
    content, total = MOVE_EXEC_GATE_RESTORE_RE.subn(_move_exec_gate_restore, content)
    if not any(marker in content for marker in LEGACY_MOVE_EXEC_MARKERS):
        return content, total
    gate_match = MOVE_EXEC_GATE_NAME_RE.search(content)
    if gate_match is None:
        return content, total
    gate_var = gate_match.group(1)

    def _legacy_restore(match: "re.Match[str]") -> str:
        prefix = match.group(1)
        var = match.group(2)
        vscode = match.group(3)
        return (
            f"{prefix}{var}=await Promise.resolve("
            f"{vscode}.cursor.checkFeatureGate({gate_var})).catch(()=>!1)"
        )

    content, legacy_count = MOVE_EXEC_GATE_LEGACY_RE.subn(_legacy_restore, content)
    return content, total + legacy_count


# 5) 子代理组（agent-host dist 657/675 chunk）。所有规则均按 marker 精确还原，字节级可回退。
# 5a) resume 的 mode 解析：resumeAgentId && mode UNSPECIFIED && !readonly 原本得到 UNSPECIFIED，
#     随后被门控判为 mode-not-supported 踢回 connect；改为 AGENT。
SUBAGENT_RESUME_MODE_RE = re.compile(
    r"((\w+)\.resumeAgentId&&\2\.mode===[\w$]+\.[\w$]+\.UNSPECIFIED&&!\2\.readonly\?)"
    r"([\w$]+\.[\w$]+)\.UNSPECIFIED(:\2\.mode===[\w$]+\.[\w$]+\.PLAN\?\3\.PLAN:)"
)
SUBAGENT_RESUME_MODE_RESTORE_RE = re.compile(
    r"\?" + re.escape(SAND_SUBAGENT_RESUME_MODE_MARKER) + r"([\w$]+\.[\w$]+)\.AGENT:"
)


def _subagent_resume_mode_sub(match: "re.Match[str]") -> str:
    return (
        match.group(1)
        + SAND_SUBAGENT_RESUME_MODE_MARKER
        + match.group(3)
        + ".AGENT"
        + match.group(4)
    )


# 5b) hasUnsupportedRunOptions：去掉 subagentTypeName / parentAgentToolCallId 两项，
#     子代理运行也允许进 managed-local。directMetaParentChildSubagent 仍保留为不支持。
SUBAGENT_ROUTE_RE = re.compile(
    r"(!0===(\w+)\.runOptions\.excludeWorkspaceContext)"
    r"\|\|void 0!==\2\.runOptions\.subagentTypeName"
    r"\|\|void 0!==\2\.runOptions\.parentAgentToolCallId"
    r"(\|\|!0===\2\.runOptions\.directMetaParentChildSubagent)"
)
SUBAGENT_ROUTE_RESTORE_RE = re.compile(
    r"(!0===(\w+)\.runOptions\.excludeWorkspaceContext)"
    + re.escape(SAND_SUBAGENT_ROUTE_MARKER)
    + r"(\|\|!0===\2\.runOptions\.directMetaParentChildSubagent)"
)


def _subagent_route_sub(match: "re.Match[str]") -> str:
    return match.group(1) + SAND_SUBAGENT_ROUTE_MARKER + match.group(3)


def _subagent_route_restore(match: "re.Match[str]") -> str:
    var = match.group(2)
    return (
        match.group(1)
        + f"||void 0!=={var}.runOptions.subagentTypeName"
        + f"||void 0!=={var}.runOptions.parentAgentToolCallId"
        + match.group(3)
    )


# 5c) 动作门控：放行 summarize / resume / backgroundTaskCompletion；
#     mode / simulated 判定用 !1&& 置为死代码：原表达式（含 minified 的枚举路径）原样保留，
#     卸载时据此字节级还原。Multitask、Plan→Build in Parallel 都依赖这两处放行。
_ACTION_ROUTE_ALLOWED_ACTIONS = (
    '["userMessageAction","summarizeAction","resumeAction","backgroundTaskCompletionAction"]'
)
ACTION_ROUTE_RE = re.compile(
    r'return"userMessageAction"!==(\w+)\.actionCase\?"action-not-supported":'
    r'\1\.requestedMode!==([\w$]+\.[\w$]+)\.AGENT\?"mode-not-supported":'
    r'\1\.simulatedUserMessage\?"simulated-message-not-supported":'
)
ACTION_ROUTE_RESTORE_RE = re.compile(
    r"return"
    + re.escape(SAND_ACTION_ROUTE_MARKER)
    + re.escape("!" + _ACTION_ROUTE_ALLOWED_ACTIONS)
    + r'\.includes\((\w+)\.actionCase\)\?"action-not-supported":'
    r'!1&&\1\.requestedMode!==([\w$]+\.[\w$]+)\.AGENT\?"mode-not-supported":'
    r'!1&&\1\.simulatedUserMessage\?"simulated-message-not-supported":'
)
# V1（只放行 AGENT、拒绝 simulated）→ 升级为 V2；卸载时同样还原为官方原文。
ACTION_ROUTE_V1_RE = re.compile(
    r"return"
    + re.escape(SAND_ACTION_ROUTE_V1_MARKER)
    + re.escape("!" + _ACTION_ROUTE_ALLOWED_ACTIONS)
    + r'\.includes\((\w+)\.actionCase\)\?"action-not-supported":'
    r'"userMessageAction"===\1\.actionCase&&\1\.requestedMode!==([\w$]+\.[\w$]+)\.AGENT\?"mode-not-supported":'
    r'"userMessageAction"===\1\.actionCase&&\1\.simulatedUserMessage\?"simulated-message-not-supported":'
)


def _action_route_sub(match: "re.Match[str]") -> str:
    var = match.group(1)
    mode_enum = match.group(2)
    return (
        "return"
        + SAND_ACTION_ROUTE_MARKER
        + "!"
        + _ACTION_ROUTE_ALLOWED_ACTIONS
        + f'.includes({var}.actionCase)?"action-not-supported":'
        + f'!1&&{var}.requestedMode!=={mode_enum}.AGENT?"mode-not-supported":'
        + f'!1&&{var}.simulatedUserMessage?"simulated-message-not-supported":'
    )


def _action_route_restore(match: "re.Match[str]") -> str:
    var = match.group(1)
    mode_enum = match.group(2)
    return (
        f'return"userMessageAction"!=={var}.actionCase?"action-not-supported":'
        f'{var}.requestedMode!=={mode_enum}.AGENT?"mode-not-supported":'
        f'{var}.simulatedUserMessage?"simulated-message-not-supported":'
    )


# 5d) 运行时特性表：追加 useClientSideSubagent:!0（子代理在本机跑）。
SUBAGENT_SESSION_RE = re.compile(
    r"(const \w+=\{enableEmptyResponseRetry:!0,(?:(?!useClientSideSubagent)[^{}])*?)\}"
)
SUBAGENT_SESSION_PATCH = ",useClientSideSubagent:!0" + SAND_SUBAGENT_SESSION_MARKER


def _subagent_session_sub(match: "re.Match[str]") -> str:
    return match.group(1) + SUBAGENT_SESSION_PATCH + "}"


# 5e) taskToolProps：从 void 0 变为可用配置，模型才拿得到 Task 工具。
#     请求 / 解析后模型 / max 取自同一 createAgentConfig 闭包里的 e / i / l。注意 i 是
#     e.resolvedModel.modelId——UI 展示用的复合 slug（claude-fable-5-1-thinking-max），线上协议的
#     requestedModel.modelId 则是基础 ID（claude-fable-5-1）。子代理必须沿用后者，见 V3 模板。
TASK_TOOL_ANCHOR_RE = re.compile(
    r"(generateImage:\w+\(\w+,\{modelId:(\w+),maxMode:(\w+)\}\),"
    r"generateImageSuspiciousKeywords:\[\],imageGenerationConcurrencyLimiter:\w+\(\d+\),"
    r"isGenerateImageModelRestricted:!1,taskToolProps:)void 0\}"
)
TASK_TOOL_CONFIG_FN_RE = re.compile(
    r"createAgentConfig:\w+=>function\((\w+),\w+\)\{var [\w,]+;const \w+=\1\.resolvedModelMetadata"
)
# 旧版（V1 / V2）注入体：V2（≤1.1.10 / Toolkit 1.2.6）、V1（Toolkit 1.2.4/1.2.5：可能没有
# subagentTypeName 短路、modelsBySlug 为空 Map、normalizeCustomSubagents 为 e=>e）。
# 末尾的 \} 只闭合配置对象本身，供下面三条正则共用。
_TASK_TOOL_LEGACY_BODY = (
    r"/\*SAND_MANAGED_TASK_TOOL_V[12]\*/"
    r"parentRequestedModelName:\w+,parentModelParameters:\w+\.requestedModel\.parameters,"
    r"parentMaxMode:\w+,isModelBlocked:\(\)=>!1,isModelValid:\w+=>\w+===\w+,"
    r'requiresMaxMode:\(\)=>!1,compareModelCosts:\(\)=>0,subagentModelForcePolicy:"none",'
    r"requireServerSideSubagent:!1,"
    r"subagentModels:\{modelsBySlug:new Map(?:\(\[\[\w+,\{slug:\w+\}\]\]\))?\},"
    r"normalizeCustomSubagents:(?:\(\)=>\[\]|\w+=>\w+),getTaskToolConfig:async\(\)=>\(\{\}\)\}"
)
# 完整的旧版注入（锚点处），还原为官方原文 `taskToolProps:void 0}`。
TASK_TOOL_LEGACY_RESTORE_RE = re.compile(
    r"(isGenerateImageModelRestricted:!1,taskToolProps:)"
    r"(?:void 0!==\w+\.runOptions\.subagentTypeName\?void 0:)?\{"
    + _TASK_TOOL_LEGACY_BODY
    + r"\}"
)
# 被其他工具截断后的旧版残尾。Toolkit 的注入正则把旧版注入开头的 `taskToolProps:void 0` 当成
# 官方原文替换成了自己的 `SAND_CHILD?void 0:{...}`，留下
#   `{Toolkit 对象}!==e.runOptions.subagentTypeName?void 0:{/*V2*/...}`
# 三元表达式因此恒为 void 0——父对话拿不到任何 Task 工具配置。install / uninstall 都先摘掉它。
TASK_TOOL_DANGLING_RE = re.compile(
    r"!==\w+\.runOptions\.subagentTypeName\?void 0:\{" + _TASK_TOOL_LEGACY_BODY
)
# V3 注入：子代理模型沿用父请求的 requestedModel.modelId（服务端认的基础 ID），thinking /
# effort / max 仍由 parentModelParameters / parentMaxMode 单独携带；解析后的复合 slug 只作兜底。
# 以 `null!=` 开头而不是 `void 0!==`，避免其他工具的 `taskToolProps:void 0` 前缀匹配再次把它截断。
# 占位符：@REQ@ 请求变量、@MODEL@ 解析后的模型变量、@MAX@ maxMode 变量（均为 minified 名）。
_TASK_TOOL_PARENT_MODEL_VAR = "__sandParentModel"
_TASK_TOOL_V3_TEMPLATE = (
    "null!=@REQ@.runOptions.subagentTypeName?void 0:("
    + _TASK_TOOL_PARENT_MODEL_VAR
    + "=>({"
    + SAND_TASK_TOOL_MARKER
    + "parentRequestedModelName:" + _TASK_TOOL_PARENT_MODEL_VAR + ","
    "parentModelParameters:@REQ@.requestedModel.parameters,"
    "parentMaxMode:@MAX@,"
    "isModelBlocked:()=>!1,"
    "isModelValid:e=>e===" + _TASK_TOOL_PARENT_MODEL_VAR + ","
    "requiresMaxMode:()=>!1,"
    "compareModelCosts:()=>0,"
    'subagentModelForcePolicy:"none",'
    "requireServerSideSubagent:!1,"
    "subagentModels:{modelsBySlug:new Map([["
    + _TASK_TOOL_PARENT_MODEL_VAR + ",{slug:" + _TASK_TOOL_PARENT_MODEL_VAR + "}]])},"
    "normalizeCustomSubagents:()=>[],"
    "getTaskToolConfig:async()=>({})}))(@REQ@.requestedModel.modelId||@MODEL@)}"
)
_TASK_TOOL_V3_PLACEHOLDERS = ("@REQ@", "@MODEL@", "@MAX@")


def _template_regex(template: str, placeholders: Iterable[str], token: str = r"[\w$]+") -> str:
    """把字面量模板转成正则：占位符位置放 token，其余逐字转义。"""
    names = tuple(placeholders)
    splitter = re.compile("|".join(re.escape(name) for name in names))
    parts: List[str] = []
    pos = 0
    for match in splitter.finditer(template):
        parts.append(re.escape(template[pos : match.start()]))
        parts.append(token)
        pos = match.end()
    parts.append(re.escape(template[pos:]))
    return "".join(parts)


# 模板末尾的 "}" 同时闭合外层 tools:{...}，还原时统一写回 `\1void 0}`。
TASK_TOOL_V3_RESTORE_RE = re.compile(
    r"(isGenerateImageModelRestricted:!1,taskToolProps:)"
    + _template_regex(_TASK_TOOL_V3_TEMPLATE, _TASK_TOOL_V3_PLACEHOLDERS)
)


def _task_tool_props_js(req: str, model: str, max_mode: str) -> str:
    return (
        _TASK_TOOL_V3_TEMPLATE.replace("@REQ@", req)
        .replace("@MODEL@", model)
        .replace("@MAX@", max_mode)
    )


def _restore_task_tool_props(content: str) -> Tuple[str, int]:
    """把本工具任一版本的 Task 工具注入还原为官方原文；其他工具的注入原样保留。

    返回 (新内容, 处理数)。悬垂残尾单独摘除：它后面紧跟的是其他工具的注入，不能写回 void 0。
    """
    total = 0
    content, n = TASK_TOOL_V3_RESTORE_RE.subn(r"\1void 0}", content)
    total += n
    content, n = TASK_TOOL_LEGACY_RESTORE_RE.subn(r"\1void 0}", content)
    total += n
    content, n = TASK_TOOL_DANGLING_RE.subn("", content)
    total += n
    return content, total


def _apply_task_tool_props(content: str) -> Tuple[str, int]:
    # 其他 Sand 工具已经提供了 Task 工具配置：不叠加，否则两段注入拼在一起整个表达式失效。
    if FOREIGN_TASK_TOOL_PROPS_RE.search(content):
        return content, 0
    match = TASK_TOOL_ANCHOR_RE.search(content)
    if match is None:
        return content, 0
    # 只在锚点之前、同一 createAgentConfig 闭包内取请求变量名（最近的一处）。
    window_start = max(0, match.start() - 20000)
    fn_matches = list(TASK_TOOL_CONFIG_FN_RE.finditer(content, window_start, match.start()))
    if not fn_matches:
        return content, 0
    req = fn_matches[-1].group(1)
    # _task_tool_props_js 末尾的 "}" 闭合外层 tools:{...}（配置对象已在 IIFE 内闭合）。
    replacement = match.group(1) + _task_tool_props_js(req, match.group(2), match.group(3))
    return content[: match.start()] + replacement + content[match.end():], 1


# 5f) 渲染层唤醒（workbench desktop/glass）：在 interactive-child 判定前补上
#     source==="subagent"，绕过 long_running_jobs 门控；变量名按构建捕获。
SUBAGENT_WAKE_RE = re.compile(
    r'([A-Za-z_$][\w$]*)\.source==="interactive-child"\|\|'
    r'\1\.payload\.notificationContext==="user_driven_interactive_child"'
)
SUBAGENT_WAKE_RESTORE_RE = re.compile(
    r'([A-Za-z_$][\w$]*)\.source==="subagent"'
    + re.escape(SAND_SUBAGENT_WAKE_MARKER)
    + r'\|\|\1\.source==="interactive-child"\|\|'
    r'\1\.payload\.notificationContext==="user_driven_interactive_child"'
)


def _subagent_wake_sub(match: "re.Match[str]") -> str:
    var = match.group(1)
    return f'{var}.source==="subagent"' + SAND_SUBAGENT_WAKE_MARKER + "||" + match.group(0)


def _subagent_wake_restore(match: "re.Match[str]") -> str:
    var = match.group(1)
    return (
        f'{var}.source==="interactive-child"||'
        f'{var}.payload.notificationContext==="user_driven_interactive_child"'
    )


def _apply_subagent_wake(content: str) -> Tuple[str, int]:
    """返回 (新内容, 新增 marker 数)。已打补丁的锚点先还原再重打，保证幂等。"""
    before = content.count(SAND_SUBAGENT_WAKE_MARKER)
    content = SUBAGENT_WAKE_RESTORE_RE.sub(_subagent_wake_restore, content)
    content = SUBAGENT_WAKE_RE.sub(_subagent_wake_sub, content)
    return content, max(0, content.count(SAND_SUBAGENT_WAKE_MARKER) - before)


def _subagent_wake_anchor_count(content: str) -> int:
    """渲染层唤醒锚点总数（原始 + 已打补丁）。"""
    normalized = SUBAGENT_WAKE_RESTORE_RE.sub(_subagent_wake_restore, content)
    return len(SUBAGENT_WAKE_RE.findall(normalized))


def _apply_subagent_patches(content: str) -> Tuple[str, int]:
    total = 0
    content, n = SUBAGENT_RESUME_MODE_RE.subn(_subagent_resume_mode_sub, content)
    total += n
    content, n = SUBAGENT_ROUTE_RE.subn(_subagent_route_sub, content)
    total += n
    # 旧 V1 门控先还原为官方原文，再统一打成 V2。
    content, n = ACTION_ROUTE_V1_RE.subn(_action_route_restore, content)
    content, n = ACTION_ROUTE_RE.subn(_action_route_sub, content)
    total += n
    content, n = SUBAGENT_SESSION_RE.subn(_subagent_session_sub, content, count=1)
    total += n
    # 旧形态先还原再按当前规则重打，统一到唯一形态：完整的 V1 / V2 精确还原为官方原文
    # （随后重打 V3 计为升级）；剩下的才是被其他工具截断的残尾，摘掉它让其他工具的注入
    # 重新生效，单独计为一次修复。
    had_task_tool = SAND_TASK_TOOL_MARKER in content
    content, _ = TASK_TOOL_LEGACY_RESTORE_RE.subn(r"\1void 0}", content)
    content, n = TASK_TOOL_DANGLING_RE.subn("", content)
    total += n
    content, _ = TASK_TOOL_V3_RESTORE_RE.subn(r"\1void 0}", content)
    content, n = _apply_task_tool_props(content)
    total += 0 if had_task_tool else n
    return content, total


def _subagent_readiness(contents: Iterable[str]) -> Dict[str, int]:
    """统计子代理运行链就绪锚点在所有目标文件里的命中数。"""
    texts = tuple(contents)
    return {
        name: sum(len(pattern.findall(text)) for text in texts)
        for name, pattern in SUBAGENT_READY_ANCHORS
    }


def _remove_subagent_patches(content: str) -> Tuple[str, int]:
    total = 0
    content, n = SUBAGENT_RESUME_MODE_RESTORE_RE.subn(r"?\1.UNSPECIFIED:", content)
    total += n
    content, n = SUBAGENT_ROUTE_RESTORE_RE.subn(_subagent_route_restore, content)
    total += n
    content, n = ACTION_ROUTE_RESTORE_RE.subn(_action_route_restore, content)
    total += n
    content, n = ACTION_ROUTE_V1_RE.subn(_action_route_restore, content)
    total += n
    n = content.count(SUBAGENT_SESSION_PATCH)
    if n:
        content = content.replace(SUBAGENT_SESSION_PATCH, "")
        total += n
    content, n = _restore_task_tool_props(content)
    total += n
    return content, total


def _remove_subagent_wake(content: str) -> Tuple[str, int]:
    return SUBAGENT_WAKE_RESTORE_RE.subn(_subagent_wake_restore, content)


def _subagent_marker_count(content: str) -> int:
    return sum(content.count(marker) for marker in SUBAGENT_MARKERS)


def _legacy_subagent_marker_count(content: str) -> int:
    return sum(content.count(marker) for marker in LEGACY_SUBAGENT_MARKERS)


def _joe_stream_session_js() -> str:
    """Joe/Stream 会话体。仅 Stream-only client 使用；带 runInference 的走原函数。"""
    return (
        'const n=t.requestedModel;'
        'if(void 0===n)throw new Error("Sand direct Stream requires requestedModel");'
        'const o=String(n.modelId||""),i=o.toLowerCase(),'
        'r=new Map((n.parameters||[]).map(e=>[e.id,e.value])),'
        's=new Joe(e,n,void 0,void 0).getSession(),'
        'p={getExecutor:e=>new RK(s.getExecutor(e))},'
        'a={vendor:i.includes("grok")?"xai":i.includes("gemini")?"gemini":'
        'i.includes("claude")||i.includes("opus")||i.includes("sonnet")||i.includes("fable")?'
        '"anthropic":i.includes("gpt")||i.includes("codex")?"openai":"unknown",'
        'promptVersion:"latest",reasoningEffort:r.get("effort"),'
        'isGrok45ProductPrompt:i.includes("grok"),'
        'isClaude4x:i.includes("claude")||i.includes("opus")||i.includes("sonnet")||i.includes("fable"),'
        'isFable5:i.includes("fable-5"),'
        'isOpus5:i.includes("opus-5")||i.includes("opus5"),'
        'isOpus48:i.includes("opus-4.8")||i.includes("opus48"),'
        'isOpus46:i.includes("opus-4.6")||i.includes("opus46"),'
        'isOpus45:i.includes("opus-4.5")||i.includes("opus45"),'
        'isSonnet45:i.includes("sonnet-4.5")||i.includes("sonnet45"),'
        'isSonnet4:i.includes("sonnet-4")||i.includes("sonnet4"),'
        'isGemini3:i.includes("gemini-3")||i.includes("gemini3"),'
        'isGpt56:i.includes("gpt-5.6")||i.includes("gpt5.6"),'
        'isGpt55:i.includes("gpt-5.5")||i.includes("gpt5.5"),'
        'isGpt54:i.includes("gpt-5.4")||i.includes("gpt5.4"),'
        'isGpt53Codex:i.includes("gpt-5.3-codex"),'
        'isGpt52Codex:i.includes("gpt-5.2-codex"),'
        'isCodexFamily:i.includes("codex"),isGpt5Family:i.includes("gpt-5")};'
        'return{promptSession:s,promptToolSession:p,attempt:{resolvedModel:cre(n),'
        'supportsSelfSummary:!1,routedModelDisplayName:o,'
        'resolvedModelMetadata:nre(a,o),finish:()=>Promise.resolve()}}'
    )


def _legacy_direct_stream_injection() -> str:
    """1.1.0 / 桌面 1.2.2 字面量：无条件 return Joe/Stream，会把本地工具链截断。"""
    return (
        "{"
        + SAND_DIRECT_STREAM_MARKER
        + 'const n=t.requestedModel;'
        'if(void 0===n)throw new Error("Sand direct Stream requires requestedModel");'
        'const o=String(n.modelId||""),i=o.toLowerCase(),'
        'r=new Map(n.parameters.map(e=>[e.id,e.value])),'
        's=new Joe(e,n,void 0,void 0).getSession(),'
        'p={getExecutor:e=>new RK(s.getExecutor(e))},'
        'a={vendor:i.includes("grok")?"xai":i.includes("gemini")?"gemini":'
        'i.includes("claude")||i.includes("opus")||i.includes("sonnet")||i.includes("fable")?'
        '"anthropic":i.includes("gpt")||i.includes("codex")?"openai":"unknown",'
        'promptVersion:"latest",reasoningEffort:r.get("effort"),'
        'isGrok45ProductPrompt:i.includes("grok"),'
        'isClaude4x:i.includes("claude")||i.includes("opus")||i.includes("sonnet")||i.includes("fable"),'
        'isFable5:i.includes("fable-5"),'
        'isOpus5:i.includes("opus-5")||i.includes("opus5"),'
        'isOpus48:i.includes("opus-4.8")||i.includes("opus48"),'
        'isOpus46:i.includes("opus-4.6")||i.includes("opus46"),'
        'isOpus45:i.includes("opus-4.5")||i.includes("opus45"),'
        'isSonnet45:i.includes("sonnet-4.5")||i.includes("sonnet45"),'
        'isSonnet4:i.includes("sonnet-4")||i.includes("sonnet4"),'
        'isGemini3:i.includes("gemini-3")||i.includes("gemini3"),'
        'isGpt56:i.includes("gpt-5.6")||i.includes("gpt5.6"),'
        'isGpt55:i.includes("gpt-5.5")||i.includes("gpt5.5"),'
        'isGpt54:i.includes("gpt-5.4")||i.includes("gpt5.4"),'
        'isGpt53Codex:i.includes("gpt-5.3-codex"),'
        'isGpt52Codex:i.includes("gpt-5.2-codex"),'
        'isCodexFamily:i.includes("codex"),isGpt5Family:i.includes("gpt-5")};'
        'return{promptSession:s,promptToolSession:p,attempt:{resolvedModel:cre(n),'
        'supportsSelfSummary:!1,routedModelDisplayName:o,'
        'resolvedModelMetadata:nre(a,o),finish:()=>Promise.resolve()}}}'
    )


def _direct_stream_injection() -> str:
    return (
        "{"
        + SAND_DIRECT_STREAM_MARKER
        + 'if(!(e&&typeof e.runInference==="function")){'
        + _joe_stream_session_js()
        + "}}"
    )


DIRECT_STREAM_SNIPPET_RE = re.compile(
    re.escape("{")
    + re.escape(SAND_DIRECT_STREAM_MARKER)
    + r"[\s\S]*?finish:\(\)=>Promise\.resolve\(\)\}+"
)


def _strip_direct_stream_injection(content: str) -> Tuple[str, int]:
    """卸掉 1.1.0–1.1.3 对 createPromptSession 的短路，恢复官方 runInference。"""
    if SAND_DIRECT_STREAM_MARKER not in content:
        return content, 0
    total = 0
    for exact in (_direct_stream_injection(), _legacy_direct_stream_injection()):
        count = content.count(exact)
        if count:
            content = content.replace(exact, "")
            total += count
    if SAND_DIRECT_STREAM_MARKER in content:
        content, n = DIRECT_STREAM_SNIPPET_RE.subn("", content)
        total += n
    return content, total


def _content_has_stream_anchors(content: str) -> bool:
    return (
        MANAGED_LOCAL_ROUTE_RE.search(content) is not None
        or LOCAL_RUNTIME_LOAD_RE.search(content) is not None
        or AGENT_HOST_IDENTITY_ORIGINAL in content
        or DIRECT_STREAM_ANCHOR in content
        or MOVE_EXEC_GATE_RE.search(content) is not None
        or AGENT_HOST_ENABLEMENT_RE.search(content) is not None
        or any(marker in content for marker in LEGACY_MOVE_EXEC_MARKERS)
    )


def _move_exec_marker_count(content: str) -> int:
    """旧版（≤1.1.9）强制 move_exec 的残留 marker 数，含 1.1.8 之前的标记名。"""
    return sum(content.count(marker) for marker in MOVE_EXEC_MARKERS)


def _platform_name() -> str:
    if sys.platform == "win32":
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    if sys.platform.startswith("linux"):
        return "linux"
    raise SandToolError("当前仅支持 Windows、macOS 和 Linux")


def _enable_windows_ansi() -> bool:
    if sys.platform != "win32":
        return True
    try:
        kernel32 = ctypes.windll.kernel32
        for handle_id in (-11, -12):
            handle = kernel32.GetStdHandle(handle_id)
            if handle in (0, -1):
                continue
            mode = ctypes.c_uint32()
            if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
                continue
            kernel32.SetConsoleMode(handle, mode.value | 0x0004)
        return True
    except Exception:
        return False


def _configure_console() -> None:
    global _COLOR_ENABLED
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass
    if os.environ.get("NO_COLOR"):
        _COLOR_ENABLED = False
        return
    _COLOR_ENABLED = _enable_windows_ansi() and sys.stdout.isatty()


def colorize(text: str, *codes: str) -> str:
    if not _COLOR_ENABLED or not codes:
        return text
    return "".join(codes) + text + ANSI_RESET


def print_warn(text: str) -> None:
    print(colorize(text, ANSI_YELLOW))


def print_error(text: str) -> None:
    print(colorize(text, ANSI_RED), file=sys.stderr)


class LoadingSpinner:
    def __init__(self, message: str = "处理中") -> None:
        self.message = message
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def __enter__(self) -> "LoadingSpinner":
        if sys.stdout.isatty():
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()
        else:
            print(colorize(self.message + "...", ANSI_BLUE), flush=True)
        return self

    def __exit__(self, *_exc: object) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join()
            print("\r" + " " * 48 + "\r", end="", flush=True)

    def _run(self) -> None:
        frames = ("|", "/", "-", "\\")
        index = 0
        while not self._stop.wait(0.1):
            text = f"{frames[index % 4]} {self.message}"
            print("\r" + colorize(text, ANSI_BLUE), end="", flush=True)
            index += 1


def _linux_invoking_account():
    """Resolve the desktop account that invoked sudo/pkexec, if any."""
    if not sys.platform.startswith("linux"):
        return None
    try:
        import pwd

        for variable in ("SUDO_UID", "PKEXEC_UID"):
            raw_uid = os.environ.get(variable, "").strip()
            if raw_uid.isdigit() and int(raw_uid) != 0:
                return pwd.getpwuid(int(raw_uid))
        sudo_user = os.environ.get("SUDO_USER", "").strip()
        if sudo_user and sudo_user != "root":
            return pwd.getpwnam(sudo_user)
    except (ImportError, KeyError, OSError, ValueError):
        return None
    return None


def _config_dir() -> Path:
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
        if base:
            return Path(base) / "SandClientMode" / "sand-client-cli"
        return Path.home() / "AppData" / "Local" / "SandClientMode" / "sand-client-cli"
    if sys.platform == "darwin":
        return (
            Path.home()
            / "Library"
            / "Application Support"
            / "SandClientMode"
            / "sand-client-cli"
        )
    home = Path.home()
    if sys.platform.startswith("linux"):
        account = _linux_invoking_account() if getattr(os, "geteuid", lambda: 1)() == 0 else None
        if account is not None:
            home = Path(account.pw_dir)
            # sudo -H changes HOME to /root; the original user's config must remain discoverable.
            return home / ".config" / "SandClientMode" / "sand-client-cli"
        xdg_config = os.environ.get("XDG_CONFIG_HOME", "").strip()
        if xdg_config:
            xdg_path = Path(xdg_config).expanduser()
            if xdg_path.is_absolute():
                return xdg_path / "SandClientMode" / "sand-client-cli"
    return home / ".config" / "SandClientMode" / "sand-client-cli"


def _config_path() -> Path:
    return _config_dir() / "config.json"


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _path_key(path: Path) -> str:
    try:
        normalized = str(path.resolve())
    except (OSError, RuntimeError, ValueError):
        normalized = os.path.abspath(os.fspath(path))
    return os.path.normcase(normalized)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _product_checksum(data: bytes) -> str:
    digest = hashlib.sha256(data).digest()
    return base64.b64encode(digest).decode("ascii").rstrip("=")


def _atomic_write(path: Path, data: bytes, mode: Optional[int] = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.parent / (
        f".{path.name}.sand-client-{os.getpid()}-{time.time_ns()}.tmp"
    )
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    fd: Optional[int] = None
    try:
        fd = os.open(str(temp), flags, 0o600)
        with os.fdopen(fd, "wb", closefd=True) as handle:
            fd = None
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if mode is not None:
            os.chmod(temp, stat.S_IMODE(mode))
        try:
            os.replace(temp, path)
        except PermissionError:
            original_mode: Optional[int] = None
            if path.exists():
                original_mode = stat.S_IMODE(path.stat().st_mode)
                os.chmod(path, original_mode | stat.S_IWRITE)
            try:
                os.replace(temp, path)
            except BaseException:
                if original_mode is not None and path.exists():
                    try:
                        os.chmod(path, original_mode)
                    except OSError:
                        pass
                raise
        if mode is not None:
            os.chmod(path, stat.S_IMODE(mode))
    finally:
        if fd is not None:
            os.close(fd)
        try:
            if temp.exists():
                temp.unlink()
        except OSError:
            pass


def _write_json_atomic(path: Path, value: Mapping[str, object]) -> None:
    data = (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    _atomic_write(path, data, 0o600)


def _load_config() -> Mapping[str, object]:
    path = _config_path()
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise SandToolError(
            f"配置文件损坏：{path}\n请运行 set-path auto 后重新检测"
        ) from exc
    if not isinstance(value, dict) or value.get("version") != CONFIG_VERSION:
        raise SandToolError(
            f"不支持的配置文件：{path}\n请运行 set-path auto 后重新检测"
        )
    return value


def _read_product(product_path: Path) -> Mapping[str, object]:
    try:
        size = product_path.stat().st_size
        if size <= 0 or size > 1024 * 1024:
            raise SandToolError(f"product.json 大小异常：{product_path}")
        raw = product_path.read_bytes()
        value = json.loads(raw.decode("utf-8-sig"))
    except SandToolError:
        raise
    except Exception as exc:
        raise SandToolError(f"无法读取 Cursor product.json：{product_path}") from exc
    if not isinstance(value, dict):
        raise SandToolError(f"Cursor product.json 格式错误：{product_path}")
    name = str(value.get("applicationName") or value.get("nameShort") or "")
    if name.casefold() != "cursor":
        raise SandToolError(f"所选目录不是 Cursor 安装：{product_path}")
    return value


def _find_app_bundle(app_root: Path) -> Optional[Path]:
    for item in (app_root, *app_root.parents):
        if item.name.casefold() == "cursor.app":
            return item
    return None


def _linux_launcher_references(path: Path) -> Iterable[Path]:
    """从 Linux 启动脚本（如发行版打包的 /usr/bin/cursor）里提取可能的 app 路径。"""
    try:
        if path.stat().st_size > 256 * 1024:
            return
        text = path.read_text(encoding="utf-8", errors="replace")
    except (OSError, UnicodeError):
        return
    # 刻意收窄：只有提到 cursor 或 Electron resources 目录的路径才值得拿去校验。
    patterns = (
        re.compile(r'"(/[^"\r\n]+)"'),
        re.compile(r"'(/[^'\r\n]+)'"),
        re.compile(r"(?<![A-Za-z0-9_])(/[^\s\"'`;&|]+)"),
    )
    seen: Set[str] = set()
    for line in text.splitlines():
        if line.lstrip().startswith("#"):
            continue
        for pattern in patterns:
            for match in pattern.finditer(line):
                value = match.group(1).rstrip(".,:;)]}")
                is_assignment = bool(
                    re.search(
                        r"(?:^|[\s;])[A-Za-z_][A-Za-z0-9_]*=[\"']?$",
                        line[: match.start()],
                    )
                )
                if (
                    "cursor" not in value.casefold()
                    and "resources/app" not in value.casefold()
                    and not is_assignment
                ):
                    continue
                candidate = Path(value)
                key = _path_key(candidate)
                if key in seen:
                    continue
                seen.add(key)
                yield candidate


def _linux_launcher_targets(launcher: Path, app_root: Path, install_root: Path) -> bool:
    """安装目录之外的启动脚本是否明确指向这份 Cursor。"""
    app_root = app_root.resolve()
    install_root = install_root.resolve()
    for reference in _linux_launcher_references(launcher):
        try:
            resolved = reference.resolve(strict=False)
        except (OSError, RuntimeError, ValueError):
            continue
        if resolved == app_root or _is_within(resolved, app_root) or resolved == install_root:
            return True
    return False


def _candidate_app_roots(raw_path: Path) -> Iterable[Path]:
    path = raw_path
    launcher_path = path if path.is_file() else None
    if path.is_file():
        if path.name.casefold() == "product.json":
            path = path.parent
        else:
            path = path.parent
    current = path
    for _ in range(8):
        yield current
        yield current / "resources" / "app"
        yield current / "Resources" / "app"
        yield current / "Contents" / "Resources" / "app"
        if sys.platform.startswith("linux") and launcher_path is None and current == path:
            # 解压后的 AppImage（squashfs-root）与发行版包会把 app 放在嵌套的文件系统根下。
            for relative in (
                ("usr", "share", "cursor", "resources", "app"),
                ("usr", "share", "Cursor", "resources", "app"),
                ("usr", "lib", "cursor", "resources", "app"),
                ("usr", "lib", "Cursor", "resources", "app"),
                ("opt", "Cursor", "resources", "app"),
                ("opt", "cursor", "resources", "app"),
            ):
                yield current.joinpath(*relative)
        if current.parent == current:
            break
        current = current.parent
    if launcher_path is not None and sys.platform.startswith("linux"):
        for reference in _linux_launcher_references(launcher_path):
            current_reference = reference
            for _ in range(8):
                yield current_reference
                yield current_reference / "resources" / "app"
                if current_reference.parent == current_reference:
                    break
                current_reference = current_reference.parent


def _linux_install_root(app_root: Path) -> Path:
    """包含 Linux Cursor `resources/app` 的安装根目录。"""
    for parent in (app_root, *app_root.parents):
        if parent.name.casefold() == "resources" and parent.parent != parent:
            return parent.parent
    return app_root


def _resolve_executable(
    app_root: Path, preferred_executable: Optional[Path] = None
) -> Tuple[Path, Path]:
    if sys.platform == "win32":
        if app_root.parent.name.casefold() == "resources":
            install_root = app_root.parent.parent
        else:
            install_root = app_root
        candidates: Tuple[Path, ...] = (
            install_root / "Cursor.exe",
            install_root / "cursor.exe",
        )
    elif sys.platform == "darwin":
        bundle = _find_app_bundle(app_root)
        if bundle is None:
            raise SandToolError("macOS Cursor 路径必须位于 Cursor.app 内")
        install_root = bundle
        candidates = (bundle / "Contents" / "MacOS" / "Cursor",)
    elif sys.platform.startswith("linux"):
        install_root = _linux_install_root(app_root)
        # 官方 .deb/tar 把 Electron 可执行文件放在 resources 旁边；部分重打包放到 bin/；
        # Remote SSH 服务端是 ~/.cursor-server/bin/linux-x64/<commit>/bin/cursor-server。
        candidate_roots = (
            install_root,
            install_root / "bin",
            install_root / "app",
            install_root / "usr" / "bin",
            install_root / "usr" / "lib" / "cursor",
            install_root / "usr" / "share" / "cursor",
            app_root.parent,
            app_root.parent / "bin",
        )
        names = (
            "cursor",
            "Cursor",
            "cursor-server",
            "cursor-bin",
            "Cursor-bin",
            "AppRun",
            "cursor.sh",
            "Cursor.sh",
        )
        linux_candidates: List[Path] = []
        if preferred_executable is not None:
            linux_candidates.append(preferred_executable)
        linux_candidates.extend(root / name for root in candidate_roots for name in names)
        linux_candidates.extend((Path("/usr/bin/cursor"), Path("/usr/local/bin/cursor")))
        path_cursor = shutil.which("cursor")
        if path_cursor:
            linux_candidates.append(Path(path_cursor))
        candidates = tuple(linux_candidates)
    else:
        raise SandToolError("当前仅支持 Windows、macOS 和 Linux")

    seen: Set[str] = set()
    for executable in candidates:
        key = _path_key(executable)
        if key in seen:
            continue
        seen.add(key)
        try:
            resolved = executable.resolve(strict=True)
        except (FileNotFoundError, OSError, ValueError):
            continue
        if not resolved.is_file():
            continue
        if sys.platform.startswith("linux") and not os.access(resolved, os.X_OK):
            continue
        if _is_within(resolved, install_root.resolve()):
            return install_root.resolve(), resolved
        # 发行版启动脚本（如 AUR 包的 /usr/bin/cursor）在安装目录之外 exec 共享 Electron，
        # 只要脚本里明确写着这份 app 的路径就认它。
        if sys.platform.startswith("linux") and _linux_launcher_targets(
            executable, app_root, install_root
        ):
            return install_root.resolve(), resolved
    raise SandToolError(f"未找到 Cursor 可执行文件：{install_root}")


def layout_from_path(value: Union[str, Path]) -> CursorLayout:
    raw_text = str(value).strip().strip('"')
    if not raw_text:
        raise SandToolError("Cursor 路径不能为空")
    if sys.platform == "win32" and (
        raw_text.startswith("\\\\") or raw_text.startswith("\\\\?\\")
    ):
        raise SandToolError("不支持 UNC 或 Windows 设备路径")

    raw = Path(raw_text).expanduser()
    if not raw.is_absolute():
        raise SandToolError(f"Cursor 路径必须是绝对路径：{raw}")
    try:
        raw = raw.resolve(strict=True)
    except (FileNotFoundError, OSError, ValueError) as exc:
        raise SandToolError(f"Cursor 路径不存在：{raw}") from exc

    seen: Set[str] = set()
    last_error: Optional[Exception] = None
    for candidate in _candidate_app_roots(raw):
        try:
            app_root = candidate.resolve(strict=True)
        except (FileNotFoundError, OSError, ValueError):
            continue
        key = _path_key(app_root)
        if key in seen:
            continue
        seen.add(key)

        product_json = app_root / "product.json"
        if not product_json.is_file():
            continue
        try:
            product_real = product_json.resolve(strict=True)
            if not _is_within(product_real, app_root):
                raise SandToolError("product.json 符号链接逃逸出 Cursor app 目录")
            product = _read_product(product_real)
            # 用户直接给的是可执行文件/启动脚本时优先采用它（如 Flatpak/tar 的自定义启动器）。
            preferred_executable = (
                raw if raw.is_file() and os.access(raw, os.X_OK) else None
            )
            install_root, executable = _resolve_executable(
                app_root, preferred_executable=preferred_executable
            )

            targets: List[Path] = []
            for rel, _extension_name in TARGET_SPECS:
                target = app_root.joinpath(*rel.split("/"))
                if not target.is_file():
                    continue
                target_real = target.resolve(strict=True)
                if not _is_within(target_real, app_root):
                    raise SandToolError(f"目标文件符号链接逃逸：{target}")
                targets.append(target_real)

            # 动态纳入 agent-host dist 下的所有 chunk（路由/Stream 逻辑所在编号随构建变化）。
            seen_targets = {_path_key(t) for t in targets}
            dist_dir = app_root.joinpath(*AGENT_HOST_DIST_REL.split("/"))
            if dist_dir.is_dir():
                for chunk in sorted(dist_dir.glob("*.js")):
                    if chunk.name == "main.js" or chunk.name.endswith("-worker.js"):
                        continue
                    if not chunk.is_file():
                        continue
                    try:
                        chunk_real = chunk.resolve(strict=True)
                    except (FileNotFoundError, OSError):
                        continue
                    if not _is_within(chunk_real, app_root):
                        continue
                    if _path_key(chunk_real) in seen_targets:
                        continue
                    seen_targets.add(_path_key(chunk_real))
                    targets.append(chunk_real)

            if not targets:
                raise SandToolError(
                    "Cursor 使用 app.asar 或当前版本没有可识别的 Sand 目标文件"
                )

            ext_host = app_root.joinpath(*EXT_HOST_REL.split("/"))
            ext_host_real = ext_host.resolve(strict=True) if ext_host.is_file() else None
            version = str(product.get("version") or product.get("commit") or "未知")
            return CursorLayout(
                install_root=install_root,
                app_root=app_root,
                product_json=product_real,
                executable=executable,
                target_paths=tuple(targets),
                ext_host_path=ext_host_real,
                version=version,
                is_remote_server=executable.name == "cursor-server",
            )
        except SandToolError as exc:
            last_error = exc
            continue

    if last_error:
        raise SandToolError(f"Cursor 路径校验失败：{last_error}") from last_error
    if (
        sys.platform.startswith("linux")
        and raw.is_file()
        and raw.suffix.casefold() == ".appimage"
    ):
        raise SandToolError(
            "AppImage 是只读压缩镜像，无法直接修改；请先运行 "
            "`./Cursor*.AppImage --appimage-extract`，再把路径设为解压出的 squashfs-root 目录"
        )
    raise SandToolError(f"路径中未找到 Cursor resources/app：{raw}")


def _powershell_executable() -> Optional[str]:
    return shutil.which("powershell.exe") or shutil.which("powershell") or shutil.which("pwsh")


def _windows_running_candidates() -> List[str]:
    powershell = _powershell_executable()
    if not powershell:
        return []
    script = (
        "$ErrorActionPreference='SilentlyContinue';"
        "[Console]::OutputEncoding=[System.Text.UTF8Encoding]::new();"
        "Get-CimInstance Win32_Process -Filter \"Name='Cursor.exe'\" | "
        "ForEach-Object { if ($_.ExecutablePath) { $_.ExecutablePath } }"
    )
    try:
        result = subprocess.run(
            [powershell, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if result.returncode != 0:
        return []
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def _windows_registry_candidates() -> List[str]:
    if sys.platform != "win32":
        return []
    try:
        import winreg
    except ImportError:
        return []

    candidates: List[str] = []
    roots = (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE)
    views = (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY)
    uninstall = r"Software\Microsoft\Windows\CurrentVersion\Uninstall"
    for root in roots:
        for view in views:
            try:
                parent = winreg.OpenKey(root, uninstall, 0, winreg.KEY_READ | view)
            except OSError:
                continue
            with parent:
                index = 0
                while True:
                    try:
                        name = winreg.EnumKey(parent, index)
                    except OSError:
                        break
                    index += 1
                    try:
                        child = winreg.OpenKey(parent, name)
                    except OSError:
                        continue
                    with child:
                        def read(name_: str) -> str:
                            try:
                                return str(winreg.QueryValueEx(child, name_)[0] or "")
                            except OSError:
                                return ""

                        display_name = read("DisplayName").strip()
                        publisher = read("Publisher").strip()
                        if display_name.casefold() != "cursor" and "anysphere" not in publisher.casefold():
                            continue
                        install_location = read("InstallLocation").strip().strip('"')
                        display_icon = read("DisplayIcon").strip().strip('"')
                        if install_location:
                            candidates.append(install_location)
                        if display_icon:
                            icon_path = re.sub(r",\s*-?\d+$", "", display_icon).strip('"')
                            candidates.append(icon_path)
    return candidates


def _mac_process_paths(strict: bool = False) -> List[Tuple[int, Path]]:
    try:
        libproc = ctypes.CDLL("/usr/lib/libproc.dylib")
        proc_pidpath = libproc.proc_pidpath
        proc_pidpath.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
        proc_pidpath.restype = ctypes.c_int
        result = subprocess.run(
            ["ps", "-axo", "pid="],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        if strict:
            raise SandToolError("无法读取 macOS 进程可执行路径") from exc
        return []
    if result.returncode != 0:
        if strict:
            raise SandToolError("无法读取 macOS 进程可执行路径")
        return []
    values: List[Tuple[int, Path]] = []
    for line in result.stdout.splitlines():
        try:
            pid = int(line.strip())
        except ValueError:
            continue
        buffer = ctypes.create_string_buffer(4096)
        length = proc_pidpath(pid, buffer, len(buffer))
        if length <= 0:
            continue
        try:
            executable = Path(os.fsdecode(buffer.value)).resolve(strict=False)
        except (OSError, ValueError):
            continue
        values.append((pid, executable))
    return values


def _bundle_for_executable(executable: Path) -> Optional[Path]:
    for item in (executable, *executable.parents):
        if item.name.casefold() == "cursor.app":
            return item
    return None


def _mac_running_candidates() -> List[str]:
    values: Dict[str, str] = {}
    for _pid, executable in _mac_process_paths():
        bundle = _bundle_for_executable(executable)
        if bundle is not None:
            values.setdefault(_path_key(bundle), str(bundle))
    return list(values.values())


def _mac_spotlight_candidates() -> List[str]:
    mdfind = shutil.which("mdfind")
    if not mdfind:
        return []
    try:
        result = subprocess.run(
            [
                mdfind,
                "kMDItemCFBundleIdentifier == 'com.todesktop.230313mzl4w4u92'",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if result.returncode != 0:
        return []
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


@dataclass(frozen=True)
class _LinuxProcess:
    ppid: int
    pgid: int
    sid: int
    state: str
    start_time: int
    executable: Optional[Path]
    argv: Tuple[str, ...]


def _read_linux_proc_stat(path: Path) -> Tuple[int, int, int, str, int]:
    raw = path.read_text(encoding="utf-8", errors="replace")
    # comm 可以包含空格和括号，以最后一个右括号为界再解析固定字段。
    tail = raw.rsplit(")", 1)[1].split()
    return int(tail[1]), int(tail[2]), int(tail[3]), tail[0], int(tail[19])


def _linux_process_snapshot(strict: bool = False) -> Dict[int, _LinuxProcess]:
    proc_root = Path("/proc")
    try:
        entries = tuple(proc_root.iterdir())
    except OSError as exc:
        if strict:
            raise SandToolError("无法读取 Linux /proc 进程信息") from exc
        return {}

    processes: Dict[int, _LinuxProcess] = {}
    for entry in entries:
        if not entry.name.isdigit():
            continue
        try:
            pid = int(entry.name)
            ppid, pgid, sid, state, start_time = _read_linux_proc_stat(entry / "stat")
            cmdline = (entry / "cmdline").read_bytes()
        except (OSError, ValueError, IndexError):
            continue

        executable: Optional[Path] = None
        try:
            target = os.readlink(entry / "exe")
            if target.endswith(" (deleted)"):
                target = target[:-10]
            executable = Path(target).resolve(strict=False)
        except (OSError, ValueError):
            pass
        argv = tuple(
            os.fsdecode(part) for part in cmdline.split(b"\0") if part
        )
        processes[pid] = _LinuxProcess(
            ppid=ppid,
            pgid=pgid,
            sid=sid,
            state=state,
            start_time=start_time,
            executable=executable,
            argv=argv,
        )
    if strict and not processes:
        raise SandToolError("无法读取 Linux /proc 进程信息")
    return processes


def _linux_direct_cursor_pids(
    layout: CursorLayout,
    processes: Mapping[int, _LinuxProcess],
) -> Set[int]:
    selected_executable = layout.executable.resolve(strict=False)
    entrypoint = (layout.app_root / "cursor.mjs").resolve(strict=False)
    direct: Set[int] = set()
    for pid, process in processes.items():
        if process.executable == selected_executable:
            direct.add(pid)
            continue
        for argument in process.argv[1:]:
            if not argument.startswith("/"):
                continue
            try:
                argument_path = Path(argument).resolve(strict=False)
            except (OSError, RuntimeError, ValueError):
                continue
            if argument_path in (selected_executable, entrypoint):
                direct.add(pid)
                break
    return direct


def _linux_cursor_processes(
    layout: CursorLayout,
    strict: bool = False,
) -> Tuple[Dict[int, _LinuxProcess], Set[int]]:
    processes = _linux_process_snapshot(strict=strict)
    direct = _linux_direct_cursor_pids(layout, processes)
    selected = set(direct)

    # Desktop 启动的 Electron 主进程通常是独立 session/group leader。
    # 只在两者都等于 PID 时纳入整组，避免误伤从普通终端启动的其他程序。
    dedicated_groups = {
        process.pgid
        for pid in direct
        for process in (processes[pid],)
        if process.pgid == pid and process.sid == pid
    }
    if dedicated_groups:
        selected.update(
            pid for pid, process in processes.items() if process.pgid in dedicated_groups
        )

    # 系统 Electron 打包的子进程只显示 /usr/lib/electron，需要通过祖先链归属。
    while True:
        descendants = {
            pid for pid, process in processes.items() if process.ppid in selected
        }
        next_selected = selected | descendants
        if next_selected == selected:
            break
        selected = next_selected
    return processes, selected


def _linux_main_pids(
    layout: CursorLayout,
    processes: Optional[Mapping[int, _LinuxProcess]] = None,
) -> Set[int]:
    """Return only Cursor's Electron entry process(es), excluding helper shells."""
    snapshot = processes if processes is not None else _linux_process_snapshot()
    entrypoint = (layout.app_root / "cursor.mjs").resolve(strict=False)
    selected_executable = layout.executable.resolve(strict=False)
    result: Set[int] = set()
    for pid, process in snapshot.items():
        if process.executable == selected_executable:
            result.add(pid)
            continue
        if any(
            argument.startswith("/")
            and _linux_argument_resolves_to(argument, entrypoint)
            for argument in process.argv[1:]
        ):
            result.add(pid)
    return result


def _linux_argument_resolves_to(argument: str, expected: Path) -> bool:
    try:
        return Path(argument).resolve(strict=False) == expected
    except (OSError, RuntimeError, ValueError):
        return False


def _linux_running_candidates() -> List[str]:
    values: Dict[str, str] = {}
    for process in _linux_process_snapshot().values():
        if process.executable is not None and process.executable.name.casefold() == "cursor":
            values.setdefault(_path_key(process.executable), str(process.executable))
        for argument in process.argv[1:]:
            if not argument.startswith("/"):
                continue
            path = Path(argument)
            if path.name.casefold() != "cursor.mjs":
                continue
            try:
                resolved = path.resolve(strict=True)
            except (FileNotFoundError, OSError, RuntimeError, ValueError):
                continue
            values.setdefault(_path_key(resolved.parent), str(resolved.parent))
    return list(values.values())


def _linux_user_home() -> Path:
    """sudo 提权后仍返回原登录用户的 HOME，用于扫描 ~/.local、Flatpak 用户安装等。"""
    if sys.platform.startswith("linux") and getattr(os, "geteuid", lambda: 1)() == 0:
        account = _linux_invoking_account()
        if account is not None:
            return Path(account.pw_dir)
    return Path.home()


def _linux_flatpak_candidates(app_id: str) -> List[str]:
    if not app_id or "/" in app_id or "\\" in app_id or app_id in {".", ".."}:
        return []
    values: List[str] = []
    for root in (
        Path("/var/lib/flatpak/app"),
        _linux_user_home() / ".local" / "share" / "flatpak" / "app",
    ):
        files_root = root / app_id / "current" / "active" / "files"
        values.extend(
            (
                str(files_root),
                str(files_root / "opt" / "Cursor"),
                str(files_root / "usr" / "share" / "cursor"),
            )
        )
    return values


def _linux_desktop_candidates() -> List[str]:
    """从 XDG .desktop 启动项的 Exec= 读取 Cursor 可执行文件路径。"""
    directories: List[Path] = []
    xdg_data_home = os.environ.get("XDG_DATA_HOME", "").strip()
    if xdg_data_home and Path(xdg_data_home).expanduser().is_absolute():
        directories.append(Path(xdg_data_home).expanduser() / "applications")
    else:
        directories.append(_linux_user_home() / ".local" / "share" / "applications")
    data_dirs = os.environ.get("XDG_DATA_DIRS", "").strip() or "/usr/local/share:/usr/share"
    directories.extend(
        Path(raw_dir).expanduser() / "applications" for raw_dir in data_dirs.split(":") if raw_dir
    )

    values: Dict[str, str] = {}
    seen_directories: Set[str] = set()
    for directory in directories:
        directory_key = _path_key(directory)
        if directory_key in seen_directories or not directory.is_dir():
            continue
        seen_directories.add(directory_key)
        try:
            entries = sorted(directory.iterdir(), key=lambda item: item.name.casefold())
        except OSError:
            continue
        for desktop in entries:
            if desktop.suffix.casefold() != ".desktop":
                continue
            try:
                if desktop.stat().st_size > 256 * 1024:
                    continue
                text = desktop.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if "cursor" not in desktop.name.casefold() and "cursor" not in text.casefold():
                continue
            for line in text.splitlines():
                if not line.startswith(("Exec=", "TryExec=")):
                    continue
                try:
                    argv = shlex.split(line.split("=", 1)[1].strip())
                except ValueError:
                    continue
                if not argv:
                    continue
                if argv[0] == "flatpak":
                    app_id = next(
                        (
                            token
                            for token in reversed(argv[1:])
                            if not token.startswith(("-", "%")) and token != "run"
                        ),
                        "",
                    )
                    for candidate in _linux_flatpak_candidates(app_id):
                        values.setdefault(_path_key(Path(candidate)), candidate)
                    continue
                # 部分 .desktop 用 env VAR=… 前缀包一层。
                index = 0
                if argv[0] == "env":
                    index = 1
                    while index < len(argv) and "=" in argv[index]:
                        index += 1
                    if index >= len(argv):
                        continue
                command = argv[index]
                if not command or command.startswith("%"):
                    continue
                if not os.path.isabs(command):
                    command = shutil.which(command) or command
                try:
                    executable = Path(command).expanduser()
                    if not executable.is_absolute():
                        continue
                    executable = executable.resolve(strict=False)
                except (OSError, RuntimeError, ValueError):
                    continue
                values.setdefault(_path_key(executable), str(executable))
    return list(values.values())


def _linux_default_candidates() -> List[str]:
    """官方 .deb/tar、发行版包、Snap、Flatpak 以及用户目录下的常见安装位置。"""
    home = _linux_user_home()
    values: List[Path] = [
        Path("/usr/share/cursor"),
        Path("/usr/share/Cursor"),
        Path("/usr/lib/cursor"),
        Path("/usr/lib/Cursor"),
        Path("/opt/Cursor"),
        Path("/opt/cursor"),
        Path("/usr/local/cursor"),
        Path("/usr/local/Cursor"),
        Path("/usr/local/lib/cursor"),
        Path("/usr/local/share/cursor"),
        Path("/usr/bin/cursor"),
        Path("/usr/local/bin/cursor"),
        Path("/snap/cursor/current"),
        Path("/snap/cursor/current/opt/Cursor"),
        Path("/snap/cursor/current/usr/share/cursor"),
        Path("/var/lib/snapd/snap/cursor/current"),
        Path("/var/lib/snapd/snap/cursor/current/opt/Cursor"),
        Path("/var/lib/snapd/snap/cursor/current/usr/share/cursor"),
        home / ".local" / "share" / "cursor",
        home / ".local" / "share" / "Cursor",
        home / ".local" / "opt" / "Cursor",
        home / ".local" / "opt" / "cursor",
    ]
    values.extend(Path(p) for p in _linux_flatpak_candidates("com.cursor.Cursor"))
    xdg_data_home = os.environ.get("XDG_DATA_HOME", "").strip()
    if xdg_data_home and Path(xdg_data_home).expanduser().is_absolute():
        values.extend((Path(xdg_data_home).expanduser() / "Cursor", Path(xdg_data_home).expanduser() / "cursor"))

    # tar 包常被解压成 /opt/Cursor-linux-x64 或 ~/Cursor-*；只扫这几个一级目录，避免全盘遍历。
    for parent in (Path("/opt"), Path("/usr/local"), Path("/usr/lib"), Path("/usr/share"), home, home / "Applications", home / "apps"):
        try:
            values.extend(item for item in parent.iterdir() if item.is_dir() and "cursor" in item.name.casefold())
        except OSError:
            continue
    for flatpak_root in (Path("/var/lib/flatpak/app"), home / ".local" / "share" / "flatpak" / "app"):
        try:
            apps = [app for app in flatpak_root.iterdir() if app.is_dir() and "cursor" in app.name.casefold()]
        except OSError:
            continue
        for app in apps:
            values.extend(Path(p) for p in _linux_flatpak_candidates(app.name))

    result: List[str] = []
    seen: Set[str] = set()
    for value in values:
        key = _path_key(value)
        if key in seen:
            continue
        seen.add(key)
        result.append(str(value))
    return result


def _linux_remote_server_candidates() -> List[str]:
    """Cursor Remote SSH 服务端：~/.cursor-server/bin/<os>-<arch>/<commit>/。

    只在没有桌面版时才会被自动选中（候选组排在最后）；桌面版与服务端并存的机器请用
    SAND_CURSOR_INSTALL_DIR 显式指定服务端目录。
    """
    values: List[str] = []
    for home in {_linux_user_home(), Path.home()}:
        bin_root = home / ".cursor-server" / "bin"
        try:
            platforms = sorted(p for p in bin_root.iterdir() if p.is_dir())
        except OSError:
            continue
        for platform_dir in platforms:
            if platform_dir.name == "multiplex-server":
                continue
            try:
                commits = sorted(c for c in platform_dir.iterdir() if c.is_dir())
            except OSError:
                continue
            values.extend(str(c) for c in commits if (c / "bin" / "cursor-server").is_file())
    return values


def _default_candidate_groups() -> Iterable[Tuple[str, Sequence[str]]]:
    env_candidate = os.environ.get("SAND_CURSOR_INSTALL_DIR", "").strip()
    if env_candidate:
        yield "环境变量 SAND_CURSOR_INSTALL_DIR", (env_candidate,)

    if sys.platform == "win32":
        # 先查默认安装目录（纯 Path 判断，秒级、无子进程），命中就不必跑慢的 PowerShell。
        local = os.environ.get("LOCALAPPDATA", "")
        program_files = os.environ.get("ProgramFiles", r"C:\Program Files")
        program_files_x86 = os.environ.get("ProgramFiles(x86)", "")
        defaults = [
            str(Path(local) / "Programs" / "Cursor") if local else "",
            str(Path(local) / "Programs" / "cursor") if local else "",
            str(Path(local) / "Cursor") if local else "",
            str(Path(program_files) / "Cursor"),
            str(Path(program_files_x86) / "Cursor") if program_files_x86 else "",
        ]
        yield "Windows 默认目录", tuple(x for x in defaults if x)
        yield "Windows 安装登记", _windows_registry_candidates()
        # 运行中的 Cursor 用 PowerShell CIM 查询较慢，放最后兜底（非默认路径安装时才需要）。
        yield "运行中的 Cursor", _windows_running_candidates()
    elif sys.platform == "darwin":
        yield "运行中的 Cursor", _mac_running_candidates()
        yield "macOS Spotlight", _mac_spotlight_candidates()
        yield "macOS 默认目录", (
            "/Applications/Cursor.app",
            str(Path.home() / "Applications" / "Cursor.app"),
        )
    elif sys.platform.startswith("linux"):
        yield "Linux 默认目录", tuple(_linux_default_candidates())
        yield "Linux desktop 启动项", _linux_desktop_candidates()
        yield "运行中的 Cursor", _linux_running_candidates()

    path_cursor = shutil.which("cursor")
    if path_cursor:
        yield "PATH", (path_cursor,)

    if sys.platform.startswith("linux"):
        # 无桌面版的纯服务器（只跑 Remote SSH 服务端）才会走到这里。
        yield "Remote SSH 服务端", _linux_remote_server_candidates()


def _valid_layouts(values: Sequence[str]) -> List[CursorLayout]:
    layouts: Dict[str, CursorLayout] = {}
    for value in values:
        if not value:
            continue
        try:
            layout = layout_from_path(value)
        except (SandToolError, OSError, RuntimeError, ValueError):
            continue
        layouts.setdefault(_path_key(layout.app_root), layout)
    return list(layouts.values())


def resolve_cursor_layout() -> CursorLayout:
    # 显式环境变量优先于已保存的路径：同一台机器上可能既有桌面版（config 里记录的）
    # 又有 Remote SSH 服务端，对后者打补丁时不应改动桌面版的配置。
    env_candidate = os.environ.get("SAND_CURSOR_INSTALL_DIR", "").strip()
    if env_candidate:
        return layout_from_path(env_candidate)

    configured = _load_config().get("cursorInstallRoot")
    if isinstance(configured, str) and configured.strip():
        try:
            return layout_from_path(configured)
        except SandToolError as exc:
            raise SandToolError(
                f"已设置的 Cursor 路径失效：{configured}\n"
                "请运行 set-path <新路径>，或运行 set-path auto 恢复自动检测"
            ) from exc

    for source, values in _default_candidate_groups():
        layouts = _valid_layouts(tuple(values))
        if len(layouts) == 1:
            return layouts[0]
        if len(layouts) > 1:
            options = "\n".join(f"  - {item.install_root}" for item in layouts)
            raise SandToolError(
                f"{source}检测到多个 Cursor 安装，请先在菜单中选择 3 设置路径：\n{options}"
            )
    raise SandToolError(
        "未检测到 Cursor 安装，请在菜单中选择 3 设置 Cursor 路径"
        "（Cursor.exe、Cursor.app、Linux 可填 /usr/share/cursor 或 /usr/bin/cursor，或 resources/app）"
    )


def save_cursor_path(value: str) -> Optional[CursorLayout]:
    if value.strip().casefold() in {"auto", "clear", "reset"}:
        _write_json_atomic(
            _config_path(),
            {
                "version": CONFIG_VERSION,
                "cursorInstallRoot": "",
                "lastVerifiedVersion": "",
                "updatedAt": datetime.now(timezone.utc).isoformat(),
            },
        )
        return None

    layout = layout_from_path(value)
    _write_json_atomic(
        _config_path(),
        {
            "version": CONFIG_VERSION,
            "cursorInstallRoot": str(layout.install_root),
            "lastVerifiedVersion": layout.version,
            "updatedAt": datetime.now(timezone.utc).isoformat(),
        },
    )
    return layout


def apply_patch_to_content(content: str) -> Tuple[str, PatchStats]:
    stats = PatchStats()
    next_content = content
    legacy_client_re = re.compile(
        rf"([\"'])sand\1{LEGACY_CLIENT_MARKER_PATTERN}"
    )
    next_content, stats.migrated_client = legacy_client_re.subn(
        lambda match: match.group(1)
        + "sand"
        + match.group(1)
        + SAND_CLIENT_MARKER,
        next_content,
    )
    legacy_eligibility = "return!1;" + LEGACY_SAND_ELIGIBILITY_MARKER
    stats.migrated_eligibility = next_content.count(legacy_eligibility)
    next_content = next_content.replace(
        legacy_eligibility,
        "return!1;" + SAND_ELIGIBILITY_MARKER,
    )
    # 按请求分流 client-type：AgentService / agent.v1 出 ide，其余出 sand。
    # 必须替换整个第二实参（不能只改 ?? fallback）：Connect 拦截器会在出站前再次
    # applyRequestHeaders，g 已被改成 sand 时 ?? 短路，字面 sand 也会覆盖 prepareAgentRun 的 ide。
    def _smart_header(match: "re.Match[str]") -> str:
        stats.set_header += 1
        obj = match.group(1)
        q = match.group(2)
        original_arg = match.group(3)
        value_quote = match.group(4)
        value = match.group(5)
        markers = match.group(6)
        # 实参带旧版 V1 marker 时，值已被改写；按 marker 语义推回官方原值。
        if markers:
            if "SAND_CLIENT_EXISTING" in markers:
                restored_value = "sand"
            elif "GLASSFIX" in markers:
                restored_value = "glass"
            else:
                restored_value = "ide"
            original_arg = original_arg[: original_arg.rfind(value_quote + value + value_quote)] + (
                value_quote + restored_value + value_quote
            )
        return (
            f"{obj}.header.set({q}x-cursor-client-type{q},"
            f"{SAND_HDRFIX_V2_FN}({obj}){SAND_HDRFIX_V2_MARKER}||({original_arg}))"
        )

    next_content = HEADER_SET_SIMPLE_RE.sub(_smart_header, next_content)

    for key, rule in CLIENT_RULES:
        def replace_client(match: re.Match[str], stat_key: str = key) -> str:
            current = match.group(3)
            setattr(stats, stat_key, getattr(stats, stat_key) + 1)
            if current == "sand":
                stats.adopted_sand += 1
                marker = SAND_CLIENT_EXISTING_MARKER
            else:
                marker = SAND_CLIENT_MARKER
            return (
                match.group(1)
                + match.group(2)
                + "sand"
                + match.group(2)
                + marker
            )

        next_content = rule.sub(replace_client, next_content)

    # glass UI 关键修复：client-type 三元 isGlass?"glass":"ide" 在 glass UI 上取「真分支」= "glass"，
    # 服务端把 "glass" 当普通 IDE 免费号拦。强制真分支也为 "sand"（对齐 Grok Bot 的 lb="sand"）。
    glass_true_pattern = re.compile(r'(isGlass\?)(["\'])glass\2(:)(["\'])(?:ide|sand)\4')

    def _fix_glass_true(match: "re.Match[str]") -> str:
        stats.is_glass += 1
        q1 = match.group(2)
        q2 = match.group(4)
        return f"{match.group(1)}{q1}sand{q1}{SAND_GLASSFIX_MARKER}{match.group(3)}{q2}sand{q2}"

    next_content = glass_true_pattern.sub(_fix_glass_true, next_content)

    # 版本无关：匹配任意「函数体第一句是 const{adminSettingsService:...}」的资格函数并注入 return!1，
    # 取代原固定混淆函数名清单（会随 Cursor 版本失效）。注入后 { 后是 return，不会重复匹配，天然幂等。
    eligibility_pattern = re.compile(
        r"(function\s+[A-Za-z0-9_$]+\([A-Za-z0-9_$]+\)\{)(const\{adminSettingsService:)"
    )

    def inject_eligibility(match: "re.Match[str]") -> str:
        stats.eligibility += 1
        return match.group(1) + "return!1;" + SAND_ELIGIBILITY_MARKER + match.group(2)

    next_content = eligibility_pattern.sub(inject_eligibility, next_content)

    # 解锁模型列表（移植自 cursor-fd unlock-membership）：
    # 免费账号的「模型选择器」判定函数体形如  ...})\{return X===M.FREE&&Y&&Z===void 0}
    # 在函数体开头插入 return!1; 让它恒为 false（不再因 FREE 锁命名模型）。原表达式作为死代码保留，可回退。
    # M 用 \w+ 泛化（不同版本变量名不同），比 cursor-fd 写死 lr 更耐版本。
    model_lock_pattern = re.compile(
        r"(hasResolvedTeamMembership:\w+,teamId:\w+\}\)\{)(return \w+===\w+\.FREE&&\w+&&\w+===void 0\})"
    )

    def inject_model_unlock(match: "re.Match[str]") -> str:
        stats.model_unlock += 1
        return match.group(1) + "return!1;" + SAND_MODEL_UNLOCK_MARKER + match.group(2)

    next_content = model_lock_pattern.sub(inject_model_unlock, next_content)

    # 会员判定改 PRO（3.8.24 实测命中）：客户端 _membershipType 读的是 storageService 里
    # cursorAuth/stripeMembershipType（值就是 "free"/"pro" 等枚举字符串）。改成 =>"pro"||原读取，
    # 短路恒返回 "pro"，让所有 ===qs.FREE 判定失效、===qs.PRO 成立。原读取保留为死代码，可回退。
    mem_pro_pattern = re.compile(r"(_membershipType=\(\)=>)(this\.storageService\.get\()")

    def inject_mem_pro(match: "re.Match[str]") -> str:
        stats.model_unlock += 1
        return match.group(1) + '"enterprise"||' + SAND_MEM_PRO_MARKER + match.group(2)

    next_content = mem_pro_pattern.sub(inject_mem_pro, next_content)
    # 刷新旧补丁里的 "pro" -> "enterprise"（旧版打的是 pro，再打补丁时升级）。
    next_content = re.sub(
        r'"pro"\|\|(' + re.escape(SAND_MEM_PRO_MARKER) + r")",
        r'"enterprise"||\1',
        next_content,
    )

    # 解锁 Max mode（3.8.24 实测命中）：hasValidPaymentMethod=async()=>{...联网查绑卡...}
    # 免费无卡返回 false → 触发「Max mode is only available to paid users」。
    # 在函数体开头插 return!0; 恒返回 true（Promise<true>），绕过绑卡守卫。负向前瞻保证幂等，可回退。
    maxmode_pattern = re.compile(r"(hasValidPaymentMethod=async\(\)=>\{)(?!return!0;)")

    def inject_maxmode(match: "re.Match[str]") -> str:
        stats.model_unlock += 1
        return match.group(1) + "return!0;" + SAND_MAXMODE_MARKER

    next_content = maxmode_pattern.sub(inject_maxmode, next_content)

    def _restore_proxy_stream(match: "re.Match[str]") -> str:
        stats.rpc_rewrite += 1
        prefix = match.group(1)
        tr = match.group(2)
        arglist = match.group(3)
        return f"{prefix}{tr}.transport.stream({arglist})"

    next_content = STREAM_WRAP_RESTORE_RE.sub(_restore_proxy_stream, next_content)
    for old, new in _TRANSPORT_HOST_SWAPS:
        if new in next_content:
            next_content = next_content.replace(new, old)
            stats.rpc_rewrite += 1
    if NEW_RPC_PATH in next_content:
        n = next_content.count(NEW_RPC_PATH)
        next_content = next_content.replace(NEW_RPC_PATH, OLD_RPC_PATH)
        stats.rpc_rewrite += n

    def _inject_agent_ide(match: "re.Match[str]") -> str:
        stats.rpc_rewrite += 1
        ident = match.group(1)
        return (
            f'{ident}.set("x-cursor-client-type","ide"{SAND_AGENT_IDE_MARKER});'
            f"return{{headers:{ident},credentialFingerprint:"
        )

    next_content = AGENT_IDE_INJECT_RE.sub(_inject_agent_ide, next_content)
    next_content, stream_hook_count = STREAM_HOOK_REMOVE_RE.subn("", next_content)
    stats.rpc_rewrite += stream_hook_count

    next_content, route_count = MANAGED_LOCAL_ROUTE_RE.subn(
        _managed_local_route_sub, next_content
    )
    stats.managed_local_route += route_count

    next_content, runtime_load_count = LOCAL_RUNTIME_LOAD_RE.subn(
        _local_runtime_load_sub, next_content
    )
    stats.local_runtime_load += runtime_load_count

    identity_count = next_content.count(AGENT_HOST_IDENTITY_ORIGINAL)
    if identity_count:
        next_content = next_content.replace(
            AGENT_HOST_IDENTITY_ORIGINAL,
            AGENT_HOST_IDENTITY_PATCHED,
        )
        stats.agent_host_identity += identity_count

    # 1.1.10：不再强制 move_exec；把旧版强制写法还原为官方门控（stats.move_exec 记还原数）。
    next_content, move_exec_count = _restore_move_exec_gates(next_content)
    stats.move_exec += move_exec_count

    next_content, subagent_count = _apply_subagent_patches(next_content)
    stats.subagent += subagent_count
    next_content, wake_count = _apply_subagent_wake(next_content)
    stats.subagent_wake += wake_count

    # 1.1.0–1.1.3 把 hre/createPromptSession 短路成 Joe(rawInferenceClient)。
    # 官方必须先 runInference，再用握手后的 multiplex client 建 Joe；
    # 否则 Koe.stream 打到 InferenceService.Stream，工具对象没有 execute。
    next_content, direct_stripped = _strip_direct_stream_injection(next_content)
    stats.direct_stream += direct_stripped

    if SAND_AGENT_HOST_ENABLEMENT_MARKER not in next_content:
        def enable_agent_host(match: re.Match[str]) -> str:
            variable = match.group(2)
            return (
                variable
                + "=!0;"
                + SAND_AGENT_HOST_ENABLEMENT_MARKER
                + match.group(1)
                + variable
                + match.group(3)
            )

        next_content, agent_host_count = AGENT_HOST_ENABLEMENT_RE.subn(
            enable_agent_host,
            next_content,
            count=1,
        )
        stats.agent_host_enablement += agent_host_count
    if AGENTEXEC_SKIP_PATCHED in next_content:
        next_content = next_content.replace(
            AGENTEXEC_SKIP_PATCHED,
            AGENTEXEC_SKIP_ORIGINAL,
        )
    return next_content, stats


def remove_patch_from_content(content: str) -> Tuple[str, RemoveStats]:
    stats = RemoveStats()
    next_content, rpc_snip_count = _strip_rpc_snippets(content)
    stats.client_type += rpc_snip_count
    inf_run_re = re.compile(
        r'typeName:"aiserver\.v1\.InferenceService",methods:\{run:\{name:"Stream"'
        r'(,I:[$\w.]+,O:[$\w.]+,kind:)'
        r'((?:[$\w.]+\.)?ServerStreaming|1)\b'
    )

    def _restore_agent_run(match: "re.Match[str]") -> str:
        kind = match.group(2)
        old_kind = "3" if kind == "1" else kind.replace("ServerStreaming", "BiDiStreaming")
        return (
            'typeName:"agent.v1.AgentService",methods:{run:{name:"Run"'
            + match.group(1)
            + old_kind
        )

    next_content, inf_run_count = inf_run_re.subn(_restore_agent_run, next_content)
    stats.client_type += inf_run_count
    if NEW_RPC_PATH in next_content:
        n = next_content.count(NEW_RPC_PATH)
        next_content = next_content.replace(NEW_RPC_PATH, OLD_RPC_PATH)
        stats.client_type += n

    def _restore_proxy_stream(match: "re.Match[str]") -> str:
        stats.client_type += 1
        prefix = match.group(1)
        tr = match.group(2)
        arglist = match.group(3)
        return f"{prefix}{tr}.transport.stream({arglist})"

    next_content = STREAM_WRAP_RESTORE_RE.sub(_restore_proxy_stream, next_content)
    for old, new in _TRANSPORT_HOST_SWAPS:
        if new in next_content:
            next_content = next_content.replace(new, old)
            stats.client_type += 1
    next_content, agent_ide_count = AGENT_IDE_REMOVE_RE.subn("", next_content)
    stats.rpc_rewrite += agent_ide_count
    next_content, stream_hook_count = STREAM_HOOK_REMOVE_RE.subn("", next_content)
    stats.rpc_rewrite += stream_hook_count
    legacy_client_re = re.compile(
        rf"([\"'])sand\1{LEGACY_CLIENT_MARKER_PATTERN}"
    )
    next_content, legacy_client_count = legacy_client_re.subn(
        lambda match: match.group(1) + "ide" + match.group(1),
        next_content,
    )
    stats.client_type += legacy_client_count
    legacy_eligibility = "return!1;" + LEGACY_SAND_ELIGIBILITY_MARKER
    legacy_eligibility_count = next_content.count(legacy_eligibility)
    next_content = next_content.replace(legacy_eligibility, "")
    stats.eligibility += legacy_eligibility_count
    client_re = re.compile(rf"([\"'])sand\1{CLIENT_MARKER_PATTERN}")
    existing_re = re.compile(
        rf"([\"'])sand\1{CLIENT_EXISTING_MARKER_PATTERN}"
    )

    def remove_client(match: re.Match[str]) -> str:
        stats.client_type += 1
        return match.group(1) + "ide" + match.group(1)

    next_content = client_re.sub(remove_client, next_content)
    next_content, existing_count = existing_re.subn(
        lambda match: match.group(1) + "sand" + match.group(1),
        next_content,
    )
    stats.client_type += existing_count
    # 回退 glass 修复：真分支 "sand"/*GLASSFIX*/ 还原为 "glass"；强制头 "sand"/*HDRFIX*/ 还原为 "ide"。
    glassfix_re = re.compile(r"([\"'])sand\1" + re.escape(SAND_GLASSFIX_MARKER))
    next_content, glassfix_count = glassfix_re.subn(
        lambda match: match.group(1) + "glass" + match.group(1),
        next_content,
    )
    stats.client_type += glassfix_count
    hdrfix_re = re.compile(r"([\"'])sand\1" + re.escape(SAND_HDRFIX_MARKER))
    next_content, hdrfix_count = hdrfix_re.subn(
        lambda match: match.group(1) + "ide" + match.group(1),
        next_content,
    )
    stats.client_type += hdrfix_count
    # 新写法带 ||(原实参) 死代码可精确还原；旧写法没有保留原实参，只能还原成 "ide"。
    next_content, hdrfix_v2_count = HDRFIX_V2_REMOVE_RE.subn(
        lambda match: match.group(1) or '"ide"', next_content
    )
    stats.client_type += hdrfix_v2_count
    eligibility_re = re.compile(rf"return!1;{ELIGIBILITY_MARKER_PATTERN}")
    next_content, eligibility_count = eligibility_re.subn("", next_content)
    stats.eligibility += eligibility_count
    # 移除模型解锁注入：还原为原判定表达式（去掉开头插入的 return!1; + marker）。
    model_unlock_re = re.compile(r"return!1;" + re.escape(SAND_MODEL_UNLOCK_MARKER))
    next_content, model_unlock_count = model_unlock_re.subn("", next_content)
    stats.eligibility += model_unlock_count
    # 移除会员改写：去掉插入的 "enterprise"||（或旧补丁的 "pro"||）+ marker，还原原读取。
    mem_pro_re = re.compile(r'"(?:enterprise|pro)"\|\|' + re.escape(SAND_MEM_PRO_MARKER))
    next_content, mem_pro_count = mem_pro_re.subn("", next_content)
    stats.eligibility += mem_pro_count
    # 移除 Max mode 解锁：去掉插入的 return!0; + marker。
    maxmode_re = re.compile(r"return!0;" + re.escape(SAND_MAXMODE_MARKER))
    next_content, maxmode_count = maxmode_re.subn("", next_content)
    stats.eligibility += maxmode_count

    next_content, route_count = MANAGED_LOCAL_ROUTE_RESTORE_RE.subn(
        "try{return", next_content
    )
    stats.managed_local_route += route_count

    next_content, runtime_load_count = LOCAL_RUNTIME_LOAD_RESTORE_RE.subn(
        "", next_content
    )
    stats.local_runtime_load += runtime_load_count

    identity_count = next_content.count(AGENT_HOST_IDENTITY_PATCHED)
    if identity_count:
        next_content = next_content.replace(
            AGENT_HOST_IDENTITY_PATCHED,
            AGENT_HOST_IDENTITY_ORIGINAL,
        )
        stats.agent_host_identity += identity_count

    next_content, move_exec_count = _restore_move_exec_gates(next_content)
    stats.move_exec += move_exec_count

    next_content, subagent_count = _remove_subagent_patches(next_content)
    stats.subagent += subagent_count
    next_content, wake_count = _remove_subagent_wake(next_content)
    stats.subagent_wake += wake_count

    next_content, direct_count = _strip_direct_stream_injection(next_content)
    stats.direct_stream += direct_count

    next_content, agent_host_count = AGENT_HOST_ENABLEMENT_PATCH_RE.subn(
        lambda match: match.group(2) + match.group(1) + match.group(3),
        next_content,
    )
    stats.agent_host_enablement += agent_host_count
    if AGENTEXEC_SKIP_PATCHED in next_content:
        next_content = next_content.replace(
            AGENTEXEC_SKIP_PATCHED,
            AGENTEXEC_SKIP_ORIGINAL,
        )

    # 剥离会员伪装注入片段（正则匹配任意版本，缺失则无操作）。
    next_content, mem_snip_count = MEMBERSHIP_SNIPPET_RE.subn("", next_content)
    stats.client_type += mem_snip_count
    # 兜底：清除任何仍附着在 "ide"/"sand"/"glass" 上的残留 sand marker。旧补丁会把 HDRFIX 与
    # CLIENT_MODE/EXISTING 叠加在同一个值上（如 "sand"/*HDRFIX*//*CLIENT_MODE*/），上面按单一锚点的
    # 规则删掉 HDRFIX 后值变成 "ide"，导致 CLIENT_MODE 的正则（锚定 "sand"）漏删而残留，进而卸载校验失败。
    # 这里按最靠近值的 marker 决定还原值（EXISTING→sand，GLASSFIX→glass，其余→ide），保证回滚后零残留。
    residual_marker_re = re.compile(
        r'(["\'])(?:ide|sand|glass)\1((?:/\*SAND[A-Z0-9_]*_V1\*/)+)'
    )

    def _collapse_residual(match: "re.Match[str]") -> str:
        quote = match.group(1)
        first = re.match(r"/\*(SAND[A-Z0-9_]*_V1)\*/", match.group(2)).group(1)
        if "EXISTING" in first:
            value = "sand"
        elif "GLASSFIX" in first:
            value = "glass"
        else:
            value = "ide"
        return f"{quote}{value}{quote}"

    next_content, residual_count = residual_marker_re.subn(_collapse_residual, next_content)
    stats.client_type += residual_count
    return next_content, stats


def _decode_js(data: bytes, path: Path) -> str:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SandToolError(f"目标文件不是 UTF-8，拒绝修改：{path}") from exc


def _read_planned_file(path: Path) -> PlannedFile:
    original = path.read_bytes()
    return PlannedFile(
        original=original,
        next_bytes=original,
        mode=stat.S_IMODE(path.stat().st_mode),
    )


def _target_extension_name(layout: CursorLayout, file_path: Path) -> Optional[str]:
    for rel, extension_name in TARGET_SPECS:
        if not extension_name:
            continue
        candidate = layout.app_root.joinpath(*rel.split("/")).resolve()
        if candidate == file_path.resolve():
            return extension_name
    return None


def _extension_hash_map_present(content: str, extension_id: str) -> bool:
    """extensionHost 里是否有 `"id": { ... "main.js": "<hash>" }` 完整性表项。

    仅出现在扩展 ID 列表里（没有后面的 `:{`）不算；那种文件改了也不需要改哈希。
    """
    return (
        re.search(rf'"{re.escape(extension_id)}"\s*:\s*\{{', content) is not None
    )


def _update_extension_hashes(
    layout: CursorLayout,
    plan: Dict[Path, PlannedFile],
) -> None:
    changed_extensions: List[Tuple[str, bytes]] = []
    for file_path, planned in plan.items():
        extension_name = _target_extension_name(layout, file_path)
        if extension_name:
            changed_extensions.append((extension_name, planned.next_bytes))
    if not changed_extensions or layout.ext_host_path is None:
        return

    ext_path = layout.ext_host_path
    existing = plan.get(ext_path) or _read_planned_file(ext_path)
    next_content = _decode_js(existing.next_bytes, ext_path)
    original_content = _decode_js(existing.original, ext_path)

    for extension_name, next_main in changed_extensions:
        extension_id = "anysphere." + extension_name
        if not _extension_hash_map_present(next_content, extension_id):
            continue
        digest = hashlib.sha256(next_main).hexdigest()
        pattern = re.compile(
            rf'(\"{re.escape(extension_id)}\"\s*:\s*\{{[\s\S]{{0,2400}}?'
            rf'\"main\.js\"\s*:\s*\")[0-9a-f]{{64}}(\")'
        )
        next_content, count = pattern.subn(
            lambda match: match.group(1) + digest + match.group(2),
            next_content,
            count=1,
        )
        if count != 1:
            raise SandToolError(f"无法定位 {extension_id} 的内嵌 main.js 哈希")

    if next_content != original_content:
        plan[ext_path] = PlannedFile(
            original=existing.original,
            next_bytes=next_content.encode("utf-8"),
            mode=existing.mode,
        )


def _sync_product_checksums(
    layout: CursorLayout,
    plan: Dict[Path, PlannedFile],
) -> None:
    product_file = _read_planned_file(layout.product_json)
    has_bom = product_file.original.startswith(b"\xef\xbb\xbf")
    try:
        product = json.loads(product_file.original.decode("utf-8-sig"))
    except Exception as exc:
        raise SandToolError("product.json 无法解析，拒绝提交补丁") from exc
    if not isinstance(product, dict):
        raise SandToolError("product.json 顶层必须是对象")
    checksums = product.get("checksums")
    if not isinstance(checksums, dict):
        return

    out_root = (layout.app_root / "out").resolve()
    changed = False
    for key in list(checksums.keys()):
        if not isinstance(key, str):
            continue
        parts = [part for part in re.split(r"[\\/]", key) if part]
        target = out_root.joinpath(*parts).resolve()
        if not _is_within(target, out_root):
            raise SandToolError(f"product.json checksum 路径逃逸：{key}")
        planned = plan.get(target)
        if planned is not None:
            data = planned.next_bytes
        elif target.is_file():
            data = target.read_bytes()
        else:
            continue
        digest = _product_checksum(data)
        if checksums.get(key) != digest:
            checksums[key] = digest
            changed = True

    if not changed:
        return
    text = json.dumps(product, ensure_ascii=False, indent="\t")
    next_bytes = text.encode("utf-8")
    if has_bom:
        next_bytes = b"\xef\xbb\xbf" + next_bytes
    plan[layout.product_json] = PlannedFile(
        original=product_file.original,
        next_bytes=next_bytes,
        mode=product_file.mode,
    )


def _planned_extension_names(
    layout: CursorLayout,
    plan: Mapping[Path, PlannedFile],
) -> Set[str]:
    names: Set[str] = set()
    for file_path in plan:
        extension_name = _target_extension_name(layout, file_path)
        if extension_name:
            names.add(extension_name)
    return names


def _verify_extension_hashes(
    layout: CursorLayout,
    extension_names: Iterable[str],
) -> None:
    names = set(extension_names)
    if layout.ext_host_path is None or not names:
        return
    ext_content = _decode_js(layout.ext_host_path.read_bytes(), layout.ext_host_path)
    for rel, extension_name in TARGET_SPECS:
        if not extension_name or extension_name not in names:
            continue
        main_path = layout.app_root.joinpath(*rel.split("/"))
        if not main_path.is_file():
            continue
        extension_id = "anysphere." + extension_name
        if not _extension_hash_map_present(ext_content, extension_id):
            continue
        pattern = re.compile(
            rf'\"{re.escape(extension_id)}\"\s*:\s*\{{[\s\S]{{0,2400}}?'
            rf'\"main\.js\"\s*:\s*\"([0-9a-f]{{64}})\"'
        )
        match = pattern.search(ext_content)
        if not match:
            raise SandToolError(f"无法验证 {extension_id} 的内嵌哈希")
        expected = hashlib.sha256(main_path.read_bytes()).hexdigest()
        if match.group(1) != expected:
            raise SandToolError(f"{extension_id} 的内嵌哈希校验失败")


def _verify_product_checksums(layout: CursorLayout) -> int:
    product = json.loads(layout.product_json.read_bytes().decode("utf-8-sig"))
    checksums = product.get("checksums") if isinstance(product, dict) else None
    if not isinstance(checksums, dict):
        return 0
    out_root = (layout.app_root / "out").resolve()
    checked = 0
    for key, written in checksums.items():
        if not isinstance(key, str):
            continue
        parts = [part for part in re.split(r"[\\/]", key) if part]
        target = out_root.joinpath(*parts).resolve()
        if not _is_within(target, out_root) or not target.is_file():
            continue
        checked += 1
        if written != _product_checksum(target.read_bytes()):
            raise SandToolError(f"product.json 完整性哈希校验失败：{key}")
    return checked


def inspect_status(layout: CursorLayout) -> PatchStatus:
    client_markers = 0
    eligibility_markers = 0
    managed_local_route_markers = 0
    local_runtime_load_markers = 0
    direct_stream_markers = 0
    agent_host_enablement_markers = 0
    agent_host_identity_markers = 0
    move_exec_markers = 0
    legacy_client_markers = 0
    legacy_eligibility_markers = 0
    subagent_markers = 0
    legacy_subagent_markers = 0
    subagent_wake_markers = 0
    subagent_wake_anchors = 0
    task_tool_markers = 0
    foreign_task_tool_markers = 0
    ide_matches = 0
    external_sand_matches = 0
    external_marker_count = 0
    stream_capable = False
    patched_files: List[Path] = []
    for target in layout.target_paths:
        content = _decode_js(target.read_bytes(), target)
        if _content_has_stream_anchors(content) or (
            SAND_MANAGED_LOCAL_ROUTE_MARKER in content
            or SAND_LOCAL_RUNTIME_LOAD_MARKER in content
            or SAND_DIRECT_STREAM_MARKER in content
            or SAND_AGENT_HOST_ENABLEMENT_MARKER in content
            or SAND_AGENT_HOST_IDENTITY_MARKER in content
            or SAND_MOVE_EXEC_MARKER in content
        ):
            stream_capable = True
        client_count = (
            content.count(SAND_CLIENT_MARKER)
            + content.count(SAND_CLIENT_EXISTING_MARKER)
            + content.count(SAND_HDRFIX_V2_MARKER)
        )
        eligibility_count = content.count(SAND_ELIGIBILITY_MARKER)
        managed_local_route_count = content.count(SAND_MANAGED_LOCAL_ROUTE_MARKER)
        local_runtime_load_count = content.count(SAND_LOCAL_RUNTIME_LOAD_MARKER)
        direct_stream_count = content.count(SAND_DIRECT_STREAM_MARKER)
        agent_host_enablement_count = content.count(
            SAND_AGENT_HOST_ENABLEMENT_MARKER
        )
        agent_host_identity_count = content.count(
            SAND_AGENT_HOST_IDENTITY_MARKER
        )
        move_exec_count = _move_exec_marker_count(content)
        subagent_count = _subagent_marker_count(content)
        legacy_subagent_count = _legacy_subagent_marker_count(content)
        task_tool_markers += content.count(SAND_TASK_TOOL_MARKER)
        foreign_task_tool_markers += len(FOREIGN_TASK_TOOL_PROPS_RE.findall(content))
        wake_count = content.count(SAND_SUBAGENT_WAKE_MARKER)
        subagent_wake_anchors += _subagent_wake_anchor_count(content)
        legacy_client_count = len(
            re.findall(
                rf"([\"'])sand\1{LEGACY_CLIENT_MARKER_PATTERN}",
                content,
            )
        )
        legacy_eligibility_count = content.count(
            "return!1;" + LEGACY_SAND_ELIGIBILITY_MARKER
        )
        external_marker_count += max(
            0,
            len(re.findall(CLIENT_MARKER_GUARD_PATTERN, content))
            - client_count
            - legacy_client_count,
        )
        external_marker_count += max(
            0,
            len(re.findall(ELIGIBILITY_MARKER_GUARD_PATTERN, content))
            - eligibility_count
            - legacy_eligibility_count,
        )
        if (
            client_count
            + eligibility_count
            + legacy_client_count
            + legacy_eligibility_count
            + managed_local_route_count
            + local_runtime_load_count
            + direct_stream_count
            + agent_host_enablement_count
            + agent_host_identity_count
            + move_exec_count
            + subagent_count
            + legacy_subagent_count
            + wake_count
        ):
            patched_files.append(target)
        subagent_markers += subagent_count
        legacy_subagent_markers += legacy_subagent_count
        subagent_wake_markers += wake_count
        client_markers += client_count
        eligibility_markers += eligibility_count
        legacy_client_markers += legacy_client_count
        legacy_eligibility_markers += legacy_eligibility_count
        managed_local_route_markers += managed_local_route_count
        local_runtime_load_markers += local_runtime_load_count
        direct_stream_markers += direct_stream_count
        agent_host_enablement_markers += agent_host_enablement_count
        agent_host_identity_markers += agent_host_identity_count
        move_exec_markers += move_exec_count
        for _key, rule in CLIENT_RULES:
            for match in rule.finditer(content):
                if match.group(3) == "sand":
                    external_sand_matches += 1
                else:
                    ide_matches += 1
    return PatchStatus(
        client_markers=client_markers,
        eligibility_markers=eligibility_markers,
        ide_matches=ide_matches,
        external_sand_matches=external_sand_matches,
        external_marker_count=external_marker_count,
        legacy_client_markers=legacy_client_markers,
        legacy_eligibility_markers=legacy_eligibility_markers,
        patched_files=tuple(patched_files),
        managed_local_route_markers=managed_local_route_markers,
        local_runtime_load_markers=local_runtime_load_markers,
        direct_stream_markers=direct_stream_markers,
        agent_host_enablement_markers=agent_host_enablement_markers,
        agent_host_identity_markers=agent_host_identity_markers,
        move_exec_markers=move_exec_markers,
        stream_capable=stream_capable,
        remote_server=layout.is_remote_server,
        subagent_markers=subagent_markers,
        legacy_subagent_markers=legacy_subagent_markers,
        subagent_wake_markers=subagent_wake_markers,
        subagent_wake_anchors=subagent_wake_anchors,
        task_tool_markers=task_tool_markers,
        foreign_task_tool_markers=foreign_task_tool_markers,
    )


def _create_backup(
    layout: CursorLayout,
    plan: Mapping[Path, PlannedFile],
    operation: str,
) -> Tuple[Path, Dict[str, object]]:
    app_hash = hashlib.sha256(str(layout.app_root).encode("utf-8")).hexdigest()[:16]
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    backup_dir = _config_dir() / "backups" / app_hash / f"{stamp}-{operation}"
    files_dir = backup_dir / "files"
    entries: List[Dict[str, object]] = []
    for path, planned in plan.items():
        try:
            relative = path.resolve().relative_to(layout.app_root.resolve())
        except ValueError as exc:
            raise SandToolError(f"计划文件逃逸出 Cursor app：{path}") from exc
        backup_file = files_dir / relative
        _atomic_write(backup_file, planned.original, planned.mode)
        entries.append(
            {
                "path": relative.as_posix(),
                "originalSha256": _sha256(planned.original),
                "nextSha256": _sha256(planned.next_bytes),
                "mode": planned.mode,
            }
        )
    manifest: Dict[str, object] = {
        "version": 1,
        "toolVersion": TOOL_VERSION,
        "operation": operation,
        "status": "prepared",
        "appRoot": str(layout.app_root),
        "cursorVersion": layout.version,
        "createdAt": datetime.now(timezone.utc).isoformat(),
        "files": entries,
    }
    _write_json_atomic(backup_dir / "manifest.json", manifest)
    return backup_dir, manifest


def _update_backup_manifest(
    backup_dir: Path,
    manifest: Dict[str, object],
    status_value: str,
    error: Optional[str] = None,
) -> None:
    manifest["status"] = status_value
    manifest["finishedAt"] = datetime.now(timezone.utc).isoformat()
    if error:
        manifest["error"] = error[:1000]
    _write_json_atomic(backup_dir / "manifest.json", manifest)


def _commit_plan(
    layout: CursorLayout,
    plan: Mapping[Path, PlannedFile],
    operation: str,
    validator,
) -> Tuple[Tuple[Path, ...], Path]:
    if not plan:
        raise SandToolError("内部错误：提交计划为空")
    for path, planned in plan.items():
        if _sha256(path.read_bytes()) != _sha256(planned.original):
            raise SandToolError(f"文件在计划生成后发生变化，已停止操作：{path}")
    backup_dir, manifest = _create_backup(layout, plan, operation)
    attempted: List[Path] = []
    written: List[Path] = []
    try:
        for path, planned in plan.items():
            if _sha256(path.read_bytes()) != _sha256(planned.original):
                raise SandToolError(f"文件在写入前发生变化，已停止操作：{path}")
            attempted.append(path)
            _atomic_write(path, planned.next_bytes, planned.mode)
            written.append(path)
        validator()
        for path, planned in plan.items():
            if _sha256(path.read_bytes()) != _sha256(planned.next_bytes):
                raise SandToolError(f"写入后哈希校验失败：{path}")
        _update_backup_manifest(backup_dir, manifest, "committed")
        return tuple(written), backup_dir
    except (Exception, KeyboardInterrupt) as exc:
        rollback_errors: List[str] = []
        for path in reversed(attempted):
            planned = plan[path]
            try:
                current_hash = _sha256(path.read_bytes())
                original_hash = _sha256(planned.original)
                next_hash = _sha256(planned.next_bytes)
                if current_hash == original_hash:
                    continue
                if current_hash != next_hash:
                    rollback_errors.append(f"{path}: 文件已被外部修改，未覆盖")
                    continue
                _atomic_write(path, planned.original, planned.mode)
            except Exception as rollback_exc:
                rollback_errors.append(f"{path}: {rollback_exc}")
        message = str(exc)
        if rollback_errors:
            message += "; rollback errors: " + " | ".join(rollback_errors)
        try:
            _update_backup_manifest(backup_dir, manifest, "rolled_back", message)
        except Exception:
            pass
        if rollback_errors:
            raise SandToolError(
                "补丁失败且有文件未能自动回滚，请保留备份目录："
                f"{backup_dir}\n{message}"
            ) from exc
        raise


def _windows_close_cursor(layout: CursorLayout) -> int:
    """快速关闭 Cursor：taskkill 强杀进程树，然后轮询等它真正退出（释放单实例锁），
    否则随后的 start 会被判为「已有实例」而直接退出，表现为「打完补丁不重启」。"""
    name = Path(str(layout.executable)).name or "Cursor.exe"
    try:
        subprocess.run(
            ["taskkill", "/F", "/T", "/IM", name],
            capture_output=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        pass
    # 等进程真正消失（最多 5s，通常 1-2s），确保单实例锁释放。
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            result = subprocess.run(
                ["tasklist", "/FI", f"IMAGENAME eq {name}", "/NH"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            break
        if name.lower() not in (result.stdout or "").lower():
            break
        time.sleep(0.2)
    return 1


def _mac_bundle_pids(layout: CursorLayout) -> List[int]:
    bundle = _find_app_bundle(layout.app_root)
    if bundle is None:
        return []
    contents = (bundle.resolve() / "Contents").resolve()
    pids: List[int] = []
    for pid, executable in _mac_process_paths(strict=True):
        if pid != os.getpid() and _is_within(executable, contents):
            pids.append(pid)
    return pids


def _wait_for_mac_exit(layout: CursorLayout, timeout_seconds: float) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if not _mac_bundle_pids(layout):
            return True
        time.sleep(0.25)
    return not _mac_bundle_pids(layout)


def _mac_close_cursor(layout: CursorLayout) -> int:
    before = _mac_bundle_pids(layout)
    if not before:
        return 0
    selected_bundle = _find_app_bundle(layout.app_root)
    running_bundles: Dict[str, Path] = {}
    for _pid, executable in _mac_process_paths(strict=True):
        bundle = _bundle_for_executable(executable)
        if bundle is not None:
            running_bundles.setdefault(_path_key(bundle), bundle)
    if selected_bundle is not None and len(running_bundles) == 1:
        osascript = shutil.which("osascript") or "/usr/bin/osascript"
        try:
            subprocess.run(
                [
                    osascript,
                    "-e",
                    'tell application id "com.todesktop.230313mzl4w4u92" to quit',
                ],
                capture_output=True,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass
        if _wait_for_mac_exit(layout, 12):
            return len(before)

    for pid in _mac_bundle_pids(layout):
        try:
            os.kill(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
    if _wait_for_mac_exit(layout, 3):
        return len(before)

    for pid in _mac_bundle_pids(layout):
        try:
            os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    if not _wait_for_mac_exit(layout, 2):
        raise SandToolError("无法安全关闭所选 Cursor 进程，请手动退出后重试")
    return len(before)


def _linux_remaining_processes(
    expected: Mapping[int, _LinuxProcess],
) -> Set[int]:
    remaining: Set[int] = set()
    for pid, original in expected.items():
        try:
            _ppid, _pgid, _sid, state, start_time = _read_linux_proc_stat(
                Path("/proc") / str(pid) / "stat"
            )
        except (OSError, ValueError, IndexError):
            continue
        if state != "Z" and start_time == original.start_time:
            remaining.add(pid)
    return remaining


def _wait_for_linux_exit(
    expected: Mapping[int, _LinuxProcess],
    timeout_seconds: float,
) -> Set[int]:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        remaining = _linux_remaining_processes(expected)
        if not remaining:
            return set()
        time.sleep(0.25)
    return _linux_remaining_processes(expected)


def _signal_linux_processes(
    processes: Mapping[int, _LinuxProcess],
    pids: Set[int],
    sig: int,
    main_pids: Optional[Set[int]] = None,
) -> bool:
    permission_denied = False
    trusted_main_pids = main_pids if main_pids is not None else set()
    dedicated_groups = {
        process.pgid
        for pid in pids & trusted_main_pids
        for process in (processes[pid],)
        # 只信任被 cursor.mjs 精确匹配的主进程。Cursor 里的远程终端/
        # SSH 可能也是自己的 session leader，不应仅凭后代链整组误杀。
        if process.pgid == pid and process.sid == pid
    }
    for pgid in dedicated_groups:
        try:
            os.killpg(pgid, sig)
        except ProcessLookupError:
            pass
        except PermissionError:
            permission_denied = True

    grouped = {
        pid for pid in pids if processes[pid].pgid in dedicated_groups
    }
    for pid in sorted(pids - grouped, reverse=True):
        try:
            os.kill(pid, sig)
        except ProcessLookupError:
            pass
        except PermissionError:
            permission_denied = True
    return permission_denied


def _linux_close_cursor(layout: CursorLayout) -> int:
    processes, selected = _linux_cursor_processes(layout, strict=True)
    if not selected:
        return 0
    if os.getpid() in selected:
        raise SandToolError(
            "当前工具运行在所选 Cursor 的进程树中。"
            "请关闭 Cursor，并改用系统独立终端重试"
        )

    expected = {pid: processes[pid] for pid in selected}
    main_pids = _linux_main_pids(layout, processes) & selected
    permission_denied = _signal_linux_processes(
        processes, selected, signal.SIGTERM, main_pids
    )
    remaining = _wait_for_linux_exit(expected, 10)
    if remaining:
        permission_denied = (
            _signal_linux_processes(processes, remaining, signal.SIGKILL, main_pids)
            or permission_denied
        )
        remaining = _wait_for_linux_exit(
            {pid: expected[pid] for pid in remaining}, 3
        )
    if remaining:
        detail = "当前用户无权限关闭该进程；" if permission_denied else ""
        raise SandToolError(
            f"{detail}无法安全关闭所选 Cursor 进程（PID: "
            + ", ".join(str(pid) for pid in sorted(remaining))
            + "）。请在系统独立终端使用 sudo 重试"
        )
    return len(expected)


def close_cursor(layout: CursorLayout) -> int:
    if sys.platform == "win32":
        return _windows_close_cursor(layout)
    if sys.platform == "darwin":
        return _mac_close_cursor(layout)
    if sys.platform.startswith("linux"):
        return _linux_close_cursor(layout)
    raise SandToolError("当前仅支持 Windows、macOS 和 Linux")


# 启动参数：--classic 让 Cursor 直接进经典 IDE/编辑器窗口，跳过新版 Agents 中枢窗口。
# （官方设置「Open Agents Window on startup / Window Restoration」有会循环回 Agents 窗口的已知 bug，
#  --classic 启动参数是稳定绕过方式。）
CURSOR_START_ARGS: Tuple[str, ...] = ("--classic",)


def _linux_start_command(
    layout: CursorLayout,
) -> Optional[Tuple[List[str], Mapping[str, str]]]:
    command = [str(layout.executable), *CURSOR_START_ARGS]
    environment = os.environ.copy()
    if not hasattr(os, "geteuid") or os.geteuid() != 0:
        return command, environment

    account = _linux_invoking_account()
    if account is None or account.pw_uid == 0:
        print_warn(
            "Cursor 未自动重启：无法确定原登录用户，为避免以 root 运行 GUI，"
            "请回到普通用户桌面后手动启动 Cursor。"
        )
        return None

    environment.update(
        {
            "HOME": account.pw_dir,
            "USER": account.pw_name,
            "LOGNAME": account.pw_name,
            "XDG_RUNTIME_DIR": f"/run/user/{account.pw_uid}",
        }
    )
    bus_path = Path(environment["XDG_RUNTIME_DIR"]) / "bus"
    if bus_path.exists():
        # sudo 有时会保留 root 的 DBUS_SESSION_BUS_ADDRESS，必须换成桌面用户的 bus。
        environment["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path={bus_path}"
    else:
        environment.pop("DBUS_SESSION_BUS_ADDRESS", None)
    if not environment.get("XAUTHORITY"):
        xauthority = Path(account.pw_dir) / ".Xauthority"
        if xauthority.is_file():
            environment["XAUTHORITY"] = str(xauthority)
    for variable in ("SUDO_UID", "SUDO_GID", "SUDO_USER", "SUDO_COMMAND", "PKEXEC_UID"):
        environment.pop(variable, None)

    runuser = shutil.which("runuser")
    if runuser:
        return [runuser, "-m", "-u", account.pw_name, "--", *command], environment
    sudo = shutil.which("sudo")
    if sudo:
        return [sudo, "-n", "-H", "-u", account.pw_name, "--", *command], environment
    print_warn(
        "Cursor 未自动重启：系统缺少 runuser/sudo，且不会以 root 启动 GUI。"
        "请以普通用户手动启动 Cursor。"
    )
    return None


def start_cursor(layout: CursorLayout) -> bool:
    try:
        if sys.platform == "win32":
            exe = str(layout.executable)
            try:
                # 带 --classic 直接进 IDE；CREATE_NEW_PROCESS_GROUP 让 Cursor 脱离本工具独立存活。
                subprocess.Popen(
                    [exe, *CURSOR_START_ARGS],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    close_fds=True,
                    creationflags=0x00000200,  # CREATE_NEW_PROCESS_GROUP
                )
            except OSError:
                # 回退：无参数双击式启动（至少能拉起 Cursor）。
                os.startfile(exe)  # noqa: S606
            return True
        if sys.platform == "darwin":
            bundle = _find_app_bundle(layout.app_root)
            if bundle is None:
                return False
            subprocess.run(
                [shutil.which("open") or "/usr/bin/open", "-a", str(bundle), "--args", *CURSOR_START_ARGS],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=20,
                check=False,
            )
            return True
        if sys.platform.startswith("linux"):
            if layout.is_remote_server:
                # 服务端由本机 Cursor 客户端在重连时自动拉起，这里不能手动裸启。
                print_warn(
                    "已关闭 Remote SSH 服务端进程；请在本机 Cursor 里重新连接该主机"
                    "（或执行「Reload Window」）以加载补丁后的服务端。"
                )
                return False
            launch = _linux_start_command(layout)
            if launch is None:
                return False
            command, environment = launch
            subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
                start_new_session=True,
                env=environment,
            )
            return True
    except (OSError, subprocess.TimeoutExpired):
        return False
    return False


def _build_install_plan(
    layout: CursorLayout,
) -> Tuple[Dict[Path, PlannedFile], PatchStats]:
    plan: Dict[Path, PlannedFile] = {}
    total = PatchStats()
    for target in layout.target_paths:
        original = _read_planned_file(target)
        content = _decode_js(original.original, target)
        # 先剥旧 RPC/会员片段，避免 apply_patch 把注入脚本里的旧路径常量改掉。
        content, _ = _strip_rpc_snippets(content)
        if target.name in MEMBERSHIP_TARGET_NAMES:
            content = MEMBERSHIP_SNIPPET_RE.sub("", content)
        next_content, stats = apply_patch_to_content(content)
        if target.name in MEMBERSHIP_TARGET_NAMES:
            next_content = SAND_MEMBERSHIP_SNIPPET + next_content
        if next_content != content:
            plan[target] = PlannedFile(
                original=original.original,
                next_bytes=next_content.encode("utf-8"),
                mode=original.mode,
            )
        total.is_glass += stats.is_glass
        total.object_header += stats.object_header
        total.set_header += stats.set_header
        total.eligibility += stats.eligibility
        total.model_unlock += stats.model_unlock
        total.adopted_sand += stats.adopted_sand
        total.migrated_client += stats.migrated_client
        total.migrated_eligibility += stats.migrated_eligibility
        total.rpc_rewrite += stats.rpc_rewrite
        total.managed_local_route += stats.managed_local_route
        total.local_runtime_load += stats.local_runtime_load
        total.direct_stream += stats.direct_stream
        total.agent_host_enablement += stats.agent_host_enablement
        total.agent_host_identity += stats.agent_host_identity
        total.move_exec += stats.move_exec
    if plan:
        _update_extension_hashes(layout, plan)
        _sync_product_checksums(layout, plan)
    return plan, total


def _build_uninstall_plan(
    layout: CursorLayout,
) -> Tuple[Dict[Path, PlannedFile], RemoveStats]:
    plan: Dict[Path, PlannedFile] = {}
    total = RemoveStats()
    for target in layout.target_paths:
        original = _read_planned_file(target)
        content = _decode_js(original.original, target)
        next_content, stats = remove_patch_from_content(content)
        if next_content != content:
            plan[target] = PlannedFile(
                original=original.original,
                next_bytes=next_content.encode("utf-8"),
                mode=original.mode,
            )
        total.client_type += stats.client_type
        total.eligibility += stats.eligibility
        total.rpc_rewrite += stats.rpc_rewrite
        total.managed_local_route += stats.managed_local_route
        total.local_runtime_load += stats.local_runtime_load
        total.direct_stream += stats.direct_stream
        total.agent_host_enablement += stats.agent_host_enablement
        total.agent_host_identity += stats.agent_host_identity
        total.move_exec += stats.move_exec
    if plan:
        _update_extension_hashes(layout, plan)
        _sync_product_checksums(layout, plan)
    return plan, total


def _mac_seal(layout: CursorLayout) -> None:
    """macOS：改完 Cursor.app 内文件后清除扩展属性并 ad-hoc 重签名，否则系统会因签名失效拒绝启动。"""
    if sys.platform != "darwin":
        return
    bundle = _find_app_bundle(layout.app_root)
    if bundle is None:
        return
    bundle_str = str(bundle)
    for file, args in (
        ("xattr", ["-cr", bundle_str]),
        ("codesign", ["--force", "--deep", "--sign", "-", bundle_str]),
    ):
        exe = shutil.which(file) or file
        try:
            subprocess.run(
                [exe, *args],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=180,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass


def install(layout: CursorLayout) -> int:
    before = inspect_status(layout)
    if before.external_marker_count:
        raise SandToolError(
            "检测到其他 Sand 模式标记，本脚本不会接管或覆盖它；"
            "请先用原安装方式卸载"
        )
    plan, _stats = _build_install_plan(layout)
    if not plan:
        if before.installed and (not before.stream_capable or before.stream_mode_installed):
            close_cursor(layout)
            start_cursor(layout)
            return 0
        raise SandToolError("当前 Cursor 版本未匹配到 Sand 客户端模式规则")
    stream_hits = (
        before.managed_local_route_markers + _stats.managed_local_route,
        before.local_runtime_load_markers + _stats.local_runtime_load,
        before.agent_host_identity_markers + _stats.agent_host_identity,
        before.agent_host_enablement_markers + _stats.agent_host_enablement,
    )
    # 旧版强制 move_exec 的残留本身就说明这是一份 Stream 安装（正在被升级还原）。
    stream_capable = before.stream_capable or any(stream_hits) or _stats.move_exec > 0
    # 只要四类锚点各命中 ≥1 就算完整（保持「全有或全无」防半装挂起）。
    # 不再要求精确 (1,1,1,2)：agentHost 在只有 desktop（无 glass）的安装上会是 1，
    # 不同 commit 的 chunk 拆分也可能让某类命中数漂移，硬相等会误杀合法安装。
    # move_exec 门控 1.1.10 起保持官方值，不再是锚点。
    # Remote SSH 服务端没有 workbench，agentHost enablement 锚点由本机客户端提供，不在此要求。
    required_hits = stream_hits[:3] if layout.is_remote_server else stream_hits
    if stream_capable and not all(hit >= 1 for hit in required_hits):
        raise SandToolError(
            "当前 Cursor 未完整匹配 Sand Stream 规则（有锚点缺失，拒绝半装）："
            f"route={stream_hits[0]}, "
            f"runtimeLoad={stream_hits[1]}, "
            f"identity={stream_hits[2]}, "
            f"agentHost={stream_hits[3]}"
        )

    # 子代理组同样「全有或全无」：五类 agent-host 锚点要齐，且本地运行时必须真有
    # move_exec 执行器 / 子代理运行器 / Task 工具工厂这三条代码路径，否则注入的 Task 工具
    # 只会在调用时失败。命中数按补丁后的目标内容统计，已打过的旧版也能正确判断。
    subagent_total = (
        before.subagent_markers + before.legacy_subagent_markers + _stats.subagent
    )
    if stream_capable and subagent_total:
        merged_contents = [
            _decode_js(
                plan[target].next_bytes if target in plan else target.read_bytes(), target
            )
            for target in layout.target_paths
        ]
        marker_hits = {
            marker: sum(text.count(marker) for text in merged_contents)
            for marker in SUBAGENT_MARKERS
        }
        # Task 工具槽位也可由其他 Sand 工具的注入满足（本工具此时不叠加注入）。
        foreign_task_tool_hits = sum(
            len(FOREIGN_TASK_TOOL_PROPS_RE.findall(text)) for text in merged_contents
        )
        if foreign_task_tool_hits and marker_hits.get(SAND_TASK_TOOL_MARKER) == 0:
            marker_hits[SAND_TASK_TOOL_MARKER] = foreign_task_tool_hits
        missing_markers = [m for m, n in marker_hits.items() if n == 0]
        # 就绪锚点只看 agent-host 扩展：workbench 渲染包里也打包了同一份 agent 运行时代码，
        # 但 Task 工具实际在扩展宿主里执行，渲染包命中不代表运行链可用。
        agent_host_dir = layout.app_root.joinpath(*AGENT_HOST_DIST_REL.split("/")).resolve()
        readiness = _subagent_readiness(
            text
            for target, text in zip(layout.target_paths, merged_contents)
            if _is_within(target, agent_host_dir)
        )
        missing_ready = [name for name, n in readiness.items() if n == 0]
        if missing_markers or missing_ready:
            raise SandToolError(
                "当前 Cursor 未完整匹配子代理规则（拒绝半装）："
                + ", ".join(f"{m}={n}" for m, n in marker_hits.items())
                + "; "
                + ", ".join(f"{k}={v}" for k, v in readiness.items())
            )

    close_cursor(layout)
    changed_extensions = _planned_extension_names(layout, plan)

    def validate() -> None:
        status = inspect_status(layout)
        if (
            not status.installed
            or status.ide_matches != 0
            or status.external_marker_count != 0
            or status.legacy_client_markers != 0
            or status.legacy_eligibility_markers != 0
            or status.legacy_subagent_markers != 0
            or status.legacy_move_exec_forced
            or (stream_capable and not status.stream_mode_installed)
            or (stream_capable and subagent_total and not status.subagent_installed)
            or (stream_capable and subagent_total and not status.subagent_wake_installed)
        ):
            raise SandToolError(
                "安装后状态校验失败："
                f"markers={status.client_markers + status.eligibility_markers}, "
                f"remainingIde={status.ide_matches}, "
                f"streamMode={status.stream_mode_installed}, "
                f"subagent={status.subagent_markers}/{len(SUBAGENT_MARKERS)}, "
                f"wake={status.subagent_wake_markers}/{status.subagent_wake_anchors}, "
                f"legacyMoveExec={status.move_exec_markers}, "
                "remainingLegacy="
                f"{status.legacy_client_markers + status.legacy_eligibility_markers + status.legacy_subagent_markers}"
            )
        _verify_extension_hashes(layout, changed_extensions)
        _verify_product_checksums(layout)

    _commit_plan(layout, plan, "install", validate)
    _mac_seal(layout)
    close_cursor(layout)
    start_cursor(layout)
    return 0


def uninstall(layout: CursorLayout) -> int:
    before = inspect_status(layout)
    if before.external_marker_count:
        raise SandToolError(
            "检测到无法识别的 Sand 模式标记，拒绝修改；"
            "请先用原安装方式卸载"
        )
    plan, _stats = _build_uninstall_plan(layout)
    if not plan:
        start_cursor(layout)
        return 0

    close_cursor(layout)
    changed_extensions = _planned_extension_names(layout, plan)

    def validate() -> None:
        status = inspect_status(layout)
        if status.installed or status.external_marker_count:
            raise SandToolError(
                "卸载后仍有 Sand marker："
                f"{status.client_markers + status.eligibility_markers}，"
                f"external={status.external_marker_count}"
            )
        _verify_extension_hashes(layout, changed_extensions)
        _verify_product_checksums(layout)

    _commit_plan(layout, plan, "uninstall", validate)
    _mac_seal(layout)
    close_cursor(layout)
    start_cursor(layout)
    return 0


def _permission_hint() -> str:
    script = Path(__file__).resolve()
    if sys.platform == "win32":
        return "请右键以管理员身份打开 PowerShell/终端后重新运行命令。"
    if sys.platform.startswith("linux"):
        # -E 保留 SAND_CURSOR_INSTALL_DIR 等环境变量；-H 让 sudo 的 HOME 指向 root，
        # 配置目录仍会按 SUDO_USER 回到原用户。
        return (
            f'请在普通用户桌面的独立终端重试：'
            f'sudo -E -H "{sys.executable}" "{script}" <命令>'
            "（补丁完成后工具会尝试恢复为原用户重启 Cursor）"
        )
    return f'请使用管理员权限重试：sudo python3 "{script}" <命令>'


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Cursor Sand 客户端模式安装/卸载工具（Windows / macOS / Linux）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  python \"Sand客户端模式安装工具.py\" install\n"
            "  python \"Sand客户端模式安装工具.py\" uninstall\n"
            "  python \"Sand客户端模式安装工具.py\" set-path \"E:\\Development\\IDE\\cursor\"\n"
            "  python3 \"Sand客户端模式安装工具.py\" set-path /Applications/Cursor.app\n"
            "  sudo -H python3 \"Sand客户端模式安装工具.py\" set-path /usr/share/cursor\n"
            "  python \"Sand客户端模式安装工具.py\" set-path auto"
        ),
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {TOOL_VERSION}")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("install", help="安装/注入 Sand 客户端模式")
    commands.add_parser("uninstall", help="卸载 Sand 客户端模式")
    set_path = commands.add_parser("set-path", help="设置 Cursor 路径；auto 恢复自动检测")
    set_path.add_argument(
        "path",
        help="Cursor.exe、Cursor.app、Linux cursor 启动器、resources/app、安装根目录，或 auto",
    )
    return parser


def collect_status_lines() -> List[Tuple[str, str]]:
    try:
        layout = resolve_cursor_layout()
        status = inspect_status(layout)
    except SandToolError as exc:
        return [(str(exc), ANSI_YELLOW)]

    kind = "（Remote SSH 服务端）" if layout.is_remote_server else ""
    lines: List[Tuple[str, str]] = [
        (f"Cursor {layout.version}{kind}：{layout.install_root}", ANSI_BLUE)
    ]
    if status.stream_mode_installed:
        lines.append(("Stream 模式已启用", ANSI_GREEN))
        if status.legacy_move_exec_forced:
            lines.append(
                (
                    "检测到旧版强制 move_exec 补丁：每条消息首 token 会多等约 10 秒，"
                    "且 Cursor Rules 不会生效；运行 install 升级即可去掉",
                    ANSI_YELLOW,
                )
            )
        if status.subagent_installed:
            lines.append(("子代理（Task 工具）与 Multitask 模式已启用", ANSI_GREEN))
            if status.task_tool_from_foreign:
                lines.append(
                    (
                        "Task 工具配置由其他 Sand 工具提供，本工具未叠加注入（卸载时也不会触碰）",
                        ANSI_BLUE,
                    )
                )
            if status.legacy_subagent_markers:
                lines.append(
                    (
                        "检测到旧版子代理注入残留（与其他工具的注入拼在一起时 Task 工具配置会整体失效），"
                        "运行 install 清理",
                        ANSI_YELLOW,
                    )
                )
            if not status.subagent_wake_installed:
                lines.append(
                    (
                        "后台子代理完成唤醒未启用"
                        f"（{status.subagent_wake_markers}/{status.subagent_wake_anchors}），"
                        "运行 install 可补齐",
                        ANSI_YELLOW,
                    )
                )
        elif status.legacy_subagent_markers:
            lines.append(
                (
                    "子代理补丁为旧版（子代理会带着 -thinking/-max 复合模型名请求，"
                    "启动即报 Unknown model ID），运行 install 升级",
                    ANSI_YELLOW,
                )
            )
        elif status.subagent_markers:
            lines.append(
                (
                    f"子代理补丁不完整：{status.subagent_markers}/{len(SUBAGENT_MARKERS)}，"
                    "请重新运行 install",
                    ANSI_YELLOW,
                )
            )
        else:
            lines.append(("子代理（Task 工具）未启用，运行 install 可补齐", ANSI_YELLOW))
    elif status.installed:
        lines.append(("已安装 Sand 客户端模式（非 Stream 回路）", ANSI_YELLOW))
    else:
        lines.append(("尚未安装 Sand 客户端模式", ANSI_YELLOW))
    if not status.stream_capable:
        lines.append(
            (
                "本机 Cursor 没有 3.18.9 agent-host 锚点，无法启用官方 Stream 回路",
                ANSI_YELLOW,
            )
        )
    if status.external_marker_count:
        lines.append(
            (f"检测到其他工具留下的标记：{status.external_marker_count} 处", ANSI_YELLOW)
        )
    return lines


def print_banner() -> None:
    print(colorize("使用前请确保当前 Cursor 账号已经获得 Sand 资格", ANSI_YELLOW))
    print(colorize(f"官方领取页面：{SAND_ONBOARDING_URL}", ANSI_BLUE))
    for text, code in collect_status_lines():
        print(colorize(text, code))
    print()


def apply_set_path(value: str) -> int:
    save_cursor_path(value)
    return 0


def print_menu() -> None:
    print(colorize("请选择操作：", ANSI_BOLD))
    print(colorize("  1", ANSI_BOLD, ANSI_GREEN) + ") 安装")
    print(colorize("  2", ANSI_BOLD, ANSI_GREEN) + ") 卸载")
    print(colorize("  3", ANSI_BOLD, ANSI_GREEN) + ") 设置 Cursor 路径")


def prompt_set_path() -> int:
    value = input(colorize("路径> ", ANSI_BLUE)).strip()
    if not value:
        return 0
    with LoadingSpinner("正在设置路径"):
        return apply_set_path(value)


def run_choice(choice: str) -> Optional[int]:
    if choice == "1":
        with LoadingSpinner("正在安装"):
            return install(resolve_cursor_layout())
    if choice == "2":
        with LoadingSpinner("正在卸载"):
            return uninstall(resolve_cursor_layout())
    if choice == "3":
        return prompt_set_path()
    print_warn("无效选项，请输入 1-3。")
    return 0


def interactive_loop() -> int:
    while True:
        print_banner()
        print_menu()
        try:
            choice = input(colorize("请输入编号> ", ANSI_BLUE)).strip()
        except EOFError:
            print()
            return 0
        try:
            run_choice(choice)
        except PermissionError as exc:
            print_error(f"错误：没有写入权限：{exc}")
            print_error(_permission_hint())
        except SandToolError as exc:
            print_error(f"错误：{exc}")
        except KeyboardInterrupt:
            print()
            return 0
        except Exception as exc:
            print_error(f"未预期错误：{exc}")
        print()


def main(argv: Optional[Sequence[str]] = None) -> int:
    _configure_console()
    args_list = list(sys.argv[1:] if argv is None else argv)
    try:
        _platform_name()
        if not args_list:
            return interactive_loop()

        print_banner()
        args = build_parser().parse_args(args_list)
        if args.command == "set-path":
            return apply_set_path(args.path)

        layout = resolve_cursor_layout()
        if args.command == "install":
            return install(layout)
        if args.command == "uninstall":
            return uninstall(layout)
        raise SandToolError(f"未知命令：{args.command}")
    except PermissionError as exc:
        print_error(f"错误：没有写入权限：{exc}")
        print_error(_permission_hint())
        return 3
    except SandToolError as exc:
        print_error(f"错误：{exc}")
        return 2
    except KeyboardInterrupt:
        print_error("操作已取消。")
        return 130
    except Exception as exc:
        print_error(f"未预期错误：{exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
