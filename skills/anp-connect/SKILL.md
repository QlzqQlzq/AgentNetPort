---
name: anp-connect
description: 把一个 Agent（或它的 Harness）接入 AgentNetPort（ANP）——判断接入形态、映射 Agent 与平台 Workspace/Session、选 delivery mode、托管身份、实现唤醒与回信、做模糊寻址、跑验收。当用户说"把 X 接入 ANP / 接上 AgentNetPort / 让这个 agent 能收发 ANP 信 / 写一个 ANP adapter / 迁移到 ANP"时使用。也用于排查"信发不出去 / 收不到唤醒 / 回信接不上原对话线 / 找不到收件人"。
---

# 把一个 Agent 接入 ANP

**一句话**：ANP Core 只管「把信送给哪个 Agent」；**「这个 Agent 在它自己的宿主里具体是哪扇门」永远是 Adapter 的事。**

## 当 Agent 需要接入 ANP 时，遵循以下流程……

① 只读公开面（Spec + SDK + Reference Adapter）——不读 Core 内部
② 先判断接入形态（Adapter Plugin / 普通进程），不预设答案
③ 写对象映射：ANP 概念 ↔ 平台概念（尤其 Agent ↔ Workspace/Session）
④ 选 delivery mode（http / exec / queue）——只选真需要的，不为兼容全实现
⑤ 实现四个最小职责：托管身份 / 唤醒入口 / 语义翻译 / 回信回收
⑥ 需要"找人"时再加模糊寻址（可选，零模型依赖）
⑦ 按真实部署路径装起来（<home>/plugins/<id>/），不偷偷依赖仓库根
⑧ 跑验收两链 + 八项边界，缺口只记录不外扩
```

每一节的「依据」都指向 `references/ANP-公开面索引.md` 里的原文位置。**先读规范再写代码**；规范与 SDK 冲突时以 Spec 为准、改 SDK 不改 Spec。

---

## ⓪ 先划边界：Adapter 不做什么

Adapter 的**四个必须**与**六条禁止**在任何平台上都成立：

| 必须做（缺一项就不成立） | |
|---|---|
| 1 **托管身份** | 代 Agent 完成首次注册，持有并在每次请求带上它的 token |
| 2 **实现唤醒** | 提供 http 端点 / 一条 exec 命令 / 或明确声明「我只轮询」 |
| 3 **语义翻译** | ANP 信封 ⇄ 平台原生消息格式 |
| 4 **回收回信** | 把平台产出的回复包成 ANP 信封发回 Core |

| 禁止做（越界即 bug） | |
|---|---|
| 做投递决策 | 要不要重投、退避多久、谁在线 —— Core 的事 |
| 读别人的收件箱 | 也禁止读 `keys.json` / `ledger.jsonl` |
| 持有 hub signing secret | 永久架构约束 |
| 代替 Core 签收 | |
| 用自己身份冒充 Agent | Adapter 身份 ≠ Agent 身份 |
| import Core 的 `src/` | Adapter 与 Core 之间只有 HTTP，没有代码依赖 |

> **硬规则**：Core 是唯一真相源。Adapter 可以持有状态，但恢复语义分两档——**⑦a 进程重启**（凭证仍在）必须能续约到新端口；**⑦b 凭证全丢**在 v0.1 与 P0-2 互斥，预期 401 / 不假装 bootstrap（见 `references/验收与缺口.md` 与 Spec Errata S2）。

**依据**：Adapter Spec §1、§2.0、§2b、§6.3。

---

## ① 只读公开面

按顺序读，**不要**去翻 Core 的实现：

```text
docs/ANP-PROTOCOL.md          寻址模型（to/from 是人，工作区是坐标，对话线是钥匙）
docs/ANP-ADAPTER-SPEC.md      ★接入的权威契约★（8 接口 / 3 delivery mode / wake 载荷 / 自检清单）
docs/ANP-PLUGIN-SPEC.md       manifest / 生命周期 / permissions / 两个 register 的方向
sdk/<adapter-sdk>/            SDK：client / envelope / transport / plugin / resolve / errors
examples/adapter_demo/        普通进程形态的完整参考
examples/adapter_plugin/      Adapter Plugin 形态的完整参考
adapters/<existing>/          已有的真实 Adapter（**只作形态参考，不要复制它的平台逻辑**）
```

读不到 SDK 或规范说不清某件事时：**先报告缺口，再决定要不要改，不要为了跑通绕过协议。**

---

## ② 判断接入形态（不预设答案）

两种形态跑的是**同一套协议**，差别只在**身份怎么来、谁拉起进程**：

| 形态 | 身份来源 | 什么时候用 |
|---|---|---|
| **普通进程** | 自己 `POST /heartbeat` 注册 | 平台自带进程模型（CLI Agent、已有常驻服务的 Agent） |
| **Adapter Plugin** | Core 拉起 + `POST /anp/register` 送身份和令牌 | 要挂进生态：随 Core 启动、能收 `/anp/register` |

**判断顺序**：

1. 宿主**是否已经有可寻址的常驻端点**，且能自主注册？→ 优先普通进程（少一层依赖）。
2. 宿主是**插件化架构**、能力由插件组合、且希望随 Core 启动？→ **Adapter Plugin 更合适**。
3. 宿主**完全没有可寻址入口**（桌面端、纯交互式 CLI）？→ 仍然可以 Plugin 形态：Adapter 自己听着
   `ANP_PLUGIN_PORT` 上的 `/anp/wake`，代管身份直接指过去。

> ⚠️ **"Plugin + wake" 不能用来定义 Adapter。** 任何 Plugin 被 Core 加载时本来就有唤醒端点。
> Adapter Plugin 的本质区别是**代管**（注册另一个 Agent 身份）与**翻译**。

**Adapter Plugin 的两个身份（P0-1）**：

```
ANP Core
   ├── Adapter Plugin 身份      anp.adapter.<platform>   ← 管理 adapter 自身
   └── Agent 身份               <platform> / <platform>-<n>  ← 代表外部 Agent 通信
