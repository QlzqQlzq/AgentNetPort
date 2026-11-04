# ANP Adapter Spec v0.1 → v0.1.1 Errata

> 状态：**B1 契约已定稿**（待合入 `ANP-ADAPTER-SPEC.md` 与实现/测试对齐）  
> 性质：**契约补全**，不重开 v0.1，不修改 wake 状态机，不增加 delivery mode。  
> 范围：本文件当前只含 **B1**。C1+B2 / D1 / G1 在 B1 完成后再写，**不预设答案**。  
> 执行顺序见 `V0.1.1-FIX-LIST.md`：**B1 完成后再进入 C1+B2**。

---

## B1 — `/anp/wake` HTTP 响应契约

### B1.1 适用范围

仅适用于 **HTTP delivery mode**（`wake.kind = "http"`）下，**Adapter 对 Core 的 wake 请求**所返回的 HTTP 响应体。

不适用于：

- exec 档（无 HTTP 响应体；见 Spec §5.2 / §5.4）
- queue 档（Core 不调用 Adapter；见 Spec §5.3）
- Adapter → Core 的 `/messages/*` 等其它接口
- 本补丁不改变 HTTP wake 的**请求**载荷、请求头、URL

### B1.2 响应契约（必须满足）

Adapter 对 Core 的 wake 请求，响应必须满足：

```json
{
  "ok": true,
  "mode": "<响应模式>"
}
```

其中：

| 字段 | 要求 |
|---|---|
| `ok` | 必须为 `true`，表示 Adapter 已正常接受本次 wake 请求。 |
| `mode` | **必须存在**，且必须为本规范定义的**合法响应模式值**（§B1.3）。 |

其它字段（如 `envelope_id`、`note`、`echo` 等）可选；Core 可忽略未知字段。

### B1.3 响应模式 `mode`（合法值闭集）

`mode` 表示 **Adapter 对本次唤醒的处理方式确认**，不是 ANP delivery mode。

合法值闭集（v0.1.1）：

| 值 | 含义（Adapter 确认「我怎样接了这一下」） |
|---|---|
| `immediate` | 已立刻按单封处理（或已进入立即处理路径） |
| `batched` | 已入本端批处理队列，稍后成批处理 |
| `session` | 已注入/绑定到某个平台会话（含会话队列） |
| `inject` | 已注入到已存在的活跃上下文/会话（与 `session` 可并存语义时优先用更具体者） |
| `followup` | 已挂到后续回合/待办，在下一轮处理 |

规则：

1. `mode` 必须是上述之一的**精确小写字符串**。
2. 空串、缺失、`null`、未知值、大小写变体 → **均不合法**。
3. Adapter **不得**用 `mode` 表示 ANP delivery mode（`http` / `exec` / `queue`）。

### B1.4 两个 “mode” 不是同一概念（强制区分）

```text
wake.kind
    → Core 使用的 delivery mode（http / exec / queue）
    → 写在心跳登记的 wake 配置里
    → 决定 Core 如何投递

wake response.mode
    → Adapter 对本次唤醒处理方式的确认（immediate / batched / session / inject / followup）
    → 写在 HTTP 响应体里
    → 决定 Core 是否将本次 wake 视为可签收完成
```

禁止混用。响应体里的 `mode` **不得**出现 `http` / `exec` / `queue`。

### B1.5 Core 判定（签收门）

对 HTTP 档一次 wake，**仅当**下列条件**全部**成立时，Core 才将本次 wake 视为完成，并执行相应的 delivery 记账与签收：

1. HTTP 传输成功（得到 2xx 响应）；
2. 响应体可解析为 JSON 对象；
3. `ok == true`；
4. `mode` 存在且为 §B1.3 合法值。

下列情形**不得**视为成功签收：

| 情形 | 结果 |
|---|---|
| 仅返回 `{"ok": true}`（缺 `mode`） | **不签收** |
| HTTP `200` 但 `ok` 不为 `true` | **不签收** |
| `ok: true` 但 `mode` 缺失 / `null` / 空串 | **不签收** |
| `ok: true` 但 `mode` 非法（未知值、`http`、大小写变体等） | **不签收** |
| 响应不是 JSON / 不是对象 / 无法解析 | **不签收** |
| HTTP 非 2xx | **不签收** |

