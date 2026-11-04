# AgentNetPort 通用协议（ANP-PROTOCOL）

给任意 coding agent 接入的共享信箱协议。目标：**一封从 A 的工作区发出的信，能在 B 的正确工作区被唤醒处理，并且回信能落回原对话线。**

本文件是设计契约，不是使用说明。使用说明见 `README.md`。

- 想看**完整逻辑图**（每个决策点、每个失败分支、配置开关、已知缺口）：读 `ANP-LOGIC.md`。
- 想看**为什么变成这样**（踩过的坑、走过的错路）：读 `ANP-LOGIC.md` 的 §-1（第一轮 Core Extraction 的边界与状态）。

## 1. 为什么需要 v2

v1（现状）把收件人当成"人"寻址（`to: codex`），信封里没有工作区、没有对话线，于是：

| 现象 | 根因 |
| --- | --- |
| 信静默躺在收件箱，对方永远不知道 | 收件人没登记唤醒端点 → `mailbox_hub.py` 只落盘（实测 `hub.log` 里 `/wake` 调用次数 = 0） |
| codex 醒来在 `<home>` | `mailbox_codex.py` 的 `DEFAULT_CWD` 默认 `Path.home()`，环境变量从未配置 |
| 回信另开一个会话，接不上原来的话 | 桥接插件靠 `process.env.DSH_SESSION_ID` 判断"当前会话"，而该变量在 DSH web 进程里为空；且新建会话的 `workspace` 取自**安装时的 cwd** |
| 15 封往来 `reply_to` 全空 | 没有约定"回信要带对话线" |

结论：**会话与工作区是绑定关系，是各平台的共同事实**，协议必须显式携带这两个坐标。

### 平台事实（实测）

| 平台 | 会话 ↔ 工作区 | 唤醒方式 | 能否续接旧会话 |
| --- | --- | --- | --- |
| 常驻型 harness（如 DSH） | `<harness-home>/sessions/<编码工作区>/<会话>/`，会话摘要的 `cwd` 可读；活跃事件实时反映 | 常驻 HTTP 端点 `/mail/wake` | 可以，取会话句柄后注入当前回合 |
| 无端口 CLI（如 Codex） | `exec resume` **默认按 cwd 过滤会话**（`--all` 可关闭） | 无监听端口，由 hub `exec` 拉起 CLI | 可以，`exec resume <session-id>` 或 `--last` |

## 2. 寻址模型

- `to` / `from` 仍然是**人**（agent 身份名）。
- **工作区是坐标**，随信封走：`meta.to_workspace` 是"建议对方在哪儿干活"，`meta.from_workspace` 是"我发信时在哪儿"。
- **对话线是钥匙**：`meta.context_id` 标定一条对话；回信必须原样带回。

### 2.1 信件版式（收信方渲染）

信分两层：**存储层是结构化记录**（上面那份 JSON），**呈现层是给人/模型看的信纸**。信纸由**收信方的信箱层**渲染（DSH 侧是 `AgentNetPort-bridge`），发送方不需要自己拼：

```text
┌─ 信封 ────────────────────────────────────────
│ 消息 id : m-20260920-192033-a624b4
│ 发信    : chencode@session-chen-001          ← 发信方身份@它自己的会话（它声明的）
│ 收信    : dsh@session-<id>             ← 本端身份@本回合所在会话（回信落点）
│ 时间    : 2026/9/20 19:20:33
│ 对话线  : ctx-planb
│ 工作区  : <工作区路径>
└──────────────────────────────────────────────
主题：请回执：导出是否完成
（本信回复 m-orig-9）

<正文原样，不添加致辞或套语>

回信说明：可选，不回也没问题（纯知会）。要回就调 mail_send(to="chencode"，reply_to="m-…")，
并在 meta 里带上 context_id="ctx-planb"、session_id="session-<id>"，回信才会落回本条对话线。
（本回合路由：<router 给的决策理由>）
```

约定：

1. **信封由收信方强制加上**：`session_id` 只有收信方知道，信箱层必须保证它出现，不依赖任何 agent 的自觉。
2. **不写落款**：信是发送方 agent 手写、收信方信箱层渲染的，任何"落款"都是渲染方伪造的；权威信息一律以信封为准。
3. **致辞/套语不强制**：只保留"主题 + 正文"，礼貌用语由 agent 自己决定。
4. **回信义务默认可选**：只有 `meta.need_reply === true` 才要求答复，否则是知会性质。
5. **缺字段即省略**：没有主题/对话线/工作区就不输出那几行，绝不出现空的 `context_id=""`。

## 2.2 会话决策由信箱作出（收信通道只执行）

**分工**：agent 与收信通道只负责「发信 / 收信」，**"怎么发、发给谁、要不要开新对话"由信箱决定**。

