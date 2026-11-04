"""全局只读管理 API：GET /admin/messages（WebUI 数据面）。

验证 docs/ANP-ADMIN-API.md 的验收项：
  - 无 correlation_id 的信能被扫到（证明没塞进 /messages/query）
  - admin 鉴权（无 / 错 token → 401）
  - inbox / unread / from / q 过滤
  - 调用前后状态字段不变（只读）
  - /messages/query 仍 owner+correlation，不被本 API 改写
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import anp_core as core  # noqa: E402

results: list[tuple[str, bool, str]] = []
TMP = Path(tempfile.mkdtemp(prefix="anp-admin-api-"))


def say(name: str, ok: bool, extra: str = "") -> None:
    results.append((name, bool(ok), extra))
    print(f"  [{'OK  ' if ok else 'FAIL'}] {name}" + (f"  <- {extra}" if extra else ""))


def post(base: str, path: str, body: dict, key: str = "", admin: str = "") -> tuple[int, dict]:
    headers = {"content-type": "application/json; charset=utf-8"}
    if admin:
        headers["x-anp-admin"] = admin
    elif key:
        headers["x-mailbox-key"] = key
    req = urllib.request.Request(
        base + path, method="POST",
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers=headers,
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, json.loads(r.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8") or "{}")
        except Exception:  # noqa: BLE001
            return e.code, {}


def get(base: str, path: str, admin: str = "") -> tuple[int, dict]:
    headers = {"x-anp-admin": admin} if admin else {}
    # path 可能带非 ASCII query（如 q=中文）——必须先编码
    if "?" in path:
        raw_path, raw_qs = path.split("?", 1)
        pairs = urllib.parse.parse_qsl(raw_qs, keep_blank_values=True)
        path = raw_path + "?" + urllib.parse.urlencode(pairs, encoding="utf-8")
    req = urllib.request.Request(base + path, method="GET", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, json.loads(r.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8") or "{}")
        except Exception:  # noqa: BLE001
            return e.code, {}


def main() -> int:
    import subprocess

    home = TMP / "home"
    home.mkdir(parents=True, exist_ok=True)
    # admin.token / hub.secret 落盘后 ACL 收紧，测试进程可能读/写不动——用环境变量钉死
    admin = "test-admin-token-selftest-admin-api"
    os.environ["AGENT_ANP_ADMIN_TOKEN"] = admin
    os.environ["AGENT_ANP_HUB_SECRET"] = "test-hub-secret-selftest-admin-api"
    # 端口：临时拿一个
    import socket

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    base = f"http://127.0.0.1:{port}"
    proc = subprocess.Popen(
        [sys.executable, str(SRC / "anp_hub.py"), "--host", "127.0.0.1",
         "--port", str(port), "--home", str(home)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        env={**os.environ, "AGENT_ANP_WEBUI": "0", "AGENT_ANP_MAINTENANCE": "0",
             "AGENT_ANP_ADMIN_TOKEN": admin,
             "AGENT_ANP_HUB_SECRET": os.environ["AGENT_ANP_HUB_SECRET"]},
    )
    try:
        for _ in range(80):
            try:
                urllib.request.urlopen(base + "/health", timeout=1)
                break
            except Exception:  # noqa: BLE001
                time.sleep(0.25)

        # 绕开 Windows 上 keys.json/_tighten 二次写入 PermissionError（既有问题，非本 API）：
        # 消息用 core.send 直接落盘；令牌表手工写好、不再走 write_keys。
        (home / "keys.json").write_text(
            json.dumps({"alice": "ak-selftest-admin", "bob": "bk-selftest-admin"},
                       ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        ak = "ak-selftest-admin"
        bk = "bk-selftest-admin"

        core.send("alice", "bob", "普通私信无关联", subject="私信", home=home)
        core.send("alice", "bob", "有关联的协作", subject="协作",
                  correlation_id="adm:thread-1", home=home)
        core.send("bob", "alice", "bob 回话", subject="回话", home=home)

        # --- 鉴权 ---
        code, r = get(base, "/admin/messages")
        say("A1 无 admin → 401", code == 401, f"code={code}")
        code, r = get(base, "/admin/messages", admin="wrong-token")
        say("A2 错 admin → 401", code == 401, f"code={code}")

        code, r = get(base, "/admin/messages", admin=admin)
        say("A3 正确 admin → 200 ok", code == 200 and r.get("ok") is True, f"code={code}")
        msgs = r.get("messages") or []
        say("A4 含无 correlation_id 的信", any(not m.get("correlation_id") for m in msgs),
            f"n={len(msgs)}")
        say("A5 含有关联线的信", any(m.get("correlation_id") == "adm:thread-1" for m in msgs))
        say("A6 每封带 inbox 字段", all(m.get("inbox") for m in msgs))
        say("A7 默认带 sig_ok", all("sig_ok" in m for m in msgs))
        say("A8 totals 在响应里", isinstance(r.get("totals"), dict) and "unread" in (r.get("totals") or {}))

        # --- 过滤 ---
        code, r = get(base, "/admin/messages?inbox=bob&unread=1", admin=admin)
        only_bob_unread = r.get("messages") or []
        say("B1 inbox+unread 过滤",
            all(m.get("inbox") == "bob" and not m.get("read") for m in only_bob_unread)
            and len(only_bob_unread) >= 1,
            f"n={len(only_bob_unread)}")

        code, r = get(base, "/admin/messages?q=普通私信", admin=admin)
        hits = r.get("messages") or []
        say("B2 q 全文搜索", len(hits) == 1 and "普通私信" in (hits[0].get("body") or ""),
            f"n={len(hits)}")

        code, r = get(base, "/admin/messages?from=bob", admin=admin)
        say("B3 from 过滤", all(m.get("from") == "bob" for m in (r.get("messages") or []))
            and len(r.get("messages") or []) >= 1)

        code, r = get(base, "/admin/messages?correlation_id=adm:thread-1", admin=admin)
        say("B4 correlation_id 过滤",
            all(m.get("correlation_id") == "adm:thread-1" for m in (r.get("messages") or []))
            and len(r.get("messages") or []) >= 1)

        # --- 只读 ---
        code, r = get(base, "/admin/messages", admin=admin)
        before = {(m.get("inbox"), m.get("id")): (m.get("read"), m.get("attempts"), m.get("delivered_at"))
                  for m in (r.get("messages") or [])}
        get(base, "/admin/messages?unread=1&q=x&limit=5", admin=admin)
        code, r2 = get(base, "/admin/messages", admin=admin)
        after = {(m.get("inbox"), m.get("id")): (m.get("read"), m.get("attempts"), m.get("delivered_at"))
                 for m in (r2.get("messages") or [])}
        say("E1 浏览前后状态字段不变（只读）", before == after)

        # --- 与 /messages/query 隔离 ---
        code, rq = post(base, "/messages/query",
                        {"from": "bob", "correlation_id": "adm:thread-1"}, bk)
        say("D1 非 owner 读关联线仍被拒（query 不因 admin API 放宽）",
            rq.get("ok") is False, json.dumps(rq, ensure_ascii=False)[:120])
        code, rq = post(base, "/messages/query",
                        {"from": "alice", "correlation_id": "adm:thread-1"}, ak)
        say("D2 owner 仍可读关联线", rq.get("ok") is True and rq.get("count", 0) >= 1)

        code, rq = post(base, "/messages/query", {"from": "alice"}, ak)
        say("D3 query 仍强制 correlation_id", rq.get("ok") is False)

    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:  # noqa: BLE001
            proc.kill()
        if os.environ.get("ANP_TEST_KEEP") != "1":
            shutil.rmtree(TMP, ignore_errors=True)

    passed = sum(1 for _, ok, _ in results if ok)
    print(f"\n===== {passed}/{len(results)} 通过 =====")
    failed = [n for n, ok, _ in results if not ok]
    if failed:
        print("失败项: " + ", ".join(failed))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
