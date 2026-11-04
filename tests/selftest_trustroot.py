"""#4.1a 验收：Adapter Plugin 的 exec 信任根。

核心问题只有一个：`<home>/plugins/<id>/` 里的 exec 唤醒脚本现在能不能过信任校验，
**同时**任意 `<home>/` 下的脚本仍然过不了。

四条硬验收（用户 2026-09-22 定）：
  1. 普通 exec Agent 仍正常（脚本在信箱安装目录内）
  2. Plugin 内代管 Agent 的 exec 正常（脚本在 `<home>/plugins/<id>/`）
  3. 越界脚本仍被拒（`<home>/` 根下，以及完全无关的目录）
  4. 真实链路：一个装在 plugins/ 下的 Adapter Plugin 用 exec wake 能被 Core 唤醒

安全边界上，反面断言和正面断言一样重要，所以 T3/T4 不许省。
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


# 一个被信任目录内的小脚本：把收到的 argv/环境落盘，用来证明『真的被执行了』。
# ★dump 路径走 argv，不走环境变量★：Core 的 wake() 不认测试自定义的 env，
# 第一版想当然设了个 ANP_ARGS_DUMP，结果子进程 open(None) 崩掉，
# 看着像『Core 没拉起子进程』——其实是自己的探针写错了，白查半天。
DUMP = (
    "import json, os, sys\n"
    "out = sys.argv[1]\n"
    "wj = os.environ.get('AGENT_ANP_WAKE_JSON') or '{}'\n"
    "rec = {\n"
    "    'argv': sys.argv,\n"
    "    'self': os.environ.get('AGENT_ANP_SELF'),\n"
    "    'has_key': bool(os.environ.get('AGENT_ANP_SELF_KEY')),\n"
    "    'msg_len': len(wj),\n"
    "    'msg_body': json.loads(wj).get('body'),\n"
    "}\n"
    "open(out, 'w', encoding='utf-8').write(json.dumps(rec, ensure_ascii=False))\n"
)


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="anp-trustroot-"))
    try:
        print("=== T0. install_roots 静态断言 ===")
        roots = [str(r) for r in core.install_roots()]
        say("T0.1 信箱安装目录仍在信任根里",
            any(str(SRC).lower() == r.lower() for r in roots), f"{len(roots)} 个根")
        say("T0.2 信任根里出现了 plugins 子树",
            any(r.lower().endswith("plugins") for r in roots), f"{roots}")
        say("T0.3 传 home 时用**那个** home 的 plugins，不是默认 home",
            any(str(tmp).lower() in str(r).lower() for r in core.install_roots(tmp)),
            str([r for r in core.install_roots(tmp) if str(tmp).lower() in str(r).lower()]))
        say("T0.4 ★信任根里没有 home 本身（只有它的 plugins/ 子树）★",
            not any(str(r).lower() == str(core.DEFAULT_HOME).lower()
                    for r in core.install_roots()))

        rig = Rig(tmp, "root")
        try:
            # ---------- 1. 安装目录内的 exec 仍正常 ----------
            print("\n=== T1. 普通 exec Agent（脚本在安装目录内）仍正常 ===")
            dump1 = tmp / "argv1.json"
            d1 = SRC / "_trust_probe1.py"
            d1.write_text(DUMP, encoding="utf-8")
            rig.post("/heartbeat", {"name": "exec.guy",
                                    "wake": {"kind": "exec",
                                             "command": [sys.executable, str(d1),
                                                         str(dump1)]}})
            ent = core.agent_table(rig.home).get("exec.guy") or {}
            ok1, why1 = core.check_wake_config(ent, rig.home)
            say("T1.1 安装目录内的 exec 通过信任校验", ok1, why1[:80])
            _, sent = rig.post("/messages/send",
                               {"from": "peer.one", "to": "exec.guy",
                                "body": "给安装目录内的脚本", "wake": True},
                               rig.peer_key())
            say("T1.2 唤醒返回成功", bool((sent.get("wake") or {}).get("ok")),
                json.dumps(sent.get("wake"), ensure_ascii=False)[:110])
            time.sleep(2.0)
            say("T1.3 子进程真的跑了并收到信封与自己的令牌", dump1.is_file(),
                (json.dumps({"self": json.loads(dump1.read_text(encoding="utf-8"))["self"],
                             "body": json.loads(dump1.read_text(encoding="utf-8"))["msg_body"]})
                 if dump1.is_file() else "dump 没生成"))
            d1.unlink()

            # ---------- 2. plugins/ 下的 exec 现在放行 ----------
            print("\n=== T2. 插件安装位置下的 exec 现在放行 ===")
            pdir = rig.home / "plugins" / "demo.one"
            pdir.mkdir(parents=True, exist_ok=True)
            dump2 = tmp / "argv2.json"
            d2 = pdir / "plugin.py"
            d2.write_text(DUMP, encoding="utf-8")
            rig.post("/heartbeat", {"name": "plug.guy",
                                    "wake": {"kind": "exec",
                                             "command": [sys.executable, str(d2),
                                                         str(dump2)]}})
            ent2 = core.agent_table(rig.home).get("plug.guy") or {}
            ok2, why2 = core.check_wake_config(ent2, rig.home)
            say("T2.1 插件目录内的 exec 通过信任校验", ok2, why2[:100])
            _, sent2 = rig.post("/messages/send",
                               {"from": "peer.one", "to": "plug.guy",
                                "body": "给插件里的脚本", "wake": True},
                               rig.peer_key())
            say("T2.2 唤醒返回成功", bool((sent2.get("wake") or {}).get("ok")),
                json.dumps(sent2.get("wake"), ensure_ascii=False)[:110])
            time.sleep(2.0)
            say("T2.3 子进程真的跑了", dump2.is_file())
            if dump2.is_file():
                rec = json.loads(dump2.read_text(encoding="utf-8"))
                say("T2.4 信封正文与令牌都对",
                    rec.get("msg_body") == "给插件里的脚本" and rec.get("has_key"),
                    json.dumps({"body": rec.get("msg_body"), "has_key": rec.get("has_key")}))

            # ---------- 3. 越界仍被拒 ----------
            print("\n=== T3. 越界脚本仍被拒（安全模型没被放宽）===")
            dump3 = tmp / "argv3.json"
            d3 = rig.home / "evil.py"          # home 根下：不是 plugins/ 子树
            d3.write_text(DUMP, encoding="utf-8")
            rig.post("/heartbeat", {"name": "evil.guy",
                                    "wake": {"kind": "exec",
                                             "command": [sys.executable, str(d3),
                                                         str(dump3)]}})
            ent3 = core.agent_table(rig.home).get("evil.guy") or {}
            ok3, why3 = core.check_wake_config(ent3, rig.home)
            say("T3.1 home 根下的脚本仍被拒", not ok3, why3[:100])
            _, sent3 = rig.post("/messages/send",
                               {"from": "peer.one", "to": "evil.guy",
                                "body": "给 home 根下的脚本", "wake": True},
                               rig.peer_key())
            say("T3.2 发信不执行它", not bool((sent3.get("wake") or {}).get("ok")),
                json.dumps(sent3.get("wake"), ensure_ascii=False)[:110])
            time.sleep(1.5)
            say("T3.3 子进程确实没有跑起来", not dump3.exists())

            dump4 = tmp / "argv4.json"
            far = tmp / "elsewhere"
            far.mkdir(parents=True, exist_ok=True)
            d4 = far / "probe.py"
            d4.write_text(DUMP, encoding="utf-8")
            rig.post("/heartbeat", {"name": "far.guy",
                                    "wake": {"kind": "exec",
                                             "command": [sys.executable, str(d4),
                                                         str(dump4)]}})
            ent4 = core.agent_table(rig.home).get("far.guy") or {}
            ok4, why4 = core.check_wake_config(ent4, rig.home)
            say("T3.4 完全无关的目录仍被拒", not ok4, why4[:90])

            # ---------- 4. http 档边界没被动过 ----------
            print("\n=== T4. http 档边界没被动过 ===")
            rig.post("/heartbeat", {"name": "ext.guy",
                                    "wake": {"kind": "http",
                                             "url": "http://8.8.8.8/wake"}})
            ent5 = core.agent_table(rig.home).get("ext.guy") or {}
            ok5, why5 = core.check_wake_config(ent5, rig.home)
            say("T4.1 非回环 http 唤醒仍被拒", not ok5, why5[:90])
            rig.post("/heartbeat", {"name": "lb.guy",
                                    "wake": {"kind": "http",
                                             "url": "http://127.0.0.1:9/wake"}})
            ent6 = core.agent_table(rig.home).get("lb.guy") or {}
            ok6, why6 = core.check_wake_config(ent6, rig.home)
            say("T4.2 回环 http 唤醒照旧放行", ok6, why6[:60])

            # ---------- 5. 真实链路 ----------
            print("\n=== T5. 真实链路：装在 plugins/ 下的 exec wake 被 Core 唤醒 ===")
            adir = rig.home / "plugins" / "anp.adapter.probe"
            adir.mkdir(parents=True, exist_ok=True)
            dump5 = tmp / "argv5.json"
            (adir / "plugin.py").write_text(DUMP, encoding="utf-8")
            rig.post("/heartbeat",
                     {"name": "probe-managed",
                      "wake": {"kind": "exec",
                               "command": [sys.executable, str(adir / "plugin.py"),
                                           str(dump5)]}})
            _, sent5 = rig.post("/messages/send",
                               {"from": "peer.one", "to": "probe-managed",
                                "body": "经插件 exec 唤醒", "wake": True},
                               rig.peer_key())
            say("T5.1 代管 agent 的 exec wake 通过校验并执行",
                bool((sent5.get("wake") or {}).get("ok")),
                json.dumps(sent5.get("wake"), ensure_ascii=False)[:110])
            time.sleep(2.0)
            say("T5.2 插件里的脚本真的被执行", dump5.is_file())
            if dump5.is_file():
                rec = json.loads(dump5.read_text(encoding="utf-8"))
                say("T5.3 拿到的是 probe-managed 的信封与令牌",
                    rec.get("self") == "probe-managed"
                    and rec.get("msg_body") == "经插件 exec 唤醒" and rec.get("has_key"),
                    json.dumps({"self": rec.get("self"),
                                "body": rec.get("msg_body")}))
        finally:
            rig.kill()

        print(f"\n===== #4.1a exec 信任根验收：{OK} 通过 / {FAIL} 失败 =====")
        if FAIL:
            print("有 FAIL：安全边界上反面断言和正面断言一样重要。")
        return 1 if FAIL else 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