> **HTTP `2xx` 状态码本身不等于消息已被签收。**  
> **仅返回 `{"ok": true}` 不构成成功签收。**

### B1.6 参考实现与 SDK 义务

以下实现必须遵守本响应契约，且默认返回值必须带合法 `mode`：

- Reference Adapter（`examples/adapter_demo/`）
- Adapter Plugin 原型（`examples/adapter_plugin/`）
- Adapter SDK 的 HTTP wake 接收路径（`sdk/anp_adapter/transport.py` / `plugin.py`）
- 一切声称符合 ANP Adapter Spec 的第三方 Adapter

### B1.7 兼容性说明

本补丁：

- **不**改变 HTTP wake 的请求载荷、请求头、URL；
- **不**改变 retry / attempts / `next_retry_at` 状态机；
- **不**增加新的 delivery mode；
- **不**改变 queue 档终态（P0-0）与 `wake_skipped` 三态标记；
- **不**改变 exec 档进程契约（P0-5）；
- **不**改变 `x-mailbox-key` = 目标 Agent token（P0-4）。

仅将现有实现中已经存在的**响应确认语义**（`wake_sign_off_ids` 要求 `mode`）正式写入规范，使「第三方只按 Spec 实现」与「Core 签收判据」一致。

### B1.8 合入锚点（给 `ANP-ADAPTER-SPEC.md`）

| 锚点 | 动作 |
|---|---|
| §5.1 http 档 | 在请求契约之后增加 **§5.1.1 响应契约**（正文即 §B1.2–B1.5） |
| §5.1 | 在 P0-4 段落后增加 §B1.4「两个 mode」对照表 |
| §10 最小骨架 | `on_wake` 返回值改为 `{"ok": True, "mode": "session"}`（或其它合法值），禁止只回 `{"ok": True}` |
| §11 自检清单 | 「唤醒」节增加：`□ http：响应含合法 mode；仅 {"ok":true} 不算签收` |
| §12 已知缺口 | 「HTTP wake 响应契约漏写」标为已由本 errata 关闭 |
| 附：12 条 P0 速查 | P0-12 一句话可补：「三种 wake 的载荷契约逐字段写死（含 http 响应 mode）」 |

### B1.9 测试钉死（至少三条）

B1 完成必须有可失败的测试，至少覆盖：

```text
① ok=true + 合法 mode   → 签收
② ok=true + 缺 mode     → 不签收
③ HTTP 200 + ok=false   → 不签收
```

建议补强（同一组内）：

```text
④ ok=true + mode 非法（如 "http" / "unknown" / ""）→ 不签收
⑤ 响应体非 JSON → 不签收
```

断言口径（与 Core 对齐）：

- **签收** = 该封 `read=true`（或 `read_by` 已记），且不再被自动重投打扰；
- **不签收** = 保持未读/未签收，且仍按既有 retry 规则可重投（本补丁不改 retry 公式）。

负例必须**可红**：人为改成只回 `{"ok": true}` 时，测试必须失败。

### B1.10 B1 完成定义（DoD）

- [x] §B1.2–B1.5 已合入 `ANP-ADAPTER-SPEC.md`（§5.1.1 / §5.1.2 / §5.1.3，读者可对照本 errata）
- [x] §10 骨架 / §11 清单已同步
- [x] Reference + SDK 默认响应含合法 `mode`（SDK + adapter_demo + adapter_plugin + Mimo 四处）
- [x] §B1.9 ①②③④⑤ 测试存在且全绿（`tests/selftest_b1_wake_mode.py` 24 项）；负例可红
- [x] 未改动 retry 状态机 / 请求载荷 / delivery mode 集合（B1-7 四条护栏）

**B1 DoD 满足后，才进入 C1+B2（先定能力，再选 credential 或 nonce，不预设答案）。**

---

## B4 — exec 档的回信只走 `/messages/send`（v0.1.1 写死）

### B4.1 问题

Spec §5.4 只写了「Core 怎么拉起 exec 进程、怎么交信封」，**没写回信怎么回来**。
第三方可以合理推出一个 Core 从不解析的通道（stdout），于是回信静默丢失。

### B4.2 契约（必须满足）

