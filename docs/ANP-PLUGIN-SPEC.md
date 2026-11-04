# ANP Plugin Specification v0.1

> 状态：**定稿待实现**。本文是第 4 步（插件系统）要实现的那份契约。
> 目标读者：第三方插件开发者、以及要实现 Core 侧插件机制的人。
>
> 两条已定的设计取舍，先摆在这里，后面所有细节都从它们推出来：
>
> 1. **Manifest 是身份与装载描述，Register 是运行时能力声明。**
>    manifest 只说「我是谁、怎么启动、需要什么」；tools / events / panel 在 register 阶段声明。
> 2. **版本用整数 `api_level`，不用 semver range。**
>    `plugin.api_level > CORE_API_LEVEL` → 拒绝；`<=` → 尝试加载。

---

## 1. 根原则

**ANP 插件不是被 import 的代码，而是网络上的一个对端。**

Core 从不 import 插件、从不解释插件内容、从不知道插件是什么应用。
一个插件 = 一个 ANP 身份 + 一个 manifest + 若干 HTTP 端点。

这一条把 supervisor / RPC / 沙箱 / 熔断 / 依赖解析全部消掉：插件崩了只是它自己的端点连不上，
对 Core 而言就是一次唤醒失败，走已有的重试。插件框架的重量因此归零。

---

## 2. Manifest（`plugins/<id>/manifest.json`）

```jsonc
{
  "id": "alice.werewolf",                 // ★必填
  "name": "Werewolf",                     // ★必填
  "version": "0.1.0",                     // ★必填（插件自己的 semver，Core 不解析）
  "api_level": 1,                         // ★必填（正整数，见 §2.2）

  "command": ["python", "plugin.py"],     // ★必填二选一：Core 拉起
  // "url": "http://127.0.0.1:9100",      // ★           已在运行

  "description": "8-agent werewolf over ANP",
  "author": "alice",
  "license": "MIT",
  "repository": "https://github.com/alice/anp-werewolf",
  "homepage": "https://github.com/alice/anp-werewolf",
  "anp_min": "0.2.0",                     // 可选：低于此版本拒绝加载（单向 > 比较）
  "cwd": ".",                             // 可选：Core 拉起时的工作目录，默认 manifest 所在目录
  "permissions": ["wake.others", "broadcast", "agents.discover",
                  "tools.register", "webui.panel"],
  "requires": { "plugins": [] },           // 可选：只校验存在，不解析版本、不自动安装
  "tags": ["game", "multi-agent"]
}
```

### 2.1 字段表

| 字段 | 必需 | 类型 | 说明 |
| --- | --- | --- | --- |
| `id` | ✅ | string | 全网唯一。也是插件的 ANP 身份名。字符集 `^[a-z0-9][a-z0-9._-]*$`（会变成目录名与工具名前缀）。**若与已登记身份重名，拒绝加载** |
| `name` | ✅ | string | 人看的名字 |
| `version` | ✅ | string | 插件自己的 semver。Core 不解析、不比较 |
| `api_level` | ✅ | int | 用的 Plugin API 代次，见 §2.2 |
| `command` | 二选一 | string[] | Core 拉起插件的 argv。**不带任何模板占位符**——端口通过环境变量 `ANP_PLUGIN_PORT` 给，配置在 `/anp/register` 时给 |
| `url` | 二选一 | string | 插件已在运行（自己起的或别人起的）。主机必须是回环 |
| `description` | | string | 给人看 |
| `author` / `license` / `repository` / `homepage` | | string | 纯元数据 |
| `anp_min` | | string | 低于此 ANP 版本拒绝加载。只做单向 `>` 比较 |
| `cwd` | | string | 拉起时的工作目录，默认 manifest 所在目录 |
| `permissions` | | string[] | 闭集，见 §5 |
| `requires.plugins` | | string[] | 依赖的插件 id。**只校验在不在**，不在就拒绝启用并说清缺谁 |
| `tags` | | string[] | 检索用 |

### 2.2 `command` / `url`：严格二选一（写死的校验规则）

```text
两个都有 → manifest.invalid
两个都没有 → manifest.invalid
只有一个 → 通过
```

这不是风格偏好，是**确定性的前提**：

