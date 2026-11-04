# ANP Adapter 参考实现（adapter_demo）

这份代码不是"实现某个真实平台"。它的用途是**证明一件事**：

> 一个完全不了解 AgentNetPort 内部实现的第三方，只拿
> `docs/ANP-ADAPTER-SPEC.md` + 这份参考代码，就能写出自己的 Adapter。

所以它守三条硬约束：

| 约束 | 怎么做 | 怎么验 |
|---|---|---|
| **零内部依赖** | 只 import 标准库，与 Core 之间只有 HTTP | `verify.py` A1-A3：把 `src/` 改名藏起来后依然能 import（D2） |
| **平台知识只在一处** | 只有 `DemoPlatform` 知道"平台长什么样" | B1.4：平台侧只看到翻译后的原生格式 |
| **mode 只是 transport** | 三种唤醒档只在 `WakeTransport` 的三个子类里不同 | `Adapter` 的主逻辑与 mode 无关 |

---

## 快速看

```
anp.adapter.demo          ← adapter 自己（管理 adapter 自身）
    │
    └── demo-01           ← 被接入的外部 agent，参与 ANP 通信
            │
            └── WakeTransport          ← ★mode 只在这里不同★
                 ├── HttpWake    http   档 Core 主动 POST /anp/wake
                 ├── ExecWake    exec   档 Core 每封信拉起一个进程
                 └── QueueWake   queue  档 没有 push 目标，adapter 自己来取
```

**两个身份是两枚不同的令牌，任何情况下不互换、不共用**（Spec §2 / P0-1）：

```
adapter 自己的令牌   → 只管 adapter 的事务（/plugin/register 之类）
agent 的令牌         → 代这个 agent 发信、收信
```

> 这是**形态一（普通进程）**的形状：adapter 和 agent 都是普通身份，adapter 自己拉起自己。
> 形态二（Adapter Plugin，由 Core 拉起）见下一节，原型在 `examples/adapter_plugin/`。

---

## 两种形态：你可能是普通进程，也可能是 Adapter Plugin

这是最容易搞混的地方，所以放在最前面。Adapter 有**两种形态**，跑的是**同一套协议**
（公开 `/heartbeat` 代管 Agent 身份、`/anp/wake` 收信封、`/messages/send` 回收回信）。
差别只在**身份怎么来的、谁拉起进程**。

### 形态一：普通外部进程 ← **本目录演示的是这种**

```
普通外部进程
    └── 自己就是一个 ANP Agent
```

自己 `/heartbeat` 注册，自己起 HTTP 服务，自己常驻。
**适用于**：平台本身有进程模型（CLI Agent、HTTP Agent、Desktop App）。

### 形态二：Adapter Plugin

```
Adapter Plugin
    └── Plugin 身份（anp.adapter.codex）
          └── 代管一个或多个 ANP Agent（codex-01 / codex-02 / codex-review）
```

由 Core 拉起，凭 `/anp/register` 送来的身份和令牌做事，
然后**代管**一个或多个外部 Agent 身份。
**适用于**：要挂进 ANP 生态、随 Core 一起启动、被 Core 统一管理。

> 原型见 `examples/adapter_plugin/`（17 项实验全绿）。

### Plugin 和 Adapter Plugin 的关系

```
Plugin
├── 普通 Plugin           （应用层：公告栏、游戏、 whatever）
└── Adapter Plugin        （接入层：把一个或多个外部平台 Agent 映射成 ANP Agent 身份）
```

Adapter Plugin **继承 Plugin System 的全部基础机制**（manifest、双向握手、工具注册、
事件订阅、面板），再增加 Adapter 自己的两项职责：

1. **代管 Agent** —— 通过公开 `/heartbeat` 注册别的 Agent 身份
2. **语义翻译** —— ANP 信封 ⇄ 平台原生格式

### 为什么 Adapter Plugin 不是「换个 wake」

2026-09-22 原型验证修正了一个曾经的错误说法。旧说法是
「Adapter = Plugin + 一个 wake 实现」，**这是错的**：

> **普通 Plugin 本来就有 wake。** Core 加载插件时就给它登记
> `wake={"kind":"http","url": <base_url>+"/anp/wake"}`，并把插件 id 登记成一个正常的
> ANP 身份。也就是说任何 Plugin 本来就是可寻址、可被唤醒的 ANP Agent。

wake 是**通信机制**，不是 Adapter 的本质区别。本质区别是**代管 + 翻译**。

### 一个必须知道的运维陷阱

Adapter Plugin 代管 Agent 走的是 `/heartbeat` 的**首次注册**门，由
`permissions.json` 的 `register` 决定：