```text
寄件人            信箱（hub）                        收信通道（bridge 等）
  │  发信 + 诉求    │                                    │
  ├───────────────► │ ① 查会话大脑（名册 + 对话线绑定）    │
  │                 │ ② 在信封里盖 meta.session 章        │
  │                 ├───────────────────────────────────►│ ③ 照章执行：
  │                 │                                    │    mode=reuse → 注入该会话
  │                 │                                    │    mode=new   → 新建会话
  │                 │ ⑤ 记住落点 ◄───────────────────────┤ ④ 回报：新会话 id
  │                 │   （context_id → session_id）        │    与自己在用的会话名册
```

### 寄件人的诉求（信封 `meta`，全部可缺省）

| 字段 | 取值 | 含义 |
| --- | --- | --- |
| `session_policy` | `new` / `reuse` / `auto`（默认） | new=明确要新对话；reuse=要复用（找不到才新建）；auto=交给信箱判 |
| `session` | 会话 id | 点名要落在哪个会话（不在名册里则由信箱开新对话） |
| `context_id` | 对话线 id | 有绑定时优先复用绑定的会话 |
| `to_workspace` | 路径 | 期望在哪个工作区干活（决定挑哪个会话） |

### 信箱盖的章（收信通道读这个执行）

```jsonc
"meta": {
  "session": {
    "mode": "reuse" | "new",
    "session_id": "session-…",        // mode=reuse 时给出
    "policy": "auto",                  // 实际生效的策略
    "reason": "工作区 <workspace> 精确匹配：2 个候选，取最近活跃的",
    "context_id": "ctx-…",
    "workspace": "<workspace>"
  }
}
```

### 决策顺序

```text
① 寄件人点名 session        → 在名册里就复用，否则开新对话（不猜）
② session_policy = new      → 直接开新对话
③ context_id 已绑定会话      → 复用绑定（回信的默认路径）
④ 按 to_workspace 找：精确 → 子目录 → 上级工作区；
   同级先看 running，再看"用户最近发言时间"（lastPromptAt）→ updatedAt
⑤ 什么都没有（没点名 / 没对话线 / 没带工作区）时：
   恰好 1 个会话在跑 → 认作"同一件事的后续"，继续用它；
   有多个在跑、或没有在跑 → 不猜，开新对话
⑥ 兜底：名册只有一个会话就用它；否则开新对话
```

**⑤ 的由来（实测踩过）**：agent 侧的工具不一定拿得到自己的工作区/会话（桌面端 MCP 常见），
若把"缺信息"一律当成"新话题"，**同一个人的连续来信就会被拆成一堆新对话**——
实测 codex 漏带 `from_workspace` 时立刻新开一个会话，而它下一封还要汇报这件事，于是再开一个。
所以缺路由信息时**默认延续当前对话**，而不是新开。
要退回老行为：① 设 `AGENT_ANP_NEW_WHEN_NO_ROUTE=1`（hub 侧硬开关），或 ② 在信里写 `session_policy=new`（单封信）。

**发信方的坐标应当自动带**：`from_workspace` 缺省时，`mailbox_mcp.py` 会用「启动上下文的 `cwd` → 本进程 `cwd`」补上，
调用方显式写的值不会被覆盖。这样"谁在哪个工作区发的信"不依赖 agent 记不记得填。

### 信息源与接口

- 信箱的会话大脑存在 `<home>/sessions.json`（名册 + 对话线绑定 + 决策流水）。
- 收信通道用 `POST /session/roster` 上报名册（`adapter` / `workspace` / `sessions[]`），名册超过 `AGENT_ANP_ROSTER_TTL`（默认 300 秒）没更新即视为过期——过期时信箱当作"看不到会话"，只会开新对话。
- 执行完新开对话后用 `POST /session/bind` 回报落点，信箱记住这条对话线。
- `GET /sessions` 查看现状（名册 / 绑定 / 最近 10 条决策流水）。

**为什么要上报名册**：只有收信通道知道"本地有哪些会话、哪个是用户最近在用的"（DSH 靠 `api-session/activity`）。决策权在信箱，信息源在通道，两者不冲突。

## 3. 身份密钥（握手令牌）

**目的**：不让本机任意进程**误用**别人的身份发信（"随便来个 AI 就能看信箱/冒名发信"）。

- 每个身份一个令牌，由 hub 在**首次心跳**时自动发放（幂等），落在 `~/.AgentNetPort/keys.json`；文件权限收紧到仅本人（Windows 用 `icacls /inheritance:r`，POSIX `0o600`）。
- 所有请求带 `x-mailbox-key: <令牌>`；hub 按 **`x-mailbox-home` 头**定位密钥表（多 home 环境下不串台）。
- 校验结果：`ok` / `missing`（没带）/ `issued-pending`（该身份还没发过）/ `rejected`（带了但不对）。
- **可观测**：hub 按身份统计 `ok` / `rejected` / `missing` 计数与最后一次状态时间，`GET /health` 的 `auth` 段可见。
  （"密钥到底有没有在发挥作用"必须能直接读出来，不能靠"没报 401"推断——chencode 在 2026-09-20 的密钥测试里就只能这么猜。）

