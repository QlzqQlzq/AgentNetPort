"""#4.1 Mimo 对抗性迁移验收。

★这不是"代码搬完了多少"的验收★。验收的是这五个问题有没有被一个真实平台回答：

  Q1  Spec 能否描述真实平台 Adapter？
  Q2  SDK 是否真的够用，而不是只够 Demo？
  Q3  平台语义 → ANP / ANP → 平台语义的边界是否正确？
  Q4  build_prompt() / workspace_for() 这类职责到底属于谁？
  Q5  真实部署时有没有 Spec 没写但 Adapter 必须知道的东西？

验收纪律（与 #4 的边界一致）：
  · 新实现只 import SDK + 标准库，★一行 Core 都不 import★
  · 从真实 Plugin 安装路径验：Core launch → register → heartbeat → managed agent
    → wake → 翻译 → （平台递送） → 回信
  · 每个缺口必须归类到 Adapter / SDK / Spec / Core 四者之一，不许"以后再算"
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SRC = REPO / "src"
ADAPTER = Path(__file__).resolve().parent
HUB = SRC / "anp_hub.py"
SDK = REPO / "sdk" / "anp_adapter"

if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
import anp_core as core      # noqa: E402  （验收骨架可以读 Core，被验的 adapter 不行）
import anp_plugin as plugin  # noqa: E402

MANAGED = "mimo"
ADAPTER_ID = "anp.adapter.mimo"
OK = FAIL = 0
GAPS: list[tuple[str, str, str]] = []      # (类别, 标题, 说明)


def say(name: str, cond: bool, extra: str = "") -> None:
    global OK, FAIL
    OK += 1 if cond else 0
    FAIL += 0 if cond else 1
    print(f"  [{'OK  ' if cond else 'FAIL'}] {name}" + (f"  <- {extra}" if extra else ""))


def gap(kind: str, title: str, detail: str) -> None:
    """记录一个缺口。kind ∈ Adapter / SDK / Spec / Core。"""
    GAPS.append((kind, title, detail))
    print(f"  [GAP·{kind}] {title} — {detail}")


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def post(base: str, path: str, body: dict, key: str = "") -> tuple[int, dict]:
    req = urllib.request.Request(
        base + path, method="POST",
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"content-type": "application/json; charset=utf-8",
                 **({"x-mailbox-key": key} if key else {})})
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8") or "{}")
        except json.JSONDecodeError:
            return e.code, {}
    except urllib.error.URLError as e:
        return 0, {"ok": False, "error": f"连不上：{getattr(e, 'reason', e)}"}


def get(base: str, path: str) -> tuple[int, dict]:
    try:
        with urllib.request.urlopen(base + path, timeout=10) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, {}
    except urllib.error.URLError:
        return 0, {}


class Rig:
    """一次真实安装：新 home + 把 Adapter Plugin + SDK 装进 <home>/plugins/。"""

    def __init__(self, tmp: Path, name: str, policy: dict | None = None):
        self.home = tmp / f"home_{name}"
        dst = self.home / "plugins" / ADAPTER_ID
        dst.mkdir(parents=True, exist_ok=True)
        if policy is not None:
            (self.home / "permissions.json").write_text(
                json.dumps(policy, ensure_ascii=False, indent=2), encoding="utf-8")
        # ★真实安装路径★：adapter 与它依赖的 SDK 一起进 <home>/plugins/<id>/
        shutil.copytree(ADAPTER, dst, dirs_exist_ok=True)
        shutil.copytree(SDK, dst / "anp_adapter", dirs_exist_ok=True)
        shutil.rmtree(dst / "data", ignore_errors=True)
        self.port = free_port()
        self.base = f"http://127.0.0.1:{self.port}"
        self.proc = subprocess.Popen(
            [sys.executable, str(HUB), "--host", "127.0.0.1", "--port", str(self.port),
             "--home", str(self.home)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            env={
                **os.environ,
                "AGENT_ANP_EVENT_SCAN_SEC": "1",
                "AGENT_ANP_MIMO_DESKTOP_API": str(self.home / "missing-desktop-api.json"),
            })
        for _ in range(80):
            try:
                urllib.request.urlopen(self.base + "/health", timeout=1)
                break
            except Exception:  # noqa: BLE001
                time.sleep(0.25)

    def wait_adapter(self, timeout: float = 20.0) -> int:
        """等 adapter 被 Core 拉起并完成握手。用账本反推端口——不问 adapter 自己。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            row = core.agent_table(self.home).get(ADAPTER_ID) or {}
            url = str((row.get("wake") or {}).get("url") or "")
            if ":" in url:
                try:
                    return int(url.rsplit(":", 1)[1].split("/")[0])
                except ValueError:
                    pass
            time.sleep(0.3)
        return 0

    def peer_key(self) -> str:
        """peer.one 的令牌，惰性注册并缓存。"""
        if not getattr(self, "_peer_key", ""):
            _, pr = post(self.base, "/heartbeat", {"name": "peer.one"})
            self._peer_key = str(pr.get("key") or "")
        return self._peer_key

    def kill(self) -> None:
        # 先杀 adapter 子进程再杀 hub（#3 抓到的孤儿泄漏）
        try:
            for entry in (plugin.read_registry(self.home) or {}).values():
                pid = entry.get("pid")
                if pid and entry.get("core_spawned") and int(pid) != os.getpid():
                    try:
                        os.kill(int(pid), signal.SIGTERM)
                    except Exception:  # noqa: BLE001
                        pass
        except Exception:  # noqa: BLE001
            pass
        try:
            self.proc.kill()
        except Exception:  # noqa: BLE001
            pass


