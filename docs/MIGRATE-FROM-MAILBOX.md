# 旧 Mailbox 功能全量盘点 → ANP 去向

> 状态：**审计 v2（全量功能树，不是只剩 WebUI）**  
> 范围：`<LEGACY_MAILBOX_ROOT>` 全部交付物。  
> 约束不变：不改 DSH · 不迁 Adapter · 不重复实现 Core · 按现行 Plugin Spec 拆。  
> 对照仓：`<ANP_ROOT>`。

---

## 0. 先回答「是不是只有 WebUI」

**不是。** 你当时做成的东西很多；上一轮把「已进 ANP Core」「Adapter/DSH」压成一句带过，看起来像只剩 WebUI。

正确说法是：

| 视角 | 数量 |
|---|---|
| 你做成的**能力/交付物** | **约 20+ 块**（见 §1 全表） |
| 其中 ANP Core **已接管** | 约一半（通信/身份/状态机/插件机制） |
| 其中属于 **Adapter / DSH**（本轮明确不迁） | 约 8 块 |
| 其中 **还没进 ANP、也不是 Adapter** | **WebUI 全家 + 会话决策定性 + DirectBackend + 运维/技能** |
| 其中还能 **按现行 Plugin Spec 再拆成 Plugin** | **很少**：公告栏已完；最多再加 diagnostics 类 |

「搞了好多」和「还能再拆成 Plugin 的很少」可以同时为真——因为大量工作已经变成 Core / Adapter 了。

---

## 1. 全量功能树（你当时搞了什么）

### 1.1 通信内核（→ **ANP Core 已接管**，禁止重做）

| # | 能力 | 旧落点 | ANP 现状 |
|---|---|---|---|
| 1 | 身份注册 / 心跳 / sticky 字段 | `mailbox_core.heartbeat` | ✅ `anp_core` |
| 2 | 身份令牌 `keys.json` / 发证 / 校验 | core + hub `_auth` | ✅ |
| 3 | hub.secret 信封 HMAC（签内容不签状态） | `sign_envelope` / `verify_envelope` / `integrity_report` | ✅ |
| 4 | 单发 / 多人信（共享 id）/ 缓存信 `wake=false` | `core.send` / `send_group` | ✅ |
| 5 | 禁止自发自收 | `core.send` | ✅ |
| 6 | 收信 `mail_inbox`（过滤 from/kind/correlation + peek） | hub `do_inbox` | ✅ `/messages/receive` ≡ mail_inbox |
| 7 | 签收模型（inbox / hub http-acked / wake:exec / cache） | `sign_off` / `wake_sign_off_ids` | ✅ |
| 8 | 长轮询 `mail_wait`（peek 不消费） | `do_wait` | ✅ |
| 9 | 待办 `mail_pending` / 手动重投 `mail_redeliver` | `do_pending` / `do_redeliver` | ✅ |
| 10 | 自动重投 + 退避 + 放弃 + `message.undelivered` | hub 后台 | ✅ |
| 11 | 消息状态机（attempts/delivered_at/read/claimed/replied…） | core | ✅ |
| 12 | 关联线 journal / `mail_query`（owner 作用域） | core + `query_journal` | ✅ |
| 13 | 三档 wake（http/exec/queue）+ 唤醒信任边界 | `wake` / `check_wake_config` | ✅（含 #4.1a `install_roots` 含 plugins/） |
| 14 | queue 终态 / `wake_skipped` 三态 | record_delivery | ✅ P0-0 |
| 15 | 对话线限速（防 A↔B 互刷） | `context_rate_exceeded` | ✅ |
| 16 | 令牌 ≠ hub.secret 永久约束 | `secret_isolation_ok` / 发证漏斗 | ✅ |
| 17 | 维护 / 裁剪 / 账本压缩 / 异步日志 | maintenance / AsyncLog | ✅ |
| 18 | 策略 register / send_to（+ 残留 announce） | `mailbox_policy` | ✅ 主体；`may_announce` 见 §4 |
| 19 | Plugin 机制（manifest/注册表/握手/tools/events/panel/lifecycle） | `mailbox_plugin` | ✅ `anp_plugin` |
| 20 | Core 8 工具面 `mail_*`（去公告后） | hub `dispatch_tool` | ✅ `/tool` |
| 21 | 管理员令牌 / `/health` / `/agents` | hub + core | ✅ |
| 22 | launch_context（MCP 启动坐标补 meta） | `write_launch_context` | ✅ core 仍在 |

**这些不是「还没迁」，是已经搬进 ANP Core。** 任何 Plugin 迁移都不得再实现一遍。

---

### 1.2 应用 / 界面 / 协作（**还没进 ANP，或只进了一半**）

