# ANP Adapter Specification v0.1（已冻结）

**给 Adapter 作者的接入规范。**

目标：让任意类型的 AI Agent 通过 Adapter 接入 AgentNetPort，而 Core 不需要知道具体平台。

本文只描述 **Adapter 层的契约**。信封格式、寻址、投递降级见 `ANP-PROTOCOL.md`；
写插件（应用层）见 `ANP-PLUGIN-SPEC.md`。

```
状态：v0.1 已冻结（2026-09-22）。
```

## v0.1 冻结了什么

冻结前最后一道验证是 **Adapter-as-Plugin 原型**（`examples/adapter_plugin/`，
17 项实验全绿、连跑 4 次稳定）——它确认了原先只是设计主张的结构关系真的走得通。
冻结时留下的五条核心结论：

### ① Adapter Plugin 的定义

> **Adapter Plugin 是一种负责将一个或多个外部平台 Agent 映射到 ANP Agent 身份，
> 并负责 ANP 与平台原生语义转换的 Plugin。**

**不是**「Plugin + 一个 wake 实现」。原型实证：**普通 Plugin 本来就有 wake**——Core
加载插件时就给它登记 `wake={kind:http, url:base_url+"/anp/wake"}` 并把插件 id 登记成
正常 ANP 身份。wake 是通信机制，不是 Adapter 的本质区别；本质区别是**代管 + 翻译**。

### ② Adapter 和普通 Plugin 的关系

```
Plugin
├── 普通 Plugin          应用层（公告栏、游戏、…）
└── Adapter Plugin       接入层（把外部平台 Agent 映射成 ANP Agent 身份）
```

Adapter Plugin 继承 Plugin System 的**全部基础机制**（manifest、双向握手、工具注册、
事件订阅、面板），再增加两项自己的职责：**代管 Agent** + **语义翻译**。

### ③ 一个 Adapter Plugin 可以代管多个 Agent

```
anp.adapter.codex
 ├── codex-01
 ├── codex-02
 └── codex-review
```

Plugin identity 与 Agent identity **保持分离**，两枚令牌不共享（P0-1）。

### ④ `register` 策略是现阶段的重要限制

```
register=open       → Adapter 可注册代管 Agent
register=allowlist  → Agent 名必须在 allow 里
register=closed     → Adapter 完全无法注册代管 Agent
```

收紧 `register` 会让**所有** Adapter Plugin 一起失效，而且报的不是权限错误，是
「Adapter 启动正常但代管的 Agent 身份不存在」。这不是 Adapter 专属权限问题，
是 **Agent identity registration policy**。详见 §2.x。

### ⑤ Reference Demo 体现「两种形态」

```
普通外部进程              自己作为 ANP Agent        → examples/adapter_demo/
Adapter Plugin            Plugin 身份 + 代管 N 个   → examples/adapter_plugin/
```

两种形态跑的是**同一套协议**（`/heartbeat` 代管、`/anp/wake` 收信封、
`/messages/send` 回收回信），差别只在身份怎么来、谁拉起进程。详见 §12.1。

> 曾在「Plugin + wake」与「代管 + 翻译」之间摇摆。原型验证后定为后者，
> 并把「不许再用 Plugin+wake 当定义」写进了符合性检查。

---

## 0. 先读这一节：没有 6 种 Agent，只有 3 个 delivery mode

你可能听说过这些分类：

```
CLI Agent · HTTP Agent · MCP Agent · Hook-based Agent · Heartbeat Agent · Polling-only Agent
```

**在 Core 眼里它们归成三类。** 这不是简化，是 Core 已经写成的事实：

| Agent 类型 | Core 看到什么 | delivery mode |
|---|---|---|
| CLI Agent | 一条可执行命令 | `exec` |
| HTTP Agent | 一个回调 URL | `http` |
| Hook-based Agent | 宿主上的一个 URL（钩子只是宿主的转发） | `http` |
| Heartbeat Agent | 一个 URL + 续约心跳 | `http` |
| MCP Agent | **什么都没有**，靠下次运行时自取 | `queue` |
| Polling-only Agent | **什么都没有** | `queue` |

「Hook」「Heartbeat」「MCP」的差别全部落在 adapter 自己那一侧。Core 只知道：

> **这是一个合法的 ANP Agent：它有个名字、有个收件箱、有一个 delivery mode。**

Codex 还是 DeepSeek，Core 不需要知道。

---

## 1. Adapter 最小职责

```
Platform ──▶ Adapter ──▶ ANP Agent        （箭头双向）
```

Adapter 是唯一知道「这个平台怎么被叫醒、怎么把信递进去、怎么把回信取出来」的东西。

### 必须做（4 项，缺一项就不成立）

| # | 职责 | 说明 |
|---|---|---|
| 1 | **托管身份** | 代 agent 完成首次注册，持有并在每次请求带上它的 token |
| 2 | **实现唤醒** | 提供一个 http 端点 / 一条 exec 命令 / 或明确声明「我只轮询」 |
| 3 | **语义翻译** | ANP 信封 ⇄ 平台原生消息格式 |
| 4 | **回收回信** | 把平台产出的回复包成 ANP 信封发回 Core |

### 禁止做（越界即 bug）

- 禁止做投递决策（要不要重投、退避多久、谁在线）——Core 的事
- 禁止读别人的收件箱、禁止读 `keys.json` / `ledger.jsonl`
- 禁止持有或使用 hub signing secret
- 禁止代替 Core 签收
- 禁止用自己的身份冒充 agent

### 一条硬规则

> **Core 是唯一真相源。Adapter 可以持有状态，但必须能在状态全丢后从 Core 恢复。**

Adapter 侧的 roster / 会话缓存都只是缓存，丢了重启即可，不能反过来让 Core 依赖它们。

---

## 2. 身份模型（P0-1）

### 2.0 Adapter Plugin 是什么

> **Adapter Plugin 是一种负责将一个或多个外部平台 Agent 映射到 ANP Agent 身份，
> 并负责 ANP 与平台原生语义转换的 Plugin。**

这个定义来自 2026-09-22 的原型验证（`examples/adapter_plugin/experiment.py`，17 项全绿）。
两处要点：

**① 它不靠"多一个 wake"来区分。** 普通 Plugin 被 Core 加载时，Core 就已经给它登记
`wake={"kind":"http","url": <base_url>+"/anp/wake"}` 并把插件 id 登记成正常 ANP 身份
（`anp_hub.py` 的插件加载路径）。也就是说，**任何 Plugin 本来就有一个唤醒端点**。
「Plugin + wake」不能作为 Adapter 的定义——wake 是通信机制，不是 Adapter 的本质区别。

