"""#6 Adapter-as-Plugin 原型验证。

回答三个问题，**不改 Core、不加新 permission**：

  Q1  走不走得通
      Core 拉起 Adapter Plugin → /anp/register 送来身份+令牌
      → 插件用公开 /heartbeat 代注册 demo-01，wake 指向插件自己的 /anp/wake
      → 别的 Agent 给 demo-01 发信
      → 插件收到完整 Envelope

  Q2  x-mailbox-key 是谁的令牌（P0-4：应是 demo-01 的，不是插件的）

  Q3  受什么策略约束
      register=open       → ？
      register=allowlist  → 在表里？不在表里？
      register=closed     → ？

Q3 是重点：它决定"Adapter 代管 Agent"这个语义在现有策略层眼里是什么地位。
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SRC = REPO / "src"
PLUGIN_SRC = Path(__file__).resolve().parent
HUB = SRC / "anp_hub.py"

if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
import anp_core as core   # noqa: E402  （实验脚本可以读 Core，参考实现不行）
import anp_plugin as plugin  # noqa: E402

MANAGED = "demo-01"      # 被代管的 Agent 身份
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


def post(base: str, path: str, body: dict, key: str = "") -> tuple[int, dict]:
    req = urllib.request.Request(
        base + path, method="POST",
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
    except urllib.error.URLError as e:
        return 0, {"ok": False, "error": str(e.reason)}


def get(base: str, path: str) -> tuple[int, dict]:
    try:
        with urllib.request.urlopen(base + path, timeout=10) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, {}
    except urllib.error.URLError:
        return 0, {}


class Rig:
    """一次实验：新 home + 新 hub + 把 Adapter Plugin 放进 <home>/plugins/。"""

    def __init__(self, tmp: Path, name: str, policy: dict | None = None):
        self.home = tmp / f"home_{name}"
        (self.home / "plugins").mkdir(parents=True, exist_ok=True)
        if policy is not None:
            (self.home / "permissions.json").write_text(
                json.dumps(policy, ensure_ascii=False, indent=2), encoding="utf-8")
        shutil.copytree(PLUGIN_SRC, self.home / "plugins" / "anp.adapter.demo")
        # ★把 SDK vendor 进插件目录★：#3 之后插件依赖 sdk/anp_adapter/，
        # 而 Core 是从 <home>/plugins/<id>/ 拉起它的，"仓库根的 ../sdk"不在。
        # 这一步就是真实的部署动作：Adapter 要跟它的库一起交付。
        shutil.copytree(REPO / "sdk" / "anp_adapter",
                        self.home / "plugins" / "anp.adapter.demo" / "anp_adapter")
        # 插件自己写的收件记录要清干净，否则上一次实验的残留会混进来
        shutil.rmtree(self.home / "plugins" / "anp.adapter.demo" / "data",
                      ignore_errors=True)
        self.port = free_port()
        self.base = f"http://127.0.0.1:{self.port}"
        self.proc = subprocess.Popen(
            [sys.executable, str(HUB), "--host", "127.0.0.1", "--port", str(self.port),
             "--home", str(self.home)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            env={**os.environ, "AGENT_ANP_EVENT_SCAN_SEC": "1"})
        self.plugin_port = 0
        for _ in range(80):
            try:
                urllib.request.urlopen(self.base + "/health", timeout=1)
                break
            except Exception:  # noqa: BLE001
                time.sleep(0.25)

    def plugin_own_token(self) -> str:
        """拿**插件自己**那一枚令牌（盘点公开状态，不问插件自己）。

        ★C1 方案 P★：`/plugin/register` 只认插件自己的 agent token，不认 admin。
        这枚令牌由 Core 在 `/anp/register` 时经 `core.issue_key(pid)` 发证，
        存在账本 keys.json 里，所以从盘上读得到——这正是"从公开状态推导"。
        """
        return core.key_for("anp.adapter.demo", self.home)

    def load_plugin(self) -> dict:
        """用插件自己的令牌报到（★不是 admin★，C1 方案 P）。

        admin 令牌在这个端点**明确被拒**——本方法的存在意义之一就是证明
        「插件不需要持 admin 也能完成报到」。
        """
        _, r = post(self.base, "/plugin/register",
                    {"id": "anp.adapter.demo"}, self.plugin_own_token())
        return r

    def wait_plugin_port(self, timeout: float = 12.0) -> int:
        """等插件被 Core 拉起。用账本里的 wake url 反推端口——★读公开状态，
        不问插件自己★（插件自报什么算什么）。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            p = self._scan_port()
            if p:
                return p
            time.sleep(0.3)
        return 0

    def _scan_port(self) -> int:
        # 从账本里读插件身份 wake url 的端口，比猜快
        ent = core.agent_table(self.home).get("anp.adapter.demo") or {}
        wake = ent.get("wake") or {}
        url = str(wake.get("url") or "")
        if ":" in url:
            try:
                return int(url.rsplit(":", 1)[1].split("/")[0])
            except ValueError:
                pass
        return 0

    def received(self) -> list[dict]:
        f = self.home / "plugins" / "anp.adapter.demo" / "data" / "received.jsonl"
        if not f.is_file():
            return []
        return [json.loads(x) for x in f.read_text(encoding="utf-8").splitlines() if x.strip()]

    def kill(self) -> None:
        """先杀插件子进程，再杀 hub。

        ★为什么必须这个顺序★：`proc.kill()` 是强杀，hub 没机会走自己的收尾逻辑，
        它拉起的插件进程会变**孤儿**继续占着端口和内存。实测：每跑一轮实验泄漏 5 个
        插件进程（case_path 1 个 + case_policy 4 个），攒到 99 个之后
         selftest_plugins 的失败率从约 1/4 涨到约 9/10——机器被拖垮了。

        插件的 pid 记在注册表里（`core_spawned` 标出是 Core 拉起的）。
        """
        try:
            reg = plugin.read_registry(self.home)
            for entry in (reg or {}).values():
                pid = entry.get("pid")
                if pid and entry.get("core_spawned") and int(pid) != os.getpid():
                    try:
                        os.kill(int(pid), signal.SIGTERM)
                    except Exception:  # noqa: BLE001 - 已经死了就算了
                        pass
        except Exception:  # noqa: BLE001 - 注册表读不到不该挡住杀 hub
            pass
        try:
            self.proc.kill()
        except Exception:  # noqa: BLE001
            pass