- 两个都有时，Core 不知道"该拉起新进程，还是连那个现成的端点"。两个出口指向同一个
  身份名，而 Core 又必须在 spawn 与 attach 之间二选一——状态立刻出现两份真相；
- 两个都没有时，Core 连"怎么找到这个插件"都不知道，`load` 没有任何可执行的动作。

所以 Core 的校验是**先数个数，再谈内容**：

```text
present(command) + present(url) != 1                      → manifest.invalid
command 不是数组 / 元素不是字符串 / 数组为空                → manifest.invalid
url 不是字符串 / 主机不是回环（127.0.0.1 / localhost / ::1）→ manifest.invalid
```

`command` 与 `url` 里**都不带模板占位符**：端口走环境变量 `ANP_PLUGIN_PORT`，
配置走 `/anp/register`（见 §4.0）。

### 2.3 版本：只用一个整数

```jsonc
{ "api_level": 1 }
```

Core 侧对应物是 `core.CORE_API_LEVEL`。加载规则只有两条：

```text
plugin.api_level >  core.CORE_API_LEVEL  → 拒绝（api.level.unsupported）
plugin.api_level <= core.CORE_API_LEVEL  → 尝试加载（尽力向后兼容）
```

- **破坏性变更** → `CORE_API_LEVEL + 1`；**纯增**（新端点、新字段、新事件）→ 不变。
- 刻意不用 `^0.3.0 || >=0.4` 这类 semver range：pre-1.0 阶段让第三方去理解范围语法没有收益，
  而"我这个插件比信箱新"用一次整数比较就够。
- 不设 `anp_max`：那是悲观约束，pre-1.0 没有意义。

### 2.4 刻意不在 manifest 里的东西

| 不放 | 为什么 |
| --- | --- |
| `tools` / `events` / `panel` | **贡献点在 register 阶段声明**。写死在静态 manifest 里，Core 就得在加载前理解插件的应用语义；而 register 时声明让"我现在提供什么"随运行时可变的配置走 |
| `config_schema` | v0.1 不校验配置 schema。插件自己读自己的 `config.json`，自己校验 |
| `capabilities` | MaiBot 用它同时承担"能干什么"和"权限"，ANP 只取后者，就是 `permissions` |
| `sdk_version` | 由 `api_level` 承担 |
| `env` | 静态环境变量是往仓库里塞密钥的入口。配置走 `config.json` |
| `i18n` / `changelog` / `urls{}` | 元数据噪音，v0.1 不要 |

**如果第三方把这些塞进 manifest，Core 一律忽略**（未知字段不报错，保证前后向兼容），
但也不生效——这是明确的降级，不是隐式的魔法。

---

## 3. 生命周期：只有四个动词

```
load    → 校验 manifest → 分配/确认身份与令牌 → 拉起进程（若 command）
       → POST 插件的 /anp/register  ★Core 发起，方向 Core → Plugin★，插件回自己的贡献
enable  → 开始投递事件与来信（默认 load 后即 enabled；disable 过后重新拉起 + 重新 register）
disable → 停止投递；★若进程是 Core 拉起的，Core 终止它（Core 只终止自己拉起的那个）★
unload  → 注销 tools / panel / events 订阅 → 撤身份令牌 → 终止 Core 拉起的进程
```

**明确不做**：`start` / `stop` / `reload` / `activate` / `deactivate`。

- `reload` = `disable` + `load`，不需要新状态。
- `start` / `stop` 是进程生命周期，不是插件状态。

### 3.1 进程事实三分离（硬规则）

```
进程事实             是否算插件状态      谁负责
──────────────────   ──────────────────  ──────────────────────────
Core 发起 load       是（enabled）        操作人
进程自己退出          否                   没人。只是"端点不可达"
端点不可达            否                   走既有重试：退避 → 5 次 →"已放弃自动重投"
                                          （/health 与 mail_pending 里可见，不静默丢弃）
操作人 disable        是（disabled）        操作人；Core 只终止自己拉起的进程
操作人 enable         是（enabled）        重新拉起 + 重新 register
```

**Core 拉起 ≠ Core 保活。** 没有 supervisor、不自动重启、不探活。
插件死了只是它的端点连不上，对 Core 而言就是一次唤醒失败。

> 这条与"崩溃不是状态"是同一件事的两面：插件状态 = 操作人意图；进程存活 = 不跟踪；
> 端点可达 = 每次投递观测。三者永远不要互相推导。