| # | 能力 | 旧落点 | 规模 | 去向 |
|---|---|---|---|---|
| 23 | **公告栏** 广播 + 列表 + 无已读模型 + seen_by | hub `do_announce`/`do_board` → 后改插件 | 已插件化 | **A：已迁 `anp.bulletin`** |
| 24 | **WebUI 只读看板**（信封/未读/投递/验签/策略/搜索） | `mailbox_webui.py` 44KB | 大 | **B1：管理伴生（不是 Plugin）** |
| 25 | **WebUI 插件面板宿主**（经 `/admin/panel/*`） | webui `_panels` | 中 | 并入 B1 |
| 26 | **WebUI 进程监督**（端口占用/拉起/停/status） | `mailbox_webui_supervisor.py` | 小 | 并入 B1 |
| 27 | **会话决策**（roster/context 绑定/decide 盖章/流水） | `mailbox_sessions.py` | 中 | **B2：非 Plugin**（send 钩子；Spec 装不下） |
| 28 | **DirectBackend 离线直连**（hub 挂了仍能收发/唤醒） | `mailbox_mcp.DirectBackend` | 中 | **B3：Adapter 客户端，不迁 Plugin** |
| 29 | **信件版式 / 信封强制渲染**（路由封头、回信坐标） | DSH bridge `renderMail` 等 | 中 | **C：DSH/Adapter**（本轮不动） |
| 30 | **收信合并**（多封 → 一个回合） | DSH bridge batch | 中 | **C：DSH** |
| 31 | **MCP 工具皮**（8 工具 schema + 渲染文案 + 插件工具合并） | `mailbox_mcp.py` 43KB | 大 | **C：Adapter** |
| 32 | **通用 exec 唤醒器** + 接线表 + 自签收 | `mailbox_spawn.py` 36KB + `mailbox_agents.json` | 大 | **C：Adapter** |
| 33 | **Codex / Chen / Mimo 适配**（含 C 档落盘适配） | `mailbox_codex/chen/mimo.py` | 大 | **C：Adapter** |
| 34 | **进程健康 API**（codex/chen health） | `*_health.py` | 小 | **C：Adapter process_api** |
| 35 | **Chen UserPromptSubmit hook**（未读注入当前会话） | `mailbox_hook.py` | 中 | **C：Adapter** |
| 36 | **DSH 收信桥**（wake/名册/bind/注入会话） | `dsh-bridge/` | 大 | **C：不改 DSH** |
| 37 | **DSH 会话路由**（resolve/sessions/pin + 记忆） | `dsh-mailbox-router/` | 大 | **C：不改 DSH** |
| 38 | **接线安装器**（MCP toml / DSH 插件双份 / rollback / migrate-keys） | `tools/install.py` 28KB | 大 | **C：运维，非 Plugin** |
| 39 | **发行打包 / 校验** | `package_release.py` | 中 | **C：运维** |
| 40 | **状态回填** | `backfill_state.py` | 小 | **C：运维** |
| 41 | **在线/离线自检** | `tests/*`、`tools/verify*.py` | 大 | 随组件；ANP 已有自己的 selftest |
| 42 | **mailbox-register 技能**（四种接线/健康/踩坑） | `skills/mailbox-register/` | 中 | **C：运维文档**（可改成 ANP Adapter 文档） |
| 43 | **根目录兼容壳**（历史路径） | `write_shims.py` + 根 `mailbox_*.py` | 小 | 不迁 |

---

## 2. 按「还要不要动」重排（回答：需要迁移什么）

```text
你搞过的 ≈ 43 项
├── 已在 ANP Core（1–22）          → 不迁、不重做
├── 已在 ANP Plugin（23 公告栏）   → 完成
├── 还没进 ANP 且不是 Adapter
│     ├── 24–26 WebUI 全家         → 迁入 ANP【管理伴生】★唯一 P1 功能搬迁
│     ├── 27   会话决策            → 定性/可选迁出 Core，【不能】按现行 Spec 做 Plugin
│     ├── 28   DirectBackend       → 不迁 Plugin（Adapter 客户端）
│     └── 38–43 运维/技能/壳       → 随 ANP 发行版重做工具链，非 Plugin
└── Adapter / DSH（29–37）        → 你明确本轮不迁
```

### 2.1 真正「还需要迁」的清单（按优先级）

| 优先级 | 项 | 形态 | 说明 |
|---|---|---|---|
| **P1** | **24–26 WebUI + supervisor** | **ANP 管理伴生**（`anp_webui` + supervisor 或合并） | hub 已有死引用 `mailbox_webui.py`；看板是管理面不是插件 |
| **P1 文档** | **27 会话决策定性** | 写清「非 Plugin / 寄居物」 | 避免后人再按 Plugin 硬拆 |
| **P2** | **23 公告栏收尾** | 已是 Plugin | 只剩旧数据迁移确认 + 看板改走 panel |
| **P2 可选** | **诊断摘要**（从 WebUI 抽） | Plugin `anp.diagnostics` | tool+panel，只打 `/health` `/agents` |
| **P2 文档** | **42 mailbox-register** | 改写为 ANP Adapter 接入说明 | 仍是运维/Adapter，不是应用插件 |
| **P3** | **28 DirectBackend** | 不迁或 Adapter 标废弃 | |
| **P3** | **38–41 运维/测试** | ANP 自己的 install/verify | 非 Plugin |
| **不做** | **29–37** | Adapter / DSH | 本轮约束 |