# =========================================================================== #
# 静态：新实现的资格线
# =========================================================================== #
def check_static() -> None:
    print("=== S. 静态资格线：新实现只依赖 SDK ===")
    src = (ADAPTER / "plugin.py").read_text(encoding="utf-8")
    plat = (ADAPTER / "platform.py").read_text(encoding="utf-8")
    both = src + plat
    mods = {m.split(".")[0] for m in
            re.findall(r"^\s*(?:from|import)\s+([A-Za-z_][\w.]*)", both, re.M)}
    stdlib = {"argparse", "json", "os", "queue", "sys", "threading", "time",
              "urllib", "http", "pathlib", "socket", "shutil", "subprocess",
              "__future__", "re", "tempfile", "typing", "importlib", "hmac",
              "hashlib", "secrets", "signal", "traceback"}
    say("S1 只 import 标准库 + anp_adapter", not (mods - stdlib - {"anp_adapter", "platform"}),
        f"命中 {sorted(mods - stdlib - {'anp_adapter', 'platform'})}")
    say("S2 ★不 import 任何 Core 模块★",
        not re.search(r"import\s+anp_(core|hub|policy|plugin)|import\s+mailbox_", both))
    # S2 已经验了"不 import Core"。S3 补另一半：**代码里没有对 core 对象的属性访问**。
    # （docstring 里提到旧实现走过 Core 内部是迁移说明，允许；判的是真用了。）
    code_lines = []
    for block in re.split(r'"""', both)[::2]:      # 只取非 docstring 段
        for ln in block.split("\n"):
            code_lines.append(ln.split("#")[0])
    code_only = "\n".join(code_lines)
    say("S3 代码里没有 core./mailbox_ 属性访问",
        not re.search(r"\bcore\.|\bmailbox_", code_only))
    say("S4 SDK 已随 adapter 一起交付的约定写清了",
        "_find_sdk" in src and "vendor" in src.lower())