### 3.2 Core 拉起时怎么给端口

```
Core 选一个空闲端口 → 以 command 拉起，环境里给 ANP_PLUGIN_PORT=<p>
插件 bind 127.0.0.1:<p> → Core POST /anp/register（带身份、令牌、配置、插件目录）
```
端口由 Core 分配，所以 `wake_url` 与 `base_url` **Core 直接就知道**，
插件连"上报自己的地址"都不需要做。（这正是"Core 拉起"相对 DSH 那种随机端口 + 心跳上报的简化。）

---

## 4. 端点：三个方向，永远不要混

### 4.0 ⚠️ 最容易踩的坑：两个 `register` 名字很像，方向相反

> **先看方向，再看字段。**
>
> | 端点 | 方向 | 谁起的头 | 谁给什么 | 什么时候发生 |
> | --- | --- | --- | --- | --- |
> | **`POST /plugin/register`** | **Plugin → Core**（插件请求 Core） | **插件** | 插件交出**贡献**（tools/events/panel），Core 发还**凭证**（身份/token/config） | 插件自己起来之后，主动来报到 |
> | **`POST /anp/register`** | **Core → Plugin**（Core 请求插件） | **Core** | Core 送来**凭证+配置**（身份/token/config/目录），插件交回**贡献** | Core 拉起插件后、reload 后，Core 主动去通知 |
>
> 一句话记法：**`/plugin/...` 是插件打给信箱的，`/anp/...` 是信箱打给插件的。**
> 看路径里是谁的名字在前：`plugin` 在前 = 插件主动；`anp` 在前 = 信箱主动。
>
> 两者**不是同一个东西的两个名字**，而是一套合理双向握手的两个方向：
> 谁来发起，谁就先交自己那一半。
>
> ★鉴权口径（C1，2026-09-23 更正）★：`POST /plugin/register` **只认插件自己的 agent token**
> （`x-mailbox-key`，即 Core 在 `/anp/register` 时发的那一枚）。
> **admin token 在这个端点明确不被接受**，也不保留运维旁路。
> 理由：这个端点要回答的是「你是不是 pid 本人」，而 admin 令牌只能证明「你是管理员」，
> 答不出「我是哪个插件」——用 admin 反而更不精确。
> 拿 A 插件的令牌报到 B 插件 → 401。未加载的插件也从没发过 key → 同样 401
> （鉴权在存在性之前，不对外泄露"这个插件有没有被加载"）。

  | 状态码 | 含义 |
  | --- | --- |
  | 401 | 凭证缺失 / 不对（"你是谁都没证明"） |
  | 403 | 身份已认证但没有权限（如 `/tool` 的 `permission.denied`） |
  | 404 | 令牌对、插件也确实没加载（纵深防御；正常情况下到不了这里） |

**为什么两个都要**：握手的两端都可能是发起方。Core 启动时它发起（走 `/anp/register` 把
配置推给插件）；插件自己重启后它发起（走 `/plugin/register` 找回自己的身份与配置）。
只有一边的话，另一边就得轮询或者记不住自己叫什么。


#### panel 端点契约（GET `/anp/panel`）

| 约束 | 说明 |
|---|---|
| **无 body** | GET 不带请求体。Hub 侧 `call_plugin_panel` 已去掉原来的 `data=b"{}"` |
| **快路径** | 面板应该是 ms 级（读本地文件）。**禁止在 panel 处理里同步回调 Hub** |
| **超时** | Hub 默认等 **2 秒**（`AGENT_ANP_PANEL_TIMEOUT` 可调）。插件故意 sleep(5) 时，Hub 会回超时错误，不占死 worker |
| **Core 不清洗** | 面板内容原样透传，Core 不解析、不缓存、不注入 |

为什么无 body：`BaseHTTPRequestHandler.do_GET` 从不 `rfile.read`，和带 Content-Length 的 GET
组合是经典错帧/卡死来源；keep-alive 下残留字节还会被当成下一条请求。

为什么禁止回调 Hub：插件若在 panel 里 `_core_get`，会和 Hub 的启动/加锁路径叠成死锁。

#### 就绪信号：`/health.preload_done`

