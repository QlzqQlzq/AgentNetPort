# DSH Adapter（ANP）

把 **DeepSeek Harness（DSH）** 接入 AgentNetPort 的 **Adapter Plugin**。

```
ANP Core（src/anp_hub.py）
      ↓  只有 HTTP，没有代码依赖
本 Adapter Plugin（一个身份 + 一个可寻址端点）
      ↓  平台语义只住在这两个翻译函数里
DSH（会话 / 工作区）
```

一句话：**Core 只知道「把信送给一个叫 `dsh` 的 Agent」；「这个 Agent 在 DSH 里
具体是哪扇门」全是本 Adapter 的事。**

## 装了什么

```
adapters/dsh/
├── manifest.json        anp.adapter.dsh（command 拉起；权限只用现有两项）
├── platform.py          ★DSH 平台语义★——ANP 一概不知道这一层
├── plugin.py            ANP 侧：代管身份、/anp/wake、信封翻译、回信回收
├── verify.py            43 项验收：两条链 + 必测八项 + 资格线
├── anp_adapter/         vendor 进来的 Adapter SDK（★部署形态必须这样★）
└── README.md            本文件
```

## 两个身份，两枚令牌（P0-1）

```
anp.adapter.dsh    ← Adapter Plugin 自己（Core 在 /anp/register 发给它）
dsh                ← 被代管的 DSH Agent 身份（本插件用公开 /heartbeat 铸造）
```

两枚**不互换、不共用、不派生**。`plugin` 令牌只用于 `/plugin/register` 与自身；
`agent` 令牌只用于代 `dsh` 收发信。验收 C1 会实测两者指纹不同。

## 接入形态：为什么是 Adapter Plugin

| 判据 | DSH 的情况 |
|---|---|
| 有插件体系、能力由插件组合 | ✅ 有 |
| 希望随 Core 启动 | ✅ 要 |
| 能收 `/anp/register` | ✅ 能 |

→ **Adapter Plugin**（不是普通外部进程）。身份由 Core 送来，不用自己猜。

## delivery mode：为什么是 `http` 而不是 `exec`

本插件给代管身份登记的唤醒端点是：

```python
def wake_declaration(self) -> dict:
    port = os.environ.get("ANP_PLUGIN_PORT") or "0"
    return {"kind": "http", "url": f"http://127.0.0.1:{port}/anp/wake"}
```

**Plugin 形态必须用 http**：`exec` 唤醒引用的脚本要过 Core 的信任校验
（只认信箱安装目录），而插件按设计装在 `<home>/plugins/<id>/`，真实部署下
**永远在外面**，一律被拒。Adapter Plugin 自己就在 `ANP_PLUGIN_PORT` 上听着
`/anp/wake`，代管身份直接指过去。

端口由 Core 每次随机分配并通过环境变量注入，所以**必须现读环境变量**，不能写死。

## 对象映射（Spec §8.5.8：Workspace / Session 不注册成 Agent）

| ANP 侧 | DSH 侧 | 边界 |
|---|---|---|
| Agent identity | 一个身份名 `dsh` | 一经注册不得改名（换名=换人） |
| Envelope | DSH 待办条目 | 只允许出现在 `to_native` / `from_native` 里 |
| delivery mode = `http` | 本插件的 `/anp/wake` | Core 只认 http/exec/queue |
| **correlation_id = 对话线** | DSH **会话** | ★不同层，不许混为一谈★ |
| meta.from/to_workspace | DSH **工作区** | 工作区是坐标，不是身份 |

```
ANP Agent「dsh」（一个身份，Core 只看得见它）
   └── DSH 侧 N 个 session / workspace   ← platform.SessionBook 维护，Core 看不见
```

会话归属**只用对话线**（`correlation_id` / `context_id`）判定，`STICKY_TTL`
之内复用，过期仍复用原会话但标记 `correlation-stale-reuse`；没有对话线才开新会话。

## 四个必须职责（Spec §1）