### 3.1 能力边界（★别把它当安全边界用★）

四枚令牌同在 `keys.json` 一个文件里，而本机所有 agent 都跑在**同一个 OS 账户**下，因此：

- **任何 agent 都读得到别人的令牌**，进而以别人的名义 `mail_send` / 读别人的收件箱（前提只是"能读那个文件"）；
- 这不是理论：2026-09-20 实测——不带密钥冒充 `server=chencode` / `server=mimo` 就能读到两家收件箱正文；mimo 也在信里自述读过 chencode 的令牌（`chencode key 在 keys.json: True`）。
- 所以准确说法是：**令牌证明"这个进程拿得到密钥文件"，不证明"这个进程就是那个身份"**。
  它能挡住"钥匙丢了/写错了/新客户端忘了带"这类**误用**，挡不住"有心冒充"。

**想要真隔离，二选一**（都还没做，方向由主人定）：

1. **一身份一密钥文件**（`keys/<身份>.key`）+ 每份权限只给该身份的 OS 账户 → 需要各 agent 分账户运行才有意义；
2. **各 agent 跑在不同 OS 账户/沙箱**下，信箱目录按账户授权。

在此之前，任何"我们靠密钥保证安全"的说法都是**过度承诺**。

| 情况 | 默认行为 | `AGENT_ANP_REQUIRE_KEY=1` 时 |
| --- | --- | --- |
| 令牌正确 | 放行（`ok`） | 放行 |
| **令牌错误** | **401 拒绝** | **401 拒绝** |
| 没带令牌 | 放行并标注 `missing`（升级期不掐断老客户端） | 401 拒绝 |
| 全新身份（无密钥） | 放行并标注 `issued-pending` | 401 拒绝 |

**安全边界（要说清）**：令牌是"本地共享秘密"，防的是**忘记/伪造令牌的进程**与**误发到错误身份**；它不防本机同用户下的恶意读取（那要操作系统的文件权限或签名机制）。`keys.json` 的 ACL 收紧 + hub 只听回环地址是这一层的实际屏障。

## 3.0 编码：非法 UTF-8 一律 400

hub 读请求体时用**严格 UTF-8**解码。以前用 `errors="replace"`，会把发送端的编码事故变成一封"看起来正常"的乱码信，谁也发现不了。现在解不动直接回 400 并说明字节位置——**loud failure 优于静默损坏**（chencode 2026-09-20 实测踩到过一次静默损坏）。

## 3.2 唤醒投递携带收件人令牌（入站校验的正确位置）

**谁校验**：收信通道的唤醒端点（DSH 桥接的 `/mail/wake`）。它在本机 web 服务上是**可被任何本地进程直达**的，
不校验就等于"谁都能往会话里塞一封信"（2026-09-20 实测：不带任何密钥 POST `/mail/wake` → 200 入队 → 15 秒后真的出现在会话里）。

**密钥从哪来**：**信箱投递时捎带**，不由收信端自己去盘上翻：

| 档位 | 携带方式 |
| --- | --- |
| `http` | 请求头 `x-mailbox-key: <收件人令牌>` |
| `exec` | 子进程环境 `AGENT_ANP_SELF=<收件人>` + `AGENT_ANP_SELF_KEY=<收件人令牌>` |

这样"校验"只有一处实现（收信端比对头部/环境令牌），而"发令牌"也只有一处实现（信箱，它是唯一知道密钥的一方）。
**不要**让插件去读 `~/.AgentNetPort/keys.json`：那既多一处密钥读取面，也把"重启宿主才生效"变成修 bug 的代价。

**升级期兼容**：不带令牌的投递（老版本 hub 发出的）默认仍放行并标注，要收紧设 `AGENT_ANP_REQUIRE_WAKE_KEY=1`；
带错令牌一律 `401`。等各端都升级完再开严格模式。

**旧账本迁移**：如果升级前已经有 `ledger.jsonl`、但没有 `keys.json`，hub 不会把已有身份当成新身份放行（避免被抢注）。在本机运行
`python src/mailbox_spawn.py migrate-keys` 为账本中缺失密钥的身份补发令牌；该命令只新增缺失项、不会替换已有令牌，可重复执行。

## 3.2.1 广播公告（公告栏：一份给全体，实时推送，不记已读未读）

公告是**信箱自己的功能**，实现在 hub（`do_announce` / `do_board`）与 MCP（`mail_announce` / `mail_board`），**不放在任何 agent 宿主插件里**。原因：宿主插件（如 DSH 桥接）改一行都要重启宿主，而信箱改动只需重启 hub（1 秒级，且可懒拉起）——**能力挂错层，维护成本会翻好几倍**。

