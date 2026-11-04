# Agent Discovery / Address Resolution —— 工程检查与架构判断

日期：2026-09-23　范围：AgentNetPort（`<ANP_ROOT>`）
结论一句话：**Core 已经提供了这件事所需的全部基础能力，缺口只在 Adapter/SDK 侧；
本次没有给 Core 加任何 API、任何字段、任何语义。**

后续修订：

**第一轮（2026-09-23，按主人反馈）**

- **§4.1 语义查清**：`last_seen`（心跳活跃度）与 `last_used`（平台记录）彻底分开，
  默认不许互相顶替 → 规范新增 **Spec §8.6**，代码与 E 段验收同步。
- **§5 决策登记**：`agents.discover` 死权限 → 规范新增 **Spec §8.7**（登记为独立决策项，
  本次不改）。
- **§4.2 措辞修正**：不再用"回归全绿"这种盖章式说法。

**第二轮（2026-09-23，主人指出"并行修改"）**

- **§4.2 重写**：只有**本轮改动范围内**的证据（demo verify / conformance / mimo verify）
  才作为证据；完整回归的数字降级为"漂移记录"，**不支撑任何结论**。
- **§4.3 新增**：把并行修改的事实（`selftest_plugins` 99/0 → 94/5 → 97/4，
  experiment 18 → 23 项）写成前提，而不是脚注。
- **§7 新增**：**R0 门禁未跑**——等并行修改收尾后重跑一次干净的；并留下交付物指纹、
  Discovery 依赖的 Core 面、以及"R0 要回答的是自洽而不是功能有无"的四行证据链。
- 本轮**不再扩 Discovery**、不动 `agents.discover`、不因为并行窗口里的数字改任何东西。

---

## 1. 先检查，再动手（任务书 §11 的五步）

| 步骤 | 做了 | 结论 |
|---|---|---|
| 读 Protocol / Plugin Spec / Adapter Spec | `docs/ANP-PROTOCOL.md` §2 寻址、`docs/ANP-ADAPTER-SPEC.md`（§6 八个接口 / §6.3 硬边界 / §8 caps 防腐 / §12 缺口表）、`docs/ANP-PLUGIN-SPEC.md` §2 | 规范的寻址模型是 `to = agent 身份名`；**没有任何一节讲"怎么找到这个名字"**，这是真实缺口 |
| 查 `GET /agents` | `src/anp_hub.py:71 agent_view()` → `{"ok":true,"agents":[…]}`，每行 `name / wake / process_api / caps / skills / description / last_seen / messages / unread` | **够用**：Description 与 Skills 是身份自报自由词表，免令牌。★`last_seen` 是**心跳/续约**的账本时间，不是"最近用过"——见 §4.1★ |
| 查 Agent metadata | `src/anp_core.py:176 STICKY_FIELDS = (wake, caps, skills, process_api, description, workspace, client)`、`heartbeat()`（§3.4 sticky 三态） | 名册里**没有** alias / tags / 平台工作区标题——但 `description/skills/caps` 都是**自由词表**，Core 只存只返回不解释 |
| 查 canonical Agent ID | `core.recipients_of()`（`anp_core.py:422`）+ `hub.do_send()`（`anp_hub.py:162`） | canonical ID **就是身份名**（`codex` / `dsh-11451` / `demo-01`），`to` 只按字面取用——**Core 确实不做任何解析** |
| 查 SDK / Reference Adapter | `sdk/anp_adapter/{client,envelope,transport,plugin,errors}.py`、`examples/adapter_demo/adapter.py`、`adapters/mimo/plugin.py` | SDK 只有 6 个 Core API 的薄封装，**没有任何寻址能力**；Demo 与 MiMo 都只支持精确 ID |

仓库内**没有**任何 fuzzy / resolve / alias 的既有实现（全仓 grep `resolve|fuzzy|alias|模糊` 只命中文档里的
"工作区解析"与 Python 标准库的 `Path.resolve`）。

### 现有能力的实测证据（不是读代码猜的）

