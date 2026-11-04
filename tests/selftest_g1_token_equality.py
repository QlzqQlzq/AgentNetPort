"""G1 验收：P0-4 的**精确 token 相等**（http 与 exec 两档）。

**补的空白**：原来的断言只测「plugin token ≠ agent token」——这是**不相等**。
一旦 Core 真把插件令牌（或 hub secret）塞进投递头，两个值依然不同，测试照样绿。
而 Spec §5.1 / §12.1 却写「已验证 P0-4」。

**这次要钉死的**：投递时捎过去的那一枚，与 `core.key_for(<目标 Agent>)` **逐字符全等**，
并且**不等于**插件令牌、**不等于** hub secret。

可红性：G1-5 人为喂一个错值，走同一套比较逻辑，必须失败。
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
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "src"

if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
_SDK = REPO / "sdk"          # G1 要直接调 SDK 的头解析，得能找到 anp_adapter
if str(_SDK) not in sys.path:
    sys.path.insert(0, str(_SDK))
import anp_core as core      # noqa: E402
import anp_plugin as plugin  # noqa: E402

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


def right_key(delivered: str, target: str, plugin_tok: str, secret: str) -> bool:
    """P0-4 的完整判据：全等 + 两条否定。抽成函数，这样才能单独喂错值验可红性。"""
    return bool(delivered) and delivered == target and delivered != plugin_tok \
        and delivered != secret


class Rig:
    def __init__(self, tmp: Path, name: str):
        self.home = tmp / f"home_{name}"
        (self.home / "plugins").mkdir(parents=True, exist_ok=True)
        self.port = free_port()
        self.base = f"http://127.0.0.1:{self.port}"
        self.proc = subprocess.Popen(
            [sys.executable, str(SRC / "anp_hub.py"), "--host", "127.0.0.1",
             "--port", str(self.port), "--home", str(self.home)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            env={**os.environ, "AGENT_ANP_EVENT_SCAN_SEC": "1"})
        for _ in range(80):
            try:
                urllib.request.urlopen(self.base + "/health", timeout=1)
                break
            except Exception:  # noqa: BLE001
                time.sleep(0.25)
        self._pkey = ""

    def post(self, path: str, body: dict, key: str = "") -> tuple[int, dict]:
        req = urllib.request.Request(
            self.base + path, method="POST",
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
        except urllib.error.URLError:
            return 0, {}

    def peer_key(self) -> str:
        if not self._pkey:
            _, r = self.post("/heartbeat", {"name": "peer.one"})
            self._pkey = str(r.get("key") or "")
        return self._pkey

    def kill(self) -> None:
        try:
            for entry in (plugin.read_registry(self.home) or {}).values():
                pid = entry.get("pid")
                if pid and entry.get("core_spawned") and int(pid) != os.getpid():
                    try:
                        os.kill(int(pid), 15)
                    except Exception:  # noqa: BLE001
                        pass
        except Exception:  # noqa: BLE001
            pass
        try:
            self.proc.kill()
        except Exception:  # noqa: BLE001
            pass


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="anp-g1-"))
    try:
        rig = Rig(tmp, "g1")
        pd = rig.home / "plugins"

        # 两枚不同的身份：被代管的 agent + adapter 自己
        AGENT = "target.one"
        ADAPTER = "anp.adapter.g1"
        ka = str((rig.post("/heartbeat", {"name": AGENT})[1]).get("key") or "")
        rig.post("/heartbeat", {"name": AGENT, "wake": {"kind": "queue"}}, ka)

        # ---------- http 档：记录真正收到的 x-mailbox-key ----------
        print("=== G1-1. http 档：投递头精确等于目标 Agent 令牌 ===")
        seen: list[dict] = []

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self) -> None:  # noqa: N802
                raw = self.rfile.read(int(self.headers.get("content-length") or 0))
                try:
                    payload = json.loads(raw.decode("utf-8"))
                except (json.JSONDecodeError, UnicodeDecodeError):
                    payload = {}
                seen.append({"x-mailbox-key": self.headers.get("x-mailbox-key"),
                             "to": (payload.get("to") or ""),
                             "msg_id": (payload.get("message") or {}).get("id")})
                body = json.dumps({"ok": True, "mode": "session"}).encode("utf-8")
                self.send_response(200)
                self.send_header("content-type", "application/json; charset=utf-8")
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        hp = free_port()
        httpd = ThreadingHTTPServer(("127.0.0.1", hp), H)
        import threading
        threading.Thread(target=httpd.serve_forever, daemon=True).start()

        pkey_adapter = str((rig.post("/heartbeat", {"name": ADAPTER})[1]).get("key") or "")
        rig.post("/heartbeat", {"name": AGENT, "wake": {
            "kind": "http", "url": f"http://127.0.0.1:{hp}/anp/wake"}}, ka)

        secret = core.hub_secret(rig.home)
        target_key = core.key_for(AGENT, rig.home)
        rig.post("/messages/send",
                 {"from": "peer.one", "to": AGENT, "body": "G1 http 信",
                  "correlation_id": "g1:http", "wake": True}, rig.peer_key())
        deadline = time.time() + 15
        while time.time() < deadline and not seen:
            time.sleep(0.3)
        got_h = (seen[0]["x-mailbox-key"] if seen else "")
        say("G1-1.1 ★http：x-mailbox-key 与 key_for(目标 Agent) 逐字符全等★",
            right_key(got_h, target_key, pkey_adapter, secret),
            json.dumps({"delivered_prefix": got_h[:8] + "…",
                        "target_prefix": target_key[:8] + "…",
                        "equal": got_h == target_key}))
        say("G1-1.2 不等于 adapter 自己那枚", got_h != pkey_adapter)
        say("G1-1.3 ★不等于 hub secret★", got_h != secret)
        say("G1-1.4 信封里的 to 也确实是目标 Agent",
            bool(seen) and seen[0]["to"] == AGENT, str(seen[0]["to"] if seen else None))
        httpd.shutdown()
        httpd.server_close()

        # ---------- exec 档：记 AGENT_ANP_SELF_KEY ----------
        print("\n=== G1-2. exec 档：环境变量 AGENT_ANP_SELF_KEY 同样精确 ===")
        dump = tmp / "exec_key.json"
        ex = pd / "g1_dump.py"
        ex.write_text(
            "import json, os, sys\n"
            "out = sys.argv[1]\n"
            "rec = {'self': os.environ.get('AGENT_ANP_SELF'),\n"
            "       'self_key': os.environ.get('AGENT_ANP_SELF_KEY'),\n"
            "       'key_alias': os.environ.get('AGENT_ANP_KEY'),\n"
            "       'msg_id': (json.loads(os.environ.get('AGENT_ANP_WAKE_JSON') or '{}')\n"
            "                  .get('id'))}\n"
            "open(out, 'w', encoding='utf-8').write(json.dumps(rec))\n",
            encoding="utf-8")
        rig.post("/heartbeat", {"name": "exec.one",
                                "wake": {"kind": "exec",
                                         "command": [sys.executable, str(ex),
                                                     str(dump)]}})
        rig.post("/messages/send",
                 {"from": "peer.one", "to": "exec.one", "body": "G1 exec 信",
                  "correlation_id": "g1:exec", "wake": True}, rig.peer_key())
        for _ in range(40):
            if dump.is_file():
                break
            time.sleep(0.5)
        if dump.is_file():
            rec = json.loads(dump.read_text(encoding="utf-8"))
            gk = str(rec.get("self_key") or "")
            ek_target = core.key_for("exec.one", rig.home)
            say("G1-2.1 ★exec：AGENT_ANP_SELF_KEY 与 key_for(exec.one) 全等★",
                right_key(gk, ek_target, pkey_adapter, secret),
                json.dumps({"equal": gk == ek_target,
                            "prefix": gk[:8] + "…" if gk else "(空)"}))
            say("G1-2.2 既有别名 AGENT_ANP_KEY 同值（不搞双轨）",
                rec.get("key_alias") == gk)
            say("G1-2.3 信封 id 确实捎到了子进程", bool(rec.get("msg_id")))
        else:
            say("G1-2.1 exec 档 dump 没生成（子进程没跑？）", False)
            say("G1-2.2 AGENT_ANP_KEY 同值", False)
            say("G1-2.3 信封 id 到达", False)

        # ---------- 负例：可红 ----------
        print("\n=== G1-3. 负例可红（Core 若投错令牌，测试必须失败）===")
        say("G1-3.1 喂入插件令牌 → 判据返回 False",
            right_key(pkey_adapter, target_key, pkey_adapter, secret) is False,
            "（这正是原 Q2.3 测不出来的那种错）")
        say("G1-3.2 喂入 hub secret → 判据返回 False",
            right_key(secret, target_key, pkey_adapter, secret) is False)
        say("G1-3.3 空头 → 判据返回 False",
            right_key("", target_key, pkey_adapter, secret) is False)
        say("G1-3.4 差一个字符 → 判据返回 False（全等，不是前缀）",
            right_key(target_key[:-1] + ("x" if target_key[-1] != "x" else "y"),
                      target_key, pkey_adapter, secret) is False)

        print("\n=== G1-4. Spec 的验证指针与测试名对应 ===")
        spec = (REPO / "docs" / "ANP-ADAPTER-SPEC.md").read_text(encoding="utf-8")
        say("G1-4.1 Spec §5.1 仍写着 x-mailbox-key 是目标 Agent 令牌",
            "x-mailbox-key` 的值，永远是目标 Agent 的 token" in spec)

        # ---------- HTTP 头大小写不敏感（G1 实测发现的 SDK 回归）----------
        print("\n=== G1-5. HTTP 头大小写不敏感（RFC 7230）===")
        from anp_adapter.envelope import from_wake_payload  # noqa: E402
        payload = {"message": {"id": "m9", "from": "p", "to": AGENT,
                               "body": "x", "kind": "message"}, "to": AGENT}
        for spelled in ("x-mailbox-key", "X-Mailbox-Key", "X-MAILBOX-KEY",
                        "X-mailbox-Key"):
            _, side = from_wake_payload(payload, {spelled: "TOK"})
            say(f"G1-5.{spelled!r} 也能取到令牌", side["agent_key"] == "TOK",
                side["agent_key"])
        _, side_none = from_wake_payload(payload, {})
        say("G1-5.5 没有这个头时仍是空串（不造假）", side_none["agent_key"] == "")

        print(f"\n===== G1 验收：{OK} 通过 / {FAIL} 失败 =====")
        if FAIL:
            print("★有 FAIL★：P0-4 必须锁'逐字符全等'，不相等不等于正确。")
        rig.kill()
        return 1 if FAIL else 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
