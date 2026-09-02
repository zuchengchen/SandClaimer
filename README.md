# Sand 资格领取器（SandClaimer）

批量给 Cursor 账号领取 **Grok Bot（Sand）** 资格的桌面小工具。iOS 玻璃浅蓝风界面，自动识别两种 token 格式，支持导入 JSON、批量领取、批量添加账号。

## 功能

- **两种 token 自动识别**：`access_token`（JWT，`eyJ...`）与 `ws token`（`user_01XXXX::eyJ...`，即 WorkosCursorSessionToken）。
- **导入方式**：直接粘贴（每行一个，可混排）、粘贴 `cursor_accounts_*.json` 内容、或「导入文件」选一个/多个 JSON。按 user id 自动去重。
- **批量领取**：逐个领取并实时显示每行状态；已开通短路、团队号自动带 `teamId`、个人号走试用、免费号标记「需绑卡」。
- **刷新状态**：只读查询每个账号的 Sand 额度与是否开通。
- **当前账号标识**：列表会标出 Cursor 当前实际登录的账号；该行的「Bot 周用量」就是当前客户端会消耗的额度。账号身份以 `cursorAuth/accessToken` 解析出的 user id 为准，旧的邮箱缓存不一致时会明确警告。切号后可点「探测本机账号」复核。
- **绕过本机 DNS 劫持**：内置 DoH（1.1.1.1）解析 `cursor.com` / `api2.cursor.sh` 真实 IP，即使本机跑着会劫持这些域名的网关（如 cgw）也能直连真实 Cursor。

## Linux 运行（推荐）

Linux 桌面系统可直接使用源码运行。在项目根目录打开终端：

```bash
chmod +x install-linux.sh start-linux.sh
./install-linux.sh       # 首次运行：创建 .venv-linux 并安装依赖
./start-linux.sh         # 以 GTK/WebKitGTK 后端启动
```

`start-linux.sh` 会自动检测 Python 和图形后端，并在需要时调用系统包管理器安装 GTK/WebKitGTK。它优先使用 `/usr/bin/python3` 并以 `--system-site-packages` 创建虚拟环境，这样可复用发行版提供的 PyGObject 绑定。如果系统无法提供 GTK/WebKitGTK，安装脚本会尝试安装 Qt 6 WebEngine 作备用后端。

支持通过环境变量覆盖默认行为：

```bash
SANDCLAIMER_BACKEND=qt ./start-linux.sh   # 强制 Qt WebEngine
SANDCLAIMER_BACKEND=gtk ./start-linux.sh  # 强制 GTK/WebKitGTK
SANDCLAIMER_PYTHON=/usr/bin/python3 ./install-linux.sh
```

Linux 需要图形桌面会话（`DISPLAY` 或 `WAYLAND_DISPLAY`）。“网页领取”功能还需系统已安装 Chromium、Chrome 或 Edge；主程序的其他功能不受影响。Linux 下本机 Cursor 安装目录常见为 `/usr/share/cursor` 或 `~/.local/share/cursor`。

### Linux 依赖问题

- Arch/Manjaro：`python-gobject`、`gtk3` 和 `webkit2gtk-4.1`。
- Debian/Ubuntu：`python3-gi`、`python3-venv`、`gir1.2-gtk-3.0` 和 `gir1.2-webkit2-4.1`（旧版本可使用 `gir1.2-webkit2-4.0`）。
- Fedora：`python3-gobject`、`gtk3` 和 `webkit2gtk4.1`。

安装脚本不会保存或记录 sudo 密码；需要提权时由 sudo 在终端中交互提示。如果不希望脚本修改系统包，可设 `SANDCLAIMER_NO_SYSTEM_PACKAGES=1`，脚本将直接尝试 Qt WebEngine 后端。

## Windows 运行（开发）

```bat
python -m pip install -r requirements.txt
python app.py
```

> Windows 需要 **Edge WebView2 运行时**（Win10/11 一般自带；缺失时到微软官网装「Evergreen WebView2 Runtime」）。

## 打包（Nuitka 编译 + 安装包）

双击或命令行运行：

```bat
build.bat
```

产物：

- `nuitka-out\SandClaimer-<版本>.exe` —— 单文件绿色版，双击即用（文件名带版本号，如 `SandClaimer-1.1.6.exe`）。
- `installer\SandClaimer-Setup-<版本>.exe` —— 中文安装向导，装到 Program Files 并建开始菜单/桌面快捷方式。

