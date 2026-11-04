# D1 实验存档：exec `delivered_at` 语义

> 结论：**实锤（P0）**，且**已按甲案修复**。`tests/selftest_d1_exec_delivery.py` 17/17。
> §1–§3 是修复前的原始证据存档；§4 已从"待确认"改为"已实施"。

| 项 | 值 |
|---|---|
| 实验脚本 | `tests/experiment_d1_exec_delivery.py`（可重跑，自建临时 hub + home） |
| 日期 | 2026-09-23 |
| Core 版本 | `src/anp_core.py` / `src/anp_hub.py`（#4.1a 之后） |
| 方法 | 真起 hub + 两个 exec agent：一个脚本 `exit(1)` 且不读环境，一个正常 `mail_ack` |

---

## 1. 时间线与四字段

### 实验组（配适器崩在启动瞬间）

注册 `crash.guy`，`wake.command = [python, d1_crash.py]`，脚本内容 `import sys; sys.exit(1)`。

发一封信，然后连续观测（单位：秒 / 值）：

| 时刻 | attempts | delivered_at | next_retry_at | read | read_by | last_error |
|---|---|---|---|---|---|---|
| 发信即刻 | 1 | `1790141632.14` | `null` | false | null | null |
| +2.5s | 1 | `1790141632.14` | `null` | false | null | null |
| +4.0s | 1 | 同左 | `null` | false | null | null |
| +5.5s | 1 | 同左 | `null` | false | null | null |
| +7.0s | 1 | 同左 | `null` | false | null | null |
| +8.5s | 1 | 同左 | `null` | false | null | null |
| +10.0s | 1 | 同左 | `null` | false | null | null |
| +11.5s | 1 | 同左 | `null` | false | null | null |
| +13.0s | 1 | 同左 | `null` | false | null | null |

发信时的 `wake` 返回：`{"ok": true, "kind": "exec", "pid": 37236}`

### 对照组（正常处理并 `mail_ack`）

注册 `ack.guy`，脚本读 `AGENT_ANP_WAKE_JSON` → 调 `/tool` 的 `mail_ack`：

| 时刻 | attempts | delivered_at | next_retry_at | read | read_by |
|---|---|---|---|---|---|
| 发信即刻 | 1 | `1790141740.17` | `null` | false | null |
| +3s | 1 | 同左 | `null` | **true** | **`ack.guy`** |
| +7s | 1 | 同左 | `null` | true | `ack.guy` |

---

## 2. 实锤判定

fix list 的判据逐条对：

| 判据 | 实测 | 结论 |
|---|---|---|
| `delivered_at != null` | `1790141632.14`（崩溃即写入） | ✅ 命中 |
| 后续 `auto_redeliver` **永不**重投 | 13 秒共 8 次采样，`attempts` 恒 1、`next_retry_at` 恒 `null` | ✅ 命中 |

**对照组成立**是关键：同一个 Core、同一条 exec 路径，只因为脚本真的 `mail_ack`，
`read_by` 就记上了 `ack.guy`、`attempts` 不再涨。

所以两组的差别**只**在「有没有 ack」，而不在别的。实验组的信：

```
delivered_at  = 有值     → 看起来"投递完成了"
attempts      = 1        → 只试过一次
next_retry_at = null     → 永远不会再试
read          = false    → 从未被读到
read_by       = null     → 从未被签收
last_error    = null     → 连个错误都没留下
```

一封信就此**永久卡在「已投递但没人读过」**，且系统认为它已经送完了。

---

## 3. 为什么会这样（代码路径）

```
core.wake()  exec 档
    └─ Popen(command) 成功
         └─ return {"ok": True, "kind": "exec", "pid": …}     ← ★只看进程拉起来了
hub.deliver_one()
    └─ outcome.ok 为真 → record_delivery(…)
         └─ 写 delivered_at = now
auto_redeliver()
    └─ 只捞 delivered_at 为空的信
         └─ 这封有 delivered_at → 永不入选
```

对照 http 档：`wake_sign_off_ids()` 要求响应带合法 `mode` 才签收（B1 刚把这条写进规范）。
而 exec 档 `wake_sign_off_ids` **故意不签收**——可它仍然通过 `outcome.ok` 写了 `delivered_at`。

**两档的判据在这里分叉了**：http 的「ok」= 适配器确认签收；exec 的「ok」= 进程拉起来了。
同名不同义，这就是裂缝。

---

## 4. 修复方案（已按甲案实施）

fix list 的推荐语义补丁：

```text
exec 档 spawn 成功 → 不写 delivered_at
                    → 只写 last_attempt_at（+ 可选 spawned_at / wake_pid）
delivered_at        → 仅在「唤醒器 mail_ack / 约定落地」时写（此时 read_by 会有值）
auto_redeliver      → 条件从「未 delivered」改为「未 ack 且到期」，
                      但★只动 exec 档★，http / queue / skipped / no_push 行为保持不变
```

### 影响面

| 项 | 判断 |
|---|---|
| exec 档 | 行为变化：崩溃/未 ack 的信会进入重投 |
| http 档 | 不变（仍走 `wake_sign_off_ids` 的 `mode` 签收门，B1 已验） |
| queue 档 | 不变（P0-0 终态） |
| `wake=false` / `no_push` | 不变 |
| 退避公式 / `max_attempts` | 不变 |

