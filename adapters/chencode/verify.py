"""ChenCode Adapter 对抗性验收。

验收纪律：
  · 被验的 adapter 只 import SDK + 标准库，★一行 Core 都不 import★
  · 从真实 Plugin 安装路径验：Core launch → register → heartbeat → wake → 翻译 → 平台 → 回信
  · 覆盖 Errata B1 / S3 / S4 / S1 / S2 关键门
  · 每个缺口归类到 Adapter / SDK / Spec / Core 四者之一
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
import anp_core as core      # noqa: E402
import anp_plugin as plugin  # noqa: E402

MANAGED = "chencode"
ADAPTER_ID = "anp.adapter.chencode"
OK = FAIL = 0
GAPS: list[tuple[str, str, str]] = []


def say(name: str, cond: bool, extra: str = "") -> None:
    global OK, FAIL
    OK += 1 if cond else 0
    FAIL += 0 if cond else 1
    print(f"  [{'OK  ' if cond else 'FAIL'}] {name}" + (f"  <- {extra}" if extra else ""))


def gap(kind: str, title: str, detail: str) -> None:
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
                 **({"x-mailbox-key": key} if key else {})} )
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
        shutil.copytree(ADAPTER, dst, dirs_exist_ok=True)
        shutil.copytree(SDK, dst / "anp_adapter", dirs_exist_ok=True)
        shutil.rmtree(dst / "data", ignore_errors=True)
        (dst / "data").mkdir(parents=True, exist_ok=True)
        self.port = free_port()
        self.base = f"http://127.0.0.1:{self.port}"
        self.proc = subprocess.Popen(
            [sys.executable, str(HUB), "--host", "127.0.0.1", "--port", str(self.port),
             "--home", str(self.home)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            env={**os.environ, "AGENT_ANP_EVENT_SCAN_SEC": "1"})
        for _ in range(80):
            try:
                urllib.request.urlopen(self.base + "/health", timeout=1)
                break
            except Exception:  # noqa: BLE001
                time.sleep(0.25)

    def wait_adapter(self, timeout: float = 20.0) -> int:
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

    def wait_managed(self, timeout: float = 15.0) -> dict:
        deadline = time.time() + timeout
        while time.time() < deadline:
            row = core.agent_table(self.home).get(MANAGED) or {}
            if row:
                return row
            time.sleep(0.3)
        return {}

    def peer_key(self) -> str:
        if not getattr(self, "_peer_key", ""):
            _, pr = post(self.base, "/heartbeat", {"name": "peer.one"})
            self._peer_key = str(pr.get("key") or "")
        return self._peer_key

    def kill(self) -> None:
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
              "hashlib", "math", "unicodedata", "collections", "dataclasses",
              "enum", "functools", "itertools", "random", "string", "struct",
              "textwrap", "uuid", "copy", "contextlib", "abc", "base64",
              "binascii", "calendar", "csv", "datetime", "decimal", "fractions",
              "glob", "io", "logging", "mimetypes", "numbers", "operator",
              "pprint", "secrets", "statistics", "types", "warnings", "weakref",
              "winreg"}
    say("S1 只 import 标准库 + anp_adapter",
        not (mods - stdlib - {"anp_adapter", "platform"}),
        f"命中 {sorted(mods - stdlib - {'anp_adapter', 'platform'})}")
    say("S2 ★不 import 任何 Core 模块★",
        not re.search(r"import\s+anp_(core|hub|policy|plugin)|import\s+mailbox_", both))
    code_lines = []
    for block in re.split(r'"""', both)[::2]:
        for ln in block.split("\n"):
            code_lines.append(ln.split("#")[0])
    code_only = "\n".join(code_lines)
    say("S3 代码里没有 core./mailbox_ 属性访问",
        not re.search(r"\bcore\.|\bmailbox_", code_only))
    say("S4 SDK 已 vendor 进 adapter 目录",
        (ADAPTER / "anp_adapter" / "__init__.py").is_file())


def check_boundary() -> None:
    print("\n=== B. 职责边界 ===")
    plat = (ADAPTER / "platform.py").read_text(encoding="utf-8")
    src = (ADAPTER / "plugin.py").read_text(encoding="utf-8")
    say("B1 build_prompt 在平台层", "def build_prompt" in plat and "def build_prompt" not in src)
    say("B2 档位探测在平台层", "def detect_tier" in plat)
    say("B3 route_from_stamp 只读 meta.session（S1）",
        "route_from_stamp" in plat and "decide" not in src)
    say("B4 平台状态在 adapter 自己的 data/", "self.data_dir" in plat and "chencode-pending" in plat)
    say("B5 should_reply 实现 S4（===true）", 'need_reply") is True' in plat or "need_reply) is True" in plat)
    say("B6 enqueue_work 先持久化（S3-A）", "def enqueue_work" in plat)
    say("B7 回信走 client.send（B4）", "client.send" in src or "._send_reply" in src)


