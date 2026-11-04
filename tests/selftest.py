"""agent-mailbox 自检：不依赖 DSH / Codex 启动，验证核心链路。

覆盖：
1. 存储读写 + 未读标记（core）
2. 唤醒投递：hub 收到 send 后 POST 到登记的 /mail/wake（本地假端点接收）
3. MCP stdio 服务器：initialize / tools/list / tools/call 全过一遍
4. send → wake → 读信 → 回信 的往返闭环
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
# 兼容两种布局：仓库里（tests/ 的兄弟 src/）与安装后平铺（同级）。
for _candidate in (HERE, HERE.parent / "src", HERE.parent):
    if (_candidate / "anp_preflight.py").is_file() and str(_candidate) not in sys.path:
        sys.path.insert(0, str(_candidate))
import anp_preflight  # noqa: E402

SRC = anp_preflight.src_dir()
REPO = anp_preflight.repo_root()

# 断言里要对着真实工具清单校验（新增工具时别写字面量）
# ★第一轮 Core Extraction★：MCP server 是 adapter，不在 Core 仓库里。
# 依赖它的项改为「在场才测、不在场就明确 SKIP」——绝不假失败，也绝不假装测过。
try:
    import anp_mcp  # noqa: E402
    HAS_MCP = True
except Exception:  # noqa: BLE001
    anp_mcp = None  # type: ignore[assignment]
    HAS_MCP = False
SKIPPED: list[str] = []

# Windows 下 python 子进程默认按本地代码页输出，会让 JSON-RPC 帧解码失败 —— 强制 UTF-8。
if os.environ.get("PYTHONUTF8") != "1":
    env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
    raise SystemExit(subprocess.call([sys.executable, str(Path(__file__).resolve())], env=env))

PY = sys.executable
TMP = Path(tempfile.mkdtemp(prefix="anp-test-"))
HOME = TMP / "home"
HUB_PORT = 0
HUB = f"http://127.0.0.1:{HUB_PORT}"
LOG_PATH = Path(os.environ.get("MAILBOX_TEST_LOG") or (TMP / "selftest-result.txt"))

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    mark = "PASS" if ok else "FAIL"
    with open(LOG_PATH, "a", encoding="utf-8") as fp:
        fp.write(f"[{mark}] {name}{('  <- ' + detail) if detail else ''}\n")
    print(f"[{mark}] {name}{('  <- ' + detail) if detail else ''}", flush=True)


def free_port(preferred: int) -> int:
    with socket.socket() as s:
        if preferred > 0:
            try:
                s.bind(("127.0.0.1", preferred))
                return preferred
            except OSError:
                pass
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# --------------------------------------------------------------------------- #
# 假唤醒端点
# --------------------------------------------------------------------------- #
class WakeHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    received: list[dict] = []

    def log_message(self, *a):  # noqa: D102
        pass

    def do_POST(self):  # noqa: N802
        n = int(self.headers.get("content-length") or 0)
        body = json.loads(self.rfile.read(n).decode("utf-8") or "{}")
        WakeHandler.received.append(body)
        payload = json.dumps({"ok": True, "mode": "fake-inject"}).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


# --------------------------------------------------------------------------- #
# MCP stdio 客户端（测试用）
# --------------------------------------------------------------------------- #
class Mcp:
    """MCP adapter 的 stdio 驱动。第一轮 adapter 不在场时不可用。"""

    def __init__(self, env: dict[str, str]):
        self.available = HAS_MCP
        self.proc = subprocess.Popen(
            [PY, str(SRC / "anp_mcp.py")],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            env={**os.environ, **env},
        )
        self.id = 0

    def call(self, method: str, params: dict | None = None) -> dict:
        self.id += 1
        frame = {"jsonrpc": "2.0", "id": self.id, "method": method}
        if params is not None:
            frame["params"] = params
        # ensure_ascii=True：脏字符会以 \udc97 转义形式上线（合法 JSON），
        # 用来验证服务器端把工具参数当入口清洗确实兜得住。
        self.proc.stdin.write(json.dumps(frame, ensure_ascii=True) + "\n")
        self.proc.stdin.flush()
        line = self.proc.stdout.readline()
        if not line:
            raise RuntimeError("MCP 进程无响应")
        return json.loads(line)

    def init(self) -> dict:
        r = self.call("initialize", {"protocolVersion": "2024-11-05", "capabilities": {}})
        self.proc.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n")
        self.proc.stdin.flush()
        return r

    def tool(self, name: str, args: dict) -> tuple[str, bool]:
        r = self.call("tools/call", {"name": name, "arguments": args})
        res = r.get("result") or {}
        text = "\n".join(c.get("text", "") for c in res.get("content") or [])
        return text, bool(res.get("isError"))

    def close(self) -> None:
        try:
            self.proc.stdin.close()
            self.proc.terminate()
        except Exception:  # noqa: BLE001
            pass


def wait_port(port: int, timeout: float = 10.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        with socket.socket() as s:
            s.settimeout(0.3)
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return True
        time.sleep(0.1)
    return False


def wait_wake_body(expected: str, timeout: float = 3.0) -> str:
    """Wait for the threaded fake wake endpoint to finish handling the request."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        for item in reversed(WakeHandler.received):
            body = ((item.get("message") or {}).get("body")) or ""
            if body == expected:
                return body
        time.sleep(0.02)
    return ""


