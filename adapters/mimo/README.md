# MiMo Desktop Adapter（ANP）

把 MiMo Desktop 接入 AgentNetPort 的 **Adapter Plugin**。这是 #4.1「真实平台对抗性迁移」
的产物——**它的价值不在「514 行搬完了」，在于用一个真实平台检验了 Adapter Spec v0.1 + SDK。**

```
Adapter Spec v0.1
      ↓
Adapter SDK（sdk/anp_adapter/）
      ↓
本 Adapter Plugin
      ↓
MiMo Desktop（serve / CLI / 队列轮询）
```

## 装了什么

```
adapters/mimo/
├── manifest.json   anp.adapter.mimo（权限只用现有的 tools.register + agents.discover）
├── platform.py     ★MiMo 平台语义★——ANP 一概不知道这一层
├── plugin.py       ANP 侧：代管身份、/anp/wake、信封翻译、回信回收
├── verify.py       28 项对抗性验收 + 7 个已归类缺口
└── README.md       本文件
```

## 两个身份

```
anp.adapter.mimo    ← Adapter Plugin 自己（管理 adapter 自身）
mimo                ← 被代管的外部 agent（参与 ANP 通信）
```

两枚不同令牌，任何情况下不互换（P0-1）。

## MiMo 的三档（平台知识，ANP 不感知）

| 档 | 条件 | 信封到了之后 |
|---|---|---|
| A | `mimo serve` 可达 | POST 到 serve 的 `/mail/wake`（载荷形状与 ANP 不同，adapter 负责变换） |
| B | 有独立的 `mimo` CLI（不把 Xiaomi MiMo 桌面 GUI 当 CLI） | 拉起 `mimo run --cd <workspace> <prompt>`；仅显式设置 `AGENT_ANP_MIMO_DANGEROUSLY_BYPASS=1` 才追加 `--yolo` |
| C | 都没有 | 落「待取信摘要」到插件自己的 `data/`，由会话侧周期性收信 |

没有独立 CLI 时，已安装的 `Xiaomi MiMo.exe` 桌面程序不会被当作 headless CLI；
CLI 只输出 `[api-base]` 等诊断信息而没有回复正文时，会按执行失败处理，不会把诊断行发成 ANP 回信。

档位只决定「怎么进平台」，**不决定「Core 怎么找到我们」**——后者统一是 http wake。

## ★本次迁移修正的最关键一处★

我最初照搬旧 Mimo 的 `exec + 独立子进程`，撞上 Core 的信任校验：

```
唤醒配置未通过信任校验（exec 唤醒引用的脚本不在信箱安装目录内）
```

原因是 `install_roots()` 只认信箱**安装目录**，而插件按设计装在 `<home>/plugins/<id>/`，
真实部署下永远在外面。而旧 Mimo 那么写是因为**它不是插件**、没有可寻址端点。

正确形状：**Adapter Plugin 自己就在 `ANP_PLUGIN_PORT` 上听着 `/anp/wake`，
代管的 agent 直接指过去。**

```python
def _wake_declaration(self) -> dict:
    port = os.environ.get("ANP_PLUGIN_PORT") or "0"
    return {"kind": "http", "url": f"http://127.0.0.1:{port}/anp/wake"}
```

这一条已作为 **Spec 缺口**记录（§4/§5 没写明它）。

## 跑验收

```powershell
python adapters/mimo/verify.py
```

会自己起一个临时 hub、把 adapter **连同 SDK 一起装进 `<home>/plugins/anp.adapter.mimo/`**
（真实安装路径，不是源码目录），然后跑完整链路：

```
Core launch → /anp/register → 公开 /heartbeat 代管 mimo → 发信 → /anp/wake
→ to_native → 递平台（按档位）→ 落摘要
```

D 段把 `src/` **整个改名藏起来**再验一遍——证明 adapter 与 Core 之间只有 SDK。

## 本次发现的 7 个缺口

`verify.py` 的 F 段固定记录，每条都在迁移中真实踩到过：

| 类 | 缺口 |
|---|---|
| **Core** | `install_roots()` 不含 `<home>/plugins/` → 真实部署下 Adapter Plugin 的 exec 唤醒一律被拒（本次已用 http 绕开，缺口仍在） |
| **Core** | 插件启动预算只有 10 秒，且超时报「端点不可达」——指向错误方向（插件其实只是慢） |
| **Spec** | 没约定 adapter 的平台状态该放哪（旧 Mimo 写进信箱 home，两套系统共管一个目录） |
| **SDK** | `PluginServer` 曾静默吞掉 `on_register` 的异常 →「adapter 正常但代管 agent 不存在」最难查（已修，`/anp/health` 暴露 `register_error`） |
| **Adapter** | 旧 Mimo 的 B 档 wake 用了协议里不存在的 `{prompt}` 占位符（Core 原样透传，所以 B 档可能从未生效） |
| **Adapter** | 旧 Mimo 直接读 Core 内部（`core.read_records` / `core.agent_table`），违反 Spec §1 |
| **Adapter** | 旧 Mimo 把平台状态写进信箱 `<HOME>` 根下 |

汇总：`{"Core": 2, "Spec": 1, "SDK": 1, "Adapter": 3}`

## 刻意没做的事

按 #4 的纪律，**没有借机改 Adapter Spec v0.1，也没改 Core**。缺口只记录、归类，等确认是
规范本身错误才另开变更。旧系统（hub 8899 + 旧四家）一行未动，作为 A/B 对照组。

## 旧实现对照

| | 旧（agent-mailbox/src/mailbox_mimo.py，514 行） | 新（本目录） |
|---|---|---|
| ANP 接入 | 直接 import `mailbox_core`，读 `read_records` | 只用 SDK 的公开 API |
| 平台状态 | 写进信箱 `<HOME>/mimo-pending.*` | 插件自己的 `data/` |
| 唤醒 | `exec` + 独立脚本（撞信任校验） | http → 插件自己的 `/anp/wake` |
| 形态 | 独立进程，hub 账本登记 | Adapter Plugin，Core 拉起 |