| `register` | 结果 |
|---|---|
| `open`（默认） | Adapter 可以注册代管 Agent |
| `allowlist` | Agent 名**必须**在 `allow` 里 |
| `closed` | **Adapter 完全无法注册代管 Agent** |

**收紧 `register` 会让所有 Adapter Plugin 一起失效**——而且报的不是权限错误，
是「Adapter 启动正常，但代管的 Agent 身份根本不存在」。

这不是 Adapter 专属权限问题，是 **Agent identity registration policy**。
包装 Adapter Plugin 前，先把要代管的 Agent 名字全加进 `register.allow`。

---

## 骨架：你要实现的就这几样

`adapter.py` 分四层，从下往上读：

| 层 | 类 | 要不要改 |
|---|---|---|
| 1 | `AnpClient` | **不改**。只封装 Spec §6.1 那 6 个 Core API |
| 2 | `DemoPlatform` | **★这就是你要改的★**。平台知识全在这里 |
| 2.5 | `AdapterDirectory` | **可选**。模糊目标 → canonical Agent ID（下面那一节） |
| 3 | `WakeTransport` 三个子类 | 只在你平台的唤醒形态不在三种之内时才加一个 |
| 4 | `Adapter` | **不改**。`bootstrap / register_agent / on_wake / send / receive` 与 mode 无关 |

`Adapter` 的五个方法对应 Spec 的要求：

```
bootstrap()       托管身份：两个身份、两枚令牌（Spec §1 第 1 条）
register_agent()  声明 agent 的 wake 档位（Spec §3）
on_wake()         唤醒入口：翻译 → 递平台 → 回收回信（Spec §1 第 2/3/4 条）
send() / receive() 代 agent 收发（Spec §1 第 3 条）
```

`Adapter.step()` 里 mode 的差异只有**一行**：

```python
envelope = self.transport.next_envelope(self.agent_client(), timeout)   # ← 只有这行因 mode 而异
if not envelope:
    return None
return self.on_wake(envelope)                                          # ← 三种档位共用
```

---

## 寻址：'发给负责 Live2D 的' → canonical Agent ID

发信方**常常不知道对方的 ID**，只知道"我要找负责 Live2D 的"。
Core 的 `to` 只认 canonical Agent ID，所以"那个谁到底是谁"由 **Adapter** 回答
（Spec §8.5）：

```
用户 / 上层 Agent
    「发给负责 Live2D 的」
        ↓
AdapterDirectory.resolve()        ← 这一步在 Adapter 里，Core 完全不知情
    ├── RESOLVED    → canonical Agent ID ──► Core 只按这个 id 投递
    ├── AMBIGUOUS   → candidates（★不猜★，让上层再指一次）
    └── NOT_FOUND
```

数据只有两处来源，**都不需要 Core 增加任何东西**：

| 来源 | 内容 | 谁持有 |
|---|---|---|
| `GET /agents` | name / description / skills / caps / **last_seen（心跳活跃度）** | Core（公开名册） |
| `LocalDirectory` | 平台短名 / 工作区标题 / alias / tags / **last_used（最近用过）** | **只有 Adapter** |

```python
r = adapter.resolve("负责 Live2D 的")
# r.status == "AMBIGUOUS" → r["candidates"] = [A, B, C]（带 reasons：哪个字段命中了哪个词）
# 换个说法就唯一了：adapter.resolve("live2d 素材") → RESOLVED demo-live2d-b

out = adapter.send_fuzzy("做 Minecraft 的", "正文")     # resolve → send 两步，分开看
# {"resolve": {…}, "send": {…}}；解析没成功时 send 恒为 None —— 找不到人就不发信
```

**三条要记住的规则：**

1. **`last_seen` ≠ `last_used`**（Spec §8.6）：
   - 名册的 `last_seen` = **心跳活跃度**（"它还活着"）→ 只用于排序，**默认不许替用户挑人**；
   - `last_used` = **Adapter 自己记的"最近真的被用过"** → 并列且差距够大时才可破并列；
   - 只有一部分候选有 `last_used` 时也不许用（那会选出"唯一有记录的那个"）；
   - 结论里 `decided_by` 会写明来源：`recency.platform` / `recency.heartbeat`。
2. **解析失败 ≠ 投递失败**："不知道发给谁"（`NOT_FOUND`/`AMBIGUOUS`）和
   "知道发给谁但投不出去"（`send` 的 `wake` / 状态字段）是两件事。
3. **没有 AI**：`resolve.py` 只依赖标准库，普通字符串比较。
   第三方 Adapter 可以完全不支持模糊解析——不调它就是精确 ID 模式，一样合规。

手工试：

