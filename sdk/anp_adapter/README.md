# ANP Adapter SDK

让一个不了解 AgentNetPort 内部实现的第三方，能写出自己的 Adapter。

```
Adapter Spec v0.1（docs/ANP-ADAPTER-SPEC.md）
        ↓
Adapter SDK（本目录）
        ↓
Reference Adapter（examples/adapter_demo/、examples/adapter_plugin/）
        ↓
Third-party Adapter
```

**SDK 只做便捷层，不新增协议语义。** 权威契约在 Spec 里；SDK 的实现必须与它一致，
不一致时改 SDK，不改 Spec。

---

## 装/带

SDK 是**随 Adapter 交付的库**，不是一个需要安装的服务。三种用法：

```powershell
# ① 仓库里原地跑（examples/ 的做法）
sys.path.insert(0, "<repo>/sdk")

# ② vendor 进 Adapter 自己的目录（部署形态，Adapter Plugin 必须这样）
#    把 sdk/anp_adapter/ 复制到你的插件/进程目录，然后
sys.path.insert(0, str(Path(__file__).resolve().parent))

# ③ 装进站点目录（pip install / PYTHONPATH）
import anp_adapter
```

> ★部署时要注意★：Adapter Plugin 由 Core 从 `<home>/plugins/<id>/` 拉起，
> 那时"仓库根的 ../sdk"不在路径里。**必须 vendor 或安装**，
> 否则插件起来就 `ModuleNotFoundError` 秒退——
> 而 Core 只会报「插件端点不可达」，非常难查。
> `examples/adapter_plugin/plugin.py` 的 `_find_sdk()` 演示了三种位置的查找顺序。

**零依赖**：只用 Python 标准库。不 import 任何 `src/` 下的 Core 模块——
Adapter 与 Core 之间只有 HTTP 协议，没有代码依赖。

---

## 模块

| 模块 | 给你什么 | 什么时候用 |
|---|---|---|
| `client.py` | `AnpClient` + 6 个 Core API 的薄封装（+ `resolve()` 便利方法） | 总要 |
| `envelope.py` | `Envelope`（只读字段视图）+ `from_wake_payload` | 总要 |
| `transport.py` | `WakeTransport` 与 `HttpWake` / `ExecWake` / `QueueWake` | 总要 |
| `plugin.py` | `PluginServer` + `RegisterResult` | 写 Adapter Plugin 时 |
| `resolve.py` | 模糊目标 → 候选 → canonical Agent ID（**可选**，无 AI） | 想支持"发给负责 Live2D 的"时 |
| `errors.py` | `AnpError`（transport / auth / policy / rejected 四类） | 想要结构化错误时 |

---

## 三个最小用法

### ① 普通进程形态（`examples/adapter_demo/` 就是这么写的）

```python
from anp_adapter import AnpClient, HttpWake, WakeTransport

t = HttpWake()
t.start(0)                      # ★先起服务：declare_wake 要用端口★
tok = AnpClient("http://127.0.0.1:8900").bootstrap()   # 两个身份、两枚令牌自动办好
```

★`Adapter` / `DemoPlatform` 这两个类在 example 里、**不在 SDK 里**★（README 原来把它们
写成 SDK 导出，照抄会直接 ImportError）。SDK 只给原语：`AnpClient` /
`HttpWake|ExecWake|QueueWake` / `WakeTransport` / `PluginServer` / `Envelope`。
"怎么把某个平台接上来"是每个 adapter 自己的适配逻辑，不是通用件。

**这份 README 的 import 是可执行的**：`tests/selftest_spec_conformance.py` 有一条
会把本文里所有 `from anp_adapter import X` 当真 import 一遍。改 README 时假如编了个
不存在的名字，测试会当场红。

### ② Adapter Plugin 形态（`examples/adapter_plugin/`）

```python
from anp_adapter import PluginServer, RegisterResult

def on_register(res: RegisterResult):
    # res.identity / res.token / res.hub_base —— Core 送来的凭证
    res.client().bootstrap("my-agent-01",
                           wake={"kind": "http",
                                 "url": f"http://127.0.0.1:{PORT}/anp/wake"})

def on_wake(env, side):         # env: Envelope
    ...翻译 → 递平台 → 回收回信...

PluginServer(on_register=on_register, on_wake=on_wake).serve_forever()
```

### ③ 只当 HTTP 客户端用

```python
from anp_adapter import AnpClient

c = AnpClient("http://127.0.0.1:8900", token, "my-agent")
r = c.bootstrap("my-agent")                  # 首次注册，拿令牌
r = c.send("peer", "你好", correlation_id="x:1")
r = c.receive(peek=True, unread_only=True)   # 带完整状态字段
r = c.query("x:1")                           # 按对话线查
```

### ④ 模糊寻址：`resolve()`（可选）