exec 子进程的回信，**唯一合法出口**是 `POST /messages/send`（§6.1 接口 3）。
**禁止**把 stdout 当作回信通道。

| 不允许 | 为什么 |
|---|---|
| stdout 当回信 | Core 的 exec 契约是「拉起、投环境变量、不再管它」，没有任何人解析 stdout |
| 靠文件 / 本地 socket 代传 | 那不是 ANP 接口，第三方的第二种实现猜不到 |
| 不带 `reply_to` / `correlation_id` | 对话线会断，回信落不回原对话 |

### B4.3 责任边界

**回信失败的责任在 adapter 侧。** Core 不会因为 exec 子进程没回信而重投——
重投只看 ack（见 D1 的三事实）。想让回信可靠，adapter 自己得察觉
「进了平台」与「发回 ANP」两步的失败。

### B4.4 合入锚点

| 锚点 | 动作 |
|---|---|
| §5.6（新增） | 契约正文 + 正确形状代码示例 |
| §5.4 | 补一句「本节的进程契约不含回信路径，回信见 §5.6」 |

### B4.5 现状核对

参考实现（`examples/adapter_demo/adapter.py` 的 `--wake-exec`）**已经合规**：
它往 stdout 打的只是 wake 响应的回显，真正的回信走 `client.send()`。
本条是**把既成事实写成契约**，不是新增限制。

---

## C1 — `/plugin/register` 鉴权最小化（v0.1.1，用户 2026-09-23 拍板）

### C1.1 修的是什么

Spec §2b 写「Adapter Plugin token 只用于 `POST /plugin/register`」，而 Hub 实现
直接调 `_admin_ok()`。**一边说用插件自己的令牌，一边只收 admin**——身份分层被击穿：
插件要么拿不到报到权，要么被迫持有全局 admin。

### C1.2 拍板结果（四条）

| # | 决策 | 落地 |
|---|---|---|
| ① | **方案 P**：鉴插件自己的 credential，**不引入 nonce** | `core.verify_key(pid, given, HOME)` |
| ② | **B2 改文档**：admin 不能续约已有身份 | Spec §3.2.1（新增，删掉错误那行） |
| ③ | **admin 明确拒绝**，不留运维旁路 | 401 + `code=auth.plugin-credential` |
| ④ | **401 = 凭证缺失/不对；403 = 已认证但无权限** | 未加载插件 → 401（不是 404） |

### C1.3 能力定义（先定能力，再定令牌）

`/plugin/register` 只需要三件事：**证明"我是某个已加载的插件"**、
**更新我自己的注册声明**（tools/events/panel）、**取回我自己的凭证**。
不需要读他人消息、不需要管其他插件、不需要铸造身份、不需要 `hub.secret`。

该端点问的是「**你是不是 pid 本人**」，不是「**你是不是管理员**」。
插件自己的 token 就够，且**比 admin 更精确**——admin 令牌只证明"你是管理员"，
答不出"我是哪个插件"。

### C1.4 为什么否决 nonce 方案

插件**在 `/anp/register` 时已经拿到过自己的 agent token**（`entry["token"]`，
响应体与请求头各一份），重启后原样持着。再发一枚 register nonce 是**并存两枚
同样能力的东西**——多一个要轮换、要过期、要防重放的秘密，却没多买到任何能力。

### C1.5 修完后的身份图

```text
hub.secret        → 仅 Core 内部签名。任何响应体都不出现，admin 也拿不到
plugin credential → 证明"我是这个插件"：报到 / 更新自己贡献 / 收 /anp/register
agent token       → 代表某个 Agent 收发（含插件自己那一枚）
admin token       → 面板 / /sessions / 运维脚本。不进 Adapter
```

### C1.6 401 / 403 分层（④的落地）

| 状态码 | 含义 | 例子 |
|---|---|---|
| 401 | 凭证缺失 / 不对（"你是谁都没证明"） | 没带头、头不对、拿 A 插件令牌报到 B、admin 令牌、插件从未发证 |
| 403 | 身份已认证但没有权限 | `/tool` 的 `permission.denied` |
| 404 | 令牌对、插件也确实没加载 | 纵深防御；正常情况下鉴权先挡，到不了 |

