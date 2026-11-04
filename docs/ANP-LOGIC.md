
# AgentNetPort 完整逻辑（截至 2026-09-22）

**这份文档回答一个问题：一封信从"agent 想发"到"对方处理完"，中间所有可能的分支是什么。**

写的是**代码的真实行为**（逐个决策点读过源码），不是设计意图。凡"设计里想做但代码没做"的，一律标 ★未实现★。

---

## 0. 永久架构约束 + Core P0 封板

### 0.1 两条永久约束（主人 2026-09-22 定，已写成可执行代码）

| 约束 | 含义 | 可执行形式 |
| --- | --- | --- |
| **Agent token ≠ Hub signing secret** | `keys.json` 里的身份令牌与信封签名密钥是两串钥匙，**绝不允许互相顶替** | `core.secret_isolation_ok()` / `write_keys()` 落盘前校验 / `issue_key()` 发证漏斗——任一发现顶替即 `ValueError` |
| **插件身份认证 ≠ Envelope 签名密钥** | 插件因为"能调用 Core API"就顺手拿到 `hub.secret`，是**绝对禁止**的。插件拿到的只有它自己那一枚 agent token | 第 4 步落地时的硬性验收项：`/anp/register` 的响应体里不得出现 `hub.secret`；Core 不提供任何"读取 hub.secret"的接口（连管理员也不给——管理员要看的是 `/health.integrity` 的**结论**，不是密钥） |

 Hub 一旦把签名密钥发出去，"只有 Hub 能合法生成签名"就失效，第 3 步的完整性闭环变成装饰。

### 0.2 Core P0 基础层：已封板

以下五项是 ANP Core 的**最小必要集**，2026-09-22 起视为**稳定契约**：

```text
身份与寻址    身份令牌 · 心跳名册 · 单人/多人/插件地址 · caps + skills（只存只返回）
信封          id / ts / from / to / subject / body / kind / reply_to /
              correlation_id / meta / sig（HMAC）
传输          落盘 + 跨进程锁 + 原子写 + 编码清洗（丢不了信）
收发          send / receive(peek|ack|过滤) / wait
唤醒          http / exec / queue 三档原语 + 唤醒配置信任边界
可靠性        attempts / 退避 / 自动重投 / 已读未读签收 / 数据生命周期(prune+compact)
消息记录      journals/<关联线>（仅 owner 可读）+ messages.query
完整性        信封 HMAC（信封签、状态不签）；篡改可检出、可观测
安全          身份令牌 · 管理员令牌 · 权限策略 · 密度上限 · 请求体上限
```

**封板之后的原则**：

> Core 提供更稳定的原语，而不是 Core 开始理解应用。

后续即使加功能，也要能回答"这是通信本身，还是通信的一种用法"；是用法就走 Plugin / Adapter。
已经判定为寄居物、留待搬迁的三项（公告栏、`sessions.py` 会话决策、WebUI 仪表盘、DirectBackend）
见各节，**在 Plugin / Adapter 分层落地前不再往 Core 里加东西**。

---

## 1. 一句话总览

**agent 只做两件事：发信、收信。别的都归信箱。**

```text
                    ┌──────────────────────────────────────────┐
                    │  信箱 hub（常驻 HTTP，127.0.0.1:8900）      │
                    │  纯标准库 · 664 行 · 可被懒拉起 · 非 agent   │
                    │                                          │
   发信 ──────────► │ ① 验身份  ② 决定会话  ③ 落盘  ④ 唤醒  ⑤ 签收 │
                    └───────┬──────────────────────┬───────────┘
                            │ http 档              │ exec 档
                            ▼                      ▼
                   常驻收信端（DSH 桥接）      唤醒器（spawn/chen/codex/mimo）
                   注入 / 新建会话             拉起进程 → 读信 → 干活 → 回信 → 自签收
```

**hub 不读信的内容**。它只按规则转发、落盘、盖章、签名——是邮局，不是收信人。

---

## 2. 参与者与职责边界

| 角色 | 是谁 | 权限与职责 | **不做**什么 |
| --- | --- | --- | --- |
| **hub** | `mailbox_hub.py`（pid 常驻，可懒拉起） | 身份校验、会话决策、落盘、唤醒、签收、公告栏、体检 | 不读内容、不干活、不生成文字 |
| **agent** | dsh / codex / chencode / mimo | 用 MCP 工具发信收信、干活、回信 | 不管会话怎么选、不管投递怎么走、不用管未读状态 |
| **唤醒器** | `mailbox_spawn.py`（通用）、`mailbox_chen.py`、`mailbox_codex.py`、`mailbox_mimo.py` | exec 档的适配器：peek 读信 → 渲染提示词 → 拉进程 → 跑完自签收 | 不决定会话（那是 hub 的决定），不消费正文状态 |
| **MCP 客户端** | `mailbox_mcp.py`（stdio，身份由 `AGENT_ANP_SELF` 定） | 把 9 个工具翻译成 hub HTTP 调用；hub 不可达时降级 direct | 不做会话决策（direct 路径下★退化为"没有决策"★） |
| **收信通道** | DSH 桥接插件 `AgentNetPort-bridge` | 按 hub 盖的章注入/新建会话；上报会话名册；渲染信封；自签收 | 不自己挑会话（只在"信封没盖章"时兜底） |
| **router 插件** | `dsh-mailbox-router` | 本地会话查询 `/mail/resolve`（**已降级为兜底**，hub 才是决策方） | 不再是决策方 |
| **公告栏** | `bulletin.jsonl`（hub 提供读写） | 一份公告给全体；实时推送；**无已读未读**；可回看 | 不进任何人收件箱 |

---

## 3. 数据模型（`~/.AgentNetPort/`）

| 文件 | 内容 | 谁写 |
| --- | --- | --- |
| `inbox/<身份>.jsonl` | 该身份的**私有**信件流水（含状态机字段） | 信箱（send / record_delivery / sign_off / mark_replied） |
| `ledger.jsonl` | 身份名册：心跳（`ts`）、唤醒端点（`wake`）、能力（`caps`）、描述 | 各端心跳（hub 也给自己写一条，但**没有 wake**——它从不收信） |
| `sessions.json` | 会话大脑：`rosters`（各通道上报的会话名册）、`contexts`（对话线→会话绑定）、`decisions`（最近 200 条决策流水） | 收信通道上报 + hub 决策 |
| `journals/<关联线>.jsonl` | **关联线日志**：第一行是头记录（`owner` = 第一个发信的人），其后是该线全部信的副本。`mail_query` 只读这个，不读任何人的收件箱 | `core.send()` 带 `correlation_id` 时自动落 |
| `bulletin.jsonl` | 公告栏（只存一条条公告，**没有 read 字段**，`seen_by` = 推送覆盖到谁） | hub / MCP |
| `keys.json` | 身份令牌（**四枚在同一个文件里**，见 §9 能力边界） | hub 首次心跳自动发 |
| `logs/*.log` | hub 访问日志、各唤醒器日志（`spawn.log`、`chen-wake.log`…） | 各进程 |
| `launch-context.json` | "我是被哪条通道/哪个凭证拉起来的"（30 分钟过期） | 唤醒器写、MCP 读 |

**一封信的字段**（`inbox/*.jsonl` 里的一行）：

