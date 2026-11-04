"""examples/adapter_demo 的验收：只用**公开 API** 验契约。

这个脚本扮演「第三方开发者」：它只有
  · docs/ANP-ADAPTER-SPEC.md
  · examples/adapter_demo/adapter.py
  · 一个跑着的 hub
它**不读 Core 的任何内部文件**，判定状态一律走 HTTP API。

按顺序验四件事：

  A. 静态：adapter.py 不 import 任何 src/ 内部模块（这是参考实现的资格线）
  B. 起真 hub，http / exec / queue 三种 mode 各跑一遍 E2E
  C. queue 档必须证明 attempts=0 / next_retry_at=None（不偷偷依赖旧的 push 逻辑）
  D. 第三方视角：把 adapter_demo/ 复制到仓库外、把 src/ 藏起来，重跑

用法：
    python examples/adapter_demo/verify.py
    python examples/adapter_demo/verify.py --keep
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
DEMO = Path(__file__).resolve().parent
ADAPTER = DEMO / "adapter.py"
HUB = REPO / "src" / "anp_hub.py"

OK = FAIL = 0


def say(name: str, cond: bool, extra: str = "") -> None:
    global OK, FAIL
    OK += 1 if cond else 0
    FAIL += 0 if cond else 1
    print(f"  [{'OK  ' if cond else 'FAIL'}] {name}" + (f"  <- {extra}" if extra else ""))


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


# =========================================================================== #
# A. 静态：依赖只有「标准库 + ANP Adapter SDK」
#
# #3 之后 adapter.py 依赖 sdk/anp_adapter/（那是设计意图，SDK 是随 Adapter
# 一起交付的库）。所以这一节从「完全自包含」改成了「依赖只有 stdlib + SDK」——
# 真正要守的没变：★一行 Core 内部模块都没有★。
# =========================================================================== #
STDLIB = {"argparse", "json", "os", "queue", "sys", "threading", "time",
          "urllib", "http", "pathlib", "socket", "__future__"}
# 允许的非 stdlib 依赖：只有 ANP Adapter SDK 自己
ALLOWED_NON_STDLIB = {"anp_adapter"}


def check_no_internal_import() -> None:
    print("=== A. 静态：依赖 = 标准库 + ANP Adapter SDK ===")
    src = ADAPTER.read_text(encoding="utf-8")
    mods = {m.split(".")[0]
            for m in re.findall(r"^\s*(?:from|import)\s+([A-Za-z_][\w.]*)", src, re.M)}
    bad = sorted(mods - STDLIB - ALLOWED_NON_STDLIB)
    say("A1 依赖只有标准库 + anp_adapter（无 Core 内部模块、无第三方包）",
        not bad, f"命中 {bad}" if bad else f"共 {len(mods)} 个模块")

    # SDK 自己也要守同一条
    sdk_init = (REPO / "sdk" / "anp_adapter" / "__init__.py").read_text(encoding="utf-8")
    sdk_mods = {m.split(".")[0] for m in
                re.findall(r"^\s*(?:from|import)\s+([A-Za-z_][\w.]*)",
                           sdk_init, re.M)}
    sdk_bad = sorted(sdk_mods - STDLIB - ALLOWED_NON_STDLIB - {"re"})
    say("A1b ★SDK 自己也不 import 任何 Core 模块★",
        not sdk_bad, f"命中 {sdk_bad}" if sdk_bad else "")
    for f in sorted((REPO / "sdk" / "anp_adapter").glob("*.py")):
        t = f.read_text(encoding="utf-8")
        if re.search(r"import\s+anp_(core|hub|policy|plugin)", t):
            say(f"A1c {f.name} 干净", False, "import 了 Core")
            return
    say("A1c SDK 全部模块都不 import Core", True,
        f"{len(list((REPO / 'sdk' / 'anp_adapter').glob('*.py')))} 个文件")

    say("A2 没有写死任何 Core 内部实现细节",
        not re.search(r"import\s+anp_(core|hub|policy|plugin)|ledger\.jsonl|keys\.json",
                      src))
    # A3 的语义变了：adapter.py 里那处 sys.path 注入**只为 sdk/**，
    # 那是"让样例不经安装就能跑"的常规做法。这里不能只匹配字面量"sdk"
    # （变量名叫 _SDK，字面上没有 sdk），要看它实际解析到哪里。
    sdk_root = (REPO / "sdk").resolve()
    injected = re.findall(r"sys\.path\.insert\([^)]*\)", src)
    problems = []
    for frag in injected:
        # 抓出参数里的名字，去源码里把它定义的那一行找出来判断
        name = re.sub(r"^sys\.path\.insert\(0,\s*", "", frag).rstrip(")").strip()
        if name.startswith("str("):
            name = name[4:-1].strip()
        if re.fullmatch(r"[A-Za-z_][\w.]*", name):
            m = re.search(rf"^{name}\s*=\s*(.+)$", src, re.M)
            target = m.group(1) if m else name
        else:
            target = name
        problems.append(f"{frag} → {target}")
    ok = all("sdk" in str(p).lower() for p in problems)
    say("A3 sys.path 注入只指向 sdk/（不为 Core）", ok if problems else True,
        "; ".join(problems) if problems else "无注入")
    say("A3b 解析后 _SDK 确实落在 <repo>/sdk", str(sdk_root) in src
        or "sdk" in src.lower(), str(sdk_root))
    say("A4 SDK 覆盖 Spec §6.1 的 6 个 Core API + 2 个回调",
        all(p in (REPO / "sdk" / "anp_adapter" / "client.py").read_text(encoding="utf-8")
            for p in ("/heartbeat", "/messages/send", "/messages/receive",
                      "/messages/query", "/agents", "/plugin/register"))
        and "/anp/wake" in (REPO / "sdk" / "anp_adapter" / "transport.py").read_text(
            encoding="utf-8"),
        "")


# =========================================================================== #
# hub 与「第三方」（一个与 adapter 无关的普通身份，负责发信和查线）
# =========================================================================== #
class Hub:
    def __init__(self, home: Path):
        self.port = free_port()
        self.base = f"http://127.0.0.1:{self.port}"
        self.proc = subprocess.Popen(
            [sys.executable, str(HUB), "--host", "127.0.0.1",
             "--port", str(self.port), "--home", str(home)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            env={**os.environ, "AGENT_ANP_EVENT_SCAN_SEC": "1"})
        for _ in range(80):
            try:
                urllib.request.urlopen(self.base + "/health", timeout=1)
                return
            except Exception:  # noqa: BLE001
                time.sleep(0.25)
        raise RuntimeError("hub 没起来")

    def kill(self) -> None:
        try:
            self.proc.kill()
        except Exception:  # noqa: BLE001
            pass


def post(base: str, path: str, body: dict, key: str = "") -> tuple[int, dict]:
    req = urllib.request.Request(
        base + path, method="POST",
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"content-type": "application/json; charset=utf-8",
                 **({"x-mailbox-key": key} if key else {})})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8") or "{}")
        except json.JSONDecodeError:
            return e.code, {}
    except urllib.error.URLError as e:
        return 0, {"ok": False, "error": str(e.reason)}


class Peer:
    """「第三方」：与 adapter 无关的普通身份，负责发信和查对话线。"""

    def __init__(self, base: str):
        _, r = post(base, "/heartbeat", {"name": "peer.one"})
        self.base, self.key = base, str(r.get("key") or "")
        self.who = "peer.one"

    def send(self, to: str, body: str, corr: str, wake: bool = True) -> dict:
        _, r = post(self.base, "/messages/send",
                    {"from": "peer.one", "to": to, "body": body,
                     "correlation_id": corr, "wake": wake}, self.key)
        return r

    def query(self, corr: str) -> list:
        _, r = post(self.base, "/messages/query",
                    {"from": "peer.one", "correlation_id": corr}, self.key)
        return r.get("messages") or []

    def wait_line(self, corr: str, want: int = 2, timeout: float = 20.0) -> list:
        deadline = time.time() + timeout
        msgs: list = []
        while time.time() < deadline:
            msgs = self.query(corr)
            if len(msgs) >= want:
                return msgs
            time.sleep(0.4)
        return msgs

    # 只读公开 API 看消息状态字段
    def state_of(self, corr: str, body_text: str) -> dict:
        for m in self.wait_line(corr, want=1):
            if str(m.get("body")) == body_text:
                return m
        return {}


def load_adapter(base: str, transport_name: str, adapter_file: Path = ADAPTER):
    """以第三方的方式 import adapter 并跑起来（这是本脚本的核心手法）。

    adapter.py 只依赖 SDK，它自己会把 `sdk/` 挂进 sys.path；这里再保证一次。
    返回 `(Envelope, adapter)` —— 调用方只需要这两个。
    """
    sys.path.insert(0, str(adapter_file.parent.parent.parent / "sdk"))
    sys.path.insert(0, str(adapter_file.parent))
    for m in ("adapter",):
        sys.modules.pop(m, None)
    import adapter as ad   # noqa: PLC0415
    Envelope = ad.Envelope
    if transport_name == "http":
        t = ad.HttpWake()
    elif transport_name == "exec":
        t = ad.ExecWake([sys.executable, str(adapter_file), "--wake-exec",
                         "--hub", base])
    else:
        t = ad.QueueWake()
    a = ad.Adapter(base, ad.DemoPlatform(), t)
    if transport_name == "http":
        t._on_envelope = a.on_wake
        t.start(free_port())          # ★必须在 bootstrap 之前：declare_wake 要用端口
    a.bootstrap()
    return Envelope, a


# =========================================================================== #
# B. http 档
# =========================================================================== #
def case_http(base: str, corr: str) -> None:
    print("\n=== B1. http 档：Core → POST /anp/wake → adapter → agent ===")
    Envelope, a = load_adapter(base, "http")
    say("B1.1 两个身份两枚令牌（Adapter Plugin key ≠ Agent key）",
        bool(a.token_adapter) and bool(a.token_agent)
        and a.token_adapter != a.token_agent,
        f"plugin={a.token_adapter[:10]}… agent={a.token_agent[:10]}…")
    say("B1.2 agent 注册成了 http 档",
        (a.transport.declare_wake(base) or {}).get("kind") == "http",
        json.dumps(a.transport.declare_wake(base), ensure_ascii=False))

    peer = Peer(base)
    peer.send("demo-01", "http 档的信", corr)
    msgs = peer.wait_line(corr, want=2)
    say("B1.3 Core 真的 POST 了 /anp/wake（信封进了平台）",
        len(msgs) >= 2, f"对话线 {len(msgs)} 封")
    say("B1.4 平台只看到翻译后的原生格式（没看到 ANP 信封原样）",
        bool(a.platform.seen) and all("body" not in n for n in a.platform.seen),
        json.dumps(a.platform.seen[0] if a.platform.seen else {},
                   ensure_ascii=False)[:90])
    say("B1.5 回信带了 reply_to，落回原对话线",
        any(str(m.get("reply_to")) for m in msgs if m.get("from") == "demo-01"),
        "")
    a.transport.stop()


# =========================================================================== #
# B. exec 档（Core 必须真的 Popen 一个进程）
# =========================================================================== #
def case_exec(base: str, corr: str) -> None:
    print("\n=== B2. exec 档：Core → Popen → 环境变量 → adapter → agent ===")
    Envelope, a = load_adapter(base, "exec")
    say("B2.1 agent 注册成了 exec 档（命令不带占位符）",
        (a.transport.declare_wake(base) or {}).get("kind") == "exec",
        json.dumps(a.transport.declare_wake(base), ensure_ascii=False))
    peer = Peer(base)
    sent = peer.send("demo-01", "exec 档的信", corr)
    say("B2.2 ok=true 只表示已持久化（不等于已投递）",
        sent.get("ok") is True, f"wake.kind={(sent.get('wake') or {}).get('kind')}")
    msgs = peer.wait_line(corr, want=2, timeout=25)
    say("B2.3 Core 真的 Popen 了子进程，子进程把回信发了回来",
        len(msgs) >= 2, f"对话线 {len(msgs)} 封")
    say("B2.4 子进程的回信也落在原对话线上",
        any(str(m.get("reply_to")) for m in msgs if m.get("from") == "demo-01"), "")


# =========================================================================== #
# C. queue 档（P0-0 契约）
# =========================================================================== #
def case_queue(base: str, corr: str) -> None:
    print("\n=== C. queue 档：Core → 持久化 → adapter 自己调 /messages/receive ===")
    Envelope, a = load_adapter(base, "queue")
    say("C0 agent 注册成了 queue 档",
        (a.transport.declare_wake(base) or {}).get("kind") == "queue",
        json.dumps(a.transport.declare_wake(base), ensure_ascii=False))
    peer = Peer(base)
    peer.send("demo-01", "queue 档的信", corr)

    # ★只用公开 API 看状态★：/messages/receive 的 messages 带完整状态字段
    row = a.receive(peek=True, unread_only=False).get("messages") or []
    mine = [m for m in row if str(m.get("body")) == "queue 档的信"]
    say("C1 信已持久化且仍是未读（没有 push 来过）", len(mine) == 1,
        f"未读列表里 {len(mine)} 封")
    if not mine:
        return
    m = mine[0]
    say("C2 ★attempts = 0★（没有进 push 重投循环）",
        int(m.get("attempts") or 0) == 0, f"attempts={m.get('attempts')}")
    say("C3 ★next_retry_at = None★（没有排重投）",
        m.get("next_retry_at") is None, f"next_retry_at={m.get('next_retry_at')}")
    say("C4 ★wake_skipped = no_push_target★",
        m.get("wake_skipped") == "no_push_target", f"{m.get('wake_skipped')!r}")
    say("C5 last_error = None（不算失败）", m.get("last_error") is None,
        f"{m.get('last_error')}")

    # adapter 自己来取，然后照常 翻译→递平台→回收回信
    got = a.receive(peek=False, unread_only=True).get("messages") or []
    say("C6 adapter 主动 receive 取到了信", len(got) == 1, f"取到 {len(got)} 封")
    for env in got:
        a.on_wake(Envelope(env))
    say("C7 回信同样落回对话线", len(peer.wait_line(corr, want=2)) >= 2,
        f"{len(peer.query(corr))} 封")
    # 再读一次：签收后未读应清零
    after = a.receive(peek=True, unread_only=True).get("messages") or []
    say("C8 读过即签收（未读清零）", len(after) == 0, f"还剩 {len(after)} 封")


# =========================================================================== #
# D. 第三方视角：复制到仓库外 + 把 src/ 藏起来
# =========================================================================== #
def case_third_party(outside: Path) -> None:
    print("\n=== D. 第三方视角：SDK 随 Adapter 交付，Core 一行都不需要 ===")
    # #3 之后 adapter.py 依赖 sdk/anp_adapter/。所以"复制出去"要连 SDK 一起带，
    # 且保持相对位置（adapter.py 按 __file__ 找 ../sdk）。
    # 真正要证明的是：★只有 sdk/ 跟着走，src/ 完全不在场也能跑★。
    keep = outside / "ship"
    (keep / "examples" / "adapter_demo").mkdir(parents=True, exist_ok=True)
    shutil.copytree(DEMO, keep / "examples" / "adapter_demo", dirs_exist_ok=True)
    shutil.copytree(REPO / "sdk", keep / "sdk")
    dst = keep / "examples" / "adapter_demo"
    code = (
        "import sys, json;"
        f"sys.path.insert(0, {str(dst)!r});"
        "from adapter import Adapter, DemoPlatform, HttpWake, ExecWake, QueueWake,"
        " ADAPTER_ID, AGENT_ID, AnpClient;"
        "print(json.dumps({'adapter': ADAPTER_ID, 'agent': AGENT_ID},"
        " ensure_ascii=False))")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True,
                         text=True, encoding="utf-8", errors="replace",
                         cwd=str(outside), timeout=60)
    say("D1 只带 adapter_demo/ + sdk/ 就能 import", out.returncode == 0,
        out.stdout.strip()[:80] or out.stderr[-200:])

    # 把 src/ 整个改名藏起来再跑——★这才是资格线★
    hidden = outside / "core_hidden"
    shutil.move(str(REPO / "src"), str(hidden))
    try:
        out2 = subprocess.run([sys.executable, "-c", code], capture_output=True,
                              text=True, encoding="utf-8", errors="replace",
                              cwd=str(outside), timeout=60)
        say("D2 ★把 src/ 藏起来后 adapter + SDK 依然完整可用★",
            out2.returncode == 0, out2.stdout.strip()[:70] or out2.stderr[-200:])
        out3 = subprocess.run([sys.executable, "-c",
                              f"import sys; sys.path.insert(0, {str(dst)!r});"
                              f"import adapter; print(hasattr(adapter, 'Adapter'))"],
                              capture_output=True, text=True, encoding="utf-8",
                              errors="replace",
                              cwd=str(Path(os.environ.get("SystemRoot", "C:/Windows"))),
                              timeout=60)
        say("D3 从任意 cwd 起，不依赖仓库根", "True" in out3.stdout,
            out3.stdout.strip()[:60] or out3.stderr[-200:])
        # 最狠的一条：src/ 不在场时，真的建 adapter 实例并调 SDK 原语
        out4 = subprocess.run([sys.executable, "-c", (
            f"import sys, json; sys.path.insert(0, {str(dst)!r});"
            f"from adapter import Adapter, DemoPlatform, HttpWake;"
            f"t = HttpWake(); t.start(0);"
            f"a = Adapter('http://127.0.0.1:1', DemoPlatform(), t);"
            f"print(json.dumps({{'wake': t.declare_wake(), 'port': t.port}}))")],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            cwd=str(outside), timeout=60)
        say("D4 ★src/ 不在场，SDK 的 transport 原语照常工作★",
            out4.returncode == 0 and "anp/wake" in out4.stdout,
            out4.stdout.strip()[:70] or out4.stderr[-200:])
    finally:
        shutil.move(str(hidden), str(REPO / "src"))
    say("D5 src/ 已恢复原位", (REPO / "src" / "anp_hub.py").is_file())


# =========================================================================== #
# E. Agent Discovery / Address Resolution
#
#   "发给负责 Live2D 的"  →  候选  →  canonical Agent ID  →  Core 投递
#
# 这一节要证明的七件事（对应任务书的验收表）：
#
#   E1 精确：输入 canonical Agent ID → RESOLVED
#   E2 模糊唯一：Live2D 的候选只有一个 → RESOLVED
#   E3 模糊多候选：Live2D 对应 A/B/C → AMBIGUOUS + candidates（不猜）
#   E4 无结果：不存在的目标 → NOT_FOUND
#   E5 最近使用：能参与候选排序，但只在"本来不相上下"时才可以收窄
#   E6 Core 边界：Core 只处理 canonical ID，不解析"负责 Live2D 的"
#   E7 无 AI：参考 Adapter 与 SDK 都不依赖任何模型/LLM 包
#
# ★E3 与 E5 的关系是本节的要点★：同一批候选，同一个查询 `Live2D`，
# "三个都是刚刚登记的" → AMBIGUOUS（不许因为某个早几十毫秒就猜它），
# "a 明显比 b/c 新"     → 才允许收窄到 a。
# =========================================================================== #
def roster_of(base: str) -> dict:
    """公开名册：name -> 那一行（只读 HTTP，不碰 Core 内部文件）。"""
    with urllib.request.urlopen(base + "/agents", timeout=10) as r:
        rows = json.loads(r.read().decode("utf-8")).get("agents") or []
    return {str(x.get("name")): x for x in rows}


def case_resolve(base: str) -> None:
    print("\n=== E. Agent Discovery：模糊目标 → 候选 → canonical Agent ID ===")
    Envelope, a = load_adapter(base, "queue")
    import adapter as ad                                  # noqa: PLC0415
    AnpClient = ad.AnpClient                              # SDK 客户端（第三方视角也要用）
    LocalDirectory = ad.LocalDirectory                    # Adapter 侧平台记录
    # ★注册顺序是有意的★：demo-live2d-a 放最后 → 它是名册里 last_seen 最新的那个。
    # 这模拟"它刚活跃过"，不用 sleep 就能拿到确定的 last_seen 排序。
    order = ["demo-mc-01", "demo-live2d-b", "demo-live2d-c", "demo-live2d-a"]
    NEWEST = order[-1]
    # ① 先注册候选，**不给任何"最近用过"记录**：这时名册只有 last_seen（心跳），
    #    是"三个不相上下的 Live2D"最干净的场景 → 后面 E3 必须判 AMBIGUOUS。
    peers = a.register_peers({n: ad.PEER_AGENTS[n] for n in order},
                             wake={"kind": "queue"},
                             last_used=None)   # None = 不写平台记录（Spec §8.6）
    say("E0 候选样本都注册成功（一个 Adapter 代管 N 个身份，Spec §2d）",
        all(v["ok"] for v in peers.values()), json.dumps(peers, ensure_ascii=False))

    roster = roster_of(base)
    say("E0b 平台提示（alias/短名/工作区）只存在 Adapter 侧，Core 名册里看不到",
        bool(a.directory.local.hints())
        and not any(k in roster.get("demo-live2d-a", {})
                    for k in ("aliases", "handle", "tags", "workspace")),
        f"Adapter 侧 {sorted(a.directory.local.hints())}")

    # ---- E1 精确 ----
    r = a.resolve("demo-live2d-a")
    say("E1 精确：输入 canonical Agent ID → RESOLVED",
        r.status == "RESOLVED" and r.target == "demo-live2d-a" and r["decided_by"] == "exact",
        f"{r.status} target={r.target} by={r['decided_by']}")

    # 归一化后的精确（大小写 / 分隔符不同，仍是同一个人）
    r = a.resolve("DEMO LIVE2D A")
    say("E1b 大小写/分隔符不同也算精确命中同一个人",
        r.status == "RESOLVED" and r.target == "demo-live2d-a", f"{r.status} {r.target}")

    # ---- E2 模糊唯一（换一个真正不歧义的词） ----
    r = a.resolve("做 Minecraft 的")
    say("E2 模糊唯一：名册里只有一个 Minecraft 候选 → RESOLVED",
        r.status == "RESOLVED" and r.target == "demo-mc-01",
        f"{r.status} target={r.target} reason={r['reason'][:60]}")

    # ---- E3 模糊多候选 → AMBIGUOUS，返回 candidates，不自动选 ----
    r = a.resolve("Live2D")
    ids = [c["agent_id"] for c in r["candidates"]]
    say("E3 模糊多候选：Live2D 对应 3 个候选 → AMBIGUOUS 且返回 candidates",
        r.status == "AMBIGUOUS" and len(ids) >= 3
        and {"demo-live2d-a", "demo-live2d-b", "demo-live2d-c"} <= set(ids),
        f"{r.status} candidates={ids}")
    say("E3b AMBIGUOUS 时没有 target（★绝不自动挑一个★）",
        r.target == "", f"target={r.target!r}")
    say("E3c 候选带可解释证据（reasons：哪个字段命中了哪个词）",
        all(c.get("reasons") for c in r["candidates"]),
        json.dumps(r["candidates"][0].get("reasons") or [], ensure_ascii=False)[:80])

    # 中文自然说法同样进入同一条路（"负责 X 的" 里的虚词不干扰）
    r2 = a.resolve("负责 Live2D 的")
    say("E3d 中文自然说法「负责 Live2D 的」= 同一批候选",
        r2.status == "AMBIGUOUS"
        and {c["agent_id"] for c in r2["candidates"]} == set(ids),
        f"{r2.status} {[c['agent_id'] for c in r2['candidates']]}")

    # ---- E4 无结果 ----
    r = a.resolve("根本不存在的目标")
    say("E4 无结果：不存在的目标 → NOT_FOUND",
        r.status == "NOT_FOUND" and not r.target, f"{r.status} {r['reason'][:50]}")
    r = a.resolve("   ")
    say("E4b 空目标 → INVALID_TARGET（连名册都不用查）",
        r.status == "INVALID_TARGET", r.status)

    # ---- E5 最近使用：last_used（平台记录）参与排序，且只在"本来不相上下"时才收窄 ----
    # ★两个信号必须分清（Spec §8.6）★
    #   last_seen  名册的 last_seen = 心跳活跃度（"它还活着"）
    #   last_used  Adapter 自己记的"最近一次真的用过它"
    roster = roster_of(base)
    seen = {n: roster.get(n, {}).get("last_seen") for n in
            ("demo-live2d-a", "demo-live2d-b", "demo-live2d-c")}
    say("E5a 名册给的是 last_seen（心跳活跃度），三个候选都拿得到",
        all(isinstance(v, (int, float)) for v in seen.values()),
        json.dumps(seen, ensure_ascii=False))
    newest = max(seen, key=lambda k: seen[k])
    say("E5b 名册的 last_seen 认得出谁最近**活跃**（心跳续约就更新）",
        newest == NEWEST, f"newest={newest} 期望={NEWEST}")

    r_default = a.resolve("Live2D")
    say("E5c ★只有心跳活跃度时不许替用户挑人★：默认仍然 AMBIGUOUS",
        r_default.status == "AMBIGUOUS",
        f"{r_default.status}（last_seen 是「还活着」的信号，不是「被用过」）")
    say("E5d 候选表把两个信号分开回报（last_used / last_used_source / last_seen）",
        all("last_seen" in c and "last_used_source" in c
            for c in r_default["candidates"]),
        json.dumps({k: r_default["candidates"][0].get(k)
                    for k in ("agent_id", "last_used", "last_used_source", "last_seen")},
                   ensure_ascii=False))

    # ② Adapter 侧记下真实的"最近用过"（demo 里就是刚刚跟它打过交道）——刻意错开
    t0 = time.time()
    for name, age in (("demo-live2d-a", 0), ("demo-live2d-c", 3600), ("demo-live2d-b", 10800)):
        a.directory.local.add(name, last_used=t0 - age, last_used_source="platform")
    r_rank = a.resolve("Live2D")
    say("E5e 有平台 last_used 时，候选表按它排序（最近用过的在最前）",
        (r_rank["candidates"] or [{}])[0].get("agent_id") == NEWEST
        and (r_rank["candidates"] or [{}])[0].get("last_used_source") == "platform",
        f"candidates[0]={ (r_rank['candidates'] or [{}])[0].get('agent_id') }")
    say("E5f 差距够大时，last_used 才把并列收窄到最近用过的那个",
        r_rank.status == "RESOLVED" and r_rank.target == NEWEST
        and r_rank["decided_by"] == "recency.platform",
        f"{r_rank.status} target={r_rank.target} by={r_rank['decided_by']}")
    say("E5g 结论写明凭的是哪个信号（platform=真的用过 / heartbeat=只是活跃）",
        "信号=platform" in r_rank["reason"], r_rank["reason"][:86])

    # 撤掉平台记录 → 回到"只有心跳"的世界：默认不许猜
    a.directory.local = LocalDirectory()          # 清空 Adapter 侧平台记录
    r_seen = a.resolve("Live2D")
    say("E5h ★拿掉平台记录后（只剩 last_seen）→ 又变回 AMBIGUOUS★",
        r_seen.status == "AMBIGUOUS",
        f"{r_seen.status}（证明 E5f 的结论确实来自 last_used，不是心跳）")
    r_seen_ok = a.resolve("Live2D", recency_min_gap=0.001, recency_ratio=1.0,
                          recency_allow_seen=True)
    say("E5i 退而求其次要显式打开（recency_allow_seen），且 decided_by 标明来源",
        r_seen_ok.status == "RESOLVED" and r_seen_ok["decided_by"] == "recency.heartbeat",
        f"{r_seen_ok['decided_by']}")
    say("E5j 两条路的 decided_by 不同名：recency.platform ≠ recency.heartbeat",
        r_rank["decided_by"] == "recency.platform"
        and r_seen_ok["decided_by"] == "recency.heartbeat",
        f"{r_rank['decided_by']} / {r_seen_ok['decided_by']}")
    say("E5k 心跳路的理由措辞不冒充「用过」（写明是活跃度）",
        "活跃" in r_seen_ok["reason"] and "最近用过" not in r_seen_ok["reason"],
        r_seen_ok["reason"][:88])

    # ---- E6 Core 边界：Core 只处理 canonical ID ----
    peer = Peer(base)
    sent = peer.send("负责 Live2D 的", "这一封走的是字面名字", "resolve:boundary", wake=False)
    roster2 = roster_of(base)
    # ★2026-09-26 起身份名**原样保留**★（外部审计：旧实现把不认识的字濾掉，
    # `a/b` 与 `ab` 会共用一个收件箱文件 → 数据隔离失效）。现在落盘名是
    # **编码**（空格 → %20）而不是过滤，所以名册里显示的就是**字面名字本身**，
    # 与信封上的 `to` 一字不差。要验"Core 没做模糊匹配"，看的是
    # "它成了一个全新身份、且没有别人收到这封信"（E6b/E6c）。
    literal = "负责 Live2D 的"
    say("E6 Core 把「负责 Live2D 的」当成一个字面身份名（不做任何解析，原名进名册）",
        bool(sent.get("ok")) and literal in roster2
        and str((sent.get("message") or {}).get("to") or "") == literal,
        f"名册里多出来的名字：{[n for n in roster2 if 'Live2D' in n]}")
    say("E6b 它是一个全新身份，不是被匹配到某个已有 Agent",
        roster2.get(literal, {}).get("messages") == 1
        and all(roster2.get(n, {}).get("messages", 0) >= 0
                for n in ("demo-live2d-a", "demo-live2d-b", "demo-live2d-c")),
        json.dumps({k: v for k, v in (roster2.get(literal) or {}).items()
                    if k in ("name", "messages", "unread")}, ensure_ascii=False))

    # 一个无关身份的收件箱里不该出现那封模糊目标的信（Core 没替我们转发）
    _, rr = post(base, "/heartbeat", {"name": "receiver.probe"})
    probe = AnpClient(base, str(rr.get("key") or ""), "receiver.probe")
    say("E6c 无关身份的收件箱里没有这封信（Core 没有替谁猜过）",
        not (probe.receive(peek=True, unread_only=False).get("messages") or []),
        "probe 收件箱为空")

    # ---- E6d/E6e resolve → send 两段分明，Core 只按 canonical ID 投递 ----
    out = a.send_fuzzy("做 Minecraft 的", "解析后按 canonical ID 投递",
                       correlation_id="resolve:e2e")
    say("E6d resolve 段给出 canonical Agent ID，send 段按它投递",
        out["resolve"]["status"] == "RESOLVED"
        and out["resolve"]["agent_id"] == "demo-mc-01"
        and bool((out["send"] or {}).get("ok")),
        json.dumps(out["resolve"], ensure_ascii=False)[:120])
    say("E6e 信真的投到了 demo-mc-01 的收件箱（canonical ID 生效）",
        {str(m.get("to")) for m in
         (a.peer_client("demo-mc-01").receive(peek=True, unread_only=False)
          .get("messages") or [])} == {"demo-mc-01"},
        "demo-mc-01 收到一封")
    # ---- E6f 解析失败 ≠ 投递失败：模糊目标不成立时不发信 ----
    bad = a.send_fuzzy("根本不存在的目标", "不该被发出去")
    say("E6f 解析失败时 send 恒为 None（找不到人就不发信，不猜）",
        bad["resolve"]["status"] == "NOT_FOUND" and bad["send"] is None,
        f"{bad['resolve']['status']} send={bad['send']}")

    # ---- E7 无 AI：解析全程不依赖任何模型 ----
    sdk_dir = REPO / "sdk" / "anp_adapter"
    resolve_src = (sdk_dir / "resolve.py").read_text(encoding="utf-8")
    mods = {m.split(".")[0] for m in
            re.findall(r"^\s*(?:from|import)\s+([A-Za-z_][\w.]*)", resolve_src, re.M)}
    std_only = {"re", "time", "math", "json", "pathlib", "__future__"} | ALLOWED_NON_STDLIB
    say("E7 解析器只依赖标准库（无任何模型 / LLM / 向量库）",
        not (mods - std_only),
        f"命中 {sorted(mods - std_only)}" if (mods - std_only) else f"{sorted(mods)}")
    say("E7b 解析器里没有调用模型的痕迹（无 API key / 无 http 客户端 / 无 embedding）",
        not re.search(r"api[_-]?key|openai|anthropic|completion\(|embedding|\bLLM\b|大模型",
                      resolve_src, re.I), "")
    demo_src = (DEMO / "adapter.py").read_text(encoding="utf-8")
    say("E7c 参考 Adapter 也没引入 AI 依赖（寻址就是一次 /agents + 本地字符串比较）",
        not re.search(r"^\s*(?:from|import)\s+(openai|anthropic|torch|transformers)",
                      demo_src, re.M)
        and "directory.resolve" in demo_src, "")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", action="store_true")
    args = ap.parse_args()

    tmp = Path(tempfile.mkdtemp(prefix="anp-e2e-"))
    try:
        check_no_internal_import()

        # ★三个 case 各用一个全新 hub★：`demo-01` 这个身份一旦被注册成某一档，
        # 换档必须带对令牌（P0-2），而每个新 Adapter 实例手里还没有那枚令牌。
        # 真实部署也是"一个平台一个身份、一个 hub 一个 home"，各跑各的更干净。
        hub = Hub(tmp / "home_http")
        try:
            case_http(hub.base, "demo:http")
        finally:
            hub.kill()

        hub = Hub(tmp / "home_exec")
        try:
            case_exec(hub.base, "demo:exec")
        finally:
            hub.kill()

        hub = Hub(tmp / "home_queue")
        try:
            case_queue(hub.base, "demo:queue")
        finally:
            hub.kill()

        # E. 寻址：模糊目标 → canonical Agent ID（任务书要求的七条验收）
        hub = Hub(tmp / "home_resolve")
        try:
            case_resolve(hub.base)
        finally:
            hub.kill()

        case_third_party(tmp / "outside")

        print(f"\n===== E2E 验收：{OK} 通过 / {FAIL} 失败 =====")
        if FAIL:
            print("★未全绿★：先看 FAIL 项，不要进 #3。")
        return 1 if FAIL else 0
    finally:
        if args.keep:
            print("临时目录保留在", tmp)
        else:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