**② 本质区别是"代管"与"翻译"。** Adapter Plugin 会通过公开 `/heartbeat` 注册**另一个**
Agent 身份，并让那个身份的 wake 指向 Adapter Plugin 自己的端点——因为外部平台通常没有
可寻址入口。同时它承担 ANP 信封 ⇄ 平台原生格式的双向翻译。

```
ANP Core
   ├── Adapter Plugin 身份      anp.adapter.codex     ← 管理 adapter 自身
   └── Agent 身份               codex / codex-01 /
                                codex-review         ← 代表外部 agent 参与通信
```

> **Adapter Plugin 的身份用于管理 Adapter 自身；Agent identity 用于代表外部 Agent 参与
> ANP 通信。两者不是同一身份，也不共享 token。**

### 派生规则

| # | 规则 |
|---|---|
| 2a | 两个身份各有各的 token，**任何情况下不互换、不共用、不派生** |
| 2b | Adapter Plugin token 只用于：`POST /plugin/register`、取自身配置、收 Core 打来的 `/anp/register`。<br>★`/plugin/register` 只认插件自己的 token★（C1，2026-09-23）：admin token **明确不被接受**，也不留运维旁路——该端点问的是「你是不是 pid 本人」，不是「你是不是管理员」；admin 令牌答不出「我是哪个插件」。 |
| 2c | Agent token 只用于：代该 agent 发信 / 收信 |
| 2d | 一个 Adapter Plugin 可管理 **N 个** Agent 身份 |
| 2e | 一个 Agent 身份**只归一个** adapter 管——否则两个 adapter 抢同一个 wake 端点，行为不确定 |
| 2f | Agent 身份被顶替的防护由 §3 的「已有身份必须带对令牌」保证，与 adapter 无关 |

### 令牌由谁持有（必须写明的信任假设）

Adapter 代 agent 完成首次 bootstrap——因为只有 adapter 知道平台给的名字。所以：

> **Adapter 持有它管理的所有 agent 的 agent token。**

这与永久约束 `Agent token ≠ Hub signing secret` **不冲突，但必须相邻理解**：

```
adapter 持有 agent token    → 「和同一 OS 账户等信任」          可接受
adapter 持有 hub.secret     → 「只有 Hub 能合法生成签名」失效    ★绝对禁止★
```

### 命名约定

- Adapter Plugin 身份：`anp.adapter.<platform>`
- Agent 身份：`<platform>` 或 `<platform>-<instance>`（如 `codex`、`codex-review`）

下划线、大小写按平台习惯，但**一个身份名一经注册不得更改**（换名等于换人）。

### 2.x 代管受什么约束：`register` 策略

Adapter Plugin 代 Agent 注册，走的是 `/heartbeat` 的**首次注册**门
（`demo-01` 是新名字）。这个门由 `permissions.json` 的 `register` 决定，
**与 Adapter Plugin 自己的令牌无关**——那枚令牌只在它是管理员令牌时才影响 `is_admin`。

原型验证实测四种配置：

| `register` | 结果 | 说明 |
|---|---|---|
| `open`（默认） | **代管成功** | 任何本机进程都能铸造新身份 |
| `allowlist` + 名字在表里 | **代管成功** | `demo-01` 必须在 `register.allow` 里 |
| `allowlist` + 名字不在表里 | **代管被拒（403）** | 插件拿不到身份，Adapter 起不来 |
| `closed` | **代管被拒（403）** | 同上 |

**这是当前最大的运维陷阱**：

> 一旦把 `register` 收紧成 `allowlist` 或 `closed`，
> **所有 Adapter Plugin 会一起失效**——不是权限报错，是"agent 身份根本不存在"。

包装 Adapter Plugin 的人必须先把自己要代管的所有 Agent 名字加进
`permissions.json` 的 `register.allow`。

**当前机制不提供任何 Adapter 专属表达**：没有"允许插件代管身份"这类权限，
`register.allow` 是唯一控制点，而且它认的是**身份名**，不认"代管关系"。
这是原型验证暴露的机制缺口——**先按现状写清约束**，是否要在 Plugin Spec 里
补正式机制是后续决定，不在 v0.1 范围内。

---

## 3. 注册：`POST /heartbeat`（P0-2 / P0-6 / P0-10）

### 3.1 双语义写死

```
POST /heartbeat
   首次调用  = bootstrap registration   不需要密钥，响应里发令牌
   后续调用  = lease renewal + metadata refresh    必须带密钥
```

名字保持不变，是因为注册和续约的载荷、鉴权门、合并规则完全相同；拆成两个端点只会让
第三方多猜一次「为什么第一次要调 heartbeat」。

### 3.2 身份门（P0 Security Invariant）

```
新 name + 无 key        → 创建身份（仍要过 Core 的 register 策略层）
已有 name + 无 key      → ★401 拒绝★
已有 name + 正确 key    → 续约 / 更新声明
已有 name + 管理员令牌  → ★401 拒绝★（见下方说明，2026-09-23 更正）
```

**为什么这是 P0**：如果「已有 name + 无 key」也被允许并重新发令牌，那么任何本机进程只要
知道 `codex` 这个名字就能重新获得该 Agent 的身份。**绝不允许。**

#### 3.2.1 更正：管理员令牌**不能**续约已有身份（v0.1.1，B2）

本节原先写「已有 name + 管理员令牌 → 放行」。**这与实现相反，2026-09-23 删除。**

| | 旧描述（错） | 实际 / 现描述 |
|---|---|---|
| 已有 name + 身份自己的 key | 续约 | 续约（不变） |
| 已有 name + admin token | 放行 | **401 拒绝** |

管理员的真实能力只在**铸造新身份**那一侧（`新 name + 无 key` 分支：持 admin 令牌者永远
放行），**不包括**改动已有身份的 wake 端点。方向与 P0-2 同一条：能重发令牌 = 能顶掉别人的
唤醒端点，所以不放宽。

需要改已有身份只有两条路：**拿到它自己的 key**；或者把它从账本里清掉后重新铸造
（那等于新建身份，不是续约）。

### 3.3 请求字段