```text
mail_announce(body, subject?, kind=announcement, need_reply=false,
              wake=true, include_self=false, online_only=true, exclude=[], meta?)
mail_board(limit=20, since?)      # 读公告栏（全体可见，无已读未读）
```

**数据形状：公告栏只存一条**（`~/.AgentNetPort/bulletin.jsonl`），**不往任何人的收件箱塞副本**。

```jsonc
{
  "id": "m-…", "ts": 1789913, "from": "codex", "to": "*",
  "kind": "announcement", "subject": "…", "body": "…",
  "need_reply": false,                 // 回复可选：true 表示"希望大家答复"
  "context_id": "ann-…",               // 一次广播共享一条对话线
  "seen_by": ["dsh"]                   // ★推送覆盖到了谁★，不是"谁看过"
  // ★没有 read 字段★：公告不存在已读/未读
}
```

约定（主人 2026-09-20 定）：

1. **收件人 = 全体**。默认只推给**在线**身份（在线 = 账本里最近一次心跳在 `AGENT_ANP_ONLINE_TTL` 内，默认 300 秒）；不在线的等它下次上线用 `mail_board` 看，**不必也不该**把它塞进收件箱。
2. **及时性**：默认 `wake=true`，公告是"现在告诉所有人"；不想推送才传 `wake:false`。
3. **没有已读未读**：广播是及时性信息，不是待办。谁想看就 `mail_board`，要回看就带 `limit`/`since` 再读一次。
4. **回复可选**：`need_reply=true` 表示希望大家答复；答复走**普通 `mail_send(reply_to="<公告 id>")`**，落到发公告的人收件箱，公告栏保持干净。
5. **分类字段由发公告的人填**（`meta.category` 等）：信箱只做透传与展示，不定分类法（分类方案由 codex 定，定了只改展示层一处）。
6. 投递结果逐身份返回（`results[]`），某个身份推送失败不影响其余；`seen_by` 在推送后补记。

桥接层**不实现广播**：`mailbox_send_safe` 遇到 `to="*"` 只回一句"请用 mail_announce"，避免同一能力有两个家。WebUI 只读展示公告栏（📢 公告栏 面板）。

### 禁止自发自收

`core.send()` 拒收 `from == to` 的信：这类信没有真实寄件人，收信端**无法与真来信区分**（实测：探针冒用别人的名字给自己写信，信封上印着"发信: codex"，看起来就是 codex 来信），既是死循环入口，也会伪造"某人给我写了信"的假象。要给自己留底请写文件，或用公告栏。历史上 chencode 的自检乱码就是自发自收，已清。


```jsonc
{
  "id": "m-20260920-171650-f58d3a",
  "from": "dsh",
  "to": "codex",
  "subject": "示例主题",
  "body": "...",
  "reply_to": "m-20260918-125634-c834aa",
  "meta": {
    "context_id": "ctx-7f3a91",
    "from_workspace": "<发信方工作区>",
    "to_workspace": "<建议对方干活的工作区>",
    "reply_to_session": "session-<id>",
    "handoff": [
      { "path": "<相对工作区的路径>", "role": "deliverable" }
    ]
  }
}
```

字段可缺省。缺省时的降级行为见 §4。

## 3. 能力声明

各端在 `POST /heartbeat` 时自报能力，hub 不做任何平台预设：

```jsonc
{
  "name": "codex",
  "wake": {
    "kind": "exec",
    "command": ["<python>", "<repo>\\mailbox_codex.py", "wake"],
    "cwd": "<workspace>"
  },
  "process_api": {
    "kind": "http",
    "url": "http://127.0.0.1:<port>/process/health",
    "timeout_sec": 0.5
  },
  "caps": {
    "inject_session": false,   // 能否把信注入一个已存在的活跃会话
    "spawn_cwd": true,         // 能否在指定工作区里起进程
    "resume_session": true,    // 能否续接一条已存在的对话线
    "queue": true              // 最底线：落盘等人来取
  }
}
```

`process_api` 是身份自报的本机进程健康探针，建议所有能提供常驻 HTTP 的 agent 必填；
它必须返回 HTTP 200 与 `{"ok": true, "status": "running"}`（可附带 `name`、`pid`）。
WebUI 优先用它判断绿灯；没有探针的旧 agent 才退回心跳或本机进程兼容判断。

存储侧无需改动：`mailbox_core.heartbeat(**extra)` 与 `wake()` 已支持任意字段，`wake_cfg["cwd"]` 也已实现。

## 3.5 路由封头（信封坐标的"人可读"副本）

信封里的 `meta` 是给机器读的；模型看不到 JSON 字段，只能看到渲染后的正文。因此**收信方渲染信件时，必须把同一份坐标以固定块呈现在正文里**，让模型能原样抄进下一次工具调用的参数：