```jsonc
{
  "id": "m-20260920-224003-6134ad",   // 时间戳+随机
  "ts": 1789915203.6,                 // 发信时间
  "from": "mimo", "to": "dsh",        // 发信人 / 收信人（都是身份名）
  "subject": "…", "body": "…",
  "kind": "message",                  // message / announcement / …
  "reply_to": "m-…",                  // 这是对哪封信的回复（可空）
  "correlation_id": "x:1",            // 应用关联线（任务/游戏）；与 meta.context_id 分工见 §20.3
  "sig": "…",                         // ★信封内容 HMAC-SHA256（状态机字段不签）见 §21
  "read": false,                      // 已读（签收后为 true）
  "read_ts": null, "read_by": null,   // 什么时候、被谁签收的
  "claimed_at": null, "claimed_by": null,  // 旧字段：早期"消费"语义留下的（见 §14）
  "replied_at": null, "replied_by": null,  // 原信被回信的时间与回信 id
  "attempts": 0,                      // 投递尝试次数
  "delivered_at": null,               // 首次投递成功时间
  "last_attempt_at": null,
  "last_error": null,                 // 最近一次投递失败原因
  "next_retry_at": 1789915203.6,      // 下次建议重投（成功时置 null）
  "meta": {                           // 信封坐标 + 信箱盖的章
    "context_id": "ctx-…",            // 对话线
    "from_workspace": "<workspace>",
    "to_workspace": "…",              // 可空
    "reply_to_session": "session-…",  // 发信方自己的会话（回信落点）
    "need_reply": true,               // 这封信要求答复吗
    "session": {                      // ★hub 盖的章★（收信通道照这个执行）
      "mode": "reuse|new", "session_id": "session-…|null",
      "policy": "auto|new|reuse", "reason": "人话解释",
      "context_id": "ctx-…", "workspace": "<workspace>"
    }
  }
}
```

---

## 4. 发信全链路（含每个分岔）

### 4.1 入口：agent 调 `mail_send`

```text
mail_send(to, body, subject?, reply_to?, kind?, need_reply?, wake?, meta?)
   │
   ├─ hub 活着？ ──是──► POST /hub/tool {name, server=自己, args}
   │                      │
   │                      ├─ 验身份（§4.2）失败 → 401，信根本没落盘
   │                      └─ 通过 → do_send（§4.3 起）
   │
   └─ hub 不可达 ──► 先试懒拉起 core.ensure_hub()
                      ├─ 起来了 → 走上面那条
                      └─ 还是不可达 → **direct 降级**：
                          本地 core.send() 落盘 + 本地 wake()
                          ★注意：这条路径**没有会话决策**（hub 不在，没人盖章），
                            也没有 read_by 留痕与 replied 记账★
```

**记这一条**：hub 不在时的信会"少盖章"。所以"保持 hub 活着"不是偏好，是功能完整性的前提。

### 4.2 身份校验（`_auth`）

| 情形 | 结果 | 说明 |
| --- | --- | --- |
| 令牌正确 | 放行（`state=ok`） | 每次结果都计入 `/health` 的 `auth` 段 |
| 身份**从未发过**令牌 | 放行（`issued-pending`） | 否则新身份第一次心跳就被自己锁在门外 |
| 该身份有令牌，但请求**没带** | **401**（`missing`） | 身份名是请求方自己填的，不带就等于可随意冒充 |
| 带了但**不对** | **401**（`rejected`） | —— |
| 逃生阀 | `AGENT_ANP_REQUIRE_KEY=0` 放回宽松（missing 也放行） | 默认**强制** |

### 4.3 落盘前的两道门

1. **自发自收拦截**：`from == to` → `ValueError`，400 拒绝。理由：这类信没有真实寄件人，信封上却与真来信**无法区分**（实测伪造过，主人当场看穿）。
2. **正文非空**：`body` 去空白后为空 → 拒绝。

### 4.4 会话决策（`sessions.decide`，**按顺序短路**）

```text
① 点名 session          在名册里 → reuse 它
                        不在名册 → new（不猜）
② policy = "new"        → new（压过下面所有）
③ context_id 已绑定会话   → reuse 绑定的那个（回信的默认路径）
④ 有工作区信息           → 精确 → 子目录 → 上级，逐档找：
                            命中多个时排序 running > lastPromptAt > updatedAt，取第一个
                            ★一档命中就不再往下看★
⑤ 什么都没带            恰好 1 个会话在跑 → reuse（认作"同一件事的后续"）
   且无绑定              0 个或 ≥2 个在跑 → new（不猜）
⑥ 兜底                  名册只有 1 个会话 → reuse 它
                        policy=reuse 且名册非空 → 取最近活跃的
                        否则 → new
```

**名册过期判定**：通道上报的名册超过 `AGENT_ANP_ROSTER_TTL`（默认 300 秒）没更新 → 当作"看不到任何会话" → 只可能走 ②/⑤/⑥ 的 new 分支。

**逃生阀**：`AGENT_ANP_NEW_WHEN_NO_ROUTE=1` 让 ⑤ 退回老行为（缺路由信息就 new）。

**决策结果被盖章**成 `meta.session`，并记进 `sessions.json` 的 `decisions` 流水（含"投递是否成功"）。**这是事后追"这封信为什么落那儿"的唯一凭据。**

### 4.5 唤醒（`core.wake`，按登记档位分岔）

| 档位 | hub 做什么 | 成功判据 | 失败分支 |
| --- | --- | --- | --- |
| `http` | POST 收信端唤醒端点，带 `x-mailbox-key: <收件人令牌>` | 响应 `ok=true` 且带 `mode`（batched/immediate/session） | 连接失败 / 401 / 超时 → `ok=false` + `error` |
| `exec` | `Popen` 拉起 `command`，环境带 `AGENT_ANP_WAKE_JSON` + `AGENT_ANP_SELF` + `AGENT_ANP_SELF_KEY`/`AGENT_ANP_KEY` | 只表示"进程起来了"（拿到 pid） | 起不来 → `ok=false` |
| `queue` / 未登记 | 什么都不做 | —— | `ok=false`，原因写 `未登记唤醒端点` / `仅落盘` |
| `wake=false`（发信方指定） | 不唤醒，只落盘 | —— | `ok=false`，原因 `发送方指定不唤醒` |

### 4.6 投递结果记账（`record_delivery`，**幂等**）

```text
成功 → attempts+1、last_attempt_at=now、delivered_at=首次成功时间（不覆盖）、
        next_retry_at=null、last_error=null
失败 → attempts+1、last_attempt_at=now、last_error=原因、
        next_retry_at = now + 120 × min(attempts, 6)   ← 指数式退避，封顶 6 倍
```

★未实现★（曾经）→ **已实现（信箱自己做）**：见下方 §4.6.1。

**4.6.1 自动重投（2026-09-20 晚补）**

`record_delivery` 只把退避时间写进 `next_retry_at`；过去必须有人记得手动 `mail_redeliver`，
没人记得信就烂在箱底。现在 hub 自带一个 daemon 线程：

```text
每 AGENT_ANP_REDELIVER_SEC（默认 120 秒）扫一次
   └─ 取"从未投递成功 + next_retry_at 已到期"的信，每轮最多 AGENT_ANP_REDELIVER_LIMIT（默认 3）封
        ├─ 尝试次数 < AGENT_ANP_REDELIVER_MAX（默认 5）→ 重投一次
        └─ 达到上限 → 停手，把 next_retry_at 置空、last_error 写成"已放弃自动重投（…）"
                      （★不静默丢弃★：体检与 mail_pending 里看得见，要再试就手动 mail_redeliver）
```