```jsonc
POST /heartbeat
x-mailbox-key: <已有身份时必填>

{
  "name": "codex",                    // ★Identity（唯一必填）★

  "wake": {                            // ★Transport（必填；queue 档可整体省略）★
    "kind": "http",                   //   http | exec | queue
    "url": "http://127.0.0.1:9100/anp/wake",   //   kind=http 时必填，主机必须回环
    "command": ["python", "adapter.py"],       //   kind=exec 时必填，argv 数组
    "cwd": "D:/work"                  //   可选
  },

  "caps": {                            // Capability（可选，见 §8）
    "inject_session": false,
    "spawn_cwd": true,
    "resume_session": true,
    "queue": true
  },
  "skills": ["coding", "review"],      // Capability（可选，自由词表）

  "process_api": {                      // Runtime（可选，见 §7）
    "kind": "http",
    "url": "http://127.0.0.1:43129/process/health"
  },
  "description": "Codex CLI",           // Runtime（可选）
  "workspace": "D:/work",               // Runtime（可选）
  "client": "codex"                     // Runtime（可选）
}
```

**Permission 不在这个列表里——这是设计决定，不是遗漏。**

> **Agent 的权限不由自己声明，由 Core 的策略层决定。** `permissions.json` 的
> `register` / `announce` / `send_to` 三项由操作人配置，Core 每次决策重读。
> agent 自报权限等于让被测者自己出题。

这与插件的 manifest `permissions` 不同：**插件**的权限在 manifest 声明，因为 Core 亲手
拉起它、能执行校验；**agent** 是外部进程，Core 只能约束「它调这个 API 行不行」，约束不了
它本身。两种来源，同一个原则：**Core 决定。**

### 3.4 sticky 字段三态（P0-6）

以下字段是 **sticky registration fields**：

```
wake · caps · skills · process_api · description · workspace · client
```

```
字段缺失      → preserve     沿用账本里的旧值
字段 = null   → clear        清掉；下次缺失时不会复活
字段 = {}     → replace      替换成空对象，不是删除
```

**为什么这是 P0**：不写明，第三方无法判断 `"process_api": null` 是「我没更新」还是
「我要清掉它」，而这两种理解的后果完全不同。

> 不给 `wake` 的续约心跳不会抹掉唤醒端点。但**改错方向要当心**：传 `wake: null` 会清掉它。

### 3.5 响应

```jsonc
{
  "ok": true,
  "auth": "bootstrap",          // 或 "renewal"
  "key": "<agent token>",       // ★首次在这里拿到，此后每次请求带着它★
  "registered": { /* 账本里这条身份现在的完整样子 */ },
  "home": "~/.agentnetport"
}
```

---

## 4. Delivery mode 三档（P0-3 / P0-0）

```
wake.kind
├── http   → Core push        Core POST 信封到 adapter 的端点
├── exec   → Core launch      Core 拉起 adapter 的命令
└── queue  → Core persists    Core 只落盘；Agent 稍后自己拉
```

### 4.1 `queue` 是 delivery mode，不是唤醒机制

原表述「至少一种被叫醒的方式」有内在矛盾（queue 档没有唤醒）。最小 ANP Agent 的定义是：

> **一个身份 + 一个收件箱 + 一个 delivery mode（三者之一）。**

`queue` 的语义是「delivery mode = no push」——「不叫醒，我自己来取」也是一种合法的接入。

### 4.2 queue 档的终态（P0-0，已实现）

```
wake.kind == "queue"（或没有 push 目标）
    ↓
消息正常持久化
attempts        = 0
next_retry_at   = None
last_error      = None
message.undelivered 不触发
wake_skipped    = "no_push_target"
```

**为什么这是 P0**：修之前，queue 档身份每收一封信都会 `attempts+1` 并排重投，自动重投再
白试 4 次，最后标「已放弃自动重投」并误发一个 `message.undelivered`——而错误信息本身已经
写着「仅落盘，等待对方主动收信」。代码知道这是 queue，却没有按 queue 处理。

### 4.3 三态标记必须分清

```
wake=false            → skipped     → 发送方选择不唤醒      wake_skipped = true
wake.kind=queue       → no_push     → 收件人没有 push 目标  wake_skipped = "no_push_target"
http/exec 失败        → failed      → 可以进入 retry       wake_skipped 缺失
```

**禁止把这三者合并成一个布尔。** 混在一起，查询、审计、事件、UI 就没法区分
「我让它别唤醒」和「这个收件人本来就只能自己来取」。

### 4.4 没有 push 目标的其它情形

`delivery_mode` 的判定规则：`kind` 显式为 `queue` → queue；`http` 有 `url` → http；
`exec` 有 `command` → exec；**其余一律 queue**（包括压根没有 `wake` 字段的身份）。

---

## 5. Wake 载荷契约（P0-12 / P0-4 / P0-5）

这是**每个 adapter 唯一必须实现的回调**。Core 打给你什么，写死如下。

### 5.1 http 档

```
POST {wake.url}
content-type: application/json; charset=utf-8
x-mailbox-key: <目标 Agent 的 token>          ← ★P0-4★
body: {"message": <完整信封>, "to": "<Agent 身份名>"}
```

> **`/anp/wake` 请求头 `x-mailbox-key` 的值，永远是目标 Agent 的 token，**
> **不是 Adapter Plugin 的 token，更不是 hub secret。**

这与 §2 的身份分离形成闭环：看到这个头就知道是哪一枚。

#### 5.1.1 响应契约（★v0.1.1 补★，权威文本见 errata）

Adapter 对 Core 的 wake 请求，**响应**必须满足：

```json
{ "ok": true, "mode": "<响应模式>" }
```

| 字段 | 要求 |
|---|---|
| `ok` | 必须为 `true`，表示 Adapter 已正常接受本次 wake 请求 |
| `mode` | **必须存在**，且必须是下面的合法响应模式值之一 |

其它字段（`envelope_id` / `note` …）可选，Core 忽略未知字段。

**合法响应模式闭集（v0.1.1）**——`mode` 表示 Adapter 对本次唤醒**处理方式的确认**：

| 值 | 含义 |
|---|---|
| `immediate` | 已立刻按单封处理（或已进入立即处理路径） |
| `batched` | 已入本端批处理队列，稍后成批处理 |
| `session` | 已注入/绑定到某个平台会话（含会话队列） |
| `inject` | 已注入到已存在的活跃上下文/会话 |
| `followup` | 已挂到后续回合/待办，下一轮处理 |

规则：精确小写字符串；空串 / 缺失 / `null` / 未知值 / 大小写变体 → **均不合法**；
**不得**用 `mode` 表示 ANP delivery mode。