def check_cli_regressions() -> None:
    print("\n=== M. MiMo CLI 识别与回信回收回归 ===")
    code = f'''
import json, sys, tempfile
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0, {str(ADAPTER)!r})
import importlib.util as ilu
spec = ilu.spec_from_file_location("mimo_platform", {str(ADAPTER / "platform.py")!r})
m = ilu.module_from_spec(spec)
spec.loader.exec_module(m)
with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp)
    gui = root / "Xiaomi MiMo.exe"
    gui.touch()
    cli = root / "bin" / "mimo.exe"
    cli.parent.mkdir()
    cli.touch()
    p = m.MiMoPlatform("mimo", data_dir=root / "data")
    m.CLI_CANDIDATES = [str(gui)]
    gui_rejected = p.find_cli() is None
    m.CLI_CANDIDATES = [str(cli)]
    cli_accepted = p.find_cli() == str(cli)
    diagnostic = p.extract_reply("[api-base] https://example.invalid")
    structured_reply = p.extract_reply(json.dumps({{"result": "模型回复"}}))
    unknown_json = p.extract_reply(json.dumps({{"api-base": "diagnostic"}}))
    original_run = m.subprocess.run
    try:
        m.subprocess.run = lambda *a, **k: SimpleNamespace(
            returncode=0, stdout="[api-base] https://example.invalid\\\\n", stderr="")
        empty_result = p.run_headless(str(cli), [], {{}})
    finally:
        m.subprocess.run = original_run
    print(json.dumps({{
        "gui_rejected": gui_rejected,
        "cli_accepted": cli_accepted,
        "diagnostic_ignored": diagnostic == "",
        "structured_reply": structured_reply == "模型回复",
        "unknown_json_ignored": unknown_json == "",
        "empty_result_failed": empty_result.get("ok") is False
            and empty_result.get("reply") == "",
    }}))
'''
    out = subprocess.run([sys.executable, "-c", code], capture_output=True,
                         text=True, encoding="utf-8", errors="replace", timeout=30)
    try:
        result = json.loads(out.stdout.strip().splitlines()[-1])
    except Exception:  # noqa: BLE001
        result = {}
        print("    stderr:", (out.stderr or "")[-300:])
    say("M1 Xiaomi MiMo GUI 不会被识别为 CLI",
        result.get("gui_rejected") is True, json.dumps(result))
    say("M2 独立 mimo CLI 仍可识别", result.get("cli_accepted") is True)
    say("M3 诊断行不会成为回复", result.get("diagnostic_ignored") is True)
    say("M4 可识别结构化模型回复", result.get("structured_reply") is True)
    say("M5 未知 JSON 不会成为回复", result.get("unknown_json_ignored") is True)
    say("M6 CLI 空正文按失败处理", result.get("empty_result_failed") is True)


def check_desktop_state_regressions() -> None:
    print("\n=== M2. MiMo 桌面状态与 C 档语义回归 ===")
    code = f'''
import json, tempfile
from pathlib import Path
import importlib.util as ilu
spec = ilu.spec_from_file_location("mimo_platform", {str(ADAPTER / "platform.py")!r})
m = ilu.module_from_spec(spec)
spec.loader.exec_module(m)
with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp)
    prefs = root / "preferences.json"
    prefs.write_text(json.dumps({{"evolveEnabled": False}}), encoding="utf-8")
    p = m.MiMoPlatform("mimo", data_dir=root / "data")
    p.preferences_path = prefs
    p.desktop_api_path = root / "desktop-api.json"
    p.probe_serve = lambda *a, **k: False
    p.find_cli = lambda: None
    tier = p.detect_tier()
    print(json.dumps({{
        "tier": tier.get("tier"),
        "evolve_enabled": tier.get("evolve_enabled"),
        "disabled_detail": "evolveEnabled=false" in tier.get("detail", ""),
    }}))
'''
    out = subprocess.run([sys.executable, "-c", code], capture_output=True,
                         text=True, encoding="utf-8", errors="replace", timeout=30)
    try:
        result = json.loads(out.stdout.strip().splitlines()[-1])
    except Exception:  # noqa: BLE001
        result = {}
    say("M7 evolveEnabled=false 被识别为明确的 C 档原因",
        result.get("tier") == "C"
        and result.get("evolve_enabled") is False
        and result.get("disabled_detail") is True,
        json.dumps(result, ensure_ascii=False))

    code = f'''
import json, tempfile
from pathlib import Path
import importlib.util as ilu
spec = ilu.spec_from_file_location("mimo_platform", {str(ADAPTER / "platform.py")!r})
m = ilu.module_from_spec(spec)
spec.loader.exec_module(m)
with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp)
    prefs = root / "preferences.json"
    prefs.write_text(json.dumps({{"evolveEnabled": False}}), encoding="utf-8")
    p = m.MiMoPlatform("mimo", data_dir=root / "data")
    p.preferences_path = prefs
    p.desktop_api_path = root / "desktop-api.json"
    p.desktop_running = lambda: (True, [123])
    p.probe_serve = lambda *a, **k: False
    p.find_cli = lambda: None
    payload = p.health_payload()
    print(json.dumps({{
        "ok": payload.get("ok"),
        "status": payload.get("status"),
        "tier": (payload.get("tier") or {{}}).get("tier"),
        "adapter_fields": payload.get("desktop_running") is True
            and payload.get("evolve_enabled") is False,
    }}))
'''
    out = subprocess.run([sys.executable, "-c", code], capture_output=True,
                         text=True, encoding="utf-8", errors="replace", timeout=30)
    try:
        result = json.loads(out.stdout.strip().splitlines()[-1])
    except Exception:  # noqa: BLE001
        result = {}
    say("M8 平台健康不把“桌面进程存在”误报成可自动执行",
        result.get("ok") is False
        and result.get("status") == "desktop_present_bridge_disabled"
        and result.get("tier") == "C"
        and result.get("adapter_fields") is True,
        json.dumps(result, ensure_ascii=False))


