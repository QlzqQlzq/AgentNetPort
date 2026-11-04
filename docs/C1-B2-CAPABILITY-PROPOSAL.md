# C1+B2 能力提案：`/plugin/register` 到底需要什么能力

> 性质：**提案，未动任何代码**。按 `V0.1.1-FIX-LIST.md` 的要求，C1+B2
> **禁止预设 credential/nonce 答案**，必须先回答「需要什么能力」。本文只做这件事。
> 拍板后再动 `src/`、SDK、Spec、tests。

---

## 1. 先把事实钉住（三条，全部读过源码，不是推测）

### 事实 1：`/plugin/register` 现在要 **admin token**

`src/anp_hub.py` 该路由直接调 `self._admin_ok()`：

```
需要管理员令牌（插件报到只能由管理员/插件进程用同一枚令牌完成）
hint: 令牌在 ~/.agentnetport/admin.token；已加载插件在 load 时由 Core 直接握手，
      这个端点是给"插件自己重启后回来报到"用的
```

而 `docs/ANP-ADAPTER-SPEC.md` §2b 写的是：

> | 2b | Adapter Plugin token 只用于：`POST /plugin/register`、取自身配置、收 `/anp/register` |

**一边说"用插件自己的令牌"，一边只收 admin —— 这就是 C1 的分叉。**
后果是二选一，两个都坏：插件要报到就得持有 admin；或者它报到不了。

### 事实 2：admin 已经能**铸造新身份**，但**不能**续约已有身份

`/heartbeat` 的两个分支：

```
身份没出现过（known=False）→ policy.may_register(name, admin_token_ok(token), home)
                             ★持管理员令牌的人永远放行★
已经存在（known=True）      → 只 self._auth(name)，即必须拿对身份自己的密钥）
                             ★没有任何 admin 旁路★
```

所以 Spec §3.2 第 247 行这句：

> `已有 name + 管理员令牌 → 放行（否则上锁会把接线脚本关在门外）`

**与实现相反**。admin 能造新身份，但不能动已有身份的端点。这是 B2 的分叉——
只不过方向和 fix list 担心的不一样：**实现比文档更严**，不是更松。

（顺带：§3.2 那句话描述的能力其实**由 `known=False` 分支实现了**，只是位置不同。）

### 事实 3：插件在 `/anp/register` 里**已经拿到过自己的令牌**

`src/anp_plugin.py` 的方向二（Core → Plugin）：

```python
payload = {"identity": entry["id"],
           "token": entry.get("token"),        # ★插件自己那一枚 agent token★
           "config": ..., "plugin_dir": ..., "api_level": ..., "reload_reason": ...}
# 头里也带同一枚（x-mailbox-key），插件可拿来确认"这确实是信箱发来的"
```

永久架构约束（`anp_core.py:1120`）已经把底座铺好了：

- `/anp/register` **只发插件自己那一枚 agent token**，任何响应体都不含 `hub.secret`
- Core **不提供读 hub.secret 的接口**，连管理员也不给

**⇒ 这意味着插件根本不需要 admin。**它重启后手里就已经有 `entry["token"]` 这一枚
（Core 存在 `PLUGINS` / 账本里，重启后原样发还）。用这一枚做报到鉴权，
能力刚好够，且不需要新增任何 permission 枚举。

---

## 2. 那么 `/plugin/register` 到底需要什么能力？

去掉身份分层被击穿的部分，它真正要做的事只有三件：

| # | 能力 | 说明 |
|---|---|---|
| **A** | **证明"我就是某个已加载的插件"** | 不是"我是管理员"，而是"我是 pid=anp.adapter.mimo 那个插件" |
| **B** | **更新我自己的注册声明** | tools / events / panel（贡献表）。★只能改自己的★ |
| **C** | **取回我自己的凭证** | identity + token + config + plugin_dir |

**不需要的**（这三条是 fix list 点名排除的，我复核后同意）：

- ❌ 读他人消息、看别人收件箱
- ❌ 管理其他插件（load / unload / reload 别人的）
- ❌ 铸造新的 agent 身份
- ❌ 读 `hub.secret`
- ❌ 看 `/sessions`、看板等管理员面

**判据一句话**：这个端点该问的是「**你是不是 pid 本人**」，
不是「**你是不是管理员**」。前者用插件自己的 agent token 就能答，且**答得更准**——
admin 令牌只证明"你是管理员"，不证明"你是这个插件"。

---

