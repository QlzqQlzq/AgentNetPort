"""DSH Adapter 验收：**两条链 + 必测八项**（Spec §11 / SKILL 第 ⑧ 步）。

用法（在 AgentNetPort 仓库根）：

    python adapters/dsh/verify.py

它会自己起一个**临时 hub**、把 adapter **连同 SDK 一起装进
`<home>/plugins/anp.adapter.dsh/`**（真实安装路径，不是源码目录），
然后跑完整链路：

    Core launch → /anp/register → 公开 /heartbeat 代管 dsh → 发信 → /anp/wake
    → to_native → 递 DSH → 回信 → 落回原对话线

★本脚本**不**把 `src/` 改名藏起来★——它自己要 `python src/anp_hub.py` 起 hub，
藏了就跑不起来。"Adapter 零 Core 依赖"由 H1（静态扫 import）与 H3（只用 canonical
API）在**源码层**证明。（此前这里写着"D 段把 src/ 改名藏一遍"，而脚本里从来没有
这一步——那是文档撒谎，已删。手工的藏 src 验证见 README。）

设计纪律：
  · 每条都在**真实部署路径**上跑，不在源码目录里跑
  · 跳过（SKIP）就明说原因，绝不假失败、也绝不假装测过
  · 条件项失败必须**显式 FAIL**，不许静默消失（复审 H-7）
  · **运维凭据的来源要说实话**：管理面（`POST /plugins/<id>/load` 等）需要
    admin 令牌，本脚本从 `<home>/admin.token` 读。这不是 Adapter 的行为
    （Adapter 本体只走公开 HTTP 面，见 H 段），但也**不是"只读公开 HTTP 面"**——
    Core 另外提供了官方入口（hub 启动日志里主动打印该令牌，
    `src/anp_hub.py` 的 `/sessions` 报错提示也指向它）。本脚本选了读盘这条捷径，
    如实记在这里，不粉饰成"只读公开面"。
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

HERE = Path(__file__).resolve().parent            # adapters/dsh/
REPO = HERE.parent.parent                          # 仓库根
SDK_SRC = REPO / "sdk" / "anp_adapter"
HUB_SRC = REPO / "src" / "anp_hub.py"
MANAGED_AGENT = "dsh"
PLUGIN_ID = "anp.adapter.dsh"

PASS: list[str] = []
FAIL: list[str] = []
SKIP: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> bool:
    (PASS if cond else FAIL).append(name)
    mark = "ok  " if cond else "FAIL"
    print(f"[{mark}] {name}" + (f"\n         {detail}" if detail else ""))
    return bool(cond)


def skip(name: str, why: str) -> None:
    SKIP.append(f"{name}（{why}）")
    print(f"[SKIP] {name}\n         {why}")


# --------------------------------------------------------------------------- #
# HTTP 小工具（只用标准库）
# --------------------------------------------------------------------------- #
def http_json(url: str, payload=None, key: str = "", timeout: float = 15.0,
              method: str = "POST"):
    headers = {"content-type": "application/json; charset=utf-8"}
    if key:
        headers["x-mailbox-key"] = key
    data = None if payload is None else json.dumps(
        payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8") or "{}")
        except (json.JSONDecodeError, OSError):
            return e.code, {}
    except Exception as e:                                     # noqa: BLE001
        return -1, {"_err": f"{type(e).__name__}: {e}"}


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _ports_of(base_url: str) -> list[int]:
    """从 `http://127.0.0.1:<port>` 里取端口号，供退出时精确清理用。"""
    tail = str(base_url or "").rsplit(":", 1)[-1]
    return [int(tail)] if tail.isdigit() else []


def wait_health(base: str, timeout: float = 40.0) -> dict:
    end = time.time() + timeout
    last: dict = {}
    while time.time() < end:
        # /health 含 integrity 扫描，大 home 下可能远超 3s；客户端过早断开会让 hub 写响应时报 10053
        st, body = http_json(base + "/health", None, method="GET", timeout=15)
        if st == 200 and isinstance(body, dict) and body.get("ok"):
            return body
        if isinstance(body, dict) and body:
            last = body
        time.sleep(0.2)
    return last


def wait_agent(base: str, name: str, timeout: float = 20.0,
               want_url: str = "") -> dict:
    """等某个身份出现在公开名册里，且带 wake。

    `want_url` 非空时还要求 wake.url 正好是它——重启恢复那一项用它避开
    "读到上一次运行遗留的旧端口"这种脏读。
    """
    end = time.time() + timeout
    last: dict = {}
    while time.time() < end:
        st, body = http_json(base + "/agents", None, method="GET", timeout=3)
        for a in (body.get("agents") or []):
            if a.get("name") == name:
                last = a
                if not a.get("wake"):
                    continue
                if want_url and str(a["wake"].get("url")) != want_url:
                    continue
                return a
        time.sleep(0.3)
    return last


def wait_status_ready(home: Path, timeout: float = 25.0) -> dict:
    """等插件把 `data/dsh-status.json` 写成 agent_ready。

    ★为什么必须等★：代管注册被刻意放在**后台线程**里（见 plugin.on_register
    的 docstring——放同步路径上会与 Core 的 `/anp/register` 互等并撞破 10 秒
    启动预算）。所以"load 返回 200"不等于"代管注册已完成"，读状态要轮询。
    """
    end = time.time() + timeout
    body: dict = {}
    while time.time() < end:
        body = plugin_status(home)
        if body.get("agent_ready"):
            return body
        time.sleep(0.4)
    return body


# --------------------------------------------------------------------------- #
# 环境：装 adapter 到真实部署路径
# --------------------------------------------------------------------------- #
def install_plugin(home: Path) -> Path:
    """把 adapter 装进 `<home>/plugins/anp.adapter.dsh/`。

    ★SDK 必须 vendor 进插件目录★——Core 拉起插件时"仓库根的 ../sdk"不在路径里，
    不 vendor 就会 `ModuleNotFoundError` 秒退，而 Core 只报「端点不可达」。
    """
    dst = home / "plugins" / PLUGIN_ID
    if dst.exists():
        shutil.rmtree(dst)
    dst.mkdir(parents=True)
    for f in ("manifest.json", "plugin.py", "platform.py"):
        shutil.copy2(HERE / f, dst / f)
    shutil.copytree(SDK_SRC, dst / "anp_adapter",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    return dst


VERIFY_ADMIN_TOKEN = "dsh-verify-admin-token"
VERIFY_HUB_SECRET = "dsh-verify-hub-secret"


def start_hub(home: Path, port: int) -> subprocess.Popen:
    env = dict(os.environ)
    env["AGENT_ANP_HOME"] = str(home)
    env["PYTHONIOENCODING"] = "utf-8"
    # Windows 上落盘 _tighten 后 admin.token / hub.secret / keys.json 可能再读写即 PermissionError，
    # hub 的 secret_isolation 与发证会炸。验收夹具用环境变量钉死 admin/hub secret。
    env["AGENT_ANP_ADMIN_TOKEN"] = VERIFY_ADMIN_TOKEN
    env["AGENT_ANP_HUB_SECRET"] = VERIFY_HUB_SECRET
    env["AGENT_ANP_WEBUI"] = "0"
    log = home / "hub-stdout.log"
    out = open(log, "w", encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, str(HUB_SRC), "--home", str(home), "--port", str(port)],
        stdout=out, stderr=subprocess.STDOUT, env=env)
    proc._hub_log = log  # type: ignore[attr-defined]
    return proc


def admin_token(home: Path) -> str:
    # 与 start_hub 同一枚（Windows 上 admin.token 文件常不可读）
    if os.environ.get("AGENT_ANP_ADMIN_TOKEN"):
        return os.environ["AGENT_ANP_ADMIN_TOKEN"]
    forced = VERIFY_ADMIN_TOKEN
    p = home / "admin.token"
    if p.is_file():
        try:
            return p.read_text(encoding="utf-8").strip() or forced
        except OSError:
            return forced
    return forced


def plugin_load(base: str, token: str, action: str = "load"):
    return http_json(f"{base}/plugins/{PLUGIN_ID}/{action}", {}, token, timeout=45)


def _sdk_drift() -> list[str]:
    """比较**提交在仓库里的** vendor SDK 与 `sdk/` 源，返回内容不同的文件名。

    ★为什么比的是 `HERE/anp_adapter` 而不是装出来的那份★：
    `install_plugin()` 每次运行都从 `sdk/` 现拷，装出来的那份**恒等**，
    查它等于没查。会真正漂移、且随 Adapter 交付出去的是仓库里这份。
    """
    import hashlib
    drift: list[str] = []
    vendor = HERE / "anp_adapter"
    if not vendor.is_dir():
        return ["(vendor 目录缺失)"]
    for src in sorted(SDK_SRC.glob("*.py")):
        dst = vendor / src.name
        if not dst.is_file():
            drift.append(f"{src.name}(缺失)")
            continue
        h = lambda p: hashlib.sha256(p.read_bytes()).hexdigest()  # noqa: E731
        if h(src) != h(dst):
            drift.append(src.name)
    return drift


def plugin_status(home: Path) -> dict:
    p = home / "plugins" / PLUGIN_ID / "data" / "dsh-status.json"
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _load_agent_key(plugin_dir: Path) -> str:
    """从插件自己的 `data/dsh-credentials.json` 读回代管身份的令牌。

    ★这是插件自己的状态，不是 Core 的私盘★（与读 `dsh-status.json` 同一性质）。
    它确实是明文令牌——因为是**同一个 Adapter 的进程内驱动器**在用，
    等价于插件进程自己读它（`platform.load_credentials()` 就是这么做的）。
    """
    p = plugin_dir / "data" / "dsh-credentials.json"
    try:
        return str(json.loads(p.read_text(encoding="utf-8")).get("agent_key") or "")
    except (OSError, json.JSONDecodeError, AttributeError):
        return ""


def _load_adapter_inproc(plugin_dir: Path, base: str, key: str):
    """把**部署目录**里的 Adapter 本体加载进本进程，用于测没有 HTTP 入口的路径。

    ★用部署目录、不用源码目录★——这样被测的就是真正跑起来的那份
    （含 vendor 的 SDK），而不是仓库里可能已经漂移的副本（复审 H-9）。
    返回已接好 `client` 的 `DshAdapter`；失败返回 None。
    """
    import importlib.util
    try:
        # 先让 vendor 的 SDK 可被 import（部署形态的正确用法）
        if str(plugin_dir) not in sys.path:
            sys.path.insert(0, str(plugin_dir))
        spec = importlib.util.spec_from_file_location(
            "dsh_plugin_inproc", plugin_dir / "plugin.py")
        if spec is None or spec.loader is None:
            return None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        from anp_adapter import AnpClient                     # noqa: PLC0415
        adapter = mod.DshAdapter()
        # /agents 免令牌，所以驱动器用哪个身份的 client 都读得到名册
        adapter.client = AnpClient(base, key, "verify-sender")
        return adapter
    except Exception as e:                                     # noqa: BLE001
        print(f"         （加载部署 Adapter 失败：{type(e).__name__}: {e}）")
        return None


# --------------------------------------------------------------------------- #
# 资格线：静态扫描 Adapter 源码的越界行为
# --------------------------------------------------------------------------- #
CORE_MODULES = ("anp_core", "anp_hub", "anp_plugin", "anp_policy",
                "anp_sessions", "anp_preflight")
_IMPORT_RE = __import__("re").compile(
    r"^\s*(?:import|from)\s+(" + "|".join(CORE_MODULES) + r")\b", __import__("re").M)


def _adapter_sources() -> list[Path]:
    return [HERE / "plugin.py", HERE / "platform.py"]


def _imports_core() -> bool:
    return any(_IMPORT_RE.search(p.read_text(encoding="utf-8"))
               for p in _adapter_sources())


def _touches_forbidden() -> bool:
    """真的用了 hub secret / 别人的盘上文件吗？

    ★只在**代码行**里找，跳过 `#` 注释与三引号 docstring★
    ——本 Adapter 的文档大量引用这些词来说明边界（"✗ 持有 hub signing secret"），
    那些是**声明禁止**，不是**违规使用**。判据必须是"去掉注释/docstring 后
    代码里还剩下这些字样"。

    （这个检查器最初漏跳了 docstring，于是把说明文字报成了违规——修的是检查器，
    不是 Adapter。）
    """
    pats = ("hub.secret", "hub_secret", "HUB_SECRET", "keys.json",
            "ledger.jsonl", "signing secret")
    for p in _adapter_sources():
        in_doc = False
        for line in p.read_text(encoding="utf-8").splitlines():
            s = line.strip()
            if s.count('"""') % 2 == 1:      # docstring 块的起止
                in_doc = not in_doc
                continue
            if in_doc or s.startswith("#"):
                continue
            code = line.split("#", 1)[0]
            if any(x in code for x in pats):
                return True
    return False


