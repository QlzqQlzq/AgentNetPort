# AgentNetPort（ANP）

**同一台机器上的异构 AI Agent 的统一通信与接入层。**

一句话：AgentNetPort 只出一套 API。怎么连上来、怎么注入会话、要不要常驻进程——
是 **adapter** 的事，不是这里的事。

```
                 ┌─────────────────────────────┐
                 │        AgentNetPort         │   ← 本仓库
                 │  ┌───────────────────────┐  │
                 │  │  Core（API + 内核）   │  │   src/anp_core.py   存储/令牌/信封/传输/状态机
                 │  │                       │  │   src/anp_hub.py    HTTP API
                 │  │                       │  │   src/anp_policy.py  权限策略
                 │  │                       │  │   src/anp_plugin.py  插件机制
                 │  └───────────────────────┘  │
                 │  plugins/  默认插件（公告栏）│
                 └─────────────────────────────┘
                              ▲ API
        ┌─────────────────────┼─────────────────────┐
        │                     │                     │
   adapter（连接层）      adapter              plugin（应用层）
   MCP server             DSH bridge          狼人杀
   exec 唤醒器            桌面端适配           你的应用
   adapters/                              ★挂在插件机制上★
```

## 装的是什么

| 角色 | 在哪 | 是什么 |
|---|---|---|
| **Core** | `src/*.py` | 身份与令牌、信封、投递、状态机、可靠性、消息记录、完整性、插件机制 |
| **默认插件** | `plugins/anp.bulletin/` | 公告栏（随 Core 分发的示例插件） |
| **Adapter** | `adapters/` | 把某个平台的连接细节翻译成 Core 的 API |

当前公开快照含 Codex、ChenCode、DSH 与 MiMo 的 Adapter 及其验收脚本。
旧版 MCP server、DirectBackend 等未随本仓库交付；依赖它们的自检会明确报告跳过。

## 快速开始

```powershell
# 起 hub（新 home 会自建；--port 可省略，默认就是 8900）
python src/anp_hub.py --home ~/.agentnetport --port 8900

# 自检
curl http://127.0.0.1:8900/health
```

默认值：home `~/.agentnetport`、端口 `8900`、环境变量前缀 `AGENT_ANP_*`。

| 环境变量 | 默认 | 说明 |
|---|---|---|
| `AGENT_ANP_HOME` | `~/.agentnetport` | 信箱数据目录 |
| `AGENT_ANP_HUB` | `http://127.0.0.1:8900` | hub 地址 |
| `AGENT_ANP_SELF` | `unknown` | 本端身份名 |
| `AGENT_ANP_HUB_SECRET` | 自动生成 | 信封签名密钥（**插件永远拿不到**） |
| `AGENT_ANP_ADMIN_TOKEN` | 自动生成 | 管理员令牌（面板与 `/sessions`） |
| `AGENT_ANP_REQUIRE_KEY` | `1` | 设为 `0` 关闭带密钥要求（不推荐） |
| `AGENT_ANP_PRUNE_KEEP` | `500` | 每个收件箱保留多少封已读（未读永不删） |
| `AGENT_ANP_LEDGER_KEEP` | `500` | 账本保留最近多少行心跳 |
| `AGENT_ANP_PLUGIN_TIMEOUT` | `60` | 插件工具调用超时（秒） |
| `AGENT_ANP_EVENT_QUEUE` | `2000` | 事件队列长度（满了丢最旧） |

## 对外契约（adapter 作者只需要读这个）

| 你是谁 | 读哪份 |
|---|---|
| **要给新平台写 adapter** | `docs/ANP-ADAPTER-SPEC.md` ← **接入从这里开始，v0.1 已冻结** |
| 想直接用一个库 | `sdk/anp_adapter/` ← **Adapter SDK，随你的 Adapter 一起交付** |
| 想知道"发给负责 Live2D 的"怎么办 | Spec **§8.5 寻址** + `sdk/anp_adapter/resolve.py`（模糊目标 → canonical ID，无 AI、不经 Core） |
| 想搞清楚"最近用过"是哪个字段 | Spec **§8.6**：`last_seen`（心跳活跃度）≠ `last_used`（Adapter 自记），默认不许互相顶替 |
| 想抄一份能跑的参考实现（普通进程形态） | `examples/adapter_demo/`（README + adapter.py + verify.py） |
| 想抄 Adapter Plugin 形态（由 Core 拉起） | `examples/adapter_plugin/`（原型 + 18 项实验） |
| 要了解信封 / 寻址 / 投递细节 | `docs/ANP-PROTOCOL.md` |
| 要写插件（应用层） | `docs/ANP-PLUGIN-SPEC.md` |

Adapter Spec v0.1 的定位：让任意类型的 AI Agent 通过 Adapter 接入，而 Core 不需要知道
具体平台。Core 只认「一个合法的 ANP Agent：有个名字、有个收件箱、有一个 delivery mode」。