def check_git_bash_regressions() -> None:
    print("\n=== G. Windows Git Bash 发现与子进程环境回归 ===")
    code = f'''
import json, os, sys, tempfile
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0, {str(ADAPTER)!r})
import importlib.util as ilu
spec = ilu.spec_from_file_location("chencode_platform", {str(ADAPTER / "platform.py")!r})
m = ilu.module_from_spec(spec)
spec.loader.exec_module(m)
with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp)
    for name in (
        "AGENT_ANP_CHEN_STUB_REPLY",
        "AGENT_ANP_CHEN_SKIP_PREFLIGHT",
        "AGENT_ANP_CHEN_FORCE_FAIL",
    ):
        os.environ.pop(name, None)
    explicit = root / "explicit" / "bash.exe"
    explicit.parent.mkdir()
    explicit.touch()
    p = m.ChenCodePlatform("chencode", data_dir=root / "data")
    os.environ["AGENT_ANP_CHEN_GIT_BASH"] = str(explicit)
    explicit_found = p.find_git_bash() == str(explicit)
    registry_found = False
    if os.name == "nt":
        git_root = root / "registered-git"
        registered = git_root / "bin" / "bash.exe"
        registered.parent.mkdir(parents=True)
        registered.touch()
        class Key:
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False
        fake_winreg = SimpleNamespace(
            HKEY_LOCAL_MACHINE=1, HKEY_CURRENT_USER=2,
            OpenKey=lambda hive, name: Key(),
            QueryValueEx=lambda handle, name: (str(git_root), 1))
        original_which = m.shutil.which
        try:
            sys.modules["winreg"] = fake_winreg
            m.shutil.which = lambda name: None
            os.environ.pop("AGENT_ANP_CHEN_GIT_BASH", None)
            os.environ.pop("AGENT_MAILBOX_CHEN_GIT_BASH", None)
            os.environ.pop("CLAUDE_CODE_GIT_BASH_PATH", None)
            os.environ["PATH"] = ""
            registry_found = p.find_git_bash() == str(registered)
        finally:
            m.shutil.which = original_which
            sys.modules.pop("winreg", None)
    bash = root / "runtime-bash.exe"
    bash.touch()
    p.find_git_bash = lambda: str(bash)
    p.pick_credential = lambda: {{
        "source": "isolated-test", "key": "not-a-real-credential",
        "base_url": "http://127.0.0.1", "model": "test", "effort": "low"}}
    p._command = lambda native, route, cred: ["fake-bun"]
    captured = {{}}
    original_run = m.subprocess.run
    try:
        m.subprocess.run = lambda *args, **kwargs: (
            captured.update(kwargs) or SimpleNamespace(
                returncode=0,
                stdout=json.dumps({{"session_id": "s-test", "result": "reply",
                                   "subtype": "success"}}),
                stderr=""))
        result = p.run({{"text": "isolated"}}, {{
            "mode": "new", "session_id": "", "workspace": str(root)}})
    finally:
        m.subprocess.run = original_run
    injected = (captured.get("env") or {{}}).get("CLAUDE_CODE_GIT_BASH_PATH") == str(bash)
    print(json.dumps({{
        "explicit_found": explicit_found,
        "registry_found": registry_found,
        "injected": injected,
        "run_ok": result.get("ok") is True and result.get("reply") == "reply",
    }}))
'''
    out = subprocess.run([sys.executable, "-c", code], capture_output=True,
                         text=True, encoding="utf-8", errors="replace", timeout=30)
    try:
        result = json.loads(out.stdout.strip().splitlines()[-1])
    except Exception:  # noqa: BLE001
        result = {}
        print("    stderr:", (out.stderr or "")[-300:])
    say("G1 Adapter 显式配置的 Git Bash 路径可发现",
        result.get("explicit_found") is True, json.dumps(result))
    if os.name == "nt":
        say("G2 Git for Windows 注册表路径可发现",
            result.get("registry_found") is True)
    else:
        print("  [SKIP] G2 注册表路径仅在 Windows 执行")
    say("G3 CLAUDE_CODE_GIT_BASH_PATH 注入 headless 子进程",
        result.get("injected") is True)
    say("G4 注入 Bash 后 headless 仍能正常回收结果",
        result.get("run_ok") is True)