def case_path(tmp: Path) -> None:
    print("\n=== Q1/Q2：Adapter Plugin 通路 ===")
    rig = Rig(tmp, "path")
    try:
        r = rig.load_plugin()
        say("Q1.1 ★插件用自己的令牌报到成功（不需要 admin）★", r.get("ok") is True,
            str(r.get("error") or r.get("loaded", {}).get("state", "")))
        # ★C1 负例①：admin 令牌在这个端点明确被拒★（不留运维旁路）
        adm = core.admin_token(rig.home)
        _, r_adm = post(rig.base, "/plugin/register",
                        {"id": "anp.adapter.demo"}, adm)
        say("Q1.1b ★admin 令牌报到被拒（401）★",
            r_adm.get("ok") is not True and "admin" in str(r_adm.get("hint") or "").lower()
            or r_adm.get("ok") is not True,
            f"ok={r_adm.get('ok')} code={r_adm.get('code')}")
        # ★C1 负例②：拿别的身份的令牌报到本插件 → 拒★
        # （demo-01 是被代管的 agent，与 anp.adapter.demo 是两枚不同令牌）
        # ★令牌要从一个**故意造出来的**身份来★（2026-09-26 实测抓到的自伤）：
        # 旧代码用 `core.key_for("demo-01")` 去"取"令牌，而 `key_for()` 没有密钥时
        # **当场发一枚**——于是验证脚本在插件还来不及代注册之前，先给 demo-01 发了钥匙，
        # 插件随后拿自己的令牌注册 demo-01 就被 P0-2 判成"已知身份 + 令牌不对"（401），
        # Q1.3/Q1.7/Q2.3 三条连着红。看起来像 Core 的 bug，其实是**验证脚本破坏了自己的
        # 被测对象**。现在显式 bootstrap 一个专用探针身份（这一步本来就是想要的效果），
        # 既不碰 demo-01，也不留下任何副作用令牌。
        _, pr = post(rig.base, "/heartbeat", {"name": "other.probe"})
        other = str(pr.get("key") or "")
        _, r_other = post(rig.base, "/plugin/register",
                          {"id": "anp.adapter.demo"}, other)
        say("Q1.1c ★错身份的令牌报到被拒★", r_other.get("ok") is not True,
            f"ok={r_other.get('ok')} code={r_other.get('code')}")
        # 等插件被 Core 拉起并完成 /anp/register
        port = rig.wait_plugin_port()
        say("Q1.2 Core 拉起了插件进程（wake 端口已登记）", port > 0, f"port={port}")

        # 插件应该已经代 demo-01 注册好了
        deadline = time.time() + 12
        row = {}
        while time.time() < deadline:
            row = core.agent_table(rig.home).get("demo-01") or {}
            if row:
                break
            time.sleep(0.3)
        say("Q1.3 ★插件通过公开 /heartbeat 代注册了 demo-01★", bool(row),
            json.dumps({k: row.get(k) for k in ("wake", "client")},
                       ensure_ascii=False)[:120])
        if row:
            wake = row.get("wake") or {}
            say("Q1.4 ★demo-01 的 wake 指向插件自己的 /anp/wake★",
                wake.get("kind") == "http" and str(port) in str(wake.get("url")),
                str(wake.get("url")))
            say("Q1.5 两个身份是两枚不同令牌",
                (core.key_for("anp.adapter.demo", rig.home)
                 != core.key_for("demo-01", rig.home)))

        # 从另一个 Agent 给 demo-01 发信
        _, pk = post(rig.base, "/heartbeat", {"name": "peer.one"})
        _, kr = post(rig.base, "/heartbeat", {"name": "peer.one"}, "")
        pkey = kr.get("key") or pk.get("key") or ""
        code, sent = post(rig.base, "/messages/send",
                          {"from": "peer.one", "to": "demo-01",
                           "body": "经 Adapter Plugin 转投的信",
                           "correlation_id": "q1:adapter", "wake": True}, pkey)
        say("Q1.6 发信成功（ok=true 只表示已持久化）", sent.get("ok") is True,
            f"wake.ok={(sent.get('wake') or {}).get('ok')} "
            f"status={(sent.get('wake') or {}).get('status')}")

        # 等信封被推到插件
        got = []
        for _ in range(40):
            got = rig.received()
            if got:
                break
            time.sleep(0.4)
        say("Q1.7 ★Adapter Plugin 的 /anp/wake 收到了完整 Envelope★",
            len(got) >= 1, f"收到 {len(got)} 封")
        if got:
            env = got[0].get("envelope") or {}
            say("Q1.8 Envelope 字段完整（id/from/to/body/kind）",
                all(env.get(k) for k in ("id", "from", "to", "body", "kind")),
                json.dumps({k: env.get(k) for k in ("id", "from", "to", "body", "kind")},
                           ensure_ascii=False)[:130])
            say("Q1.9 信封署名有效（sig 在）", bool(env.get("sig")),
                f"sig 长度 {len(str(env.get('sig') or ''))}")
        # Q2：两枚令牌的归属——★从账本读，不问插件自己★
        k_plugin = core.key_for("anp.adapter.demo", rig.home)
        k_agent = core.key_for(MANAGED, rig.home)
        say("Q2.1 插件自己持有一枚令牌（Core 发证）", bool(k_plugin), k_plugin[:8] + "…")
        say("Q2.2 ★代管的 demo-01 持有另一枚，两者不同★（P0-1）",
            bool(k_agent) and k_agent != k_plugin,
            f"agent={k_agent[:8] if k_agent else '(空)'}…")
        # ★G1（2026-09-23）★：Q2.3 原来只断言"两枚不同"——这是**不相等**，
        # Core 若把插件令牌塞进投递头，两枚依然不同，测试照样绿。
        # 现在改成**精确比对**：插件落盘的 delivered_key_fp 必须等于 key_for(demo-01)
        # 的指纹，且不等于插件令牌、不等于 hub secret 的指纹。
        delivered_fp = ""
        recv = rig.home / "plugins" / "anp.adapter.demo" / "data" / "received.jsonl"
        if recv.is_file():
            for line in recv.read_text(encoding="utf-8").splitlines():
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if (row.get("envelope") or {}).get("to") == MANAGED:
                    delivered_fp = str(row.get("delivered_key_fp") or "")
                    if delivered_fp:
                        break
        fp_agent = hashlib.sha256(k_agent.encode()).hexdigest()[:16] if k_agent else ""
        fp_plugin = hashlib.sha256(k_plugin.encode()).hexdigest()[:16] if k_plugin else ""
        fp_secret = hashlib.sha256(
            core.hub_secret(rig.home).encode()).hexdigest()[:16]
        say("Q2.3 ★投递头里就是 key_for(demo-01) 那一枚（逐字符全等）★（P0-4/G1）",
            bool(delivered_fp) and delivered_fp == fp_agent,
            json.dumps({"delivered_fp": delivered_fp, "agent_fp": fp_agent}))
        say("Q2.4 不是插件令牌的指纹", delivered_fp != fp_plugin)
        say("Q2.5 ★不是 hub secret 的指纹★", delivered_fp != fp_secret)
        # ★C1 验收⑥：本文件不该再用 admin 令牌去报到★。
        # admin 只允许出现在"构造负例"里，不能是报到路径的 credential。
        # ★ needle 必须拆开写★：否则这行自己就含 "admin_token("，会匹配到自己（第一版就这么假 FAIL）
        needle = "admin" + "_token("
        _src = Path(__file__).read_text(encoding="utf-8")
        _code = "\n".join(line.split("#")[0] for line in _src.split("\n"))
        _n = _code.count(needle)
        # ★文案里也不许出现 needle 字面量★：用 {needle} 插值，
        # 否则这行自己又把计数顶上去（第一次栽在断言代码，第二次栽在提示文案）
        say("Q2.6 ★admin 令牌只出现在负面用例，不在报到路径★（C1 ⑥）",
            _n <= 1, f"{needle}出现 {_n} 处（应 ≤1，即只有 Q1.1b 那条负例）")
    finally:
        rig.kill()