#### 5.1.2 两个 "mode" 不是同一概念（强制区分）

```
wake.kind            → Core 使用的 delivery mode（http / exec / queue）
                      → 写在心跳登记的 wake 配置里，决定 Core 怎么投递

wake response.mode   → Adapter 对本次唤醒处理方式的确认
                       （immediate / batched / session / inject / followup）
                      → 写在 HTTP 响应体里，决定 Core 是否视为可签收
```

禁止混用。响应体里的 `mode` **不得**出现 `http` / `exec` / `queue`。

#### 5.1.3 Core 判定（签收门）

**仅当**下列条件**全部**成立，Core 才把本次 wake 视为完成并记账/签收：

1. HTTP 传输成功（2xx）
2. 响应体可解析为 JSON **对象**
3. `ok == true`
4. `mode` 存在且为 §5.1.1 合法值

| 情形 | 结果 |
|---|---|
| 仅 `{"ok": true}`（缺 `mode`） | **不签收** |
| HTTP 200 但 `ok` 不为 `true` | **不签收** |
| `ok: true` 但 `mode` 缺失 / `null` / 空串 | **不签收** |
| `ok: true` 但 `mode` 非法（`"http"`、未知值、大小写变体） | **不签收** |
| 响应不是 JSON / 不是对象 / 无法解析 | **不签收** |
| HTTP 非 2xx | **不签收** |

> **HTTP `2xx` 本身不等于消息已被签收。仅返回 `{"ok": true}` 不构成成功签收。**

**签收** = 该封 `read=true`（或 `read_by` 已记）且不再被自动重投打扰；
**不签收** = 保持未读，仍按既有 retry 规则可重投（本补丁不改 retry 公式）。

### 5.2 exec 档

Core 每有一封信要投递，就拉起一个进程（§5.4），把信封通过**环境变量**交给它：

```
AGENT_ANP_WAKE_JSON   = <完整信封 JSON>
AGENT_ANP_SELF        = <Agent 身份名>
AGENT_ANP_SELF_KEY    = <目标 Agent 的 token>
AGENT_ANP_KEY         = 同上（既有约定，供"子进程再转发一次"的链路任取其一）
```

### 5.3 queue 档

Core 不调用 adapter。信落盘即为终态（§4.2）。adapter 要做的是让 Agent 自己调
`POST /messages/receive`。

### 5.4 exec 的进程契约（P0-5）

> `wake.kind = "exec"` 描述的是 **Core 的唤醒动作**：每有一封信要投递，Core 拉起一个
> 进程，把信封通过环境变量交给它，然后不再管它。
>
> `exec` **不定义 adapter 自己内部的宿主生命周期**。adapter 可以在被拉起的进程里做任何事
> （包括自己再起一个常驻服务），但那是 adapter 的私事，Core 既不期待也不依赖。
>
> ★本节只讲进程契约，不含回信路径★——回信怎么发回 ANP 见 §5.6
> （唯一合法出口是 `POST /messages/send`，禁止拿 stdout 当回信）。

**为什么必须写**：不写明，两个第三方实现可以都叫 `wake.kind = "exec"`，而一个是
「每封信 spawn 一次」、另一个是「常驻进程靠 stdin 唤醒」——Core 的行为预期完全不同。

### 5.5 收件人令牌由 Core 捎上（一条重要边界）

> **收件人令牌由 Core 在投递时捎上，adapter 不需要（也不准）去盘上翻 `keys.json`。**

这正是 adapter 永远不需要读 Core 内部数据库的原因。

### 5.6 exec 档的回信：只走 `/messages/send`（v0.1.1 写死）

> **exec 子进程的回信，唯一合法出口是 `POST /messages/send`（§6.1 接口 3）。**
> **禁止把 stdout 当成回信通道。**

三条理由：

1. **Core 不读子进程的 stdout**。exec 的契约是「拉起、投环境变量、不再管它」（§5.4），
   子进程往 stdout 打什么没有任何人会解析。
2. **stdout 没有信封结构**。没有 `id` / `from` / `correlation_id` / `kind`，
   判断不出「这是对哪一封的回」——对话线当场断。
3. **两个通道会分叉**。同一个 adapter 若 http 档走 `/messages/send`、exec 档走 stdout，
   就会出现「http 档能对话、exec 档静默丢回复」的怪事。

正确形状（子进程里）：

```python
env = envelope_from_env()                       # 从环境变量取信封（SDK）
me  = exec_identity()                           # SELF / SELF_KEY
platform_answer = deliver_to_platform(env)      # 干平台那件事
client = AnpClient(me["hub"], me["key"]).as_identity(me["key"], me["who"])
client.send(to=env.from_,
            body=from_native(platform_answer)["body"],
            reply_to=env.id,                    # 必须带回，否则落不回原对话线
            correlation_id=env.correlation_id,  # 原样带回
            kind="reply")
```

★回信失败的责任在 adapter 侧★：Core 不会因为 exec 子进程没回信而重投
（重投只看 ack/D1 的三事实）。想让回信可靠，adapter 得自己察觉
「进了平台」与「发回 ANP」两步的失败。参考实现的 `--wake-exec` 分支就是这条路径。

---

## 6. 标准接口：共 8 个（P0-11）

> **Adapter 接入所需标准接口共 8 个：6 个 Core API + 2 个 Adapter callback。**
>
> **v0.1.1 补充（Errata S1）：** ⑤/⑥ 照 `meta.session` 章执行只消费信封字段，**不算**新 API。  
> `/session/roster`、`/session/bind` 属 **通道扩展面（可选）**，见 `ANP-ADAPTER-SPEC-ERRATA-V0.1.1.md` §S1；资格线 = 六件套 ∪ 已声明扩展面。

### 6.1 Adapter → Core（6 个，canonical 面 = P0-9）

| # | 端点 | 用途 | 状态 |
|---|---|---|---|
| 1 | `POST /heartbeat` | bootstrap / renewal（§3） | canonical |
| 2 | `POST /messages/send` | 发信（含群发） | **canonical** |
| 3 | `POST /messages/receive` | 读信（过滤 + 签收） | **canonical** |
| 4 | `POST /messages/query` | 按关联线查自己的消息记录 | canonical |
| 5 | `GET /agents` | 名册（免令牌，不含正文） | canonical |
| 6 | `POST /plugin/register` | 插件形态：交贡献、换凭证 | canonical |