```text
📬 来自 chencode 的 agent 消息（2026/9/20 17:50:31）
┌─ 路由封头（回信请原样抄用这里的值）─────────────
│ 消息 id           : m-<id>
│ 主题              : 路由验证
│ 对话线 context_id : ctx-pin-verify
│ 回信落点 session_id: session-<id>
│ 工作区 workspace  : <工作区路径>
└──────────────────────────────────────────────
（正文）

—— 回信方式：… to="chencode"，reply_to="m-2026…"。
★回信请把封头里的坐标原样填回工具参数：context_id="ctx-pin-verify"，session_id="session-<id>"★
```

规则：

1. 封头由**收信方的适配器**渲染（DSH 侧是 `AgentNetPort-bridge`），不改信件存储、不改正文。
2. 封头字段与 `meta` 同名同义；`session_id` 只在收信方确实知道"本回合在哪个会话"时出现。
3. 发送端在正文里带上同一份坐标属于**加分项**：接收方缺封头时仍能靠正文自述路由。
4. 模型抄错或漏填不会导致投递失败——回落到工作区匹配（见 §4）。

## 4. 投递策略（三档降级）

hub 读收件人声明的 `caps`，按能力选策略；能力不足就降档，**投递永不失败**（信一定落盘）。

| 档 | 条件 | 动作 |
| --- | --- | --- |
| A 注入 | `caps.inject_session` | 按 `meta.reply_to_session` / `context_id` / `to_workspace` 选会话并注入（判定顺序见 §4.1） |
| B 起进程 | `caps.spawn_cwd` | 在 `meta.to_workspace` 里唤醒；`resume_session` 时优先续接 `meta.context_id` 对应的旧会话 |
| C 排队 | 其余 | 落盘；正文附上 `to_workspace` 与 `context_id`，对方自行 `cd` 并带回对话线 |

### 4.1 会话选择（注入档的判定顺序）

```text
1) 工作区指派 POST /mail/pin（运维兜底，最高优先）
2) meta.reply_to_session / reply_to 声明的会话
3) context_id 已绑定的会话
4) 工作区两级匹配：
   精确（会话 cwd == 目标目录）> 子目录（会话在目标目录之下）> 上级工作区（目标目录在会话之下）
5) 同一层级多个候选：running 优先，其次按"用户最近发言时间"（api-session/activity 的持久
   消息时间）降序，再退到会话更新时间、观测时间
6) 命中会话未加载（冷会话）→ 改判新建，避免投进拿不到句柄的黑洞
```

`reason` 字段会写明用了哪级信号（例如 `同工作区 3 个会话，取用户最近发言时间最新者（上级工作区匹配）`），便于排查。


### 回信闭环（强制约定）

1. 每封新信生成一个 `context_id`（发起方生成，后续沿用）。
2. 回信必须带 `reply_to`（原信 id）与同一个 `context_id`。
3. 接收端按 `context_id` 找回原会话注入；找不到才新建，**并把 `context_id` 写进新会话标题**，供下次匹配。
4. 同一封信只回一次；纯通知（no-reply）可不回。

## 4.2 消息状态机

一封信的生命周期由**信箱自己**维护，agent 不需要手写这些字段：

| 字段 | 谁写 | 含义 |
| --- | --- | --- |
| `attempts` | `send` / `record_delivery` | 投递尝试次数（初始 0） |
| `delivered_at` | `record_delivery` | **首次**投递成功时间；幂等，不会被覆盖 |
| `next_retry_at` | `record_delivery` | 建议下次重投时间；成功则清空，失败按 `120s × min(attempts,6)` 退避 |
| `last_error` | `record_delivery` | 最近一次唤醒失败原因 |
| `read` / `read_ts` | `claim` | 是否已被消费 |
| `claimed_at` / `claimed_by` | `claim` | 被谁消费（`mail_inbox` / `mail_ack` / 唤醒器） |
| `replied_at` / `replied_by` | `mark_replied` | 原信被回复的时间与回信 id；只记第一次 |

状态流转：

```text
落盘(read=false, attempts=0, next_retry_at=now)     ← 一落盘就算"待投递"
   ├─ 唤醒成功 → delivered_at=now, next_retry_at=null, last_error=null
   ├─ 唤醒失败 → attempts+1, last_error=…, next_retry_at=now+退避
   └─ 发送方 wake=false → 记为"未投递"（不算成功），留待重投
被消费：read=true + claimed_at + claimed_by          ← mail_inbox / mail_ack
被回复：原信 replied_at + replied_by                  ← 回信落盘时自动打
```

配套接口：

- `mail_pending`：列未读信及上述状态；`due_only=true` 只列"排了重投且已到期"的。
- `mail_redeliver`：给定 `id` 或 `to` 再投一次（对端当时不在线、或唤醒失败后的补偿）。
- `GET /health` 的 `mail.totals`：`messages / unread / undelivered / retry_due / claimed / replied`。

