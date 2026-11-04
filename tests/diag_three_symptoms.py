"""三条症状的取证：keys.json / plugin_load RemoteDisconnected / heartbeat 偶发断连。

原则：**只观测不改码**。每条给出：现象、频次、以及它属于
Core bug / 测试夹具 bug / Windows 时序 的判定证据。
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import statistics
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "src"
sys.path.insert(0, str(SRC))
import anp_core as core  # noqa: E402

OUT: list[str] = []


def say(s: str) -> None:
    OUT.append(s)
    print(s, flush=True)


def free() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class Probe:
    def __init__(self):
        self.home = Path(tempfile.mkdtemp(prefix="diag-")) / "home"
        (self.home / "plugins").mkdir(parents=True)
        self.port = free()
        self.base = f"http://127.0.0.1:{self.port}"
        self.proc: subprocess.Popen | None = None

    def start(self, env_extra: dict | None = None):
        # ★不用 text=True★：hub 的日志里可能有非 UTF-8 字节，
        # text=True 会在 reader thread 里抛 UnicodeDecodeError（本机栽过）
        self.proc = subprocess.Popen(
            [sys.executable, str(SRC / "anp_hub.py"), "--host", "127.0.0.1",
             "--port", str(self.port), "--home", str(self.home)],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            env={**os.environ, "AGENT_ANP_EVENT_SCAN_SEC": "1",
                 **(env_extra or {})})
        for _ in range(120):
            try:
                urllib.request.urlopen(self.base + "/health", timeout=0.5)
                return True
            except Exception:  # noqa: BLE001
                time.sleep(0.1)
        return False

    def req(self, method: str, path: str, body: dict | None = None,
            key: str = "", admin: str = "", timeout: float = 30.0):
        data = json.dumps(body or {}).encode("utf-8")
        r = urllib.request.Request(
            self.base + path, method=method, data=data if method == "POST" else None,
            headers={"content-type": "application/json; charset=utf-8",
                     **({"x-mailbox-key": key} if key else {}),
                     **({"x-anp-admin": admin} if admin else {})})
        try:
            with urllib.request.urlopen(r, timeout=timeout) as x:
                return x.status, x.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "replace")
        except Exception as e:  # noqa: BLE001
            return 0, f"{type(e).__name__}: {e}"

    def kill(self):
        if self.proc:
            try:
                self.proc.kill()
            except Exception:  # noqa: BLE001
                pass


def diag_heartbeat_flap():
    """症状三：heartbeat 偶发断连。并发 + 冷启动双角度取证。"""
    say("\n" + "=" * 70)
    say("③ heartbeat 偶发断连")
    say("=" * 70)
    p = Probe()
    ok = p.start()
    say(f"  hub 起来：{ok}")
    if not ok:
        return
    # 首次注册
    c, b = p.req("POST", "/heartbeat", {"name": "flap.one"})
    key = json.loads(b).get("key") or ""
    say(f"  首注册：{c} key={'有' if key else '无'}")
    kinds: Counter = Counter()
    lat: list[float] = []
    N = 120
    for i in range(N):
        t = time.time()
        c, b = p.req("POST", "/heartbeat", {"name": "flap.one"}, key=key)
        lat.append(time.time() - t)
        if c == 200:
            kinds["200"] += 1
        elif c == 0:
            kinds[f"传输失败:{b[:40]}"] += 1
        else:
            kinds[f"HTTP {c}"] += 1
        if i % 3 == 0:
            time.sleep(0.01)
    say(f"  {N} 次续约：{dict(kinds)}")
    say(f"  延迟：中位 {statistics.median(lat)*1000:.1f}ms "
        f"最大 {max(lat)*1000:.1f}ms")

    # 并发：多线程同时续约同一身份（Windows 上最容易露时序问题）
    import threading
    bad: Counter = Counter()
    lat2: list[float] = []

    def worker(n: int):
        for _ in range(15):
            t = time.time()
            try:
                c, _ = p.req("POST", "/heartbeat", {"name": f"c{n}.one"})
                lat2.append(time.time() - t)
                bad[f"{c}"] += 1
            except Exception as e:  # noqa: BLE001
                bad[f"{type(e).__name__}"] += 1

    ths = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    [t.start() for t in ths]
    [t.join() for t in ths]
    say(f"  8 线程 ×15 首注册：{dict(bad)}")
    say(f"  并发延迟：中位 {statistics.median(lat2)*1000 if lat2 else 0:.1f}ms "
        f"最大 {max(lat2)*1000 if lat2 else 0:.1f}ms")

    # 关键证据：冷启动窗口内（serve 先起、preload 未完成）就发请求
    p.kill()
    p2 = Probe()
    p2.proc = subprocess.Popen(
        [sys.executable, str(SRC / "anp_hub.py"), "--host", "127.0.0.1",
         "--port", str(p2.port), "--home", str(p2.home)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        env={**os.environ, "AGENT_ANP_EVENT_SCAN_SEC": "1"})
    got = []
    t0 = time.time()
    for _ in range(40):
        try:
            urllib.request.urlopen(p2.base + "/health", timeout=0.3)
            got.append(time.time() - t0)
            break
        except Exception:  # noqa: BLE001
            time.sleep(0.02)
    say(f"  冷启动 /health 首次可响应：{(got[0]*1000 if got else -1):.0f}ms"
        "（serve 先于 preload，所以此刻插件/账本可能还没就绪）")
    kinds2: Counter = Counter()
    for _ in range(20):
        c, b = p2.req("POST", "/heartbeat", {"name": "flap.one"})
        kinds2[f"{c}"] += 1
        if c != 200:
            kinds2[f"详情:{b[:60]}"] += 1
    say(f"  冷启动后立刻 20 次 heartbeat：{dict(kinds2)}")
    p2.kill()
    p.kill()


def diag_plugin_load():
    """症状二：plugin_load RemoteDisconnected。构造 url 型插件指向不可达端口。"""
    say("\n" + "=" * 70)
    say("② plugin_load RemoteDisconnected")
    say("=" * 70)
    p = Probe()
    if not p.start():
        return
    adm = core.admin_token(p.home)
    # 三类插件：不可达 url / 正常 command / 坏 manifest
    for pid, man in (
        ("dead.one", {"id": "dead.one", "name": "d", "version": "0.0.1",
                      "api_level": core.CORE_API_LEVEL,
                      "url": "http://127.0.0.1:1/"}),
        ("broke.one", None),
    ):
        d = p.home / "plugins" / pid
        d.mkdir(parents=True, exist_ok=True)
        if man is None:
            (d / "manifest.json").write_text('{"command":["python"],"url":"x"}',
                                             encoding="utf-8")
        else:
            (d / "manifest.json").write_text(json.dumps(man), encoding="utf-8")

    # 不可达 url：这正是当年触发 RemoteDisconnected 的那类
    for i in range(3):
        t = time.time()
        c, b = p.req("POST", "/plugins/dead.one/load", {}, admin=adm)
        say(f"  load dead.one#{i}：{c} {(time.time()-t)*1000:.0f}ms "
            f"{json.loads(b).get('code') if b.startswith('{') else b[:60]}")
    c, b = p.req("POST", "/plugins/broke.one/load", {}, admin=adm)
    say(f"  load broke.one：{c} "
        f"{json.loads(b).get('code') if b.startswith('{') else b[:60]}")
    # 反复 load 已加载的：会不会累积孤儿进程 / 占着锁
    c, b = p.req("POST", "/plugins/dead.one/load", {}, admin=adm)
    say(f"  重复 load 已加载的：{c}")
    alive = p.proc.poll() is None
    say(f"  hub 仍活着：{alive}")
    # 关健：load 期间并行打管理面，看是否被堵
    import threading
    delay = {"v": None}

    def hammer():
        t = time.time()
        c, _ = p.req("POST", "/agents", {}, admin=adm)
        delay["v"] = (time.time() - t) * 1000

    p.req("POST", "/plugins/dead.one/load", {}, admin=adm)   # 先加载一次
    th = threading.Thread(target=hammer)
    th.start()
    c, _ = p.req("POST", "/plugins/dead.one/load", {}, admin=adm)  # 再次 load（会握手）
    th.join()
    say(f"  load 期间并行 GET /agents 耗时：{delay['v']:.0f}ms"
        "（>1500ms 说明锁仍被出站 HTTP 占着）")
    p.kill()


def icacls_own_lines(path: Path) -> list[str]:
    """只取 icacls 输出里**描述了目标路径那一行**的 ACE 段。

    ★为什么不能整段做子串匹配★：中文 Windows 上 icacls 会追加本地化信息
    （"成功处理了 1 个文件"之类），整段搜 "Users"/"Everyone" 会把这些
    无关文本算进去，得出"收紧没生效"的假结论（本报告 ① 就栽过一次）。
    """
    out = subprocess.run(["icacls", str(path)], capture_output=True, timeout=15)
    raw = (out.stdout or b"") + (out.stderr or b"")
    for enc in ("gbk", "utf-8", "latin-1"):
        try:
            txt = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    # icacls 的格式：<路径> <ACE1> <ACE2>…  —— 取含路径那一行，切掉路径本身
    mine = ""
    for ln in txt.splitlines():
        if str(path) in ln:
            mine = ln.split(str(path), 1)[1]
            break
    return [t for t in mine.split() if t]


def diag_keys():
    """症状一：keys.json _tighten。POSIX 语义 vs Windows 实测。"""
    say("\n" + "=" * 70)
    say("① keys.json _tighten")
    say("=" * 70)
    p = Probe()
    if not p.start():
        return
    p.req("POST", "/heartbeat", {"name": "k.one"})
    kf = p.home / "keys.json"
    say(f"  keys.json 存在：{kf.is_file()}")
    say(f"  os.name = {os.name}  → _tighten 走 "
        f"{'icacls 分支' if os.name == 'nt' else 'chmod 分支'}")
    aces = icacls_own_lines(kf)
    say(f"  目标行的 ACE：{aces}")
    # 判定只看 ACE，不看 icacls 的本地化总结文本
    inherited = [a for a in aces if "(I)" in a or "(OI)" in a or "(CI)" in a]
    say(f"  ★继承 ACE 残留：{inherited or '无'} ← 有就是收紧没生效★")
    others = [a for a in aces
              if "everyone" in a.lower() or a.lower().startswith("builtin\\users")
              or "authenticated users" in a.lower()]
    say(f"  ★非本人 ACE：{others or '无'}★")
    # 收紧之后还得读写得动（剥了继承却 grant 失败 = 锁死自己）
    try:
        again = core.key_for("k.one", p.home)
        say(f"  ★收紧后仍能读回：{bool(again)} ★")
    except Exception as e:  # noqa: BLE001
        say(f"  ★收紧后读不回：{type(e).__name__}: {e} ★← 这就是 _tighten 锁死自己")
    for f in ("hub.secret", "admin.token"):
        fp = p.home / f
        if fp.is_file():
            say(f"  {f} 的 ACE：{icacls_own_lines(fp)}")
    p.kill()


def main() -> int:
    try:
        diag_keys()
        diag_plugin_load()
        diag_heartbeat_flap()
    finally:
        shutil.rmtree(Path(tempfile.gettempdir()) / "keep", ignore_errors=True)
    say("\n" + "=" * 70)
    report_path = Path(tempfile.gettempdir()) / "anp-diag.txt"
    report_path.write_text(
        "\n".join(OUT), encoding="utf-8")
    say(f"报告已存 {report_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
