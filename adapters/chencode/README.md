# ChenCode Adapter（ANP）

把 **ChenCode**（Electron 桌面 + Bun Runtime 编码 Agent）接入 AgentNetPort 的 **Adapter Plugin**。

```
Adapter Spec v0.1 + Errata v0.1.1（B1/B4/C1/S1–S4）
      ↓
Adapter SDK（sdk/anp_adapter/，已 vendor）
      ↓
本 Adapter Plugin（anp.adapter.chencode）
      ↓
ChenCode（桌面 / headless CLI / 待取信队列）
```

## 接入形态判定（Skill ②）

| 问题 | ChenCode 现实 | 结论 |
|---|---|---|
| 有可寻址常驻端点且能自主注册？ | 桌面/CLI 本身无 ANP 端点 | 否 |
| 插件化且要随 Core 启动？ | 希望被 Core 拉起、代管身份 | **Adapter Plugin** |
| 完全没有可寻址入口？ | 有，但只在本进程 | Plugin 自己听 `ANP_PLUGIN_PORT` |

**形态**：Adapter Plugin（Plugin 身份 `anp.adapter.chencode` + 代管 Agent 身份 `chencode`）  
**delivery mode**：`http`（Core → 本插件 `/anp/wake`）

> 不用 `exec`：插件装在 `<home>/plugins/`，Core 的 `install_roots` 信任校验会拒脚本（已知 Core 缺口）。

## 对象映射（Skill ③）

| ANP 侧 | ChenCode 侧 | 边界 |
|---|---|---|
| Agent identity `chencode` | 本机 ChenCode 实例（桌面/CLI） | 一经注册不得改名 |
| Envelope | headless prompt / pending 摘要 | 只在 `to_native`/`from_native` 出现平台知识 |
| `wake.kind=http` | 插件 `/anp/wake` 端点 | Core 只认三档之一 |
| `correlation_id` | 对话线 | 不与平台 session 混为一谈 |
| `meta.session` 章 | `--resume <sessionId>` / 新会话 | **只执行 Core 章，不自建决策树（S1）** |
| Workspace/Session | `~/.chen/projects/…` + adapter `data/` | 不注册成多个 ANP Agent |

## 平台档位（ChenCode 知识，ANP 不感知）

| 档 | 条件 | 信封到了之后 | caps |
|---|---|---|---|
| **A** | health(43127) 或桌面进程可达 | 落 `data/chencode-pending.*`，会话侧处理 | `inject_session=false` |
| **B** | 有 `bun.exe` + `bootstrap-entry.ts` | 拉起 headless 一轮，回收 `result` 作回信 | `spawn_cwd` + `resume_session` |
| **C** | 都没有 | 落盘待取信，会话侧周期收信 | 仅 `queue` |

档位只决定「怎么进平台」；**Core 怎么找到我们** 统一是 http wake。

## 职责落地

| Spec 职责 | 实现 |
|---|---|
| ① 托管身份 | `/heartbeat` 代 `chencode` 注册；agent token 存 `data/chencode-credentials.json`（⑦a） |
| ② 实现唤醒 | `POST /anp/wake`；鉴权 `x-mailbox-key` == agent token（P0-4）；响应 `ok+mode`（B1） |
| ③ 语义翻译 | `to_native` / `from_native` / `build_prompt`（仅 `platform.py`） |
| ④ 回收回信 | headless `result` → `POST /messages/send`，带 `reply_to`+`correlation_id`（B4） |

## Errata 对齐

| 条 | 对齐方式 |
|---|---|
| **B1** wake 响应 | 失败也带 `mode`；成功 `{"ok":true,"mode":"immediate\|session"}` |
| **B4** exec 回信 | 本形态为 http；回信仍只走 `/messages/send`，禁止 stdout 当回信 |
| **S1** 会话面 | ⑤⑥ 执行 `meta.session`；`/session/roster` + `/session/bind` 为 Layer B |
| **S2** 恢复 | ⑦a 凭证仍在→续约；⑦b 全丢→bootstrap 走 P0-2 401，不换名 |
| **S3** 签收/补偿 | `enqueue_work` 先落盘再签收；平台失败发失败回信 |
| **S4** need_reply | `===true` 才义务回信；缺省=知会。`AGENT_ANP_CHEN_ALWAYS_RUN=1` 时强制跑并**必须回收回信**（H-1，产出不静默） |
| **caps** | A 档 `inject_session=false`（只落摘要，未真注入活跃会话）（H-2） |

## 配置

| 环境变量 | 含义 |
|---|---|
| `ANP_CHENCODE_AGENT` | 代管身份名，默认 `chencode` |
| `ANP_CHENCODE_AGENT` / `ANP_CHENCODE_CWD` | 默认工作区 |
| `AGENT_ANP_CHEN_RUNTIME` | runtime 目录，默认 `%LOCALAPPDATA%\Programs\ChenCode\resources\runtime` |
| `AGENT_ANP_CHEN_BUN` | bun 可执行文件 |
| `AGENT_ANP_CHEN_GIT_BASH` | Windows 下 Git Bash 的 `bash.exe` 路径；未设置时从 `git` 路径、Git for Windows 注册表和默认安装目录自动发现，并注入 `CLAUDE_CODE_GIT_BASH_PATH` |
| `AGENT_ANP_CHEN_TIMEOUT` | headless 超时秒，默认 900 |
| `AGENT_ANP_CHEN_MAX_TURNS` | headless max-turns，默认 30 |
| `AGENT_ANP_CHEN_WRITE` | `=1` 时允许 Write/Edit/Bash |
| `AGENT_ANP_CHEN_MODEL` / `..._EFFORT` | 强制模型 / effort |
| `AGENT_ANP_CHEN_CREDENTIALS` | 桌面 `.credentials.json` 覆盖路径 |
| `AGENT_ANP_CHEN_ALWAYS_RUN` | `=1` 时知会也跑 headless，**且产出必须回信** |
| `AGENT_ANP_CHEN_STUB_REPLY` | 测试用：固定回信 stub，不拉真实 CLI |
| `AGENT_ANP_CHEN_SKIP_PREFLIGHT` / `AGENT_ANP_CHEN_FORCE_FAIL` | 验收用 |
| `CHEN_API_KEY` / `ANTHROPIC_BASE_URL` | 平台 API 凭证（env 优先） |

**平台 API 凭证链**：`CHEN_API_KEY` → 桌面 `customApiEndpoint` → wrapper `chen.ps1`。  
**ANP agent token** 只存插件自己的 `data/`，**从不读** `keys.json` / `ledger.jsonl` / `hub.secret`。

## 跑验收

```powershell
python adapters/chencode/verify.py
```

## 目录

```
adapters/chencode/
├── manifest.json    anp.adapter.chencode
├── platform.py      ★ChenCode 平台语义★
├── plugin.py        ANP 侧：托管身份 / wake / 翻译 / 回信 / 会话
├── verify.py        对抗性验收 + 缺口记录
├── anp_adapter/     vendored SDK（部署形态）
└── data/            平台状态（work / pending / sessions / credentials / logs）
```