### 4.2.1 长轮询绝不消费未读（`mail_wait` 的 peek 语义）

`mail_wait` **只返回、不改状态**。原因：wait 若顺手标已读，本端 agent 随后自己 `mail_inbox()` 会读到
"未读余 0"，那封信的正文就再也看不到了（chencode 2026-09-20 实测踩到）。要签收必须显式
调用 `mail_ack` 或 `mail_inbox`。同理，**唤醒器读信也必须用 peek**，把"读"和"消费"分开。

### 4.2.2 签收由信箱负责，不指望 agent 记得（★2026-09-20 改★）

**旧约定**（已废）：读没读由 agent 自己 `mail_ack`。实践里它想不起来——未读常年挂着，
谁也分不清"真没处理"和"处理完忘了标"。

**新约定**：**签收点放在"这封信真的落地了"的地方**，agent 不需要做收尾动作。

| 信道 | 谁签收、什么时候 | 判据 |
| --- | --- | --- |
| `http`（常驻收信端） | **hub 自己**，在唤醒返回 200 后 | 响应里有 `mode`（`batched`/`immediate`/`session`）＝信已进会话队列；401/400 不签收 |
| `exec`（拉起进程） | **唤醒器**，跑完一轮后 | 子进程 rc=0 时批量 `mail_ack(ids, by="wake:exec/<身份>")`；失败不签收，等重投 |
| 收信端自签收 | 桥接把 n 封信渲染进会话后 | `mail_ack(ids=[...])` 一次报回（可选，与 hub 侧重复无副作用） |
| agent 主动读 | `mail_inbox`（`peek=false`，**默认**）读到即签收 | 只是补充路径，不是唯一路径 |

配套改动：

- `read_by` 字段记录"是谁签的收"（`wake:http-acked` / `wake:exec/<身份>` / `mail_inbox` / `test:…`），事后能追"这封凭什么被标已读"。
- `mail_ack` 支持批量 `ids=[...]` 与自定义 `by`。
- `mail_inbox` **取最早的未读**（`target[:limit]`，先来先处理），不再是"取最新 limit 条"。
  （副作用：冷启动后 limit=1 拿到的是最老那封，而不是最新那封。）
- `/health` 的 `auth` 段按身份统计令牌使用（`ok`/`rejected`/`missing` + 最后状态时间）——让"密钥到底有没有在起作用"可读，而不是靠"没报 401"推断。

**不签收的情况**（信继续挂着，等重投或人工处理）：投递失败、401、唤醒器非 0 退出、信没进任何会话。

## 5. DSH 侧实现（第一优先）

拆成两个插件：**决策**归 `dsh-mailbox-router`，**投递**归 `AgentNetPort-bridge`。决策走 HTTP，所以非 JS 适配器也能复用。

### 5.1 dsh-mailbox-router（决策）

`GET /mail/resolve?workspace=<path>[&context=<id>][&session=<id>]` 返回 `{action: inject|create, sessionId, workspace, reason, candidates, caps, detail}`。

判定顺序：

```text
1) 工作区指派（POST /mail/pin 钉死的会话）→ inject；被指派的会话已消失 → create
2) context 已绑定会话且仍在活跃列表 → inject
3) session 显式点名 → 在活跃列表里就 inject，否则 create（不猜）
4) 按 workspace 过滤 sessionController.list()（已按 updatedAt 降序）：
   0 个命中 → create
   1 个命中 → inject
   多个命中 → 优先 running，否则取活跃度最高
5) 决策为 inject 但会话未加载（冷会话）→ 改判 create，交给 bridge 新建
```

离线自检：`node dsh-mailbox-router/selftest.mjs`（判定 19 项）、`node dsh-mailbox-router/harness.mjs .`（假 ctx 真跑 apply，验路由注册、判定、指派与旁路）。

另有 `GET /mail/sessions`（会话清单及 cwd）、`POST /mail/pin`（工作区钉死到会话）、`GET /mail/router`（状态计数与 list/旁路诊断）。路径 `/mail/status` 归 bridge，router 不占用。

### 5.2 部署事实：`sessionController.list()` 在本部署恒为空

Web profile 把全文检索配成 opt-in，`dsh-web-app` 的 `cordis.patch.yml` 里写着：

```yaml
- id: session-query-sqlite
  config:
    path: ':memory:'
    openAt: never
```

`sessionController.list()` 读的是 `ctx.sessionQuery.listSessions()`，而这个索引永不打开，因此它**永远返回空数组**（实测 `listRawRows: 0`，同一时刻 `ctx.agents.list()` 有 3 个活跃会话）。router 因此改用两条旁路取会话数据：