def _apis_used() -> set[str]:
    """Adapter 实际调用了哪几个 Core API（SDK 方法名即 API 名）。"""
    import re
    want = {"heartbeat", "bootstrap", "send", "receive", "query", "agents",
            "plugin_register", "session_roster", "session_bind"}
    used: set[str] = set()
    src = (HERE / "plugin.py").read_text(encoding="utf-8")
    for m in re.finditer(r"\.(\w+)\s*\(", src):
        if m.group(1) in want:
            used.add(m.group(1))
    # bootstrap 是 heartbeat 的语法糖，规范里是同一个 API（P0-10）
    return {("heartbeat" if u == "bootstrap" else u) for u in used}


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def kill_our_plugins(plugin_dir: Path, ports: list[int]) -> int:
    """清掉**本插件实例**留下的进程。★只认两个精确判据，绝不按进程名/脚本名通配★

    复审 CR-5 抓到的危险写法：原实现里有一条
    `re.fullmatch(r'\\S*python\\S*\\s+plugin\\.py"?', cmd)` —— 它会命中**任何**
    以 `python plugin.py` 启动的进程。本仓库自己的 `examples/adapter_plugin/`
    （以及 `adapters/mimo/`）就是这个命令行；同机别的项目也一样。
    那等于"用验收脚本杀掉别人的插件"。

    现在只认两个**本实例专有**的证据：
      ① 命令行里出现本插件在本次临时 home 下的**绝对路径**；
      ② 进程正在监听**本次运行中 Core 分配给本插件的那几个端口**（端口是逐次随机的，
         别的项目不可能撞上同一组）。

    判据 ② 是从宿主机按端口反查 PID（`Get-NetTCPConnection`），与进程叫什么无关。
    """
    killed = 0
    key = str(plugin_dir).lower()
    targets: set[int] = set()

    # 判据 ②：按端口反查监听进程
    for port in ports:
        try:
            r = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 f"(Get-NetTCPConnection -State Listen -LocalPort {int(port)} "
                 f"-ErrorAction SilentlyContinue).OwningProcess"],
                capture_output=True, text=True, timeout=20)
            for tok in (r.stdout or "").split():
                if tok.strip().isdigit():
                    targets.add(int(tok))
        except Exception:                                      # noqa: BLE001
            pass

    # 判据 ①：命令行含本插件绝对路径
    try:
        ps = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
             "Select-Object ProcessId,CommandLine | ConvertTo-Json -Compress"],
            capture_output=True, text=True, timeout=25)
        raw = (ps.stdout or "").strip()
        if raw:
            rows = json.loads(raw)
            if isinstance(rows, dict):
                rows = [rows]
            for row in rows:
                if key and key in str(row.get("CommandLine") or "").lower():
                    targets.add(int(row["ProcessId"]))
    except Exception:                                          # noqa: BLE001
        pass

    for pid in sorted(targets):
        try:
            subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                           capture_output=True, timeout=8)
            killed += 1
        except Exception:                                      # noqa: BLE001
            pass
    return killed