| 职责 | 落在哪 |
|---|---|
| ① 托管身份 | `register_agent()` → 公开 `POST /heartbeat` |
| ② 实现唤醒 | `PluginServer` 的 `/anp/wake` |
| ③ 语义翻译 | `platform.to_native` / `platform.from_native` |
| ④ 回收回信 | `_reply_ack()` / `reply()` → `POST /messages/send` |

**入站鉴权**：`/anp/wake` 的 `x-mailbox-key` **永远是目标 Agent 的令牌**
（不是本插件的、更不是 hub secret）。令牌由 Core 投递时捎带，所以本插件
**从不翻 `keys.json`**。不匹配就回 `ok: false`——**绝不许假装签收**。

**响应必须带合法 `mode`**（Spec §5.1.1）：只回 `{"ok":true}` Core **不签收**，
信会保持未读并被反复重投。本插件回 `"batched"`（信已落入 DSH 待办队列，尚未注入运行中会话）。
注意 `wake.kind`（http/exec/queue）与响应里的 `mode`
（immediate/batched/session/inject/followup）**不是同一个概念**。

## 平台状态放哪

**放插件自己的 `data/`，绝不写进信箱 `<home>` 根下**（否则两套系统共管一个目录）：

```
data/dsh-pending.jsonl     递进来的待办（机器读）
data/dsh-pending.md        同一件事的人读版
data/dsh-sessions.json     对话线 → 会话映射
data/dsh-outbox.jsonl      发回 Core 的回信账
data/dsh-status.json       运行状态（验收与排障读它）
data/dsh-credentials.json  ★代管身份的令牌，供重启恢复★
```

## ★本次踩到的两个真问题（都已修，都写进代码注释）★

### 1. `on_register` 里同步回调 Core → 两端互等，撞破 10 秒启动预算

最初在 `on_register` 里直接同步调 `register_agent()`，而它会 POST Core 的
`/heartbeat`。此刻 Core 正卡在 `register_out()` 里等本插件回 `/anp/register` 的包
—— **两端互等**。实测该 handler 要 **6.3 秒**才回包，叠加冷启动后冲破 Core 的
10 秒预算，Core 只报：

```
插件端点不可达：POST http://127.0.0.1:<port>/anp/register —— TimeoutError
```

而插件进程其实**完全健康**（`/anp/health` 返回 `ok:true`）——报错方向完全指错。

**修法**：握手回包与代管注册**解耦**，注册丢后台线程；状态写
`data/dsh-status.json`，让"插件起来了"与"代管 agent 注册成功没有"仍然分得开。

### 2. 重启后 wake 停在死掉的旧端口

Core 每次拉起插件都分配**随机新端口**。插件重启后必须带**旧令牌**去
`/heartbeat` 续约才能把 wake 更新到新端口；否则它会以"新身份"去 bootstrap，
撞上 P0-2 的「已有 name + 无密钥 → 401」，**wake 就永远停在死掉的旧端口上**
——症状是"注册成功但信永远投不进来"。

**修法**：Adapter 持有它代管身份的令牌（Spec §2 明文允许），落盘
`data/dsh-credentials.json`，重启后读回续约。验收 G2 实测
`旧端口 → 新端口` 自愈成功。

### 3. 新版 DSH 换了 CLI 形态，且多行 prompt 不能走 argv（2026-10-12）

DSH 从"自建桌面壳 + npm runtime"换成**官方 Electron 安装版**后，CLI 整条路都变了：

| | 旧版 | 新版 |
|---|---|---|
| CLI | `<root>/runtime/node_modules/@deepseek-ai/dsh/lib/bin.js` | `<安装目录>/resources/runtime/cli/bin/dsh.cmd` |
| 拉起方式 | 要 `node` 前置 | `dsh.cmd` 自带 Electron 运行时，直接可执行 |
| 部署位置 | `<DSH_SOURCE>`（自建壳源码树） | `%LOCALAPPDATA%\Programs\DeepSeek Harness` |

