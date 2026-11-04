"""第 4 步第 1 片的自检：manifest 校验 + 注册表 + 双向握手 + 生命周期。

跑法：python -X utf8 tests/selftest_plugins.py
全部在临时 home 里跑，不碰真实信箱。
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

import anp_core as core      # noqa: E402
import anp_plugin as plugin  # noqa: E402

OK = FAIL = 0
SKIPPED: list[str] = []
TMP: Path
HOM: Path


def check(name: str, cond: bool, detail: str = "") -> None:
    global OK, FAIL
    OK += 1 if cond else 0
    FAIL += 0 if cond else 1
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  <- {detail}" if detail else ""))


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def _wait_events(fake: "FakePlugin", base: int, want: set, *, timeout: float = 12.0,
                 already: set | None = None) -> set:
    """等插件把 `want` 里的事件都收齐，返回已收到的事件类型集合。"""
    deadline = time.time() + timeout
    have = set(already or ())
    while time.time() < deadline:
        for e in fake.seen_events()[base:]:
            t = (e.get("body") or {}).get("type")
            if t:
                have.add(t)
        if want <= have:
            break
        time.sleep(0.3)
    return have


def _wait_agent_online(fake: "FakePlugin", agent: str, base: int,
                       timeout: float = 25.0) -> bool:
    """等**某一个身份**的 agent.online 到齐。

    ★为什么不能只等"事件类型出现过"★：事件扫描器一次扫描会把这期间上过线的身份
    一起补报，分几次扫描时报几条却不一。原来 V5/V6 只等"出现过 agent.online"就
    往下走，早到的那条（通常是插件自己的）会让人以为齐了。实测这就是 V6/V6b
    抖动的真因：不是事件没发，是**检查得太早**。

    A/B 实测（2026-09-22）：把扫描周期从默认 15s 压到 5s，失败率反而从 1/6 升到
    4/6——短周期把两次补报拆得更开。所以正确解法是「按身份等」，不是「改周期」。
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        for e in fake.seen_events()[base:]:
            b = e.get("body") or {}
            if b.get("type") == "agent.online" and b.get("agent") == agent:
                return True
        time.sleep(0.3)
    return False


def validate(**over) -> object:
    """校验一个基线 manifest，按 over 增删字段。返回 dict（通过）或错误码（拒绝）。"""
    m = {"id": "a.b", "name": "t", "version": "0.1.0", "api_level": 1,
         "command": ["python", "plugin.py"]}
    m.update(over)
    try:
        return plugin.validate_manifest(m)
    except plugin.ManifestError as e:
        return e.code


class FakePlugin:
    """一个真的会实现 `/anp/register` 的插件进程（让握手跑真 HTTP）。"""

    SEEN: dict = {}

    def __init__(self, root: Path, pid: str = "alice.echo", extra_perms: list | None = None):
        self.dir = root / pid.replace(".", "-")
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / "manifest.json").write_text(json.dumps({
            "id": pid, "name": "Echo", "version": "0.1.0", "api_level": 1,
            "command": [sys.executable, str(self.dir / "plugin.py")],
            "permissions": extra_perms or ["wake.others", "tools.register"],
        }, ensure_ascii=False), encoding="utf-8")
        (self.dir / "config.json").write_text(
            json.dumps({"board": True}, ensure_ascii=False), encoding="utf-8")
        (self.dir / "plugin.py").write_text('''
import json, os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
HERE = os.path.dirname(os.path.abspath(__file__))
class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_GET(self):
        if self.path == "/anp/panel":
            # 面板：原样返回一段内容，Core 必须一字不改地转出来
            resp = {"ok": True, "panel_of": os.environ.get("ANP_PLUGIN_ID"), "items": ["一", "二"]}
            b = json.dumps(resp, ensure_ascii=False).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json; charset=utf-8")
            self.send_header("content-length", str(len(b))); self.end_headers()
            self.wfile.write(b)
            return
        self.send_response(404); self.send_header("content-length", "0"); self.end_headers()
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("content-length") or 0)).decode("utf-8") or "{}")
        if self.path == "/anp/events":
            # 事件同样写文件（这也是另一个进程，测试进程里的对象它看不见）
            # 用 print(..., file=fp) 而不是字符串拼换行符：这段代码本身是测试进程里一个
            # 三引号字符串中的源码，任何转义序列都会被提前解释，写出去就不是这段代码了。
            with open(os.path.join(HERE, "seen_events.jsonl"), "a", encoding="utf-8") as fp:
                print(json.dumps({"body": body, "key": self.headers.get("x-mailbox-key")},
                                 ensure_ascii=False), file=fp)
            resp = {"ok": True, "received": str(body.get("type") or "")}
            b = json.dumps(resp, ensure_ascii=False).encode()
            self.send_response(200); self.send_header("content-length", str(len(b)))
            self.end_headers(); self.wfile.write(b)
            return
        if self.path == "/anp/tool":
            # ★写文件，不写内存★：这是另一个进程，测试进程里的 SEEN 它看不见
            # （曾经在这里引用测试模块的 FakePlugin.SEEN → 子进程 NameError →
            #  handler 崩 → hub 侧只看到 RemoteDisconnected，非常难查）
            open(os.path.join(HERE, "seen_tool.json"), "w", encoding="utf-8").write(json.dumps(
                {"body": body, "key": self.headers.get("x-mailbox-key")}, ensure_ascii=False))
            tool = str(body.get("name") or "")
            if tool == "boom":
                resp = {"ok": False, "error": "插件自己炸了"}
            else:
                resp = {"ok": True, "text": "echo:" + str((body.get("arguments") or {}).get("said", ""))}
            b = json.dumps(resp, ensure_ascii=False).encode()
            self.send_response(200); self.send_header("content-length", str(len(b)))
            self.end_headers(); self.wfile.write(b)
            return
        if self.path == "/anp/register":
            # 把 Core 送来的凭证与配置原样落盘，供测试断言
            open(os.path.join(HERE, "seen.json"), "w", encoding="utf-8").write(json.dumps(
                {"body": body, "key": self.headers.get("x-mailbox-key")}, ensure_ascii=False))
            resp = {"ok": True,
                    "tools": [
                        {"name": "echoback", "description": "回显",
                         "input_schema": {"type": "object",
                                          "properties": {"said": {"type": "string"}},
                                          "required": ["said"]}},
                        {"name": "boom", "description": "故意失败", "input_schema": {"type": "object"}}],
                    "events": ["agent.online", "agent.offline", "message.delivered",
                               "message.undelivered"],
                    "events_url": "http://127.0.0.1:" + os.environ["ANP_PLUGIN_PORT"] + "/anp/events",
                    "panel": "/anp/panel"}
        else:
            resp = {"ok": True, "mode": "session"}
        b = json.dumps(resp, ensure_ascii=False).encode()
        self.send_response(200); self.send_header("content-length", str(len(b)))
        self.end_headers(); self.wfile.write(b)
ThreadingHTTPServer(("127.0.0.1", int(os.environ["ANP_PLUGIN_PORT"])), H).serve_forever()
''', encoding="utf-8")
        self.id = pid

    def seen(self) -> dict:
        p = self.dir / "seen.json"
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}

    def seen_tool(self) -> dict:
        p = self.dir / "seen_tool.json"
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}

    def seen_events(self) -> list[dict]:
        p = self.dir / "seen_events.jsonl"
        if not p.exists():
            return []
        out = []
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return out