```
$ curl GET /agents                  → 免令牌，含 name/description/skills/caps/last_seen
$ POST /messages/send {"to":"负责 Live2D 的"}
  → ok=true，名册里凭空多出一个身份「负责Live2D的」（clean_text 只规范空白）
  → demo-live2d-a 的收件箱一封都没多
```

第二行就是**Core 边界的直接证据**：Core 把自然语言目标当成一个字面身份名，
既不报错也不转译——它压根不认识"负责 Live2D 的"这种说法。

---

## 2. 架构判断

### 2.1 能力归属

```
Core        ── 公开名册（GET /agents）+ canonical ID + 校验 + 投递
Adapter     ── 模糊目标 → 候选 → canonical ID（平台语义、平台提示、排序、消歧）
```

**结论：不该给 Core 加 `/resolve` 端点。** 三条理由：

1. `/agents` 已经把解析所需的公开信息给全了（§1 实测）。加端点等于让 Core 参与
   "谁更像 Live2D 的负责人"这种判断——这正是 Spec §8 那条防腐规则要防的
   （"不写明，几年后 `if caps["resume_session"]:` 就会长进 Core"）。
2. 平台语义（工作区标题、平台短名、别名）**Core 不该保存**。任务书 §7/§8 与
   Protocol §2 一致：Workspace / Session 是 Adapter 内部对象。
3. **Adapter 可以完全不支持模糊解析**（任务书 §13）。能力放在 Core 就变成
   "人人都有、人人都得维护"；放在 SDK 里则是"想用就用"。

### 2.2 唯一真正缺失的东西

**一个 Adapter 侧、零模型依赖的"候选 → 消歧"实现。** 其余全是现成的。

### 2.3 两个必须写死的语义（否则功能会变成事故）

> **① `last_used` 只参与排序，且只有在候选本来就不相上下、并列者都有同一来源的
> `last_used`、且领先者确实新得多时才允许据此收窄；否则一律 `AMBIGUOUS`。**

否则"Live2D"会被"谁最近动过"悄悄改写成一个猜测——任务书 §4.2 明令禁止。

> **② `last_seen`（心跳活跃度）不许冒充 `last_used`（最近被用过）。**
> Adapter 没有平台记录时，默认**不**用活跃度破并列；要用必须显式打开
> （`recency_allow_seen=True`），且结论标明 `recency.heartbeat`、措辞只说"最近活跃"。

`/agents` 给不出 `last_used`（Core 的 `delivered_at`/`read_ts`/`read_by` 不在名册里），
所以"最近用过"要么 Adapter 自己记，要么明确接受更弱的信号。详见 Spec §8.6。

---

## 3. 改了什么（Core 零改动）

| 文件 | 改动 | 性质 |
|---|---|---|
| `sdk/anp_adapter/resolve.py` | **新增**。`build_dir` / `LocalDirectory` / `resolve` / `shape`；状态 `RESOLVED/AMBIGUOUS/NOT_FOUND/INVALID_TARGET/DIRECTORY_UNAVAILABLE` + 四个保留态；`last_seen` 与 `last_used` 分开（`Candidate.recency` + `last_used_source`） | 只依赖标准库，不 import Core，不新增 Core API |
| `sdk/anp_adapter/client.py` | 加 `AnpClient.resolve(target, hints=None)` | 薄便利：**只调已有的 `GET /agents`** |
| `sdk/anp_adapter/__init__.py` | 导出 resolve 面 | — |
| `examples/adapter_demo/adapter.py` | 加 `AdapterDirectory`（第 2.5 层）、`Adapter.resolve/send_fuzzy/register_peers/peer_client`、CLI `--resolve/--to/--hints`；`register_peers` 记录平台侧 `last_used` | 参考实现演示"Adapter 侧怎么找人" |
| `examples/adapter_demo/verify.py` | 加 **E 段**（31 项，含 `last_seen`/`last_used` 分流验收） | 验收 |
| `docs/ANP-ADAPTER-SPEC.md` | 加 **§8.5 寻址**、**§8.6 时间信号**、**§8.7 权限待决项**；自检清单与缺口表更新 | 规范 |
| `sdk/anp_adapter/README.md`、`examples/adapter_demo/README.md`、`README.md` | 用法与测试数 | 文档 |