**deprecated**：`POST /send` ≡ `POST /messages/send`。别名与正名走同一段代码，响应体一字
不改，只在 `/send` 的响应上带 `deprecation: use POST /messages/send` 头
（**该头必须纯 ASCII**）。一个版本周期后移除。

`POST /tool` 是 **Core 工具 + 插件工具的唯一分发入口**，不列入 deprecated。
`/messages/receive` 与 `/tool` 的 `mail_inbox` **共享同一份底层实现**——所以两个入口的返回
逐字段一致，不可能分叉。

### 6.2 Core → Adapter（2 个，adapter 必须实现）

| # | 端点 | 用途 |
|---|---|---|
| 7 | `POST /anp/wake` | 收信封 → 翻译 → 递给平台 |
| 8 | `POST /anp/register` | 插件形态：收身份 / 令牌 / 配置 |

### 6.3 Adapter 拿不到的（硬边界）

```
✗ keys.json / ledger.jsonl / 任何人的 inbox 文件
✗ hub signing secret（永久架构约束）
✗ 代别人签收的能力
```

---

## 7. `status`：三个独立事实，不是一个对象（P0-7）

> **本规范不定义统一的 `status` 对象。**

只定义三个互不推导的独立事实：

| 事实 | 名字 | 谁提供 | Core 怎么用 |
|---|---|---|---|
| 进程活着吗 | `liveness` | adapter 可选（`process_api`） | 只透传，Core 不探测 |
| 端点可达吗 | `reachability` | **每次投递时观测** | 决定是否重投（受 §4.2 约束） |
| 我在场吗 | `lease` | adapter 周期 `/heartbeat` | 决定「在线」名单 |

**明文禁止**：任何 adapter 返回 `{"status": "online"}` 这类复合状态。它必然导致 Core 去猜
online 是哪一种。

具体推论：

```
进程退出 ≠ 端点不可达        （进程死了端点当然不通，但那是观测结果，不是状态跃迁）
端点不可达 ≠ 不在线          （lease 还活着，只是这一刻没打通）
lease 过期 ≠ 进程死了        （只是没续约）
```

**绝不准从其中任何一个推导另一个。**

---

## 8. `caps`：谁声明、谁消费、失败意味什么（P0-8）

`caps` 不是 Core 能力，是 **adapter 的能力广告**。

```
键                 谁声明          谁消费                        消费失败意味什么
────────────────   ────────────    ─────────────────────────    ──────────────────────
inject_session     只允许 adapter   上层会话规划器                 退化到 queue：不注入，等 agent 自己取
spawn_cwd          只允许 adapter   唤醒器（exec 档）              退化到 adapter 自己的默认 cwd
resume_session     只允许 adapter   上层会话规划器                 退化到开新会话
queue              只允许 adapter   投递路径（§4）                 ★无退化★——这是底线档
```

> **Core 不依赖任何 `caps` 键做基础通信决策。** `caps` 的消费者是上层规划器，不是 Core。

**这是一条防腐规则**：不写明，几年后 `if caps["resume_session"]:` 就会长进 Core，
Adapter 层塌回来，整个分层白做。

`skills` 是自由词表（`["coding", "review"]`），Core 不解释、不校验，只透传。

---

## 8.5 寻址：模糊目标 → 候选 → canonical Agent ID（Discovery / Address Resolution）

### 8.5.1 问题

发信方常常**根本不知道对方的 canonical Agent ID**。它只知道"我要找负责 Live2D 的"：

```
用户 / 上层 Agent
    「发给负责 Live2D 的」
        ↓   ← 这一步落在谁的头上？
    to = ???                       ← Core 的 /messages/send 只认 canonical Agent ID
```

`to` 必须是 canonical Agent ID（Spec 全文的 `to` 都是身份名，见 §3.3 与 Protocol §2）。
所以"用户说的那个谁到底是谁"必须有人回答——**但回答它的人不是 Core**。

### 8.5.2 归属（一条边界，不是一处实现）

| 谁 | 负责 | 不负责 |
|---|---|---|
| **Core** | 公开名册（`GET /agents`：name / description / skills / caps / last_seen）、canonical ID、投递 | 不解释「负责 Live2D 的」「昨天那个工作区」「这个项目」；不懂 Workspace / Session / 平台标题 |
| **Adapter** | 收模糊目标、搜候选、结合平台信息、排序、折成 canonical ID、必要时回候选让人再指一次 | 不做投递决策（重试/退避/在线） |

> **Core 负责"把信送给谁"；Adapter 负责"用户说的那个谁到底是谁，
> 以及平台里具体该投到哪扇门"。**

**不需要新增 Core API。** `/agents` 已经提供解析所需的全部公开身份信息
（`description` / `skills` / `caps` 都是身份自报的自由词表，Core 只存、只返回、**不解释**）。
`alias` / 平台短名 / 工作区标题这类信息**不放进 Core**：它们是平台语义，由 Adapter 自己持有
（`sdk/anp_adapter/resolve.py` 的 `LocalDirectory`）。

### 8.5.3 解析结果（Adapter 侧的状态，不是新的 Core 错误码）

```
RESOLVED              唯一目标 → target = canonical Agent ID，交给 /messages/send
AMBIGUOUS             多个相近候选 → 返回 candidates，★不猜★
NOT_FOUND             名册里没有匹配（附最接近的几个，仅作提示）
INVALID_TARGET        目标本身不合法（空 / 非字符串 / 去掉虚词后没有可匹配的词）
DIRECTORY_UNAVAILABLE 名册取不到（连不上 Core / /agents 为空）
NETWORK_ERROR         保留：传输层错误（Send 阶段）
CONNECTION_FAILED     保留：连接建立失败
ANP_OFFLINE           保留：对端 ANP 不在线
ANP_MAINTENANCE       保留：对端 ANP 维护中
```

后四个是 **reserved**：当前协议里 Core 不会回这几种（`/agents` 免令牌，只有"连不上"和
"被拒"两种失败）。留着是为了让 Adapter 有一处统一的地方表达投递阶段的对端状态，
而不是各自发明字符串——Spec §12 的"统一错误码没有 adapter 侧约定"那条缺口，
先在这一层收口。

### 8.5.4 Resolve 与 Send 必须分开（§9 的同一件事的另一面）

```
Resolve("负责 Live2D 的")  →  AMBIGUOUS / candidates      ← "不知道发给谁"
Resolve("demo-live2d-a")   →  RESOLVED / agent_id          ← 知道发给谁了
Send(agent_id="demo-live2d-a") → ok=true, wake={...}       ← "邮箱能不能投出去"是另一件事
```