★未加载插件回 401 不是 404★：它从没发过 key，`verify_key` 在鉴权步就失败。
这样**不对外泄露"这个插件有没有被加载"**。若把鉴权挪到存在性之后，未加载插件
会从 401 变 404——那是信息泄露，别那么做。

### C1.7 兼容性

- **不**改变 `/plugin/register` 的请求/响应业务字段；
- **不**新增 permission 枚举，不做完整 RBAC；
- **不**动 `hub.secret` 隔离（永久架构约束原样保持）；
- `/plugins/<id>/reload` 等**管理员端点不随本次改动**——只动 `/plugin/register` 一个路由。

### C1.8 验收（逐条对到测试）

| # | 要求 | 测试 |
|---|---|---|
| ① | 插件仅持自己的 token 能报到 | `experiment.py` Q1.1；`selftest_plugins.py` E1 |
| ② | admin 调该路径有明确行为 | E5b / Q1.1b（401 + `auth.plugin-credential`） |
| ③ | 响应体仍不含 `hub.secret` | E3b（原本就有，仍绿） |
| ④ | 四处文档一致 | `selftest_spec_conformance.py` C1-1/2/3（+59→65） |
| ⑤ | A 插件令牌报到 B 插件 → 拒绝 | E5c / Q1.1c |
| ⑥ | experiment 不再依赖 admin 报到 | Q2.6（`admin` 只出现在负面用例，≤1 处） |
| — | 未加载插件 → 401 而非 404 | E6 |

---

## 后续 errata 占位（本文件暂不填写）

| ID | 主题 | 状态 |
|---|---|---|
| C1+B2 | `/plugin/register` 鉴权最小化 + heartbeat admin 文档/实现对齐 | ✅ **已按拍板实施**：方案 P / 改文档 / admin 明确拒绝 / 401 分层（详见 §C1） |
| D1 | exec `delivered_at` 语义 | ✅ 已按甲案修复（`docs/D1-EXPERIMENT-RECORD.md`，17/17） |
| G1 | P0-4 精确 token 相等 | ✅ 已补（17/17），并修掉 SDK 头大小写 bug |
| B3 | sticky 三态行为测试 | ✅ 已补（15/15） |
| B4 | exec 回信只走 `/messages/send` | ✅ 本条 |
| 其它 | 见 `V0.1.1-FIX-LIST.md` 债务区 | 不在 v0.1.1 errata 范围 |

---

## S1 — 会话面：canonical 六件套 vs 通道扩展面（2026-09-24 裁定）

### S1.1 背景冲突

| 来源 | 说法 |
|---|---|
| Spec §6.1 | Adapter→Core **canonical 面 = 6 个 API**（heartbeat / messages.send·receive·query / agents / plugin.register） |
| SKILL / 验收与缺口 ⑤⑥ | 要求执行 Core 盖的 `meta.session`，并 `POST /session/bind` 回报落点 |
| `anp_hub` | 已实现 `/session/roster`、`/session/bind`（**不在** §6.1 六件套） |

若把 `/session/*` 强算进「只用 6 API」，则照 SKILL 做 ⑤⑥ 会让资格线假红；若禁止 `/session/*`，则 ⑤ 的 bind 无法落地。

### S1.2 裁定：拆成两层，不二选一

```text
Layer A · canonical 六件套（所有 Adapter 必用）
  POST /heartbeat
  POST /messages/send
  POST /messages/receive
  POST /messages/query
  GET  /agents
  POST /plugin/register          ← 仅插件形态

Layer A+ · 信封字段消费（不算新 API）
  读 wake 载荷里的 meta.session / meta.session_policy / correlation_id
  照 Core 盖章执行 reuse|new —— 禁止 Adapter 再实现一套决策树

Layer B · 通道扩展面（可选；有 Workspace/Session 的通道才要）
  POST /session/roster           ← 上报名册（超 TTL 视为看不到会话）
  POST /session/bind             ← 新开对话后回报落点
```

### S1.3 规则