### 风险复验（改前担心的两点，实测结论）
   ① 「未 ack 就重投」——实测是**有限**的（见 §6）：默认上限 5 次，
   之后 `next_retry_at=null` + `last_error="已放弃自动重投（…）"`，**不是无限重启**。
   退避默认 120s × min(attempts,6)，5 次约 28 分钟，节奏不算风暴。
   ② 「ack 之前这封信算几分」——由 `spawned_at` / `wake_pid` 承担，不混入 `delivered_at`。

**实施中自己制造又修掉的两条错（都记，防止再犯）**：

1. **`do_redeliver` 也要改**。第一版只改了 `deliver_one`，结果第二投就把
   `delivered_at` 写上，信立刻被 `undelivered_only` 排除，attempts 卡在 2 不动。
   → 记账口径必须**两处一起改**（`deliver_one` / `do_redeliver`）。

2. **凭印象给 `do_inbox` 加了个不存在的 `else` 分支**。
   我加的是 `else: stamped = 0; for r in picked: r["read"] = True`。
   而 `picked` 与 `rows` 是**同一批 dict 对象**，peek 一改，返回里从 `rows` 现算的
   `unread` 跟着变 0 → P0-9 的 4.1 直接打红（`未读 0 → 2`）。
   后来才发现：正确的循环本来就该在 `if mark:` 分支里（sign_off 落盘后同步内存副本），
   peek 分支**什么都不该做**。
   **教训：改老函数前先读它原本长什么样，不要凭印象补分支。**

---

## 5. 顺带发现（API 形状坑）

实验过程中踩到两处 API 形状，都不算 bug，但容易让人写出"看着绿"的假测试：

| 现象 | 真相 |
|---|---|
| `/tool` 的 `mail_ack` 回 `ok:false「要签收哪些信？」` | 参数键名是 **`args`**，不是 `arguments`；且 ack 里是 **`ids` / `id`**，不是 `message_ids` |
| 给 `wake=false` 的信做 ack 前，它已经 `read=true` / `read_by='cache:silent-delivery'` | 存在一条自动签收路径（`cache:silent-delivery`），**会掩盖未签收场景**。D1 的对照组必须用 `wake=true`，否则 read_by 永远有值、测不出差别 |

第 2 条值得单独查：`cache:silent-delivery` 是谁写的、在什么条件下写。
它可能让任何"未签收"类测试假绿。


---

---



---

7. 甲的有限重投实测（用户 2026-09-23 要求"必须实际查，不是猜"）

用 http 打一个空端口，制造与甲案**同一种状态**（`delivered_at` 空 + attempts 递增 + `next_retry_at` 有值），
设 `AGENT_ANP_REDELIVER_SEC=15` / `RETRY_SEC=2` / `REDELIVER_MAX=5`：

| 时刻 | attempts | delivered_at | 备注 |
|---|---|---|---|
| t=1s | 1 | 空 | 首次投递失败 |
## 6. 甲的有限重投实测（用户 2026-09-23 要求"必须实际查，不是猜"）

用 http 打一个空端口，制造与甲案**同一种状态**（`delivered_at` 空 + attempts 递增 +
`next_retry_at` 有值），设 `AGENT_ANP_REDELIVER_SEC=15` / `RETRY_SEC=2` / `REDELIVER_MAX=5`：

| 时刻 | attempts | delivered_at | 备注 |
|---|---|---|---|
| t=1s | 1 | 空 | 首次投递失败 |
| t=15s | 2 | 空 | 扫描轮 1 |
| t=32s | 3 | 空 | 扫描轮 2 |
| t=49s | 4 | 空 | 扫描轮 3 |
| t=66s | 5 | 空 | 扫描轮 4 |
| t=81s | 5 | 空 | `已放弃自动重投（尝试 5 次 ≥ 上限 5）`，`next_retry_at=null` |

**结论：有限重试 → abandoned，不是无限重启。**（`AGENT_ANP_REDELIVER_SEC` 下限 15 秒，
不设就是 120 秒——观测窗口不够会误判成"卡住"，这个坑踩过一次。）

## 7. `cache:silent-delivery` 是什么（用户第一个问题）

| 问题 | 答案 |
|---|---|
| 谁写的 | `src/anp_hub.py:286`，在 `deliver_one` 的 `skipped` 分支 |
| 什么条件 | **发送方明确 `wake=false`**（缓存信）。这是你四个选项里的 ③（触发条件） |
| 表示什么 | **① 消息已经送达**——安静送达，只是没叫醒人（你的原话："它是送达到了的"） |
| 是不是 bug | **不是**。2026-09-21 你自己指出的需求：缓存信不该永远挂在未读里 |

它还给了一个反证：`sign_off` **只写 `read`/`read_ts`/`read_by`，不写 `delivered_at`**，
而 `sign_off` 的 docstring 承诺了三个签收点，其中**"exec 档唤醒器"在代码里根本不存在调用点**——
这正是 D1 的精确根因。所以 D1 的修法是让 ack 路径补写 `delivered_at`（`core.stamp_delivered`），
而不是去改 `sign_off`（那会把缓存信的 `delivered_at` 也一起改掉，违反"不合并两个机制"的约定）。