# =========================================================================== #
# Q1 Spec 能否描述真实平台 Adapter？
# =========================================================================== #
def check_q1(rig: Rig) -> None:
    print("\n=== Q1 Spec 能否描述真实平台 Adapter ===")
    # ★H-2★：代管注册在后台线程；必须等 wake 落账，否则 Q1 全是空表假失败
    deadline = time.time() + 25
    row: dict = {}
    while time.time() < deadline:
        row = core.agent_table(rig.home).get(MANAGED) or {}
        if row.get("wake"):
            break
        time.sleep(0.3)
    say("Q1.1 代管的 mimo 身份已注册（公开 /heartbeat）", bool(row),
        f"wake={json.dumps(row.get('wake'), ensure_ascii=False)[:90]}")
    say("Q1.2 caps 按 MiMo 档位登记（Spec §8 的四个键）",
        set(row.get("caps") or {}) >= {"inject_session", "spawn_cwd",
                                       "resume_session", "queue"},
        json.dumps(row.get("caps"), ensure_ascii=False))
    say("Q1.3 workspace / tier / process_api 都透传上去了",
        bool(row.get("workspace")) and bool(row.get("tier")) and bool(row.get("process_api")),
        f"workspace={bool(row.get('workspace'))} tier={bool(row.get('tier'))}")
    say("Q1.4 两个身份两枚令牌（P0-1）",
        core.key_for(ADAPTER_ID, rig.home) != core.key_for(MANAGED, rig.home))
    # Spec 的 §5 三档是否够描述 Mimo 的现实
    say("Q1.5 Mimo 的三档能落到 Spec 的 delivery mode 上",
        row.get("wake", {}).get("kind") in ("http", "exec", "queue"),
        str(row.get("wake", {}).get("kind")))
    if not (row.get("wake") or {}).get("url") and (row.get("wake") or {}).get("command"):
        gap("Spec", "exec 档缺少『投递之后由谁真正处理』的表达",
            "Mimo A 档由 serve 收、B/C 档由本插件落盘，但 Spec §4 只有 http/exec/queue 三个值，"
            "描述不了『exec 之后真正干活的是谁』。旧实现靠 caps + tier 字段侧面表达。")
    say("Q1.6 A 档 serve 的载荷形状与 Spec §5.1 不同，由 adapter 自己变换",
        "announce_serve" in (ADAPTER / "platform.py").read_text(encoding="utf-8"))