1. **⑤/⑥ 判定** = Layer A+（读 `meta.session` 照章执行）。**不需要**新 HTTP 面。  
2. **⑤ 的 `POST /session/bind`** = Layer B。仅「平台真有多会话/工作区」的 Adapter 需要。  
3. 资格线「只用 canonical API」白名单 = **Layer A ∪ Layer B**；禁止出现六件套之外的**消息面**私有 API。  
4. **禁止**把 `sessions.decide` 再实现进 Adapter（双真相）。Core 是决策方；Adapter 是执行方 + 可选回报方。  
5. 无 Workspace/Session 的通道（纯 queue / 单会话 CLI）：⑤⑥ 只验 A+（有章就执行）；`/session/bind` 显式 **SKIP 并写原因**，不许用别的用例顶编号。

### S1.4 对文档的落地要求

| 文档 | 改法 |
|---|---|
| Spec §6.1 | 标题改为「canonical 六件套」；增加 §6.1b「通道扩展面（可选）」列 `/session/roster`、`/session/bind` |
| SKILL 自检「接口」 | 「只用 6 API」→「只用六件套 + 已声明的通道扩展面」 |
| 验收 ⑤ | 拆成 ⑤a 照章执行（必测）+ ⑤b `/session/bind`（有会话通道必测 / 无则 SKIP） |
| 各 Adapter H3 | 白名单加入 `session_roster` / `session_bind`（或 HTTP 路径集合） |

### S1.5 兼容性

- 不修改六件套业务字段与 P0-9  
- 不把 `/session/*` 写进「所有 Adapter 必做」  
- 不新增 delivery mode / 不改 wake 载荷形状（`meta.session` 原本就在）

---

## S2 — 必测⑦ 与 P0-2：拆成 ⑦a / ⑦b（2026-09-24 裁定）

### S2.1 冲突

- SKILL 硬规则 / ⑦：「状态全丢后从 Core 恢复」；测法 = 清本地缓存 → 重启 → 仍能收发。  
- P0-2：已有 name + 无 key → **401**，**绝不重发令牌**；v0.1 无 remint 面。

Plugin 形态下「清掉凭证文件」= 丢 agent token，**无法**在不破坏 P0-2 的前提下无钥续命。

### S2.2 裁定：改 ⑦ 的可执行定义，**不改 P0-2，不开 remint 后门**

| 子项 | 名称 | 含义 | 要求 |
|---|---|---|---|
| **⑦a** | 进程重启恢复 | 凭证仍在（插件 data/ 中的 agent key）→ 新端口续约 wake、身份复用 | **必须过** |
| **⑦b** | 凭证全丢 | 删光本地 agent key 后重启 | **v0.1 明文限制**：预期 **401 / 不假装 bootstrap**；须运维 remint 或换名。测试断言「拒绝且不静默换身份」 |

### S2.3 「从 Core 恢复」在 v0.1 的准确含义

```text
身份与令牌     → 只由 Core 发放 / 续约（/heartbeat）
wake 端点     → 由续约更新到新端口
平台会话映射   → 允许本地缓存；可按 meta.session 重建
凭证全丢       → 不等于自动重生；走运维 remint（另立项），不得用 admin/重铸糊弄
```

### S2.4 测试要求

- ⑦a：unload/load 或真重启，断言 **同一** agent key 指纹 + wake URL 更新（现有 G2/G3 方向）  
- ⑦b：删除 `dsh-credentials.json` → 重启 → 断言 `/heartbeat` 续约 **401**，且名册未出现「新铸造的同名身份」  
- 禁止把 ⑦b 测成「清缓存仍能 bootstrap」——那将直接违反 P0-2

### S2.5 后续（不在 v0.1）

若产品需要真正的无钥恢复：单独立项设计「代管关系证明 + 一次性 recovery nonce」（plugin token 证明自己管理该 agent）。在此之前，**丢失 token = 运维事件**。

---

## 后续 errata 占位

| ID | 主题 | 状态 |
|---|---|---|
| S1 | 六件套 / 通道扩展面 | ✅ **本裁定** |
| S2 | ⑦a/⑦b 与 P0-2 | ✅ **本裁定** |
| S3 | wake 签收 / 异步执行 / 失败补偿 | ✅ **本裁定** |
| S4 | need_reply 默认义务 | ✅ **本裁定** |

---

## S3 — wake 签收 · 异步执行 · 失败补偿（2026-09-24 裁定）

### S3.1 问题（Codex Adapter CR-2）