def check_tier_precedence() -> None:
    """桌面进程存在但无健康端点时，不应遮住可用的 B 档 CLI。"""
    print("\n=== T. 档位选择优先级 ===")
    code = f'''
import importlib.util, json
spec = importlib.util.spec_from_file_location(
    "chencode_platform", {str(ADAPTER / "platform.py")!r})
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
p = m.ChenCodePlatform("chencode")
p.probe_health = lambda: False
p.desktop_running = lambda: (True, [123])
p.find_bun = lambda: "bun.exe"
p.find_entry = lambda: "bootstrap-entry.ts"
with_cli = p.detect_tier()
p.find_bun = lambda: None
p.find_entry = lambda: None
without_cli = p.detect_tier()
print(json.dumps({{"with_cli": with_cli, "without_cli": without_cli}}))
'''
    out = subprocess.run([sys.executable, "-c", code], capture_output=True,
                         text=True, encoding="utf-8", errors="replace", timeout=30)
    try:
        result = json.loads(out.stdout.strip().splitlines()[-1])
    except Exception:  # noqa: BLE001
        result = {}
        print("    stderr:", (out.stderr or "")[-300:])
    say("T1 健康端点不可达时，可用 CLI 优先于仅进程探针",
        (result.get("with_cli") or {}).get("tier") == "B",
        json.dumps(result.get("with_cli"), ensure_ascii=False))
    say("T2 没有 CLI 时才回落桌面进程 A 档",
        (result.get("without_cli") or {}).get("tier") == "A",
        json.dumps(result.get("without_cli"), ensure_ascii=False))


def check_wake_mode_contract() -> None:
    print("\n=== W. B1 wake 响应契约（单元级）===")
    # 直接测 SDK 的 handle_wake 默认 + adapter 的失败路径形状
    code = (
        "import sys, json, os;"
        f"sys.path.insert(0, {str(ADAPTER)!r});"
        f"sys.path.insert(0, {str(ADAPTER / 'anp_adapter')!r});"
        "from anp_adapter.envelope import RESPONSE_MODES, DEFAULT_RESPONSE_MODE, from_wake_payload;"
        "env, side = from_wake_payload("
        "  {'message': {'id':'m1','from':'p','to':'chencode','body':'x','kind':'message'},"
        "   'to': 'chencode'},"
        "  {'X-Mailbox-Key': 'tok'});"
        "print(json.dumps({'ok_env': env.is_complete, 'key': side['agent_key'],"
        "  'modes': list(RESPONSE_MODES), 'default': DEFAULT_RESPONSE_MODE}))"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True,
                         text=True, encoding="utf-8", errors="replace", timeout=30)
    try:
        d = json.loads(out.stdout.strip().splitlines()[-1])
    except Exception:  # noqa: BLE001
        d = {}
    say("W1 from_wake_payload 解析信封 + 大小写不敏感取 agent_key",
        d.get("ok_env") is True and d.get("key") == "tok", json.dumps(d))
    say("W2 合法 mode 闭集含 immediate/session",
        "immediate" in (d.get("modes") or []) and "session" in (d.get("modes") or []),
        str(d.get("modes")))
    say("W3 默认 mode 合法", d.get("default") in (d.get("modes") or []), str(d.get("default")))


def check_s4_unit() -> None:
    print("\n=== N. S4 need_reply 单元级 ===")
    code = f'''
import sys, json
from types import SimpleNamespace
sys.path.insert(0, {str(ADAPTER)!r})
import importlib.util as ilu
spec = ilu.spec_from_file_location("p", {str(ADAPTER / "platform.py")!r})
m = ilu.module_from_spec(spec)
spec.loader.exec_module(m)
p = m.ChenCodePlatform("chencode")
def E(meta):
    return SimpleNamespace(meta=meta, id="x", from_="a", body="b",
                           kind="message", subject="", reply_to="",
                           correlation_id="c")
print(json.dumps({{
    "true": p.should_reply(E({{"need_reply": True}})),
    "false": p.should_reply(E({{"need_reply": False}})),
    "missing": p.should_reply(E({{}})),
    "none": p.should_reply(E({{"need_reply": None}})),
}}))
'''
    out = subprocess.run([sys.executable, "-c", code], capture_output=True,
                         text=True, encoding="utf-8", errors="replace", timeout=30)
    try:
        d = json.loads(out.stdout.strip().splitlines()[-1])
    except Exception:  # noqa: BLE001
        d = {}
        print("    stderr:", (out.stderr or "")[-300:])
    say("N1 need_reply=true → 要求回信", d.get("true") is True, json.dumps(d))
    say("N2 need_reply=false → 不要求", d.get("false") is False)
    say("N3 need_reply 缺省 → 不要求（知会）", d.get("missing") is False)
    say("N4 need_reply=null → 不要求", d.get("none") is False)