关掉：`AGENT_ANP_AUTO_REDELIVER=0`。上一轮结果在 `/health` 的 `auto_redeliver` 段。
**边界**：只治"投递失败"；"送到了但对方没处理完"（`delivered_at` 有值、`read` 仍 false）不在自动重投范围内。

### 4.7 签收（`sign_off`，见 §6）

### 4.8 回信记账

`reply_to` 非空 → 给**原信**打 `replied_at` / `replied_by`（只记第一次）。
判据：原信躺在 `sender` 的收件箱里（回复方 = sender）。

### 4.9 发信返回给 agent 的东西

```jsonc
{ "ok": true, "mode": "hub", "message": {…}, 
  "session_decision": { "mode": "reuse", "session_id": "session-…", "reason": "…" },
  "wake": { "ok": true, "status": 200, "mode": "batched", … },
  "signed_off": 1,          // http 档当场签收的封数
  "auth": "ok" }
```

---

## 5. 收信全链路

### 5.1 http 档（DSH 这类常驻收信端）

```text
hub POST /mail/wake（带令牌）
   │
   ├─ 令牌不对/没带（严格模式）→ 401，**信不入队**
   ├─ 没有消息体 → 400
   ├─ batchWindowSec = 0 → 立即逐封投递（调试用）
   └─ 默认 → 按"目标"分组入队，15 秒窗口到点合并投递
              │
              ├─ 信封有 hub 的章 → 照章执行：reuse→注入指定会话 / new→新建会话+回报绑定
              └─ 信封没盖章（旧信/直连）→ 退 router 兜底 → 再退本地兜底 → 还不行就 new
              │
              └─ 渲染成"信封 + 正文 + 回信坐标"注入会话（作为一个真实回合）
```

**合并投递规则**：一批里**目标不同**的分成多组（各自投各自的会话）；`new` 独占一组。
`/mail/status` 暴露 `batch.{windowSec,pending,batches,merged,lastFlush}` 与 `auth.rejected`。

### 5.2 exec 档（唤醒器）

```text
hub Popen 唤醒器（环境带信封 + 令牌）
   │
   ├─ peek 读未读（**不改状态**：读与消费分开，否则 agent 醒来看到"未读余 0"、正文丢了）
   ├─ 没有未读 → 记日志退出（rc=0）
   ├─ 渲染提示词（含"to 填原信的 from，别写死 dsh" + 坐标要求）+ 拉真实 agent 进程
   │     └─ 子进程自己用 MCP 读信、干活、回信
   └─ 子进程 rc=0 → **唤醒器批量签收**（read_by=wake:exec/<身份>）
         rc≠0 → 不签收，信留着等重投
```

### 5.3 queue 档（C 档，如 mimo）

```text
hub Popen 适配器 → 落盘待取信摘要（mimo-pending.md/json）+ 记日志 → 退出
对端自己周期性 mail_inbox（消费）或 peek（只看）
```

### 5.4 agent 自己读

```text
mail_inbox(unread_only=true, limit=20, peek=false)
   ├─ peek=true  → 只看不改，未读状态原样
   └─ peek=false → 取**最早的** limit 封未读（先来先处理）→ 当场签收（read_by=mail_inbox）
```

```text
mail_wait(timeout_sec)  ← 长轮询，**永远是 peek**：只返回、不签收
```

---

## 6. 签收（谁把"已读"标上的）

| 触发点 | 谁签 | `read_by` | 判据 |
| --- | --- | --- | --- |
| http 档投递成功 | hub | `wake:http-acked` | 响应有 `mode`（信已进会话队列） |
| exec 档跑完一轮 | 唤醒器 | `wake:exec/<身份>` | 子进程 rc=0 |
| agent 主动读 | MCP / hub | `mail_inbox` | 调了非 peek 的读 |
| 人工/工具补签 | 任意 | `mail_ack` / 自定义 `by` | `mail_ack(id=…)` 或 `mail_ack(ids=[…], by=…)` |

**不签收**（信继续挂着）：投递失败、401、唤醒器非 0 退出、信没进任何会话。
**公告不签收**：公告栏没有 read 字段（`seen_by` 只表示推送覆盖到谁）。

---

## 7. 消息状态机（完整状态）

```text
                ┌──────────────┐
   落盘 ───────► │  待投递       │ attempts=0, delivered_at=null, next_retry_at=now
                └──────┬───────┘
                       │ wake
        ┌──────────────┴───────────────┐
        ▼                              ▼
  ┌───────────┐                  ┌───────────┐
  │  已投递    │                  │  投递失败  │ attempts+1, last_error 有值
  │ delivered │                  │           │ next_retry_at = now+120×min(n,6)
  └─────┬─────┘                  └─────┬─────┘
        │ 签收                          │ 手动 mail_redeliver 可回到"待投递"
        ▼                              └──────► 再投（成功→已投递）
  ┌───────────┐
  │  已签收    │ read=true, read_ts, read_by
  └─────┬─────┘
        │ 回信（对方带 reply_to）
        ▼
  ┌───────────┐
  │  已回信    │ replied_at, replied_by（只记第一次）
  └───────────┘
```

**别混淆的三个"已读"字段**：

- `read` / `read_ts` / `read_by` —— **现在用的**：签收（谁签的、什么时候）。
- `claimed_at` / `claimed_by` —— ★旧语义残留，且已无调用方★：早期"被消费"走 `core.claim()`，现在签收走 `sign_off()`；
  全仓检索确认 `claim()` **已无任何调用点**（只剩定义），这两个字段只在老信里出现，新代码不再写。
- `seen_by`（公告专用）—— 推送覆盖到了谁，**不是"谁看过"**。

---

## 8. 公告（广播）链路

```text
mail_announce(body, subject?, kind?, need_reply?, wake=true, include_self=false,
              online_only=true, exclude=[], meta?)
   │
   ├─ 在 hub：算"在线名单"（账本心跳 ≤ 300 秒）→ 去掉自己/hub/排除项
   ├─ 公告栏**只写一条**（bulletin.jsonl，无 read 字段，shared context_id=ann-…）
   ├─ wake=true 时逐个推送（推送的信封带 broadcast/context_id/need_reply）
   └─ 推送覆盖记进 seen_by

读公告：mail_board(limit=20, since?)   ← 全体可读，不看未读状态
回复：need_reply=true 时，走普通 mail_send(reply_to=公告 id) → 落到发公告人的收件箱
```

**在线判定可调**：`AGENT_ANP_ONLINE_TTL`（默认 300 秒）。
**在线 0 人是正常结果**，不是失败（公告已在栏里，谁上线谁看得到）。

---

## 9. 身份与令牌：能力边界

- 令牌在 `keys.json`（`secrets.token_hex(24)`），hub 首次心跳自动发。
- 请求带 `x-mailbox-key`；hub 按 `x-mailbox-home` 头定位密钥表。
- **可观测**：`GET /health` 的 `auth` 段按身份给 `ok`/`rejected`/`missing` 计数与最后状态时间。