实现里出现：`/anp/wake` 立刻 `ok:true` + `mode`（Core **签收**）→ 后台线程跑平台（如 `codex exec`，可达数十分钟）→ 失败只写本地 log。  
结果：发信方永远等不到回信，Core 也不会重投。这是**静默丢工作**。

根因不只是代码：Spec B1 只写了「何时算签收」，**没写清签收之后 Adapter 还欠什么**。

### S3.2 语义（必须写进 Spec / Skill，并按此实现）

```text
ok:true + 合法 mode
    = 本次 wake 已被 Adapter 接受，并已进入「平台处理队列/轨道」
    ≠ 平台已处理完成
    ≠ 一定会有回信
    = Core 可以记 delivered_at / 签收（不再自动重投本次投递）

因此 Adapter 在返回 ok:true 之前，必须至少做到「工作已持久化可追」，
并且在异步失败时必须有补偿动作 —— 禁止静默丢弃。
```

**三阶段契约：**

| 阶段 | 要求 |
|---|---|
| **A. 签收前** | 校验鉴权与信封完整；把待办落到插件 `data/`（或等价持久队列）；仅当持久化成功才允许 `ok:true` |
| **B. 响应** | `{"ok": true, "mode": <闭集>}`；可附 `accepted: true`、`envelope_id` 等材料 |
| **C. 异步/延后执行** | 失败时**必须补偿**，三选一（按优先序）：① 向 `from` 发失败回信（带 `reply_to` + `correlation_id`）；② 留在本地待重试队列并可观测；③ 平台原生失败回执。**禁止**只写 log |

**鉴权失败 / 信封不完整：** 一律 `ok: false`（可带 `mode`），**不得**签收。

### S3.3 与 queue 档的关系

- `queue` delivery mode：Core 不 push，签收语义见 P0-0。  
- 本条约束的是 **http/exec 已 push 进 Adapter 之后** 的「接受 vs 完成」。

### S3.4 测试要求

1. 平台执行失败（exit≠0 / 超时）→ **不得**静默；必须出现补偿动作（失败回信或 pending 可观测）。  
2. 持久化失败 → 响应必须 `ok: false`（或等价不签收）。  
3. 成功路径：签收后仍有回信（现有两条链保持）。

---

## S4 — `need_reply` 默认义务（2026-09-24 裁定）

### S4.1 冲突

Protocol/旧文档：只有 `need_reply === true` 才**要求**答复，否则是知会。  
Codex `should_reply`：`meta.need_reply is not False` → **默认总是回信**。

### S4.2 裁定

```text
need_reply === true   → 平台产出后应尽力回信（失败走 S3 补偿）
need_reply === false  → 明确不要求回信
need_reply 缺省       → **不要求**义务回信（知会）
```

长任务型平台（Codex CLI）在 `need_reply` 缺省时：**允许**仅落盘结果摘要到插件 data/，或按 S3 失败补偿；**不**默认烧完整平台会话去回一封没人要求的信。  
若产品要「默认总是回信」，必须在**发信 meta** 显式 `need_reply: true`，不得由 Adapter 私自改默认。

### S4.3 测试要求

- `need_reply` 缺省 → 不强制回信（或仅知会落盘）  
- `need_reply: true` → 有回信且带 `reply_to` + `correlation_id`

---

## S5 — 2026-09-26 外部独立审计：契约级修订

> 来源：`docs/AUDIT-20260926.md`（逐条复现 + 修复记录）。
> 性质：**不重开 Spec**，只把"代码本来就是那个意思、但文档没写/写错"的部分钉死，
> 以及把审计发现的两处契约 bug 固定下来。回归门禁：`tests/selftest_audit_20260926.py`（70 项）。

### S5.1 exec 唤醒信任边界：校验的是"被执行的脚本"，不是"任意一个像路径的参数"

旧规则：argv 里**任意一个**像路径的参数落在安装目录内 → 放行。
后果：`python -c "<任意代码>" <任意可信路径>` 也能过（真正执行的是 `-c` 的正文）。
另外相对路径按 hub 进程 cwd 解析，而实际执行用 `wake.cwd`；`argv[0]` 完全不看
（`["/tmp/evil.exe", "<可信路径>"]` 也能过）。

