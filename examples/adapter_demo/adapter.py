"""ANP Adapter 参考实现 —— 骨架（基于 ANP Adapter SDK）。

这份代码的用途：**证明一个不了解 ANP 内部实现的第三方，只凭
`docs/ANP-ADAPTER-SPEC.md` + `sdk/anp_adapter/` 就能写出一个可用的 Adapter。**

三条硬约束（`verify.py` 会逐条验）：

  ① 只 import 标准库 + ANP Adapter SDK。★不 import 任何 src/ 下的 Core 模块★
  ② 平台知识只出现在 DemoPlatform 一处
  ③ 三种 delivery mode 只是 transport 差异，Adapter 主逻辑与 mode 无关

与 #2 版的区别：所有 ANP 协议面（HTTP 客户端、6 个 Core API、Envelope、
wake 载荷解析、三档 transport）都从 `sdk/anp_adapter/` 来了。
这个文件只剩下**骨架**与**平台语义**，正好是第三方该写的那两层。

跑法：
    python examples/adapter_demo/adapter.py --serve-http --port 9001 --hub http://127.0.0.1:8900
    python examples/adapter_demo/adapter.py --poll --hub ...        # queue 档主动来取
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

# 让这个文件能被单独跑（不经安装）：把仓库根的 sdk/ 加进来。
# ★注意这加的是 SDK，不是 Core★——Core 一行都不 import。
_HERE = Path(__file__).resolve().parent
_SDK = _HERE.parent.parent / "sdk"
if _SDK.is_dir() and str(_SDK) not in sys.path:
    sys.path.insert(0, str(_SDK))

from anp_adapter import (AnpClient, Envelope, ExecWake, HttpWake,   # noqa: E402
                         QueueWake, DEFAULT_RESPONSE_MODE,
                         envelope_from_env, exec_identity, free_port,
                         LocalDirectory, merge_hints, shape,
                         RESOLVED)

# 两个身份的命名约定（Spec §2）。
# ★这是两个不同的身份、两枚不同的令牌，任何情况下不互换、不共用★（P0-1）
ADAPTER_ID = "anp.adapter.demo"
AGENT_ID = "demo-01"

# 参考目录里另外几个身份：用来演示"一个 Adapter 可以代管 N 个 Agent"
# （Adapter Spec §2「派生规则」2d），以及**模糊寻址必须区分得开**这件事。
# ★它们不是被这个 demo 代管的外部平台 agent★，只是名册里的候选样本。
#
# `hints` 是**只有 Adapter 知道**的平台信息（平台短名 / 工作区标题 / 别名 / 标签）：
# 它们走 Adapter 自己的 LocalDirectory，★一个字节都不进 Core★。
#
# ★三个 live2d 候选的 hints 刻意保持同一形状★（都有别名、都有短名、都有工作区）：
# 这样"Live2D 有三个候选"这个歧义才是真的歧义，而不是被平台提示的多少制造出来的。
# 一个候选有别名、别的没有，那叫"只认识一个"——那是另一回事，不该拿来测歧义。
PEER_AGENTS = {
    "demo-live2d-a": {
        "description": "负责 Live2D 与立绘的 agent",
        "skills": ["live2d", "rigging"],
        "hints": {"aliases": ["立绘"], "handle": "dsh-11451",
                  "workspace": "Live2D 研究", "tags": ["art"]},
    },
    "demo-live2d-b": {
        "description": "Live2D 素材整理与超分",
        "skills": ["live2d", "upscale"],
        "hints": {"aliases": ["素材整理"], "handle": "mimo-0721",
                  "workspace": "Live2D 素材库", "tags": ["art"]},
    },
    "demo-live2d-c": {
        "description": "Live2D 模型绑定（psd2live）",
        "skills": ["live2d", "psd"],
        "hints": {"aliases": ["模型绑定"], "handle": "codex-id02",
                  "workspace": "Live2D 绑定台", "tags": ["art"]},
    },
    "demo-mc-01": {
        "description": "Minecraft 材质与数据包",
        "skills": ["minecraft", "datapack"],
        "hints": {"aliases": ["mc", "我的世界"], "workspace": "MC 材质包"},
    },
}


# =========================================================================== #
# 第 2 层  DemoPlatform —— ★★ 平台知识只在这里 ★★
#
# 真实世界里这一层是：把信封塞进 Codex 的会话队列、POST 进 DSH 的 web 服务、
# 或者写给某个我们从未听过的平台的客户端。参考实现用内存信箱假装是平台。
#
# 接新平台 = 改写这一个类。其余文件一个字都不用动。
# =========================================================================== #
class DemoPlatform:
    """假平台：一个能收消息、能产出回信的内存信箱。"""

    def __init__(self) -> None:
        self.inbox: list[dict] = []
        self.seen: list[dict] = []

    # ① 语义翻译：ANP 信封 → 平台原生格式
    def to_native(self, env: Envelope) -> dict:
        """★翻译★：把 ANP 信封变成平台能懂的东西。

        注意这里**只取正文和对话线**，不把 envelope 整个塞过去——
        那就是"把 ANP 的内部形状泄给平台"。真实平台通常也只接受
        "会话 id + 一段文字"。
        """
        return {"session_hint": str(env.meta.get("context_id") or ""),
                "text": env.body,
                "sender": env.from_,
                "correlation_id": env.correlation_id}

    # ② 递进平台
    def deliver(self, native: dict) -> None:
        """★递平台★：真实平台这里是"注入会话"或"POST 出去"。"""
        self.inbox.append(native)
        self.seen.append(native)

    # ③ 平台干活（★这不是 Adapter 的职责★）
    def platform_answers(self, env: Envelope) -> dict:
        """平台自己干完活，产出一条原生回信。真实世界里这一步由平台完成。"""
        native = self.to_native(env)
        self.deliver(native)
        return {"text": f"平台已处理：{native['text']}",
                "correlation_id": native["correlation_id"]}

    # ④ 语义翻译：平台原生回信 → 发回 ANP 所需的字段
    def from_native(self, answer: dict) -> dict:
        """★翻译回来★。

        ★correlation_id 必须原样带回★，否则回信落不回原对话线。
        reply_to 由 Adapter 自动补（用信封的 id），平台通常不关心它。
        """
        return {"body": str(answer.get("text") or ""),
                "correlation_id": str(answer.get("correlation_id") or ""),
                "kind": "reply"}


# =========================================================================== #
# 第 2.5 层  AdapterDirectory —— ★"用户说的那个谁到底是谁"★
#
# 这是 Agent Discovery / Address Resolution 的那一层，**整个在 Adapter 侧**：
#
#     用户 / 上层 Agent
#         ↓  "发给负责 Live2D 的"
#     AdapterDirectory.resolve()
#         ├── RESOLVED   → canonical Agent ID ──► Core 只按这个 id 投递
#         ├── AMBIGUOUS  → candidates（不猜）
#         └── NOT_FOUND
#
# 数据来源只有两处，都不需要 Core 增加任何东西：
#
#   ① Core 的公开名册 `GET /agents`（name / description / skills / caps / last_seen）
#   ② Adapter 自己知道的平台提示（平台短名 / 工作区别名 / 标签 / 同义词写法）
#
# ★② 永远留在 Adapter 进程里★：Core 不保存、不需要、也看不懂它们。
# =========================================================================== #
class AdapterDirectory:
    """模糊目标 → 候选 → canonical Agent ID。

    纯本地计算（SDK 的 `resolve()`，普通字符串算法，无 AI），一次 HTTP 只为拿名册。
    """

    def __init__(self, hub_base: str, local: LocalDirectory | None = None,
                 extra_dirs: list | None = None):
        self.hub_base = str(hub_base).rstrip("/")
        self.local = local or LocalDirectory()
        self.extra_dirs = list(extra_dirs or [])

    def hints(self) -> dict:
        return merge_hints(self.local, *self.extra_dirs)

    def resolve(self, target: str, client: AnpClient | None = None, **kw):
        """解析一个模糊目标。返回 SDK 的 `Resolution`（带 status / target / candidates）。

        `client` 可以不传——没有令牌也能读名册（`/agents` 免令牌）。
        传了就用它的 hub 地址，这样 Adapter 只需配一处。
        """
        c = client or AnpClient(self.hub_base)
        return c.resolve(target, hints=self.hints(), **kw)

    # ---- 两阶段：resolve 与 send 分开，两种失败不混（任务书 §9） ----
    def send(self, agent_id: str, body: str, client: AnpClient | None = None, **kw) -> dict:
        """按 canonical Agent ID 发信。★这一层的失败是投递失败，不是找不到人★。"""
        if not agent_id:
            return {"ok": False, "stage": "send", "error": "没有 canonical Agent ID"}
        c = client or AnpClient(self.hub_base)
        out = c.send(agent_id, body, **kw)
        out.setdefault("stage", "send")
        return out

    def resolve_and_send(self, target: str, body: str, client: AnpClient | None = None,
                         **kw) -> dict:
        """`resolve → send` 的便利写法（**不是新协议**，就是上面两步）。

        返回里两段分明：

            {"resolve": {...}, "send": {...} | None}

        AMBIGUOUS / NOT_FOUND 时 `send` 恒为 None——**找不到人就不发信**，
        绝不为"猜一个"而投递。
        """
        r = self.resolve(target, client=client)
        out = {"resolve": shape(r), "send": None}
        if r.status != RESOLVED:
            return out
        out["send"] = self.send(r.target, body, client=client, **kw)
        return out


# =========================================================================== #
# 第 3 层  Adapter —— 通用骨架，与 mode 无关
#
# mode 的差异全部在 transport 里（SDK 的 HttpWake / ExecWake / QueueWake）。
# = 职责正好是 Spec §1 那四条：托管身份 / 实现唤醒 / 语义翻译 / 回收回信。
# =========================================================================== #
class Adapter:
    def __init__(self, hub_base: str, platform: DemoPlatform, transport,
                 directory: AdapterDirectory | None = None):
        self.hub_base = str(hub_base).rstrip("/")
        self.platform = platform
        self.transport = transport
        self.directory = directory or AdapterDirectory(self.hub_base)
        # 代管身份的令牌（Spec §2「Adapter 持有它管理的所有 agent 的 agent token」）
        self._peer_keys: dict[str, str] = {}
        # 两个身份各持一枚令牌（P0-1）
        self.token_adapter = ""
        self.token_agent = ""

    # ---- Spec §1 第 1 条：托管身份 ----
    def bootstrap(self) -> dict:
        """注册 adapter 自己 + 代 agent 注册。★两个身份两枚令牌★（P0-1）。

        adapter 的身份只用于 adapter 自身的事务；agent 的身份才参与 ANP 通信。
        两次 /heartbeat 各自返回各自的 key，谁的就是谁的。
        """
        # ① adapter 自己的身份
        r1 = AnpClient(self.hub_base).bootstrap(
            ADAPTER_ID, description="参考实现的 adapter 身份",
            caps={"queue": True})
        self.token_adapter = str(r1.get("key") or "")
        # ② agent 的身份（这个才是被接入的那个）。首次不带 key → 拿令牌
        r2 = self.register_agent()
        self.token_agent = str(r2.get("key") or "")
        return {"adapter": r1, "agent": r2}

    # ---- Spec §3：注册 agent ----
    def register_agent(self) -> dict:
        """声明 agent 的 wake。★token 必须带上，否则第二次注册会被 401 挡掉★（P0-2）。"""
        c = self.agent_client()
        return c.heartbeat(AGENT_ID, token=c.token,
                           wake=self.transport.declare_wake(self.hub_base),
                           description=f"参考 agent（{self.transport.name} 档）")

    def agent_client(self) -> AnpClient:
        return AnpClient(self.hub_base, self.token_agent, AGENT_ID)

    # ---- Spec §1 第 2、3、4 条：唤醒入口 ----
    def on_wake(self, env: Envelope, side: dict | None = None) -> dict:
        """收到一封唤醒后做什么。★与 mode 无关★，三种档位走的是同一个函数。

        side['agent_key']（来自 x-mailbox-key）是**目标 Agent** 的令牌，
        不是 adapter 的（P0-4）。
        """
        side = side or {}
        native = self.platform.to_native(env)          # ① 翻译
        answer = self.platform.platform_answers(env)   # ② 递平台 + 平台干活
        reply = self._reap(answer, env)                # ③ 回收回信
        # ★必须带合法 mode，否则 Core 不签收★（v0.1.1 errata B1 / Spec §5.1.1）
        return {"ok": True, "mode": DEFAULT_RESPONSE_MODE, "to": env.to,
                "pushed_text": native["text"], "reply": reply,
                "key_present": bool(side.get("agent_key"))}

    # ---- Spec §1 第 4 条：回收回信 ----
    def _reap(self, answer: dict, env: Envelope) -> dict:
        fields = self.platform.from_native(answer)
        if not fields["body"]:
            return {"ok": False, "error": "平台没给出文字回信"}
        return self.agent_client().send(
            to=env.from_,
            body=fields["body"],
            reply_to=env.id,                       # ★必须带回★
            correlation_id=(fields["correlation_id"] or env.correlation_id),
            kind=fields["kind"],
            wake=(self.transport.name != "queue"))

    # ---- Spec §1 第 2 条的驱动面 ----
    def step(self, timeout: float = 5.0) -> dict | None:
        """跑一步：从 transport 取一封，然后统一处理。★mode 的差异只在这一行。"""
        env = self.transport.next_envelope(self.agent_client(), timeout)
        if not env:
            return None
        return self.on_wake(env)

    # ---- Spec §1 第 3 条：代 agent 发信 / 收信 ----
    def send(self, to: str, body: str, **kw) -> dict:
        return self.agent_client().send(to, body, **kw)

    def receive(self, **kw) -> dict:
        return self.agent_client().receive(**kw)

    # ---- 寻址层（Agent Discovery / Address Resolution）----
    def resolve(self, target: str, **kw):
        """模糊目标 → canonical Agent ID。**只解析，不发信。**"""
        return self.directory.resolve(target, client=self.agent_client(), **kw)

    def send_fuzzy(self, target: str, body: str, **kw) -> dict:
        """"发给负责 Live2D 的" → 解析 → 按 canonical Agent ID 投递。

        返回 `{"resolve": …, "send": …}`，两段分明：解析失败时 `send` 为 None，
        绝不会"猜一个就发"。这与 Spec §9「ok 只表示已持久化」是同一件事的两面——
        任何一段的成败都不能从另一段推导。
        """
        return self.directory.resolve_and_send(target, body,
                                               client=self.agent_client(), **kw)

    # ---- 参考目录：把候选样本登记进名册（只为演示"多个候选"的场景）----
    def register_peers(self, agents: dict | None = None, wake: dict | None = None,
                       last_used: float | None = -1.0) -> dict:
        """把 `PEER_AGENTS` 里的身份注册成 queue 档，并给 Adapter 侧补平台提示。

        ★这里动用的是**公开 API**（`POST /heartbeat`）★：Core 只看到
        "name + description + skills + wake"，看不到 Adapter 的 alias / 标签 /
        工作区标题——那些只写进 Adapter 自己的 LocalDirectory。

        `wake=None` 表示**不声明** wake：续约心跳不带 wake 时沿用旧值（Spec §3.4），
        所以"只想刷新 last_seen"的调用不会把已有端点抹掉。

        `last_used` 是**平台侧的"最近用过"**（Spec §8.6）：

            -1.0（默认）  记成"刚刚用过"（平台此刻正在跟它打交道）
            None          ★不写这条记录★（于是名册只剩 last_seen=心跳活跃度）
            <时间戳>       写这个值
        """
        table = agents or PEER_AGENTS
        done: dict[str, dict] = {}
        for name, spec in table.items():
            kw = {} if wake is None else {"wake": wake}
            r = AnpClient(self.hub_base).bootstrap(
                name, description=spec.get("description") or "",
                skills=list(spec.get("skills") or []), **kw)
            key = str(r.get("key") or "")
            # 平台侧提示：alias / handle / workspace / tags —— **只在 Adapter 进程里**
            hint = dict(spec.get("hints") or {})
            # ★"最近用过"是平台自己记的，不是 Core 名册里的 last_seen★：
            # 名册的 last_seen = 心跳活跃度（"它还活着"），跟"它最近被用过"是两件事。
            used = spec.get("last_used", last_used)
            if used is None:
                hint.pop("last_used", None)           # 只留 last_seen
            else:
                hint["last_used"] = float(time.time() if float(used) < 0 else used)
                hint["last_used_source"] = "platform"
            self.directory.local.add(name, **hint)
            done[name] = {"ok": bool(r.get("ok")), "key": key[:8] + "…" if key else ""}
            if key:
                self._peer_keys[name] = key
        return done

    def peer_client(self, name: str) -> AnpClient:
        """以某个已代管身份读/发（令牌就是它自己那枚，P0-1/P0-2）。"""
        return AnpClient(self.hub_base, self._peer_keys.get(name, ""), name)


def make_transport(name: str, hub_base: str) -> object:
    """按名字造一个 transport。第三方在这里加自己的唤醒形态。"""
    if name == "http":
        return HttpWake()
    if name == "exec":
        return ExecWake([sys.executable, str(Path(__file__).resolve()),
                         "--wake-exec", "--hub", hub_base])
    if name == "queue":
        return QueueWake()
    raise SystemExit(f"未知 mode：{name}（http / exec / queue）")


def _directory_for(args) -> AdapterDirectory:
    """给命令行用的目录：可带本地平台提示（--hints 文件）。"""
    local = LocalDirectory(args.hints) if args.hints else LocalDirectory()
    return AdapterDirectory(args.hub, local)


def _resolve_client(args) -> AnpClient:
    """解析用的身份：`/agents` 免令牌，但发信需要一枚。

    ★只用于命令行演示★：真部署里 adapter 自己的 token 由 bootstrap 拿。
    没给 key 就先 bootstrap 一次（新身份能拿到），给了就带上。
    """
    return AnpClient(args.hub, args.key or "", args.as_identity or "adapter.dir")


def main() -> int:
    ap = argparse.ArgumentParser(description="ANP Adapter 参考实现（基于 SDK）")
    ap.add_argument("--serve-http", action="store_true",
                    help="起 http 档的唤醒服务，等 Core POST /anp/wake")
    ap.add_argument("--poll", action="store_true",
                    help="queue 档：主动收一次信")
    ap.add_argument("--wake-exec", action="store_true",
                    help="exec 档：由 Core 拉起时用，不要手工跑")
    # ---- 寻址：模糊目标 → canonical Agent ID（Agent Discovery）----
    ap.add_argument("--resolve", metavar="TARGET",
                    help='解析一个模糊目标，例如 "负责 Live2D 的"（只解析，不发信）')
    ap.add_argument("--to", metavar="TARGET",
                    help="解析并发送：目标可以是 canonical ID，也可以是模糊说法")
    ap.add_argument("--body", default="", help="配合 --to 的正文")
    ap.add_argument("--hints", help="本地平台提示（alias/工作区标题/短名）JSON 文件")
    ap.add_argument("--as-identity", default="adapter.dir",
                    help="--to 发信时用的发信方身份（默认 adapter.dir）")
    ap.add_argument("--key", default=os.environ.get("AGENT_ANP_KEY") or "",
                    help="发信身份的令牌；不给则先 bootstrap 一次")
    ap.add_argument("--hub", default=os.environ.get("AGENT_ANP_HUB")
                    or "http://127.0.0.1:8900")
    ap.add_argument("--port", type=int, default=0)
    args = ap.parse_args()

    if args.resolve:
        d = _directory_for(args)
        c = _resolve_client(args)
        # 解析不需要令牌；但给了身份就想让它存在，方便下一句直接发信
        print(json.dumps(shape(d.resolve(args.resolve, client=c)), ensure_ascii=False))
        return 0

    if args.to:
        d = _directory_for(args)
        c = _resolve_client(args)
        if not c.token:
            _r = c.bootstrap(args.as_identity, description="参考实现的解析/发信身份")
            c.token = str(_r.get("key") or c.token)
        body = args.body or "（空正文）"
        out = d.resolve_and_send(args.to, body, client=c)
        print(json.dumps(out, ensure_ascii=False))
        # 解析没成功 = 没发信（退出码 2，与"投递失败"区分）
        return 0 if out["resolve"]["status"] == RESOLVED and (out["send"] or {}).get("ok") \
            else (2 if out["resolve"]["status"] != RESOLVED else 1)

    if args.wake_exec:
        # exec 档：Core 拉起我们。信封与令牌全在环境变量里（Spec §5.2）。
        # ★AGENT_ANP_SELF_KEY 是目标 Agent 的令牌，不是 adapter 的★（P0-4）
        env = envelope_from_env()
        if env is None:
            print("AGENT_ANP_WAKE_JSON 为空或不是信封", file=sys.stderr)
            return 1
        me = exec_identity()
        a = Adapter(args.hub, DemoPlatform(), make_transport("exec", args.hub))
        a.token_agent = me["key"]
        print(json.dumps(a.on_wake(env, {"agent_key": me["key"]}),
                         ensure_ascii=False))
        return 0

    if args.serve_http:
        port = args.port or free_port()
        t = make_transport("http", args.hub)
        a = Adapter(args.hub, DemoPlatform(), t)
        t.start(port)                       # ★先起服务：declare_wake 要用端口★
        a.bootstrap()                       # 然后才注册（wake 指向刚起的端口）
        print(json.dumps({"ok": True, "listening": t.port, "agent": AGENT_ID},
                         ensure_ascii=False), flush=True)
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
        finally:
            t.stop()
        return 0

    if args.poll:
        a = Adapter(args.hub, DemoPlatform(), make_transport("queue", args.hub))
        a.bootstrap()
        got = a.receive(peek=False, unread_only=True)
        for env in got.get("messages") or []:
            a.on_wake(Envelope(env))
        print(json.dumps({"ok": True, "received": len(got.get("messages") or [])},
                         ensure_ascii=False))
        return 0

    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