**"不知道发给谁"和"知道发给谁但投不出去"绝不允许混成一种错误。**
`resolve` 的结果里带 `resolve_stage: "resolve"` 就是这个提醒；`send` 的成败看它自己的
`ok` / `wake` / 消息状态字段（§9）。

### 8.5.5 规则（Reference Adapter 实现在 `sdk/anp_adapter/resolve.py`）

1. **精确优先**：归一后与某个 Agent ID 相同 → 直接 RESOLVED（`agent-8f31` / `demo-01`）。
2. **匹配只用公开信息 + 本地提示**：name / alias / skills / caps / description /
   平台短名 / 平台工作区标题 / tags；普通字符串比较（归一化 + 词元命中 + 区分力加权），
   **不引入任何模型**。
3. **`last_used` 只能排序，不能覆盖歧义**（语义见 §8.6，★它不是名册的 `last_seen`★）：
   - 它参与候选表的排序（同分时最近用过的在前）；
   - **只有当候选本来就不相上下、并列者都有同一来源的 `last_used`、且领先者确实新得多**
     （年龄差 ≥ 门槛、且至少新若干倍）时，才允许据此收窄到它；
   - 达不到门槛 → **AMBIGUOUS，返回候选，让上层再指一次**。
   - 缺 `last_used` 或时间戳异常（未来时间）→ 不作证据；只有心跳活跃度时默认不破并列。
4. **没有任何匹配** → NOT_FOUND。附带的"最接近的"只是提示，不是候选。
5. **AMBIGUOUS 不得自动挑选**——最近使用、别名多、描述长，都不是自动挑的理由。

### 8.5.6 可选性（第三方 Adapter 的自由）

> **模糊解析是可选能力，不是接入门槛。**

Adapter 只读 Spec §6.1 的 6 个 API 就能完成接入；一个只支持精确 `to=<Agent ID>` 的
Adapter 完全合规。要支持模糊寻址再用 `resolve.py`（或自己写一个），
Core 不需要知道谁支持、谁不支持。

### 8.5.7 边界示例（可执行的验收）

`examples/adapter_demo/verify.py` 的 **E 段**把下面每一条都跑了一遍：

```
E1  输入 canonical Agent ID                 → RESOLVED（decided_by=exact）
E2  「做 Minecraft 的」，候选唯一              → RESOLVED
E3  「Live2D」，候选 A/B/C                    → AMBIGUOUS + candidates（不含 target）
E4  「根本不存在的目标」                       → NOT_FOUND；空目标 → INVALID_TARGET
E5  last_seen 只排序、不许猜；last_used（平台记录）才可破并列；两条路 decided_by 不同名
E6  Core 把「负责 Live2D 的」当字面身份名（不解析）；canonical ID 才投得到
E7  resolve.py 只依赖标准库（无模型 / 无 LLM / 无向量库）
```

### 8.5.8 Workspace / Session 不注册成 Agent

平台里有多个 Workspace / Session，**不代表要给每一个注册一个 ANP Agent**：

```
Adapter（一个身份）
└── 平台侧 N 个 Workspace / Session   ← 它们是 Adapter 内部对象，Core 看不见
```

```
ANP Agent + 平台侧 route/context   →  平台原生 Workspace / Session
        （Adapter 内部完成这段翻译；原生 ID 可以是随机值）
```

需要用**工作区标题**做用户侧解析时（"Live2D 那个工作区"）：

```
标题 → 候选 Workspace → native workspace ID      ← 全部在 Adapter 里
```

Core 不保存平台工作区标题，也不参与这段解析。

---

## 8.6 时间信号：`last_seen` ≠ `last_used`（★不许互相顶替★）

寻址会用到"谁最近动过"。**这里有两个语义不同的信号，混用就是撒谎。**

| 信号 | 名字 | 从哪来 | 语义 | 能用来做什么 |
|---|---|---|---|---|
| **`last_seen`** | 名册字段 `GET /agents` 的 `last_seen` | Core：`core.agent_table()[name]["ts"]`＝账本里该身份**最后一条记录**的时间戳（心跳 / 续约 / 探活；`core.online()` 用的也是它） | 「最近**活跃**」＝它还活着 | **只用于候选排序**（给人看）。★默认不许用它替用户挑人★ |
| **`last_used`** | Adapter 自己记 | Adapter 的平台侧记录（"这个身份最近一次真的被处理过"） | 「最近**被用过**」 | 排序 + 在真正并列时**破并列** |

**Core 的 `/agents` 给不出 `last_used`。** Core 里真正代表"被使用"的是消息状态字段
——`delivered_at`（只在真签收时写；D1 之后 exec 档 spawn 不写）、`read_ts`、`read_by`
——它们**不在名册里**，聚合它们要遍历收件箱。所以：

- Adapter 要么**自己记** `last_used`（推荐，`LocalDirectory.add(agent_id, last_used=…)`）；
- 要么明确接受"只有心跳活跃度"这个更弱的信号，而那时 **`resolve` 默认不据此破并列**
  （显式传 `recency_allow_seen=True` 才允许，且结论标明 `recency.heartbeat`）。

**四条硬规则**（`sdk/anp_adapter/resolve.py` 里是可执行代码）：

1. `build_dir` 里 **`last_seen` 只来自名册、`last_used` 只来自 Adapter 提示**，互不代填。
2. 候选视图同时给 `last_used` / `last_used_source` / `last_seen`，调用方看得见来源。
3. 破并列只在**并列的每个候选都有同一来源**的最近信号时进行：
   - 只有一部分候选有记录 → 不许用（否则选出来的是"唯一有记录的那个"，不是最近的）；
   - 只有 `last_seen` → 默认不许用。
4. `decided_by` 与 `reason` 必须写明信号来源（`recency.platform` / `recency.heartbeat`），
   且**心跳来源的措辞只能说"最近活跃"，不许说"最近用过"**。

---

## 8.7 待决项：公开 `/agents` 与 `agents.discover` 权限（★登记，未改★）

**现状（两个事实同时成立）**：

```
GET /agents           → 免令牌：任何本机进程都能读 name/description/skills/caps/last_seen
ANP-PLUGIN-SPEC §2.1  → 权限表里写着：agents.discover = 调 GET /agents
```