**症状**：三条候选路径全部指向已删除的目录 → `FileNotFoundError: DSH CLI not
found` → 消费者回一封失败补偿信。从 Core 侧看"投递成功、签收成功"，
**完全看不出平台根本没被叫起来**。

**修法**（`_resolve_cli()` / `_dsh_cli_candidates()`）：
`ANP_DSH_CLI` → `ANP_DSH_DESKTOP/<安装目录>` → `%LOCALAPPDATA%` 默认位置 →
旧版 `ANP_DSH_ROOT` 布局，`.cmd` 不前置 node（`is_js` 只对 `.js` 为真）。

★第二个坑更隐蔽★：新版 `dsh --profile headless <task...>` 的 task 是
**多个词按空格 join**。多行 prompt 当 argv 传会被压坏——实测单行 OK，
两行起就丢正文，模型收到的是"消息："后面空白的残句，于是回一封
"信封里没有正文"的客气话。**固定短语没有原样带回**是它唯一的可观测信号。

**修法**：走 `-`（stdin）。`--help` 明写 `` `-` reads stdin ``，实测换行完整保留。
保留 `ANP_DSH_PROMPT_VIA_ARGV=1` 可强制回退 argv（旧版 DSH 没有 `-`）。

> ★验收盲区（必须知道）★：`verify.py` **从不调 `_run_headless`**——它发的信
> `need_reply` 都缺省，消费者按 Errata S4 只落 `notice` 不跑平台。所以
> **verify 全绿 ≠ DSH 真的被叫起来**。上面这个坑就是全绿期间发生的。
> 补测办法：驱动安装目录里的 Adapter 本体调 `_run_headless()`，
> 或从 ANP 真发一封 `need_reply=true` 的信，断言回信正文**原样带回固定短语**。

## 跑验收

```powershell
python adapters/dsh/verify.py
```

它会自己起一个**临时 hub**、把 adapter **连同 SDK 一起装进
`<home>/plugins/anp.adapter.dsh/`**（真实安装路径，不是源码目录），再跑：

```
A 健康 / 密钥隔离        B Core 拉起插件 + 代管身份
C 身份与令牌（P0-1/2/6）  D 正向链 Core → Adapter → DSH
E 收信回执落回原对话线    F 入站鉴权 + 必测八项
G 必测⑦ 进程重启恢复      I Adapter 本体驱动（reply() / Adapter.resolve / CR-1 钉死）
H 资格线（零 Core 依赖 / 边界纪律）
```

最近一次结果：**PASS 56　FAIL 0　SKIP 0**。

### ★vendor SDK 漂移事故（已修，已加锁）★

2026-09-24 发现：仓库 `sdk/anp_adapter/errors.py` 被更新（删掉零调用的死方法
`from_response`），而 **`adapters/dsh/anp_adapter/errors.py` 仍是旧版**——
随 Adapter 交付出去的那份已经和源不一致了。这正是复审 H-9 担心的漂移。

- **已修**：重拷 vendor，七个文件逐一哈希同源。
- **已加锁**：验收新增 **H5**，比较**仓库里的 vendor** 与 `sdk/` 逐文件哈希，漂移即红。
  （H4 只证明"装出来的那份存在"，而它每次现拷，恒真——查它等于没查。）

### ★关于这个数字，必须说清的话★

早先一轮报的 "PASS 43" **不构成合规证明**——独立复审确认其中多处为假证明或测错对象。
逐条修订如下：