### 2.2 按现行 Plugin Spec **能**再拆成的 Plugin（很少，但是真的）

| Plugin | 来源旧功能 | 贡献 | 权限 | 状态 |
|---|---|---|---|---|
| `anp.bulletin` | 23 广播公告 | tools `post`/`list` + panel | wake.others, broadcast, tools.register, webui.panel | **已完成** |
| `anp.diagnostics`（可选） | 24 侧栏的完整性/策略/概况摘要 | tools `summary` + panel | tools.register, webui.panel, agents.discover | 待做 P2 |

**没有第三块「必须新建」的 Plugin。**  
不是因为你没做功能，而是其余大块天然是：Core（已收）、Adapter（不迁）、DSH（不动）、管理伴生（WebUI）、send 钩子（Spec 外）。

---

## 3. 为何 24–28 不能假装成 Plugin（一句话版）

| 项 | 卡在哪条 Spec |
|---|---|
| WebUI 全文浏览 | 插件不得读他人 inbox / ledger；panel 是嵌入片段不是整站 |
| 会话决策 | 贡献点只有 tools/events/panel；决策在 `send` 路径内，无 pre-send 钩子 |
| DirectBackend | 插件是网络对等体；它是 MCP 进程内本地文件后端 |

硬拆成 Plugin = 要么违 Spec，要么扩 Spec（本轮不扩）。

---

## 4. 顺带清理（不是功能迁移）

| 项 | 动作 |
|---|---|
| `may_announce` | 广播权已由插件 `broadcast` 承担；确认无调用后删/弃用 |
| `start_webui_companion` → `mailbox_webui.py` | 随 B1 改为 ANP 内路径 |
| Plugin Spec P1「WebUI/会话/Direct 搬成默认插件与 Adapter」 | 改成与 §2 一致 |
| ANP README「寄居物三项」 | 对齐本清单 |

---

## 5. 对照表：旧模块 → 去向（按文件）

| 旧文件 | 去向 |
|---|---|
| `src/mailbox_core.py` | Core（已迁 `anp_core`） |
| `src/mailbox_hub.py` | Core（已迁 `anp_hub`） |
| `src/mailbox_plugin.py` | Core（已迁 `anp_plugin`） |
| `src/mailbox_policy.py` | Core（已迁 `anp_policy`） |
| `src/mailbox_preflight.py` | Core（已迁 `anp_preflight`） |
| `src/mailbox_sessions.py` | Core 寄居（已迁 `anp_sessions`）→ **B2 非 Plugin** |
| `src/mailbox_webui.py` | **待迁 ANP 管理伴生 B1** |
| `src/mailbox_webui_supervisor.py` | **并入 B1** |
| `src/mailbox_mcp.py` | Adapter（含 DirectBackend）→ 不迁 Plugin |
| `src/mailbox_spawn.py` + `mailbox_agents.json` | Adapter → 不迁 |
| `src/mailbox_codex.py` / `chen.py` / `mimo.py` | Adapter → 不迁 |
| `src/mailbox_*_health.py` | Adapter → 不迁 |
| `src/mailbox_hook.py` | Adapter → 不迁 |
| `plugins/anp.bulletin/` | **已迁 ANP** |
| `dsh-bridge/` / `dsh-mailbox-router/` | DSH → 不动 |
| `tools/install.py` 等 | 运维 → ANP 发行版重做 |
| `skills/mailbox-register/` | 运维文档 → 可改 ANP Adapter 文档 |
| `tests/*` | 随组件；ANP 已有对应 selftest |

---

## 6. 执行顺序（修订）

```text
1. B1  WebUI + supervisor 迁入 ANP 管理面（消死引用）     ← 唯一 P1 代码搬迁
2. 文档：会话决策定性 + Plugin Spec P1 纠偏 + may_announce 清理
3. （可选）anp.diagnostics
4. （可选）mailbox-register → ANP Adapter 接入文档
5. 其余 Adapter/DSH/运维按 #4 与发行版节奏，不在本清单
```

---

## 7. 一句话

**你搞过的是整套「可信中转层 + 三家接入 + 看板 + 会话 + 公告 + 接线」；ANP 已收走通信内核与插件机制，公告栏已成 Plugin。还没进 ANP 的大块是 WebUI 全家、会话定性、DirectBackend 与运维/技能——其中只有 WebUI 适合现在搬，而且是管理伴生不是 Plugin；能再拆成 Plugin 的应用面本来就只剩公告栏（已完）和可选 diagnostics。**