```

- 两枚令牌**不互换、不共用、不派生**；
- plugin token 只用于 `/plugin/register`、取自身配置、收 `/anp/register`；
- agent token 只用于代该 agent 收发信；
- 一个 Adapter Plugin 可管 **N 个** Agent；**一个 Agent 只归一个 Adapter 管**；
- **admin token 不被 `/plugin/register` 接受**——它问的是「你是不是 pid 本人」，admin 答不出「我是哪个插件」。
  **不要要求或保存 admin token。**

**依据**：Adapter Spec §2.0、§2a–2f、§12.1；Plugin Spec §4.0。

### ⚠️ 代管会被 `register` 策略整体掐死

`/heartbeat` 的首次注册门由 Core 的 `permissions.json` 的 `register` 决定，**与 Adapter 自己的令牌无关**：

| `register` | 结果 |
|---|---|
| `open`（默认） | 代管成功 |
| `allowlist` + 名字在表里 | 代管成功 |
| `allowlist` + 名字不在表里 | **代管被拒 403** |
| `closed` | **代管被拒 403** |

> **当前最大的运维陷阱**：一旦收紧成 `allowlist` / `closed`，**所有 Adapter Plugin 一起失效**——
> 不是权限报错，是"agent 身份根本不存在"。包装的人必须先把**要代管的所有 Agent 名字**
> 加进 `register.allow`。当前机制**没有**"允许插件代管身份"这类专属表达。

---

## ③ 写对象映射（这一节决定架构对不对）

把两边的概念列成表，**在写代码之前**。至少要覆盖：

| ANP 侧 | 平台侧 | 边界 |
|---|---|---|
| Agent identity | 平台 Agent / 使用者 | ANP 只认身份名，且**一经注册不得改名**（换名等于换人） |
| Envelope（from/to/reply_to/correlation_id/meta） | 平台原生消息结构 | 翻译函数里才允许出现平台知识 |
| delivery mode | 平台怎么被叫醒 | Core 只认 http/exec/queue 三档 |
| **correlation / conversation** | 平台**对话线** | ★不同层，不许混为一个概念★ |
| 公开名册（name/description/skills/caps） | 平台别名、短名、工作区标题 | 平台语义**不放进 Core** |

### ★Workspace / Session 怎么映射（最容易做错的一节）★

平台里可能有多个 Workspace / Session，**平台原生 ID 还常常是随机值**。

```
✗ 错：每个 Workspace 注册成一个 ANP Agent
✓ 对：ANP Agent  →  Adapter  →  平台 Workspace / Session
```

```
Adapter（一个身份）
└── 平台侧 N 个 Workspace / Session   ← 它们是 Adapter 内部对象，Core 看不见
```

规则：

1. **Agent ↔ Workspace/Session 的映射由 Adapter 自己维护**，Core 不需要理解平台原生 ID。
2. 平台已有 Workspace 生命周期 / 标题 / 最近使用时，**跟随平台已有机制**，不要在 ANP 里重造一套。
3. 用**工作区标题**做用户侧解析（用户会说「<某个工作区标题> 那个工作区」）时：
   标题 → 候选 Workspace → native ID，**全部在 Adapter 里完成**；Core 不保存平台工作区标题。
4. 发信时坐标要**自动带**：`from_workspace` 缺省从启动上下文 `cwd` / 本进程 `cwd` 补，
   调用方显式写的值不覆盖。

**依据**：Adapter Spec §8.5.8；Protocol §2。

---

## ④ 选 delivery mode

```
wake.kind
├── http   → Core push       Core POST 信封到你的端点
├── exec   → Core launch     每有一封信，Core 拉起一个进程投环境变量
└── queue  → Core persists   只落盘；Agent 稍后自己拉
```

**判定规则**（Core 侧的算法，照抄理解即可）：
`kind` 显式为 `queue` → queue；`http` 有 `url` → http；`exec` 有 `command` → exec；**其余一律 queue**
（包括压根没有 `wake` 字段的身份）。

**不要为了兼容全部实现。** `queue` 是**合法的 delivery mode**，不是"没接好"：

> 最小 ANP Agent 的定义 = **一个身份 + 一个收件箱 + 一个 delivery mode（三者之一）**。

| 你的平台 | 选它 |
|---|---|
| 有常驻 HTTP 服务，能被 POST | `http`（**首选**，最省事也最可靠） |
| 只有 CLI / 无从寻址入口，但要随信即起 | `exec` |
| MCP / 纯轮询 / 桌面端 / 不想被打断 | `queue` |

**`exec` 的进程契约（P0-5）**：Core 每封信念一次进程、投环境变量、**然后不再管它**。
`exec` **不定义**你内部的宿主生命周期——你可以在子进程里再起常驻服务，那是你的私事，Core 不依赖。

### ⚠️ 真实部署下 `exec` 常常走不通

`exec` 唤醒引用的脚本通常要过 Core 的**信任校验**（只认信箱安装目录）。
而插件按设计装在 `<home>/plugins/<id>/`，真实部署下**永远在外面**，于是：

```
唤醒配置未通过信任校验（exec 唤醒引用的脚本不在信箱安装目录内）
```

→ **Adapter Plugin 的正解是 `http`：插件自己就在 `ANP_PLUGIN_PORT` 上听着 `/anp/wake`，
代管的 agent 直接指过去。**

```python
def _wake_declaration(self) -> dict:
    port = os.environ.get("ANP_PLUGIN_PORT") or "0"
    return {"kind": "http", "url": f"http://127.0.0.1:{port}/anp/wake"}