于是 `agents.discover` 目前是**死权限**：声明它不多拿任何东西，不声明也照样读全量名册。
**这不影响 Discovery 本身**（§8.5 只依赖公开名册），但会造成规范／API 认知分叉。

**两种收敛方向（二选一；本次不选、也不改）**：

| 方向 | 做法 | 代价 |
|---|---|---|
| **A（推荐）公开目录** | 明确 `/agents` 是**寻址基础设施**：公开、免令牌；把 `agents.discover` 降级成审计标记，或从权限闭集里删掉 | 要改 Plugin Spec 权限表 + `anp_plugin.PERMISSIONS`，已声明该权限的插件跟着改 |
| **B 受权限控制** | `/agents` 要求权限（插件按 manifest、agent 按策略） | 打断 `ensure_hub` / `register_self` / `webui_alive` 等**免令牌探活**依赖；模糊寻址变成"先拿特殊权限才能找人" |

**倾向 A 的理由**：Agent Discovery 本身就是寻址基础设施，§8.5 的模糊解析天然需要目录；
"想找人还得先拿一个只读权限"换不来任何安全收益（`/agents` 不含正文、不含令牌）。

**为什么现在不改**：这是**权限语义债**，属于 Plugin Spec 的范围，与 Discovery 的实现路线无关；
改动会牵动探活依赖与插件权限闭集，应该单独一次决策落地。
在那之前**以代码为准（公开）**，本文与 Plugin Spec 的差异以此节为准。

---

## 8.7a 2026-09-26 裁定：`agents.discover` 有了一个真执行点，`/agents` 保持公开

外部独立审计把"死权限"记成 P1（"插件权限声明基本未执行"）。
本轮按**方向 A 的公开目录立场 + 一个真执行点**落地，不改 `/agents` 的免令牌性质：

```
GET /agents                     → 仍然免令牌（寻址基础设施，SDK resolve() 依赖它）
/tool → mail_agents             → 查表：插件身份没有 agents.discover → permission.denied
```

**为什么不再算"死权限"**：权限的意义是"声明了才能用某个能力"。现在
`mail_agents` 这个**工具面**是真查表的（调用方带令牌、身份可识别）；
而裸 `GET /agents` 保持公开，是因为它是探活与模糊寻址的地基，
给它上令牌的代价（打断每个 Adapter 的 vendored 客户端）远大于收益
（名册里没有正文、没有令牌，且威胁模型仍是同 OS 账户）。

**仍未做的事（明确记账）**：如果将来要让插件也拿不到裸名册，需要一次
"Agent 侧身份凭证进 GET /agents + SDK 同步升级"的协同变更，那是 v0.2 级动作，
本版本不做。见 `docs/AUDIT-20260926.md`。

---

## 9. `ok` 的语义（Adapter 作者最容易误读的一条）
```
POST /messages/send → ok = true
```

**只表示「消息已经成功持久化」**，不表示对端已收到，更不表示已消费。

真正的投递状态看这两处：

```
wake 字段              → 这一次投递的结果（kind / ok / error）
消息记录里的状态字段    → delivered_at / attempts / next_retry_at /
                          last_error / read / wake_skipped
```

把 `ok=true` 当成「Agent 已经消费」是 adapter 侧最常见的误读。

---

## 10. 最小 adapter 骨架

只用 protocol §6 的接口，不含任何 Core 内部路径，不含任何平台知识之外的逻辑：

```python
# 依赖只有标准库 + 本规范
TOKEN = bootstrap()                    # POST /heartbeat，存下响应里的 key

def on_wake(envelope, headers):        # POST /anp/wake —— http 档
    # headers["x-mailbox-key"] 是 **目标 Agent 的 token**，不是 adapter plugin 的
    platform_inbox.push(to_native(envelope))       # ① 翻译 + 递平台
    # ★必须带合法 mode，否则 Core 不签收★（v0.1.1，见 §5.1.1–§5.1.3）
    # 按你平台真实的处理方式选：immediate / batched / session / inject / followup
    return {"ok": True, "mode": "session"}

def on_platform_reply(platform_msg):   # 平台说"我干完了"
    env = last_envelope_for(platform_msg)
    core_send(to=env["from"],
              body=from_native(platform_msg),      # ② 回收回信
              reply_to=env["id"],
              correlation_id=env["correlation_id"])
    return {"ok": True}

def on_poll():                         # queue 档：agent 自己来取
    got = receive(peek=False)          # POST /messages/receive
    for m in got["messages"]:
        platform_inbox.push(to_native(m))
```

三个动作，对应三个职责：**翻译（`to_native` / `from_native`）、唤醒入口（`on_wake`）、
回信回收（`on_platform_reply`）**。平台知识只应出现在前两个翻译函数里。

> ★`on_wake` 的返回值★：只回 `{"ok": True}` **不会**被 Core 当作签收——
> 必须带合法 `mode`。这是 v0.1.1 补上的裂缝，见 §5.1.1–§5.1.3。

---

## 11. 第三方自检清单

写完 adapter，逐条过。任何一条不过，说明还缺东西——**缺的东西应该报给规范，不是猜**。

```
身份
  □ Adapter Plugin 身份与 Agent 身份是两个不同名字、两枚不同令牌
  □ Agent 身份的令牌只在投递/收发时用；plugin 令牌只用于 /plugin/register

寻址（可选，做了就逐条过）
  □ 精确 Agent ID → RESOLVED，且不会再去匹配别人
  □ 候选唯一 → RESOLVED；多个相近候选 → AMBIGUOUS + candidates（★不自动挑★）
  □ 没有任何匹配 → NOT_FOUND
  □ last_seen 只用于排序；last_used（平台记录）才可破并列，且差距不足时仍 AMBIGUOUS
  □ 候选视图分开回报 last_used / last_used_source / last_seen（★不互相顶替★，§8.6）
  □ resolve 与 send 的失败分得开（"不知道发给谁" ≠ "投不出去"）
  □ 解析只用公开名册 + 本地平台提示；没有把平台语义塞进 Core

注册
  □ 首次 POST /heartbeat 无密钥能拿到 key
  □ 第二次同名字无密钥 → 401（不是重新发 key）
  □ 同名字 + 正确 key → 200 续约
  □ 不带 wake 的续约心跳不会抹掉既有 wake 端点
  □ wake: null 能主动清掉端点

唤醒（按你选的档位）
  □ http：/anp/wake 收到 {"message":…,"to":…}，x-mailbox-key 里是 Agent 令牌
  □ http：★响应含合法 mode；仅 {"ok":true} 不算签收★（§5.1.1–§5.1.3）
  □ exec：子进程能从 AGENT_ANP_WAKE_JSON / SELF / SELF_KEY 三项环境变量拿到信封
  □ queue：agent 能自己 POST /messages/receive 取到信

接口
  □ 只用 §6.1 那 6 个 Core API，没有读任何 Core 内部文件
  □ 没有出现过 hub signing secret
  □ /send 已换成 /messages/send

状态
  □ 没有向 Core 上报过 {"status": "online"} 这类复合状态
  □ 没有从 liveness 推导 reachability，或反之

收发闭环
  □ 收信 → 平台处理 → 回信，回信带 reply_to + correlation_id
  □ verify：把一封信的 body 改一个字，你的回信仍然发得出去（信封签的是正文）
```

