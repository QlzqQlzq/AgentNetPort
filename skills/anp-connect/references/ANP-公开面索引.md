# ANP 公开面索引

**只读这些。** 不读 Core 内部实现、不 import `src/`。
下表路径相对 ANP 仓库根。

## 文档

| 文件 | 读它回答什么问题 |
|---|---|
| `docs/ANP-PROTOCOL.md` | 寻址模型（`to`/`from` 是**人**，工作区是**坐标**，对话线是**钥匙**）；密钥与握手；编码（非法 UTF-8 一律 400）；广播公告 |
| `docs/ANP-ADAPTER-SPEC.md` | ★**接入的权威契约**★ 8 接口 / 3 delivery mode / wake 载荷逐字段 / resolve 规则 / 自检清单 / 缺口表 / 12 条 P0 |
| `docs/ANP-PLUGIN-SPEC.md` | manifest 字段与校验、四个生命周期动词、两个 `register` 的方向、permissions、events、Core 拉起怎么给端口 |
| `docs/AGENT-DISCOVERY-RESOLUTION.md` | 寻址这件事的工程检查报告：为什么能力归 Adapter、`last_seen`/`last_used` 怎么分开 |
| `docs/ANP-ADAPTER-SPEC-ERRATA-V0.1.1.md` | v0.1.1 更正（含 `/plugin/register` 鉴权口径、wake 响应 `mode`） |
| `docs/ANP-LOGIC.md` | 完整逻辑图：每个决策点、每个失败分支、配置开关、已知缺口 |

## SDK

| 模块 | 给你什么 | 什么时候用 |
|---|---|---|
| `client.py` | `AnpClient` + 6 个 Core API 的薄封装（+ `resolve()` 便利方法） | 总要 |
| `envelope.py` | `Envelope`（只读字段视图）+ `from_wake_payload` | 总要 |
| `transport.py` | `WakeTransport` 与 `HttpWake` / `ExecWake` / `QueueWake` | 总要 |
| `plugin.py` | `PluginServer` + `RegisterResult` | 写 Adapter Plugin 时 |
| `resolve.py` | 模糊目标 → 候选 → canonical Agent ID（**可选**，无 AI，零模型依赖） | 想支持「发给负责 <某个项目> 的那个」时 |
| `errors.py` | `AnpError`（transport / auth / policy / rejected 四类） | 想要结构化错误时 |

**SDK 不管什么**（这些永远在你的 Adapter 里）：平台语义、会话怎么注入、会话怎么创建/恢复、
Agent 怎么回复、投递决策、要不要支持模糊寻址、业务逻辑。

## 参考实现

| 路径 | 形态 | 读它的什么 |
|---|---|---|
| `examples/adapter_demo/` | **普通进程** | 完整最小实现 + `verify.py` 的 E 段寻址验收 |
| `examples/adapter_plugin/` | **Adapter Plugin** | `_find_sdk()` 的三种查找顺序、Core 拉起 → `/anp/register` 全链 |
| `adapters/mimo/` | 真实平台 Plugin | 只作**形态**参考；**不要复制它的平台逻辑** |

## 6 个 Core API（`§6.1`，canonical 面）

| # | 端点 | 用途 |
|---|---|---|
| 1 | `POST /heartbeat` | bootstrap / renewal |
| 2 | `POST /messages/send` | 发信（含群发）★canonical★ |
| 3 | `POST /messages/receive` | 读信（过滤 + 签收）★canonical★ |
| 4 | `POST /messages/query` | 按关联线查自己的消息记录 |
| 5 | `GET /agents` | 名册（**免令牌**，不含正文） |
| 6 | `POST /plugin/register` | 插件形态：交贡献、换凭证 |

`POST /send` 是 `/messages/send` 的 **deprecated 别名**（响应体一字不改，只多一个纯 ASCII 的
`deprecation` 头）。新代码用正名。

## 2 个 Adapter callback（`§6.2`，你必须实现）

| # | 端点 | 用途 |
|---|---|---|
| 7 | `POST /anp/wake` | 收信封 → 翻译 → 递给平台 |
| 8 | `POST /anp/register` | 插件形态：收身份 / 令牌 / 配置 |

## 硬边界（`§6.3`）

```
✗ keys.json / ledger.jsonl / 任何人的 inbox 文件
✗ hub signing secret（永久架构约束）
✗ 代别人签收的能力
✗ admin token（不要要求、不要保存）
```

## 12 条 P0 速查

| 编号 | 一句话 |
|---|---|
| P0-0 | queue 档不得进 push 重投循环；`attempts=0`、`next_retry_at=None`、不触发 `message.undelivered` |
| P0-1 | Adapter Plugin 身份 ≠ Agent 身份；两身份两令牌，不共享 |
| P0-2 | `/heartbeat` 已有身份必须带对密钥；无密钥一律 401，绝不重新发令牌 |
| P0-3 | `queue` 是 delivery mode，不是唤醒机制 |
| P0-4 | `/anp/wake` 的 `x-mailbox-key` 永远是**目标 Agent** 的令牌 |
| P0-5 | `exec` = 一次投递一个进程；Adapter 内部宿主生命周期自己定 |
| P0-6 | sticky 三态：缺失→保留 / `null`→清 / `{}`→替换 |
| P0-7 | 不定义统一 `status`；只有 `liveness` / `reachability` / `lease` 三个独立事实 |
| P0-8 | `caps` 是 Adapter 的能力广告；Core 不依赖它做基础通信决策 |
| P0-9 | canonical `/messages/receive` + `/send` 别名；receive 与 `mail_inbox` 同一份实现 |
| P0-10 | `/heartbeat` 首次 = bootstrap，后续 = renewal + metadata refresh |
| P0-11 | 标准接口共 8 个：6 Core API + 2 Adapter callback |
| P0-12 | 三种 wake 的载荷契约逐字段写死（含 http 响应 `mode`） |

## 已登记的缺口（`§12`，冻结后补，不阻断接入）

| 级别 | 缺口 | 状态 |
|---|---|---|
| **P0** | `register=allowlist/closed` 会让**所有** Adapter Plugin 一起失效 | 已写清约束，待补正式机制 |
| **P1** | `agents.discover` 是**死权限**（`/agents` 实际免令牌） | 已登记为独立决策项；决定前**以代码为准（公开）** |
| P1 | 没有 Adapter 自检脚本 | `examples/adapter_plugin/experiment.py` 可作雏形 |
| P1 | 统一错误码没有 Adapter 侧约定 | Resolve 阶段已收口；Send 阶段仍用 HTTP 语义 |
| — | Core 的 `install_roots()` 不含 `<home>/plugins/` | 真实部署下 Plugin 的 `exec` 唤醒一律被拒（用 http 绕开） |
| — | 插件启动预算 10 秒，超时报「端点不可达」 | 指向错误方向（其实只是慢） |
| — | Spec 没约定 Adapter 的平台状态该放哪 | 建议放插件自己的 `data/` |

**发现新缺口时**：按 `Core / Spec / SDK / Adapter` 归类，附命令与输出，
**先记录不擅自扩大 Core**。