```

**依据**：Adapter Spec §4、§4.2、§5.4；Plugin Spec §3.2。

---

## ⑤ 实现四个职责

### 5.1 托管身份：`POST /heartbeat`

```
首次调用 = bootstrap registration   不需要密钥，响应里发令牌
后续调用 = lease renewal + metadata refresh   必须带密钥
```

**身份门（P0 Security Invariant）**：

| 情形 | 结果 |
|---|---|
| 新 name + 无 key | 创建身份（仍要过 Core 的 `register` 策略层） |
| **已有 name + 无 key** | ★**401 拒绝**★（绝不重新发令牌） |
| 已有 name + 正确 key | 续约 / 更新声明 |
| 已有 name + **管理员令牌** | ★**401 拒绝**★ —— admin 的能力只在**铸造新身份**那一侧 |

**为什么是 P0**：否则任何本机进程只要知道名字就能重新拿到该 Agent 的身份。

**sticky 字段三态（P0-6）**——写心跳时最容易踩：

```
字段缺失  → 保留原值
字段 null → 清掉
字段 {}   → 替换
```

> 不带 `wake` 的续约心跳**不会**抹掉既有端点；要主动清端点得显式 `"wake": null`。

### 5.2 唤醒入口：`POST /anp/wake`

**http 档请求**：

```
POST {wake.url}
content-type: application/json; charset=utf-8
x-mailbox-key: <目标 Agent 的 token>        ← ★P0-4★
body: {"message": <完整信封>, "to": "<Agent 身份名>"}
```

> `/anp/wake` 的 `x-mailbox-key` **永远是目标 Agent 的 token**，
> 不是 Adapter Plugin 的，更不是 hub secret。

**★响应契约（最容易被漏掉的一条）★**：必须返回

```json
{ "ok": true, "mode": "<合法响应模式>" }
```

`mode` 闭集（**精确小写**，不得用 delivery mode 的值）：

| 值 | 含义 |
|---|---|
| `immediate` | 已立刻按单封处理 |
| `batched` | 已入本端批处理队列 |
| `session` | 已注入/绑定到某个平台会话 |
| `inject` | 已注入到已存在的活跃上下文 |
| `followup` | 已挂到后续回合/待办 |

**Core 的签收门**——下列**全部**成立才视为完成：

1. HTTP 传输成功（2xx）　2. 响应体可解析为 JSON **对象**　3. `ok == true`　4. `mode` 存在且合法

| 情形 | 结果 |
|---|---|
| 只回 `{"ok": true}`（缺 `mode`） | **不签收** |
| HTTP 200 但 `ok` 不为 true | **不签收** |
| `mode` 缺失 / `null` / 空串 / 大小写变体 / `"http"` | **不签收** |

> **HTTP 2xx 本身不等于已签收。** 不签收 = 保持未读，仍按既有 retry 规则可重投。

**★签收 ≠ 处理完成（Errata S3）★**：`ok:true` + `mode` = **已接受并进入平台处理队列**，
Core 可记签收、不再重投本次投递。返回前必须把待办**持久化**；异步/延后失败必须**补偿**
（失败回信 / 本地待重试可观测 / 平台失败回执）——**禁止静默丢弃**。
鉴权失败或信封不完整 → `ok: false`，不得签收。

**need_reply 默认义务（Errata S4）**：
`true` → 尽力回信；`false` → 不要求；**缺省 = 知会，不要求义务回信**。
长任务平台不得在缺省时擅自烧完整会话去回一封没人要求的信。

**两个 `mode` 不是同一概念**（强制区分）：

```
wake.kind          → Core 的 delivery mode（http/exec/queue），写在你登记的心跳里
wake response.mode → 你对本次唤醒处理方式的确认，写在 HTTP 响应体里
```

**入站鉴权**：唤醒端点在宿主服务上**可被任何本地进程直达**，不校验就等于"谁都能往会话里塞一封信"。
令牌**由 Core 投递时捎带**，**不要**让你的插件去盘上翻 `keys.json`（那既多一处读取面，
又把"重启宿主才生效"变成修 bug 的代价）。校验只有一处实现：比对头部/环境里的令牌。

**exec 档**：信封通过**环境变量**交给子进程：

```
AGENT_ANP_WAKE_JSON   = <完整信封 JSON>
AGENT_ANP_SELF        = <Agent 身份名>
AGENT_ANP_SELF_KEY    = <目标 Agent 的 token>
AGENT_ANP_KEY         = 同上（链路任取其一）
```

### 5.3 语义翻译

平台知识**只应出现在两个翻译函数里**：`to_native(envelope)` 与 `from_native(platform_msg)`。
SDK 不管这一段——它一无所知，这是你的平台知识。

### 5.4 回收回信

```python
client.send(to=env.from_,
            body=from_native(platform_answer)["body"],
            reply_to=env.id,                    # ★必须带回，否则落不回原对话线★
            correlation_id=env.correlation_id,  # ★原样带回★
            kind="reply")
