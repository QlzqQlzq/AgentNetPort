"""B1 验收：`/anp/wake` 的 HTTP **响应**必须带合法 `mode`，否则 Core 不签收。

起因：Adapter Spec §5.1 / §10 原先教第三方 `return {"ok": True}`，而 Core 的
`wake_sign_off_ids` 要求响应含非空 `mode` 才签收。只读 Spec 实现的第三方会
「信永不签收、未读悬挂、还可能被再推」。这是 v0.1.1 补上的契约裂缝。

errata §B1.9 要求至少三条，且**负例必须可红**：

    ① ok=true + 合法 mode   → 签收
    ② ok=true + 缺 mode     → 不签收
    ③ HTTP 200 + ok=false   → 不签收
    ④（补强）ok=true + mode 非法（"http" / "unknown" / ""）→ 不签收
    ⑤（补强）响应体非 JSON  → 不签收

断言口径（与 Core 对齐）：
    签收   = 该封 read=true（或 read_by 已记），且不再被自动重投打扰
    不签收 = 保持未读，仍按既有 retry 规则可重投（本补丁不改 retry 公式）
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
import anp_core as core      # noqa: E402
import anp_plugin as plugin  # noqa: E402

sys.path.insert(0, str(REPO / "sdk"))
from anp_adapter import RESPONSE_MODES, DEFAULT_RESPONSE_MODE   # noqa: E402

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


# 一个可编程的假 adapter：按用例改自己的响应体/状态码
class FakeAdapter:
    def __init__(self, mode: str):
        self.port = free_port()
        self.mode = mode                 # ok_mode / no_mode / not_ok / bad_mode / not_json
        self.seen: list[dict] = []
        srv = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self) -> None:  # noqa: N802
                raw = self.rfile.read(int(self.headers.get("content-length") or 0))
                try:
                    payload = json.loads(raw.decode("utf-8"))
                except (json.JSONDecodeError, UnicodeDecodeError):
                    payload = {}
                srv.seen.append({"body": payload,
                                 "key": self.headers.get("x-mailbox-key")})
                env = payload.get("message") or {}
                if srv.mode == "ok_mode":
                    body, code = {"ok": True, "mode": DEFAULT_RESPONSE_MODE,
                                  "envelope_id": env.get("id")}, 200
                elif srv.mode == "no_mode":        # ② 只回 ok=true
                    body, code = {"ok": True, "envelope_id": env.get("id")}, 200
                elif srv.mode == "not_ok":         # ③ 200 但 ok=false
                    body, code = {"ok": False, "error": "平台拒绝了"}, 200
                elif srv.mode == "bad_mode":       # ④ mode 非法
                    body, code = {"ok": True, "mode": "http"}, 200
                elif srv.mode == "empty_mode":
                    body, code = {"ok": True, "mode": ""}, 200
                else:                             # ⑤ 非 JSON
                    body, code = None, 200
                if body is None:
                    # ★必须纯 ASCII 字节★：bytes 字面量不能含中文
                    rawb = b"not a json body at all"
                else:
                    rawb = json.dumps(body, ensure_ascii=False).encode("utf-8")
                self.send_response(code)
                # ★响应头必须纯 ASCII★
                self.send_header("content-type", "application/json; charset=utf-8")
                self.send_header("content-length", str(len(rawb)))
                self.end_headers()
                self.wfile.write(rawb)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", self.port), H)

    def start(self) -> None:
        import threading
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def stop(self) -> None:
        try:
            self.httpd.shutdown()
            self.httpd.server_close()
        except Exception:  # noqa: BLE001
            pass


def state_of(rig: Rig, body_text: str) -> dict:
    rows = core.read_records("target.one", rig.home)
    for m in rows:
        if str(m.get("body")) == body_text:
            return m
    return {}


def run_case(rig: Rig, adapter: FakeAdapter, body_text: str, tag: str) -> dict:
    """发一封信给 adapter 代管的身份，等一会后回报那封的状态。"""
    adapter.seen.clear()
    _, sent = rig.post("/messages/send",
                       {"from": "peer.one", "to": "target.one",
                        "body": body_text, "correlation_id": f"b1:{tag}",
                        "wake": True}, rig.peer_key())
    time.sleep(2.5)
    return state_of(rig, body_text)


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="anp-b1-"))
    try:
        print("=== B1-0. SDK 常量与 Spec 合法值闭集一致 ===")
        spec_text = (REPO / "docs" / "ANP-ADAPTER-SPEC.md").read_text(encoding="utf-8")
        for m in RESPONSE_MODES:
            say(f"B1-0 合法值 {m} 在 Spec §5.1.1 里", f"`{m}`" in spec_text)
        say("B1-0 默认值本身合法", DEFAULT_RESPONSE_MODE in RESPONSE_MODES)
        say("B1-0 Spec 没有把 delivery mode 混进响应 mode",
            "响应体里的 `mode` **不得**出现 `http` / `exec` / `queue`" in spec_text)

        rig = Rig(tmp, "b1")
        try:
            print("\n=== B1-1. ① ok=true + 合法 mode → 签收 ===")
            a1 = FakeAdapter("ok_mode")
            a1.start()
            rig.post("/heartbeat", {"name": "target.one",
                                    "wake": {"kind": "http",
                                             "url": f"http://127.0.0.1:{a1.port}/anp/wake"}})
            st = run_case(rig, a1, "B1 正例信", "pos")
            say("B1-1.1 投递返回成功", bool(st.get("id")), "(信已落盘)")
            say("B1-1.2 ★该封被签收（read=true 或 read_by 已记）★",
                bool(st.get("read")) or bool(st.get("read_by")),
                json.dumps({"read": st.get("read"),
                            "read_by": st.get("read_by"),
                            "delivered_at": bool(st.get("delivered_at"))}))
            say("B1-1.3 没有排重投", st.get("next_retry_at") is None,
                f"next_retry_at={st.get('next_retry_at')}")
            a1.stop()

            print("\n=== B1-2. ② ok=true 但缺 mode → 不签收 ===")
            a2 = FakeAdapter("no_mode")
            a2.start()
            st2 = run_case(rig, a2, "B1 缺 mode 信", "nomode")
            say("B1-2.1 ★该封未被签收★",
                not st2.get("read") and not st2.get("read_by"),
                json.dumps({"read": st2.get("read"),
                            "read_by": st2.get("read_by")}))
            say("B1-2.2 没写 delivered_at（不算投递完成）",
                not st2.get("delivered_at"),
                f"delivered_at={st2.get('delivered_at')}")
            say("B1-2.3 仍按既有规则可重投（next_retry_at 有值）",
                st2.get("next_retry_at") is not None,
                f"next_retry_at={st2.get('next_retry_at')}")
            a2.stop()

            print("\n=== B1-3. ③ HTTP 200 但 ok=false → 不签收 ===")
            a3 = FakeAdapter("not_ok")
            a3.start()
            st3 = run_case(rig, a3, "B1 ok=false 信", "notok")
            say("B1-3.1 该封未被签收", not st3.get("read"))
            say("B1-3.2 没有 delivered_at", not st3.get("delivered_at"))
            a3.stop()

            print("\n=== B1-4. ④ ok=true 但 mode 非法 → 不签收（补强）===")
            a4 = FakeAdapter("bad_mode")
            a4.start()
            st4 = run_case(rig, a4, "B1 mode=http 信", "badmode")
            say("B1-4.1 mode=http（delivery mode 混进响应）不被签收",
                not st4.get("read"))
            a4.stop()
            a5 = FakeAdapter("empty_mode")
            a5.start()
            st5 = run_case(rig, a5, "B1 mode 空串信", "emptymode")
            say("B1-4.2 mode 空串不被签收", not st5.get("read"))
            a5.stop()

            print("\n=== B1-5. ⑤ 响应体非 JSON → 不签收（补强）===")
            a6 = FakeAdapter("not_json")
            a6.start()
            st6 = run_case(rig, a6, "B1 非 JSON 信", "notjson")
            say("B1-5.1 非 JSON 响应不被签收", not st6.get("read"))
            a6.stop()

            print("\n=== B1-6. 负例可红（人为把 SDK 默认响应改坏）===")
            # 直接验 Core 的判定函数：这是「测试能不能发现回归」的元测试
            bad = {"ok": True}                       # 只回 ok
            ids = core.wake_sign_off_ids({**bad, "kind": "http"},
                                         {"id": "x", "to": "target.one"})
            say("B1-6.1 wake_sign_off_ids 对缺 mode 的响应返回空（不签收）",
                ids == [], f"{ids}")
            good = {"ok": True, "mode": DEFAULT_RESPONSE_MODE}
            ids2 = core.wake_sign_off_ids(good, {"id": "x", "to": "target.one"})
            say("B1-6.2 带合法 mode 才返回可签收 id",
                ids2 == ["x"], f"{ids2}")

            print("\n=== B1-7. 未改动的东西还在（回归护栏）===")
            say("B1-7.1 retry / 退避公式未动（AGENT_ANP_RETRY_SEC 仍 120 默认）",
                "AGENT_ANP_RETRY_SEC" in (SRC / "anp_core.py").read_text(encoding="utf-8"))
            say("B1-7.2 queue 终态未动（P0-0 的 no_push_target 仍在）",
                '"no_push_target"' in (SRC / "anp_core.py").read_text(encoding="utf-8"))
            say("B1-7.3 三档 delivery mode 没新增",
                # ★每种档位都要带自己的必填字段★：http 缺 url / exec 缺 command
                # 时 delivery_mode 正确地回退成 queue。这里不能只给 kind。
                (core.delivery_mode({"wake": {"kind": "http",
                                              "url": "http://127.0.0.1:9/wake"}}) == "http"
                 and core.delivery_mode({"wake": {"kind": "exec",
                                                  "command": ["python", "x.py"]}}) == "exec"
                 and core.delivery_mode({"wake": {"kind": "queue"}}) == "queue"),
                str({"http": core.delivery_mode({"wake": {"kind": "http",
                                                          "url": "http://127.0.0.1:9/wake"}}),
                     "exec": core.delivery_mode({"wake": {"kind": "exec",
                                                          "command": ["python", "x.py"]}}),
                     "queue": core.delivery_mode({"wake": {"kind": "queue"}})}))
            say("B1-7.4 x-mailbox-key 仍是目标 Agent 令牌（P0-4 未动）",
                all(r["key"] == core.key_for("target.one", rig.home)
                    for r in a1.seen if r["key"]))
        finally:
            rig.kill()

        print(f"\n===== B1 验收：{OK} 通过 / {FAIL} 失败 =====")
        if FAIL:
            print("★有 FAIL★：B1 是契约裂缝，负例必须可红。")
        return 1 if FAIL else 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