| 问题 | 原状 | 现在 |
|---|---|---|
| **假证明** | `key_is_agent_not_plugin` 是**写死常量**，测试读它等于没测 | 改为真指纹材料；P0-4 由 **D2 接受 + I11 拒绝**这一对行为承载 |
| **假证明** | `C1` 读插件自算的 `two_tokens_distinct` | 拆成 C1a（**独立**：Core 名册里两身份并存）+ C1b（明确标注为自报，仅参考） |
| **测错对象** | F3–F7 走仓库 SDK 的 `AnpClient.resolve`，**从未**调用 `DshAdapter.resolve` | I5–I8 直接驱动**部署目录**里的 Adapter 本体 |
| **测错对象** | E1–E3 验的是 `_reply_ack()` 的固定模板，`reply()`（真反向链出口）零调用 | I1–I4 真调 `reply()`，并断言正文**不是**回执模板 |
| **静默消失** | D7/D8、E2/E3 嵌在 `if` 里，父项失败则子项**连计数都不进**，分母浮动 | 一律执行，父项失败即如实 FAIL |
| **白送绿票** | G5 重复断言 E1 的变量 | 已删 |
| **假恢复** | G3 只断言文件存在，容易被读成"状态全丢可恢复" | 标签改为"仅证明**进程重启**可续约" |
| **危险清理** | 退出清理带 `plugin.py` 通配，会命中**任何**同机 `python plugin.py`（本仓 `examples/`、`adapters/mimo/` 都是） | 只认两个本实例专有判据：插件绝对路径、本次随机端口 |

**这个数字仍应按"打对折"读**：浅层 smoke（HTTP 200 类）与条件项依然占一部分。
凡是被降级、被标注为"自报/仅参考"的项，都写在结果文字里。

单独验"零 Core 依赖"（把 `src/` 藏起来，Adapter 仍应 import 成功）：

```powershell
Rename-Item src _src_hidden
python -c "import importlib.util,sys;from pathlib import Path;H=Path('adapters/dsh');sys.path.insert(0,str(H));s=importlib.util.spec_from_file_location('p',H/'plugin.py');m=importlib.util.module_from_spec(s);s.loader.exec_module(m);print('ok')"
Rename-Item _src_hidden src
```

## 刻意没做的事

- **没有改 Core、没有改 Adapter Spec、没有改 SDK**。踩到的问题按纪律归类记录，
  只有确认是规范本身错误才另开变更。
- **没有为兼容全部 delivery mode 而全实现**。只做 `http` 一档——`queue` 是合法
  终态，但不是本平台需要的档。
- **没有把平台语义塞进 Core**。DSH 的工作区标题、会话 ID、别名全部留在 Adapter。

## 已知缺口（记录，不外扩）