```

- **`reply_to` + `correlation_id` 是对话线的全部依据**：不填 = 对方那封往来断线。
- **exec 档的回信唯一合法出口是 `POST /messages/send`——禁止把 stdout 当回信通道。**
  Core 不读子进程 stdout；stdout 也没有信封结构（判断不出"这是对哪一封的回"）。
- ★**回信失败的责任在 Adapter 侧**★：Core 不会因为 exec 子进程没回信而重投
  （重投只看 ack/D1 的三事实）。所以要自己察觉「进了平台」与「发回 ANP」两步的失败。

**依据**：Adapter Spec §3.1–3.4、§5.1–5.6、§6.2、§10。

---

## ⑥ 模糊寻址（可选，零模型依赖）

**这一步是可选能力，不是接入门槛。** 只支持精确 `to=<Agent ID>` 的 Adapter 完全合规。

问题：Core 的 `/messages/send` **只认 canonical Agent ID**；但发信方常常只知道一个模糊目标
（「负责 <某个项目> 的那个」）。

```
Core     ── 公开名册 GET /agents + canonical ID + 投递
Adapter  ── 模糊目标 → 候选 → canonical ID（平台语义、提示、排序、消歧）
```

**不需要给 Core 加端点**。`GET /agents` 已提供解析所需的全部公开信息
（`description` / `skills` / `caps` 都是**身份自报的自由词表**，Core 只存、只返回、**不解释**）。
`alias` / 平台短名 / 工作区标题这类信息**不放进 Core**，由 Adapter 自己持有。

**四个出口（Adapter 侧状态，不是新的 Core 错误码）**：

```
RESOLVED               唯一目标 → target = canonical Agent ID
AMBIGUOUS              多个相近候选 → 返回 candidates，★不猜★
NOT_FOUND              名册里没有匹配（可附最接近的，仅作提示）
INVALID_TARGET         目标本身不合法（空 / 非字符串 / 去掉虚词后没有可匹配的词）
DIRECTORY_UNAVAILABLE  名册取不到
```

**规则**：

1. **精确优先**：归一后与某个 Agent ID 相同 → 直接 RESOLVED。
2. **匹配只用公开信息 + 本地提示**，普通字符串比较（归一化 + 词元命中 + 区分力加权），
   **不引入任何模型**。
3. **`last_used` 只能排序，不能覆盖歧义**：只有候选本来就不相上下、并列者都有**同一来源**的
   `last_used`、且领先者**确实新得多**时，才允许据此收窄；达不到门槛 → **AMBIGUOUS**。
4. 没有任何匹配 → NOT_FOUND。
5. **AMBIGUOUS 不得自动挑选**——最近使用、别名多、描述长，都不是自动挑的理由。
6. ★**`last_seen` ≠ `last_used`，不许互相顶替**★：

| 信号 | 从哪来 | 语义 | 能做什么 |
|---|---|---|---|
| `last_seen` | Core 名册 `/agents`（心跳/续约时间） | 最近**活跃**（它还活着） | **只用于排序**，默认不许替用户挑人 |
| `last_used` | **Adapter 自己记**（平台侧"最近真的被用过"） | 最近**被用过** | 排序 + 真并列时破并列 |

`/agents` **给不出** `last_used`。想要这条更强的信号，就自己在 Adapter 侧记一笔
（`LocalDirectory.add(agent_id, last_used=time.time())`）。
措辞也必须分清：心跳来源只能说"最近活跃"，**不许说"最近用过"**。

**Resolve 与 Send 必须分开**：

```
Resolve("负责 <某个项目> 的那个")  → AMBIGUOUS / candidates   ← "不知道发给谁"
Resolve("<canonical-agent-id>")   → RESOLVED / agent_id       ← 知道发给谁了
Send(agent_id)                    → ok=true, wake={...}       ← "投不投得出去"是另一件事
```

> **"不知道发给谁"和"知道发给谁但投不出去"绝不允许混成一种错误。**

**可选扩展**：如果以后认为需要 Resolver Agent / LLM，作为**可选扩展**加，不要强制使用 AI 做寻址。

**依据**：Adapter Spec §8.5、§8.6、§8.7。

### ★`ok=true` 的语义（最容易误读的一条）★

```
POST /messages/send → ok = true
```

**只表示「消息已经成功持久化」**，不表示对端已收到，更不表示已消费。
真正的投递状态看两处：`wake` 字段（本次投递结果）+ 消息记录里的状态字段
（`delivered_at` / `attempts` / `next_retry_at` / `last_error` / `read` / `wake_skipped`）。

---

## ⑦ 按真实部署路径装起来

**Adapter Plugin 由 Core 从 `<home>/plugins/<plugin-id>/` 拉起。** 那时"仓库根的 `../sdk`"
**不在路径里**。必须 vendor 或安装，否则插件起来就 `ModuleNotFoundError` 秒退——
**而 Core 只会报「插件端点不可达」，非常难查。**

三种用法：

```python
# ① 仓库里原地跑（examples/ 的做法）
sys.path.insert(0, "<repo>/sdk")