## 3. 修完后的身份图（fix list 的目标，逐条对齐）

```text
hub.secret        → 仅 Core 内部签名。任何响应体都不出现，admin 也拿不到（已是现状）
plugin credential → 证明"我是这个插件"：报到 / 更新自己贡献 / 收 /anp/register
agent token       → 代表某个 Agent 收发（含插件自己那一枚）
admin token       → 面板 / /sessions / 运维脚本。★不进 Adapter★
```

修完后**没有任何 Adapter/Plugin 需要持 admin**。

---

## 4. 两个方案（都是"鉴权最小化"，不重做权限体系）

### 方案 P：鉴插件自己的 agent token（**推荐**）

```python
# hub 侧 /plugin/register，把 admin 门换成身份门
ok, why = core.verify_key(pid, given_token)     # given_token 来自 x-mailbox-key
if not ok:
    401 "插件报到要用插件自己的令牌（Core 在 /anp/register 时发过那一枚）"
```

**为什么够**：能力 A/B/C 全部落在这一个判断上——`verify_key(pid, …)` 本身就是
"你是不是 pid 本人"。比 admin **更精确**（admin 答不出"我是哪个插件"）。

**为什么安全**：

- 插件无法报到成**别的**插件（token 与 pid 绑定，A 插件 token 报到 B 插件 → `verify_key` 失败）
- 拿不到 `hub.secret`（响应体本来就不含，永久约束已保证）
- 无法借此读他人消息 / 管别的插件（路由里根本没有这些能力）

**改动面**：hub 一处判断 + SDK `plugin_register()` 签名去掉 `admin_token` 改成传插件自己的 token
+ Spec §2b/§4.0 把"admin"字样去掉 + experiment 不再读 `core.admin_token`。

### 方案 N：Core 下发 register nonce

`/anp/register` 时额外带一个一次性/可轮换的 `register_nonce`，仅能报到自己。

**否决理由**：插件**已经有自己的 agent token 了**（事实 3）。再发一枚 nonce 是
**并存两枚同样能力的东西**，违背最小化——多一个要轮换、要过期、要防重放的秘密，
却没多买到任何能力。若将来有"报到必须比持有 token 更难"的要求，再考虑。

### 明确不选：保留 admin 旁路（文档降级为"仅运维"）

fix list 给的备选。**我建议不选**：它保留了击穿点，只是把文档写软。
而 Mimo 已经是 Adapter Plugin 的真实样本——它现在**已经**能只靠自己那枚令牌
完成所有真实通信（本机实测 28/28），没有任何一环需要 admin。留着旁路没有受益者。

---

## 5. B2：Spec §3.2 与实现对不齐怎么办

现状是**实现更严**（admin 不能续约已有身份），**文档更松**（写"放行"）。

**建议改文档，不改实现**：

| Spec §3.2 现状 | 建议改成 |
|---|---|
| `已有 name + 管理员令牌 → 放行` | **删除这一行** |
| （无对应描述） | 补：`已有 name + 无 key → ★401 拒绝★`；要改已有身份只有两条路：拿到它自己的 key，或用 `/tool` 的 `mail_send` 之外的管理员面**重新 mint**（那是 `known=False` 分支的能力，不是续约） |

**理由**：收紧的方向本身是对的（P0-2 的同一条：能重发令牌 = 能顶掉别人的唤醒端点）。
把文档改严只是把事实写对；若反过来放宽实现去迁就文档，是**主动打开一个
任意本机进程顶替他人身份的口子**。

需要一起核的：`docs/ANP-PLUGIN-SPEC.md` §4.0 有没有同样的措辞。

---

## 6. 验收标准（fix list §C1 的六条，逐条对到可测的东西）

| # | 要求 | 怎么测 |
|---|---|---|
| ① | 插件**仅持自己的 token** 能完成 `/plugin/register`（重启报到） | experiment 里把 `core.admin_token` 那行换成插件 token，仍报到成功 |
| ② | 拿 admin 调该路径有明确行为 | 一条负例：admin 令牌调 `/plugin/register` → 明确拒绝（或文档写明"仅运维路径"，与代码一致） |
| ③ | 响应体仍不含 `hub.secret` | 断言 `hub.secret` 不出现在响应体任何位置 |
| ④ | Spec 2b / §3.2 / §4.0 / SDK 签名四处一致 | `selftest_spec_conformance` 加一条文档-代码对照 |
| ⑤ | **负例：A 插件 token 报到 B 插件 → 拒绝** | `verify_key(B, token_of_A)` 必须失败 |
| ⑥ | experiment 不再依赖 `core.admin_token` 完成报到 | grep 级断言：experiment 源码里不出现 `admin_token`（或注明仅测试夹具） |