```powershell
# 只解析（起一个 hub，注册几个候选样本之后）
python examples/adapter_demo/adapter.py --resolve "负责 Live2D 的" --hub http://127.0.0.1:8900
# 解析 + 按 canonical ID 发信
python examples/adapter_demo/adapter.py --to "做 Minecraft 的" --body "你好" --hub http://127.0.0.1:8900
# 带上 Adapter 自己的平台提示（alias / 工作区标题 / 短名）
python examples/adapter_demo/adapter.py --resolve "立绘那个" --hints hints.json --hub http://127.0.0.1:8900
```

---

## 接自己的平台要改什么

改 `DemoPlatform` 的三个翻译面：

```python
class DemoPlatform:
    def to_native(self, envelope) -> dict     # ANP 信封 → 平台能懂的东西
    def deliver(self, native) -> None         # 递进平台（注入会话 / POST 出去）
    def platform_answers(self, envelope)      # ★平台自己干活★（这不是 adapter 的职责）
    def from_native(self, answer) -> dict     # 平台回信 → 发回 ANP 所需的字段
```

**三条铁律：**

1. `from_native` 必须把 `correlation_id` 原样带回，否则回信落不回原对话线
2. `reply_to` 由 `Adapter._reap()` 自动补（用信封的 `id`），平台通常不关心它
3. `to_native` 不要把整个 envelope 塞给平台——那是把 ANP 的内部形状泄出去

如果你的平台唤醒形态不在 http / exec / queue 之内，加一个 `WakeTransport` 子类，
回答两个问题：`declare_wake()` 怎么写注册声明、`next_envelope()` 从哪拿下一封。

---

## 三种 mode 验证的是契约，不是功能

```
HTTP    Core → POST /anp/wake → adapter → agent
        验：信封真的被打过来了、x-mailbox-key 是目标 Agent 的令牌

EXEC    Core → Popen → 环境变量 → adapter → agent
        验：Core 真的拉起了进程、子进程从 AGENT_ANP_SELF_KEY 拿到 Agent 令牌

QUEUE   Core → 持久化 → adapter 自己调 /messages/receive
        验：attempts=0、next_retry_at=None、wake_skipped="no_push_target"
```

queue 档那三条是 P0-0 的契约。它们**不能**靠旧的 push 逻辑顺带达成，
必须是被显式判定为"没有 push 目标"——所以 verify.py C2-C5 是逐条对着公开 API 断言的。

---

## 跑验收

```powershell
python examples/adapter_demo/verify.py
```

它会自己起临时 hub，跑完清理。加 `--keep` 保留临时目录排查。
现在是 **61 项**（原 30 项契约测试 + E 段 31 项寻址验收）。

验收内容：

```
A. 静态：只 import 标准库、无 src/ 依赖、无 sys.path 注入、只用 Spec 的 8 个端点
B1. http 档：真起唤醒服务，让 Core 真的 POST 过来
B2. exec 档：让 Core 真的 Popen 一个子进程
C.  queue 档：逐步证明 P0-0 的四条不变量
E.  寻址：精确 / 唯一 / 多候选 / 无结果 / last_seen 只排序 / last_used 才破并列 /
    Core 边界 / 无 AI
D.  第三方视角：复制到仓库外 + 把 src/ 整个改名藏起来，重跑 import
```

D 段是这个参考实现的**资格线**——如果哪天 adapter.py 因为"图方便"import 了 Core
内部模块，D2 会立刻红。

---

## 手工跑 adapter 进程

```powershell
# http 档：起唤醒服务（真实部署里由你的进程常驻）
python examples/adapter_demo/adapter.py --serve-http --port 9001 --hub http://127.0.0.1:8900

# exec 档：这一支由 Core 拉起，不要手工跑
AGENT_ANP_WAKE_JSON='{"id":"m-1","from":"a","body":"hi"}' ^
AGENT_ANP_SELF=demo-01 ^
AGENT_ANP_SELF_KEY=<agent token> ^
python examples/adapter_demo/adapter.py --wake-exec

# queue 档：主动收一次
python examples/adapter_demo/adapter.py --poll --hub http://127.0.0.1:8900
```

---

## 这份代码刻意没做的事

- **没有重试、没有退避、没有"在线"判断**——那些是 Core 的事（Spec §1「禁止做」）
- **没有读任何 Core 内部文件**——没有 `keys.json`、没有 `ledger.jsonl`
- **没有统一的 `status` 对象**——Spec §7 只认 `liveness` / `reachability` / `lease`
  三个独立事实
- **没有把三个 mode 写成三个 demo**——它们共用同一个 `Adapter`

---

## 下一步

这份参考实现是 SDK（#3）的来源。SDK 应该从它里面**抽出来**，而不是反过来先设计一个
SDK 再强迫示例去适应它。所以顺序是：先把这份参考实现稳定住 → 再抽 SDK。
