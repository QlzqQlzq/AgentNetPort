# Core / Windows 夹具侧 · 三症状取证报告

> 原则：**只观测不改码**。每条给出：现象、真实根因、以及 Core bug / 测试夹具 bug /
> Windows 时序 的判定 + 证据。
> 证据脚本：`tests/diag_three_symptoms.py`（可重跑）。

---

## 结论摘要

| 症状 | 判定 | 一句话 |
|---|---|---|
| ① `keys.json` _tighten | **Core 正确，非 bug** | Windows 走 icacls 分支，实测已去继承、只留本人；报告的 "Users" 命中文本是误读（下面 ①.3） |
| ② `plugin_load` RemoteDisconnected | **非 bug，已在此前修复** | 现在回结构化 `endpoint.unreachable`；锁也不再被出站 HTTP 占（并行请求 3ms） |
| ③ `heartbeat` 偶发断连 | **非 bug，是夹具/P0-2 语义** | 401 全部来自"已知身份不带 key"，正是 P0-2 要求的拒绝；无一次传输层断连 |

**三条都不是 Core bug，也不需要改 Core。**

---

## ① `keys.json` _tighten

### 实测（Windows 11，Python 3.13）

```
keys.json 存在：True
os.name = nt → _tighten 走 icacls 分支
目标行的 ACE：['EXAMPLE-PC\example-user:(F)']
★继承 ACE 残留：无★
★非本人 ACE：无★
★收紧后仍能读回：True ★
hub.secret 的 ACE：['EXAMPLE-PC\example-user:(F)']
admin.token 的 ACE：['EXAMPLE-PC\example-user:(F)']
```

### 判定：**收紧生效，不是 bug**

而且这里确实有一个**真的 Windows 坑，已修**（2026-09-24）：早期 `_tighten`
只 `grant {USERNAME}:F`，而 `USERNAME` 往往不是 icacls 认的账号名
（要 `DOMAIN\user` 或 SID）。于是 `/inheritance:r` 剥了继承、grant 又**静默失败**，
文件就对**包括 hub 自己在内的所有人**不可读写——之后每次
`write_text`/`read_text` 都是 PermissionError（keys.json / plugins.json /
admin.token / hub.secret 都踩过，`write_registry` 因此还加过 unlink 重建的兜底）。

现在的 `_tighten`：用 `whoami` 拿权威账号名 → 剥继承 → **显式**授予 Full →
失败则 `/inheritance:e` 回滚。**宁可少收紧，也不锁死自己。**

（回滚这条正是"keys.json _tighten"作为症状上报的可能来由：收紧反而让
后续写不进去。）

三个敏感文件（`keys.json` / `hub.secret` / `admin.token`）的 ACL 都是
`<当前用户>:(F)`，且 **没有任何继承条目**——即家用机默认会出现的
`BUILTIN\Users:(RX)` / `Everyone:(RX)` 都没有。

### ①.3 报告里 "Users" 命中的真相 —— **我自己的取证脚本误报**

第一版脚本把 **icacls 的整段输出**（含中文 Windows 的本地化总结行，
如"成功处理了 1 个文件"）拿去做子串匹配，把那些无关文本里的 "Users"
当成了 ACE 主体，于是得出"收紧没生效"的假结论。

修正后的取证（`icacls_own_lines`）**只取含目标路径那一行、切掉路径本身**，
得到干净的 `['EXAMPLE-PC\example-user:(F)']`。同时补了一条更重要的断言：
**收紧之后仍能读回**——剥了继承却 grant 失败 = 把自己锁死，
这是 `_tighten` 类代码最危险的失败模式，比"没收紧"严重得多。

**教训**：断言 ACL 只认「目标行切路径后的 ACE 列表」，绝不整段做子串匹配；
并且一定要同时验"收紧后自己还读写得动"。

---

## ② `plugin_load` RemoteDisconnected

### 实测

```
load dead.one#0：409 2322ms endpoint.unreachable
load dead.one#1：409 2242ms endpoint.unreachable
load dead.one#2：409 2219ms endpoint.unreachable
load broke.one：409 manifest.invalid
hub 仍活着：True
load 期间并行 GET /agents 耗时：3ms
```

### 判定：**不是 bug；症状已在 2026-09-24 的 panel 修复中一并解决**

当年 `RemoteDisconnected` 的根因是：`plugin_load` 把 `register_out` 挪到
`PLUGIN_LOCK` 外时**忘了把 `except ManifestError` 一起搬过去**——异常冒到
HTTP handler，客户端只看到连接被断。已修（锁外那步现在包了同样的 except，
回结构化 `endpoint.unreachable`）。

上面的实测印证三点：

1. **不可达插件 → 409 + `endpoint.unreachable`**（有 code、有 error、有 hint），
   不再是裸连接断开；
2. **hub 不死**（连续 4 次坏 load 之后 `poll() is None` 仍为 True）；
3. **锁没被出站 HTTP 占**：load 进行时并行 `GET /agents` 只要 3ms。
   这正是 `register_out` 移到锁外的效果——修之前这一步会等满 10 秒。

### 附：2.2 秒的来源