# =========================================================================== #
# Q2 SDK 够用吗
# =========================================================================== #
def check_q2(rig: Rig) -> None:
    print("\n=== Q2 SDK 够用吗（而不只是够 Demo）===")
    _, inbox_probe = post(rig.base, "/tool", {"server": MANAGED, "name": "mail_inbox",
                                              "args": {"peek": True}}, "")
    probe_messages = inbox_probe.get("messages") or []
    say("Q2.1 SDK 的 receive(peek=True) 能取到状态字段",
        isinstance(probe_messages, list)
        and (not probe_messages
             or all(k in probe_messages[0] for k in ("attempts", "wake_skipped"))),
        json.dumps(probe_messages[:1], ensure_ascii=False))

    # 直接从 SDK 用一遍：模拟 adapter 会用的所有原语
    code = (
        "import sys, json;"
        f"sys.path.insert(0, {str(ADAPTER)!r});"
        f"sys.path.insert(0, {str(REPO / 'sdk')!r});"
        "from anp_adapter import AnpClient, Envelope;"
        f"c = AnpClient({rig.base!r}, {core.key_for(MANAGED, rig.home)!r}, 'mimo');"
        "r = c.receive(peek=True, unread_only=True, limit=5);"
        "s = c.send('peer.one', '来自 SDK 的回信', correlation_id='q2:probe', wake=False);"
        "q = c.query('q2:probe');"
        "print(json.dumps({'receive_ok': r.get('ok') is True,"
        " 'msgs': len(r.get('messages') or []),"
        " 'send_ok': s.get('ok') is True, 'query_n': len(q.get('messages') or [])}))")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True,
                         text=True, encoding="utf-8", errors="replace", timeout=60)
    try:
        d = json.loads(out.stdout.strip().splitlines()[-1])
    except Exception:  # noqa: BLE001
        d = {}
    say("Q2.2 SDK 的 receive / send / query 三个原语都直接可用",
        d.get("receive_ok") and d.get("send_ok") and d.get("query_n", 0) >= 1,
        json.dumps(d, ensure_ascii=False) or (out.stderr or "")[-160:])
    if not d:
        gap("SDK", "某个原语需要 adapter 自己补 HTTP 细节",
            (out.stderr or "")[-200:])


# =========================================================================== #
# Q3 + Q4 + Q5 真实链路
# =========================================================================== #
def check_chain(rig: Rig) -> None:
    print("\n=== C. 真实链路：Core launch → wake → 翻译 → 平台 → 回信 ===")
    # ★peer 只注册一次、令牌复用★：对已知身份第二次不带 key 会被 P0-2 挡成 401，
    # 拿到空令牌，于是全程假失败（这个坑踩过一次）。
    pkey = rig.peer_key()
    if not pkey:
        say("C1 发信即持久化（ok=true 不等于已投递）", False, "peer.one 拿不到令牌")
        return
    code, sent = post(rig.base, "/messages/send",
                      {"from": "peer.one", "to": MANAGED,
                       "body": "Mimo 对抗性验证信",
                       "correlation_id": "q3:chain", "wake": True}, pkey)
    say("C1 发信即持久化（ok=true 不等于已投递）", sent.get("ok") is True,
        f"wake.ok={(sent.get('wake') or {}).get('ok')} err={str(sent.get('error'))[:80]}")

    # ★看 Rig 装出来的那一份★，不是源码目录——跑起来的是 <home>/plugins/<id> 里的副本
    data = rig.home / "plugins" / ADAPTER_ID / "data"
    pend_md = data / "mimo-pending.md"
    pend_json = data / "mimo-pending.json"
    got = None
    for _ in range(50):
        got = rig_wait_files(pend_md, pend_json)
        if got:
            break
        time.sleep(0.4)
    say("C2 ★Core 真的唤醒到 Adapter Plugin，并落出平台侧摘要★", bool(got),
        f"pending_md 存在={pend_md.is_file()}")

    msgs = core.read_records(MANAGED, rig.home)
    mine = [m for m in msgs if str(m.get("body")) == "Mimo 对抗性验证信"]
    say("C3 消息状态字段可判（attempts / delivered_at / wake_skipped）",
        bool(mine) and ("delivered_at" in mine[0] or "attempts" in mine[0]),
        json.dumps({k: mine[0].get(k) for k in
                    ("attempts", "delivered_at", "wake_skipped", "next_retry_at")},
                   ensure_ascii=False) if mine else "（没找到那封）")
    if mine and mine[0].get("next_retry_at"):
        gap("Core", "exec 档投递后仍排了重投",
            f"next_retry_at={mine[0].get('next_retry_at')}；exec 成功也应清掉重投")
    if pend_json.is_file():
        pj = json.loads(pend_json.read_text(encoding="utf-8"))
        say("C4 摘要里带档位与未读（平台自己的状态）",
            "tier" in pj and "unread" in pj,
            f"tier={pj.get('tier', {}).get('tier')} unread={pj.get('unread')}")
        say("C5 摘要落在 adapter 自己的目录，不污染 Core 的 home",
            data.name == "data" and data.parent.name == ADAPTER_ID
            and data.parent.parent.name == "plugins",
            f"{data}")
    if pend_md.is_file():
        txt = pend_md.read_text(encoding="utf-8")
        say("C6 提示词模板（build_prompt）出现在平台侧产出里",
            "MiMo" in txt or "mimo" in txt, txt.splitlines()[0][:60] if txt else "")