def main() -> int:
    global HUB_PORT
    HUB_PORT = free_port(0)
    hub_url = f"http://127.0.0.1:{HUB_PORT}"
    HOME.mkdir(parents=True, exist_ok=True)

    # ---- 1. core 存储 ----
    import anp_core as core

    rec = core.send("tester", "peer", "你好喵", subject="hello", home=HOME)
    rows = core.read_records("peer", HOME)
    check("core: 落盘 + 读回", len(rows) == 1 and rows[0]["body"] == "你好喵", f"id={rec['id']}")
    check("core: 未读统计", sum(1 for r in rows if not r.get("read")) == 1)
    core.mark_read("peer", {rec["id"]}, HOME)
    check("core: 标记已读", core.read_records("peer", HOME)[0]["read"] is True)

    wake_srv = ThreadingHTTPServer(("127.0.0.1", 0), WakeHandler)
    wake_port = wake_srv.server_address[1]
    threading.Thread(target=wake_srv.serve_forever, daemon=True).start()

    # ---- 2. hub 起来 ----
    hub_err = open(TMP / "hub.stderr.log", "w", encoding="utf-8")
    hub = subprocess.Popen(
        [PY, str(SRC / "anp_hub.py"), "--port", str(HUB_PORT), "--home", str(HOME)],
        stdout=hub_err, stderr=subprocess.STDOUT, text=True, encoding="utf-8",
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    up = wait_port(HUB_PORT)
    check("hub: 启动监听", up, hub_url)

    def hub_post(path: str, payload: dict, timeout: float = 5.0) -> dict:
        import urllib.request

        req = urllib.request.Request(
            hub_url + path,
            data=json.dumps(payload, ensure_ascii=False).encode(),
            headers={"content-type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))

    def hub_post_with_key(path: str, payload: dict, key: str, timeout: float = 5.0) -> dict:
        import urllib.request

        req = urllib.request.Request(
            hub_url + path,
            data=json.dumps(payload, ensure_ascii=False).encode(),
            headers={"content-type": "application/json", "x-mailbox-key": key},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))

    if up:
        # 首次心跳 = 新身份引导：不需要密钥，且**响应里直接发密钥**（协议 §3.1）。
        hb = hub_post("/heartbeat", {
            "name": "peer",
            "wake": {"kind": "http", "url": f"http://127.0.0.1:{wake_port}/mail/wake"},
            "description": "测试用假端点",
            "home": str(HOME),
        })
        check("hub: 首次心跳引导（响应发密钥）", hb.get("ok") is True and bool(hb.get("key")),
              json.dumps({k: v for k, v in hb.items() if k != "key"}, ensure_ascii=False)[:160])

        # ★已登记的身份再心跳必须带对密钥★（这条以前没有鉴权，任何本机进程都能顶掉唤醒端点）
        try:
            hub_post("/heartbeat", {"name": "peer", "wake": {"kind": "exec", "command": ["cmd"]}})
            check("hub: 顶替已登记身份的心跳被拒", False, "居然成功了")
        except urllib.error.HTTPError as e:
            check("hub: 顶替已登记身份的心跳被拒", e.code == 401, f"HTTP {e.code}")
        still = core.agent_table(HOME).get("peer", {}).get("wake", {})
        check("hub: 唤醒端点未被顶掉", still.get("kind") == "http", json.dumps(still, ensure_ascii=False)[:120])

        # 带对密钥心跳应当成功（续约路径）
        hb2 = hub_post_with_key("/heartbeat", {"name": "peer", "wake": still}, hb.get("key") or "")
        check("hub: 带密钥心跳续约成功", hb2.get("ok") is True, json.dumps(hb2.get("auth"), ensure_ascii=False))

        ledger_rows = core.read_ledger(HOME)
        table_names = sorted(core.agent_table(HOME).keys())
        check("hub: 账本已落盘", "peer" in table_names,
              f"ledger={len(ledger_rows)} names={table_names}")

        # 发信人也要先"报到"拿到密钥：数据面现在一律要令牌（协议 §3.1）
        tst = hub_post("/heartbeat", {"name": "tester", "description": "自检发信人"})
        tester_key = tst.get("key") or ""
        check("hub: 发信人引导拿到密钥", bool(tester_key), "有" if tester_key else "无")
        # The MCP client is a separate identity too; bootstrap it before
        # exercising the authenticated data plane.
        codex_hb = hub_post("/heartbeat", {"name": "codex", "description": "MCP 自检客户端"})
        check("hub: MCP 客户端引导拿到密钥", bool(codex_hb.get("key")),
              "有" if codex_hb.get("key") else "无")
        sent = hub_post_with_key("/send",
                                 {"from": "tester", "to": "peer", "body": "被叫醒了吗",
                                  "subject": "wake-test", "home": str(HOME)}, tester_key)
        woke = sent.get("wake") or {}
        check("hub: send 后触发唤醒", woke.get("ok") is True, json.dumps(woke, ensure_ascii=False))
        body = wait_wake_body("被叫醒了吗")
        check("hub: 唤醒载荷带正文", body == "被叫醒了吗", body)

    # ---- 3. MCP stdio（hub 模式）：第一轮 adapter 不在场，整段条件化 ----
    if HAS_MCP:
        codex = Mcp({"AGENT_ANP_SELF": "codex", "AGENT_ANP_HUB": hub_url,
                     "AGENT_ANP_HOME": str(HOME)})
        init = codex.init()
        check("mcp: initialize",
              (init.get("result") or {}).get("serverInfo", {}).get("name") == "AgentNetPort",
              json.dumps(init.get("result", {}).get("serverInfo", {})))
        names = sorted(x["name"] for x in
                       codex.call("tools/list").get("result", {}).get("tools", []))
        check("mcp: tools/list", names == sorted(x["name"] for x in anp_mcp.TOOLS),
              f"{len(names)} 个: " + ",".join(names))
    else:
        SKIPPED.append("mcp: initialize / tools/list（MCP adapter 不在 Core 仓库，第二轮随 adapters/ 测）")

    # ---- 4. 往返闭环：peer 读信 → 回信 → codex 收信 ----
    # ★这一节原本走 MCP 工具，但它验的是 Core 的收发语义，与 adapter 无关★
    # （Core Extraction 后覆盖不缩水）→ 改成直连 hub 的 HTTP 原语来验。
    # 注意：信在投递成功时会被信箱自动签收（协议 §4.2.2），所以必须用
    # unread_only=False 复查——用 unread_only=True 会读到「未读余 0」而误判成「没收到信」。
    def _call(server: str, key: str, tool: str, args: dict) -> dict:
        req = urllib.request.urlopen(urllib.request.Request(
            hub_url + "/tool", method="POST",
            data=json.dumps({"server": server, "name": tool, "args": args}).encode(),
            headers={"content-type": "application/json", "x-mailbox-key": key}), timeout=25)
        with req as r:
            return json.loads(r.read().decode())

    peer_key = str(hb.get("key") or "")        # hb = 第 1 节首次引导时发下来的那把
    codex_key = str(codex_hb.get("key") or "")
    # 先由 codex 发一封给 peer。★走 Hub 现在的真实路由 /send★：规范 §4 写的
    # /messages/send、/messages/receive 目前**还没实现**（spec 与实现的偏差，第一轮只记不改）。
    hub_post_with_key("/send", {"from": "codex", "to": "peer", "body": "来自 Core 的信",
                                "subject": "roundtrip"}, codex_key)
    got = _call("peer", peer_key, "mail_inbox", {"unread_only": False})
    msgs = got.get("messages") or []
    check("往返: peer 收到 codex 的信（已被自动签收，复查历史可见）",
          any("来自 Core 的信" in str(m.get("body") or "") for m in msgs),
          json.dumps([m.get("body") for m in msgs], ensure_ascii=False)[:120])
    reply = _call("peer", peer_key, "mail_send",
                  {"to": "codex", "body": "收到喵，这就干活",
                   "reply_to": (msgs[0].get("id") if msgs else None)})
    check("往返: peer 回信", reply.get("ok") is True,
          json.dumps(reply, ensure_ascii=False)[:120])
    back = _call("codex", codex_key, "mail_inbox", {"unread_only": False})
    check("往返: codex 收到回信",
          any("收到喵" in str(m.get("body") or "") for m in (back.get("messages") or [])),
          json.dumps([m.get("body") for m in (back.get("messages") or [])],
                    ensure_ascii=False)[:120])

    # ---- 4b. 群发：一封多人信拆成 N 份副本，共享同一个 message id ----
    # ★这条不变量在 hub 层（hub.send_group），core.send 只接单个收件人★
    grp = hub_post_with_key("/send", {"from": "codex", "to": ["peer", "tester"],
                                "body": "群发的信", "subject": "group"}, codex_key)
    gid = ((grp.get("message") or {}).get("id")) or ""
    check("群发: 一封多人信发出", bool(gid) and grp.get("ok") is True,
          json.dumps(grp, ensure_ascii=False)[:120])
    def _count(who: str, key: str) -> list:
        r = _call(who, key, "mail_inbox", {"unread_only": False, "limit": 50})
        return [m for m in (r.get("messages") or []) if m.get("id") == gid]
    a1, a2 = _count("peer", peer_key), _count("tester", tester_key)
    check("群发: 每个收件人各得一份副本", len(a1) == 1 and len(a2) == 1,
          f"peer={len(a1)} tester={len(a2)}")
    check("群发: ★N 份副本共享同一个 message id★",
          bool(a1) and bool(a2) and a1[0]["id"] == a2[0]["id"], gid)
    check("群发: 每份副本的 to 是各自的名字（不是拼出假身份）",
          bool(a1) and bool(a2) and a1[0].get("to") == "peer" and a2[0].get("to") == "tester",
          f"{[a1[0].get('to') if a1 else None, a2[0].get('to') if a2 else None]}")

    # ---- 5. 降级 direct 模式 ----
    os.environ["AGENT_ANP_HUB"] = "http://127.0.0.1:1"
    if HAS_MCP:
        lone = Mcp({"AGENT_ANP_SELF": "lonely", "AGENT_ANP_HUB": "http://127.0.0.1:1",
                    "AGENT_ANP_HOME": str(HOME), "AGENT_ANP_NO_AUTOSTART": "1"})
        lone.init()
        solo, solo_err = lone.tool("mail_send", {"to": "peer", "body": "hub 挂了也要能发"})
        check("降级: MCP direct 模式可用", (not solo_err) and "已投递" in solo,
              solo.replace("\n", " | ")[:160])
    else:
        SKIPPED.append("降级: MCP direct 模式（DirectBackend 在 adapter 侧，第二轮测）")
    # 同一件事的 Core 侧版本：hub 不可达时 core.send() 仍然能落盘
    rec = core.send("lonely", "peer", "hub 挂了也要能发", home=HOME)
    rows = [r for r in core.read_records("peer", HOME) if r.get("id") == rec.get("id")]
    check("降级: core.send() 直写可用（hub 不在场也不丢信）",
          bool(rows) and rows[0].get("body") == "hub 挂了也要能发", f"落盘 {len(rows)} 份")
    check("降级: 直写的信同样带信封签名",
          bool(rec.get("sig")) and core.verify_envelope(rec, HOME)[0])

    # ---- 6. 回归：孤立代理字符（模型可能吐出来的脏字符）必须被清洗而不是炸掉 ----
    dirty = "日志\u2713 正常，坏字符在这里 -> \udc97 <- 后面还有正文"
    cleaned = core.clean_text(dirty)
    check("清洗: clean_text 能处理孤立代理", "\udc97" not in cleaned and "后面还有正文" in cleaned,
          repr(cleaned[:60]))
    try:
        cleaned.encode("utf-8")
        ok_encode = True
    except UnicodeEncodeError as e:
        ok_encode = False
        detail = str(e)
    check("清洗: 结果可 UTF-8 编码", ok_encode, "" if ok_encode else detail)

    sent = _call("codex", codex_key, "mail_send",
                 {"to": "peer", "body": dirty, "subject": "脏字符回归"})
    check("回归: 带孤立代理的正文也能发出", sent.get("ok") is True,
          json.dumps(sent, ensure_ascii=False)[:160])
    # order=newest：箱里信多了以后，默认取"最早的 N 封"会漏掉刚发的那封
    back = _call("peer", peer_key, "mail_inbox",
                 {"unread_only": False, "limit": 5, "order": "newest"})
    back_text = json.dumps(back.get("messages") or [], ensure_ascii=False)
    check("回归: 对端收到的正文已清洗且完整",
          "后面还有正文" in back_text and "\udc97" not in back_text, back_text[:160])

    # ---- 清理 ----
    for c in [x for x in (globals().get("codex"), globals().get("peer")) if x is not None]:
        c.close()
    if up:
        hub.terminate()
    wake_srv.shutdown()

    passed = sum(1 for _, ok, _ in results if ok)
    print(f"\n===== {passed}/{len(results)} 通过 =====")
    failed = [n for n, ok, _ in results if not ok]
    if failed:
        print("失败项: " + ", ".join(failed))
    if SKIPPED:
        print(f"跳过 {len(SKIPPED)} 项（adapter 不在 Core 仓库，第二轮随 adapters/ 测）：")
        for s in SKIPPED:
            print("   - " + s)
    if os.environ.get("ANP_TEST_KEEP") != "1":
        shutil.rmtree(TMP, ignore_errors=True)
    else:
        print(f"保留现场: {TMP}")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
