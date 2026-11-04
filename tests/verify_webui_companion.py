# -*- coding: utf-8 -*-
"""伴生 WebUI 链路四项端到端验证（独立 home，不动 live 服务）。

  1. 自动启动：hub 起 → 伴生看板起来（日志行 + 端口 + /health）
  2. 健康检查：看板 /health 报对服务名与 hub 地址
  3. 复用判定：第二个实例认出"已是本壳"，不重复拉起
  4. 退出联动：hub 死 → 伴生看板自己退
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
OK = FAIL = 0


def say(name: str, cond: bool, extra: str = "") -> None:
    global OK, FAIL
    OK += 1 if cond else 0
    FAIL += 0 if cond else 1
    print(f"  [{'OK  ' if cond else 'FAIL'}] {name}" + (f"  <- {extra}" if extra else ""))


def alive(pid: int) -> bool:
    return f'"{pid}"' in subprocess.run(
        ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV"],
        capture_output=True, text=True, encoding="utf-8", errors="replace").stdout


def wait_http(url: str, timeout: float = 20.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as r:
                return json.loads(r.read().decode("utf-8") or "{}")
        except Exception:  # noqa: BLE001
            time.sleep(0.5)
    return {}


def wait_file_contains(path: Path, needle: str, timeout: float = 20.0) -> str:
    """Wait for an async child process to flush a diagnostic line."""
    deadline = time.time() + timeout
    text = ""
    while time.time() < deadline:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
        if needle in text:
            return text
        time.sleep(0.2)
    return text


def port_pid(port: int) -> int:
    out = subprocess.run(["powershell", "-NoProfile", "-Command",
                          f"(Get-NetTCPConnection -State Listen -LocalPort {port} "
                          f"-ErrorAction SilentlyContinue).OwningProcess"],
                         capture_output=True, text=True, encoding="utf-8",
                         errors="replace").stdout.strip()
    try:
        return int(out)
    except ValueError:
        return 0


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="anp-companion-"))
    try:
        home = tmp / "home"
        (home / "plugins").mkdir(parents=True)
        hub_port, webui_port = 8981, 8982
        env = {**os.environ, "AGENT_ANP_HOME": str(home),
               "AGENT_ANP_WEBUI_PORT": str(webui_port),
               "AGENT_ANP_MAINTENANCE": "0",
               "PYTHONUTF8": "1",
               "PYTHONIOENCODING": "utf-8"}
        logf = open(tmp / "hub.log", "w", encoding="utf-8")
        hub = subprocess.Popen(
            [sys.executable, str(REPO / "src" / "anp_hub.py"), "--host", "127.0.0.1",
             "--port", str(hub_port), "--home", str(home)],
            stdout=logf, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, env=env)
        try:
            h = wait_http(f"http://127.0.0.1:{hub_port}/health", 30)
            say("0. hub 起来了", bool(h.get("ok")), f"port={hub_port}")

            # --- 1. 自动启动 ---
            deadline = time.time() + 40
            pid = 0
            while time.time() < deadline and not pid:
                pid = port_pid(webui_port)
                time.sleep(1)
            hub_log = wait_file_contains(tmp / "hub.log", "看板伴生：pid=", 20)
            say("1.1 伴生看板自动启动（端口被占上）", pid > 0, f"pid={pid}")
            say("1.2 hub 日志写明伴生关系",
                any("看板伴生：pid=" in line for line in hub_log.splitlines()),
                [line for line in hub_log.splitlines()
                 if "看板伴生：pid=" in line][:1])

            # --- 2. 健康检查 ---
            wh = wait_http(f"http://127.0.0.1:{webui_port}/health", 15)
            say("2.1 看板 /health 报服务名", wh.get("service") == "AgentNetPort-webui",
                json.dumps(wh, ensure_ascii=False)[:90])
            say("2.2 /health 指向正确的 hub",
                wh.get("hub") == f"http://127.0.0.1:{hub_port}", str(wh.get("hub")))
            say("2.3 /health 不含令牌", "adminToken" not in json.dumps(wh))

            # --- 3. 复用判定 ---
            probe_log = tmp / "reuse.log"
            with open(probe_log, "w", encoding="utf-8") as pl:
                p2 = subprocess.Popen(
                    [sys.executable, str(REPO / "webui" / "desktop.py"), "--no-browser",
                     "--port", str(webui_port), "--no-sync-adapters"],
                    stdout=pl, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, env=env)
                wait_file_contains(probe_log, "复用", 20)
                p2.terminate()
                try:
                    p2.wait(timeout=5)
                except Exception:  # noqa: BLE001
                    p2.kill()
            rtext = probe_log.read_text(encoding="utf-8", errors="replace")
            say("3.1 第二个实例认出已是本壳并复用",
                "复用" in rtext and "静态站就绪" not in rtext,
                [line for line in rtext.splitlines() if "复用" in line][:1])
            say("3.2 端口仍归原伴生（没有第二个监听者）",
                port_pid(webui_port) == pid, f"期望 {pid}，实际 {port_pid(webui_port)}")

            # --- 4. 退出联动 ---
            hub.kill()
            hub.wait(timeout=5)
            deadline = time.time() + 100
            while time.time() < deadline and alive(pid):
                time.sleep(2)
            say("4.1 hub 死后伴生看板自己退（不再留孤儿）", not alive(pid),
                f"pid={pid} 仍活着={alive(pid)}")
            say("4.2 端口随之释放", port_pid(webui_port) == 0)
        finally:
            try:
                hub.kill()
            except Exception:  # noqa: BLE001
                pass
            for stray in subprocess.run(
                    ["powershell", "-NoProfile", "-Command",
                     "@(Get-CimInstance Win32_Process | Where-Object { "
                     "$_.CommandLine -like '*desktop.py*' }).ProcessId"],
                    capture_output=True, text=True, encoding="utf-8",
                    errors="replace").stdout.split():
                pass
            logf.close()
        print(f"\n===== 伴生链路四项验证：{OK} 通过 / {FAIL} 失败 =====")
        return 1 if FAIL else 0
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