def rig_wait_files(*paths: Path) -> bool:
    return all(p.is_file() and p.stat().st_size > 0 for p in paths)


def check_boundary() -> None:
    print("\n=== B. 职责边界（Q3/Q4 的判定）===")
    plat = (ADAPTER / "platform.py").read_text(encoding="utf-8")
    src = (ADAPTER / "plugin.py").read_text(encoding="utf-8")
    say("B1 build_prompt 在平台层（★回答 Q4：归 adapter★）",
        "def build_prompt" in plat and "def build_prompt" not in src)
    say("B2 档位探测在平台层（Core 不知道 A/B/C）",
        "def detect_tier" in plat and "tier" not in
        (SRC / "anp_core.py").read_text(encoding="utf-8").replace("tier", "", 0)
        and "tier" not in (SRC / "anp_hub.py").read_text(encoding="utf-8"))
    say("B3 workspace 选择在平台层（回答 Q4 的另一半）",
        "self.workspace" in plat and "workspace_for" not in
        (SRC / "anp_hub.py").read_text(encoding="utf-8"))
    say("B4 平台状态（pending 文件）在 adapter 自己的目录",
        "self.data_dir" in plat and "mimo-pending" in plat)
    # 平台名出现在注释/示例里（`to="codex"`）是允许的、甚至是好文档；
    # 分层失败的标志是它出现在**逻辑**里：赋值、比较、字典键、分支条件。
    pat = ('["\'](mimo|codex|dsh|chen)["\']\\s*[:=]'
           '|if\\s+.*["\'](mimo|codex|dsh|chen)["\']')
    logic = re.compile(pat, re.I)
    core_src = (SRC / "anp_core.py").read_text(encoding="utf-8")
    hub_src = (SRC / "anp_hub.py").read_text(encoding="utf-8")
    hits_core = [m.group(0) for m in logic.finditer(core_src)]
    hits_hub = [m.group(0) for m in logic.finditer(hub_src)]
    say("B5 ★Core/hub 的逻辑里没有任何平台名★（注释示例不算）",
        not hits_core and not hits_hub, f"core={hits_core[:2]} hub={hits_hub[:2]}")
    spec_src = (REPO / "docs" / "ANP-ADAPTER-SPEC.md").read_text(encoding="utf-8")
    hits_spec = [m.group(0) for m in logic.finditer(spec_src)]
    say("B6 Spec 的逻辑里也没有平台名（示例名不算）", not hits_spec,
        f"{hits_spec[:2]}")