def check_session_stamp() -> None:
    print("\n=== X. S1 会话章执行 ===")
    code = f'''
import sys, json
from types import SimpleNamespace
sys.path.insert(0, {str(ADAPTER)!r})
import importlib.util as ilu
spec = ilu.spec_from_file_location("p", {str(ADAPTER / "platform.py")!r})
m = ilu.module_from_spec(spec)
spec.loader.exec_module(m)
p = m.ChenCodePlatform("chencode")
def E(meta):
    return SimpleNamespace(meta=meta)
r1 = p.route_from_stamp(E({{"session": {{"mode": "reuse", "session_id": "s-1"}}}}))
r2 = p.route_from_stamp(E({{"session": {{"mode": "new"}}}}))
r3 = p.route_from_stamp(E({{}}))
print(json.dumps({{"reuse": r1, "new": r2, "empty": r3}}))
'''
    out = subprocess.run([sys.executable, "-c", code], capture_output=True,
                         text=True, encoding="utf-8", errors="replace", timeout=30)
    try:
        d = json.loads(out.stdout.strip().splitlines()[-1])
    except Exception:  # noqa: BLE001
        d = {}
        print("    stderr:", (out.stderr or "")[-300:])
    say("X1 meta.session reuse → mode=reuse + session_id",
        (d.get("reuse") or {}).get("mode") == "reuse"
        and (d.get("reuse") or {}).get("session_id") == "s-1",
        json.dumps(d.get("reuse")))
    say("X2 meta.session new → mode=new", (d.get("new") or {}).get("mode") == "new")
    say("X3 缺省 → mode=new（不自建决策）", (d.get("empty") or {}).get("mode") == "new")


def check_chain(rig: Rig) -> None:
    print("\n=== C. 真实链路：Core launch → wake → 签收 → 平台/补偿 ===")
    pkey = rig.peer_key()
    if not pkey:
        say("C0 peer.one 拿到令牌", False)
        return
    say("C0 peer.one 拿到令牌", True)

    # ① need_reply 缺省（知会）→ 签收、不强制跑平台
    code, sent = post(rig.base, "/messages/send", {
        "from": "peer.one", "to": MANAGED,
        "body": "ChenCode 知会验证信",
        "correlation_id": "c:notice", "wake": True,
    }, pkey)
    say("C1 知会发信持久化", sent.get("ok") is True,
        f"wake.ok={(sent.get('wake') or {}).get('ok')}")

    data = rig.home / "plugins" / ADAPTER_ID / "data"
    pend_md = data / "chencode-pending.md"
    pend_json = data / "chencode-pending.json"
    work = data / "chencode-work.jsonl"
    got = False
    for _ in range(40):
        if pend_md.is_file() and pend_json.is_file() and work.is_file():
            got = True
            break
        time.sleep(0.3)
    say("C2 ★S3-A 待办与摘要落盘★（插件自己的 data/）", got,
        f"pending_md={pend_md.is_file()} work={work.is_file()}")

    if work.is_file():
        rows = [json.loads(x) for x in work.read_text(encoding="utf-8").splitlines() if x.strip()]
        states = [r.get("state") for r in rows]
        say("C3 知会路径 finish_work=done（queued-as-notice）",
            "done" in states, f"states={states}")

    msgs = core.read_records(MANAGED, rig.home)
    mine = [m for m in msgs if "知会验证信" in str(m.get("body") or "")]
    say("C4 消息状态字段可判", bool(mine) and ("delivered_at" in mine[0] or "attempts" in mine[0]),
        json.dumps({k: (mine[0].get(k) if mine else None) for k in
                    ("attempts", "delivered_at", "wake_skipped", "next_retry_at", "read")},
                   ensure_ascii=False))

    # ② need_reply=true → 应进入异步执行；强制失败路径测 S3 补偿
    code2, sent2 = post(rig.base, "/messages/send", {
        "from": "peer.one", "to": MANAGED,
        "body": "ChenCode need_reply 验证信",
        "correlation_id": "c:reply", "wake": True,
        "meta": {"need_reply": True, "context_id": "ctx-test-1"},
    }, pkey)
    say("C5 need_reply 发信持久化", sent2.get("ok") is True)
    reply_found = False
    for _ in range(50):
        # 回信收件人是 peer.one（原信 from），应在 peer 收件箱里
        _, inbox = post(rig.base, "/messages/receive", {
            "from": "peer.one", "peek": True, "unread_only": False,
            "limit": 50, "order": "oldest",
        }, pkey)
        msgs = inbox.get("messages") or []
        if any(str(m.get("kind") or "") == "reply"
               and str(m.get("from") or "") == MANAGED
               and str(m.get("correlation_id") or "") == "c:reply"
               for m in msgs):
            reply_found = True
            break
        time.sleep(0.4)
    say("C6 ★S3-C 失败补偿或回信出现（不静默）★", reply_found,
        "peer.one 收件箱有 chencode 的 reply")

    if work.is_file():
        rows = [json.loads(x) for x in work.read_text(encoding="utf-8").splitlines() if x.strip()]
        say("C7 need_reply 路径有终态记录", any(r.get("state") in ("done", "failed") for r in rows),
            f"states={[r.get('state') for r in rows]}")