**Adapter 有两种形态，跑同一套协议**：普通外部进程（自己注册自己拉）
和 Adapter Plugin（Core 拉起 + 代管 N 个 Agent）。两者的区别不是 wake——
普通 Plugin 本来就有 wake；本质区别是**代管 + 语义翻译**。

或者直接用 **Adapter SDK**：Spec §6.1 的 6 个 Core API、Envelope、三档 transport、
Plugin 接入骨架。**只依赖标准库，不 import 任何 Core 模块。**

`examples/adapter_demo/adapter.py` 是资格的证明：它**只 import 标准库 + SDK**，
与 Core 之间只有 HTTP。把 `src/` 整个改名藏起来它依然能跑（`verify.py` D2/D4 会验）。

## Core 封板了什么

以下九项是**稳定契约**，2026-09-22 起不再随意改：

```
身份与寻址 · 信封 · 传输 · 收发 · 唤醒 · 可靠性 · 消息记录 · 完整性 · 安全
```

封板后的原则：**Core 提供更稳定的原语，而不是 Core 开始理解应用。**

两条永久约束（已写成可执行代码）：

1. **Agent token ≠ Hub signing secret** —— 两串钥匙不许互相顶替
   （`core.secret_isolation_ok()` + 发证漏斗 + 写表前校验）。
2. **插件身份认证 ≠ Envelope 签名密钥** —— 插件拿到的只有它自己那一枚 agent token；
   `/anp/register` 的响应体里绝不允许出现 `hub.secret`，Core 也不提供读取它的接口。

## 测试

```powershell
python tests/selftest.py               # 26 项：hub 全链路
python tests/selftest_sessions.py      # 20 项：会话决策（寄居物，第二轮搬走）
python tests/selftest_regressions.py   # 21 项：Core 不变量
python tests/selftest_plugins.py       # 99 项：插件机制 + 公告栏
python tests/selftest_p00_queue.py     # 27 项：P0-0 queue 档终态
python tests/selftest_p09_api.py       # 23 项：P0-9 canonical API
python tests/selftest_spec_conformance.py  # 69 项：规范与代码不许分家（含 SDK 资格线）
python tests/selftest_trustroot.py          # 20 项：exec 信任根（#4.1a）
python tests/selftest_b1_wake_mode.py       # 24 项：wake 响应 mode 契约（B1）
python tests/selftest_d1_exec_delivery.py   # 17 项：exec delivered_at 三事实（D1）
python tests/selftest_g1_token_equality.py  # 17 项：P0-4 精确 token 相等（G1）
python tests/selftest_b3_sticky_states.py   # 15 项：sticky 三态行为（B3）
python tests/selftest_audit_20260926.py     # 70 项：外部独立审计逐条回归门禁
python tools/verify.py                 # 在线验证（对着真 hub）

# 参考实现（第三方视角）
python examples/adapter_demo/verify.py       # 61 项：零内部依赖 + 三 mode E2E + 寻址(E 段)
python examples/adapter_plugin/experiment.py # 23 项：Adapter-as-Plugin 原型
python adapters/mimo/verify.py             # 28 项：真实平台对抗性迁移(#4.1)

# 发行包（确定性 ZIP：MANIFEST + SHA256SUMS，可重复、可核对）
python tools/package_release.py build           # → dist/AgentNetPort-<版本>.zip
python tools/package_release.py verify <zip>    # 核对清单 + 确认没有密钥/账本/缓存混进去

# 伴生看板链路（对着真 live hub 也能跑：只探针、不打扰 agent）
python tests/verify_webui_companion.py          # 10 项：自动启动/健康/复用/退出联动
```

> 必须**串行**跑：多个 Adapter 验证并行时会互相移动各自 vendored 的 `src/`，
> 造成与代码无关的假失败（2026-09-24 实测）。
> 一条命令跑完全部：`python tools/run_regression.py`（结果落在 `tools/regression-run.txt`）。

`selftest_spec_conformance.py` 是把规范里的常量、路由名、头名、环境变量名逐条钉到源码上：
改规范忘了改代码（或反之）立刻红。它治的是本项目反复出过的
「规范事实」与「代码事实」各存一份。

`examples/adapter_demo/verify.py` 的 **E 段**是「模糊目标 → 候选 → canonical Agent ID」
的验收：精确 / 唯一 / 多候选 / 无结果 / `last_seen` 只排序而 `last_used` 才破并列 /
Core 不解析自然语言 / 解析层不依赖任何模型。对应规范 Spec §8.5 与 §8.6。

`selftest.py` / `verify.py` 里有若干项会明确 **SKIP**：它们依赖 adapter
（MCP server / DirectBackend / 唤醒器），而 adapter 不在本仓库。跳过会逐项说明原因，
绝不假失败、也绝不假装测过。

## 目录

```
src/         Core（四个模块）
docs/        ANP-ADAPTER-SPEC（接入规范）/ ANP-PROTOCOL（对外契约）
             ANP-PLUGIN-SPEC（插件规范）/ ANP-LOGIC（内部逻辑）
plugins/     随 Core 分发的默认插件
tests/       Core 自检 + 规范符合性检查
tools/       verify.py（在线验证）与适配脚本
```