★**能力边界（别当安全边界用）**★：四枚令牌在**同一个文件**里，而所有 agent 跑在**同一个 OS 账户**下 →
**任何 agent 都读得到别人的令牌，进而以别人名义发信**（已实测：不带密钥冒充 `server=chencode` 曾能读到其收件箱正文；mimo 自述读过 chencode 的令牌）。
令牌证明的是"这个进程拿得到密钥文件"，不是"这个进程就是那个身份"。
真隔离二选一（★都未实现★）：一身份一密钥文件 + 各自 OS 账户；或各 agent 分账户运行。

---

## 10. 失败分支总表（每一个都有可观测痕迹）

| 现象 | 触发条件 | 可观测痕迹 | 怎么处理 |
| --- | --- | --- | --- |
| `401 missing` | 该身份有令牌但请求没带 | `/health` 的 `auth.by_identity.<名>.missing` 增加；hub stderr 有 `[hub] 401` | 客户端补 `x-mailbox-key` |
| `401 rejected` | 令牌不对 | 同上 `.rejected` | 检查 `keys.json` 与客户端读取路径 |
| `400 不是合法 UTF-8` | 请求体非法字节 | 响应体 | 发送端显式 UTF-8；DSH 侧改用 `mailbox_send_safe` |
| `400 自发自收` | `from == to` | 响应体 | 别给自己写信；留底请写文件 |
| 落盘了但"未投递" | 收信端未登记唤醒端点、或唤醒失败 | `delivered_at=null`、`last_error`、`mail_pending` | 补登记（`register`）或 `mail_redeliver` |
| 信投了但"没人处理" | 唤醒器 rc≠0、会话回合失败 | `read=false`（未签收） | `mail_pending` → `mail_redeliver` |
| 公告"没投出去" | ★旧 MCP 渲染误判★（新进程已修） | 工具文案 | 用 `mail_board` 复核，公告其实已落盘 |
| 名册过期 | 通道 ≥300 秒没上报 | `GET /sessions` 里 `stale=true` | 收信端（插件）是否在跑 |
| 会话挑错/开新对话 | 缺路由信息且多会话在跑 | `sessions.json` 的 `decisions` 里的 `reason` | 信里带 `context_id` / `to_workspace`；或写 `session_policy` |
| **同一封信被投两次** | 投递进行中被自动重投捡走（租约缺失） | 收信端日志里同一 id 出现两次 | 已修：`claim_lease` 占位 + `send()` 初值不再是"立刻到期" |
| **并发下读文件失败** | Windows 字节锁：写者持锁时读者 `PermissionError` | `/health` 断连、客户端降级 direct | 已修：`read_text_shared()` 重试 + 写侧不再每次 fsync / 无改动不写盘 |
| **有人顶替别人的唤醒端点** | `/heartbeat` 以前无鉴权 | 账本里该身份的 `wake` 变了 | 已修：已登记身份改唤醒必须带对密钥 |

---

## 11. 配置开关总表

| 变量 | 默认 | 作用 |
| --- | --- | --- |
| `AGENT_ANP_HOME` | `~/.AgentNetPort` | 信箱数据根目录 |
| `AGENT_ANP_HUB` | `http://127.0.0.1:8900` | hub 地址 |
| `AGENT_ANP_SELF` | —— | 本端身份（每个 agent 必设） |
| `AGENT_ANP_HOST` / `_PORT` | `127.0.0.1` / `8899` | hub 监听 |
| `AGENT_ANP_REQUIRE_KEY` | `1`（强制） | 设 `0` 让"没带令牌"也放行 |
| `AGENT_ANP_REQUIRE_WAKE_KEY` | 空（宽容） | 桥接侧：设 `1` 则"没带令牌的唤醒"一律 401；设 `0` 则本端无令牌时也放行 |
| `AGENT_ANP_ROSTER_TTL` | `300` | 名册过期秒数 |
| `AGENT_ANP_ONLINE_TTL` | `300` | "在线"判定秒数 |
| `AGENT_ANP_RETRY_SEC` | `120` | 重投退避基数（× attempts，封顶 6） |
| `AGENT_ANP_AUTO_REDELIVER` | `1`（开） | 设 `0` 关掉 hub 的自动重投线程 |
| `AGENT_ANP_REDELIVER_SEC` | `120` | 自动重投扫描间隔（秒，最小 15） |
| `AGENT_ANP_REDELIVER_LIMIT` | `3` | 每轮最多重投几封 |
| `AGENT_ANP_REDELIVER_MAX` | `5` | 单封尝试到几次就放弃自动重投 |
| `AGENT_ANP_NEW_WHEN_NO_ROUTE` | 空 | 设 `1` 恢复"缺路由信息就开新对话" |
| `AGENT_ANP_HTTP_TIMEOUT` | `600` | MCP→hub 超时 |
| `AGENT_ANP_AGENTS` | 同目录 `mailbox_agents.json` | 接线表路径 |
| `AGENT_ANP_WEBUI_PORT` | `8898` | 只读看板端口 |
| `AGENT_ANP_CHEN_*` | 见 `mailbox_chen.py` 头部 | ChenCode 凭证链/模型/工具白名单/max-turns(30) |
| `AGENT_ANP_CODEX_*` | 见 `mailbox_codex.py` 头部 | Codex 二进制/额外参数/cwd/超时(1800) |
| `AGENT_ANP_MIMO_*` | 见 `mailbox_mimo.py` 头部 | MiMo CLI/serve 探测（决定 A/B/C 档） |

---

## 12. 接口清单（谁能调什么）

**hub HTTP**：

| 方法 | 路径 | 用途 | 要令牌 |
| --- | --- | --- | --- |
| GET | `/health` | 体检：信件计数 + **auth 统计** + pid | 否 |
| GET | `/agents` | 名册（唤醒端点、未读数） | 否 |
| GET | `/sessions` | 会话名册 + 对话线绑定 + 最近 10 条决策 | 否 |
| POST | `/send` | 发信（含会话决策） | 是 |
| POST | `/tool` | 9 个 MCP 工具的统一入口 | 是 |
| POST | `/heartbeat` | 登记/续约身份与唤醒端点 | 首次自动发令牌 |
| POST | `/session/roster` | 收信通道上报会话名册 | 是 |
| POST | `/session/bind` | 收信通道回报"对话线落在哪个会话" | 是 |

**MCP 工具（10 个）**：`mail_send` `mail_inbox` `mail_wait` `mail_query` `mail_ack` `mail_pending` `mail_redeliver` `mail_announce` `mail_board` `mail_agents`

**HTTP 路由（新增）**：`POST /messages/query` — 按关联线查消息记录（Plugin API 入口，只有创建者能读）

**收信端路由（DSH 桥接，本机 web 端口）**：`/mail/wake`（收信，要令牌）、`/mail/status`（自检）
**router 插件**：`/mail/resolve`（本地会话查询，兜底用）、`/mail/sessions`、`/mail/pin`
**看板**：`mailbox_webui.py` → `http://127.0.0.1:8898`（只读：信件 + 公告栏）

---

## 13. 已知缺口（★写清楚，别当成已完成★）

1. **exec 档没有"恢复同一会话"**：`codex exec` / ChenCode 每次唤醒都是新线程（`resume_session` 只是能力声明）。
   ★ChenCode 侧已有解★：`mailbox_hook.py`（UserPromptSubmit hook）在主人提交提示词时查一次未读并注入**当前会话**，
   来信就地处理、回信留在同一上下文里——见 §14。Codex 侧仍无对应机制。