def check_p0_identity(rig: Rig) -> None:
    print("\n=== I. 身份与 P0 ===")
    tbl = core.agent_table(rig.home)
    arow = tbl.get(ADAPTER_ID) or {}
    mrow = tbl.get(MANAGED) or {}
    say("I1 插件身份已登记", bool(arow))
    say("I2 代管 agent 身份已登记", bool(mrow),
        f"wake={json.dumps(mrow.get('wake'), ensure_ascii=False)[:100]}")
    say("I3 两枚令牌不同（P0-1）",
        core.key_for(ADAPTER_ID, rig.home) != core.key_for(MANAGED, rig.home))
    say("I4 wake.kind=http 且指向插件端点（P0-4 载荷）",
        (mrow.get("wake") or {}).get("kind") == "http"
        and "/anp/wake" in str((mrow.get("wake") or {}).get("url") or ""))
    say("I5 caps 四键齐全（P0-8）",
        set(mrow.get("caps") or {}) >= {"inject_session", "spawn_cwd",
                                        "resume_session", "queue"},
        json.dumps(mrow.get("caps"), ensure_ascii=False))
    say("I6 process_api 已声明", bool(mrow.get("process_api")))
    say("I7 workspace / client 已透传",
        bool(mrow.get("workspace")) and str(mrow.get("client") or "") == "chencode")


def check_h3_whitelist(rig: Rig) -> None:
    print("\n=== H3. 接口白名单（六件套 + 通道扩展面）===")
    src = (ADAPTER / "plugin.py").read_text(encoding="utf-8") + \
          (ADAPTER / "platform.py").read_text(encoding="utf-8")
    allowed = {
        "/heartbeat", "/messages/send", "/messages/receive", "/messages/query",
        "/agents", "/plugin/register", "/session/roster", "/session/bind",
        "/anp/wake", "/anp/register", "/anp/health",
    }
    found = set(re.findall(r'["\'](/[a-zA-Z0-9_./-]+)["\']', src))
    # 过滤明显不是 HTTP 面的路径
    http_like = {p for p in found if p.startswith("/") and not p.startswith("/Users")
                 and " " not in p}
    extra = {p for p in http_like if p not in allowed
             and not p.endswith((".json", ".ts", ".txt", ".md", ".log"))}
    say("H3 没有六件套+扩展面之外的消息面私有 API", not extra, f"extra={sorted(extra)[:8]}")


def record_known_gaps() -> None:
    print("\n=== F. 缺口记录 ===")
    # 以临时 home 的 plugins 为准：真实 DEFAULT_HOME 可能已含 plugins 路径
    if True:
        gap("Core", "install_roots() 不含 <home>/plugins/",
            "exec 唤醒脚本在插件目录会被信任校验拒。本 adapter 已用 http wake 绕开。")
    gap("Core", "插件启动预算 10s 且超时报错指向错误方向",
        "慢活（探测/代管注册）必须放后台；本 adapter 的 main 先 serve_forever。")
    spec = (REPO / "docs" / "ANP-ADAPTER-SPEC.md").read_text(encoding="utf-8")
    if "指向插件自己" not in spec:
        gap("Spec", "§4/§5 未写明 Adapter Plugin 代管 agent 应指向插件自己的 /anp/wake",
            "ChenCode/Codex/Mimo 均踩过；应写进 Spec。")
    if "data/" not in spec and "自己的目录" not in spec:
        gap("Spec", "未约定 adapter 平台状态目录",
            "本实现放插件 data/；旧 mailbox_chen 曾写信箱 HOME 根下。")
    gap("Adapter", "旧 mailbox_chen 依赖 mailbox MCP 回信",
        "新实现改为 headless result → /messages/send，不再依赖 mailbox_mcp。"
        "交互会话侧的自发回信通道仍待产品化（ANP MCP 工具或 reply 文件）。")
    gap("Adapter", "凭证预检会打真实 API（1 token）",
        "无网/无凭证时 B 档直接失败并补偿；A/C 档仍可收信。"
        "可选 AGENT_ANP_CHEN_ALWAYS_RUN 控制知会是否也跑。")