---

## 7. 影响面与风险

| 文件 | 改什么 | 风险 |
|---|---|---|
| `src/anp_hub.py` | `/plugin/register` 的 admin 门 → `verify_key` 门 | 低。只收紧，不放宽 |
| `sdk/anp_adapter/client.py` | `plugin_register(plugin_id, admin_token)` → `(plugin_id, plugin_token)` | 低。签名变，调用点少 |
| `examples/adapter_plugin/experiment.py` | 不再读 `core.admin_token` | 低 |
| `docs/ANP-ADAPTER-SPEC.md` | §2b / §3.2 | 只改文档 |
| `docs/ANP-PLUGIN-SPEC.md` | §4.0 对齐 | 只改文档 |
| `tests/selftest_plugins.py` | 现有用例可能依赖 admin 报到 | **要先看**：若有用 admin 走通 /plugin/register 的用例，改为插件 token |

### 已数清：`selftest_plugins.py` 剖面 E 有 3 处受影响

`tests/selftest_plugins.py` 的「E. /plugin/register」段，逐条对过：

| 用例 | 现状 | 方案 P 之后 |
|---|---|---|
| **E1** 插件主动报到成功 | 用 `admin` 调，期望 `ok` + `identity` | 改成用插件自己的 `core.key_for("alice.echo", home)` 调。**1 处改动** |
| **E2/E3/E3b** 响应内容 | 断言 token 是自己的、不是 hub.secret、带 config 等 | **不变**。这三条本来就是对的，且正好是方案 P 的护栏 |
| **E5** 无令牌报到 → 401 | 传空串 | **仍通过**，但错误原因从"要管理员令牌"变成"要插件自己的令牌"。宜补一条：**A 插件的令牌报到 B 插件 → 401**（验收 ⑤） |
| **E6** 未加载插件报到 → 404 | 用 `admin` 调 `ghost.plugin` | ⚠️ **见下面这个真问题** |

#### ⚠️ E6 暴露一个必须决定的顺序问题

现在 admin 门在前、存在性检查在后，所以 `ghost.plugin`（未加载）拿 admin 能走到 404。
方案 P 把门换成 `verify_key(pid, token)` 后，`ghost.plugin` **从来没有发过 key**，
于是它会在**鉴权那一步就失败 → 401**，而不是 404。

两条路，我倾向第二条：

1. 保持鉴权在前 → E6 断言的从 404 变成 401（"你没资格问这个插件"）
   ——但这样**泄露了"这个插件没被加载"**吗？其实不泄露：401 对已加载/未加载一视同仁。
2. 存在性检查提前 → 未加载一律 404，鉴权只对已加载的做
   ——信息暴露更少，但多一次无序查询

**推荐 1**（鉴权在前，未加载也回 401）：更符合"先证明你是谁"，且实现简单。
代价是 E6 的期望码要从 404 改 401，并相应改文案。

> 这条是提案阶段才浮出来的，若不先定，实施时会顺手选错顺序。

### 顺带：`/plugins/<id>/reload` 仍是 admin 路由

剖面 E 里 `h.call("/plugins/alice.echo/reload", {}, admin)` 是**另一个端点**
（管理员运维面），**不随本次改动**。方案 P 只动 `/plugin/register` 一个路由。

**明确不做**（fix list 划的界，我遵守）：不做完整 RBAC、不发新 permission 枚举、
不动 `hub.secret` 隔离。

---

## 8. 待你拍板的四件事

1. **方案 P 还是 N？** 我推荐 **P**（插件已有自己的 token，nonce 是多一个同样能力的秘密）
2. **B2 改文档还是改实现？** 我推荐**改文档**（实现更严，放宽它会打开顶替他人身份的口子）
3. **② 的行为**：admin 调 `/plugin/register` 修完后应该**明确拒绝**，还是**保留为运维旁路**？
   我推荐**明确拒绝**（Mimo 已证明无人需要 admin）
4. **E6 的鉴权顺序**：未加载的插件报到应该回 **401**（鉴权在前）还是 **404**（存在性在前）？
   我推荐 **401**（"先证明你是谁"，且对已加载/未加载一视同仁，不泄露加载状态）