# ② ★vendor 进 Adapter 自己的目录（部署形态，Adapter Plugin 必须这样）★
sys.path.insert(0, str(Path(__file__).resolve().parent))

# ③ 装进站点目录（pip install / PYTHONPATH）
import anp_adapter
```

- **SDK 是随 Adapter 交付的库**，不是一个需要安装的服务。零依赖：只用标准库，
  不 import 任何 Core 模块——Adapter 与 Core 之间**只有 HTTP 协议，没有代码依赖**。
- **平台状态放你自己的目录**（插件自己的 `data/`），**不要**写进信箱 `<home>` 根下——
  否则两套系统共管一个目录。

**manifest 要点**（Plugin 形态）：

```jsonc
{
  "id": "anp.adapter.<platform>",   // ★必填；也是插件的 ANP 身份名；^[a-z0-9][a-z0-9._-]*$
  "name": "<Platform> Adapter",
  "version": "0.1.0",               // 插件自己的 semver，Core 不解析
  "api_level": 1,                   // ★必填；> CORE_API_LEVEL 则拒绝加载
  "command": ["python", "plugin.py"],   // ★command / url 严格二选一★
  "description": "...",
  "permissions": ["tools.register", "agents.discover"]
}
```

- `command` 与 `url` **两个都有 → invalid；两个都没有 → invalid；只有一个 → 通过**。
- **都不带任何模板占位符**——端口通过环境变量 `ANP_PLUGIN_PORT` 给，配置在 `/anp/register` 时给。
- `url` 主机必须是回环（`127.0.0.1` / `localhost` / `::1`）。
- 刻意**不放**在 manifest 里的：`tools` / `events` / `panel`（贡献点在 register 阶段声明）、
  `config_schema`、`capabilities`（就是 `permissions`）、`sdk_version`（用 `api_level`）、`env`。

**生命周期只有四个动词**：`load` / `enable` / `disable` / `unload`。
（`reload` = `disable` + `load`；`start`/`stop` 是进程生命周期，不是插件状态。）

**进程事实三分离（硬规则）**：插件状态 = 操作人意图；进程存活 = 不跟踪；端点可达 = 每次投递观测。
**三者永远不要互相推导。** `Core 拉起 ≠ Core 保活`：没有 supervisor、不自动重启、不探活；
插件死了只是它的端点连不上，对 Core 而言就是一次唤醒失败。

**依据**：SDK README「装/带」、Plugin Spec §2、§2.2、§2.4、§3、§3.1、§3.2。

---

## ⑧ 验收：两条链 + 八项边界

### 两条链都要证明能真正跑通

```
正向：Core → Adapter → Agent → 平台 Workspace / Session
反向：平台 Workspace / Session → Adapter → ANP Envelope → Core → 其他 Agent
```

### 必测八项

```
① 精确 Agent ID
② 模糊目标且唯一
③ 模糊目标多候选
④ 不存在目标
⑤ 新 Workspace / 新 Session
⑥ 已有 Workspace / 新 Session
⑦ Adapter 重启后的恢复
⑧ ANP 消息回复
```

### 自检清单（逐条过，任何一条不过就说明还缺东西）

```
身份
  □ Adapter Plugin 身份与 Agent 身份是两个不同名字、两枚不同令牌
  □ Agent 令牌只在投递/收发时用；plugin 令牌只用于 /plugin/register