| 类 | 缺口 |
|---|---|
| **Spec ↔ SKILL 冲突** | SKILL 的必测⑤⑥ 要求 Adapter 用 `POST /session/roster` + `POST /session/bind`，且 `references/验收与缺口.md:28` 明写「**⑤/⑥ 的判定不要自己实现**——Core 盖 `meta.session` 章，你照章执行」。但 **Spec §6.1 冻结的 canonical 6 API 里没有 session 面**（grep 零命中），而 `anp_hub.py:1177/1194` 又确实实现了这两个端点。**照 SKILL 修 ⑤⑥，本 Adapter 的 H3「只用 canonical API」检查就会红。** 这是冻结契约与 SKILL 的分叉，需要规范层裁决，Adapter 不擅自扩 API。**本 Adapter 当前`SessionBook` 自作会话决策，属于对 SKILL 该条的偏离，未修。** |
| **SKILL ⑦ ↔ P0-2 互斥** | SKILL 给的 ⑦ 测法是「杀掉 Adapter → **清掉它的本地缓存** → 重启 → 确认仍能收发」。但清掉缓存 = 丢掉 agent token，而 **P0-2 规定已有身份无密钥一律 401、绝不重发**，Core 也无删除/重铸身份的管理端点。所以"状态全丢后从 Core 恢复"在 Plugin 形态下**不可满足**。本 Adapter 的 G 段因此只能证明**进程重启**恢复（这是它真能证明的），差距如实标注而非粉饰。 |
| **Core** | `POST /plugins/<id>/reload` 在插件刚被 unload 后返回 **409 且响应体为空**，看不出原因；改用 `unload` + `load` 两步才拿到可读结果。 |
| **Core** | 插件启动预算 10 秒且**超时报「端点不可达」**，指向错误方向（插件其实很健康，只是握手路径上被我们自己的回调堵住了）。本次靠 Adapter 侧异步化绕开，缺口仍在。 |
| **Core** | 没有"删除/重置某个身份"的管理端点。身份一旦铸造、令牌一丢，P0-2 会让它**永远无法恢复**，只能换名字（等于换人）或换 home。对真实部署这是硬约束，值得在 Spec 里写明运维路径。 |
| **Core** | `install_roots()` 不含 `<home>/plugins/` → 真实部署下 Adapter Plugin 的 `exec` 唤醒一律被信任校验拒绝（本 Adapter 已用 `http` 绕开）。 |
| **Spec** | 没约定 adapter 的平台状态该放哪（本 Adapter 按 SKILL 放在插件自己的 `data/`）。 |
| **SDK** | `PluginServer.start()` 在**内部**定义并绑定 Handler 类，外部无法在不重写整段的情况下加一条 GET 路径。想让 Adapter 暴露自己的只读状态面，只能落盘或自建 server。 |
| **Adapter（未修）** | 必测⑤ 无断言；F8/F9 两条都标 ⑥（名实不符）。 |
| **Adapter（已修）** | `SessionBook.route` 已加 `RLock`；`env.id` 认领/释放幂等（`mark_seen`/`unmark_seen`，失败释放允许重投）；`on_wake` 对 `deliver_to_platform` 有 try/except。 |
| **Adapter（未修）** | 负向用例仍缺：非法 `mode`、仅 `{"ok":true}` 不签收、`register=allowlist`、admin 令牌续约被拒。 |
| **Adapter（未修）** | `caps` 广告了 `inject_session` / `resume_session`，但实际只有"落盘待办"，没有把信推进运行中会话的通道——**广告强于实现**。 |
| **Adapter** | `/anp/wake` 收到信后发的是"已进平台"的**收执**；DSH 侧真正的产出由 `reply()` 发回。当前没有把待办推给运行中的 DSH 会话的常驻通道（靠 DSH 侧读 `data/dsh-pending.jsonl` 消费）。 |
| **Adapter（未修）** | `verify.py` 不覆盖 `_run_headless`（它发的信 `need_reply` 都缺省 → 只落 `notice`），所以"平台真的被叫起来"这条在验收里**没有门禁**。2026-10-12 的 CLI 形态变更就是在全绿期间发生的。补测是手工驱动安装目录的 Adapter 本体，未并入 verify.py。 |
| **Adapter（未修）** | `dsh-work.json` 里的 `failed` 记录 `retryable` 写死 `False`（失败已发补偿回信，不在同一进程自动重试）。2026-10-08 有一条 CLI 找不到的 `failed` 会**永远停在那里**，需人工决定是否重置。 |

### 已修的 P0（复审 CR-1）

`on_wake` 原判据是 `if self.agent_key and got != self.agent_key:` —— 期望键为空时
**整个条件为假**，于是代管注册尚未完成的窗口里，任意/空 `x-mailbox-key` 都会放行并
回 `ok:true` + `mode:session`，**Core 会当成已签收**（同时破 P0-4 与"禁止代 Core 签收"）。

现在改为默认拒绝（键空 / 头空 / 不等 → `ok:false`），并由 **I9/I10 两条确定性断言**钉死
（直接把 Adapter 的 `agent_key` 置空再调 `on_wake`，不靠抢时序）。

## 与 SDK / Spec 的关系

- 权威契约在 `docs/ANP-ADAPTER-SPEC.md`（v0.1 已冻结）；SDK 只是便捷层，
  两者冲突时**以 Spec 为准、改 SDK 不改 Spec**。
- 本 Adapter 只用 canonical 6 个 Core API 中的 3 个：
  `POST /heartbeat`、`POST /messages/send`、`GET /agents`（外加 SDK 的可选
  `resolve()`，它只读 `/agents`，不是第 7 个 API）。
- 与 Core 之间**只有 HTTP**，`src/` 藏起来也能跑（验收 H1 静态扫描 + 上面的手工验证）。