2. **桌面端线程注入没有接口**（Codex）：即使 CLI 恢复了会话，也未必显示在主人的桌面窗口里。
3. ~~**hub 侧没有自动重投**~~ → **已做**（§4.6.1 + §15①）。
4. **没有"缓存信件正文"**（`mail_put`/`mail_get`）：公告栏做了，纯缓存仓库没做（主人说"再说"）。
5. **token 无签名/时间窗**：单因子令牌，能防误用、防不了冒充（§9）。
6. **`/health`、`/agents` 无需令牌**：本机可读（含路径与唤醒端点），只泄信息不能写。
   要上锁由主人定——注意 `ensure_hub` / `register_self` / 看板伴生探测都依赖它们免令牌。
   （`/sessions` 已于 2026-09-22 上管理员令牌：它含工作区绝对路径与 session id。）
7. ~~**loader 重复 id 风险**~~ → **已收敛**（§15②）。
8. ~~**旧唤醒器模板漂移**~~ → **已改成薄壳**（§15③），模板只有一处。
9. **唤醒配置的信任边界只是"降级"不是"解决"**（2026-09-22）：`core.check_wake_config()` 把 exec 唤醒限制在
   信箱安装目录内、http 唤醒限制在回环，于是"往账本追加一行假 wake"不再等于任意命令执行。
   但攻击者若能**写进安装目录**（或能读 `keys.json` 冒充已登记身份发心跳）就仍在信任圈内。
   真正的隔离 = 一身份一 OS 账户/沙箱（未做，方向由主人定）。
10. **hub 的 stderr 进无人读的管道时，约 100 个请求后整体阻塞**（2026-09-22 实测，新旧代码同样）：
    每个请求打一行访问日志，管道缓冲区（Windows 约几 KB）写满后 `write` 阻塞，`/health` 也不响应。
    规避见 README「已知限制」。修法（日志异步化 / 可选关访问日志）未做。

---

## 14. 收信的另一条路：交互式 agent 的 UserPromptSubmit hook

`mailbox_hook.py` 解决"交互式 agent 没有会话线程"的问题（2026-09-20）：

```text
主人在交互式 agent 里提交提示词
   └─ 平台触发 UserPromptSubmit hook（配置在该 agent 的用户级 settings.json）
        └─ hook 跑 mailbox_hook.py：peek 查未读 → 有新的就把信封概要追加进**本轮上下文**
             └─ 模型就地处理、就地回信，全部留在主人正在用的那条会话里
```

- 去重：已上报的 id 记在 `<mailbox-home>/hooks/<身份>-seen-ids.json`（同一封不重复刷屏）。
- ★两个注意★：① 它 **peek 不签收**（历史原因：怕吃掉正文状态）；现在签收归信箱，所以 hook 报过的信
  要到 `mail_inbox` 真读过才标已读。② 同一 agent 的**桌面端与 CLI 是两套配置**，hook 要各挂一份
  （CLI 挂在用户级 `settings.json`，桌面版挂在它自己的配置文件里），否则只有一边的会话会自动注入来信。
- ★`--bare` 会跳过 hooks★：headless 唤醒路径吃不到这条 hook，所以它只管交互会话；
  headless 仍走唤醒器（`mailbox_spawn.py`）。两条路分工，不冲突。

## 15. 2026-09-20 晚的三处收敛

**① 自动重投（信箱自己兑现退避时间）**：见 §4.6.1。
开关：`AGENT_ANP_AUTO_REDELIVER`（默认 `1`）、`_REDELIVER_SEC`（默认 120）、`_REDELIVER_LIMIT`（默认每轮 3）、
`_REDELIVER_MAX`（默认 5 次后放弃并把原因写进 `last_error`）。`/health` 的 `auto_redeliver` 段可看上一轮结果。

**② loader 重复入口（router 被两处加载）**：根因是 `dsh-mailbox-router` **既在 profile 的 `dsh.profile.bundles` 列表里、
又在 `cordis.patch.yml` 里被 insert**。修法：从 `bundles` 里移除它，只保留 patch insert（config 本来就在 insert 上），
与同一 profile 里 `AgentNetPort-bridge` / `uni-memory-bridge` 的加载方式对齐。
**代价**：profile 改动要重启 DSH 才生效；不重启**不影响当前行为**（当前进程已按旧配置加载完毕）。
体检结果：`dsh-message-edit` / `dsh-delete-history` / `dsh-custom-commands` / `dsh-loop-guard` 仍是"仅 bundles"（未重复）。

**③ 旧唤醒器改薄壳**：`mailbox_codex.py` / `mailbox_chen.py` 的 `main()` 现在只转发
`mailbox_spawn.py --agent <自己>`，模板与逻辑只有一处（`--prompt` 干跑的输出与通用器**逐字节一致**，已实测）。
旧实现保留为 `_legacy_main` / 原有函数，仅供备查，不再走入口。


---

## 16. 术语对照（别被旧词带偏）

| 说法 | 含义 |
| --- | --- |
| **档位 A/B/C** | A=http 常驻端点；B=exec 拉起进程；C=queue 只落盘等轮询 |
| **收信通道** | 收信端（DSH 桥接、各唤醒器） |
| **盖章 / 信封坐标** | hub 写进 `meta.session` 的决策 / 信里带的 `context_id` 等地址 |
| **签收** | 标 `read` + 记 `read_by`（现在的"已读"语义） |
| **消费（旧词）** | 早期 `claim()` 的语义，字段 `claimed_at/claimed_by`，已被签收取代 |
| **对话线** | `context_id`：一件事的往来共享它，回信靠它落回同一会话 |
| **名册** | ①身份名册（`ledger.jsonl`）②会话名册（`sessions.json` 的 rosters）——上下文能区分 |
| **懒拉起** | hub 不在时由客户端/桥接心跳自动把它起来（`core.ensure_hub`） |

## 17. 自检与复现命令（每条都跑过）

> 路径按 2026-09-21 的目录重构写（运行时 `src/`、脚本 `tools/`、自检 `tests/`、文档 `docs/`）。
> 根目录同名的 `.py` 是兼容壳（转发到真文件），老命令照旧能跑，但新写的东西请用真路径。

```bash
cd <repo>

# 主自检（26 项：存储、唤醒、MCP、往返、编码清洗）
python tests/selftest.py

# 会话决策矩阵（20 项，含"缺路由信息不再一律开新对话"与逃生阀）
python tests/selftest_sessions.py

# 接线表体检（bin/cwd/引用文件/密钥是否就位）
python src/mailbox_spawn.py --check
python src/mailbox_spawn.py --list

# 收信链路（在装好的桥接插件运行时副本目录下跑）：合并、分组、入站校验、公告信封
cd <harness-home>/profiles/web/node_modules/AgentNetPort-bridge
node recv-test.mjs .          # 26 项
node recv-group-test.mjs .    # 分组不串批

# 现场体检
curl http://127.0.0.1:8900/health      # 信件计数 + auth 统计
curl -H 'x-mailbox-key: <admin.token>' http://127.0.0.1:8900/sessions  # ★要管理员令牌★ 会话名册 + 对话线绑定 + 最近决策
curl http://127.0.0.1:8900/agents      # 名册与唤醒端点
```

---

## 18. 2026-09-22 安全收紧（六项）