Hub 的 serve 线程**先于**插件预加载启动（P0-1，2026-09-23）——否则启动阶段 `/health`
连不上，插件回调 Hub 还会对等死锁。代价是「HTTP 通」不再蕴含「插件就绪」。

| 判据 | 含义 |
|---|---|
| `GET /health` 通 | HTTP 已 accept，但插件可能还没起来 |
| `/health.preload_done: true` | 插件预加载已跑完（含失败也置位，不会永远等） |

调用方要断言插件工具/面板，就轮询 `preload_done`，别用 sleep 猜时间。

**Payload 分工只有一条规则，不会出现两套真相**：

```text
凭证（身份名 / token / config / 目录） 一律由 Core 给  ← 不由插件自报
贡献（tools / events / panel）          一律由插件给   ← 不由 Core 猜测
```

两边都是"发起方交自己那一半，应答方交自己那一半"：

```jsonc
// 方向一：插件发起 —— POST /plugin/register   （Plugin → Core）
请求  { "tools": [...], "events": [...], "events_url": "...", "panel": "..." }
响应  { "ok": true, "identity": "alice.werewolf", "token": "<插件自己的 agent token>",
        "config": {...}, "plugin_dir": "...", "base_url": "...", "api_level": 1 }

// 方向二：Core 发起 —— POST /anp/register     （Core → Plugin）
请求  { "identity": "alice.werewolf", "token": "<插件自己的 agent token>",
        "config": {...}, "plugin_dir": "...", "api_level": 1, "reload_reason": "load" }
响应  { "ok": true, "tools": [...], "events": [...], "events_url": "...", "panel": "..." }
```

> ★永久约束★：两个方向里的 `token` 都是**插件自己那一枚 agent token**，
> **任何响应体里都不允许出现 `hub.secret`**（见 §0.1 / MAILBOX-LOGIC）。

SDK 侧的命名因此也是分开的，不要合成一个方法：

```python
plugin.register_with_core(tools=[...], events=[...])   # → POST /plugin/register（我主动）
plugin.on_register(identity, token, config, dir_)      # ← POST /anp/register （Core 找我）
```

#### 方向 A：Plugin → Core（插件向 Core 请求，6 个）

| 端点 | 插件专属? | 说明 |
| --- | --- | --- |
| `POST /plugin/register` | ✅ | **插件主动报到**：交贡献、换凭证。方向与 payload 见 §4.0（极易与 `/anp/register` 混淆） |
| `GET  /plugin/config` | ✅ | 可选使用——配置在 register 时已交付，这是给"想热读配置"的插件 |
| `POST /messages/send` | 共享 | `{to, body, subject?, kind?, reply_to?, correlation_id?, wake?}` |
| `POST /messages/receive` | 共享 | `{peek?, from?, correlation_id?, kind?, limit?}` |
| `POST /messages/query` | 共享 | 但 **owner 作用域**：只能查自己创建的关联线 |
| `GET  /agents` | 共享 | 返回 `caps` / `skills`（Core 只存只返回，不匹配不调度） |

**插件不是特权客户端，它首先是一个身份。**

已被明确删掉的"看起来该有"的接口：`broadcast()`（`to=[...]` 就是广播）、
`create_task()`（关联 id 是字符串，插件自己生成）、`query_status()`（`messages.query` + 信封
状态字段已覆盖）、`register_handler()`（并入 register 的一个字段）、独立 `agents.wake()`
（唤醒的唯一目的就是送信，`send` 已顺带做）。

#### 方向 B：Core → Plugin（插件必须实现的端点，2 必需 + 3 按条件）

| 端点 | 何时必需 | Core 什么时候调它 | 带什么 |
| --- | --- | --- | --- |
| `POST /anp/wake` | **总是** | 有信投给它时（复用既有 `wake` 机制） | 完整信封 + `x-mailbox-key: <插件的 agent token>` |
| `POST /anp/register` | **总是** | `load` / `enable` / reload 时（**Core 主动**，方向 Core → Plugin；payload 见 §4.0） | 身份名、**插件自己的 token**、config、插件目录、`api_level` → 插件回贡献 |
| `POST /anp/tool` | 注册了 tools | Agent 调用插件工具时 | `{name, arguments, caller_identity}` |
| `POST /anp/events` | 订阅了 events | 事件发生时 | `{type, ...}` |
| `GET  /anp/panel` | 有 `webui.panel` | 管理员开面板时 | 无 |