寻址（可选，做了就逐条过）
  □ 精确 Agent ID → RESOLVED，且不会再去匹配别人
  □ 候选唯一 → RESOLVED；多个相近候选 → AMBIGUOUS + candidates（★不自动挑★）
  □ 没有任何匹配 → NOT_FOUND
  □ last_seen 只用于排序；last_used（平台记录）才可破并列，差距不足时仍 AMBIGUOUS
  □ 候选视图分开回报 last_used / last_used_source / last_seen（★不互相顶替★）
  □ resolve 与 send 的失败分得开
  □ 解析只用公开名册 + 本地平台提示；没有把平台语义塞进 Core

注册
  □ 首次 POST /heartbeat 无密钥能拿到 key
  □ 第二次同名字无密钥 → 401（不是重新发 key）
  □ 同名字 + 正确 key → 200 续约
  □ 不带 wake 的续约心跳不会抹掉既有 wake 端点
  □ wake: null 能主动清掉端点

唤醒（按你选的档位）
  □ http：/anp/wake 收到 {"message":…,"to":…}，x-mailbox-key 里是 Agent 令牌
  □ http：★响应含合法 mode；仅 {"ok":true} 不算签收★
  □ exec：子进程能从 AGENT_ANP_WAKE_JSON / SELF / SELF_KEY 三项环境变量拿到信封
  □ queue：agent 能自己 POST /messages/receive 取到信

