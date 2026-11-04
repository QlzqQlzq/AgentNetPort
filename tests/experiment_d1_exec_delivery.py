"""D1 最小实验：exec 档 spawn 成功是否直接写 `delivered_at`，会不会让信永不被重投。

**只实证、不改任何代码。** 结论决定了 1 = call。答辩纪律见 fix list：

    若 delivered_at != null 且后续 auto_redeliver 永不重投 → 实锤，进入修复
    若不成立 → D1 关闭，不动状态机

实验设计（fix list §D1「最小实验意图」逐条照做）：

    1. 注册 exec Agent，wake.command 指向「立刻 exit(1) 且不读环境」的脚本
    2. POST /messages/send 一封信
    3. 立刻读状态：attempts / delivered_at / next_retry_at / read / last_error
    4. 跑一轮 auto_redeliver（或等扫描线程）
    5. 再读同一封
    6. 对照组：脚本「正常处理并 mail_ack」→ 确认不会被再投
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

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "src"

if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
import anp_core as core      # noqa: E402

HOME = Path(tempfile.mkdtemp(prefix="d1-"))
PORT = 0


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def post(base: str, path: str, body: dict, key: str = "") -> dict:
    req = urllib.request.Request(
        base + path, method="POST",
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"content-type": "application/json; charset=utf-8",
                 **({"x-mailbox-key": key} if key else {})})
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode("utf-8") or "{}")
        except json.JSONDecodeError:
            return {}


def peer_key(base: str, home: Path) -> str:
    r = post(base, "/heartbeat", {"name": "peer.one"})
    k = str(r.get("key") or "")
    if not k and "peer.one" in core.agent_table(home):
        k = core.key_for("peer.one", home)
    return k


def snap(home: Path, who: str, body_text: str) -> dict:
    """把那封信的五个关键字段抓出来（同一封靠 body 认）。"""
    for m in core.read_records(who, home):
        if str(m.get("body")) == body_text:
            return {
                "attempts": m.get("attempts"),
                "delivered_at": m.get("delivered_at"),
                "next_retry_at": m.get("next_retry_at"),
                "read": m.get("read"),
                "read_by": m.get("read_by"),
                "last_error": (str(m.get("last_error"))[:70]
                               if m.get("last_error") else None),
            }
    return {"找不到了": True}


def show(tag: str, d: dict) -> None:
    print(f"    {tag}: " + json.dumps(d, ensure_ascii=False))


def write_crash_script(p: Path) -> None:
    """实验组：立刻 exit(1) 且不读环境，模拟 adapter 崩在启动瞬间。"""
    p.write_text("import sys\nsys.exit(1)\n", encoding="utf-8")


def write_ack_script(p: Path, hub: str) -> None:
    """对照组：正常读信封、处理、通过 **/tool 的 mail_ack** 确认落地（= 唤醒器 ack）。

    ★第一次写错了端点★：ack 不是独立 HTTP 路径，是 `/tool` 里的 `mail_ack` 工具。
    打错端点时对照组和实验组终态看起来一模一样——共组失效，实验就白做了。
    """
    # 被生成的子进程脚本。BASE 由调用方以 str() 注入。
    p.write_text(
        "import json, os, sys, urllib.request\n"
        "BASE = " + repr(hub) + "\n"
        "raw = os.environ.get('AGENT_ANP_WAKE_JSON') or '{}'\n"
        "m = json.loads(raw)\n"
        "who = os.environ.get('AGENT_ANP_SELF') or ''\n"
        "key = os.environ.get('AGENT_ANP_SELF_KEY') or ''\n"
        "body = json.dumps({\n"
        "    'server': who,\n"
        "    'name': 'mail_ack',\n"
        # ★/tool 的参数键名是 args（不是 arguments），且 do_ack 里是 ids/id★。
        # 这两个名字错过一次，ack 就静默失败，对照组看着"正常"其实是空的。
        "    'args': {'ids': [m.get('id')]},\n"
        "}, ensure_ascii=False)\n"
        "req = urllib.request.Request(BASE + '/tool', method='POST',\n"
        "                            data=body.encode('utf-8'),\n"
        "                            headers={\n"
        "                                'content-type': 'application/json; charset=utf-8',\n"
        "                                'x-mailbox-key': key,\n"
        "                            })\n"
        "try:\n"
        "    urllib.request.urlopen(req, timeout=10).read()\n"
        "except Exception as e:\n"
        "    print('ack 失败', e, file=sys.stderr)\n",
        encoding="utf-8")

def main() -> int:
    (HOME / "plugins").mkdir(parents=True, exist_ok=True)
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    hub = subprocess.Popen(
        [sys.executable, str(SRC / "anp_hub.py"), "--host", "127.0.0.1",
         "--port", str(port), "--home", str(HOME)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(80):
        try:
            urllib.request.urlopen(base + "/health", timeout=1)
            break
        except Exception:  # noqa: BLE001
            time.sleep(0.25)

    pkey = peer_key(base, HOME)
    print(f"hub: {base}\nhome: {HOME}\npeer: {'ok' if pkey else 'FAIL'}\n")

    # 两个脚本都得在受信任目录内（#4.1a 之后 <home>/plugins 已是信任根）
    pd = HOME / "plugins"
    crash = pd / "d1_crash.py"
    ack = pd / "d1_ack.py"
    write_crash_script(crash)
    write_ack_script(ack, base)

    # ================= 实验组 =================
    print("=== 实验组：exec 脚本立刻 exit(1)（适配器崩在启动瞬间）===")
    post(base, "/heartbeat", {"name": "crash.guy",
                              "wake": {"kind": "exec",
                                       "command": [sys.executable, str(crash)]}})
    _, sent = None, post(base, "/messages/send",
                         {"from": "peer.one", "to": "crash.guy",
                          "body": "D1 实验信-崩", "wake": True}, pkey)
    print(f"    发信返回 wake: {json.dumps(sent.get('wake'), ensure_ascii=False)}")
    print("    （Popen 成功即 ok=true，但子进程立刻 exit(1)）")
    show("发信后即刻   ", snap(HOME, "crash.guy", "D1 实验信-崩"))

    # 等一轮投递扫描 + 退避窗
    print("    …等扫描与退避…")
    for i in range(8):
        time.sleep(1.5)
        s = snap(HOME, "crash.guy", "D1 实验信-崩")
        show(f"t={i*1.5+2.5:.1f}s   ", s)
        if (s.get("attempts") or 0) > 1:
            break

    final = snap(HOME, "crash.guy", "D1 实验信-崩")
    show("实验组终态   ", final)

    # ================= 对照组 =================
    print("\n=== 对照组：exec 脚本正常处理并 mail_ack ===")
    post(base, "/heartbeat", {"name": "ack.guy",
                              "wake": {"kind": "exec",
                                       "command": [sys.executable, str(ack)]}})
    post(base, "/messages/send", {"from": "peer.one", "to": "ack.guy",
                                  "body": "D1 对照信-ack", "wake": True}, pkey)
    show("发信后即刻   ", snap(HOME, "ack.guy", "D1 对照信-ack"))
    time.sleep(3.0)
    show("3s 后       ", snap(HOME, "ack.guy", "D1 对照信-ack"))
    time.sleep(4.0)
    ctrl_before = snap(HOME, "ack.guy", "D1 对照信-ack")
    show("7s 后（该已被 ack，不该再动）", ctrl_before)

    # ================= 判定 =================
    print("\n=== 判定（fix list §D1 的判据）===")
    delivered = final.get("delivered_at")
    attempts = final.get("attempts") or 0
    print(f"    实验组 delivered_at = {delivered!r}")
    print(f"    实验组 attempts     = {attempts}")
    print(f"    实验组 read         = {final.get('read')!r}")
    print(f"    对照组 delivered_at = {ctrl_before.get('delivered_at')!r}")
    print(f"    对照组 attempts     = {ctrl_before.get('attempts')!r}")
    print(f"    对照组 read_by     = {ctrl_before.get('read_by')!r}")

    verdict = []
    if delivered is not None:
        verdict.append("P0：delivered_at 被写入了（崩溃子进程也算投递完成）")
    verdict.append(f"attempts={attempts}（>1 说明有在重投，==1 说明一次后放弃）")
    if ctrl_before.get("delivered_at") is not None and efforts_ok(ctrl_before):
        verdict.append("对照组正常：ack 后 delivered_at 有值、不再重投")
    for v in verdict:
        print("      ·", v)

    print("\n结论见 fix list 判据："
          "\n    delivered_at != null 且 auto_redeliver 永不重投 → D1 实锤，进修复"
          "\n    否则 → D1 关闭，不动状态机")
    print(f"（本次证据：{'实锤' if (delivered is not None and attempts <= 1) else '需人工判读'}）")

    try:
        hub.kill()
    except Exception:  # noqa: BLE001
        pass
    shutil.rmtree(HOME, ignore_errors=True)
    return 0


def efforts_ok(s: dict) -> bool:
    return (s.get("attempts") or 0) <= 1


if __name__ == "__main__":
    sys.exit(main())