| # | 洞 | 修法 | 位置 |
| --- | --- | --- | --- |
| ① | 任意 agent 能用 `by="wake:*"` 抹掉别人的未读、用别人的消息 id 反复重投、读别人的待办元数据 | 归属校验只认"箱主本人"或 hub 内部（`sender == "hub"`），删掉 `by` 前缀白名单；`mail_pending` 只能查自己 | `hub.do_ack` / `do_redeliver` / `do_pending`，`mcp.DirectBackend` 同规则 |
| ② | 请求体指定 `home` 可让"鉴权用一个 home、数据用另一个 home"，并能读写任意目录 | 数据面一律用服务端 HOME（`_auth` 早就这么做了，请求体参数是同一漏洞的另一个入口） | 所有 `do_*` |
| ③ | 往 `ledger.jsonl` 追加一行假 `wake.exec` = 以本用户身份执行任意命令；http 唤醒 url 指向外网 = 来信正文外泄 | `core.check_wake_config()`：exec 脚本必须落在受信任根内——
   信箱安装目录 **∪ `<home>/plugins/`（#4.1a）** ∪ `AGENT_ANP_TRUST_WAKE_PATHS`；
   注意受信任的**只有 plugins/ 这一棵，不是整个 `<home>/`**；http 主机必须回环 | `core.wake()` 唯一执行点 |
| ④ | 看板 `/api/state` 零鉴权可读全量正文；`GET /sessions` 泄露工作区路径与 session id | 管理员令牌（`AGENT_ANP_ADMIN_TOKEN` / `~/.AgentNetPort/admin.token`），看板首次打开贴一次并存 localStorage | `webui.do_GET`、`hub.do_GET` |
| ⑤ | 两个 agent 可无限互相唤醒（无任何 hop/TTL/循环判断），每条都烧 token | 按"收件箱 × 时间窗"计数：同一对话线 30 封 / 全箱 120 封 / 600 秒，超了拒收并 loud failure；逃生阀 `meta.rate_bypass=true` | `rate_guard()` + `core.context_rate_exceeded()` |
| ⑥ | 请求体无体积上限 | `AGENT_ANP_MAX_BODY_BYTES`，默认 4 MiB | `Handler._body()` |

**顺带修掉的报告不实**：`mail_redeliver` 以前恒返回 `ok:true`，"全被归属校验挡下"或"唤醒全失败"也报成功，
连 hub 自己的自动重投统计都把失败记成成功。现在 `ok` = "这一轮真的投出去至少一封"。

---

## 19. 统一权限策略（2026-09-22）

**为什么不是"一身份一密钥文件"**（主人定：「统一 + 权限就行」）：本机所有 agent 跑在**同一个
OS 账户**下，拆成一身份一文件只是把同一把钥匙换了个抽屉，挡不住读取。真正该管的是**行为权限**。

**做法**：密钥表仍是一个 `keys.json`，在这一层之上放一份**统一的人可编辑策略**
`<home>/permissions.json`（`src/mailbox_policy.py`，`AGENT_ANP_POLICY` 可改位置）。
**文件不存在 = 三项全 `open` = 与没有这一层时行为完全一致**——接入层不能默默改变行为。

| 项 | 管什么 | 决策点 | 收紧时的拒绝 |
| --- | --- | --- | --- |
| `register` | 谁能**铸造新身份**。首次 `POST /heartbeat` 会自动发密钥，否则本机任何进程都能凭空造一个身份、拿一个收件箱、开始发信（真实账本里的 `fixprobe` / `regressprobe` / `root` / `freshprobe` 就是这么来的） | `hub.do_POST:/heartbeat` 的 bootstrap 分支 | 403 |
| `announce` | 谁能**广播全员**（推送全员、可能拉起不在线的 agent，是花钱的动作） | `hub.do_announce` | `ok:false` |
| `send_to` | 谁能**发给谁**（拓扑控制），`allow` 是对象：`{"dsh": ["codex", "*"]}` | `hub.do_send` / `send_group`（多人信逐个收件人判） | `ok:false` |

规则：

- 取值 `open`（默认）/ `allowlist`（`allow` 名单内放行，`"*"` 通配）/ `closed`。
- **改完即生效**：每次决策都重读，不用重启 hub，也不用重启任何 agent。
- **持管理员令牌永远放行**（`hub._admin_ok`）——否则上锁会把 `tools/install.py` /
  `mailbox_spawn.py register` 自己关在门外。
- **策略读不动/解析失败 → 收紧项一律拒绝**（`PolicyUnreadable` → 三项置 closed）并 loud log。
  只影响**显式配置过收紧**的人，默认 open 的人无感，不会静默放开。
- **同一语义只准有一份实现**：hub 与 MCP 的 direct 降级路径都调 `mailbox_policy` 的三个
  `may_*`，否则"降级"就是绕权后门。
- 现状可读：`GET /health` 的 `policy` 段、看板侧栏「可铸身份 / 可广播 / 可发给」。

**仍然没解决**：`keys.json` 单文件 + 同 OS 账户 ⇒ 一个进程仍可**顶替已登记身份**发信
（它读得到别人的令牌）。权限层管"能做什么"，不管"你是不是你"；后者要 OS 账户/沙箱。

---

## 20. 第 1、2 步重建（2026-09-22）：可靠 + 可关联

### 20.1 日志不再能卡死服务（修一个真实故障）
`sys.stderr` 全改为 `core.AsyncLog`（队列 + 后台线程，满了丢弃并计数）。
背景：hub 的 stderr 常被调用方重定向进**无人读的管道**，Windows 匿名管道缓冲区只有几 KB，
写满后 `write` 阻塞 → 整个 hub 停止响应（连 `/health` 都不通）。A/B 实测旧代码与新版在
**约 103 个请求**时同样出现；改后连打 600 个请求全通。丢弃量在 `/health` 的 `log` 段可见。

### 20.2 数据生命周期接线（`prune()` 终于有人调了）
新增 `core.compact_ledger()` / `core.maintenance()`，由 hub 的后台线程定期跑
（`AGENT_ANP_MAINTENANCE`，默认每小时）。
- 收件箱：保留最近 `AGENT_ANP_PRUNE_KEEP`（500）封**已读** + **全部未读**；
- 账本：每身份留最后一条 + 最近 `AGENT_ANP_LEDGER_KEEP`（500）行心跳，压缩前留 `.compact-bak`；
- 修掉 `prune()` 去重会抹掉未读信的旧问题（同 id 保留未读那一份）。
实测：账本 2000 → 500 行，`agent_table()`/`identity_known()` 全部不受影响；密钥不在账本里，压缩不碰鉴权。

### 20.3 `correlation_id`：与 `context_id` 彻底分开
| 字段 | 归谁 | 谁消费 |
| --- | --- | --- |
| `meta.context_id` | **平台会话线** | 会话决策（Adapter 知识，将来搬走） |
| `correlation_id`（信封顶层） | **应用关联线** | Core 只携带 + 索引 + **绝不解释** |

带 `correlation_id` 的信额外落一份到 `journals/<id>.jsonl`，头记录记 `owner`（第一个发信的人）。
`mail_query`（工具）/ `POST /messages/query`（HTTP）读它，**授权规则只有一条：只有 owner 能读**
（hub 自己与管理员令牌除外）。不建任务注册表、不做 KV——owner 就写在文件头，没有需要同步的第二份状态。