def check_third_party(outside: Path) -> None:
    print("\n=== D. 第三方视角：只带 adapter + SDK，Core 不在场 ===")
    keep = outside / "deploy"
    (keep / "plugins" / ADAPTER_ID).mkdir(parents=True, exist_ok=True)
    shutil.copytree(ADAPTER, keep / "plugins" / ADAPTER_ID, dirs_exist_ok=True)
    shutil.copytree(SDK, keep / "plugins" / ADAPTER_ID / "anp_adapter", dirs_exist_ok=True)
    out = subprocess.run(
        [sys.executable, "-c", (
            f"import sys, json; sys.path.insert(0, "
            f"{str(keep / 'plugins' / ADAPTER_ID)!r});"
            f"import os; os.environ['AGENT_ANP_HOME'] = {str(keep)!r};"
            "import importlib.util as ilu;"
            f"spec = ilu.spec_from_file_location('p', "
            f"{str(keep / 'plugins' / ADAPTER_ID / 'platform.py')!r});"
            "m = ilu.module_from_spec(spec); spec.loader.exec_module(m);"
            "p = m.ChenCodePlatform('chencode');"
            "t = p.detect_tier();"
            "print(json.dumps({'tier': t['tier'], 'caps': p.caps_for(t['tier'])}))"
        )],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        cwd=str(outside), timeout=60)
    say("D1 只带 adapter+SDK 就能建平台对象", out.returncode == 0,
        out.stdout.strip()[:100] or out.stderr[-200:])

    hidden = outside / "core_hidden"
    if SRC.is_dir():
        shutil.move(str(SRC), str(hidden))
        try:
            out2 = subprocess.run(
                [sys.executable, "-c", (
                    f"import sys, json; sys.path.insert(0, "
                    f"{str(keep / 'plugins' / ADAPTER_ID)!r});"
                    "import importlib.util as ilu;"
                    f"spec = ilu.spec_from_file_location('p', "
                    f"{str(keep / 'plugins' / ADAPTER_ID / 'platform.py')!r});"
                    "m = ilu.module_from_spec(spec); spec.loader.exec_module(m);"
                    "p = m.ChenCodePlatform('chencode'); print(p.detect_tier()['tier'])"
                )],
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

    tmp = Path(tempfile.mkdtemp(prefix="anp-chencode-"))
    try:
        # 验收环境：插件由 Core 子进程拉起，必须在启动前写入环境，
        # 否则 SKIP_PREFLIGHT / FORCE_FAIL 不会被插件继承。
        os.environ["AGENT_ANP_CHEN_SKIP_PREFLIGHT"] = "1"
        os.environ["AGENT_ANP_CHEN_FORCE_FAIL"] = "1"
        check_static()
        check_boundary()
        check_git_bash_regressions()
        check_tier_precedence()
        check_wake_mode_contract()
        check_s4_unit()
        check_session_stamp()
        check_h3_whitelist_simple()
        check_h2_caps()
        check_enqueue_idempotent()

        print("\n=== R. 真实安装路径（Core launch → plugin bootstrap）===")
        rig = Rig(tmp, "chencode")
        try:
            port = rig.wait_adapter()
            say("R1 Core 拉起了 Adapter Plugin 进程", port > 0, f"port={port}")
            if port > 0:
                managed = rig.wait_managed()
                say("R2 代管身份出现（公开 /heartbeat）", bool(managed))
                write_flags(rig,
                            AGENT_ANP_CHEN_SKIP_PREFLIGHT="1",
                            AGENT_ANP_CHEN_FORCE_FAIL="1")
                check_p0_identity(rig)
                check_chain(rig)
                check_success_reply(rig)
            else:
                print("★adapter 没起来，后续项失去意义★")
        finally:
            rig.kill()

        record_known_gaps()
        check_third_party(tmp / "outside")

        print(f"\n===== ChenCode Adapter 验收：{OK} 通过 / {FAIL} 失败 =====")
        if GAPS:
            print(f"\n===== {len(GAPS)} 个缺口 =====")
            for kind, title, detail in GAPS:
                print(f"  [{kind}] {title}")
                print(f"        {detail}")
            by: dict[str, int] = {}
            for k, _, _ in GAPS:
                by[k] = by.get(k, 0) + 1
            print(f"  汇总：{json.dumps(by, ensure_ascii=False)}")
        return 1 if FAIL else 0
    finally:
        if args.keep:
            print("临时目录保留在", tmp)
        else:
            shutil.rmtree(tmp, ignore_errors=True)


def check_h3_whitelist_simple() -> None:
    print("\n=== H3. 接口白名单（静态）===")
    src = (ADAPTER / "plugin.py").read_text(encoding="utf-8") + \
          (ADAPTER / "platform.py").read_text(encoding="utf-8")
    allowed = {
        "/heartbeat", "/messages/send", "/messages/receive", "/messages/query",
        "/agents", "/plugin/register", "/session/roster", "/session/bind",
        "/anp/wake", "/anp/register", "/anp/health",
        # 平台 API 预检路径（非 ANP 消息面）
        "/messages", "/v1", "/v1/messages",
    }
    found = set(re.findall(r'["\'](/[a-zA-Z0-9_./-]+)["\']', src))
    extra = {p for p in found if p.startswith("/") and p not in allowed
             and not p.endswith((".json", ".ts", ".txt", ".md", ".log", ".py"))
             and "/Programs" not in p and "/Users" not in p and "/.chen" not in p}
    say("H3 没有六件套+扩展面之外的消息面私有 API", not extra, f"extra={sorted(extra)[:8]}")


def check_h2_caps() -> None:
    print("\n=== CAPS. H-2 诚实性 ===")
    code = f'''
import sys, json
sys.path.insert(0, {str(ADAPTER)!r})
import importlib.util as ilu
spec = ilu.spec_from_file_location("p", {str(ADAPTER / "platform.py")!r})
m = ilu.module_from_spec(spec)
spec.loader.exec_module(m)
p = m.ChenCodePlatform("chencode")
print(json.dumps({{t: p.caps_for(t) for t in ("A", "B", "C")}}))
'''
    out = subprocess.run([sys.executable, "-c", code], capture_output=True,
                         text=True, encoding="utf-8", errors="replace", timeout=30)
    try:
        d = json.loads(out.stdout.strip().splitlines()[-1])
    except Exception:  # noqa: BLE001
        d = {}
        print("    stderr:", (out.stderr or "")[-200:])
    say("H2-A A 档 inject_session=false（只落摘要，未真注入）",
        (d.get("A") or {}).get("inject_session") is False, json.dumps(d.get("A")))
    say("H2-B B 档 inject_session=false", (d.get("B") or {}).get("inject_session") is False)
    say("H2-C C 档 queue=true 且 spawn_cwd=false",
        (d.get("C") or {}).get("queue") is True and (d.get("C") or {}).get("spawn_cwd") is False)


def check_enqueue_idempotent() -> None:
    print("\n=== ID. M-4 入队幂等 ===")
    code = f'''
import sys, json, tempfile
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0, {str(ADAPTER)!r})
import importlib.util as ilu
spec = ilu.spec_from_file_location("p", {str(ADAPTER / "platform.py")!r})
m = ilu.module_from_spec(spec)
spec.loader.exec_module(m)
td = Path(tempfile.mkdtemp())
p = m.ChenCodePlatform("chencode", data_dir=td)
env = SimpleNamespace(id="env-dup", from_="a", correlation_id="c",
                      meta={{}}, body="b", kind="message", subject="", reply_to="")
native = {{"text": "b"}}
route = {{"mode": "new", "session_id": "", "workspace": ".", "reason": "t"}}
id1 = p.enqueue_work(env, native, route)
id2 = p.enqueue_work(env, native, route)
rows = [json.loads(x) for x in p.work_path.read_text().splitlines() if x.strip()]
queued = [r for r in rows if r.get("state") == "queued"]
print(json.dumps({{"id1": id1, "id2": id2, "queued": len(queued)}}))
'''
    out = subprocess.run([sys.executable, "-c", code], capture_output=True,
                         text=True, encoding="utf-8", errors="replace", timeout=30)
    try:
        d = json.loads(out.stdout.strip().splitlines()[-1])
    except Exception:  # noqa: BLE001
        d = {}
        print("    stderr:", (out.stderr or "")[-200:])
    say("ID1 同一 envelope.id 只入队一次",
        d.get("id1") == d.get("id2") == "env-dup" and d.get("queued") == 1,
        json.dumps(d))


def wait_reply_inbox(base: str, peer_key: str, correlation_id: str,
                     marker: str, attempts: int = 50) -> dict | None:
    for _ in range(attempts):
        _, inbox = post(base, "/messages/receive", {
            "from": "peer.one", "peek": True, "unread_only": False,
            "limit": 50, "order": "oldest",
        }, peer_key)
        for m in (inbox.get("messages") or []):
            if (str(m.get("kind") or "") == "reply"
                    and str(m.get("from") or "") == MANAGED
                    and str(m.get("correlation_id") or "") == correlation_id
                    and marker in str(m.get("body") or "")):
                return m
        time.sleep(0.3)
    return None


def write_flags(rig: Rig, **flags: str) -> None:
    """写插件 data/adapter-flags.json——独立进程只能靠文件读运行时开关。"""
    data = rig.home / "plugins" / ADAPTER_ID / "data"
    data.mkdir(parents=True, exist_ok=True)
    payload = {k: v for k, v in flags.items() if v is not None}
    (data / "adapter-flags.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def check_success_reply(rig: Rig) -> None:
    """M-1：成功 headless stub → 回信（reply_to + correlation_id）E2E。"""
    print("\n=== E2E. 成功回信路径（stub）===")
    write_flags(rig,
                AGENT_ANP_CHEN_FORCE_FAIL="",
                AGENT_ANP_CHEN_SKIP_PREFLIGHT="1",
                AGENT_ANP_CHEN_STUB_REPLY="ANP-OK success-reply-marker")
    try:
        pkey = rig.peer_key()
        code, sent = post(rig.base, "/messages/send", {
            "from": "peer.one", "to": MANAGED,
            "body": "成功回信 E2E",
            "correlation_id": "c:success", "wake": True,
            "meta": {"need_reply": True, "context_id": "ctx-success"},
        }, pkey)
        say("E2E-1 need_reply 发信持久化", sent.get("ok") is True)
        msg = wait_reply_inbox(rig.base, pkey, "c:success", "ANP-OK success-reply-marker")
        say("E2E-2 ★成功回信落到 peer 箱（带对话线）★", msg is not None,
            f"reply_to={ (msg or {}).get('reply_to') }")
        say("E2E-3 reply_to 指回原信", bool(msg and msg.get("reply_to")),
            str((msg or {}).get("reply_to") or ""))

        # ALWAYS_RUN 知会：need_reply 缺省也必须回收产出（H-1）
        write_flags(rig,
                    AGENT_ANP_CHEN_FORCE_FAIL="",
                    AGENT_ANP_CHEN_SKIP_PREFLIGHT="1",
                    AGENT_ANP_CHEN_STUB_REPLY="ALWAYS-RUN notice-reply-marker",
                    AGENT_ANP_CHEN_ALWAYS_RUN="1")
        code2, sent2 = post(rig.base, "/messages/send", {
            "from": "peer.one", "to": MANAGED,
            "body": "ALWAYS_RUN 知会",
            "correlation_id": "c:always", "wake": True,
        }, pkey)
        say("E2E-4 ALWAYS_RUN 知会发信", sent2.get("ok") is True)
        msg2 = wait_reply_inbox(rig.base, pkey, "c:always", "ALWAYS-RUN notice-reply-marker")
        say("E2E-5 ★H-1 force_run 产出不静默：知会也回信★", msg2 is not None)
        data = rig.home / "plugins" / ADAPTER_ID / "data"
        work = data / "chencode-work.jsonl"
        detail_ok = False
        if work.is_file():
            rows = [json.loads(x) for x in work.read_text(encoding="utf-8").splitlines() if x.strip()]
            detail_ok = any(r.get("detail") == "replied" for r in rows)
        say("E2E-6 work 明细含 replied", detail_ok)
    finally:
        write_flags(rig,
                    AGENT_ANP_CHEN_STUB_REPLY="",
                    AGENT_ANP_CHEN_ALWAYS_RUN="",
                    AGENT_ANP_CHEN_SKIP_PREFLIGHT="1",
                    AGENT_ANP_CHEN_FORCE_FAIL="1")


if __name__ == "__main__":
    sys.exit(main())