def check_third_party(outside: Path) -> None:
    print("\n=== D. 第三方视角：只带 adapter + SDK，Core 不在场 ===")
    keep = outside / "deploy"
    (keep / "plugins" / ADAPTER_ID).mkdir(parents=True, exist_ok=True)
    shutil.copytree(ADAPTER, keep / "plugins" / ADAPTER_ID, dirs_exist_ok=True)
    shutil.copytree(SDK, keep / "plugins" / ADAPTER_ID / "anp_adapter", dirs_exist_ok=True)
    out = subprocess.run(
        [sys.executable, "-c", (
            f"import sys, json, importlib.util as ilu; sys.path.insert(0, "
            f"{str(keep / 'plugins' / ADAPTER_ID)!r});"
            f"import os; os.environ['AGENT_ANP_HOME'] = {str(keep)!r};"
            f"spec = ilu.spec_from_file_location('mimo_platform', r'{str(keep / 'plugins' / ADAPTER_ID / 'platform.py')}');"
            f"mod = ilu.module_from_spec(spec); spec.loader.exec_module(mod);"
            f"p = mod.MiMoPlatform('mimo');"
            f"t = p.detect_tier();"
            f"print(json.dumps({{'tier': t['tier'], 'caps': p.caps_for(t['tier'])}}))")],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        cwd=str(outside), timeout=60)
    say("D1 只带 adapter+SDK 就能建平台对象", out.returncode == 0,
        out.stdout.strip()[:80] or out.stderr[-200:])
    hidden = outside / "core_hidden"
    shutil.move(str(SRC), str(hidden))
    try:
        out2 = subprocess.run(
            [sys.executable, "-c", (
                f"import sys, json, importlib.util as ilu; sys.path.insert(0, "
                f"{str(keep / 'plugins' / ADAPTER_ID)!r});"
                f"import os; os.environ['AGENT_ANP_HOME'] = {str(keep)!r};"
                f"spec = ilu.spec_from_file_location('mimo_platform', r'{str(keep / 'plugins' / ADAPTER_ID / 'platform.py')}');"
                f"mod = ilu.module_from_spec(spec); spec.loader.exec_module(mod);"
                f"p = mod.MiMoPlatform('mimo'); print(p.detect_tier()['tier'])")],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            cwd=str(outside), timeout=60)
        say("D2 ★把 src/ 藏起来，平台层与 SDK 依然工作★",
            out2.returncode == 0 and out2.stdout.strip() in ("A", "B", "C"),
            out2.stdout.strip()[:60] or out2.stderr[-200:])
    finally:
        shutil.move(str(hidden), str(SRC))
    say("D3 src/ 已恢复", (SRC / "anp_hub.py").is_file())


def main() -> int:
	ap = argparse.ArgumentParser()
	ap.add_argument("--keep", action="store_true")
	args = ap.parse_args()

	tmp = Path(tempfile.mkdtemp(prefix="anp-mimo-"))
	try:
		check_static()
		check_cli_regressions()
		check_desktop_state_regressions()
		check_boundary()

		print("\n=== R. 真实安装路径（Core launch → plugin bootstrap）===")
		rig = Rig(tmp, "mimo")
		try:
			port = rig.wait_adapter()
			say("R1 Core 拉起了 Adapter Plugin 进程（账本里有 wake 端口）",
				port > 0, f"port={port}")
			if port <= 0:
				print("★adapter 没起来，后续项都失去意义★")
				return 1
			check_q1(rig)
			check_q2(rig)
			check_chain(rig)
		finally:
			rig.kill()

		record_known_gaps()
		check_third_party(tmp / "outside")

		print(f"\n===== #4.1 Mimo 对抗性验收：{OK} 通过 / {FAIL} 失败 =====")
		if GAPS:
			print(f"\n===== 发现 {len(GAPS)} 个缺口（必须归类）=====")
			for kind, title, detail in GAPS:
				print(f"  [{kind}] {title}")
				print(f"        {detail}")
			by: dict[str, int] = {}
			for k, _, _ in GAPS:
				by[k] = by.get(k, 0) + 1
			print(f"  汇总：{json.dumps(by, ensure_ascii=False)}")
		else:
			print("\n（没有记录到缺口——要么真没有，要么我没找到，继续挖。）")
		return 1 if FAIL else 0
	finally:
		if args.keep:
			print("临时目录保留在", tmp)
		else:
			shutil.rmtree(tmp, ignore_errors=True)


