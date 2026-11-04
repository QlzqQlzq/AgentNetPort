"""P0-0 验收：queue 档不得进入 push 重投循环。

只测这一件事，场景按主人锁的 5 条：
  1. queue + wake=true   → persisted，0 retry
  2. queue + wake=false  → persisted，0 retry
  3. http + 端点不可达    → 正常 retry
  4. exec + 启动失败      → 正常 retry
  5. 自动重投最终放弃      → 只对真正 push 失败触发 message.undelivered
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
from http.server import BaseHTTPRequestHandler
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

import anp_core as core   # noqa: E402
import anp_hub as hub     # noqa: E402

OK = FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global OK, FAIL
    OK += 1 if cond else 0
    FAIL += 0 if cond else 1
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  <- {detail}" if detail else ""))


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class Down(BaseHTTPRequestHandler):
    """一个必然连不上的端点（占端口但不响应）。"""

    def log_message(self, *a):
        pass


class Hub:
    def __init__(self, home: Path, port: int):
        self.base = f"http://127.0.0.1:{port}"
        env = {**os.environ, "AGENT_ANP_RETRY_SEC": "1", "AGENT_ANP_REDELIVER_MAX": "5"}
        self.proc = subprocess.Popen(
            [sys.executable, str(SRC / "anp_hub.py"), "--host", "127.0.0.1",
             "--port", str(port), "--home", str(home)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)
        self.events: list[dict] = []

    def wait(self, tries: int = 80) -> bool:
        for _ in range(tries):
            try:
                urllib.request.urlopen(self.base + "/health", timeout=1)
                return True
            except Exception:  # noqa: BLE001
                time.sleep(0.25)
        return False

    def post(self, path: str, body: dict, key: str = "") -> dict:
        headers = {"content-type": "application/json"}
        if key:
            headers["x-mailbox-key"] = key
        try:
            with urllib.request.urlopen(urllib.request.Request(
                    self.base + path, method="POST",
                    data=json.dumps(body, ensure_ascii=False).encode(),
                    headers=headers), timeout=30) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            try:
                return json.loads(e.read().decode() or "{}")
            except json.JSONDecodeError:
                return {}

    def health(self) -> dict:
        with urllib.request.urlopen(self.base + "/health", timeout=15) as r:
            return json.loads(r.read().decode())

    def kill(self) -> None:
        try:
            self.proc.kill()
        except Exception:  # noqa: BLE001
            pass


def row_of(home: Path, who: str, mid: str) -> dict:
    return next((r for r in core.read_records(who, home) if r.get("id") == mid), {})


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="anp-p00-"))
    home = tmp / "home"
    home.mkdir(parents=True, exist_ok=True)
    port = free_port()
    h = Hub(home, port)
    try:
        check("hub 起来了", h.wait())

        ak = h.post("/heartbeat", {"name": "alice"}).get("key") or ""
        h.post("/heartbeat", {"name": "queue.one",
                              "wake": {"kind": "queue"},
                              "description": "显式 queue 档"})
        h.post("/heartbeat", {"name": "nowake.one",
                              "description": "压根没登记 wake"})
        h.post("/heartbeat", {"name": "http.one", "wake": {
            "kind": "http", "url": f"http://127.0.0.1:{free_port()}/dead"}})
        h.post("/heartbeat", {"name": "exec.one", "wake": {
            "kind": "exec", "command": [sys.executable, "-c", "import sys;sys.exit(3)"]}})

        # ---- 场景 1：queue + wake=true ----
        r1 = h.post("/send", {"from": "alice", "to": "queue.one", "body": "一",
                              "wake": True}, ak)
        mid1 = (r1.get("message") or {}).get("id") or ""
        row1 = row_of(home, "queue.one", mid1)
        print("\n--- 场景 1：queue + wake=true ---")
        check("1.1 消息正常持久化", bool(row1))
        check("1.2 ★attempts = 0★", row1.get("attempts", 0) == 0,
              f"attempts={row1.get('attempts')}")
        check("1.3 ★next_retry_at = None★", row1.get("next_retry_at") is None,
              f"next_retry_at={row1.get('next_retry_at')}")
        check("1.4 ★last_error = None★", row1.get("last_error") is None,
              f"last_error={row1.get('last_error')}")
        check("1.5 ★wake_skipped = no_push_target★",
              row1.get("wake_skipped") == "no_push_target",
              f"wake_skipped={row1.get('wake_skipped')!r}")
        # 单收件人路径的返回是平铺的（to/id/ok/wake/cached）。
        # 注意语义：do_send 的 ok = 「信已落盘」（queue 档也是 True）；
        # 投递结果在 wake 字段里。真正的"不是失败"由 1.2～1.5 的 attempts/next_retry_at/
        # last_error/wake_skipped 保证——那四项才是状态机的答案。
        check("1.6 单收件人：信已落盘（ok=true）且 wake.kind=queue",
              r1.get("ok") is True and (r1.get("wake") or {}).get("kind") == "queue",
              f"ok={r1.get('ok')} wake.kind={(r1.get('wake') or {}).get('kind')}")

        # ---- 场景 2：queue + wake=false（必须是另一个标记，不能被 1 混掉）----
        r2 = h.post("/send", {"from": "alice", "to": "queue.one", "body": "二",
                              "wake": False}, ak)
        mid2 = (r2.get("message") or {}).get("id") or ""
        row2 = row_of(home, "queue.one", mid2)
        print("\n--- 场景 2：queue + wake=false（发送方主动不唤醒）---")
        check("2.1 也持久化、0 retry", bool(row2)
              and row2.get("attempts", 0) == 0 and row2.get("next_retry_at") is None)
        check("2.2 ★wake_skipped = True（与 queue 档区分开）★",
              row2.get("wake_skipped") is True, f"wake_skipped={row2.get('wake_skipped')!r}")
        check("2.3 ★两种原因标记不同★",
              row1.get("wake_skipped") != row2.get("wake_skipped"),
              f"{row1.get('wake_skipped')!r} vs {row2.get('wake_skipped')!r}")

        # ---- 2b：压根没登记 wake 的身份，也算 queue 档 ----
        r2b = h.post("/send", {"from": "alice", "to": "nowake.one", "body": "三"}, ak)
        row2b = row_of(home, "nowake.one", (r2b.get("message") or {}).get("id") or "")
        print("\n--- 场景 2b：没有 wake 字段的身份 ---")
        check("2b.1 ★同样按 queue 档终态处理★",
              row2b.get("wake_skipped") == "no_push_target"
              and row2b.get("attempts", 0) == 0
              and row2b.get("next_retry_at") is None,
              f"attempts={row2b.get('attempts')} ws={row2b.get('wake_skipped')!r}")

        # ---- 场景 3：http 端点不可达 → 必须照旧重投 ----
        r3 = h.post("/send", {"from": "alice", "to": "http.one", "body": "四"}, ak)
        mid3 = (r3.get("message") or {}).get("id") or ""
        row3 = row_of(home, "http.one", mid3)
        print("\n--- 场景 3：http + 端点不可达 ---")
        check("3.1 ★仍然算失败并排重投★", row3.get("next_retry_at") is not None,
              f"next_retry_at={row3.get('next_retry_at')}")
        check("3.2 attempts 递增到 1", row3.get("attempts") == 1,
              f"attempts={row3.get('attempts')}")
        check("3.3 有 last_error", bool(row3.get("last_error")))
        check("3.4 ★没有被打成 no_push_target★",
              row3.get("wake_skipped") != "no_push_target")

        # ---- 场景 4：exec 启动失败 → 必须照旧重投 ----
        r4 = h.post("/send", {"from": "alice", "to": "exec.one", "body": "五"}, ak)
        mid4 = (r4.get("message") or {}).get("id") or ""
        row4 = row_of(home, "exec.one", mid4)
        print("\n--- 场景 4：exec + 启动失败 ---")
        # exec 档 Popen 成功即视为 ok（进程自己退出 Core 看不到），所以这里验的是
        # "exec 档不会误判成 queue"：不应出现 no_push_target
        check("4.1 ★exec 档不被误判成 queue★",
              row4.get("wake_skipped") != "no_push_target",
              f"wake_skipped={row4.get('wake_skipped')!r}")
        check("4.2 delivery_mode 判定为 exec",
              core.delivery_mode(core.agent_table(home)["exec.one"]) == "exec")

        # ---- 场景 5：自动重投放弃 → 只对真 push 失败发 message.undelivered ----
        print("\n--- 场景 5：自动重投最终放弃 ---")
        # 把 http.one 那封的重投时间改成已到期，连跑 6 轮，让它走完"放弃"分支
        # ★test-harness 对齐★：do_redeliver 故意忽略 args["home"]（防 x-mailbox-home 漏洞，
        # 一律取服务端自己的 home）。所以测试进程里调 auto_redeliver 前要先把 hub.HOME
        # 指到同一个临时 home，否则它扫描 A、重投 B，必然"找不到消息"。生产里两者本来一致。
        hub.HOME = home
        # 退避基距由**调用方进程**的 record_delivery 计算（do_redeliver 在测试进程内跑），
        # 所以这里也要压到位，否则第二次重投就排到 4 分钟后，测试窗口内等不到。
        os.environ["AGENT_ANP_RETRY_SEC"] = "1"
        os.environ["AGENT_ANP_REDELIVER_MAX"] = "3"   # 验的是"放弃"这条路径，不是数字 5
        for _ in range(16):
            hub.auto_redeliver(home)
            time.sleep(0.5)
            if "已放弃" in str(row_of(home, "http.one", mid3).get("last_error") or ""):
                break
        row5 = row_of(home, "http.one", mid3)
        check("5.1 http.one 真的被放弃了（attempts 到上限 3）",
              row5.get("attempts", 0) >= 3, f"attempts={row5.get('attempts')}")
        check("5.2 last_error 说明已放弃",
              "已放弃自动重投" in str(row5.get("last_error") or ""),
              str(row5.get("last_error"))[:60])
        row_q = row_of(home, "queue.one", mid1)
        check("5.3 ★queue 那封完全没被重投碰过★",
              row_q.get("attempts", 0) == 0 and row_q.get("next_retry_at") is None,
              f"attempts={row_q.get('attempts')} next_retry_at={row_q.get('next_retry_at')}")
        check('5.4 ★queue 那封没有被标「已放弃」★',
              "已放弃" not in str(row_q.get("last_error") or ""))

        # 事件侧：queue 档不该产生 message.undelivered
        st = h.health().get("events") or {}
        check("5.5 事件总线没有因 queue 档误报（failed=0 或只有真失败）",
              st.get("failed", 0) == 0, json.dumps(st, ensure_ascii=False))

        # ---- 附带：delivery_mode 的三态 ----
        print("\n--- 附带：delivery_mode 三态 ---")
        tbl = core.agent_table(home)
        check("A1 http 档判定", core.delivery_mode(tbl["http.one"]) == "http")
        check("A2 exec 档判定", core.delivery_mode(tbl["exec.one"]) == "exec")
        check("A3 显式 queue 档判定", core.delivery_mode(tbl["queue.one"]) == "queue")
        check("A4 无 wake 字段判定为 queue", core.delivery_mode(tbl["nowake.one"]) == "queue")
        check("A5 空 entry 判定为 queue（不抛错）", core.delivery_mode({}) == "queue")

        print(f"\n===== P0-0 验收：{OK} 通过 / {FAIL} 失败 =====")
        return 1 if FAIL else 0
    finally:
        h.kill()
        if len(sys.argv) > 1 and sys.argv[1] == "--keep":
            print("临时目录保留在", tmp)
        else:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