> 版本号统一取自 `sand_patch.py` 的 `TOOL_VERSION`，`build.bat` / `make_share.ps1` 会自动读取并写进产物文件名，无需多处手改。

`build.bat` 会依次：装依赖 → 修补 Nuitka 的 pywebview 插件 → 生成图标 → Nuitka 编译 → Inno Setup 打安装包。

### 为什么用 Nuitka（而非 PyInstaller）

- **启动更快**：Python 源码被编译成 C/机器码，不是解释执行的 `.pyc`。
- **天然混淆/加密**：产物是原生机器码，源码不可还原；onefile 运行时把负载解压到临时目录再执行（相当于加密封装），比 PyInstaller 的可直接解包 `.pyc` 强得多。
- `build.bat` 用 `--mingw64 --assume-yes-for-downloads`：首次编译 Nuitka 会自动下载并缓存 MinGW64，无需手动装 MSVC；之后走缓存会快很多。

> `patch_plugin.py`：Nuitka 4.1.3 的 pywebview 插件在 Windows 白名单里漏了 pywebview 6.2.x 新增的 `webview.platforms.win32`，会导致打包后 winforms 后端起不来。该脚本幂等地把它补进白名单，`build.bat` 已自动调用。
>
> `ChineseSimplified.isl`：安装向导的简体中文语言包（Inno Setup 默认不含）。

## Cursor 补丁（`sand_patch.py`）

补丁分三组，全部带 marker、可按字节精确回退（`uninstall` 后与官方原文件一致）：

| 组 | 作用 | 落在哪 |
|---|---|---|
| 客户端身份 / 会员伪装 | Sand 身份、资格判定、会员/模型列表伪装 | 渲染层 + 扩展宿主 |
| Stream 回路 | managed-local 路由、本地 runtime、agent-host sand 身份 | `cursor-agent-host` 等扩展 |
| 子代理 | Task 工具、resume / summarize / 后台完成动作放行、Multitask / Plan 等模式放行、后台子代理完成唤醒 | agent-host 扩展 + 渲染层 |

- 官方 managed-local 门控只放行 Agent 模式且拒绝 simulated 消息；「Start Multitasking」「Build in Parallel」都是 `mode=multitask` 的 simulated 消息，所以需要子代理组才能在 Sand 号上用 Multitask。
- 子代理组是「全有或全无」：五类 agent-host 锚点与运行链就绪锚点（cursor-agent-exec 共享运行时 / 子代理运行器 / Task 工具工厂）任一缺失就拒绝安装，避免注入一个永远失败的 Task 工具。
- 兼容 Sand Stream Toolkit 1.2.x 打过的安装：旧 marker（`ACTION_ROUTE_V1`、`TASK_TOOL_V1`、`TASK_TOOL_V2`）会被识别并原地升级，状态栏提示「旧版，运行 install 升级」。
- **1.1.11 修复子代理模型 ID**（`TASK_TOOL_V3`）。≤1.1.10 把 `createAgentConfig` 里解析后的复合 slug（如 `claude-fable-5-1-thinking-max`，thinking / max 已拼进名字）当成子代理的 `requestedModel.modelId`，而服务端只认基础 ID（`claude-fable-5-1`，thinking / effort / max 走 `parameters` 与 `maxMode`）——子代理一启动就 `ERROR_BAD_MODEL_NAME`「AI Model Not Found: Unknown model ID: claude-fable-5-1-thinking-max」，Multitask 全部失败而主对话正常。V3 改为沿用父请求的 `requestedModel.modelId`。重跑一次 `install` 升级，重启 Cursor 后 Agent Host 日志里子代理那条「Selected Agent Host turn runtime」的 `modelId` 应与父对话一致。
- 与其他 Sand 工具共存：若 `taskToolProps` 锚点已被其他工具注入（如 Toolkit 的 `SAND_SUBAGENT_TASK_PROPS_V2`，它同样使用 `requestedModel.modelId`），本工具不再叠加注入、卸载时也不触碰它，状态栏提示「Task 工具配置由其他 Sand 工具提供」。此前两段注入若被拼接在一起（Toolkit 把本工具旧版注入开头的 `taskToolProps:void 0` 当成官方原文替换，留下 `!==e.runOptions.subagentTypeName?void 0:{...}` 残尾），整个表达式会恒为 `void 0`，父对话拿不到任何 Task 工具配置；`install` / `uninstall` 会先把这段残尾摘掉。
- **1.1.10 起不再强制 `move_exec`**（`cursor_agent_host_move_exec` 门控保持官方值）。1.1.4–1.1.9 把它强制为开，agent-host 便不再激活 `cursor-agent-exec` 运行时，而它是唯一向 workbench 推送 Cursor Rules / Agent Skills / 自定义子代理的组件；workbench 每条消息都要等这份推送，等不到就 10 秒超时——表现为**每条消息首 token 固定多等约 10 秒**（`requestTraces` 中 `buildFromPushedData=10006ms`），且 `.cursor/rules`、User Rules 从不进 prompt。旧安装重跑一次 `install` 即可去掉（状态栏会提示「旧版 move_exec 强制仍在」）。