class Hub:
    def __init__(self, home: Path, port: int, proc: subprocess.Popen):
        self.home, self.port, self.proc = home, port, proc
        self.base = f"http://127.0.0.1:{port}"

    def wait(self, tries: int = 80) -> bool:
        """等 HTTP 能响应。

        ★P0-1（2026-09-23）★：serve 线程现在**先于**插件预加载启动（否则启动阶段
        /health 连不上，插件回调 Hub 还会对等死锁）。所以这里返回只表示
        "HTTP 通了"，**不表示"插件已就绪"**——要断言插件工具/面板，
        先用 `wait_plugins()`。
        """
        for _ in range(tries):
            try:
                urllib.request.urlopen(self.base + "/health", timeout=1)
                return True
            except Exception:  # noqa: BLE001
                time.sleep(0.25)
        return False

    def wait_plugins(self, tries: int = 60) -> bool:
        """等插件预加载跑完（轮询 /health 的 preload_done）。

        别用 sleep 猜时间：插件就绪快慢取决于机器与插件自身。
        """
        for _ in range(tries):
            try:
                with urllib.request.urlopen(self.base + "/health", timeout=2) as r:
                    d = json.loads(r.read().decode("utf-8"))
                if d.get("preload_done"):
                    return True
            except Exception:  # noqa: BLE001
                pass
            time.sleep(0.25)
        return False

    def call(self, path: str, body: dict, admin: str = "", key: str = "") -> tuple[int, dict]:
        data = json.dumps(body, ensure_ascii=False).encode()
        headers = {"content-type": "application/json"}
        if key:
            headers["x-mailbox-key"] = key
        if admin:
            headers["x-anp-admin"] = admin
        try:
            with urllib.request.urlopen(urllib.request.Request(self.base + path, data=data,
                                                               headers=headers, method="POST"),
                                        timeout=30) as r:
                return r.status, json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            try:
                return e.code, json.loads(e.read().decode() or "{}")
            except json.JSONDecodeError:
                return e.code, {}

    def get(self, path: str, admin: str = "") -> dict:
        headers = {"x-anp-admin": admin} if admin else {}
        with urllib.request.urlopen(urllib.request.Request(self.base + path, headers=headers),
                                    timeout=25) as r:
            return json.loads(r.read().decode())

    def get_raw(self, path: str, admin: str = "") -> tuple[int, str, str]:
        """原样取（面板代理要验 content-type 与正文是否被改过）。"""
        headers = {"x-anp-admin": admin} if admin else {}
        try:
            with urllib.request.urlopen(urllib.request.Request(self.base + path, headers=headers),
                                        timeout=25) as r:
                return r.status, r.read().decode("utf-8", "replace"),                     str(r.headers.get("content-type") or "")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "replace"),                 str(e.headers.get("content-type") or "")

    def kill(self) -> None:
        try:
            self.proc.kill()
        except Exception:  # noqa: BLE001
            pass