### 20.4 receive 过滤
`mail_inbox` 新增 `from` / `kind` / `correlation_id` 三个过滤参数（只让结果更窄，签收口径不变）。
`direct` 降级路径同一套参数与同一份授权规则。

---

## 21. 信封完整性与能力发现（2026-09-22 第 3 步）

### 21.1 Envelope HMAC
`core.sign_envelope()` 在 `send()` / `post_bulletin()` 落盘前给信封**内容**算 HMAC-SHA256
（密钥 = 信箱自己的 `hub.secret`，首次生成时 ACL 收紧，`AGENT_ANP_HUB_SECRET` 可覆盖）。

| 决策 | 内容 |
| --- | --- |
| 签哪些字段 | `SIGNED_FIELDS` = id / ts / from / to / subject / body / kind / reply_to / correlation_id / meta |
| 不签哪些 | 全部状态机字段（read / delivered_at / attempts / next_retry_at / last_error / claimed_* / replied_* / wake_skipped）+ sig 自己 |
| 为什么 | 信箱每次签收/投递/重投都改状态字段，签进去第一次 `mark_read` 就失效。**信封签名，状态不签。** |
| 密钥用谁的 | **信箱自己的**，不是发件人令牌。所有令牌同在 keys.json 里，谁都能读，用它签等于没签 |
| 篡改怎么办 | `core.verify_envelope()` 返回 `(False, "tampered")`；读路径（mail_inbox / mail_query / 看板）逐封标注 `sig_ok`/`sig_why`，`/health` 有 `integrity` 段（checked/signed/tampered + 清单） |
| 为什么标注而不拦截 | 签名是**可观测性**不是访问控制。拦截等于给"改坏一封信就让它消失"的 DoS 手段。所以照常返回、照常签收，结论摆在明面上 |
| 一份信两份副本 | 带关联线的信在收件箱与 `journals/` 各有一份、各自签名。`mail_query(state=true)` 会把**收件箱那份**的验签结果一并返回，两份对不上当场暴露 |
| 老信 | 无 `sig` → `unsigned`，不算坏 |

**威胁模型（写明，别当过安全边界）**：同 OS 账户下读得到 `hub.secret` 的进程照样能签。
这一步买的是**可审计性与防意外改写/磁盘损坏**；防冒充仍靠 OS 账户/沙箱（未做）。

### 21.2 caps / skills 暴露
- `caps`（平台能力：inject_session / spawn_cwd / resume_session / queue）一直都在账本里，
  但 `agent_view()` 原来只挑固定字段，**`caps` 被丢在读取侧**。现在 `mail_agents` 与看板都返回它。
- `skills` 是身份自报的**任务能力**（自由词表：coding / analysis / vision…），由心跳带来，
  已加入 `STICKY_FIELDS`（否则一次不带 skills 的心跳就把它抹掉——`process_api` 2026-09-21 踩过）。
  显式传 `None` 可清掉。
- **Core 只存、只返回**：不解释词表、不匹配能力、不自动调度。要"找最合适的 agent"是上层能力。

---

## 22. 插件系统第 1 片（2026-09-22）：manifest + 注册表 + 双向握手

### 22.1 新文件 `src/mailbox_plugin.py`
manifest 读/校验、`plugins.json` 注册表、端口分配、进程拉起/终止、双向握手。
**它不认识任何具体插件**——公告栏、狼人杀都还没有进来。

### 22.2 manifest 校验的确定性（定稿规则）
| 规则 | 结果 |
| --- | --- |
| `command` / `url` **严格二选一**（先数个数再谈内容） | 两个都有 / 两个都没有 → `manifest.invalid` |
| `id` 字符集 `^[a-z0-9][a-z0-9._-]*$`、必须小写、不得是路径片段 | 否则 `manifest.invalid` |
| `api_level` 正整数（bool 也不算 int） | 否则 `manifest.invalid` |
| `permissions` ⊆ 5 个闭集，**输出按字母序**（与声明顺序无关） | 否则 `manifest.invalid` |
| 未知字段 | **一律忽略**：不报错（保前后向兼容），也不生效（明确降级） |
| `tools` / `events` / `panel` | 写进 manifest 同样被忽略——贡献点在 register 阶段声明 |

### 22.3 双向握手：两个 register 方向相反（spec §4.0）
| 端点 | 方向 | 发起方 | 交什么 | 换什么 |
| --- | --- | --- | --- | --- |
| `POST /plugin/register` | Plugin → Core | 插件 | 贡献（tools/events/panel） | 身份 / **自己的 token** / config / 目录 |
| `POST /anp/register` | Core → Plugin | Core | 身份 / token / config / 目录 / api_level | 贡献 |

Payload 分工只有一条规则：**凭证一律由 Core 给，贡献一律由插件给**。
两边都是"发起方交自己那一半，应答方交自己那一半"——不会出现两套真相。
两个端点的请求头都带 `x-mailbox-key: <插件的 agent token>`（不是 hub.secret）。
记法：**`/plugin/...` 是插件打给信箱的，`/anp/...` 是信箱打给插件的。**

### 22.4 生命周期四动词 + 进程事实三分离
`load` / `enable` / `disable` / `unload`（`reload` = unload + load）。
★核心不变量★：**Core 拉起 ≠ Core 保活**。插件自己退出 = "端点不可达" = 走既有重投，
**不是状态变化**；Core 只终止**自己拉起的**那个进程，绝不碰外部进程。

### 22.5 实现期踩到并修掉的三件事
1. **`read_text_shared()` 对不存在的文件返回空串，不是抛 `FileNotFoundError`**——
   我按后者接，第一次读注册表就炸。已在三处（注册表 / config / manifest）改正。
2. **URL 动词被当成状态值**：`/plugins/<id>/disable` 的 `action` 直接传进
   `plugin_set_state`，于是 `state == "disabled"` 永不成立 → disable 不终止进程，
   且 state 被写成 `"disable"`（一个不存在的状态）。已改成显式映射 + 入参白名单校验。
3. **重复 load 泄漏进程**：startup 预加载 + 手动 load 会拉起两个进程。已改成
   load 前先终止 Core 自己拉起的上一个。

### 22.6 管理面与可观测
| 端点 | 方法 | 谁能调 |
| --- | --- | --- |
| `GET  /plugins` | 列出（脱敏：token 只给前 8 位） | 任何人 |
| `POST /plugins/<id>/<load\|enable\|disable\|unload\|reload>` | 生命周期 | 管理员令牌 |
| `POST /plugin/register` | 插件主动报到 | 管理员令牌 |
| `GET  /plugin/config` | 热读配置 | 管理员令牌 |

`/health.plugins` 给出 total / enabled / registered / **secret_isolation**
（hub.secret 与任何 agent token 是否顶替的自检，永久架构约束）。

---

## 23. 第 4 步第 2 片（2026-09-22）：三个代理 + 公告栏迁进插件

### 23.1 Tool Proxy
- `plugin.tool_registry()`：`{MCP工具名: {plugin_id, canonical, tool, base_url}}`，只收 **enabled 且已握手**的插件。
- 名字映射：规范名 `alice.werewolf.create_game` → MCP 名 `alice_werewolf_create_game`
  （MCP 工具名只允许 `^[a-zA-Z0-9_-]{1,64}$`，**不含点号**）。撞名时**后一个不上**，由 `tool_clashes()` 报出来。