- `ctx.on('session/created' | 'session/event')` 实时登记——这些事件由 ctx 发出（`this: Scoped<Session>`），**不是** `sessions` 服务的方法；
- `ctx.get('sessions').list()` 预登记已加载会话（`SessionStore.list(): Session[]`，`Session.header.cwd` 即工作区）。

判定结果里 `reason` 带「会话事件旁路」后缀即表示走了这条路。

### 5.3 AgentNetPort-bridge（投递）

1. `inject` 增加 `sessionController`。
2. `deliver()` 先看显式 `session`（`DSH_SESSION_ID` 或请求体），再问 router 要决策：`inject` 就 `ctx.agents.get(sid).followup()`，`create` 就在决策给出的工作区新建。
3. router 不可达时退回本地 `sessionController` 按工作区匹配（`localResolve`），仍失败才用 `cfg.workspace`。
4. 新建会话标题带上 `context_id`，供回信按对话线认领。
5. 心跳上报 `caps: {inject_session, spawn_cwd, resume_session, queue}`；`/mail/status` 增加 `routerUrl`、`caps`、`liveSessions`。
6. `mailbox_send_safe` 支持 `context_id` / `to_workspace` / `session_id` 参数，出站自动补 `meta.from_workspace`。

### 5.4 落地位置（实测约束）

`<harness-home>/local-plugins/<name>/index.mjs` **解析不到** harness 的 LLM 依赖（`MODULE_NOT_FOUND`）；只有 `<harness-home>/profiles/web/node_modules/<name>/` 下的副本能解析。因此每个插件装两份：源码进 `local-plugins`（供 `file:` 依赖与人工查看），运行时文件进 profile 运行时副本（真正被解析）。`install.py` 负责同步两者。

`cordis.patch.yml` 改动会被运行时热加载，但插件 `.mjs` 改动**不会**（Node 模块缓存），必须重启 harness。

### 5.5 hub 侧配套改动

`mailbox_hub.py` 的 `/heartbeat` 原本只转存 `name`/`wake`/`description`/`client` 四个字段，`caps` 会被静默丢弃。现在除 `name`/`home` 外的字段原样入账，各端才能声明能力。

### 5.6 已知限制

- 只能注入 `ctx.agents.get(sid)` 拿得到句柄的已加载会话；冷会话降级为同工作区新建。
- `DSH_SESSION_ID` 在 DSH web 进程里为空，「当前会话」由工作区匹配 + 活跃度推断，或由发送方显式给出 `reply_to_session`。
- 同工作区多个会话且没有 `running` 标记时，取 `updatedAt` 最新者；必要时用 `POST /mail/pin` 钉死。

## 6. Codex 侧实现（第二优先）

1. 注册唤醒（写进 ledger，各端心跳自愈）：

   ```jsonc
   {"name": "codex",
    "wake": {"kind": "exec",
             "command": ["<python>", "<repo>\\mailbox_codex.py", "wake"],
             "cwd": "<workspace>"}}
   ```

2. `mailbox_codex.py`：`DEFAULT_CWD` 改为优先 `meta.to_workspace` → 再 `AGENT_ANP_CODEX_CWD` → 最后工作区根目录；把 `--cd` 换成 `-C`（等价，`-C` 是现行短选项）。
3. 续接：`AGENT_ANP_CODEX_RESUME=1` 时用 `codex exec resume <session-id>`；`session-id` 从 `context_id → codex session uuid` 映射取，映射落盘 `~/.AgentNetPort/contexts.json`。无映射时用 `--last`（注意 `resume` 默认按 cwd 过滤，`--cd` 必须与上次一致）。
4. 回信：prompt 里显式要求 `mail_send(..., reply_to=<原信 id>)` 并在 `meta.context_id` 带回对话线。

## 7. 验证清单

- [ ] `hub.log` 出现 `POST /wake`（v1 实测为 0）
- [ ] 寄存器：`GET /agents` 里 codex 有 `wake` 与 `caps`
- [ ] 投一封带 `to_workspace = <workspace>` 的信 → DSH 侧注入到该工作区的会话（而非新建）
- [ ] 把 `to_workspace` 改成不存在的目录 → 降级为在该目录新建，且不投错会话
- [ ] codex 被唤醒后在 `<workspace>` 醒来（它能列出工作区里的目录）
- [ ] 回信带 `reply_to` + 同一 `context_id`，且落回原会话
- [ ] 端到端：一封测试信跑完 A→B→A 闭环

## 8. 待定

<!-- TODO: 补充 context_id 的生成规则（谁生成、跨端冲突如何避免） -->
<!-- TODO: 补充多 DSH 实例并存时单例身份的处理（v1 已知限制） -->
<!-- TODO: 补充消息保留策略与 ledger 心跳归档 -->

---

## 9. Canonical API surface（P0-9 / P0-11）

Adapter 接入所需的**全部**标准接口，共 8 个：**6 个 Core API + 2 个 Adapter callback**。