**`src/` 一行未改。** 没有新端点、没有新字段、没有新权限、没有新环境变量。

### 匹配算法（公开、可解释、无 AI）

```
① 归一化精确命中 Agent ID                                  → RESOLVED(exact)
② 逐字段命中：name / alias / 平台短名 / skills / caps /
   description / tags / client / 平台工作区标题
   命中强度 raw（门槛用） + 区分力加权 weighted（比较用）
③ 多词查询要求词元覆盖率 ≥ 一半（"live2d 素材"必须中 2 个词）
④ raw < 门槛                                              → NOT_FOUND
⑤ 只有它够格 / weighted 领先 ≥ margin 倍                   → RESOLVED(unique|margin)
⑥ 前两名不相上下 + 领先者明显更新（差距与倍数双门槛）        → RESOLVED(recency)
⑦ 其余                                                    → AMBIGUOUS + candidates
```

- **区分力加权**解决"所有候选都声明了 live2d"这种命中不算证据的问题；
- **时间戳异常（未来时间）不作证据**——实测踩到过：时钟偏差让"差距不足 30 秒"
  也判成 recency，三个 20 毫秒内登记的候选被强行收窄成一个；
- 候选带 `reasons`（哪个字段命中了哪个词），结论可解释、可审计。

---

## 4. 验收（可复跑）

```powershell
python examples/adapter_demo/verify.py     # 61 通过 / 0 失败（A7 B9 C9 E31 D5）
```

E 段逐条对应任务书 §12：

| 任务书要求 | 验收项 | 实测 |
|---|---|---|
| 精确 | E1 / E1b | canonical ID（含大小写、分隔符变体）→ RESOLVED(exact) |
| 模糊唯一 | E2 | 「做 Minecraft 的」→ RESOLVED demo-mc-01 |
| 模糊多候选 | E3 / E3b / E3c / E3d | 「Live2D」→ AMBIGUOUS，3 个候选，**无 target**，候选带证据 |
| 无结果 | E4 / E4b | 不存在的目标 → NOT_FOUND；空目标 → INVALID_TARGET |
| 最近使用 | E5a–E5k | 见 §4.1（这一条被拆成"两个信号"逐项验） |
| Core 边界 | E6–E6f | Core 把「负责 Live2D 的」当字面身份名；canonical ID 才投得到；解析失败时 `send` 为 None |
| 无 AI | E7 / E7b / E7c | `resolve.py` 只 import `re/time/math/json/pathlib`；无 API key / 无 http 客户端 / 无 embedding |

### 4.1 "最近使用"这一条的最终语义（`last_seen` ≠ `last_used`）

见 Spec §8.6。E 段现在把两个信号分开验，不再混为一谈：

```
E5a/E5b  名册给的是 last_seen（心跳活跃度），能认出谁最近活跃        ← 事实
E5c      ★只有心跳活跃度 → 默认 AMBIGUOUS，不许替用户挑人★
E5d      候选视图分开回报 last_used / last_used_source / last_seen
E5e/E5f  有平台 last_used 且差距够大 → RESOLVED，decided_by=recency.platform
E5g      结论写明信号来源
E5h      ★撤掉平台记录 → 又变回 AMBIGUOUS★（证明结论来自 last_used 而非心跳）
E5i      退而求其次必须显式打开 recency_allow_seen → decided_by=recency.heartbeat
E5j/E5k  两条路 decided_by 不同名；心跳路的措辞只敢说"最近活跃"
```

`/agents` 的 `last_seen` 查清了是**心跳/续约的账本时间**（`core.agent_table()[name]["ts"]`，
`core.online()` 用的同一个值），**不是"被使用"**；Core 里真正代表"被使用"的
`delivered_at` / `read_ts` / `read_by` 不在名册里。所以：想要"最近用过"，Adapter 自己记
（`LocalDirectory.add(agent_id, last_used=…)`），否则就接受"只有活跃度"这个更弱的信号。

### 4.2 回归口径（★只把"本轮自己改动范围"当证据★）

**Discovery 这一支的证据（可以当证据用）**：