**一个插件最少只需实现 2 个端点**（`/anp/wake` + `/anp/register`）就能工作。

#### 方向 C：管理面（操作人 / Core 自己用，不是插件 API）

| 端点 | 谁能调 |
| --- | --- |
| `GET  /admin/panel/<id>` | 管理员令牌 → 代理到插件 `GET /anp/panel` |
| `POST /plugins/<id>/load\|enable\|disable\|unload\|reload` | 管理员令牌 |

> Core 代理面板而不让 WebUI 直读插件文件：Core 是唯一管理边界，插件只提供 `/anp/panel`。
> 多一次 HTTP 跳换来的边界清晰——将来 WebUI 换实现，插件不会因此绑定到任何文件结构。

### 4.1 Register 时声明的贡献

```jsonc
// 插件在 register（两个方向都是它给这段）里声明的贡献
{
  "ok": true,
  "tools": [
    { "name": "create_game",                       // 规范名：<id>.create_game
      "description": "创建一局狼人杀",
      "input_schema": {"type":"object","properties":{
          "players":{"type":"array","items":{"type":"string"}}},"required":["players"]} }
  ],
  "events": ["agent.online", "agent.offline", "message.delivered", "message.undelivered"],
  "events_url": "http://127.0.0.1:<port>/anp/events",
  "panel": "/anp/panel"
}
```

| 贡献点 | 挂载方式 | 要权限 |
| --- | --- | --- |
| **Tools** | register 时声明，Core 合并进 `tools/list`、按名转发 `POST /anp/tool` | `tools.register` |
| **Event subscriber** | register 时声明 `events` + `events_url` | 无（订阅免费，事件本身免费） |
| **Panel** | register 时声明路径，Core 以 `/admin/panel/<id>` 代理 | `webui.panel` |
| **Message kind** | 直接用 `kind: "<plugin>.<type>"`，Core 原样透传 | **不需要注册**——零成本的扩展点 |
| **Correlation namespace** | `<plugin_id>:<instance>`，如 `alice.werewolf:game-001` | **不需要注册**——只是约定 |

> ⚠️ **实现细节（直接影响第三方）**：MCP 工具名只允许 `^[a-zA-Z0-9_-]{1,64}$`，**不能含点号**。
> 所以规范层写 `alice.werewolf.create_game`，MCP 实际暴露 `alice_werewolf_create_game`。
> Core 负责映射，插件只写规范名。

---

## 5. Permissions：5 个，全部是 Core **能真正校验**的

```jsonc
"permissions": ["wake.others", "broadcast", "agents.discover", "tools.register", "webui.panel"]
```

| 权限 | 放行什么 | 为什么单列 |
| --- | --- | --- |
| `wake.others` | 出站 `wake=true` | **最值钱的一个**：一个 `wake=true` 发给 8 个 agent = 拉起 8 个进程 = 8 份 token |
| `broadcast` | `to` 为多收件人或 `*` | 打扰全员 + 花钱 |
| `agents.discover` | 调 `GET /agents` | 拓扑知识 |
| `tools.register` | 往 agent 可见 API 加工具 | 插件能**指令 agent**，权力最大 |
| `webui.panel` | 提供管理面板 | 面板默认只有管理员能看，插件是在往管理面输出内容 |

**故意不收进枚举的**：`messaging`（插件本来就是身份，不收发信就没有存在意义）、
`filesystem` / `process` / `network`（Core 校验不了，收进来就是假权限）。
这三类改为**约定**：插件只写自己的 `plugins/<id>/` 目录。

**执行方式**：`load` 时把 manifest 的 `permissions` 记进账本，调用点查表，不在表里 → `permission.denied`。
没有 token 签发机制——就是一次查表。

### 5.1 每个权限的**实际执行点**（2026-09-26 补齐）

> 起因：外部独立审计实测"声明了 5 个权限，只有 `webui.panel` 有检查"——
> `permissions: []` 的插件照样能唤起别人、能广播、能把工具塞进 agent 可见 API。
> 声明而不执行比没有声明更坏（读 manifest 的人会以为边界存在）。
> 下表是唯一的执行点清单；新增权限必须同时登记执行点，否则就是假权限。