### Remote SSH 服务端

Cursor 通过 SSH 远程开发时，agent-host 等扩展在**远端**的 `~/.cursor-server/bin/<os>-<arch>/<commit>/` 里运行，远端也必须打补丁，否则 Sand 号会报 unpaid invoice。该布局没有渲染层，工具会自动跳过渲染层规则：

```bash
SAND_CURSOR_INSTALL_DIR=~/.cursor-server/bin/linux-x64/<commit> python3 sand_patch.py install
```

打完后在本机 Cursor 里对该主机执行「Reload Window」。纯服务器（没装桌面版）会自动发现服务端目录，无需环境变量。Cursor 升级后 `<commit>` 目录会变，需重新打。

### Linux 路径发现

自动扫描官方 `.deb`/tar、发行版包、Snap、Flatpak、`~/.local` 及 XDG `.desktop` 启动项；AppImage 需先 `--appimage-extract` 再把路径指向解压目录。`sudo` 提权时配置与扫描仍以原登录用户的 HOME 为准。

## 领取规则（与 Cursor 官方一致）

- **付费账号**（Pro+ / Ultra / Team）：直接开通，无需绑卡。
- **免费账号**：领取需先验证信用卡，工具会标记「需绑卡」（如返回验证链接会一并给出）。
- **团队账号**：走团队通道并自动带上 `teamId`（从 `get-me` 读取）。团队级开通是否覆盖全部成员座位，取决于 Cursor 侧策略。

## 用到的官方接口（均实测确认）

| 用途 | 方法 | 端点 | 鉴权 |
|---|---|---|---|
| 查额度 | POST | `api2.cursor.sh/aiserver.v1.DashboardService/GetSandUsageStatus` | Bearer accessToken |
| 查资格 | POST | `cursor.com/api/dashboard/get-sand-access-status` | 会话 cookie |
| 取 teamId | POST | `cursor.com/api/dashboard/get-me` | 会话 cookie |
| 个人领取 | POST | `cursor.com/api/dashboard/start-sand-trial` | cookie + Origin |
| 团队领取 | POST | `cursor.com/api/dashboard/request-sand-team-access`（body `{teamId}`） | cookie + Origin |

## 安全

- token 只在本机内存与本机↔Cursor 官方之间使用，不上传任何第三方服务。
- 请勿把含 token 的 JSON 或本工具日志分享给他人。

## 项目结构

```
sand-claimer/
├─ app.py                # pywebview 入口 + JS 桥接
├─ sand_api.py           # Cursor Sand 查询/领取
├─ accounts.py           # token/JSON 导入与账号表
├─ sand_patch.py         # Cursor 客户端模式补丁 / 回退（桌面版 + Remote SSH 服务端）
├─ resolve.py            # DoH 绕过 DNS 劫持
├─ web/                  # 玻璃风 UI（index.html / style.css / app.js）
├─ requirements-linux.txt # Linux 运行依赖
├─ install-linux.sh      # Linux 一键安装（venv + 后端检查）
├─ start-linux.sh        # Linux 启动器
├─ make_icon.py          # 生成多尺寸 icon.ico（自带沙漏图标，可用 assets/icon-1024.png 覆盖）
├─ patch_plugin.py       # 修补 Nuitka pywebview 插件（补 win32）
├─ installer.iss         # Inno Setup 安装包脚本
├─ ChineseSimplified.isl # 安装向导简体中文语言包
├─ icon.ico              # 应用图标（由 make_icon.py 生成）
├─ requirements.txt
└─ build.bat             # 一键：编译 + 打安装包
```

`web/` 是现在的标准资源目录。旧压缩包中可能还会看到带字面反斜杠的 `web\\index.html` 等文件名；Linux 脚本会自动复制到标准目录，不影响旧包兼容。
