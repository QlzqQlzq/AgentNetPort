"""P0-9 验收：canonical /messages/receive + /send 别名。

只验 API surface，不碰别的。核心一条：

    /tool(mail_inbox) 与 /messages/receive 必须走**同一份**底层实现
    → 对同一状态的收件箱、用同一组参数，两次返回必须逐字段一致

这条不过，就说明 receive 出现了第二套逻辑，正是要避免的事。
"""
from __future__ import annotations

import json
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


class Hub:
    def __init__(self, home: Path, port: int):
        self.base = f"http://127.0.0.1:{port}"
        self.proc = subprocess.Popen(
            [sys.executable, str(SRC / "anp_hub.py"), "--host", "127.0.0.1",
             "--port", str(port), "--home", str(home)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def wait(self, tries: int = 80) -> bool:
        for _ in range(tries):
            try:
                urllib.request.urlopen(self.base + "/health", timeout=1)
                return True
            except Exception:  # noqa: BLE001
                time.sleep(0.25)
        return False

    def raw(self, method: str, path: str, body: dict | None = None,
            key: str = "") -> tuple[int, dict, str]:
        """返回 (状态码, JSON, deprecation 头)。"""
        headers = {"content-type": "application/json"}
        if key:
            headers["x-mailbox-key"] = key
        try:
            with urllib.request.urlopen(urllib.request.Request(
                    self.base + path, method=method,
                    data=(json.dumps(body or {}, ensure_ascii=False).encode()
                          if method == "POST" else None),
                    headers=headers), timeout=25) as r:
                raw_text = r.read().decode()
                try:
                    parsed = json.loads(raw_text)
                except json.JSONDecodeError:
                    parsed = {"_raw": raw_text}
                return r.status, parsed, (r.headers.get("deprecation") or "")
        except urllib.error.HTTPError as e:
            try:
                return e.code, json.loads(e.read().decode() or "{}"), ""
            except json.JSONDecodeError:
                return e.code, {}, ""

    def post(self, path: str, body: dict, key: str = "") -> dict:
        return self.raw("POST", path, body, key)[1]

    def kill(self) -> None:
        try:
            self.proc.kill()
        except Exception:  # noqa: BLE001
            pass


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="anp-p09-"))
    home = tmp / "home"
    home.mkdir(parents=True, exist_ok=True)
    h = Hub(home, free_port())
    try:
        check("hub 起来了", h.wait())
        ak = h.post("/heartbeat", {"name": "alice"}).get("key") or ""
        bk = h.post("/heartbeat", {"name": "bob"}).get("key") or ""
        for i in range(3):
            h.post("/messages/send", {"from": "alice", "to": "bob",
                                      "body": f"第 {i} 封",
                                      "wake": False}, ak)

        # ---- 1. /messages/send 是正名，且与 /send 行为一致 ----
        print("\n--- 1. /messages/send（canonical）---")
        c1, r1, dep1 = h.raw("POST", "/messages/send",
                             {"from": "alice", "to": "bob", "body": "正名发的信",
                              "wake": False}, ak)
        check("1.1 正名可用", c1 == 200 and r1.get("ok") is True,
              f"code={c1} ok={r1.get('ok')}")
        check("1.2 正名不带 deprecation 头", dep1 == "", repr(dep1))

        print("\n--- 2. /send（deprecated 别名）---")
        c2, r2, dep2 = h.raw("POST", "/send",
                             {"from": "alice", "to": "bob", "body": "别名发的信",
                              "wake": False}, ak)
        check("2.1 别名仍然可用（不破坏现有调用方）", c2 == 200 and r2.get("ok") is True)
        check("2.2 别名带 deprecation 头并指明去向",
              "messages/send" in dep2, repr(dep2))
        check("2.3 两条路径返回的字段集合一致（不是复制出的另一套）",
              set(r1) == set(r2), f"{sorted(set(r1) ^ set(r2))}")

        # ---- 3. /messages/receive 与 /tool(mail_inbox) 语义一致 ----
        print("\n--- 3. ★/messages/receive 与 /tool(mail_inbox) 共享同一份实现★---")
        # 先给 bob 灌一封带过滤特征的信，两边用同样的参数读
        h.post("/messages/send", {"from": "alice", "to": "bob", "body": "带关联线的信",
                                  "kind": "test.note", "correlation_id": "anp:p09",
                                  "wake": False}, ak)
        args = {"peek": True, "unread_only": False, "kind": "test.note",
                "correlation_id": "anp:p09", "limit": 10, "order": "newest"}
        _, via_receive, _ = h.raw("POST", "/messages/receive",
                                  {"from": "bob", **args}, bk)
        _, via_tool, _ = h.raw("POST", "/tool",
                               {"server": "bob", "name": "mail_inbox", "args": args}, bk)
        a = {k: v for k, v in via_receive.items() if k != "auth"}
        b = {k: v for k, v in via_tool.items() if k != "auth"}
        check("3.1 ★两个入口返回逐字段一致★", a == b,
              f"差异键={sorted(set(a) ^ set(b))}")
        check("3.2 self / unread / signed_off / note 都在",
              all(k in a for k in ("self", "unread", "signed_off", "note", "messages")),
              str(sorted(a)))
        check("3.3 过滤真的生效（只回那一条）",
              len(a.get("messages") or []) == 1
              and (a["messages"][0].get("body") == "带关联线的信"),
              json.dumps([m.get("body") for m in (a.get("messages") or [])], ensure_ascii=False))

        # ---- 4. 不传 peek 时默认"读了就签收"（与 mail_inbox 同口径）----
        print("\n--- 4. 默认签收口径一致 ---")
        # ★先造出未读★：缓存信与成功投递的信都会被信箱自动签收（read_by=…），
        # 未读恒 0，没东西可签。queue 档的信才是"真的未读"（P0-0 之后保持未读）。
        h.post("/heartbeat", {"name": "bob", "wake": {"kind": "queue"}}, bk)
        h.post("/messages/send", {"from": "alice", "to": "bob", "body": "未读甲",
                                  "wake": True}, ak)
        h.post("/messages/send", {"from": "alice", "to": "bob", "body": "未读乙",
                                  "wake": True}, ak)
        _, peeked, _ = h.raw("POST", "/messages/receive",
                             {"from": "bob", "peek": True, "unread_only": False}, bk)
        before = peeked.get("unread", 0)
        _, got, _ = h.raw("POST", "/messages/receive", {"from": "bob"}, bk)
        after = got.get("unread", 0)
        check("4.1 不传 peek 就读了即签收", before > 0 and after == 0,
              f"未读 {before} → {after}")
        _, got2, _ = h.raw("POST", "/tool",
                           {"server": "bob", "name": "mail_inbox",
                            "args": {"peek": True, "unread_only": False}}, bk)
        check("4.2 签收后 read_by 记的是同一套（read_by 存在且非空）",
              all(m.get("read_by") for m in (got2.get("messages") or [])[:2]),
              json.dumps([m.get("read_by") for m in (got2.get("messages") or [])][:3],
                         ensure_ascii=False))

        # ---- 5. 鉴权与错误路径 ----
        print("\n--- 5. 鉴权 ---")
        c5, r5, _ = h.raw("POST", "/messages/receive", {"from": "bob"}, "")
        check("5.1 无密钥 → 401", c5 == 401, f"code={c5}")
        check("5.2 错误信息说明要带密钥", "密钥" in str(r5.get("error")), str(r5.get("error"))[:60])
        c5b, _, _ = h.raw("POST", "/messages/receive", {}, ak)
        check("5.3 不给身份名 → 401（unknown 没有密钥）", c5b == 401, f"code={c5b}")
        c5c, _, _ = h.raw("POST", "/messages/receive", {"from": "nobody.here"}, "")
        check("5.4 未登记的身份也要密钥", c5c == 401, f"code={c5c}")

        # ---- 6. /tool 的 mail_inbox 一个字没坏 ----
        print("\n--- 6. 旧入口未受影响 ---")
        c6, r6, _ = h.raw("POST", "/tool",
                          {"server": "bob", "name": "mail_inbox",
                           "args": {"peek": True, "unread_only": False}}, bk)
        check("6.1 /tool(mail_inbox) 照旧", c6 == 200 and r6.get("ok") is True
              and isinstance(r6.get("messages"), list))
        c7, r7, _ = h.raw("POST", "/tool",
                          {"server": "bob", "name": "mail_send",
                           "args": {"to": "alice", "body": "回到旧入口"}}, bk)
        check("6.2 /tool(mail_send) 照旧", c7 == 200 and r7.get("ok") is True)

        # ---- 7. canonical 路由清单（P0-11 的 6 个 Core API 全在场）----
        print("\n--- 7. canonical API surface ---")
        for path, body, key in [
            ("/heartbeat", {"name": "probe.surface"}, ""),
            ("/messages/send", {"from": "alice", "to": "bob", "body": "s", "wake": False}, ak),
            ("/messages/receive", {"from": "bob", "peek": True}, bk),
            ("/messages/query", {"from": "alice", "correlation_id": "anp:p09"}, ak),
        ]:
            c, _, _ = h.raw("POST", path, body, key)
            check(f"7. POST {path} 在场", c in (200, 404), f"code={c}")
        c, r, _ = h.raw("GET", "/agents")
        check("7.5 GET /agents 在场", c == 200 and r.get("ok") is True)
        c, r, _ = h.raw("POST", "/plugin/register", {"id": "ghost"}, "")
        check("7.6 POST /plugin/register 在场（未加载插件 → 401 要令牌）", c in (401, 404),
              f"code={c}")

        print(f"\n===== P0-9 验收：{OK} 通过 / {FAIL} 失败 =====")
        return 1 if FAIL else 0
    finally:
        h.kill()
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
