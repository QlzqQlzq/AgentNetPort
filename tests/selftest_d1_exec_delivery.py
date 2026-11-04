"""D1 修复（甲）验收：exec 档 `delivered_at` 只在唤醒器确认时写。

**修的裂缝**（证据见 `docs/D1-EXPERIMENT-RECORD.md`）：exec 的 `Popen` 成功即被当成
"已投递"，`delivered_at` 当场写上。子进程秒崩、或跑完但不 ack 时，信永久卡在
「已投递但没人读过」，`auto_redeliver` 永不重投，`attempts` 停在 1。

**甲案的三个事实**：

    attempted = Core 尝试叫醒了          （attempts / last_attempt_at）
    spawned   = 进程成功启动了           （spawned_at / wake_pid）
    delivered = 唤醒器确认收到/处理了    （delivered_at，只在 ack / 收信时写）

三条硬验收（用户 2026-09-23 定）：**崩溃 / 不 ack / 正常 ack**。

另外钉住两件事：
  · 重投是**有限**的（实测 5 次后明说"已放弃"，不是无限重启）；
  · http / queue / `wake=false` / `no_push` 四条老路径行为**不变**——
    `cache:silent-delivery` 是独立机制，不许借 D1 顺手改掉。
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

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "src"

if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
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


class Rig:
    """一个真 hub + 一个真实 home；默认把退避/扫描压到最短，好让测试跑得快。"""

    def __init__(self, tmp: Path, name: str, **env):
        self.home = tmp / f"home_{name}"
        (self.home / "plugins").mkdir(parents=True, exist_ok=True)
        self.port = free_port()
        self.base = f"http://127.0.0.1:{self.port}"
        self.proc = subprocess.Popen(
            [sys.executable, str(SRC / "anp_hub.py"), "--host", "127.0.0.1",
             "--port", str(self.port), "--home", str(self.home)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            env={**os.environ, "AGENT_ANP_EVENT_SCAN_SEC": "1",
                 "AGENT_ANP_RETRY_SEC": "2", "AGENT_ANP_REDELIVER_SEC": "5",
                 "AGENT_ANP_REDELIVER_MAX": "3", "AGENT_ANP_REDELIVER_LIMIT": "3",
                 **env})
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

    def send(self, to: str, body: str, wake: bool = True, tag: str = "") -> dict:
        _, r = self.post("/messages/send",
                        {"from": "peer.one", "to": to, "body": body,
                         "correlation_id": f"d1:{tag or body}", "wake": wake},
                        self.peer_key())
        return r

    def state(self, who: str, body: str) -> dict:
        for m in core.read_records(who, self.home):
            if str(m.get("body")) == body:
                return m
        return {}

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


def brief(s: dict) -> str:
    return json.dumps({"attempts": s.get("attempts"),
                       "delivered_at": bool(s.get("delivered_at")),
                       "spawned_at": bool(s.get("spawned_at")),
                       "wake_pid": bool(s.get("wake_pid")),
                       "next_retry_at": bool(s.get("next_retry_at")),
                       "read": s.get("read"),
                       "read_by": s.get("read_by")}, ensure_ascii=False)


def wait_until(rig: Rig, who: str, body: str, pred, timeout: float = 90.0) -> dict:
    """轮询到 pred(record) 成立，或超时返回最后一次快照。"""
    deadline = time.time() + timeout
    s: dict = {}
    while time.time() < deadline:
        s = rig.state(who, body)
        if s and pred(s):
            return s
        time.sleep(0.5)
    return s


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="anp-d1-"))
    try:
        rig = Rig(tmp, "d1")
        pd = rig.home / "plugins"

        # ---------- ① exec 子进程秒崩 ----------
        print("=== D1-1. exec 子进程立刻 exit(1)（适配器崩在启动瞬间）===")
        crash = pd / "d1_crash.py"
        crash.write_text("import sys\nsys.exit(1)\n", encoding="utf-8")
        rig.post("/heartbeat", {"name": "crash.guy",
                                "wake": {"kind": "exec",
                                         "command": [sys.executable, str(crash)]}})
        r = rig.send("crash.guy", "D1 崩溃信", tag="crash")
        say("D1-1.1 发信侧 wake 仍报 Popen 成功",
            bool((r.get("wake") or {}).get("ok")),
            json.dumps(r.get("wake"), ensure_ascii=False)[:90])
        s = rig.state("crash.guy", "D1 崩溃信")
        say("D1-1.2 ★spawned_at 记了、但 delivered_at 为空★",
            bool(s.get("spawned_at")) and not s.get("delivered_at"), brief(s))
        say("D1-1.3 wake_pid 也记了（可追是哪个进程）",
            bool(s.get("wake_pid")), f"wake_pid={bool(s.get('wake_pid'))}")
        say("D1-1.4 排了重投（给 ack 留窗口）", bool(s.get("next_retry_at")))

        print("\n=== D1-2. 它会被自动重投，且**有限**（这是甲案的前提）===")
        s3 = wait_until(rig, "crash.guy", "D1 崩溃信",
                        lambda x: int(x.get("attempts") or 0) >= 3)
        say("D1-2.1 attempts 涨上去了（不再卡在 1）",
            int(s3.get("attempts") or 0) >= 3, f"attempts={s3.get('attempts')}")
        abandoned = wait_until(rig, "crash.guy", "D1 崩溃信",
                               lambda x: str(x.get("last_error") or "")
                               .startswith("已放弃自动重投"), timeout=60)
        say("D1-2.2 ★到上限后明说'已放弃'并停手（不是无限重启）★",
            str(abandoned.get("last_error") or "").startswith("已放弃自动重投"),
            str(abandoned.get("last_error"))[:70])
        say("D1-2.3 停手后 next_retry_at 清空", not abandoned.get("next_retry_at"))
        say("D1-2.4 仍然未读（不会被悄悄划掉）", not abandoned.get("read"))

        # ---------- ② exec 跑完但不 ack ----------
        print("\n=== D1-3. exec 跑完但**不 ack**（懒得收尾的适配器）===")
        noack = pd / "d1_noack.py"
        noack.write_text("import json,os,sys\n"
                         "m = json.loads(os.environ.get('AGENT_ANP_WAKE_JSON') or '{}')\n"
                         "print('处理了但不 ack:', m.get('id'))\n", encoding="utf-8")
        rig.post("/heartbeat", {"name": "noack.guy",
                                "wake": {"kind": "exec",
                                         "command": [sys.executable, str(noack)]}})
        rig.send("noack.guy", "D1 不ack信", tag="noack")
        s = rig.state("noack.guy", "D1 不ack信")
        say("D1-3.1 同样不写 delivered_at", not s.get("delivered_at"), brief(s))
        s2 = wait_until(rig, "noack.guy", "D1 不ack信",
                        lambda x: int(x.get("attempts") or 0) >= 2)
        say("D1-3.2 会被重投（不 ack 不等于完成）",
            int(s2.get("attempts") or 0) >= 2, f"attempts={s2.get('attempts')}")

        # ---------- ③ exec 正常处理并 ack ----------
        print("\n=== D1-4. exec 正常处理并 mail_ack（对照组）===")
        ack = pd / "d1_ack.py"
        ack.write_text(
            "import json, os, sys, urllib.request\n"
            "BASE = " + repr(rig.base) + "\n"
            "raw = os.environ.get('AGENT_ANP_WAKE_JSON') or '{}'\n"
            "m = json.loads(raw)\n"
            "who = os.environ.get('AGENT_ANP_SELF') or ''\n"
            "key = os.environ.get('AGENT_ANP_SELF_KEY') or ''\n"
            "body = json.dumps({'server': who, 'name': 'mail_ack',\n"
            "                    'args': {'ids': [m.get('id')]}}, ensure_ascii=False)\n"
            "req = urllib.request.Request(BASE + '/tool', method='POST',\n"
            "                            data=body.encode('utf-8'),\n"
            "                            headers={\n"
            "                                'content-type':\n"
            "                                'application/json; charset=utf-8',\n"
            "                                'x-mailbox-key': key})\n"
            "urllib.request.urlopen(req, timeout=10).read()\n", encoding="utf-8")
        rig.post("/heartbeat", {"name": "ack.guy",
                                "wake": {"kind": "exec",
                                         "command": [sys.executable, str(ack)]}})
        rig.send("ack.guy", "D1 正常ack信", tag="ackok")
        s_ack = wait_until(rig, "ack.guy", "D1 正常ack信",
                           lambda x: bool(x.get("read")), timeout=30)
        say("D1-4.1 ★ack 之后 read=true 且 delivered_at 补写上了★",
            bool(s_ack.get("read")) and bool(s_ack.get("delivered_at")), brief(s_ack))
        say("D1-4.2 read_by 记的是 ack 的人", s_ack.get("read_by") == "ack.guy",
            f"read_by={s_ack.get('read_by')}")
        # ack 之后不该再被拉起：等过两个扫描周期，attempts 不该涨
        time.sleep(13)
        s_ack2 = rig.state("ack.guy", "D1 正常ack信")
        say("D1-4.3 ack 后不再重投（attempts 不涨）",
            int(s_ack2.get("attempts") or 0) == int(s_ack.get("attempts") or 0)
            and not s_ack2.get("next_retry_at"),
            f"attempts={s_ack2.get('attempts')}")

        # ---------- 老路径行为不变 ----------
        print("\n=== D1-5. http / queue / wake=false 三条老路径行为不变 ===")
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        import threading
        answered: list[dict] = []

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self) -> None:  # noqa: N802
                raw = self.rfile.read(int(self.headers.get("content-length") or 0))
                payload = json.loads(raw.decode("utf-8"))
                answered.append(payload)
                body = json.dumps({"ok": True, "mode": "session"}).encode("utf-8")
                self.send_response(200)
                self.send_header("content-type", "application/json; charset=utf-8")
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        hp = free_port()
        httpd = ThreadingHTTPServer(("127.0.0.1", hp), H)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        rig.post("/heartbeat", {"name": "http.guy",
                                "wake": {"kind": "http",
                                         "url": f"http://127.0.0.1:{hp}/anp/wake"}})
        rig.send("http.guy", "D1 http信", tag="http")
        s_http = wait_until(rig, "http.guy", "D1 http信", lambda x: bool(x.get("read")), 20)
        say("D1-5.1 http 档照旧：带 mode 即签收，delivered_at 有值",
            bool(s_http.get("delivered_at")) and s_http.get("read_by") == "wake:http-acked",
            brief(s_http))
        httpd.shutdown()
        httpd.server_close()

        rig.post("/heartbeat", {"name": "queue.guy", "wake": {"kind": "queue"}})
        # ★wake 必须给 True★：wake=false 走的是 skipped 路径（发送方主动不唤醒），
        # 只有 wake=true 才会走到 P0-0 的 no_push 判定。
        rig.send("queue.guy", "D1 queue信", wake=True, tag="queue")
        s_q = wait_until(rig, "queue.guy", "D1 queue信",
                        lambda x: x.get("wake_skipped") == "no_push_target", 20)
        # ★别写 `attempts or -1`★：0 是合法值（queue 档终态就是 0），
        # `0 or -1` 会得到 -1，把正确的状态判成错的。
        a_q = s_q.get("attempts")
        say("D1-5.2 ★P0-0 queue 终态未动★：wake_skipped=no_push_target、attempts=0",
            s_q.get("wake_skipped") == "no_push_target" and a_q == 0,
            json.dumps({"wake_skipped": s_q.get("wake_skipped"),
                        "attempts": a_q}, ensure_ascii=False))

        # wake=false（缓存信）：read_by 必须是 cache:silent-delivery，
        # 且 delivered_at 仍为空——这一条专门防"顺手把 cache 路径也改了"
        rig.post("/heartbeat", {"name": "cached.guy",
                                "wake": {"kind": "exec",
                                         "command": [sys.executable, str(noack)]}})
        rig.send("cached.guy", "D1 缓存信", wake=False, tag="cache")
        s_c = wait_until(rig, "cached.guy", "D1 缓存信", lambda x: bool(x.get("read")), 20)
        say("D1-5.3 ★缓存信仍是 cache:silent-delivery 语义★",
            s_c.get("read_by") == "cache:silent-delivery" and s_c.get("wake_skipped") is True,
            json.dumps({"read": s_c.get("read"), "read_by": s_c.get("read_by"),
                        "wake_skipped": s_c.get("wake_skipped"),
                        "attempts": s_c.get("attempts")}, ensure_ascii=False))
        say("D1-5.4 ★缓存信的 delivered_at 仍为空（没被 D1 顺手改掉）★",
            not s_c.get("delivered_at"), brief(s_c))

        print(f"\n===== D1（甲）验收：{OK} 通过 / {FAIL} 失败 =====")
        if FAIL:
            print("★有 FAIL★：三个事实（attempted / spawned / delivered）不能混。")
        rig.kill()
        return 1 if FAIL else 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