- 调用：`POST /tool` 里 Core 自己不认识的名字 → `plugin.call_plugin_tool()` → 插件的 `/anp/tool`，
  带 `{name: 规范名, arguments, caller_identity}` + `x-mailbox-key: <插件自己的 token>`。
- MCP 侧 `tools/list` = 自己的 `TOOLS` + 插件的（hub 的 `GET /tools` 或直读 `plugins.json`，同一份真相）。
- **插件自己失败 → `plugin.error`**，不冒充成功；端点不通 → `endpoint.unreachable`；超时 → `timeout`。
- ★render 分支必须放在最前面★：曾经把 `mode == "plugin"` 的判断漏在一次区间删除里，于是一次插件调用
  被按 `mail_send` 的格式渲染成"已投递 None → None"——看着像成功，其实什么都没说。

### 23.2 Event Proxy（spec §6：Core 只出 4 个事件）
`plugin.EventBus`：有界队列（默认 2000，满了丢最旧并计数）+ 后台线程逐个 POST 到 `events_url`。
- **旁路，绝不阻塞**：入队即返回；单个订阅者失败只记数；线程异常不外溢。
- 四个源：`message.delivered`（`deliver_one` 里唤醒成功时）、`message.undelivered`
  （自动重投**第一次**标记"已放弃"时，别每轮扫描重播）、`agent.online` / `agent.offline`
  （后台扫 TTL 跃迁——过期不需要任何心跳也会发生，所以必须有个线程）。
- 事件与消息投递状态**完全无关**：插件端点不能替 hub 签收。

### 23.3 Panel Proxy
`GET /admin/panel`（列表）与 `GET /admin/panel/<id>`（内容）→ 代理插件的 `GET /anp/panel`。
- Core **不清洗、不解析、不缓存**（新增 `_raw()` 直接透传，包括 content-type）。
- 要管理员令牌 + 插件得有 `webui.panel` 权限（没有 → 403 `permission.denied`）。
- WebUI 的公告栏面板改成通用的"插件面板"一档：看板只经 `/admin/panel/<id>` 拿内容。

### 23.4 公告栏搬成插件（`plugins/anp.bulletin/`）
Core 里**一行公告代码都不剩**：`core.post_bulletin/read_bulletins/mark_seen/bulletin_path` 全删，
hub 的 `do_announce/do_board` 全删，MCP 的 `mail_announce/mail_board` 全删（Core 工具 10 → 8）。
插件自己存 `plugins/anp.bulletin/data/bulletin.jsonl`，提供 `anp_bulletin_post` / `anp_bulletin_list`
两个工具与一个面板；首次启动把旧的 `~/.AgentNetPort/bulletin.jsonl` 迁进来，
旧文件改名 `.migrated` 留档（可回滚）。

**一处行为变化（要明说）**：推送从"唤醒但不落收件箱"变成"一封多人信（N 份副本 + 唤醒）"。
因为插件只能调用 `/messages/send`，而 Core 没有"只唤醒不落盘"这个原语。
公告栏**记录**本身仍是一份、仍无已读未读（在插件自己的存储里）。

### 23.5 实现期修掉的真 bug
1. **握手有竞态**：`subprocess.Popen` 立刻返回，插件进程还没 bind 就 POST `/anp/register`
   → 连接被拒。改成带退避的重试（只重试传输层失败，应用层拒绝不重试）。
2. **`enable` 在 `disable` 之后不重新拉起**：URL 动词 `disable` 曾被当成状态值传进去，
   修好映射后又发现"只有 disable 分支会置 `killed`，enable 分支永远看不到"。
   现在 `disable` 记 `core_terminated=True`（Core 对自己动作的事实记录，**不是进程状态推断**），
   `enable` 照这个决定是否重新拉起。
3. **重复 load 泄漏进程**：startup 预加载 + 手动 load 拉起两个。改成 load 前先终止上一个。
4. **`EventBus` 在 import 时绑定默认 home**：hub 的 `HOME` 是 `main()` 解析 `--home` 后才定的，
   于是事件总线去读**真实信箱**的注册表。加了 `bind_home()`，startup 时重绑。
5. **插件根本没被登记成身份**：`plugin_load` 只发 key 没走 `heartbeat()`，
   `agent_table()` 查不到它 → `deliver_one` 走"还没登记过唤醒端点"分支，
   **发给插件的信永远投不出去**（工具能通、消息不通，最难查的半身不遂）。现在 load 时补一次心跳。

---

## 24. P0-0 与 P0-9（2026-09-22）：两条写进协议的语义边界

### 24.1 P0-0：queue 档不再被当成投递失败

**修前的错误行为**（实测）：queue 档身份收到信 →

```
attempts = 1
next_retry_at = <120 秒后>
last_error = "收件人未配置唤醒端点（仅落盘，等待对方主动收信）"
```

自动重投再白试 4 次，最后标「已放弃自动重投」并**误发一个 `message.undelivered`**。
而错误信息本身已经写着「仅落盘，等待对方主动收信」——代码知道这是 queue，却没有按 queue 处理。

**改动（三处，只此三处）**：

1. `anp_core.delivery_mode(entry)`：新增。把「这个身份有没有 push 目标」变成显式判断
   （`http` / `exec` / `queue`），与 `wake()` 自己的分派保持一致。
2. `anp_core.record_delivery()` 新增 `no_push` 参数，与既有 `skipped` **分开**：
   ```
   skipped     发送方主动选择不唤醒（wake=false） → wake_skipped = True
   no_push     收件人没有 push 目标（queue 档）   → wake_skipped = "no_push_target"
   ```
   两者都不递增 attempts、不排重投、不记 last_error，但**标记不同**。
3. `anp_hub.deliver_one()` / `anp_hub.do_redeliver()` 两处识别 queue 档。

**第二层保护**：`anp_core.pending_messages()` 里本来就有
`if due_only and rec.get("wake_skipped"): continue`，所以 queue 档被「前置判断 + 扫描过滤」
双保险挡住。**但不能让这层历史兼容逻辑成为 queue 正确性的唯一依赖**——显式的
`delivery_mode` 判断才是第一道。

### 24.2 P0-9：canonical `/messages/receive` + `/send` 别名

- `POST /messages/receive`：**直接调 `do_inbox`**——`/tool` 的 `mail_inbox` 也是调它，
  两个入口共享同一份实现，不可能出现语义分叉（测试 3.1 逐字段断言一致）。
- `POST /messages/send`：正名。`/send` 变成它的别名，**同一段代码**，
  响应体一字不改，只多一个 `deprecation` 响应头。

**踩的坑**：响应头里放了中文箭头 `→`，`http.server` 的 `send_header` 用 latin-1 编码，
直接 `UnicodeEncodeError` → handler 半路死掉 → 客户端读到截断的 body。
**响应头必须纯 ASCII。**

### 24.3 顺带确认（未改代码）

- `do_redeliver` 故意忽略 `args["home"]`（防 `x-mailbox-home` 漏洞，一律取服务端 home）。
  于是 `auto_redeliver(home)` 扫的是传入 home、重投用 `hub.HOME`；生产里恒等，
  只有测试进程跨 home 调用会报「找不到消息」。
- `do_send` 的 `ok` 语义是「信已落盘」而非「投递成功」，投递结果在 `wake` 字段。已写进协议 §10。