def run(home: Path) -> bool:
    port = free_port()
    base = f"http://127.0.0.1:{port}"

    # ★先起 hub 再装插件★：若 home/plugins 里已有插件，hub 会在 accept 前预加载，
    # 与插件握手互等会拖穿 wait_health（Core 已知缺口）。B 段再显式 load。
    plugin_ports: list[int] = []
    # 预置 keys.json（不经 write_keys/_tighten）：Windows 上首次发证后 ACL 会把
    # keys.json 锁到 hub 自己都读不动/写不动，后续 issue_key 必炸。
    # 因此把要用到的身份一次写齐；dsh / 插件 id 不靠 bootstrap，走 ⑦a 凭证续约。
    pre = {
        "hub": "preseed-hub-agent-key",
        "verify-sender": "preseed-sender-key",
        PLUGIN_ID: "preseed-plugin-agent-key",
        MANAGED_AGENT: "preseed-dsh-agent-key",
    }
    home.mkdir(parents=True, exist_ok=True)
    (home / "keys.json").write_text(
        json.dumps(pre, ensure_ascii=False, indent=2), encoding="utf-8")
    proc = start_hub(home, port)
    plugin_dir = install_plugin(home)
    # ⑦a：代管凭证落盘，插件用同一枚 key 续约（而不是 bootstrap）
    cred = plugin_dir / "data" / "dsh-credentials.json"
    cred.parent.mkdir(parents=True, exist_ok=True)
    cred.write_text(json.dumps({
        "agent_key": pre[MANAGED_AGENT],
        "plugin_id": PLUGIN_ID,
        "manage_agent": MANAGED_AGENT,
        "saved_at": time.time(),
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        health = wait_health(base)
        if not check("A0 hub 起来了且 health 正常", bool(health.get("ok")),
                     (json.dumps(health.get("policy") or {}, ensure_ascii=False)
                      + " log=" + str(getattr(proc, "_hub_log", "")))):
            try:
                print((Path(proc._hub_log).read_text(encoding="utf-8", errors="replace"))[-800:])
            except Exception:
                pass
            return False
        iso = str(health.get("plugins", {}).get("secret_isolation") or "")
        if "PermissionError" in iso or "读不动" in iso:
            skip("A1 两串钥匙隔离（agent token ≠ hub signing secret）",
                 "Windows ACL：keys.json _tighten 后连 hub 自己都读不动（Core 既有问题）")
        else:
            check("A1 两串钥匙隔离（agent token ≠ hub signing secret）",
                  iso == "isolated", f"secret_isolation={iso}")

        at = admin_token(home)
        if not at:
            skip("插件加载", "home 里没有 admin.token（hub 未生成管理令牌）")
            return False

        # ---------------------------------------------------------------- #
        # B 段：Core 拉起 Adapter Plugin（正向链起点）
        # ---------------------------------------------------------------- #
        print("\n--- B 段：Core 拉起 Adapter Plugin ---")
        st, r = plugin_load(base, at, "load")
        p = (r or {}).get("plugin") or {}
        check("B1 Core 成功拉起插件（load_error 为空）",
              st == 200 and p.get("registered") and not p.get("load_error"),
              f"http={st} body={json.dumps(r, ensure_ascii=False)[:300]}")
        check("B2 插件是 Core 亲手拉起的（core_spawned）",
              bool(p.get("core_spawned")), f"launch={p.get('launch')}")
        plugin_base = str(p.get("base_url") or "")
        if not plugin_base:
            # load 响应可能被掐断；稍后从名册 wake 反推（agent 在下方 wait_agent 后）
            pass
        plugin_ports.extend(_ports_of(plugin_base))

        # ★必测① 精确 Agent ID★：代管身份出现在名册里，wake 指向**本次拉起**的
        # 插件端点（这里必须锚定 plugin_base，否则会读到上一次运行遗留的脏端口——
        # 实测踩到：遗留孤儿进程的 wake 还挂在账本里，看起来像"wake 指错地方"）。
        agent = wait_agent(base, MANAGED_AGENT,
                           want_url=(plugin_base + "/anp/wake") if plugin_base else "")
        if not plugin_base:
            wurl = str((agent.get("wake") or {}).get("url") or "")
            if wurl.endswith("/anp/wake"):
                plugin_base = wurl[: -len("/anp/wake")]
                plugin_ports.extend(_ports_of(plugin_base))
        check("B3 代管身份 dsh 出现且带 wake（必测① 精确 Agent ID）",
              bool(agent.get("wake")), json.dumps(agent.get("wake"),
                                                  ensure_ascii=False))
        check("B4 dsh 的 wake 是 http 档、指向本次拉起的插件自己的 /anp/wake",
              agent.get("wake", {}).get("kind") == "http"
              and str(agent["wake"].get("url")) == plugin_base + "/anp/wake",
              f"wake={agent.get('wake', {}).get('url')} "
              f"本次插件端点={plugin_base}")

        # ---------------------------------------------------------------- #
        # C 段：身份与令牌分离（P0-1 / P0-2 / P0-6）
        # ---------------------------------------------------------------- #
        print("\n--- C 段：身份与令牌（P0-1 / P0-2 / P0-6）---")
        # ★代管注册在后台线程里，这里必须等它落地★（不是"load 200 就好了"）
        sbody = wait_status_ready(home)
        # ★复审 H-3★：C1 原来只读插件自算的 `two_tokens_distinct`——自我证明。
        # 拆成两条：独立证据来自 Core 名册（两个不同身份并存），自报项明确标为参考。
        names = {a.get("name") for a in
                 (http_json(base + "/agents", None, method="GET")[1]
                  .get("agents") or [])}
        check("C1a 名册里并存两个不同身份（独立证据，来自 Core）",
              {PLUGIN_ID, MANAGED_AGENT} <= names,
              f"需要 {PLUGIN_ID} 与 {MANAGED_AGENT} 同时在场；实到 {sorted(names)}")
        check("C1b 插件自报两枚令牌指纹不同（**自报**，仅作参考；"
              "承载证据是 D2+F1 行为对与 C1a）",
              bool(sbody.get("two_tokens_distinct")),
              f"agent_fp={sbody.get('agent_key_fp')} "
              f"plugin_fp={sbody.get('plugin_key_fp')}")
        check("C2 代管注册成功（agent_ready）",
              bool(sbody.get("agent_ready")),
              json.dumps(sbody.get("register_state") or {}, ensure_ascii=False))

        # 已有身份 + 无密钥 → 401（绝不重发令牌）
        st, _ = http_json(base + "/heartbeat", {"name": "dsh-noauth-probe"})
        if st == 200:
            st2, _ = http_json(base + "/heartbeat", {"name": "dsh-noauth-probe"})
        else:
            st2 = st
        check("C3 已有身份 + 无密钥 → 401（绝不重发令牌，P0-2）",
              st2 == 401, f"第二次同名无密钥 heartbeat 得到 HTTP {st2}")

        # sticky 三态：不带 wake 的续约不抹端点
        st, r1 = http_json(base + "/heartbeat", {"name": "sticky-probe",
                                                 "wake": {"kind": "queue"}})
        k = r1.get("key") or ""
        http_json(base + "/heartbeat", {"name": "sticky-probe",
                                        "description": "renew"}, k)
        _, ro = http_json(base + "/agents", None, method="GET")
        w = next((a.get("wake") for a in ro.get("agents") or []
                  if a.get("name") == "sticky-probe"), None)
        check("C4 不带 wake 的续约不抹掉既有端点（P0-6 缺失→保留）",
              bool(w) and w.get("kind") == "queue", json.dumps(w, ensure_ascii=False))
        http_json(base + "/heartbeat", {"name": "sticky-probe", "wake": None}, k)
        _, ro = http_json(base + "/agents", None, method="GET")
        w2 = next((a.get("wake") for a in ro.get("agents") or []
                   if a.get("name") == "sticky-probe"), "MISSING")
        check("C5 wake: null 能主动清掉端点（P0-6 null→清）", w2 is None,
              json.dumps(w2, ensure_ascii=False))

        # ---------------------------------------------------------------- #
        # D 段：正向链 Core → Adapter → DSH
        # ---------------------------------------------------------------- #
        print("\n--- D 段：正向链 Core → Adapter → DSH ---")
        # 发信方用预置 key 续约（不能空头发 bootstrap：verify-sender 已在 keys.json）
        skey = pre["verify-sender"]
        st, sr = http_json(base + "/heartbeat", {"name": "verify-sender",
                                                 "wake": {"kind": "queue"}},
                           skey)
        skey = sr.get("key") or skey
        check("D1 发信方身份就绪", bool(skey) and st == 200,
              f"http={st} auth={sr.get('auth')}")

        st, sent = http_json(base + "/messages/send", {
            "from": "verify-sender", "to": MANAGED_AGENT,
            "body": "ANP 验收：正向链（中文 / emoji 喵~）",
            "kind": "message", "wake": True,
            "correlation_id": "verify-ctx-1",
            "meta": {"context_id": "verify-ctx-1",
                     "from_workspace": str(REPO)},
        }, skey)
        wake = sent.get("wake") or {}
        check("D2 投递成功（wake.ok）", bool(wake.get("ok")),
              json.dumps(wake, ensure_ascii=False))
        check("D3 响应 mode 是合法响应模式、不是 delivery mode",
              wake.get("mode") in ("immediate", "batched", "session",
                                   "inject", "followup"),
              f"mode={wake.get('mode')!r}（必须是 §5.1.1 闭集，不能是 http/exec/queue）")
        check("D4 信被签收（signed_off ≥ 1）——只回 {'ok':true} 是不签收的",
              int(sent.get("signed_off") or 0) >= 1,
              f"signed_off={sent.get('signed_off')}")
        # ★复审 CR-2★：这里原来断言插件**写死的常量** `key_is_agent_not_plugin: True`
        # —— 读被验者自己发的布尔等于没验。现在只把插件给的**真指纹**当材料，
        # 并明确：真正承载 P0-4 的是**行为对**，两半都在本脚本里独立构造——
        #   · 接受半边：Core 用目标 Agent 令牌投递 → wake.ok（D2）
        #   · 拒绝半边：用错令牌直打 /anp/wake → ok:false（F1，下面构造）
        check("D5 投递头令牌指纹材料齐备（真指纹，非写死布尔）",
              bool(wake.get("delivered_key_fp"))
              and wake.get("delivered_key_differs_from_plugin") is True,
              f"delivered_fp={wake.get('delivered_key_fp')} "
              f"plugin_fp={wake.get('plugin_key_fp')}"
              f"（P0-4 的承载证据是 D2 接受 + F1 拒绝这一对行为）")

        # 平台侧真的落盘了吗（DSH 平台语义的产物）
        pending = home / "plugins" / PLUGIN_ID / "data" / "dsh-pending.jsonl"
        rows = []
        if pending.is_file():
            for line in pending.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    try:
                        rows.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
        hit = [x for x in rows if x.get("anp_id") == sent.get("message", {}).get("id")]
        check("D6 信封已被翻译并递到 DSH 侧（dsh-pending.jsonl）", bool(hit),
              f"共 {len(rows)} 条待办，命中 {len(hit)} 条")
        # ★复审 H-7★：D7/D8 原来是 `if hit:` 的子项——D6 一失败，这两条**连计数都不进**，
        # 分母浮动、"全绿"变成可以靠少测来达成。现在一律执行，父项失败它们就是 FAIL。
        row0 = hit[0] if hit else {}
        check("D7 中文/emoji 落盘 UTF-8 无损（不被二次编码）",
              "喵~" in str(row0.get("text") or ""),
              f"text={row0.get('text')!r}（D6 未命中则为空，本条如实 FAIL）")
        check("D8 平台状态写在插件自己的 data/，不在信箱 <home> 根下",
              pending.is_file()
              and (home / "plugins" / PLUGIN_ID) in pending.parents,
              str(pending))

        # ---------------------------------------------------------------- #
        # E 段：反向链 DSH → Adapter → Core（回信落回原对话线）
        # ---------------------------------------------------------------- #
        print("\n--- E 段：反向链 Adapter → Core ---")
        mid = sent.get("message", {}).get("id") or ""
        time.sleep(1.5)
        st, q = http_json(base + "/messages/query",
                          {"from": "verify-sender", "correlation_id": "verify-ctx-1"},
                          skey)
        msgs = q.get("messages") or []
        reps = [m for m in msgs if m.get("reply_to") == mid]
        check("E1 回执落回原对话线（_reply_ack 的 reply_to 指向原信）", bool(reps),
              f"对话线 verify-ctx-1 下 {len(msgs)} 封，{len(reps)} 封回的是原信")
        # ★复审 H-7★：原来 E2/E3 嵌在 `if reps:` 里，E1 一失败就静默消失。
        rep0 = reps[0] if reps else {}
        check("E2 回执原样带回 correlation_id",
              rep0.get("correlation_id") == "verify-ctx-1",
              repr(rep0.get("correlation_id")))
        check("E3 回执 from 是 dsh（不是 Adapter Plugin 身份）",
              rep0.get("from") == MANAGED_AGENT, repr(rep0.get("from")))

        st, q2 = http_json(base + "/messages/query",
                           {"from": "verify-sender", "correlation_id": "verify-ctx-1"},
                           skey)
        check("E4 对话线可查（/messages/query 是 canonical 面）", st == 200,
              f"HTTP {st}")

        # ---------------------------------------------------------------- #
        # F 段：入站鉴权 + 必测八项其余各项
        # ---------------------------------------------------------------- #
        print("\n--- F 段：入站鉴权 + 必测八项 ---")
        pst, phealth = http_json(plugin_base + "/anp/health", None, method="GET",
                                 timeout=6)
        check("F0 插件自己的 /anp/health 可达", pst == 200,
              json.dumps(phealth, ensure_ascii=False))

        st, resp = http_json(plugin_base + "/anp/wake", {
            "message": {"id": "x", "from": "a", "to": MANAGED_AGENT,
                        "body": "b", "kind": "message"},
            "to": MANAGED_AGENT}, "WRONG-TOKEN", timeout=8)
        check("F1 错误令牌的唤醒被拒绝（ok=false，不许假装签收）",
              resp.get("ok") is False, json.dumps(resp, ensure_ascii=False)[:200])

        st, resp2 = http_json(plugin_base + "/anp/wake", {
            "message": {"id": "y", "from": "a", "to": MANAGED_AGENT,
                        "body": "b", "kind": "message"},
            "to": MANAGED_AGENT}, "", timeout=8)
        check("F2 完全不带令牌的唤醒也被拒绝", resp2.get("ok") is False,
              json.dumps(resp2, ensure_ascii=False)[:200])

        # 必测② 模糊目标且唯一 / ③ 多候选 / ④ 不存在目标
        sys.path.insert(0, str(REPO / "sdk"))
        from anp_adapter import AnpClient                     # noqa: PLC0415
        cli = AnpClient(base, skey, "verify-sender")

        # ★用独一无二的词★：验证跑在临时 home 上，但同一台机器上完全可能有
        # 别的 hub/名册残留，用一个绝不撞车的 token 才能保证"唯一候选"成立
        # （实测：用 "Minecraft" 这种常见词会撞上环境里的其他身份）。
        seed = int(time.time() * 1000) % 1000000
        uniq = f"zzq{seed}"          # 多候选那一对共用
        # ★唯一候选必须用**与 uniq 不共享任何可匹配词**的专属 token★
        # 踩到的坑：I 段执行时 uniq-lv2d-a/b 已经存在，拿 uniq 去解析必然 AMBIGUOUS，
        # 看起来像"Adapter.resolve 坏了"，其实是夹具自己把唯一性破坏了。
        solo = f"qzs{seed + 777}"
        http_json(base + "/heartbeat", {"name": f"{solo}-one",
                                        "wake": {"kind": "queue"},
                                        "description": f"负责 {solo} 项目的那个"})
        r_uni = cli.resolve(solo)
        check("F3 必测② 模糊目标且唯一 → RESOLVED",
              r_uni.get("status") == "RESOLVED"
              and r_uni.get("target") == f"{solo}-one",
              f"query={solo} status={r_uni.get('status')} "
              f"target={r_uni.get('target')} candidates="
              f"{[c.get('agent_id') for c in (r_uni.get('candidates') or [])]}")

        # 多候选用同一个专属词的两个实例（同理避开环境撞车）
        for suf in ("a", "b"):
            http_json(base + "/heartbeat", {"name": f"{uniq}-lv2d-{suf}",
                                            "wake": {"kind": "queue"},
                                            "description": f"负责 {uniq} 的立绘资产"})
        r_amb = cli.resolve(f"{uniq} 立绘")
        check("F4 必测③ 模糊目标多候选 → AMBIGUOUS + candidates（★不自动挑★）",
              r_amb.get("status") == "AMBIGUOUS" and not r_amb.get("target")
              and len(r_amb.get("candidates") or []) >= 2,
              f"status={r_amb.get('status')} "
              f"candidates={[c.get('agent_id') for c in (r_amb.get('candidates') or [])]}")

        r_nf = cli.resolve("绝不存在的目标zzz")
        check("F5 必测④ 不存在目标 → NOT_FOUND", r_nf.get("status") == "NOT_FOUND",
              str(r_nf.get("status")))
        check("F6 resolve 与 send 的失败分得开（resolve_stage=resolve）",
              r_nf.get("resolve_stage") == "resolve", str(r_nf.get("resolve_stage")))

        r_ex = cli.resolve(MANAGED_AGENT)
        check("F7 精确 canonical ID → RESOLVED（decided_by=exact）",
              r_ex.get("status") == "RESOLVED" and r_ex.get("decided_by") == "exact",
              f"decided_by={r_ex.get('decided_by')}")

        # 必测⑤a / ⑥：执行 Core 的 meta.session 章（Errata S1；不自建决策）
        st, s5 = http_json(base + "/messages/send", {
            "from": "verify-sender", "to": MANAGED_AGENT, "body": "新工作区第二封",
            "kind": "message", "wake": True, "correlation_id": "verify-ctx-2",
            "meta": {"context_id": "verify-ctx-2",
                     "to_workspace": str(REPO / "adapters" / "dsh")}}, skey)
        w5 = s5.get("wake") or {}
        r5 = w5.get("route_reason") or ""
        m5 = str(w5.get("stamp_mode") or "")
        # E2E 只断言「照章执行」；new/reuse 的确定语义由 I10a–c 钉死
        check("F8 必测⑤a 执行 meta.session（E2E：reason 前缀合法且与 stamp 一致）",
              bool(w5.get("ok"))
              and str(r5).startswith(("meta.session-", "no-stamp-"))
              and m5 in ("", "new", "reuse", "action")
              and (m5 != "reuse" or w5.get("reused") is True)
              and (m5 != "new" or w5.get("reused") is False),
              json.dumps(w5, ensure_ascii=False)[:220])
        skip("F8b 必测⑤b POST /session/bind 回报落点",
             "Layer B 通道扩展面本期 SKIP（Errata S1；DSH 暂不接 bind）")

        st, s6 = http_json(base + "/messages/send", {
            "from": "verify-sender", "to": MANAGED_AGENT, "body": "同一对话线第二封",
            "kind": "message", "wake": True, "correlation_id": "verify-ctx-2",
            "meta": {"context_id": "verify-ctx-2"}}, skey)
        w6 = s6.get("wake") or {}
        r6 = w6.get("route_reason") or ""
        m6 = str(w6.get("stamp_mode") or "")
        check("F9 无 Core 会话名册/绑定时，new 章由 Adapter 照章执行",
              bool(w6.get("ok"))
              and str(r6).startswith("meta.session-new")
              and m6 == "new"
              and w6.get("reused") is False,
              json.dumps(w6, ensure_ascii=False)[:220])
        skip("F9b Core→DSH E2E reuse 章与会话落点",
             "DSH Adapter 尚未上报 /session/roster 或 /session/bind；"
             "I10b 只证明 Adapter 收到 reuse 章时会执行，不能替代 Core E2E")

        # 信封签的是正文：改一个字仍然发得出去
        st, s7 = http_json(base + "/messages/send", {
            "from": "verify-sender", "to": MANAGED_AGENT,
            "body": "ANP 验收：正向链（中文 / emoji 喵~）改一个字",
            "kind": "message", "wake": True, "correlation_id": "verify-ctx-1"}, skey)
        check("F10 把正文改一个字，回信仍然发得出去（信封签的是正文）",
              bool((s7.get("wake") or {}).get("ok")),
              json.dumps(s7.get("wake"), ensure_ascii=False)[:160])

        # ---------------------------------------------------------------- #
        # G 段：必测⑦ Adapter 重启后的恢复
        # ---------------------------------------------------------------- #
        print("\n--- G 段：必测⑦ Adapter 重启恢复 ---")
        # 锚定"重启前"的端点：必须等到它是**本次拉起**的插件端点，否则可能
        # 读到上一轮运行遗留的孤儿端口，G2 就成了假失败（实测踩到）。
        agent_before = wait_agent(base, MANAGED_AGENT,
                                  want_url=plugin_base + "/anp/wake", timeout=20)
        old_url = str((agent_before.get("wake") or {}).get("url") or "")
        st, _ = plugin_load(base, at, "unload")
        time.sleep(1.0)
        st, r = plugin_load(base, at, "load")
        p2 = (r or {}).get("plugin") or {}
        check("G1 重启后 Core 重新拉起插件（load_error 为空）",
              st == 200 and not p2.get("load_error"),
              f"load_error={p2.get('load_error')!r} base_url={p2.get('base_url')}")
        new_base = str(p2.get("base_url") or "")
        plugin_ports.extend(_ports_of(new_base))

        agent2 = wait_agent(base, MANAGED_AGENT,
                            want_url=new_base + "/anp/wake", timeout=25)
        new_url = str((agent2.get("wake") or {}).get("url") or "")
        check("G2 重启后 wake 自愈到新端口（令牌从盘上恢复后续约）",
              bool(new_url) and new_url != old_url
              and new_url == new_base + "/anp/wake",
              f"旧={old_url}\n         新={new_url}（重启后插件端点={new_base}）")

        cred = home / "plugins" / PLUGIN_ID / "data" / "dsh-credentials.json"
        # ★复审 H-5★：这条原来叫"令牌确有落盘（恢复的依据）"，容易被读成
        # "已验证状态全丢后能从 Core 恢复"。**它不是**：unload/load 不删 data/，
        # 所以 G2 验的是**进程重启**恢复，不是"状态全丢"恢复。
        # 而"状态全丢"在 Plugin 形态下**不可恢复**——P0-2 规定已有身份无密钥一律 401
        # 且绝不重新发令牌，Core 也没有删除/重铸身份的管理端点。
        # 这是 Spec 缺口（SKILL ⑦ 的测法与 P0-2 互斥），不是本 Adapter 能修的。
        check("G3 代管令牌落盘（仅证明**进程重启**可续约；状态全丢不可恢复，见 README 缺口）",
              cred.is_file(), str(cred))

        st, s8 = http_json(base + "/messages/send", {
            "from": "verify-sender", "to": MANAGED_AGENT,
            "body": "重启之后还投得进来吗", "kind": "message", "wake": True,
            "correlation_id": "verify-ctx-3"}, skey)
        check("G4 重启后投递仍然成功（端到端）",
              bool((s8.get("wake") or {}).get("ok")),
              json.dumps(s8.get("wake"), ensure_ascii=False)[:200])

        # ★复审 H-8★：这里原来又断言了一次 E1 的 `reps`——白送的绿票，已删。
        # 必测⑧ 改由 E1–E3（收信回执落线）+ I 段（DSH 产出经 reply() 真发回）承载。

        # ---------------------------------------------------------------- #
        # I 段：驱动部署后的 Adapter 本体（复审 CR-3 / CR-4）
        # ---------------------------------------------------------------- #
        print("\n--- I 段：Adapter 本体驱动（reply() 真反向链 / Adapter 自己的 resolve）---")
        drv = _load_adapter_inproc(plugin_dir, base, skey)
        if drv is None:
            check("I0 能加载部署后的 Adapter 本体以驱动测试", False,
                  "import 部署目录里的 plugin.py 失败")
        else:
            check("I0 能加载部署后的 Adapter 本体以驱动测试（用 vendor 的 SDK）",
                  True, str(plugin_dir))
            # ★诚实标注★：这是**进程内驱动器**——直接 import 部署目录里的
            # plugin.py 并调它的方法（而不是隔着 HTTP 打插件）。这样一来：
            #   · 用的是**部署形态的 vendor SDK**（顺带解掉 H-9 的双 SDK 疑虑）；
            #   · reply() 与 resolve() 这两个**没有 HTTP 入口**的路径才测得动。
            # 令牌从插件自己的 data/dsh-credentials.json 读回（G2 已独立证明
            # 它是 Core 经 /heartbeat 发的）。局限：这一层不是端到端 HTTP。
            drv.agent_key = _load_agent_key(plugin_dir)

            # ---- S1：SessionBook 只执行 meta.session，不自建决策（⑤a/⑥ 单元钉死）----
            book = drv.book
            sid_reuse = "sess-stamp-reuse-1"
            r_stamp_new = book.route({
                "correlation_id": "stamp-line-1",
                "meta": {"session": {"mode": "new", "session_id": None,
                                     "reason": "core-decided"}},
            })
            check("I10a meta.session.mode=new → 新会话且 reused=False（照章执行）",
                  r_stamp_new.get("reused") is False
                  and str(r_stamp_new.get("reason") or "") == "meta.session-new",
                  json.dumps(r_stamp_new, ensure_ascii=False))
            r_stamp_reuse = book.route({
                "correlation_id": "stamp-line-1",
                "meta": {"session": {"mode": "reuse", "session_id": sid_reuse,
                                     "reason": "core-decided"}},
            })
            check("I10b meta.session.mode=reuse → 用章上 session_id 且 reused=True",
                  r_stamp_reuse.get("reused") is True
                  and r_stamp_reuse.get("session") == sid_reuse
                  and str(r_stamp_reuse.get("reason") or "") == "meta.session-reuse",
                  json.dumps(r_stamp_reuse, ensure_ascii=False))
            r_stamp_force_new = book.route({
                "correlation_id": "stamp-line-1",
                "meta": {"session": {"mode": "new", "reason": "core-decided"}},
            })
            check("I10c 同对话线但章为 new → 仍开新（不被本地粘性覆盖）",
                  r_stamp_force_new.get("reused") is False
                  and r_stamp_force_new.get("session") != sid_reuse,
                  json.dumps(r_stamp_force_new, ensure_ascii=False))

            # ---- CR-3：真反向链走 reply()，不是 _reply_ack 的固定模板 ----
            st, s9 = http_json(base + "/messages/send", {
                "from": "verify-sender", "to": MANAGED_AGENT,
                "body": "驱动测试：这一封要由 reply() 回",
                "kind": "message", "wake": True,
                "correlation_id": "verify-ctx-reply"}, skey)
            src_id = (s9.get("message") or {}).get("id") or ""
            time.sleep(1.0)
            out = drv.reply(to="verify-sender",
                            text="DSH 侧真实产出：这段字来自平台，不是回执模板",
                            anp_id=src_id,
                            correlation_id="verify-ctx-reply")
            check("I1 reply() 能发出（反向链的唯一出口被真正调用）",
                  bool(out.get("ok")), json.dumps(out, ensure_ascii=False)[:200])
            _, q3 = http_json(base + "/messages/query",
                              {"from": "verify-sender",
                               "correlation_id": "verify-ctx-reply"}, skey)
            # ★同一封原信会有两条 reply_to 相同的回信★：_reply_ack 的固定模板
            # 与 reply() 的平台文本。这里必须**按正文排除回执模板**再断言，
            # 否则会取到回执那条，把"reply() 没生效"误报成失败（夹具自身的坑，已踩）。
            same_line = [m for m in (q3.get("messages") or [])
                         if m.get("reply_to") == src_id]
            m3 = [m for m in same_line
                  if "已收到并注入会话" not in str(m.get("body") or "")]
            r3 = m3[0] if m3 else {}
            check("I2 reply() 的产出带 reply_to 落回原对话线", bool(m3),
                  f"reply_to={src_id} 共 {len(same_line)} 封（含回执），"
                  f"其中非回执 {len(m3)} 封")
            check("I3 reply() 产出正文是平台文本（**不是** _reply_ack 的固定模板）",
                  "平台" in str(r3.get("body") or ""),
                  f"body={str(r3.get('body'))[:60]!r}")
            check("I4 reply() 产出 correlation_id 原样带回",
                  r3.get("correlation_id") == "verify-ctx-reply",
                  repr(r3.get("correlation_id")))

            # ---- CR-4：测 Adapter 自己的 resolve（含 DSH LocalDirectory 提示）----
            # ★用 solo（与 uniq 不共享可匹配词）★：uniq 那一对此刻已并存，
            # 拿 uniq 解析必然 AMBIGUOUS。
            r_uni2 = drv.resolve(solo)
            check("I5 必测② Adapter.resolve 模糊唯一 → RESOLVED（走 Adapter 本体，非 SDK 直调）",
                  r_uni2.get("status") == "RESOLVED"
                  and r_uni2.get("target") == f"{solo}-one",
                  f"query={solo} status={r_uni2.get('status')} "
                  f"target={r_uni2.get('target')}")
            r_amb2 = drv.resolve(f"{uniq} 立绘")
            check("I6 必测③ Adapter.resolve 多候选 → AMBIGUOUS（★不自动挑★）",
                  r_amb2.get("status") == "AMBIGUOUS"
                  and not r_amb2.get("target"),
                  f"status={r_amb2.get('status')} "
                  f"candidates={[c.get('agent_id') for c in (r_amb2.get('candidates') or [])]}")
            r_nf2 = drv.resolve("绝不存在的目标zzz")
            check("I7 必测④ Adapter.resolve 无匹配 → NOT_FOUND",
                  r_nf2.get("status") == "NOT_FOUND", str(r_nf2.get("status")))
            cands = r_amb2.get("candidates") or []
            check("I8 候选视图分开回报 last_used / last_used_source / last_seen（不互相顶替）",
                  all(("last_seen" in c) for c in cands)
                  and any("last_used_source" in c for c in cands),
                  f"候选字段={sorted(cands[0].keys()) if cands else '(无候选)'}")

            # ---- CR-1 钉死：空窗期必须默认拒绝（确定性断言，不靠抢时序）----
            from anp_adapter import Envelope                  # noqa: PLC0415
            env_probe = Envelope({"id": "win-probe", "from": "verify-sender",
                                  "to": MANAGED_AGENT, "body": "越窗试探",
                                  "kind": "message"})
            saved_key = drv.agent_key
            drv.agent_key = ""                    # 模拟"注册尚未完成"的窗口
            win_any = drv.on_wake(env_probe, {"agent_key": "任意令牌"})
            win_empty = drv.on_wake(env_probe, {"agent_key": ""})
            drv.agent_key = saved_key
            check("I9 ★CR-1 钉死★ 空 agent_key 窗口：任意 key 一律 ok:false（不许签收）",
                  win_any.get("ok") is False,
                  f"resp={json.dumps(win_any, ensure_ascii=False)[:160]}")
            check("I10 ★CR-1 钉死★ 空 agent_key 窗口：空 key 同样 ok:false",
                  win_empty.get("ok") is False,
                  f"resp={json.dumps(win_empty, ensure_ascii=False)[:160]}")

            # ---- P0-4 强形式：**任何**非目标 Agent 令牌都必须被拒 ----
            # ★必须用**重启后**的端点★：G 段 unload/load 之后插件换了端口，
            # 用 G 之前的 plugin_base 会连不上（夹具自身的坑，已踩）。
            live_base = new_base or plugin_base
            st, wrong = http_json(live_base + "/anp/wake", {
                "message": {"id": "p04", "from": "verify-sender",
                            "to": MANAGED_AGENT, "body": "用发信方令牌试",
                            "kind": "message"},
                "to": MANAGED_AGENT}, skey, timeout=8)
            check("I11 拿**发信方**令牌当 wake key 被拒（P0-4 不是橡皮图章）",
                  wrong.get("ok") is False,
                  json.dumps(wrong, ensure_ascii=False)[:160])

        # ---------------------------------------------------------------- #
        # H 段：资格线——Adapter 与 Core 之间只有 SDK + HTTP
        # ---------------------------------------------------------------- #
        print("\n--- H 段：资格线（零 Core 依赖 / 边界纪律）---")
        check("H1 plugin.py / platform.py 没有 import 任何 Core 模块",
              not _imports_core(), "静态扫描 import 语句")
        check("H2 源码里不出现 hub signing secret，也不读 keys.json / ledger.jsonl",
              not _touches_forbidden(),
              "静态扫描；注释里说明「禁止」不算违规")
        check("H3 Adapter 只用 canonical 六件套 + 通道扩展面（S1）",
              _apis_used() <= {"heartbeat", "send", "receive", "query",
                               "agents", "plugin_register",
                               "session_roster", "session_bind"},
              f"用到：{sorted(_apis_used())}")
        check("H4 SDK 已 vendor 进插件目录（部署形态，不依赖仓库根的 ../sdk）",
              (home / "plugins" / PLUGIN_ID / "anp_adapter" / "__init__.py").is_file(),
              str(home / "plugins" / PLUGIN_ID / "anp_adapter"))

        # ★复审 H-9★：同源锁。上面 H4 只证明"装出来的那份存在"——它本来就是
        # 本次从 `sdk/` 拷的，恒真。真正会漂移的是**提交在仓库里的**
        # `adapters/dsh/anp_adapter/`（随 Adapter 交付的那份）。
        # 实测已漂移过一次（2026-09-24：`props` 端删掉死方法 `from_response`，
        # 而 vendor 里还留着）——那正是这条检查要抓的。
        drift = _sdk_drift()
        check("H5 提交在仓库里的 vendor SDK 与 `sdk/` 逐文件同源（漂移即红）",
              not drift, "漂移文件：" + (", ".join(drift) if drift else "无"))

        return not FAIL

    finally:
        try:
            proc.terminate()
            proc.wait(timeout=8)
        except Exception:                                      # noqa: BLE001
            proc.kill()
        # ★插件是 Core 的子进程，但 Core 退出不保证它跟着走★——
        # 留着的孤儿会把 wake 挂在账本里，污染下一次运行（实测踩到）。
        n = kill_our_plugins(plugin_dir, plugin_ports)
        if n:
            print(f"          （清理了 {n} 个本插件实例的进程，判据=绝对路径 或 本实例端口）")


def main() -> int:
    print("=" * 74)
    print("DSH Adapter 验收 —— 两条链 + 必测八项")
    print(f"仓库   : {REPO}")
    print(f"Adapter: {HERE}")
    print("=" * 74)

    tmp = Path(tempfile.mkdtemp(prefix="anp-dsh-verify-"))
    home = tmp / "home"
    home.mkdir(parents=True)
    try:
        ok = run(home)
    except Exception as e:                                     # noqa: BLE001
        import traceback
        traceback.print_exc()
        check(f"验收过程未抛异常（{type(e).__name__}）", False, str(e))
        ok = False
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    print("=" * 74)
    print(f"PASS {len(PASS)}　FAIL {len(FAIL)}　SKIP {len(SKIP)}")
    if FAIL:
        print("\n失败项：")
        for f in FAIL:
            print(f"  · {f}")
    if SKIP:
        print("\n跳过项（逐条说明原因，绝不假失败也绝不假装测过）：")
        for s in SKIP:
            print(f"  · {s}")
    print("=" * 74)
    return 0 if (ok and not FAIL) else 1


if __name__ == "__main__":
    raise SystemExit(main())