**新规则（三层）**：

1. **开关层**：出现内联执行开关（`-c` / `-e` / `-m` / `--eval` / `/c`）或
   按路径再加载代码的开关（`-r` / `--require` / `--import` / `--loader`）→ 一律拒绝；
2. **argv[0] 层**：解释器（`python` / `node` / `bun` / `deno` / `cmd` / `powershell` /
   `bash`…，**认名字不认路径**）或**安装目录内**的可执行文件；其它一律拒绝；
3. **脚本层**：第一个带源码扩展名的参数（没有则退回第一个像路径的参数）必须在信任根内；
   它之后再出现"像代码"的参数也要在信任根内。

相对路径一律按 `wake.cwd` 解析（实际执行目录）。信任根仍是
`src/` + 仓库根 + `<home>/plugins/` + `AGENT_ANP_TRUST_WAKE_PATHS`。
**数据参数不受影响**（`dump.json`、`--hub http://127.0.0.1:PORT` 这类不是代码，不校验）。

### S5.2 身份名 / 关联线 id 的落盘编码：**单射**（不同输入 → 不同文件）

旧规则是"滤掉不认识的字符"：`a/b`、`a:b`、`ab` 全变成 `ab`。
后果：不同身份**共用一个收件箱文件**（互相读到/签收/重投对方的信）；
不同关联线**共用一个 journal 文件**（授权判据是共享文件头的 owner → 越权读）。

新规则：不认识的字符按 UTF-8 字节**百分号转义**（`%` 自己转 `%25`），
只含 `[字母 数字 -_.@]` 的名字（现有全部身份）**输出一字不变**，
老收件箱文件照旧命中。`known_inboxes()` 会解码回原始身份名再给展示面。
另外：`POST /heartbeat` 现在**拒绝**含编码字符集之外字符的身份名（400）。

### S5.3 插件权限：5 个闭集权限全部有真执行点

执行点清单见 `ANP-PLUGIN-SPEC.md` §5.1（本次新增）。
一句话版本：`tools.register` 管注册表、`wake.others`/`broadcast` 管出站发信、
`webui.panel` 管面板、`agents.discover` 管 `/tool` 的 `mail_agents`。
只对**已加载插件身份**查表——Adapter 代管的 agent 与 MCP 客户端是普通身份，不受影响。

### S5.4 两个契约 bug（本次修掉）

| 位置 | 旧行为 | 新行为 |
|---|---|---|
| `POST /plugin/register` 响应 | 硬编码 `"auth": "admin"` | `"auth": "plugin-credential"`（这个入口验的是插件自己的令牌，admin 在此明确不被接受） |
| `mail_inbox(order="newest")` | 取最新 N 封但**仍按旧→新排**（与 `mail_pending` 的 C,B 不一致） | 真的倒序返回 |

### S5.5 其它非契约但影响发布的修订

- **默认端口统一为 8900**：README / SDK / `core.DEFAULT_HUB` / hub 的 argparse 与
  `spawn_hub` 回落值，现在全部是 8900（hub 自己原来写 8899，"按 README 起再访问 8900"必失败）。
- **伴生 WebUI 指向 `webui/desktop.py`**：原来找不存在的 `src/mailbox_webui.py`，
  默认开启却静默跳过（看板其实从来没起来）。伴生时传 `--no-browser --no-sync-adapters`，
  并用 `AGENT_ANP_HUB_URL` 把 hub 地址交给它。
- **安装器**：复用 Core 的 manifest 校验（挡 `id="..\victim"` 这类路径穿越），
  退出码改为"全部装上且全部加载成功才 0"。
- **WebUI 令牌边界**：`/_anp/session` 只发给**回环客户端**（非回环 404）；
  本地 admin 令牌**只发往回环 hub**（`AGENT_ANP_HUB_URL` 指向远程时不再外送）。
- **伴生看板的生命周期**：hub 拉起 `webui/desktop.py` 时注入 `ANP_WEBUI_COMPANION=1`，
  看板据此挂 hub 看门狗（hub 不在就自己退）——否则每个 hub 死后都留一个 idle 看板进程
  （2026-09-26 实测 107 个孤儿）。`GET /health` 只报服务名，**不回令牌**。