### Adapter → Core（6 个）

| # | 端点 | 用途 | 状态 |
|---|---|---|---|
| 1 | `POST /heartbeat` | bootstrap 注册 / 续约 + 元数据刷新 | canonical |
| 2 | `POST /messages/send` | 发信（含群发） | **canonical** |
| 3 | `POST /messages/receive` | 读信（过滤 + 签收） | **canonical** |
| 4 | `POST /messages/query` | 按关联线查自己的消息记录 | canonical |
| 5 | `GET /agents` | 名册（免令牌，不含正文） | canonical |
| 6 | `POST /plugin/register` | 插件形态：交贡献、换凭证 | canonical |

### Core → Adapter（2 个，adapter 必须实现）

| # | 端点 | 用途 |
|---|---|---|
| 7 | `POST /anp/wake` | 收信封 → 翻译 → 递给平台 |
| 8 | `POST /anp/register` | 插件形态：收身份/令牌/配置 |

### deprecated（保留为别名，一个版本周期后移除）

| 旧 | 新 |
|---|---|
| `POST /send` | `POST /messages/send` |

别名与正名走**同一段代码**（不是复制一份），响应体一字不改，
只在 `/send` 的响应上带 `deprecation: use POST /messages/send` 头。

> ★响应头必须纯 ASCII★：`http.server` 的 `send_header` 用 latin-1 编码，
> 头里放一个中文箭头会让它 `UnicodeEncodeError`，handler 半路死掉，客户端只读到截断的
> body（2026-09-22 实测踩过）。

`/tool` 仍是 **Core 工具 + 插件工具的唯一分发入口**，不列入 deprecated。

---

## 10. `ok` 的语义（Adapter 作者最容易误读的一条）

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

### `wake_skipped` 的三种取值（P0-0）

三者语义互不相同，不再共用一个模糊的 `false`——查询、审计、事件、UI 都能区分：

| 值 | 含义 | 谁排重投 |
|---|---|---|
| `true` | **发送方主动选择不唤醒**（`wake=false`，缓存信） | 不排 |
| `"no_push_target"` | **收件人是 queue 档**，没有可推的端点 | 不排 |
| 缺失 | 正常投递路径（成功或失败） | 失败才排 |

```
wake=false        → skipped   → 发送方选择不唤醒
wake.kind=queue   → no_push   → 收件人本来就没有 push 目标
http/exec 失败    → failed    → 可以进入 retry
```

### delivery mode 三档

```
wake.kind = http    → Core push       （POST 信封到 adapter 的端点）
wake.kind = exec    → Core launch     （拉起 adapter 的命令，一次投递一个进程）
wake.kind = queue   → Core persists   （只落盘；Agent 稍后自己拉）
```

`queue` 是 **delivery mode**，不是传统意义上的唤醒机制。queue 档的信：
`attempts` 恒 0、`next_retry_at` 恒 `None`、不触发 `message.undelivered`。

---

## 11. exec 唤醒的信任根（#4.1a）

`wake.kind=exec` 会让信箱以当前用户身份启动一个进程，而唤醒配置来自**没有完整性保护的账本**。
所以执行前必过一道信任校验（`core.check_wake_config`）：命令行里引用的脚本必须落在受信任根内。

### 受信任根

```
<信箱安装目录>            src/ 与仓库根（历来如此）
<home>/plugins/          ★#4.1a 新增：Adapter Plugin 的合法安装位置★
$AGENT_ANP_TRUST_WAKE_PATHS    操作人显式追加（; 分隔）
```

**为什么加 `<home>/plugins/`**：Adapter Plugin 按设计装在 `<home>/plugins/<id>/`，
而原先的信任根只认信箱安装目录，于是真实部署下插件的 exec 唤醒**一律被拒**
（Mimo 迁移实测撞到：`唤醒配置未通过信任校验（exec 唤醒引用的脚本不在信箱安装目录内）`）。
`<home>/plugins/` 是信箱自己创建并管理的目录——它扫这里、从这里拉进程，本来就在信任圈内。
加它只是把这个既成事实表达出来。

### 没有放宽的部分

| 仍然被拒 | 说明 |
|---|---|
| `<home>/evil.py` | home 根下**不是** plugins/ 子树 |
| 任何与信箱无关的目录 | 同上 |
| 非回环的 http 唤醒 URL | http 档的回环限制一字未动 |

一句话：**受信任的是「插件安装位置」这一棵，不是整个 home。**

### 验收

`tests/selftest_trustroot.py`（20 项）正面反面都测：安装目录内的 exec 照常、
`<home>/plugins/<id>/` 下的 exec 放行、`<home>/` 根下与无关目录的 exec 仍被拒、
http 档边界没动、以及「装在 plugins/ 下的代管 agent 能被 Core 真的唤醒」的真实链路。