| 权限 | 执行点 | 不在表里时发生什么 |
| --- | --- | --- |
| `tools.register` | `plugin.tool_registry()`（`/tools` 列表与 `/tool` 调用共用这一份） | 工具**根本不进注册表**：`/tools` 列不到、`/tool` 调不到，与"没有这些工具"不可区分 |
| `wake.others` | hub `do_send` / `send_group` 出站前（本表就是查表规则） | `wake=true` 的发信请求被拒：`permission.denied` |
| `broadcast` | 同上，判据是**收件人 > 1**（spec §5：多收件人即广播） | 多收件人发信被拒：`permission.denied` |
| `webui.panel` | `GET /admin/panel/<id>` 与 `plugin.panels()` | 403 `permission.denied`（这一条本来就在） |
| `agents.discover` | `/tool` 的 `mail_agents`（调用方带令牌自证身份，可查表） | `permission.denied` |

**两条边界（写清楚，别当成漏洞看）**：

1. 只对**已加载插件身份**生效。插件是唯一在 manifest 里声明权限的一类，所以也只有它能被查表；
   各 Adapter 代管的 agent 与 MCP 客户端是普通身份（`plugin.permissions_of()` 返回 `None`），
   不参与查表——否则"收紧插件权限"会顺手打断整个接入生态。
2. `GET /agents` **仍然免令牌**：它被 SDK 的 `resolve()` 依赖（每个 Adapter 都 vendored 了那一份
   客户端），给它上令牌会打断所有 Adapter 的模糊寻址。所以 `agents.discover` 管的是
   **`/tool` 工具面**（调用方可识别），不是那个裸 HTTP 端点。本机拓扑知识仍然是本机能力，
   不是跨用户边界——这与协议 §3.1"真正的隔离靠 OS 账户"是同一条边界。

---

## 6. Events：Core 只出 4 个

```http
在 /anp/register 时声明订阅（见 §4.1）
```

| 事件 | Core 为什么必须有它 |
| --- | --- |
| `agent.online` / `agent.offline` | 心跳 TTL 状态跃迁，Core 本来就在算。狼人杀要"谁在线能开局" |
| `message.delivered` | Core 本来就在写 `delivered_at`。插件要"8 个人都收到了吗"就数这个 |
| `message.undelivered` | Core 本来就在写"已放弃自动重投"。插件要知道哪封信死了 |

**明确不进 Core 的事件**：`task.created` / `task.completed` / `game.*`。这些是**插件概念**，
必须由插件自己用 `correlation_id` + 自定义 `kind` 表达。一个插件想通知另一个插件，
就发一封 `kind: "werewolf.phase"` 的信——**消息就是事件总线**。

这是 ANP 相对 MaiBot 最大的一处简化：没有 event bus。

---

## 7. Configuration

- Core 拥有路径：`<home>/plugins/<id>/config.json`
- `load` 时读它，原样交给插件的 `/anp/register`
- 改配置：编辑文件 → `POST /plugins/<id>/reload`（= disable + load）
- **v0.1 无 schema、无自动 UI**。MaiBot 用 pydantic + `__ui_label__` 自动渲染配置面板很优雅，
  但 ANP 插件可以是任何语言，强推 JSON Schema 校验器是过度设计 → 记 P1

---

## 8. 错误处理

插件所有端点统一返回：

```jsonc
{ "ok": true, ... }
{ "ok": false, "error": "人话", "code": "permission.denied" }
```

错误码闭集（6 个）：

| code | 什么时候 |
| --- | --- |
| `manifest.invalid` | 必填字段缺失/类型错/id 字符集不对/与既有身份重名 |
| `api.level.unsupported` | `plugin.api_level > core.CORE_API_LEVEL`，或 `anp_min` 高于当前版本 |
| `permission.denied` | 调了没声明权限的能力 |
| `endpoint.unreachable` | 插件端点连不上（含 load 时等 `/anp/register` 超时） |
| `plugin.error` | 插件自己返回 `ok:false` |
| `timeout` | 调用插件端点超时 |

**关键不变量：插件的端点永远不能替 hub 签收。** 签收只由 Core 在"信真的落地"时做。
插件的 `/anp/tool` 或 `/anp/events` 返回 200 只代表"插件收到了"，与消息投递状态无关——
否则一个乱返回 200 的插件能把别人的信伪造成已读。