接口
  □ 只用 §6.1 六件套 + 已声明的通道扩展面（`/session/roster`、`/session/bind`），没有读任何 Core 内部文件
  □ 没有出现过 hub signing secret
  □ /send 已换成 /messages/send（正名）

状态
  □ 没有向 Core 上报过 {"status": "online"} 这类复合状态
  □ 没有从 liveness 推导 reachability，或反之

收发闭环
  □ 收信 → 平台处理 → 回信，回信带 reply_to + correlation_id
  □ 把一封信的 body 改一个字，你的回信仍然发得出去（信封签的是正文）
  □ need_reply=true 才要求回信；缺省为知会（S4）
  □ 平台执行失败有补偿（S3），不是只写 log
```

> **`ok=true` 不是验收通过。** 状态要读 `wake` 与消息记录字段；"注册成功"更不是"通了"。

### 发现缺口怎么办

**先记录缺口 + 证据，不要擅自扩大 Core。**
按 `Core / Spec / SDK / Adapter` 四类归类，附上真实踩到的现象（命令 + 输出）。
只有确认是**规范本身错误**才另开变更。

**依据**：Adapter Spec §11、§9、§12；SDK README「自检」。

---

## ⑨ 交付时给出

```text
接入结果
修改文件
测试结果
发现的 Spec / SDK 缺口
是否需要 Core 侧修改
```

**接入的真正目的不是"硬接进去"，而是验证**：
> 一个真实、复杂、自己也拥有插件与 Workspace/Session 模型的第三方 Agent 宿主，
> 是否能仅依靠 ANP Spec + SDK + Reference Adapter 接入 ANP。

---

## 踩过的坑（照抄避免）

1. **`on_wake` 只回 `{"ok": true}`** → Core 不签收，信保持未读并被反复重投。必须带合法 `mode`。
2. **把 delivery mode 写进响应 `mode`**（`"mode": "http"`）→ 不签收。两个 `mode` 不是一回事。
3. **`/anp/wake` 的 `x-mailbox-key` 用错令牌**（用了 plugin token 或 hub secret）→ 应为**目标 Agent 的 token**。
4. **"Plugin + wake" 当作 Adapter 的定义** → 任何 Plugin 本来就有 wake；本质是**代管 + 翻译**。
5. **每个 Workspace 注册一个 ANP Agent** → 平台原生对象不是 ANP 身份，映射留在 Adapter。
6. **用 `wake.kind` 判断投递是否成功** → 只在**失败**时才有 `kind`，判断用 `wake.ok`。
7. **`register` 收紧成 `allowlist`/`closed` 却没把代管名字加进 `register.allow`** → 代管 403，
   "agent 身份根本不存在"，看起来像权限报错其实是缺名字。
8. **真实部署下用 `exec` 唤醒引用的脚本在 `<home>/plugins/` 里** → 被信任校验拒绝；Plugin 应用 http。
9. **依赖仓库根的 `../sdk`** → 装进 `<home>/plugins/<id>/` 后 `ModuleNotFoundError` 秒退，
   Core 只报"端点不可达"。必须 vendor 或安装。
10. **插件去读 `keys.json` 拿令牌** → 禁止；令牌由 Core 投递时捎带。
11. **`last_seen` 冒充 `last_used`** → 会把"谁最近动过"悄悄改写成对用户意图的猜测。
12. **把 `ok=true`、HTTP 2xx、或 `{"status":"online"}` 当成"通了"** → 三者都不是投递/消费的证据。
13. **`queue` 档被当成"没接好"去硬塞 push** → queue 是合法终态（`attempts=0`、不触发 undelivered）。
14. **平台状态写进信箱 `<home>` 根下** → 两套系统共管一个目录，应放插件自己的 `data/`。

---

## 这个 skill 本身放哪

- **正文放仓库**：`skills/anp-connect/SKILL.md`（纳入版本管理，随规范一起改）。
- **宿主的 skill 目录只放一个目录联接**（不是复制，改仓库即生效，不用两边同步）：

  ```powershell
  New-Item -ItemType Junction `
    -Path "<宿主 skill 目录>\anp-connect" `
    -Target "<repo>\skills\anp-connect"
  ```

  `<宿主 skill 目录>` 按各宿主自己的约定（不同宿主放 skill 的位置不同），本 skill 不假设具体位置。
  **不要复制副本**，副本必然漂移。

## 相关文件

**通用依据（必读，接入任何平台都要看）**：

| 文件 | 作用 |
|---|---|
| `references/ANP-公开面索引.md` | 公开面地图：每个概念对应哪份文档哪一节，缺口清单 |
| `references/接入形态决策.md` | 形态判断树、两身份模型、delivery mode 选择表 |
| `references/映射与寻址.md` | 对象映射表模板、Workspace/Session 映射、寻址规则 |
| `references/验收与缺口.md` | 两链验收步骤、必测八项、缺口归类模板 |

**可选参考（某一类宿主/平台的实例，不是接入门槛）**：

| 文件 | 作用 |
|---|---|
| `references/参考实例-插件型宿主.md` | 一类宿主（**自带插件系统 + 多 Workspace/Session**）的接入实例，用占位符写成，不是任何具体产品的档案 |
| `examples/adapter_demo/` · `examples/adapter_plugin/` | 两种形态的最小可跑参考实现 |
| `adapters/<platform>/` | 仓库里已有的真实平台 Adapter，**只作形态参考，不要复制它的平台逻辑** |