---

## 12. 已知缺口（冻结后补，不阻断接入）

这些是「现在按文档写不出来或容易踩坑」的点，按致命程度排。

| 级别 | 缺口 | 状态 |
|---|---|---|
| ~~P0~~ | `/anp/wake` 的**请求**载荷契约没写成文档 | ✅ 本文 §5 |
| ~~P0~~ | `/anp/wake` 的**响应** `mode` 契约漏写（第三方只读 Spec 会漏签收） | ✅ 本文 §5.1.1–§5.1.3，v0.1.1 errata 关闭 |
| ~~P0~~ | HTTP API 没有独立参考 | ✅ 本文 §6 + `ANP-PROTOCOL.md` §9 |
| ~~P0~~ | 规范里的 `/messages/send`、`/messages/receive` 实现里没有 | ✅ 已落地并测试 |
| ~~P0~~ | 「Adapter Plugin 是什么」没有结构定义；曾用「Plugin + wake」这种不准的表述 | ✅ 本文 §2.0，定义来自原型验证 |
| ~~P0~~ | 代管 Agent 受什么策略约束没写 | ✅ 本文 §2.x，四种配置实测 |
| **P0** | `register=allowlist/closed` 会让**所有** Adapter Plugin 一起失效 | 已写清约束（§2.x），但要补正式机制 |
| ~~P0~~ | `/plugin/register` 的鉴权口径文档与实现分叉（写 plugin token，实际要 admin） | ✅ 本文 §2b / §3.2.1 / PLUGIN-SPEC §4.0，v0.1.1 errata 关闭 |
| P1 | exec 档的回信回收没有标准契约 | ✅ 已定稿：回信**只走** `POST /messages/send`，禁止把 stdout 当回信 |
| P1 | 统一错误码没有 adapter 侧约定 | ✅ Resolve 阶段已收口（§8.5.3）；Send 阶段仍用 HTTP 语义 |
| P1 | 没有 adapter 自检脚本 | `examples/adapter_plugin/experiment.py` 可充作雏形 |
| P2 | 没有参考 adapter 可抄 | ✅ `examples/adapter_demo/` |
| P2 | 没有 SDK | ✅ `examples/adapter_plugin/` 已证明"用现有机制就能写" |
| P2 | 「发信方不知道对方 canonical ID」没有统一说法 | ✅ §8.5：Adapter 侧解析，Core 不加 API |
| P2 | `last_seen` 被当成 `last_used` 用 | ✅ §8.6：两个信号写死、默认不许顶替；Adapter 自记 `last_used` |
| **P1** | **`agents.discover` 是死权限**（`/agents` 实际免令牌） | ⚠️ 已登记为独立决策项，见 §8.7；在决定前**以代码为准（公开）** |

### 12.1 Adapter Plugin 的两种形态（原型验证过的事）

| 形态 | 身份来源 | 什么时候用 |
|---|---|---|
| **普通进程** | 自己 `/heartbeat` 注册 | 平台 SDK 自带进程模型（CLI Agent、HTTP Agent）。见 `examples/adapter_demo/` |
| **Adapter Plugin** | Core 拉起 + `/anp/register` 送身份和令牌 | 要挂进生态（被 Core 拉起、随 Core 启动、能收 `/anp/register`）。见 `examples/adapter_plugin/` |

两种形态跑的是**同一套协议**：公开 `/heartbeat` 代管 Agent 身份、`/anp/wake` 收信封、
`/messages/send` 回收回信。差别只在身份怎么来的、谁拉起进程。

原型验证确认的事实（`examples/adapter_plugin/experiment.py`，17 项全绿、连跑 4 次稳定）：

```
Core 拉起插件 → POST /anp/register 送来 identity + token
  → 插件用公开 POST /heartbeat 注册 demo-01，wake 指向插件自己的 /anp/wake
    → 别的 Agent 发信给 demo-01
      → 插件收到完整 Envelope（id/from/to/body/kind/sig 齐备），首次投递即成功
      → x-mailbox-key 是 demo-01 的令牌，不是插件的（P0-4）
```

**没有新增任何 permission。** 现有 `tools.register` + `agents.discover` 就够了。

---

## 附：12 条 P0 速查

| 编号 | 一句话 |
|---|---|
| **P0-0** | queue 档不得进 push 重投循环；`attempts=0`、`next_retry_at=None`、不触发 `message.undelivered` |
| **P0-1** | Adapter Plugin 身份 ≠ Agent 身份；两身份两令牌，不共享 |
| **P0-2** | `/heartbeat` 已有身份必须带对密钥；无密钥一律 401，绝不重新发令牌 |
| **P0-3** | `queue` 是 delivery mode，不是唤醒机制 |
| **P0-4** | `/anp/wake` 的 `x-mailbox-key` 永远是目标 Agent 的令牌 |
| **P0-5** | `exec` = 一次投递一个进程；adapter 内部宿主生命周期自己定 |
| **P0-6** | sticky 三态：缺失→保留 / `null`→清 / `{}`→替换 |
| **P0-7** | 不定义统一 `status`；只有 `liveness` / `reachability` / `lease` 三个独立事实 |
| **P0-8** | `caps` 是 adapter 的能力广告；Core 不依赖它做基础通信决策 |
| **P0-9** | canonical `/messages/receive` + `/send` 别名；receive 与 `mail_inbox` 同一份实现 |
| **P0-10** | `/heartbeat` 首次 = bootstrap，后续 = renewal + metadata refresh |
| **P0-11** | 标准接口共 8 个：6 个 Core API + 2 个 Adapter callback |
| **P0-12** | 三种 wake 的载荷契约逐字段写死（含 http 响应 `mode`） |