def main() -> int:
    global TMP, HOM
    TMP = Path(tempfile.mkdtemp(prefix="anp-plug-"))
    HOM = Path(tempfile.mkdtemp(prefix="anp-plughome-"))
    (TMP / "plugins").mkdir(parents=True, exist_ok=True)

    print("=== A. manifest 校验：command / url 严格二选一（定稿规则 §2.2）===")
    check("A1 只给 command 通过", isinstance(validate(), dict), str(validate())[:80])
    check("A2 两个都有 → manifest.invalid", validate(url="http://127.0.0.1:9000/") == "manifest.invalid")
    check("A3 两个都没有 → manifest.invalid",
          validate(command=None) == "manifest.invalid")
    check("A4 command=[] → manifest.invalid", validate(command=[]) == "manifest.invalid")
    check("A5 command 元素非字符串 → manifest.invalid", validate(command=["ok", 3]) == "manifest.invalid")
    check("A6 url 非回环 → manifest.invalid", validate(command=None, url="http://8.8.8.8/") == "manifest.invalid")
    check("A7 url 回环合法", validate(command=None, url="http://localhost:9100") != "manifest.invalid")

    print("\n=== B. manifest 其它硬规则 ===")
    check("B1 缺 id → invalid", validate(id=None) == "manifest.invalid")
    check("B2 id 非小写 → invalid", validate(id="Alice.W") == "manifest.invalid")
    check("B3 id 带路径符 → invalid", validate(id="../evil") == "manifest.invalid")
    check("B4 id 首字符非法 → invalid", validate(id="-x") == "manifest.invalid")
    check("B5 api_level 是字符串 → invalid", validate(api_level="1") == "manifest.invalid")
    check("B6 api_level=0 → invalid", validate(api_level=0) == "manifest.invalid")
    check("B7 未知权限 → invalid", validate(permissions=["fly"]) == "manifest.invalid")
    check("B8 未知字段被忽略（不报错）", isinstance(validate(foo={"a": 1}, tools=[{"name": "x"}]), dict))
    a = validate(permissions=["broadcast", "wake.others"])["permissions"]
    b = validate(permissions=["wake.others", "broadcast"])["permissions"]
    check("B8b permissions 输出确定性（两种声明顺序得到同一份结果）",
          a == b and a == sorted(a), f"{a} vs {b}")
    check("B9 requires.plugins 非数组 → invalid", validate(requires={"plugins": "alice"}) == "manifest.invalid")
    try:
        plugin.validate_manifest([1, 2])
        check("B10 顶层不是对象 → invalid", False, "竟然放过了")
    except plugin.ManifestError as e:
        check("B10 顶层不是对象 → invalid", e.code == "manifest.invalid", e.detail)

    print("\n=== C. api_level 整数方案（定稿 §2.3）===")
    try:
        plugin.check_compat({"api_level": core.CORE_API_LEVEL + 1, "anp_min": None})
        check("C1 plugin.api_level > core → 拒", False, "竟然放过了")
    except plugin.ManifestError as e:
        check("C1 plugin.api_level > core → api.level.unsupported", e.code == "api.level.unsupported", e.detail)
    plugin.check_compat({"api_level": core.CORE_API_LEVEL, "anp_min": None})
    check("C2 api_level == core 通过", True)
    plugin.check_compat({"api_level": max(1, core.CORE_API_LEVEL - 1), "anp_min": None})
    check("C3 api_level 低于 core 也通过（向后兼容）", True)
    try:
        plugin.check_compat({"api_level": 1, "anp_min": "99.0.0"})
        check("C4 anp_min 高于当前 → 拒", False, "竟然放过了")
    except plugin.ManifestError as e:
        check("C4 anp_min 高于当前 → api.level.unsupported", e.code == "api.level.unsupported")
    plugin.check_compat({"api_level": 1, "anp_min": "0.1.0"})
    check("C5 anp_min 低于当前通过", True)

    print("\n=== D. 双向握手：两个 register 方向相反 ===")
    home = Path(tempfile.mkdtemp(prefix="anp-h1-"))
    (home / "plugins").mkdir(parents=True, exist_ok=True)
    # 直接建在 hub 的 plugins/ 里：manifest 的 command 里嵌的是插件脚本的绝对路径，
    # 复制目录会让"启动的脚本"和"Core 认的目录"不是同一个地方。
    fake = FakePlugin(home / "plugins", "alice.echo",
                     ["wake.others", "tools.register", "webui.panel"])
    port = free_port()
    # ★把事件扫描周期压到 1 秒★（根因 2026-09-22）：扫描器默认 15 秒一轮
    # （`max(5.0, AGENT_ANP_EVENT_SCAN_SEC)`），而下面 V5/V6/V6b 只等 15-20 秒，
    # 窗口里容不下两次扫描；相位没对齐时属于某身份的 agent.online 就发不出来，
    # 表现为约 1/4 概率固定挂 V6/V6b（单独跑反而不挂）。hub 是这里起的子进程，
    # 所以由测试给一个短周期，不去改 Core 的轮询设计。
    proc = subprocess.Popen([sys.executable, str(SRC / "anp_hub.py"), "--host", "127.0.0.1",
                             "--port", str(port), "--home", str(home)],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            env={**os.environ, "AGENT_ANP_EVENT_SCAN_SEC": "1"})
    h = Hub(home, port, proc)
    admin = ""
    try:
        check("D0 hub 起来了", h.wait())
        check("D0b 插件预加载完成", h.wait_plugins())
        admin = (home / "admin.token").read_text(encoding="utf-8").strip()

        code, r = h.call("/plugins/alice.echo/load", {}, admin)
        check("D1 load：拉起进程 + POST 插件 /anp/register", r.get("ok") is True,
              json.dumps(r, ensure_ascii=False)[:160])
        seen = fake.seen()
        cfg = seen.get("body", {}).get("config") or {}
        check("D2 插件收到 身份/token/config/目录（Core → Plugin）",
              (seen.get("body", {}).get("identity") == "alice.echo"
               and bool(seen.get("body", {}).get("token"))
               and cfg.get("board") is True
               and str(cfg.get("hub_port") or "").isdigit()
               and seen["body"].get("plugin_dir") == str(fake.dir)),
              json.dumps(seen.get("body", {}), ensure_ascii=False)[:140])
        check("D3 ★收到的 token 是插件自己的 agent token，不是 hub.secret★",
              seen.get("key") == core.key_for("alice.echo", home)
              and seen.get("key") != core.hub_secret(home))
        check("D4 Core 记下插件回传的贡献",
              "echoback" in (r.get("contrib", {}).get("tools") or []))

        plugins = h.get("/plugins")["plugins"]
        view = next((p for p in plugins if p["id"] == "alice.echo"), {})
        check("D5 /plugins 列出它（state=enabled）", view.get("state") == "enabled")
        check("D6 列表不泄露完整 token（只给前 8 位）",
              view.get("token_hint", "").endswith("…") and len(view.get("token_hint", "")) <= 9)
        check("D7 /health.plugins 有它 + 架构自检",
              h.get("/health")["plugins"]["total"] == 1
              and h.get("/health")["plugins"]["secret_isolation"] == "isolated")

        print("\n=== E. /plugin/register（Plugin → Core，另一个方向）===")
        # 先 load 一次，把贡献表恢复成 manifest/握手时的样子（后面 E4 会覆盖它）
        h.call("/plugins/alice.echo/reload", {}, admin)
        # ★C1 方案 P（2026-09-23）★：报到用**插件自己**的 agent token，不再用 admin。
        # 注意 h.call(path, body, admin, key) 的第三个位置参数是 admin——
        # ★令牌必须放第四个（key）★，否则会进 x-anp-admin 头，
        # 而鉴权读的是 x-mailbox-key（第一版就这么把 E1-E4 全打红过）。
        # E2/E3/E3b 三条断言本来就是对的，正好当护栏。
        p_tok = core.key_for("alice.echo", home)
        code, r2 = h.call("/plugin/register", {"id": "alice.echo",
                                               "tools": [{"name": "reannounced", "description": "重新声明"}],
                                               "events": ["message.delivered"]}, "", p_tok)
        check("E1 插件主动报到成功", r2.get("ok") is True and r2.get("identity") == "alice.echo")
        check("E2 响应给的是插件自己的 token（不是 hub.secret）",
              bool(r2.get("token")) and r2["token"] == core.key_for("alice.echo", home)
              and r2["token"] != core.hub_secret(home))
        check("E3 响应带 config / plugin_dir / api_level / permissions",
              (r2.get("config") or {}).get("board") is True
              and str((r2.get("config") or {}).get("hub_port") or "").isdigit()
              and r2.get("plugin_dir") == str(fake.dir)
              and r2.get("api_level") == core.CORE_API_LEVEL
              and "tools.register" in (r2.get("permissions") or []))
        check("E3b ★响应体不含 hub.secret（永久架构约束）★",
              core.hub_secret(home) not in json.dumps(r2, ensure_ascii=False)
              and core.hub_secret(home) not in json.dumps(seen, ensure_ascii=False))
        plugins2 = h.get("/plugins")["plugins"]
        check("E4 贡献表被插件自己覆盖（reannounced 出现）",
              any(p["id"] == "alice.echo" and "reannounced" in (p.get("tools") or []) for p in plugins2))
        code, r3 = h.call("/plugin/register", {"id": "alice.echo"}, "")
        check("E5 不带令牌报到被拒（401）", code == 401 and r3.get("ok") is False)
        # ★C1 负例：admin 令牌在这个端点明确不被接受★（不留运维旁路）
        code, r3b = h.call("/plugin/register", {"id": "alice.echo"}, admin)
        check("E5b ★admin 令牌报到被拒（401）★",
              code == 401 and r3b.get("ok") is False
              and r3b.get("code") == "auth.plugin-credential",
              f"code={code} err={r3b.get('code')}")
        # ★C1 负例：拿别的身份的令牌报到本插件 → 拒★
        code, r3c = h.call("/plugin/register", {"id": "alice.echo"},
                           core.key_for("alice.echo.user", home) or "not-a-token")
        check("E5c ★错身份的令牌报到被拒（401）★", code == 401)
        # E6：未加载插件从没发过 key → 鉴权步就 401（不是 404），
        # 这样不对外泄露"这个插件有没有被加载"
        code, r4 = h.call("/plugin/register", {"id": "ghost.plugin"}, admin)
        check("E6 ★未加载插件报到 → 401（不是 404）★"
              "——鉴权在前，不泄露加载状态",
              code == 401 and r4.get("ok") is False, f"code={code}")

        print("\n=== E2. Tool Proxy：插件工具被发现、被调用、被转发 ===")
        # E4 把贡献表覆盖成了单个 reannounced，这里 reload 恢复握手时的两个工具
        h.call("/plugins/alice.echo/reload", {}, admin)
        tools = h.get("/tools")
        names = [t["name"] for t in tools.get("plugin_tools") or []]
        check("T1 GET /tools 列出插件工具（点号已映射成下划线）",
              "alice_echo_echoback" in names and "alice_echo_boom" in names, str(names))
        check("T2 保留规范名（插件侧仍用点号那个）",
              any(t.get("canonical") == "alice.echo.echoback"
                  for t in tools.get("plugin_tools") or []))
        check("T3 schema 原样透传（Core 不解释）",
              any((t.get("inputSchema") or {}).get("required") == ["said"]
                  for t in tools.get("plugin_tools") or []))
        check("T4 裸点号名不泄漏给 MCP（MCP 名不含点）",
              all("." not in t["name"] for t in tools.get("plugin_tools") or []))

        # 用一个真实 agent 身份去调（要过 _auth，且 caller_identity 必须是真的调用方）
        ak = core.key_for("caller.one", home)
        code, tr = h.call("/tool", {"server": "caller.one", "name": "alice_echo_echoback",
                                    "args": {"said": "hello"}}, "", ak)
        check("T5 通过 /tool 调用插件工具成功",
              tr.get("ok") is True and tr.get("mode") == "plugin"
              and tr.get("plugin") == "alice.echo" and tr.get("text") == "echo:hello",
              json.dumps(tr, ensure_ascii=False)[:160])
        seen_tool = fake.seen_tool()
        check("T6 转发给插件的 name 是**规范名**（不是映射后的 MCP 名）",
              (seen_tool.get("body") or {}).get("name") == "echoback",
              json.dumps(seen_tool.get("body", {}), ensure_ascii=False)[:120])
        check("T7 带上了 caller_identity（插件能自己鉴权）",
              (seen_tool.get("body") or {}).get("caller_identity") == "caller.one")
        check("T8 arguments 原样透传",
              (seen_tool.get("body") or {}).get("arguments") == {"said": "hello"})
        check("T9 ★转发时带的是插件自己的 token，不是 hub.secret★",
              seen_tool.get("key") == core.key_for("alice.echo", home)
              and seen_tool.get("key") != core.hub_secret(home))

        code, tb = h.call("/tool", {"server": "caller.one", "name": "alice_echo_boom", "args": {}}, "", ak)
        check("T10 插件自己失败 → plugin.error（不冒充成功）",
              tb.get("ok") is False and tb.get("code") == "plugin.error"
              and "插件自己炸了" in str(tb.get("error")), json.dumps(tb, ensure_ascii=False)[:140])
        code, tn = h.call("/tool", {"server": "caller.one", "name": "alice_echo_nothere", "args": {}}, "", ak)
        check("T11 调用插件没注册的工具 → 未知工具", code in (400, 404) or tn.get("ok") is False)

        # MCP 侧：tools/list 合并 + 渲染。
        # ★第一轮 Core Extraction★：MCP 是 adapter，不在 Core 仓库 → 这几项跳过并记账。
        try:
            import anp_mcp as mcp
        except ImportError:
            SKIPPED.append("T12/T13/T14（MCP 渲染与 tools/list 合并，随 adapters/ 测）")
            mcp = None
        if mcp is not None:
            # MCP 的 HOME/HUB 默认指向真实信箱；测试要指到这一轮的临时实例
            mcp.HOME, mcp.HUB = home, h.base
            listed, clashes = mcp.plugin_tools()
            check("T12 MCP 的 tools/list 合并了插件工具",
                  "alice_echo_echoback" in [x["name"] for x in listed])
            txt, is_err = mcp.render("alice_echo_echoback", tr)
            check("T13 渲染用插件返回的文本", txt == "echo:hello" and not is_err)
            txt2, is_err2 = mcp.render("alice_echo_boom", tb)
            check("T14 失败时渲染成错误（不是静默成功）", is_err2 is True and "插件自己炸了" in txt2)
            check("T13b 未知工具名不被 alias 解析成别的工具",
                  mcp.mcp_tool_alias("nope", "nope") == "nope")

        print("\n=== E3. Event Proxy：四个事件 + 旁路不阻塞 + 三者独立 ===")
        # 先把事件文件清干净，并给足扫描器一次机会感知当前在线集
        (fake.dir / "seen_events.jsonl").unlink(missing_ok=True)
        time.sleep(1.0)
        base_events = len(fake.seen_events())

        # ① message.delivered：给插件自己发一封并唤醒它
        code, sd = h.call("/tool", {"server": "caller.one", "name": "mail_send",
                                    "args": {"to": "alice.echo", "body": "触发 delivered",
                                             "kind": "test.delivered", "wake": True}}, "", ak)
        check("V1 发信成功", sd.get("ok") is True, json.dumps(sd, ensure_ascii=False)[:120])
        got = _wait_events(fake, base_events, {"message.delivered"}, timeout=12)
        check("V2 ★message.delivered 推到了订阅的插件★", "message.delivered" in got, str(sorted(got)))
        ev = next((e["body"] for e in fake.seen_events()
                   if (e.get("body") or {}).get("type") == "message.delivered"), {})
        check("V3 事件带信封坐标（id / to / kind）",
              bool(ev.get("id")) and ev.get("to") == "alice.echo"
              and ev.get("kind") == "test.delivered", json.dumps(ev, ensure_ascii=False)[:140])
        check("V3b ★事件推送带的是插件自己的 token，不是 hub.secret★",
              all(e.get("key") == core.key_for("alice.echo", home)
                  and e.get("key") != core.hub_secret(home) for e in fake.seen_events()))

        # ② agent.online：新身份来心跳一次
        before = set(got)
        nk = core.key_for("fresh.agent", home)
        code, _ = h.call("/heartbeat", {"name": "fresh.agent"}, "", nk)
        check("V4 新身份心跳成功", code == 200)
        got2 = _wait_events(fake, base_events, {"agent.online"}, timeout=20,
                            already=before)
        check("V5 ★agent.online 推到了订阅的插件★", "agent.online" in got2, str(sorted(got2)))
        # ★必须按身份等，不能只看"类型出现过"★：扫描器一次扫描会把这期间上过线的
        # 身份一起补报，分次扫描时报几条不一。只等类型的话，早到的 alice.echo 会让
        # 这里提前返回，fresh.agent 的还没发出来就判失败（V6/V6b 抖动的真因）。
        got_agent = _wait_agent_online(fake, "fresh.agent", base_events, timeout=25.0)
        online_evs = [(e.get("body") or {}).get("agent")
                      for e in fake.seen_events()
                      if (e.get("body") or {}).get("type") == "agent.online"]
        ev2 = next((e["body"] for e in fake.seen_events()
                    if (e.get("body") or {}).get("type") == "agent.online"
                    and (e.get("body") or {}).get("agent") == "fresh.agent"), {})
        check("V6 agent.online 带 agent 名（指到这个身份）",
              got_agent and bool(ev2),
              # 失败时把证据一次给全，否则每次都要靠猜：
              #   收到的 agent.online 有哪些 / 当时在线的身份 / 事件总线计数
              f"agent.online={online_evs}｜在线={core.online(home=home)}"
              f"｜总线={json.dumps(h.get('/health').get('events'), ensure_ascii=False)}")
        check("V6b 新上线的身份都被报到（不挑食）", "fresh.agent" in online_evs,
              f"{online_evs}")

        # ③ 旁路不阻塞：事件队列满/订阅者全死也不能让发信变慢或失败
        t0 = time.time()
        for i in range(5):
            h.call("/tool", {"server": "caller.one", "name": "mail_send",
                             "args": {"to": "caller.one", "body": f"连发 {i}", "wake": False}},
                   "", ak)
        elapsed = time.time() - t0
        check("V7 事件旁路不阻塞发信（5 封连发 < 3 秒）", elapsed < 3.0, f"{elapsed:.2f}s")

        # ④ /health.events 可观测
        st = h.get("/health").get("events") or {}
        check("V8 /health.events 有入队/送达计数",
              st.get("queued", 0) > 0 and st.get("delivered", 0) > 0,
              json.dumps(st, ensure_ascii=False))

        # ⑤ 三者独立：进程活着 ≠ 端点可达 ≠ 状态 enabled
        print("\n=== E3b. Panel Proxy：/admin/panel/<id> 原样代理 ===")
        pls = h.get("/admin/panel", admin)
        check("P1 GET /admin/panel 列出有面板的插件",
              any(x.get("plugin_id") == "alice.echo" for x in (pls.get("panels") or [])),
              json.dumps(pls, ensure_ascii=False)[:140])

        code, body, ctype = h.get_raw("/admin/panel/alice.echo", admin)
        check("P2 代理返回 200 且 content-type 不被人为改写",
              code == 200 and "json" in ctype.lower(), f"{code} {ctype}")
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            payload = {}
        check("P3 ★面板内容一字不改地透传（Core 不清洗不解析）★",
              payload.get("items") == ["一", "二"] and payload.get("panel_of") == "alice.echo",
              body[:120])
        check("P4 正文里的中文没被清洗掉", "一" in body and "二" in body)

        code, _, _ = h.get_raw("/admin/panel/alice.echo", "")
        check("P5 无管理员令牌 → 401", code == 401)
        code, _, _ = h.get_raw("/admin/panel/alice.echo", "wrong")
        check("P6 错管理员令牌 → 401", code == 401)
        code, body4, _ = h.get_raw("/admin/panel/ghost.plugin", admin)
        check("P7 不存在的插件 → 4xx 且不是假成功",
              code >= 400 and json.loads(body4 or "{}").get("ok") is not True, f"{code}")

        # 没有 webui.panel 权限的插件：拿不到面板
        np = home / "plugins" / "nopanel"
        np.mkdir(parents=True, exist_ok=True)
        (np / "manifest.json").write_text(json.dumps({
            "id": "nopanel.one", "name": "NoPanel", "version": "0.1.0", "api_level": 1,
            "url": "http://127.0.0.1:1/", "permissions": []}, ensure_ascii=False), encoding="utf-8")
        h.call("/plugins/nopanel.one/load", {}, admin)
        code5, body5, _ = h.get_raw("/admin/panel/nopanel.one", admin)
        check("P8 无 webui.panel 权限 → 403 permission.denied",
              code5 == 403 and json.loads(body5 or "{}").get("code") == "permission.denied",
              f"{code5} {body5[:80]}")
        h.call("/plugins/nopanel.one/unload", {}, admin)

        print("\n=== E4. 三者独立性（plugin state / process alive / endpoint reachable）===")
        code, dz = h.call("/plugins/alice.echo/disable", {}, admin)
        check("W1 disable 后 state=disabled（进程被 Core 终止）",
              any(p["id"] == "alice.echo" and p["state"] == "disabled"
                  for p in h.get("/plugins")["plugins"]))
        entry_dz = plugin.read_registry(home)["alice.echo"]
        check("W2 注册条目仍记着 pid/core_spawned（事实留档，不当成状态抹掉）",
              entry_dz.get("core_spawned") is True and bool(entry_dz.get("pid")))
        code, tz = h.call("/tool", {"server": "caller.one", "name": "alice_echo_echoback",
                                   "args": {"said": "x"}}, "", ak)
        # disabled 的插件工具**根本不在工具表里**（tool_registry 只收 enabled），
        # 所以调用方看到的是"未知工具"而不是 permission.denied——这是诚实的：
        # 停用的插件对外就像不存在。断言"办不成 + 没有冒充成功"即可，不限定错误码。
        check("W3 disabled 时调它的工具办不成（且不像成功）",
              tz.get("ok") is False, json.dumps(tz, ensure_ascii=False)[:120])
        check("W4 disabled 时订阅也被停（不再收事件）",
              not plugin.EventBus(home).subscribers("message.delivered"))
        code, en = h.call("/plugins/alice.echo/enable", {}, admin)
        check("W5 enable 重新拉起（state 回到 enabled）",
              any(p["id"] == "alice.echo" and p["state"] == "enabled"
                  for p in h.get("/plugins")["plugins"]))

        # ⑥ 端点不可达但 state 仍是 enabled：这才是"进程自己死了"的真实形态
        pid_alive = entry_dz.get("pid")
        code, _ = h.call("/plugins/alice.echo/enable", {}, admin)
        alive_entry = plugin.read_registry(home)["alice.echo"]
        try:
            import os as _os
            _os.kill(int(alive_entry.get("pid") or 0), 9)
            killed_outside = True
        except Exception:  # noqa: BLE001
            killed_outside = False
        time.sleep(0.5)
        code, td = h.call("/tool", {"server": "caller.one", "name": "alice_echo_echoback",
                                    "args": {"said": "y"}}, "", ak)
        check("W6 ★外部杀掉进程后：state 仍是 enabled，但端点不可达★",
              any(p["id"] == "alice.echo" and p["state"] == "enabled"
                  for p in h.get("/plugins")["plugins"])
              and td.get("ok") is False and td.get("code") in ("endpoint.unreachable", "timeout"),
              f"state=enabled 且 code={td.get('code')}")
        check("W7 端点不可达不影响信箱本体（同一次调用后 hub 还活着）",
              h.get("/health").get("ok") is True)
        _ = pid_alive, killed_outside

        print("\n=== F. 生命周期四动词 + 进程事实三分离 ===")
        code, r5 = h.call("/plugins/alice.echo/disable", {}, admin)
        check("F1 disable 成功并终止 Core 拉起的进程",
              r5.get("ok") is True and r5.get("terminated_core_spawned") is True)
        time.sleep(0.5)
        try:
            h.get("/health")
            alive = True
        except Exception:  # noqa: BLE001
            alive = False
        check("F2 插件进程死了，hub 照样活着（Core 不保活、不重启）", alive)
        check("F2b disable 后插件工具从 /tools 消失（不是缓存表）",
              "alice_echo_echoback" not in [t["name"] for t in h.get("/tools").get("plugin_tools") or []])
        code, r6 = h.call("/plugins/alice.echo/enable", {}, admin)
        check("F3 enable 重新拉起 + 重新握手", r6.get("ok") is True)
        code, r7 = h.call("/plugins/alice.echo/reload", {}, admin)
        check("F4 reload = unload + load", r7.get("ok") is True)
        code, r8 = h.call("/plugins/alice.echo/unload", {}, admin)
        check("F5 unload 成功并终止进程",
              r8.get("ok") is True and r8.get("terminated_core_spawned") is True)
        check("F6 unload 后 /plugins 里没有它",
              not any(p["id"] == "alice.echo" for p in h.get("/plugins")["plugins"]))
        code, r9 = h.call("/plugins/alice.echo/bogus", {}, admin)
        check("F7 未知动作被拒", code in (400, 409) and r9.get("ok") is False)
        code, _ = h.call("/plugins/alice.echo/load", {}, "wrong-token")
        check("F8 管理面要管理员令牌（错令牌 → 401）", code == 401)
    finally:
        h.kill()

    print("\n=== G. 坏 manifest 不让 hub 起不来 ===")
    home2 = Path(tempfile.mkdtemp(prefix="anp-h2-"))
    (home2 / "plugins").mkdir(parents=True, exist_ok=True)
    bad = home2 / "plugins" / "broke.one"
    bad.mkdir(parents=True, exist_ok=True)
    # command 和 url 都有 → 严格二选一判它坏
    (bad / "manifest.json").write_text(
        '{"id":"broke.one","name":"x","version":"1","api_level":1,'
        '"command":["python"],"url":"http://127.0.0.1:1/"}', encoding="utf-8")
    port2 = free_port()
    proc2 = subprocess.Popen([sys.executable, str(SRC / "anp_hub.py"), "--host", "127.0.0.1",
                              "--port", str(port2), "--home", str(home2)],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    h2 = Hub(home2, port2, proc2)
    try:
        check("G1 有坏 manifest 的 hub 仍然起来了", h2.wait())
        check("G1b 预加载已结束（坏 manifest 不该阻止 done 置位）", h2.wait_plugins())
        admin2 = (home2 / "admin.token").read_text(encoding="utf-8").strip()
        code, r10 = h2.call("/plugins/broke.one/load", {}, admin2)
        check("G2 手动 load 坏 manifest → manifest.invalid",
              r10.get("ok") is False and r10.get("code") == "manifest.invalid",
              json.dumps(r10, ensure_ascii=False)[:120])
        check("G3 坏插件没进注册表",
              not any(p["id"] == "broke.one" for p in h2.get("/plugins")["plugins"]))
    finally:
        h2.kill()

    print("\n=== H. 公告栏已是一个普通插件（Core 里没有公告概念）===")
    home3 = Path(tempfile.mkdtemp(prefix="anp-h3-"))
    shutil.copytree(Path(__file__).resolve().parents[1] / "plugins", home3 / "plugins")
    port3 = free_port()
    proc3 = subprocess.Popen([sys.executable, str(SRC / "anp_hub.py"), "--host", "127.0.0.1",
                              "--port", str(port3), "--home", str(home3)],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    h3 = Hub(home3, port3, proc3)
    try:
        check("H0 hub 带插件目录正常起来", h3.wait())
        # ★P0-1（2026-09-23）★：必须显式等插件就绪——HTTP 通了不代表插件加载完了
        check("H0b 插件预加载已完成", h3.wait_plugins())
        admin3 = (home3 / "admin.token").read_text(encoding="utf-8").strip()
        pnames = [t["name"] for t in (h3.get("/tools").get("plugin_tools") or [])]
        check("H1 公告栏的两个工具以插件工具出现",
              "anp_bulletin_post" in pnames and "anp_bulletin_list" in pnames, str(pnames))
        check("H2 规范名带命名空间（Core 不知道'公告'，只知道 anp.bulletin.post）",
              any(t.get("canonical") == "anp.bulletin.post"
                  for t in (h3.get("/tools").get("plugin_tools") or [])))
        panels = h3.get("/admin/panel", admin3)
        check("H3 公告栏提供了面板（只走 /admin/panel 这一个口子）",
              any(x.get("plugin_id") == "anp.bulletin" for x in (panels.get("panels") or [])))

        k3 = core.key_for("poster", home3)
        code, rp = h3.call("/tool", {"server": "poster", "name": "anp_bulletin_post",
                                     "args": {"body": "全体注意，第 4 步第 2 片完成",
                                              "subject": "进度通报"}}, "", k3)
        check("H4 anp_bulletin_post 成功", rp.get("ok") is True,
              json.dumps(rp, ensure_ascii=False)[:150])
        check("H4b 公告数据落在插件自己的目录里（不碰 home）",
              (home3 / "plugins" / "anp.bulletin" / "data" / "bulletin.jsonl").exists()
              and not (home3 / "bulletin.jsonl").exists())
        code, rl = h3.call("/tool", {"server": "poster", "name": "anp_bulletin_list",
                                     "args": {"limit": 5}}, "", k3)
        check("H5 anp_bulletin_list 能读回刚贴的公告",
              rl.get("ok") is True and "第 4 步第 2 片完成" in str(rl.get("text")),
              str(rl.get("text"))[:120])
        code, body, _ = h3.get_raw("/admin/panel/anp.bulletin", admin3)
        check("H6 面板里也看得到这条公告", code == 200 and "第 4 步第 2 片完成" in body)

        (home3 / "bulletin.jsonl").write_text(json.dumps(
            {"id": "b-old", "ts": 1.0, "from": "someone", "subject": "旧公告",
             "body": "升级前的公告", "kind": "announcement"}, ensure_ascii=False)
            + chr(10), encoding="utf-8")
        (home3 / "plugins" / "anp.bulletin" / "data" / "bulletin.jsonl").unlink()
        h3.kill()
        time.sleep(1.0)
        proc3b = subprocess.Popen([sys.executable, str(SRC / "anp_hub.py"), "--host", "127.0.0.1",
                                   "--port", str(port3), "--home", str(home3)],
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        h3 = Hub(home3, port3, proc3b)
        check("H7 重启后 hub 正常", h3.wait())
        # ★P0-1（2026-09-23）★：重启后同样要等预加载——否则插件工具还没注册，
        # 第一个 anp_bulletin_list 就是"未知工具"
        check("H7b 重启后插件预加载完成", h3.wait_plugins())
        code, rl2 = h3.call("/tool", {"server": "poster", "name": "anp_bulletin_list",
                                      "args": {}}, "", k3)
        check("H8 旧 bulletin.jsonl 被插件自动迁进自己的存储",
              rl2.get("ok") is True and "升级前的公告" in str(rl2.get("text")),
              str(rl2.get("text"))[:140])
        check("H9 迁移后旧文件留档不删（可回滚）", (home3 / "bulletin.jsonl.migrated").exists())

        src_core = (SRC / "anp_core.py").read_text(encoding="utf-8")
        src_hub = (SRC / "anp_hub.py").read_text(encoding="utf-8")
        src_mcp = "" if not (SRC / "anp_mcp.py").exists() else (SRC / "anp_mcp.py").read_text(encoding="utf-8")
        check("H10 core.py 里没有任何公告函数",
              all(f"def {n}" not in src_core for n in
                  ("post_bulletin", "read_bulletins", "mark_seen", "bulletin_path")))
        check("H11 hub.py 里没有 mail_announce / mail_board 分支",
              "mail_announce" not in src_hub and "mail_board" not in src_hub)
        if src_mcp:
            check("H12 MCP 工具里没有 mail_announce / mail_board",
                  "mail_announce" not in src_mcp and "mail_board" not in src_mcp)
            import anp_mcp as _mcp_h
            check("H13 Core 工具仍是 8 个（公告的两个交给了插件）",
                  len(_mcp_h.TOOLS) == 8, str(len(_mcp_h.TOOLS)))
        else:
            SKIPPED.append("H12/H13（MCP adapter 不在 Core 仓库）")
    finally:
        h3.kill()

    print("\n" + "=" * 64)
    print(f"ANP 插件机制自检（manifest + 注册表 + 双向握手 + 三个代理 + 公告栏）：{OK} 通过 / {FAIL} 失败")
    if SKIPPED:
        print(f"跳过 {len(SKIPPED)} 项（adapter 不在 Core 仓库，第二轮随 adapters/ 测）：")
        for s in SKIPPED:
            print("   - " + s)
    for d in (TMP, HOM, home, home2):
        try:
            shutil.rmtree(d)
        except OSError:
            pass
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