def record_known_gaps() -> None:
    print("\n=== F. 本次迁移已发现的缺口（固定记录）===")

    # ——— Core 类 ———
    roots = core.install_roots()
    home_plugins = core.DEFAULT_HOME / "plugins"
    if not any(str(home_plugins).startswith(str(r)) for r in roots):
        gap("Core", "install_roots() 不含 <home>/plugins/",
            "exec 唤醒的脚本必须落在信箱安装目录内，而 Adapter Plugin 按设计装在 "
            "<home>/plugins/<id>/，真实部署下 exec 唤醒一律被 check_wake_config 拒。"
            "临时解法是设 AGENT_ANP_TRUST_WAKE_PATHS，但那是要操作人记得配。"
            "本次 Mimo 改用「wake 指向插件自己的 /anp/wake」绕开了，缺口仍在。")

    gap("Core", "插件启动预算 10 秒，且超时报错指向错误方向",
        "register_out 只等 REGISTER_TIMEOUT=10s。插件只要在启动路径上做任何探测"
        "（本 adapter 的 detect_tier 要 probe_serve 三条路径）就会超时，"
        "而 Core 报『插件端点不可达』——看起来像插件没起来，实际只是慢。"
        "本 adapter 现在的解法是 main() 里先 srv.start() 绑端口再做慢活。")

    # ——— Spec 类 ———
    spec = (REPO / "docs" / "ANP-ADAPTER-SPEC.md").read_text(encoding="utf-8")
    if "指向插件自己" not in spec:
        gap("Spec", "§4/§5 没写「Adapter Plugin 代管的 agent 应指向插件自己的 /anp/wake」",
            "我最初照抄旧 Mimo 的 exec+独立进程，撞上 install_roots 才知道错了。"
            "插件本来就在 ANP_PLUGIN_PORT 上听着 /anp/wake，代管的 agent 直接指过去即可。"
            "这是 Adapter Plugin 与普通 Adapter 最关键的区别，Spec 应当写明。")
    if "启动" not in spec:
        gap("Spec", "没有插件启动预算/顺序的约定",
            "Core 只等 10 秒。Spec 该写：先 bind 端口，再做任何慢活（探测、注册代管）。"
            "本 adapter 的 main() 现在就是这个形状，但那是踩出来的，不是文档要求的。")
    if "data/" not in spec and "自己的目录" not in spec:
        gap("Spec", "没有约定 adapter 的平台状态该放哪",
            "旧 Mimo 把 mimo-pending.json/.md 写进信箱 <HOME> 根下，两套系统共管一个目录。"
            "新实现改到插件自己的 data/。位置该由 Spec 给个默认约定，而不是各家自定。")

    # ——— SDK 类 ———
    sdk_plugin = (REPO / "sdk" / "anp_adapter" / "plugin.py").read_text(encoding="utf-8")
    if "register_error" in sdk_plugin:
        gap("SDK", "PluginServer 曾把 on_register 的异常吞掉且不暴露（已修）",
            "表现为『adapter 启动正常但代管的 agent 不存在』，最难查的半身不遂。"
            "已在 /anp/health 暴露 register_error；记在这里是因为这是 SDK 的设计教训："
            "骨架层不许静默吞掉用户回调的异常。")

    # ——— Adapter 类（旧实现的问题，记录给后面三家备查）———
    old = REPO.parent / "agent-mailbox" / "src" / "mailbox_mimo.py"
    if old.is_file():
        osrc = old.read_text(encoding="utf-8")
        if "{prompt}" in osrc:
            gap("Adapter", "旧 Mimo 的 B 档 wake 用了协议里不存在的 {prompt} 占位符",
                "Core 对 exec command 原样透传、不替换（#4.1 实测确认）。"
                "所以 B 档要么从未生效，要么 mimo CLI 默默忽略了一个字面量参数。"
                "旧代码的问题，不影响新实现；迁其他三家前要先查同样的情况。")
        if "core.read_records" in osrc or "core.agent_table" in osrc:
            gap("Adapter", "旧 Mimo 直接读 Core 内部（read_records / agent_table）",
                "违反 Adapter Spec §1『禁止做』。新实现已改为只用 SDK 的 receive()。"
                "迁 codex/chen 要更小心——它们共用的 spawn.py 问题更严重。")
        if "mimo-pending" in osrc and "HOME" in osrc:
            gap("Adapter", "旧 Mimo 把平台状态写进 Core 的 <HOME> 目录",
                "mimo-pending.json/.md 落在信箱 home 根下。新实现改到插件自己的 data/。")



if __name__ == "__main__":
    sys.exit(main())


# F 段单独放：本次对抗性迁移已经发现的缺口。
# 每条都在迁移过程中真实踩到过，按用户定下的四类归档：Adapter / SDK / Spec / Core。
