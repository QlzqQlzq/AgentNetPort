"""B3 验收：sticky registration fields 的**三态行为**（P0-6）。

原来只测了「规范列的 7 个字段 == `core.STICKY_FIELDS`」（conformance §3.4），
三态**行为**一条都没有。fix list：P0-6 的三态必须逐条有可失败的测试。

    missing → 保留旧值      （本次没提这个字段）
    None    → 清掉          （本次显式给 null）
    {}      → 替换成空 dict  （本次显式给空对象，不是"没给"）

三条都走**真实 HTTP `/heartbeat`**，不是直接调 `core.heartbeat()`——
要验的就是「第三方按规范发 JSON」这条路。

顺带钉一个易混点：`{}` 与「不给」在 JSON 里是两回事，行为必须不同。
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


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="anp-b3-"))
    try:
        home = tmp / "home"
        (home / "plugins").mkdir(parents=True, exist_ok=True)
        port = free_port()
        base = f"http://127.0.0.1:{port}"
        hub = subprocess.Popen(
            [sys.executable, str(SRC / "anp_hub.py"), "--host", "127.0.0.1",
             "--port", str(port), "--home", str(home)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            env={**os.environ, "AGENT_ANP_EVENT_SCAN_SEC": "1"})
        for _ in range(80):
            try:
                urllib.request.urlopen(base + "/health", timeout=1)
                break
            except Exception:  # noqa: BLE001
                time.sleep(0.25)

        def hb(body: dict, key: str = "") -> tuple[int, dict]:
            req = urllib.request.Request(
                base + "/heartbeat", method="POST",
                data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                headers={"content-type": "application/json; charset=utf-8",
                         **({"x-mailbox-key": key} if key else {})})
            try:
                with urllib.request.urlopen(req, timeout=20) as r:
                    return r.status, json.loads(r.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                try:
                    return e.code, json.loads(e.read().decode("utf-8") or "{}")
                except json.JSONDecodeError:
                    return e.code, {}

        def row(who: str) -> dict:
            return core.agent_table(home).get(who) or {}

        OLD_WAKE = {"kind": "http", "url": "http://127.0.0.1:9/anp/wake"}
        OLD_CAPS = {"queue": True, "inject_session": True}
        OLD_PA = {"type": "legacy"}

        # 首次注册：把三个字段都立起来
        code, first = hb({"name": "sticky.guy", "wake": OLD_WAKE,
                          "caps": OLD_CAPS, "process_api": OLD_PA,
                          "description": "初版描述"})
        key = str(first.get("key") or "")
        say("B3-0 首次注册拿到令牌", bool(key))
        r0 = row("sticky.guy")
        say("B3-0.1 三个 sticky 字段都立住了",
            r0.get("wake") == OLD_WAKE and r0.get("caps") == OLD_CAPS
            and r0.get("process_api") == OLD_PA,
            json.dumps({"wake": bool(r0.get("wake")), "caps": bool(r0.get("caps")),
                        "process_api": bool(r0.get("process_api"))}))

        # ---------- ① missing → 保留 ----------
        print("\n=== B3-1. 缺失（本次没提这个字段）→ 保留旧值 ===")
        hb({"name": "sticky.guy", "description": "二版描述"}, key)
        r1 = row("sticky.guy")
        say("B3-1.1 wake 没提 → 原样保留", r1.get("wake") == OLD_WAKE,
            json.dumps(r1.get("wake")))
        say("B3-1.2 caps 没提 → 原样保留", r1.get("caps") == OLD_CAPS)
        say("B3-1.3 process_api 没提 → 原样保留", r1.get("process_api") == OLD_PA)
        say("B3-1.4 非 sticky 的 description 照常被覆盖",
            r1.get("description") == "二版描述", str(r1.get("description")))

        # ---------- ② None → 清 ----------
        print("\n=== B3-2. null（显式清空）→ 字段消失 ===")
        hb({"name": "sticky.guy", "process_api": None}, key)
        r2 = row("sticky.guy")
        say("B3-2.1 ★显式 null → process_api 被清掉★",
            r2.get("process_api") is None, json.dumps(r2.get("process_api")))
        say("B3-2.2 同时没提的 wake 仍然保留（null 只影响点到的那一个）",
            r2.get("wake") == OLD_WAKE)

        # ---------- ③ {} → 替换 ----------
        print("\n=== B3-3. {}（显式给空对象）→ 替换成空，不是保留 ===")
        hb({"name": "sticky.guy", "caps": {}}, key)
        r3 = row("sticky.guy")
        say("B3-3.1 ★显式 {} → caps 被替换成空 dict★",
            r3.get("caps") == {}, json.dumps(r3.get("caps")))
        say("B3-3.2 与「不给」区分：没给时它还是旧值（对照 B3-1.2）",
            row("sticky.guy").get("wake") == OLD_WAKE)

        # ---------- 反向确认：{} 与 missing 行为不同 ----------
        print("\n=== B3-4. 三态互不相同（防实现偷懒只做两态）===")
        # ★令牌必须收下来★：other.guy 一旦已知，后续不带 key 会被 P0-2 挡成 401，
        # 三次请求全 401 → caps 永远是 {"a":1}，看着像"null/{} 不生效"，
        # 其实是自己的请求根本没进状态机。（第一版就这么栽过）
        _, first_o = hb({"name": "other.guy", "caps": {"a": 1}}, "")
        ko = str(first_o.get("key") or "")
        say("B3-4.0 拿到了 other.guy 的令牌（否则后面三次全 401）", bool(ko))
        _, _ = hb({"name": "other.guy"}, ko)            # missing
        a = row("other.guy").get("caps")
        _, _ = hb({"name": "other.guy", "caps": None}, ko)   # null
        b = row("other.guy").get("caps")
        _, _ = hb({"name": "other.guy", "caps": {}}, ko)     # {}
        c = row("other.guy").get("caps")
        say("B3-4.1 missing 保留 / None 清空 / {} 空对象，三者两两不同",
            a == {"a": 1} and b is None and c == {},
            json.dumps({"missing": a, "null": b, "empty": c}))

        # ---------- Spec 与实现一致 ----------
        print("\n=== B3-5. Spec 的三态措辞与实现一致 ===")
        spec = (REPO / "docs" / "ANP-ADAPTER-SPEC.md").read_text(encoding="utf-8")
        # 照 Spec §3.4 的原话逐一对照（带 preserve/clear/replace 英文锚点）
        say("B3-5.1 Spec §3.4 写着三态（preserve / clear / replace）",
            "字段缺失" in spec and "preserve" in spec
            and "字段 = null" in spec and "clear" in spec
            and "字段 = {}" in spec and "replace" in spec,
            "§3.4 的三态措辞" if "字段缺失" in spec else "§3.4 措辞变了，要同步")
        say("B3-5.2 速查表 P0-6 也写着三态",
            "缺失→保留" in spec and "null`→清" in spec and "{}`→替换" in spec)
        say("B3-5.2 STICKY_FIELDS 仍是那 7 个",
            core.STICKY_FIELDS == ("wake", "caps", "skills", "process_api",
                                   "description", "workspace", "client"),
            str(core.STICKY_FIELDS))

        try:
            hub.kill()
        except Exception:  # noqa: BLE001
            pass
        print(f"\n===== B3 sticky 三态验收：{OK} 通过 / {FAIL} 失败 =====")
        if FAIL:
            print("★有 FAIL★：三态少一态，第三方就会遇到『改了却没生效』的怪事。")
        return 1 if FAIL else 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