```
examples/adapter_demo/verify.py       61 通过 / 0 失败   ← 本次新增 E 段 31 项
tests/selftest_spec_conformance.py    59 通过 / 0 失败   ← 含 R5xx SDK 资格线
adapters/mimo/verify.py               28 通过 / 0 失败
```

这三项覆盖的都是本轮改动范围（`sdk/` + `examples/adapter_demo/` + 规范文本），
不依赖 `src/` 的在途改动。

**完整回归的数字在这里不构成依据（原因见 §7）**：

```
2026-09-23 16:51 快照（仅作漂移记录，不作门禁依据）
  tests/selftest.py                     26/26 通过
  tests/selftest_sessions.py            20/20 通过
  tests/selftest_regressions.py         21/21 通过
  tests/selftest_p00_queue.py           27 通过 / 0 失败
  tests/selftest_p09_api.py             23 通过 / 0 失败   （上一轮的红项已被别人修掉）
  tests/selftest_trustroot.py           20 通过 / 0 失败
  tests/selftest_plugins.py             97 通过 / 4 失败   ← 测试对象正在被改，数字每几分钟就变
```

> **措辞纪律**：只要完整回归里还有红项、或者被测对象正在被修改，就**不**说"全绿"，
> 也不拿这些数字支持任何架构结论。正确说法是上面两段：
> **本轮改动范围的证据全绿；全局状态待并行修改收尾后重跑。**

### 4.3 并行修改对"证据"的影响（必须写明的前提）

本轮工作期间，**同一仓库有其它会话在同时改 `src/anp_hub.py` / `tests/selftest_plugins.py` /
`examples/adapter_plugin/experiment.py`**。观察到的事实：

```
tests/selftest_plugins.py   99 通过 / 0 失败  →  94 通过 / 5 失败  →  97 通过 / 4 失败
examples/adapter_plugin/experiment.py  18 项  →  23 项
tests/selftest_p09_api.py   22 通过 / 1 失败  →  23 通过 / 0 失败
```

**同一命令在几分钟内给出不同数字，说明不是 flaky，而是被测对象在变。**
所以本轮：

- Discovery 只依赖 `sdk/` + `examples/adapter_demo/` + 文档，这几项**没有被别人改**（见 §7 指纹）；
- 任何"ANP 整体是否自洽"的判断**推迟到 R0**（§7）。

---

## 5. 三个判断（不扩大范围，但写明）

1. **`alias` 不进 Core。** 别名属于平台语义，Adapter 自己的 `LocalDirectory` 就够
   （`resolve.py` 的 `hints`）。真要让 Core 承载，就违反了 §2.1 那条边界。
2. **`workspace` / `client` 存在账本里但没进 `/agents`**（`anp_hub.py:71` 只挑了几个字段）。
   这是**对的**：它们跟"谁是谁"没关系，是 Adapter 的事。本次没有动它。
3. **`agents.discover` 是死权限** —— ✅ **已登记为独立决策项，Spec §8.7**。
   事实：`/agents` 免令牌可读，而 Plugin Spec §2.1 把 `agents.discover` 写成"调 `GET /agents`"。
   两个方向（A 公开目录 / B 真做权限）与倾向（**A**）都写在 §8.7；
   **在决定之前不改代码、不改权限闭集**，并以代码为准（公开）。

---

## 6. 顺手发现的一个既有红项（与本任务无关，**已被别人修掉**）

`tests/selftest_p09_api.py` 的 **4.1「不传 peek 就读了即签收」** 在本轮开始时是红的，
当时的独立复现：

```
bob 注册为 queue 档 → alice 发两封 wake=true 的信 → 两封都 ok=true
  wake = {"ok": false, "kind": "queue", "error": "收件人是 queue 档（仅落盘，等待它主动来取）"}
peek 读到：unread = 0，两封的 read=true、read_by=None
```

即 **queue 档的信一落盘就被标成 `read=true`**，于是"未读"永远为 0；
而测试注释写着"queue 档的信才是真的未读（P0-0 之后保持未读）"，两边矛盾。