发信方常常不知道对方的 canonical ID，只知道"负责 Live2D 的那个"。
`resolve()` 把这种目标折成 canonical Agent ID —— **它只调 `GET /agents`，
匹配全在本地用普通字符串算法完成，不调用任何模型**：

```python
from anp_adapter import AnpClient, LocalDirectory, shape

c = AnpClient("http://127.0.0.1:8900", token, "my-adapter")
local = LocalDirectory()                     # ★只有 Adapter 知道的平台信息★
local.add("demo-live2d-a", aliases=["立绘"], handle="dsh-11451",
          workspace="Live2D 研究", tags=["art"])

r = c.resolve("负责 Live2D 的", hints=local.hints())
# r.status == "AMBIGUOUS" → r["candidates"] 是候选表，交给上层/用户再指一次
# r.status == "RESOLVED"  → r.target 就是可以发信的那个 id
if r.status == "RESOLVED":
    c.send(r.target, "你好")                  # ★解析成功 ≠ 投递成功★，两段分开看
```

四个出口：`RESOLVED` / `AMBIGUOUS` / `NOT_FOUND` / `INVALID_TARGET`
（外加 `DIRECTORY_UNAVAILABLE` 与四个保留状态，见 `resolve.py` 顶部）。
判定规则与边界写在 Spec §8.5；`shape(r)` 把它收成可以直接回给上层的形状。

#### ★`last_seen` ≠ `last_used`（Spec §8.6）★

| 信号 | 从哪来 | 语义 | 能不能破并列 |
|---|---|---|---|
| `last_seen` | Core 名册 `/agents`（心跳/续约的账本时间） | 最近**活跃**（它还活着） | ❌ 默认不能，只用于排序 |
| `last_used` | **Adapter 自己记**（平台侧"最近真的被用过"） | 最近**被用过** | ✅ 并列且差距够大时可以 |

`/agents` **给不出** `last_used`（Core 的 `delivered_at` / `read_ts` 不在名册里）。
所以想要"最近用过"这条更强的信号，就在 Adapter 侧记一笔：

```python
local.add("demo-live2d-a", last_used=time.time())      # 平台记录 → source=platform
a = c.resolve("Live2D", hints=local.hints())
# 全都记过 + 差距够大 → RESOLVED，decided_by="recency.platform"
# 只有心跳          → AMBIGUOUS（不猜）；要退而求其次得显式传 recency_allow_seen=True，
#                     那时 decided_by="recency.heartbeat"，理由也写"最近活跃"而不是"用过"
```

候选视图里 `last_used` / `last_used_source` / `last_seen` 三个字段都在，来源看得见。
「只有一部分候选有 `last_used`」也不许用来破并列——那会选出"唯一有记录的那个"，
不是"最近的那个"。

---

## SDK 不管什么（这些永远在你的 Adapter 里）

| 不管 | 为什么 |
|---|---|
| **平台语义** | `to_native` / `from_native` 怎么翻译，是你的平台知识 |
| **会话怎么注入** | 平台怎么把一段文字塞进会话，SDK 一无所知 |
| **会话怎么创建/恢复** | 同上 |
| **Agent 怎么回复** | 回复内容是 Agent 的决定 |
| **投递决策** | 重试、退避、谁在线——全是 Core 的事（Spec §1「禁止做」第一条） |
| **要不要支持模糊寻址** | `resolve.py` 是可选库：不 import 它，你就只支持精确 ID，一样合规 |
| **业务逻辑** | 不替你做任何决定 |

对照 Spec §12.1 的两种形态：SDK 对两者都适用，差别只在身份怎么来、谁拉起进程。

---

## 一个陷阱：`wake.kind=http` 只在**失败**时出现

```jsonc
// 成功（http 档）
"wake": {"ok": true, "status": 200, "to": "demo-01", ...}
// 失败才有 kind
"wake": {"ok": false, "kind": "exec", "error": "唤醒配置未通过信任校验…"}
```

所以**不要用 `wake.kind` 判断是否成功**，用 `wake.ok`。这条在 #6 排查时踩过。

---

## 自检

```powershell
python examples/adapter_demo/verify.py        # 61 项：零内部依赖 + 三 mode E2E + 寻址(E 段)
python examples/adapter_plugin/experiment.py  # 18 项：Adapter-as-Plugin 原型
python tests/selftest_spec_conformance.py     # 59 项：规范与代码不许分家（含 R5xx SDK 资格线）
```

R5xx 是专门盯 SDK 的：不许 import Core、只依赖标准库、6 个 Core API 覆盖全、
参考实现确实改用 SDK 而不是继续手抄协议面。

---

## 版本

`0.1.0`（2026-09-22，随 Adapter Spec v0.1 一起冻结）。
协议不变就不改 API；要改就升 `api_level` 并在 Spec 里记一笔。

`resolve.py` 是 2026-09-23 随 Spec §8.5 补进来的**可选**模块：
它不新增 Core API、不新增协议语义，只在 Adapter 侧回答"用户说的那个谁是谁"。