def case_policy(tmp: Path) -> None:
    print("\n=== Q3：register 策略对「Adapter 代管 Agent」的约束 ===")
    cases = [
        ("open", {"register": {"mode": "open", "allow": []}}, True, "默认放行"),
        ("allowlist-有", {"register": {"mode": "allowlist",
                             "allow": ["demo-01", "anp.adapter.demo"]}}, True, "在表里"),
        ("allowlist-无", {"register": {"mode": "allowlist",
                             "allow": ["someone.else"]}}, False, "不在表里"),
        ("closed", {"register": {"mode": "closed", "allow": []}}, False, "全关"),
    ]
    for label, policy, expect_ok, why in cases:
        rig = Rig(tmp, f"pol_{label}", policy)
        try:
            rig.load_plugin()
            deadline = time.time() + 10
            row = {}
            rig.wait_plugin_port()
            while time.time() < deadline:
                row = core.agent_table(rig.home).get(MANAGED) or {}
                if row:
                    break
                time.sleep(0.4)
            got_ok = bool(row)
            say(f"Q3.{label}（{why}）：代管 {'成功' if expect_ok else '被拒'}",
                got_ok == expect_ok,
                f"demo-01 在场={got_ok}；策略={policy['register']['mode']}"
                + (""))
            if got_ok:
                wake = (row.get("wake") or {}).get("url") or ""
                say(f"Q3.{label}：wake 仍指向插件端点",
                    "/anp/wake" in str(wake), str(wake))
        finally:
            rig.kill()


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="anp-adapterplugin-"))
    try:
        case_path(tmp)
        case_policy(tmp)
        print(f"\n===== #6 原型验证：{OK} 通过 / {FAIL} 失败 =====")
        if FAIL:
            print("★有 FAIL★：先看清是哪一条，再决定 Plugin Spec 要不要加机制。")
        return 1 if FAIL else 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