---

## 9. Configuration / Error / Version 之外：安装

**v0.1 Core 只发现"已经在那里的插件"**：扫描 `<home>/plugins/*/manifest.json`，逐个 load。

安装（git clone / 解压 / 复制）是**上层工具**的事，参考 SillyTavern 的
"Git 仓库一键安装 + 启用/禁用开关"，记 P1。Core 不实现市场、不实现索引、不实现更新检查。

---

## 10. 最小 SDK

第三方只需要抄一个单文件、零依赖的 SDK：

```python
class ANPPlugin:                          # 实现这 5 个回调
    def on_register(self, ident, token, cfg, paths): ...   # POST /anp/register
    def on_wake(self, envelope): ...                       # POST /anp/wake  ← 收到信
    def on_tool(self, name, args, caller): ...             # POST /anp/tool   （可选）
    def on_event(self, type_, payload): ...                # POST /anp/events （可选）
    def on_panel(self): ...                                # GET  /anp/panel  （可选）

    # 向 Core 请求的（SDK 内部就是方向 A 那 6 个）
    def send(self, to, body, *, subject=None, kind=None, reply_to=None,
             correlation_id=None, wake=False) -> message_id
    def receive(self, *, correlation_id=None, kind=None, from_=None, peek=True, limit=20)
    def query(self, *, correlation_id=None, since=None, until=None, state=False)
    def agents(self)
```

再加两份文档（`QUICKSTART.md`：一个能跑的狼人杀最小版 + 本文）。**就这些。**
没有事件总线、没有任务对象、没有 KV、没有文件系统 API、没有进程 API、没有容器。

---

## 11. P0 / P1 / P2

### P0 — 现在必须确定（本文 §2–§8 全部）

- 根原则：插件 = 身份 + manifest + HTTP 端点
- `manifest.json` 的 5 个必填字段 + `api_level` 整数版本方案
- 端点**方向三分**（Plugin→Core / Core→Plugin / 管理面），永不混淆
- 贡献点在 **register** 阶段声明，不在 manifest
- `permissions` 5 个闭集，调用点查表
- 生命周期 4 个动词 + **进程事实三分离**
- Core 只出 4 个事件；`task.*` / `game.*` 是插件概念，用消息表达
- 错误码 6 个闭集 + 「插件不能替 hub 签收」这条不变量
- **★`/anp/register` 只发插件自己的 agent token，绝不在任何响应体里带 `hub.secret`★**

### P1 — 近期做

- 安装器（git clone + 启用/禁用开关）
- 配置 Schema 子集 → 自动 UI
- 静态贡献声明（manifest 里写 `contributes`，供"装前可审"）
- 关联线查询的细粒度过滤（`kind` / `from` 组合）
- 公告栏 / WebUI / 会话决策 / DirectBackend 搬迁成默认插件与 Adapter

### P2 — 未来再考虑

- 插件市场 / 索引
- 依赖解析（现在只做存在性检查）
- 进程内 Python 插件（降低 DX，但需要沙箱才能给第三方）
- 事件订阅的谓词过滤
- 真正的身份隔离（OS 账户/沙箱）——在此之前所有"防冒充"都只是降级

---

## 12. 与 Core 现行实现的对应关系

本规范依赖的 Core 能力，**均已实现并验证**：

| 规范依赖 | 现状 |
| --- | --- |
| 身份 / 令牌 / 心跳名册 | ✅ `core.heartbeat` + `keys.json` + `policy.may_register` |
| 信封（含 `correlation_id` / `sig`） | ✅ 第 2、3 步 |
| `messages.send` / `receive`(带过滤) / `query`(owner 作用域) | ✅ 第 2 步 |
| 三档唤醒 + 唤醒配置信任边界 | ✅ 第 1 轮安全 |
| 状态机 / 重投 / 数据生命周期 | ✅ 第 1 步 |
| `agents` 返回 `caps` / `skills` | ✅ 第 3 步 |
| 管理员令牌（面板代理用） | ✅ 第 1 轮安全 |
| `core.CORE_API_LEVEL` | ✅ 本文定稿时加入 |
| **`/plugin/register` + 注册表 + tools/events/panel 代理** | ❌ **未实现——这就是第 4 步** |