`dead.one` 指向 `http://127.0.0.1:1/`，连接被拒是**立刻**失败的，
所以 2.2s 不是连接耗时，而是 `register_out` 的重试退避
（`delay = 0.05` 起、`min(delay*2, 0.5)` 封顶，累积约 2 秒后到 deadline 附近）。

这是**有意的**：插件要 `import` + `bind` 才真的 Listening，不等就会拼运气。
对 command 型插件这个等待是必要的；对 url 型插件（本就该已在运行）
略微浪费，但代价只有 2 秒且只在失败路径上，**不值得为它加分支**。

---

## ③ `heartbeat` 偶发断连

### 实测

```
首注册：200 key=有
120 次续约：{'200': 120}
延迟：中位 28.6ms 最大 55.2ms
8 线程 ×15 首注册：{'200': 8, '401': 112}      ← 见下
并发延迟：中位 6.6ms 最大 587.5ms
冷启动 /health 首次可响应：418ms
冷启动后立刻 20 次 heartbeat：{'200': 1, '401': 19}
```

### 判定：**零传输层失败；所有非 200 都是 P0-2 的正确拒绝**

| 观察 | 真相 |
|---|---|
| 120 次**续约**（带正确 key） | **120/120 = 200**，最大延迟 55ms。没有一次断连。 |
| 8 线程并发 | 后来单独复现 **40/40 全 200**（5 轮 × 8 线程）。diag 里那 112 次 401 是夹具 bug（见 ③.2） |
| 冷启动后 19 次 401 | **P0-2 的正确行为**，见 ③.1 |
| 延迟中位 28.6ms | ThreadingHTTPServer 每次请求建/销连接 + `msvcrt.locking` 落盘。Windows 本地回环的正常量级 |

**没有任何一次 `URLError` / `RemoteDisconnected` / `ConnectionResetError`。**
"偶发断连"这个症状本身不成立。

### ③.1 冷启动那 19 次 401 是 P0-2，而且**正是它该做的事**

diag 的冷启动段复用了**同一个 home**（`p.kill()` 后没换 home 就起新 hub），
而 `flap.one` 在旧 hub 上已经注册过 → 新 hub 里 `identity_known("flap.one")` 为真
→ 请求不带 key → `_auth` 拒绝 → 401：

```json
{"ok": false, "error": "flap.one 没带密钥（x-mailbox-key 头）…"}
```

这是 P0-2 的**设计目标**：已有身份必须拿对密钥才能续约，绝不重发令牌。
否则任何本机进程知道名字就能顶掉别人的唤醒端点。

**所以这 19 次 401 是安全不变量在正常工作**，不是缺陷。

### ③.2 diag 里 8 线程 112 次 401：夹具把同一个 `home` 用在了两个 hub 上

diag 的并发段其实跑在**前面那个已经被 `flap.one` 续约过 120 次的同一 home**上。
worker 注册的名字是 `c{n}.one`（各不相同，本该都是首注册），
但 diag 同时还在同一个 `Probe` 上跑过热身注册——真正的原因现已定性为
**夹具复用了同一 home**（`Probe.kill()` 之后没有换 home 就重启了 hub，
于是历史身份的 `identity_known` 为真、请求不带 key → 401）。

**直接验证**：另写复现脚本，8 线程 × 5 轮 × 各自新名字、全新 home：

```
轮0: 全 200
轮1: 全 200
轮2: 全 200
轮3: 全 200
轮4: 全 200
合计: {200: 40}
```

**40/40 全 200。** 所以那 112 次 401 来自夹具的 home 复用，不是 Core 行为。

**这条给夹具的教训**：测"首注册"必须配一个**全新的 home**；
一旦复用过 home，"首注册"就 silently 变成"已知身份无 key"，
症状看起来像随机 401，其实是确定性 401。

---

## ④ Windows 时序：确有一条真实存在，但与三症状无关

`serve_forever` 现在**先于**插件预加载启动（panel 修复的一部分），
所以 `/health` 可响应的时刻（实测 418ms）**不等于**插件/账本就绪。
这点已用 `/health.preload_done` 显式暴露，测试改用 `wait_plugins()` 轮询。

如果夹具里还有"`wait()` 一返回就去断言插件工具/面板"的写法，
在 Windows 上会**偶发**失败（取决于预加载快于还是慢于断言）。
**建议**：把所有 `h.wait()` 之后立刻断言插件状态的用例，改成
`h.wait_plugins()`。`tests/selftest_plugins.py` 的 D0b/G1b/H0b/H7b 已改。

---

## 5. 不需要改 Core

三条症状都不需要动 Core：

- `_tighten` 在 Windows 上有效，无需改；
- `plugin_load` 已回结构化错误，无需改；
- `heartbeat` 的 401 是 P0-2 的正确执行，**不能**为了"让测试绿"而放宽
  （那会打开顶替他人身份的口子）。

真正建议改的是**夹具**：
① icacls 断言只解析"含目标路径的那一行"；
② 断言插件状态前用 `wait_plugins()`，不要用 `wait() + sleep` 猜；
③ 造"已知身份"场景时明确复用同一个 home，并**预期** 401。