**现状**：2026-09-23 16:5x 复跑为 **23 通过 / 0 失败**——该红项已在并行的
`src/` 改动中被修掉（项数也从 23 涨到 23 全通过）。本次改动只落在 `sdk/` 与
`examples/adapter_demo/`，**没有碰 `src/`**，这条从头到尾都不是本次引入的。

---

## 7. R0 门禁：**未跑**（等并行修改收尾后重跑一次干净的）

### 7.1 为什么不现在跑

`src/` 与 `tests/` 当时正被其它会话修改（§4.3 的漂移记录）。在"被测对象在变"的窗口里
跑出来的全量数字，**既不能证明系统自洽，也不能证明有问题**——它只是那一刻的瞬时像。
所以本轮**不产出全局结论**，只产出本轮改动范围内的证据（§4.2）。

### 7.2 跑最终 R0 之前，先确认"停下来了"

```powershell
# ① 还有没有人正在写？（应该连续两次拿到相同的清单）
Get-ChildItem -Recurse -File -Path src,tests,sdk,examples,plugins,adapters,docs,tools |
  Where-Object { $_.LastWriteTime -gt (Get-Date).AddMinutes(-10) -and
                 $_.FullName -notmatch '__pycache__' } |
  Select-Object LastWriteTime, FullName | Sort-Object LastWriteTime

# ② 本轮交付物的指纹没被顺手改掉（**只看这五个可执行面**；对不上就先问清楚再跑）
Get-FileHash -Algorithm SHA256 sdk/anp_adapter/resolve.py, sdk/anp_adapter/client.py,
  sdk/anp_adapter/__init__.py, examples/adapter_demo/adapter.py,
  examples/adapter_demo/verify.py
```

本轮交付时（2026-09-23 16:5x）的指纹（SHA256 前 16 位，对应 **demo verify 61/0** 那次复跑）：

```
790212B7BA7E6A7E  sdk/anp_adapter/resolve.py
9D9EC3D44CCAD2C0  sdk/anp_adapter/client.py
8EC406F2B01212CA  sdk/anp_adapter/__init__.py
FD4A44644DF51BFA  examples/adapter_demo/adapter.py
F165424B708DC166  examples/adapter_demo/verify.py
```

> ★只有上面这五个是 Discovery 的**可执行面**（它们对不上就等于代码变了，必须重跑 E 段）★。
> `docs/` 三个文件的指纹**不作为判断依据**：本文档与 Spec 在交付后仍会被继续编辑
> （本轮就改了两次），拿文档哈希当"没被改过"的凭据只会制造假警报。

Discovery 依赖的 Core 面（**只读这两个端点/函数，改了就重跑 Discovery 证据**）：

```
BDDB4323689B63B2  src/anp_hub.py     ← agent_view() → GET /agents；do_send() → /messages/send
ADE65AEABBE63132  src/anp_core.py    ← recipients_of()（canonical ID 只按字面取用）
```

### 7.3 R0 要回答的不是"功能有没有"，而是"合起来还自洽吗"

按现状，R0 的证据链应该是（Discovery 侧已经就位，其余三条由各自的 owner 提供）：

```
协议     docs/ANP-PROTOCOL.md      ↔  tests/selftest_p09_api.py（canonical API 面）
插件     docs/ANP-PLUGIN-SPEC.md   ↔  tests/selftest_plugins.py（含 manifest 闭集）
适配     docs/ANP-ADAPTER-SPEC.md  ↔  tests/selftest_spec_conformance.py（R1–R5 规范/代码不许分家）
寻址     §8.5 / §8.6 / §8.7        ↔  examples/adapter_demo/verify.py E 段（31 项）
```

四行都指向同一件事：**规范里写的、代码里跑的、测试里验的，是同一份事实**。
Discovery 已经把这一行接上了（E 段就是 §8.5/§8.6 的可执行版本）；
Plugin 那一行等并行改动收干净后再确认。

### 7.4 本轮明确**不做**的事

- 不再扩 Discovery（任务板已收口：精确 / 模糊 / AMBIGUOUS / NOT_FOUND / `last_seen`↔`last_used` 分离）
- 不动 `agents.discover`（§8.7 独立待决）
- 不因为并行窗口里的数字去改任何东西
