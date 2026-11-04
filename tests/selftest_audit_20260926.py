# -*- coding: utf-8 -*-
"""2026-09-26 外部独立审计：逐项问题的回归门禁。

每一条都对应审计报告里的一个编号（A1/A2/A3…），也对应一次**可复现的实验**：
不只是"现在是对的"，而是"旧的错误形态现在必须被拒/被纠正"。
安全边界上反面断言和正面断言一样重要，所以本套件大量使用"必须拒绝"。
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
import anp_core as core      # noqa: E402
import anp_plugin as plugin  # noqa: E402

PY = sys.executable
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
    """一个临时 home 上的真 hub（独立进程，端口随机）。"""

    def __init__(self, tmp: Path, name: str):
        self.home = tmp / f"home_{name}"
        (self.home / "plugins").mkdir(parents=True, exist_ok=True)
        self.port = free_port()
        self.base = f"http://127.0.0.1:{self.port}"
        self.log = tmp / f"hub_{name}.log"
        self._logfp = open(self.log, "w", encoding="utf-8")
        self.proc = subprocess.Popen(
            [PY, str(SRC / "anp_hub.py"), "--host", "127.0.0.1",
             "--port", str(self.port), "--home", str(self.home)],
            stdout=self._logfp, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
            env={**os.environ, "AGENT_ANP_EVENT_SCAN_SEC": "1",
                 "AGENT_ANP_MAINTENANCE": "0", "AGENT_ANP_AUTO_REDELIVER": "0"})
        for _ in range(120):
            try:
                urllib.request.urlopen(self.base + "/health", timeout=1)
                break
            except Exception:  # noqa: BLE001
                time.sleep(0.25)

    def post(self, path: str, body: dict, key: str = "", raw_len: str | None = None,
             timeout: float = 20.0) -> tuple[int, dict]:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers = {"content-type": "application/json; charset=utf-8",
                   **({"x-mailbox-key": key} if key else {})}
        req = urllib.request.Request(self.base + path, method="POST", data=data, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status, json.loads(r.read().decode("utf-8") or "{}")
        except urllib.error.HTTPError as e:
            try:
                return e.code, json.loads(e.read().decode("utf-8") or "{}")
            except json.JSONDecodeError:
                return e.code, {}
        except Exception as e:  # noqa: BLE001
            return 0, {"error": f"{type(e).__name__}: {e}"}

    def raw(self, payload: bytes, wait: float = 3.0) -> bytes:
        s = socket.create_connection(("127.0.0.1", self.port), timeout=wait)
        s.sendall(payload)
        out = b""
        try:
            while True:
                chunk = s.recv(65536)
                if not chunk:
                    break
                out += chunk
        except TimeoutError:
            out += b"<<TIMEOUT>>"
        s.close()
        return out

    def key(self, name: str) -> str:
        _, r = self.post("/heartbeat", {"name": name})
        return str(r.get("key") or "")

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
        try:
            self._logfp.close()
        except Exception:  # noqa: BLE001
            pass


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="anp-audit-"))
    try:
        # =================================================================== #
        print("=== A1. exec 信任根：真正被执行的脚本必须在安装目录内 ===")
        # =================================================================== #
        home = tmp / "home_exec"
        (home / "plugins" / "x.one").mkdir(parents=True)
        (home / "plugins" / "x.one" / "p.py").write_text("print('p')\n", encoding="utf-8")
        (home / "evil.py").write_text("print('e')\n", encoding="utf-8")
        (tmp / "far").mkdir()
        (tmp / "far" / "p.py").write_text("print('f')\n", encoding="utf-8")
        trusted = SRC / "_audit_trusted.py"
        trusted.write_text("print('t')\n", encoding="utf-8")
        P = str(home / "plugins" / "x.one")

        def ex(command, cwd=""):
            cfg = {"kind": "exec", "command": command}
            if cwd:
                cfg["cwd"] = cwd
            return core.check_wake_config({"wake": cfg}, home)

        ok, why = ex([PY, "-c", "import os;os.system('calc')", str(trusted)])
        say("A1.1 python -c + 尾随可信路径 → 拒", not ok, why[:70])
        ok, _ = ex([PY, "-m", "http.server", str(trusted)])
        say("A1.2 python -m + 尾随可信路径 → 拒", not ok)
        ok, _ = ex([PY, "-c", "print(1)", str(SRC / "__nope__.py")])
        say("A1.3 尾随**不存在**的可信路径 → 拒", not ok)
        ok, why = ex([PY, "_audit_trusted.py"], cwd=str(tmp))
        say("A1.4 相对脚本按 wake.cwd 解析（不再按 hub cwd 猜）", not ok, why[:60])
        ok, _ = ex([str(tmp / "evil.exe"), str(trusted)])
        say("A1.5 ★报告外★argv[0] 是任意可执行文件 → 拒", not ok)
        ok, _ = ex(["totally-unknown-cmd", str(trusted)])
        say("A1.6 ★报告外★argv[0] 是被 PATH 解析的未知命令 → 拒", not ok)
        ok, _ = ex(["node", "--require", str(trusted), str(trusted)])
        say("A1.7 ★报告外★按路径再加载一份代码（--require）→ 拒", not ok)
        ok, _ = ex([PY, P + "/p.py"])
        say("A1.8 标准 exec（plugins/ 内脚本）仍放行", ok)
        ok, _ = ex([PY, P + "/p.py", str(tmp / "dump.json")])
        say("A1.9 脚本 + 数据参数（G1 的 dump.json）仍放行", ok)
        ok, _ = ex([PY, P + "/p.py", "--wake-exec", "--hub", "http://127.0.0.1:9001"])
        say("A1.10 脚本 + --hub URL（demo adapter）仍放行", ok)
        ok, _ = ex([P + "/run.bat".replace("run.bat", "p.py")])
        say("A1.11 argv[0] 本身在安装目录内 → 放行", ok)
        ok, _ = ex([PY, "p.py"], cwd=P)
        say("A1.12 相对脚本 + cwd 在信任根内 → 放行", ok)
        ok, _ = ex([PY, str(home / "evil.py")])
        say("A1.13 home 根下的脚本仍被拒", not ok)
        ok, _ = ex([PY, str(tmp / "far" / "p.py")])
        say("A1.14 完全无关目录的脚本仍被拒", not ok)
        ok, _ = ex([PY, P + "/p.py", str(tmp / "far" / "p.py")])
        say("A1.15 脚本可信但另带一份越界代码 → 拒", not ok)
        trusted.unlink()

        # =================================================================== #
        print("\n=== A2/A3. 身份名与关联线 id：清洗必须单射 ===")
        # =================================================================== #
        say("A2.1 a/b 与 ab 不再共用文件名",
            core._safe("a/b") != core._safe("ab"),
            f"a/b→{core._safe('a/b')} ab→{core._safe('ab')}")
        say("A2.2 a:b 与 ab 不再共用文件名",
            core._safe("a:b") != core._safe("ab"))
        say("A2.3 常用名（含点号）输出不变（老收件箱照旧命中）",
            core._safe("anp.adapter.dsh") == "anp.adapter.dsh"
            and core._safe("codex") == "codex")
        say("A2.4 _unsafe 是 _safe 的逆",
            all(core._unsafe(core._safe(n)) == n
                for n in ("a/b", "a:b", "ab", "a.b.c", "测试/身份", "a%b")),
            ", ".join(core._safe(n) for n in ("a/b", "a:b", "ab")))
        core.send("carol", "a/b", "给斜杠身份", home=home)
        core.send("carol", "ab", "给普通身份", home=home)
        rows = core.read_records("ab", home)
        say("A2.5 ab 读不到 a/b 的信（数据隔离恢复）", len(rows) == 1,
            f"读到 {len(rows)} 封")
        rows2 = core.read_records("a/b", home)
        say("A2.6 a/b 读得到自己的信且与 ab 互不串", len(rows2) == 1,
            f"读到 {len(rows2)} 封")

        say("A3.1 journal_path('game/1') != journal_path('game1')",
            core.journal_path("game/1", home) != core.journal_path("game1", home))
        core.send("alice", "bob", "m1", correlation_id="game/1", home=home)
        core.send("carol", "dave", "m2", correlation_id="game1", home=home)
        say("A3.2 两条关联线各自记自己的 owner",
            core.journal_owner("game1", home) == "carol"
            and core.journal_owner("game/1", home) == "alice",
            f"game1→{core.journal_owner('game1', home)} game/1→{core.journal_owner('game/1', home)}")
        say("A3.3 越权读仍被拒（owner 之外）",
            core.journal_owner("game/1", home) != "carol")
        say("A3.4 known_inboxes 还原原始身份名",
            "a/b" in core.known_inboxes(home), str(sorted(core.known_inboxes(home))))

        # Hub 注册侧：带编码字符集之外字符的名字要在注册时就被拒（不等落盘后才猜）
        rig3 = Rig(tmp, "regname")
        try:
            code, r = rig3.post("/heartbeat", {"name": "bad/name"})
            say("A3.5 Hub 注册拒绝含 / 的身份名（审计第 2 条的后半）",
                code == 400, f"{code} {str(r.get('error'))[:60]}")
            code, r = rig3.post("/heartbeat", {"name": "ok.name"})
            say("A3.6 合法身份名照旧注册", code == 200 and bool(r.get("key")), str(code))
        finally:
            rig3.kill()

        # =================================================================== #
        print("\n=== A4. 并发 compact_ledger 不许抹掉 heartbeat ===")
        # =================================================================== #
        home2 = tmp / "home_race"
        home2.mkdir(parents=True, exist_ok=True)
        for i in range(30):
            core.heartbeat(f"agent{i}", home2, note=str(i))
        core.heartbeat("racer", home2, note="old")
        started = threading.Event()
        orig_read = core.read_ledger

        def slow_read(h=core.DEFAULT_HOME):
            rows = orig_read(h)
            started.set()
            time.sleep(0.8)
            return rows

        core.read_ledger = slow_read
        th = threading.Thread(target=lambda: core.compact_ledger(home2, keep=10), daemon=True)
        th.start()
        started.wait(2.0)
        core.heartbeat("racer", home2, note="NEW")     # 读完之后、替换之前落的新心跳
        core.read_ledger = orig_read
        th.join(10)
        row = core.agent_table(home2).get("racer") or {}
        say("A4.1 压缩期间落的新心跳不再被抹掉", row.get("note") == "NEW",
            f"note={row.get('note')!r}")
        out = core.compact_ledger(home2, keep=10)
        say("A4.2 正常压缩仍按语义工作（每身份留最后一条）",
            out.get("ok") and len(out.get("names") or []) == 31, str(len(out.get("names") or [])))

        # =================================================================== #
        print("\n=== A5-A9. HTTP 边界 / 日志脱敏 / 权限执行（在线）===")
        # =================================================================== #
        rig = Rig(tmp, "http")
        try:
            k_call = rig.key("caller.one")
            bad_len = rig.raw(b"POST /messages/send HTTP/1.1\r\nHost: x\r\n"
                              b"Content-Length: abc\r\n\r\n{}")
            say("A5.1 非数字 content-length → 结构化 400（不是连接重置）",
                b" 400 " in bad_len, bad_len[:60].decode("utf-8", "replace"))
            neg = rig.raw(b"POST /messages/send HTTP/1.1\r\nHost: x\r\n"
                          b"Content-Length: -1\r\n\r\n", wait=4.0)
            say("A5.2 负 content-length → 结构化 400（不再挂起等 EOF）",
                b" 400 " in neg, neg[:60].decode("utf-8", "replace"))
            big = rig.raw(b"POST /messages/send HTTP/1.1\r\nHost: x\r\n"
                          b"Content-Length: 999999999\r\n\r\n{}")
            say("A5.3 超大 content-length → 400（不读进内存）",
                b" 400 " in big, big[:60].decode("utf-8", "replace"))

            # A6. 长轮询名额
            health = json.loads(urllib.request.urlopen(rig.base + "/health", timeout=5)
                               .read().decode("utf-8"))
            say("A6.1 /health 暴露长轮询名额（可观测）",
                isinstance(health.get("wait"), dict) and health["wait"].get("max", 0) >= 1,
                json.dumps(health.get("wait"), ensure_ascii=False))
            rig.post("/heartbeat", {"name": "quiet.guy",
                                    "wake": {"kind": "queue"}})
            results: list[int] = []
            lock = threading.Lock()

            qk = core.key_for("quiet.guy", rig.home)

            def waiter():
                c, _b = rig.post("/tool", {"server": "quiet.guy", "name": "mail_wait",
                                           "args": {"timeout_sec": 3}}, qk)
                with lock:
                    results.append(c)

            threads = [threading.Thread(target=waiter, daemon=True) for _ in range(3)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(20)
            say("A6.2 少量并发长轮询不受影响（不误伤）",
                all(c == 200 for c in results), str(results))

            # A6b. 名额真的会挡（不是只写在文档里）：另起一个 max=2 的 hub
            cap_home = tmp / "home_waitcap"
            (cap_home / "plugins").mkdir(parents=True)
            cap_port = free_port()
            with open(tmp / "hub_waitcap.log", "w", encoding="utf-8") as lf:
                cap = subprocess.Popen(
                    [PY, str(SRC / "anp_hub.py"), "--host", "127.0.0.1",
                     "--port", str(cap_port), "--home", str(cap_home)],
                    stdout=lf, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                    env={**os.environ, "AGENT_ANP_WAIT_MAX": "2", "AGENT_ANP_WEBUI": "0",
                         "AGENT_ANP_MAINTENANCE": "0"})
            cap_base = f"http://127.0.0.1:{cap_port}"
            for _ in range(120):
                try:
                    urllib.request.urlopen(cap_base + "/health", timeout=1)
                    break
                except Exception:  # noqa: BLE001
                    time.sleep(0.25)
            try:
                hb = json.dumps({"name": "cap.guy",
                                 "wake": {"kind": "queue"}}).encode()
                req = urllib.request.Request(cap_base + "/heartbeat", method="POST",
                                             data=hb,
                                             headers={"content-type": "application/json"})
                ck = str(json.loads(urllib.request.urlopen(req, timeout=10)
                                    .read().decode("utf-8")).get("key") or "")
                outcomes: list[str] = []
                lock2 = threading.Lock()

                def _wait_one() -> None:
                    payload = json.dumps({"server": "cap.guy", "name": "mail_wait",
                                          "args": {"timeout_sec": 5}}).encode()
                    rq = urllib.request.Request(
                        cap_base + "/tool", method="POST", data=payload,
                        headers={"content-type": "application/json",
                                 "x-mailbox-key": ck})
                    try:
                        with urllib.request.urlopen(rq, timeout=25) as resp:
                            b = json.loads(resp.read().decode("utf-8") or "{}")
                        tag = "wait.busy" if b.get("code") == "wait.busy" else "served"
                    except Exception as e:  # noqa: BLE001
                        tag = f"err:{type(e).__name__}"
                    with lock2:
                        outcomes.append(tag)

                ts = [threading.Thread(target=_wait_one, daemon=True) for _ in range(5)]
                for t in ts:
                    t.start()
                for t in ts:
                    t.join(30)
                say("A6.3 超过名额的长轮询被挡（wait.busy），不再无限堆线程",
                    outcomes.count("wait.busy") == 3 and outcomes.count("served") == 2,
                    str(sorted(outcomes)))
            finally:
                cap.terminate()
                try:
                    cap.wait(timeout=5)
                except Exception:  # noqa: BLE001
                    cap.kill()

            # A7. 令牌不进访问日志
            try:
                urllib.request.urlopen(rig.base + "/sessions?token=SECRET-TOKEN-XYZ", timeout=5)
            except urllib.error.HTTPError:
                pass
            time.sleep(1.2)
            rig.proc.terminate()
            try:
                rig.proc.wait(timeout=5)
            except Exception:  # noqa: BLE001
                rig.proc.kill()
            log_text = rig.log.read_text(encoding="utf-8", errors="replace")
            say("A7.1 hub.log 里不再出现管理员令牌明文",
                "SECRET-TOKEN-XYZ" not in log_text,
                [line for line in log_text.splitlines() if "sessions?token" in line][:1])
            say("A7.2 但请求行本身仍然可读（只是打码）",
                "sessions?token=***" in log_text or "/sessions?" in log_text)
        finally:
            rig.kill()

        # A8. 插件权限执行
        rig2 = Rig(tmp, "perm")
        try:
            admin = str(core.admin_token(rig2.home))
            # 一个什么都不声明的插件（真进程 + 真握手）
            pd = rig2.home / "plugins" / "noperm.one"
            pd.mkdir(parents=True, exist_ok=True)
            (pd / "manifest.json").write_text(json.dumps({
                "id": "noperm.one", "name": "NoPerm", "version": "0.1.0", "api_level": 1,
                "url": "http://127.0.0.1:1/", "permissions": []}), encoding="utf-8")
            code, _ = rig2.post("/plugins/noperm.one/load", {}, admin)
            say("A8.0 无权限插件能加载（声明本身合法）", code in (200, 409), str(code))
            # 它的"贡献"直接写进注册表：模拟 register 后的状态（端点不可达不影响工具表）
            reg = plugin.read_registry(rig2.home)
            if "noperm.one" in reg:
                reg["noperm.one"]["contributions"] = {"tools": [
                    {"name": "sneak", "description": "偷跑的工具",
                     "input_schema": {"type": "object"}}]}
                plugin.write_registry(reg, rig2.home)
            reg_tools = plugin.tool_registry(rig2.home)
            say("A8.1 ★无 tools.register 的工具进不了 agent 可见 API★",
                "noperm_one_sneak" not in reg_tools, str(sorted(reg_tools)))
            k_plug = core.key_for("noperm.one", rig2.home)
            code, r = rig2.post("/tool", {"server": "noperm.one", "name": "mail_agents",
                                          "args": {}}, k_plug)
            say("A8.2 无 agents.discover → permission.denied",
                r.get("ok") is False and r.get("code") == "permission.denied",
                json.dumps(r, ensure_ascii=False)[:90])
            code, r = rig2.post("/messages/send",
                                {"from": "noperm.one", "to": "caller.one",
                                 "body": "唤起你", "wake": True}, k_plug)
            say("A8.3 无 wake.others → wake=true 被拒",
                r.get("code") == "permission.denied" and r.get("permission") == "wake.others",
                json.dumps(r, ensure_ascii=False)[:90])
            code, r = rig2.post("/messages/send",
                                {"from": "noperm.one", "to": ["caller.one", "quiet.guy"],
                                 "body": "广播", "wake": False}, k_plug)
            say("A8.4 无 broadcast → 多收件人被拒",
                r.get("code") == "permission.denied" and r.get("permission") == "broadcast",
                json.dumps(r, ensure_ascii=False)[:90])
            code, r = rig2.post("/messages/send",
                                {"from": "noperm.one", "to": "caller.one",
                                 "body": "不唤起", "wake": False}, k_plug)
            say("A8.5 wake=false 单发不受影响（不是一律封死）", r.get("ok") is True,
                json.dumps(r, ensure_ascii=False)[:80])
            k_call = rig2.key("caller.one")
            code, r = rig2.post("/messages/send",
                                {"from": "caller.one", "to": "quiet.guy",
                                 "body": "普通身份唤起", "wake": True}, k_call)
            say("A8.6 普通身份（非插件）不受插件权限影响", r.get("ok") is True,
                json.dumps(r, ensure_ascii=False)[:80])
            code, r = rig2.post("/tool", {"server": "caller.one", "name": "mail_agents",
                                          "args": {}}, k_call)
            say("A8.7 普通身份仍可枚举名册", r.get("ok") is True)
        finally:
            rig2.kill()

        # =================================================================== #
        print("\n=== A10. 默认端口契约 ====")
        # =================================================================== #
        hub_src_txt = (SRC / "anp_hub.py").read_text(encoding="utf-8")
        say("A10.1 hub 的 argparse 默认端口就是 8900（与 README/SDK/DEFAULT_HUB 一致）",
            'default=int(os.environ.get("AGENT_ANP_PORT") or 8900)' in hub_src_txt)
        say("A10.2 core.DEFAULT_HUB 仍是 8900", core.DEFAULT_HUB.endswith(":8900"), core.DEFAULT_HUB)
        say("A10.3 spawn_hub 的回落端口也是 8900",
            "8900" in (SRC / "anp_core.py").read_text(encoding="utf-8").split(
                "port = parsed.port or")[1].split("\n")[0])
        readme = (REPO / "README.md").read_text(encoding="utf-8")
        say("A10.4 README 仍写 8900（契约只有一个默认值）", "--port 8900" in readme)

        # =================================================================== #
        print("\n=== A11. 伴生 WebUI 指向真实实现 ===")
        # =================================================================== #
        hub_src = (SRC / "anp_hub.py").read_text(encoding="utf-8")
        say("A11.1 代码里不再找不存在的 src/mailbox_webui.py",
            'parent / "mailbox_webui.py"' not in hub_src
            and 'parents[1] / "webui" / "desktop.py"' in hub_src)
        say("A11.2 伴生脚本指向 webui/desktop.py",
            'parents[1] / "webui" / "desktop.py"' in hub_src)
        say("A11.3 desktop.py 真实存在", (REPO / "webui" / "desktop.py").is_file())
        shell = (REPO / "webui" / "desktop.py").read_text(encoding="utf-8")
        say("A11.4 伴生时禁掉浏览器与 adapter 同步（不自伤）",
            "--no-browser" in shell and "--no-sync-adapters" in hub_src)

        # A11.5 ★hub 死后伴生看板必须自己退★（接线时实测踩到的孤儿问题）
        shell_has_watchdog = ("_hub_watchdog" in shell
                              and "ANP_WEBUI_COMPANION" in shell)
        hub_sets_flag = '"ANP_WEBUI_COMPANION": "1"' in hub_src
        say("A11.5 看板带 hub 看门狗（不留孤儿进程）",
            shell_has_watchdog and hub_sets_flag,
            f"desktop.py 看门狗={shell_has_watchdog} hub 注入开关={hub_sets_flag}")

        # =================================================================== #
        print("\n=== A12. 发布流程：包得起来、验得过、边界可验证 ===")
        # =================================================================== #
        pkg = REPO / "tools" / "package_release.py"
        say("A12.1 tools/package_release.py 存在", pkg.is_file())
        arc = tmp / "rel.zip"
        r = subprocess.run([PY, str(pkg), "build", "-o", str(arc)],
                          capture_output=True, text=True, encoding="utf-8")
        say("A12.2 build 成功", r.returncode == 0, r.stdout.strip()[:100])
        r = subprocess.run([PY, str(pkg), "verify", str(arc)],
                          capture_output=True, text=True, encoding="utf-8")
        say("A12.3 verify 成功", r.returncode == 0, r.stdout.strip()[:100])
        with zipfile.ZipFile(arc) as zf:
            names = zf.namelist()
        say("A12.4 包里含 adapters/ plugins/ webui/ sdk/",
            any("/adapters/" in n for n in names) and any("/plugins/" in n for n in names)
            and any("/webui/" in n for n in names) and any("/sdk/" in n for n in names))
        leak = [n for n in names if any(k in n for k in
                ("admin.token", "hub.secret", "keys.json", ".pyc", "ledger",
                 "verify-result", "data/", "logs/"))]
        say("A12.5 包里没有密钥/账本/缓存/日志", not leak, str(leak[:3]))
        gitignore = (REPO / ".gitignore")
        say("A12.6 仓库根有 .gitignore（发布边界可声明）", gitignore.is_file())

        # =================================================================== #
        print("\n=== A12b. 安装器：路径穿越挡住 + 退出码讲实话 ===")
        # =================================================================== #
        import shutil as _shutil

        ihome = tmp / "home_install"
        (ihome / "plugins").mkdir(parents=True)
        (ihome / "victim").mkdir()
        (ihome / "victim" / "precious.txt").write_text("别删我", encoding="utf-8")
        (ihome / "victim" / "keepdir").mkdir()
        evil = REPO / "adapters" / "zz-audit-probe"
        evil.mkdir(parents=True, exist_ok=True)
        (evil / "manifest.json").write_text(json.dumps({
            "id": "..\\victim", "name": "穿", "version": "0.0.1", "api_level": 1,
            "command": ["python", "plugin.py"]}, ensure_ascii=False), encoding="utf-8")
        (evil / "plugin.py").write_text("print('evil')\n", encoding="utf-8")
        good = REPO / "adapters" / "zz.audit.ok"
        good.mkdir(parents=True, exist_ok=True)
        (good / "manifest.json").write_text(json.dumps({
            "id": "zz.audit.ok", "name": "ok", "version": "0.0.1", "api_level": 1,
            "command": ["python", "plugin.py"]}, ensure_ascii=False), encoding="utf-8")
        (good / "plugin.py").write_text("print('ok')\n", encoding="utf-8")
        try:
            r = subprocess.run([PY, str(REPO / "tools" / "install_adapters.py"),
                                "--home", str(ihome), "--only", "zz-audit-probe", "--quiet"],
                               capture_output=True, text=True, encoding="utf-8", errors="replace")
            say("A12b.1 恶意 manifest（id 带路径分隔符）被 Core 校验挡下",
                r.returncode != 0 and "manifest 不合法" in (r.stderr or ""),
                (r.stderr or "").strip()[:90])
            say("A12b.2 目标目录内容没被删/覆盖",
                (ihome / "victim" / "precious.txt").is_file()
                and (ihome / "victim" / "keepdir").is_dir())
            r2 = subprocess.run([PY, str(REPO / "tools" / "install_adapters.py"),
                                 "--home", str(ihome), "--only", "zz.audit.ok",
                                 "--hub", "http://127.0.0.1:1", "--quiet"],
                                capture_output=True, text=True, encoding="utf-8", errors="replace")
            say("A12b.3 Hub 连不上 → 非零退出（不再假成功）", r2.returncode != 0,
                f"exit={r2.returncode}")
            r3 = subprocess.run([PY, str(REPO / "tools" / "install_adapters.py"),
                                 "--home", str(ihome), "--only", "zz.audit.ok",
                                 "--no-load", "--quiet"],
                                capture_output=True, text=True, encoding="utf-8", errors="replace")
            say("A12b.4 --no-load 且拷贝成功 → 0（真成功的形状）",
                r3.returncode == 0 and (ihome / "plugins" / "zz.audit.ok").is_dir(),
                f"exit={r3.returncode}")
        finally:
            _shutil.rmtree(evil, ignore_errors=True)
            _shutil.rmtree(good, ignore_errors=True)

        # =================================================================== #
        print("\n=== A13. WebUI 不把管理员令牌送出本机 ===")
        # =================================================================== #
        sys.path.insert(0, str(REPO / "webui"))
        import desktop  # noqa: E402

        say("A13.1 非回环 hub 不发本地令牌",
            desktop.admin_token_works("http://example.invalid", "whatever") is False)
        say("A13.2 回环判断本身仍工作（防 SSRF 的那层还在）",
            desktop._is_local_url("http://127.0.0.1:8900") is True
            and desktop._is_local_url("http://8.8.8.8") is False)
        handler = desktop.QuietHandler.__new__(desktop.QuietHandler)
        handler.client_address = ("192.168.1.50", 5000)
        say("A13.3 非回环客户端拿不到 /_anp/session",
            handler._client_is_loopback() is False)
        handler.client_address = ("127.0.0.1", 5000)
        say("A13.4 本机客户端仍能自动登录", handler._client_is_loopback() is True)

        # =================================================================== #
        print("\n=== A14. 报告外的两个契约 bug ===")
        # =================================================================== #
        say("A14.1 /plugin/register 不再回 \"auth\": \"admin\"",
            '"auth": "plugin-credential"' in hub_src)
        say("A14.2 mail_inbox 的 newest 真的倒序",
            "reversed(picked[-limit:])" in hub_src)
        print(f"\n===== 2026-09-26 审计回归门禁：{OK} 通过 / {FAIL} 失败 =====")
        if FAIL:
            print("有 FAIL：逐条看上面标红的那一项。")
        return 1 if FAIL else 0
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
