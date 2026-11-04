"""ANP 插件：manifest 校验、注册表、进程事实三分离。

★本模块的边界（spec §3.1，硬规则）★
    插件状态      = 操作人意图          → PLUGIN_STATE，Core 维护
    进程存活      = 不跟踪              → Core 不探活、不重启、不保活
    端点可达      = 每次投递观测         → 只是"端点不可达"，走既有重试

Core 拉起 ≠ Core 负责维持插件进程存活。插件自己退出 → "端点不可达" → 重试 → 放弃自动重投，
**不是**一次状态变化。这是本模块最重要的一条，别在这里加 supervisor。

★永久架构约束★：插件拿到的只有它自己那一枚 agent token。
`hub.secret` 绝不出现在本模块的任何返回值里（发证漏斗在 `core.issue_key()`）。
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

import anp_core as core

# 插件能声明的权限（闭集，spec §5）。Core 只校验这 5 个，不在表里的 → permission.denied。
PERMISSIONS = frozenset({
    "wake.others", "broadcast", "agents.discover", "tools.register", "webui.panel",
})

# manifest 必填字段（spec §2）
REQUIRED_FIELDS = ("id", "name", "version", "api_level")

# id 字符集：会变成目录名 + 工具名前缀，所以不允许路径分隔符和大写
_ID_OK = "abcdefghijklmnopqrstuvwxyz0123456789._-"

PLUGIN_STATE = ("enabled", "disabled")   # 插件状态只有这两个值（有没有别的不归我们管）

# 拉起后的握手等待：插件要在这么久之内实现 /anp/register 并应答
REGISTER_TIMEOUT = 10.0


class ManifestError(Exception):
    """manifest 有问题。`code` 是闭集错误码之一，`detail` 给人看。"""

    def __init__(self, detail: str, code: str = "manifest.invalid") -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


# --------------------------------------------------------------------------- #
# manifest 读取与校验
# --------------------------------------------------------------------------- #
def manifest_path(plugin_dir: Path | str) -> Path:
    return Path(plugin_dir) / "manifest.json"


def scan_plugins(home: Path = core.DEFAULT_HOME) -> list[Path]:
    """扫 `<home>/plugins/*/manifest.json`，按 id 排序返回目录。没有 plugins 目录就是空表。"""
    root = Path(home) / "plugins"
    if not root.is_dir():
        return []
    out = []
    for sub in sorted(root.iterdir()):
        if sub.is_dir() and manifest_path(sub).is_file():
            out.append(sub)
    return out


def _is_loopback_host(host: str) -> bool:
    h = (host or "").strip().strip("[]").lower()
    if not h:
        return False
    return h in ("127.0.0.1", "localhost", "::1") or h.startswith("127.")


def validate_manifest(raw: Any, *, dir: Path | None = None) -> dict[str, Any]:
    """校验 manifest 并返回规范化后的 dict。任何问题都抛 `ManifestError`。

    ★command / url 严格二选一★（spec §2.2）：先数个数，再谈内容。
    两个都有 / 两个都没有都算 manifest.invalid——否则 Core 不知道该 spawn 还是 attach。
    """
    if not isinstance(raw, dict):
        raise ManifestError("manifest 顶层必须是 JSON 对象")

    # --- 必填字段 ---
    for field in REQUIRED_FIELDS:
        if field not in raw:
            raise ManifestError(f"缺少必填字段：{field}")
        if raw[field] is None:
            raise ManifestError(f"必填字段不能为 null：{field}")

    # --- id ---
    pid = raw["id"]
    if not isinstance(pid, str) or not pid:
        raise ManifestError("id 必须是非空字符串")
    if pid != pid.lower():
        raise ManifestError(f"id 必须全小写（现在是 {pid}；它会变成目录名与工具名前缀）")
    bad = sorted({c for c in pid if c not in _ID_OK})
    if bad:
        raise ManifestError(f"id 含非法字符 {bad}；只允许 [a-z0-9._-]，且首字符为字母或数字")
    if pid[0] not in "abcdefghijklmnopqrstuvwxyz0123456789":
        raise ManifestError(f"id 首字符必须是字母或数字（现在是 {pid[0]!r}）")
    if "/" in pid or "\\" in pid or pid in (".", ".."):
        raise ManifestError(f"id 不能是路径片段：{pid}")

    # --- name / version ---
    for field in ("name", "version"):
        if not isinstance(raw[field], str) or not raw[field].strip():
            raise ManifestError(f"{field} 必须是非空字符串")

    # --- api_level：正整数 ---
    level = raw["api_level"]
    if isinstance(level, bool) or not isinstance(level, int):
        raise ManifestError("api_level 必须是正整数（整数代次，不是字符串也不是 semver）")
    if level < 1:
        raise ManifestError(f"api_level 必须 >= 1（现在是 {level}）")

    # --- anp_min ---
    anp_min = raw.get("anp_min")
    if anp_min is not None and (not isinstance(anp_min, str) or not anp_min.strip()):
        raise ManifestError("anp_min 如果给了就必须是非空字符串")

    # --- command / url 严格二选一 ---
    has_cmd = raw.get("command") is not None
    has_url = raw.get("url") is not None
    if has_cmd and has_url:
        raise ManifestError("command 与 url 只能有一个（都有 → Core 不知道该拉起还是该连接）")
    if not has_cmd and not has_url:
        raise ManifestError("command 与 url 必须有一个（描述 Core 怎么找到这个插件）")

    entry: dict[str, Any] = {}
    if has_cmd:
        cmd = raw["command"]
        if isinstance(cmd, str):        # 容错：裸字符串当成单元素 argv，别让第三方为类型卡住
            cmd = [cmd]
        if not isinstance(cmd, list) or not cmd:
            raise ManifestError("command 必须是非空数组（argv），例如 [\"python\", \"plugin.py\"]")
        if any(not isinstance(c, str) or not c for c in cmd):
            raise ManifestError("command 的元素必须都是非空字符串")
        entry["command"] = list(cmd)
        entry["launch"] = "spawn"
    else:
        url = raw["url"]
        if not isinstance(url, str) or not url.strip():
            raise ManifestError("url 必须是非空字符串")
        parsed = urllib.parse.urlparse(url.strip())
        if parsed.scheme not in ("http", "") or not _is_loopback_host(parsed.hostname or ""):
            raise ManifestError(
                f"url 必须指向本机回环（127.0.0.1 / localhost / ::1），现在是 {url!r}")
        entry["url"] = url.strip()
        entry["launch"] = "attach"

    # --- cwd ---
    cwd = raw.get("cwd")
    if cwd is not None:
        if not isinstance(cwd, str) or not cwd.strip():
            raise ManifestError("cwd 如果给了就必须是非空字符串")
        entry["cwd"] = cwd.strip()

    # --- permissions：闭集 ---
    perms = raw.get("permissions")
    granted: list[str] = []
    if perms is not None:
        if not isinstance(perms, list) or any(not isinstance(p, str) for p in perms):
            raise ManifestError("permissions 必须是字符串数组")
        unknown = [p for p in perms if p not in PERMISSIONS]
        if unknown:
            raise ManifestError(
                f"未知权限 {unknown}；只接受 {sorted(PERMISSIONS)}")
        granted = [p for p in sorted(PERMISSIONS) if p in set(perms)]   # 固定顺序，别让声明顺序影响输出

    # --- requires.plugins：只做存在性检查 ---
    requires = raw.get("requires")
    needs: list[str] = []
    if requires is not None:
        if not isinstance(requires, dict):
            raise ManifestError("requires 必须是对象，例如 {\"plugins\": [\"alice.util\"]}")
        needs = requires.get("plugins")
        if needs is None:
            needs = []
        if not isinstance(needs, list) or any(not isinstance(x, str) or not x for x in needs):
            raise ManifestError("requires.plugins 必须是字符串数组")

    # --- tags ---
    tags = raw.get("tags")
    if tags is not None:
        if not isinstance(tags, list) or any(not isinstance(x, str) for x in tags):
            raise ManifestError("tools/tags 这类数组字段必须是字符串数组")

    # 规范化输出：只保留 Core 认识的字段。未知字段**一律忽略**（spec §2.4）：
    # 不报错（保前后向兼容），但也不生效（明确的降级，不是隐式的魔法）。
    return {
        "id": pid,
        "name": raw["name"],
        "version": raw["version"],
        "api_level": level,
        "description": raw.get("description") if isinstance(raw.get("description"), str) else None,
        "author": raw.get("author") if isinstance(raw.get("author"), str) else None,
        "license": raw.get("license") if isinstance(raw.get("license"), str) else None,
        "repository": raw.get("repository") if isinstance(raw.get("repository"), str) else None,
        "homepage": raw.get("homepage") if isinstance(raw.get("homepage"), str) else None,
        "anp_min": (anp_min.strip() if isinstance(anp_min, str) else None),
        "permissions": granted,
        "requires": needs,
        "tags": [t for t in (tags or []) if isinstance(t, str)],
        **entry,
        "dir": str(dir) if dir is not None else None,
    }


def list_valid(home: Path = core.DEFAULT_HOME) -> tuple[dict[str, Path], list[dict[str, str]]]:
    """扫 plugins/ 并逐个校验 manifest。

    返回 `({id: 目录}, [坏的])`。坏的不抛错——一个插件的 manifest 写错不该让整个
    hub 起不来，只是那一个插件没被加载起来（错误在 `plugin_reload_all` 的返回里可见）。
    """
    good: dict[str, Path] = {}
    bad: list[dict[str, str]] = []
    for d in scan_plugins(home):
        try:
            man = load_manifest(d)
        except ManifestError as e:
            bad.append({"id": str(d), "dir": str(d), "code": e.code, "error": e.detail})
            continue
        good[man["id"]] = d
    return good, bad


def load_manifest(plugin_dir: Path | str) -> dict[str, Any]:
    """读 + 校验一个插件目录的 manifest。文件问题也归一成 ManifestError。"""
    p = Path(plugin_dir)
    mp = manifest_path(p)
    if not mp.is_file():
        raise ManifestError(f"目录里没有 manifest.json：{p}")
    text = core.read_text_shared(mp)   # 不存在 → 空串（上面已用 is_file() 挡过）
    if not text.strip():
        raise ManifestError(f"manifest.json 是空的：{mp}")
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as e:
        raise ManifestError(f"manifest.json 不是合法 JSON：{e}") from e
    return validate_manifest(raw, dir=p)


def version_tuple(text: str) -> tuple[int, ...]:
    """'0.2.0' → (0, 2, 0)。非数字段按 0 处理，长度不足补 0。只用于单向比较。"""
    out: list[int] = []
    for part in str(text or "").split("."):
        digits = "".join(ch for ch in part if ch.isdigit())
        out.append(int(digits) if digits else 0)
    while len(out) < 3:
        out.append(0)
    return tuple(out)


def check_compat(man: dict[str, Any], core_level: int = core.CORE_API_LEVEL,
                 core_version: str = "0.2.0") -> None:
    """版本兼容。拒绝时抛 ManifestError(code=api.level.unsupported)。"""
    level = man["api_level"]
    if level > core_level:
        raise ManifestError(
            f"插件用的 Plugin API 代次（api_level={level}）比本信箱新"
            f"（CORE_API_LEVEL={core_level}）；请升级信箱",
            code="api.level.unsupported")
    need = man.get("anp_min")
    if need and version_tuple(need) > version_tuple(core_version):
        raise ManifestError(
            f"插件要求信箱版本 >= {need}，当前是 {core_version}",
            code="api.level.unsupported")


# --------------------------------------------------------------------------- #
# 注册表（plugins.json）
# --------------------------------------------------------------------------- #
def registry_path(home: Path = core.DEFAULT_HOME) -> Path:
    return Path(home) / "plugins.json"


def read_registry(home: Path = core.DEFAULT_HOME) -> dict[str, Any]:
    """读注册表。文件不在 = 空表；读坏了 = 空表 + 由调用方决定（宁可空，不要崩）。"""
    p = registry_path(home)
    # ★`core.read_text_shared()` 对不存在的文件返回空串，不是抛 FileNotFoundError★
    # （2026-09-22 踩过：按 FileNotFoundError 接，第一次读注册表就炸）
    text = core.read_text_shared(p).strip()
    if not text:
        return {}
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"plugins.json 不是合法 JSON（{e}）；删掉它可以重新加载") from e
    if not isinstance(raw, dict):
        raise RuntimeError("plugins.json 顶层必须是对象")
    plugins = raw.get("plugins")
    return plugins if isinstance(plugins, dict) else {}


def write_registry(plugins: dict[str, Any], home: Path = core.DEFAULT_HOME) -> None:
    p = registry_path(home)
    payload = json.dumps({"plugins": plugins}, ensure_ascii=False, indent=2) + "\n"
    try:
        core._atomic_write_text(p, payload)
    except PermissionError:
        # Windows：_tighten 后旧文件可能拒写；删掉重建（与 _write_secret_text 同策略）
        try:
            p.unlink()
        except OSError:
            pass
        core._atomic_write_text(p, payload)
    core._tighten(p)


# --------------------------------------------------------------------------- #
# 权限查表（spec §5：load 时把 manifest 的 permissions 记进账本，调用点查表）
# --------------------------------------------------------------------------- #
def permissions_of(plugin_id: str, home: Path = core.DEFAULT_HOME) -> list[str] | None:
    """这个**已加载插件**声明了哪些权限；不是已加载插件 → None。

    ★为什么返回 None 而不是空表★：普通身份（各 Adapter 代管的 agent、MCP 客户端）
    根本没有 manifest，也就没有权限这回事——它们不参与插件权限查表。
    把"不是插件"和"是插件但什么都没声明"混为一谈，会让调用点把它们一起挡掉。
    """
    entry = (read_registry(home) or {}).get(str(plugin_id))
    if not isinstance(entry, dict):
        return None
    return [p for p in (entry.get("permissions") or []) if p in PERMISSIONS]


def has_permission(plugin_id: str, perm: str, home: Path = core.DEFAULT_HOME) -> bool:
    """这个已加载插件有 perm 吗？（`permissions_of` 的便捷形式）"""
    perms = permissions_of(plugin_id, home)
    return bool(perms) and perm in perms


# --------------------------------------------------------------------------- #
# 端口与进程
# --------------------------------------------------------------------------- #
def free_port() -> int:
    """让 OS 给一个空闲端口（bind 后立刻关，交给插件 bind；ANP 只跑在本机，竞态面极小）。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])
    finally:
        s.close()


def _terminate(entry: dict[str, Any]) -> bool:
    """终止一个进程。★只终止 Core 自己拉起的那个★（spec §3.1：绝不碰外部进程）。"""
    pid = entry.get("pid")
    if not entry.get("core_spawned") or not pid:
        return False
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                       capture_output=True, check=False)
    else:
        try:
            os.kill(int(pid), 15)
        except Exception:  # noqa: BLE001 - 进程已经不在了，正是我们想要的
            return True
    return True


def _plugin_extra_env(home: Path, plugin_id: str) -> dict[str, Any]:
    """给某个插件追加的部署级环境变量（可选）。

    为什么需要（2026-09-27 实际踩到）：插件的 env 完全继承自 hub（`launch()` 里
    `dict(os.environ)`），而 hub 常由别的终端/进程拉起，**插件自己的部署参数没处放**。
    实例：codex adapter 只认 `ANP_CODEX_BIN` 或 PATH 里的 `codex`，而该 CLI 装在
    `%LOCALAPPDATA%\\...\\<hash>\\codex.exe`（版本目录、不在 PATH）→ adapter 拉不起平台，
    表现为「信投递成功、签收成功，但 agent 回一封失败补偿信」，从 Core 侧看不出原因。

    两个来源，**后者覆盖前者**：
      ① 插件自己的 `manifest.json` 的 `extra_env`（随插件走，适合"大多数机器都一样"的默认）
      ② 部署侧 `<home>/plugins/<id>/plugin.env.json`（适合"这台机器上它在哪"的差异）
    两者都是**平铺的字符串字典**；非字符串值拒绝（fail loud，避免把 `None` 塞进环境）。

    安全边界：这里只放**非秘密**的部署参数。令牌永远走 `/anp/register` 的响应体，
    不进环境变量（spec §3.2）；`hub.secret` 更不许出现在这里。
    """
    out: dict[str, Any] = {}

    def absorb(data: Any, where: str) -> None:
        if data is None:
            return
        if not isinstance(data, dict):
            raise ManifestError(f"{where} 必须是 JSON 对象（键=环境变量名）")
        for k, v in data.items():
            if not isinstance(v, str):
                raise ManifestError(f"{where} 的 {k!r} 必须是字符串（拿到 {type(v).__name__}）")
            out[str(k)] = v

    # ① manifest 里的 extra_env（目录从 home 推，兜底用注册表里的 dir）
    manifest_file = Path(home) / "plugins" / plugin_id / "manifest.json"
    if manifest_file.is_file():
        try:
            raw = json.loads(core.read_text_shared(manifest_file) or "{}")
        except json.JSONDecodeError:
            raw = {}
        if isinstance(raw, dict):
            absorb(raw.get("extra_env"), f"{manifest_file.name}.extra_env")

    # ② 部署侧覆盖文件（优先）
    deploy_file = Path(home) / "plugins" / plugin_id / "plugin.env.json"
    if deploy_file.is_file():
        try:
            absorb(json.loads(core.read_text_shared(deploy_file) or "{}"), deploy_file.name)
        except json.JSONDecodeError as e:
            raise ManifestError(f"{deploy_file} 不是合法 JSON：{e}") from e

    return out


def launch(man: dict[str, Any], entry: dict[str, Any], home: Path) -> dict[str, Any]:
    """按 manifest 拉起插件（`command`）或确认已在运行（`url`）。返回更新后的 entry。

    ★command / url 严格二选一已由 validate_manifest 保证★，这里只可能是两种之一。
    """
    launched = dict(entry)
    if man.get("launch") == "attach":
        launched["base_url"] = man["url"]
        launched["launch"] = "attach"
        launched["core_spawned"] = False
        launched["pid"] = None
        launched["port"] = None
        return launched

    port = free_port()
    cmd = list(man["command"])
    cwd = man.get("cwd") or man.get("dir") or "."
    cwd_path = Path(cwd)
    if not cwd_path.is_absolute():
        cwd_path = Path(man["dir"]) / cwd if man.get("dir") else Path.cwd() / cwd
    env = dict(os.environ)
    # 端口与环境变量是唯一的"注入点"：argv 里不带任何占位符（spec §3.2）
    env["ANP_PLUGIN_PORT"] = str(port)
    env["ANP_PLUGIN_ID"] = man["id"]
    env["AGENT_ANP_HOME"] = str(home)
    # ★给这一个插件追加的部署级环境变量★（可选，缺省不影响行为）。
    # 为什么要有它：插件的 env 完全继承自 hub（`dict(os.environ)`），而 hub 常常是被
    # 别的进程/终端拉起来的，插件需要的部署参数（例如"某 CLI 的可执行文件在哪"）没处放。
    # 今天的实际案例：codex adapter 只认 ANP_CODEX_BIN / PATH，而该 CLI 装在
    # `%LOCALAPPDATA%\...\<hash>\codex.exe`（版本目录，不在 PATH）→ 它拉不起平台，
    # 表现为"信投递成功、签收成功、但 adapter 回一封失败补偿信"。
    # 两个来源，后者优先：① <home>/plugins/<id>/plugin.env.json ② 读取器注入的 env。
    # 覆盖 `os.environ` 的值属于**有意为之**（部署就是要在不改 hub 环境的前提下配某一个插件）；
    # 但**不允许**覆盖 Core 自己那三个注入点（端口/插件 id/home），否则插件就不知道自己在哪了。
    extra = _plugin_extra_env(home, man["id"])
    for k, v in extra.items():
        if k in ("ANP_PLUGIN_PORT", "ANP_PLUGIN_ID", "AGENT_ANP_HOME"):
            continue
        env[str(k)] = str(v)
    exe = cmd[0]
    resolved = shutil.which(exe) if not os.path.isabs(exe) else exe
    if not resolved:
        raise ManifestError(f"拉起失败：找不到可执行文件 {exe!r}（PATH 里没有，或写错了）")
    creation = 0
    if os.name == "nt":
        creation = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    # 输出重定向到 DEVNULL：★stderr 不许进管道★（第 1 步修过同一个故障——
    # 无人读的管道写满会让 write 阻塞。这里由 Core 拉起的子进程同样适用）。
    proc = subprocess.Popen(cmd, cwd=str(cwd_path), env=env, stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            creationflags=creation)
    launched.update({
        "base_url": f"http://127.0.0.1:{port}",
        "launch": "spawn",
        "core_spawned": True,
        "pid": proc.pid,
        "port": port,
    })
    return launched


# --------------------------------------------------------------------------- #
# 双向握手（spec §4.0）
# --------------------------------------------------------------------------- #
def _http_json(url: str, payload: dict[str, Any] | None, *, timeout: float,
               method: str = "POST", token: str | None = None) -> tuple[bool, Any]:
    """对一个端点发 JSON。返回 `(端点通, 解析结果)`。
    HTTP 200 但 body 不是合法 JSON 也当"端点不可达"——插件说话我们听不懂，等于没说话。
    """
    data = json.dumps(payload or {}, ensure_ascii=False).encode("utf-8")
    headers = {"content-type": "application/json; charset=utf-8"}
    if token:
        # 插件自己的 agent token，不是 hub.secret（永久架构约束）
        headers["x-mailbox-key"] = token
    try:
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            return False, {"error": f"HTTP {e.code}"}
        try:
            return True, json.loads(body)
        except json.JSONDecodeError:
            return True, {"ok": False, "error": f"HTTP {e.code}: 响应不是合法 JSON"}
    except Exception as e:  # noqa: BLE001 - 连接不上/超时/DNS 都归"端点不可达"
        return False, {"error": f"{type(e).__name__}: {e}"}
    try:
        return True, json.loads(body)
    except json.JSONDecodeError:
        return False, {"error": "响应不是合法 JSON"}


def _norm_contributions(resp: Any) -> dict[str, Any]:
    """把插件自报的贡献规范化。插件乱写就忽略那一项，绝不让它污染 Core 的工具表。"""
    out: dict[str, Any] = {"tools": [], "events": [], "events_url": None, "panel": None}
    if not isinstance(resp, dict):
        return out
    tools = resp.get("tools")
    if isinstance(tools, list):
        clean: list[dict[str, Any]] = []
        for t in tools:
            if isinstance(t, dict) and isinstance(t.get("name"), str) and t["name"].strip():
                clean.append({
                    "name": t["name"].strip(),
                    "description": t.get("description") if isinstance(t.get("description"), str) else "",
                    "input_schema": t.get("input_schema") if isinstance(t.get("input_schema"), dict) else {"type": "object"},
                })
        out["tools"] = clean
    events = resp.get("events")
    if isinstance(events, list):
        out["events"] = [e for e in events if isinstance(e, str)]
    if isinstance(resp.get("events_url"), str):
        out["events_url"] = resp["events_url"]
    if isinstance(resp.get("panel"), str):
        out["panel"] = resp["panel"]
    return out


def plugin_base_url(entry: dict[str, Any]) -> str | None:
    base = str(entry.get("base_url") or "").strip()
    return base or None


def register_out(entry: dict[str, Any], home: Path, *, timeout: float = REGISTER_TIMEOUT
                 ) -> dict[str, Any]:
    """★★方向二：Core → Plugin。POST 插件的 `/anp/register`（spec §4.0）★★

    把**凭证与配置**推给插件，插件回**贡献**。
    ★永久约束★：`token` 是插件自己那一枚 agent token；本函数任何返回值里都不含 hub.secret。
    """
    base = plugin_base_url(entry)
    if not base:
        raise ManifestError("插件没有 base_url（command / url 校验应当已经挡住这种情况）")
    payload = {
        "identity": entry["id"],
        "token": entry.get("token"),
        "config": entry.get("config") or {},
        "plugin_dir": str(entry.get("dir") or ""),
        "api_level": int(entry.get("api_level") or core.CORE_API_LEVEL),
        "reload_reason": entry.get("last_action") or "load",
    }
    # ★头里带插件自己的 agent token★：与 `/anp/wake` 同一个口径（spec 方向 B）。
    # 插件可以拿"头里的 token == body 里的 token"确认这确实是信箱发来的，而不是
    # 本机别的进程冒名推送假凭证。（★不是 hub.secret★——永久架构约束）
    url = base.rstrip("/") + "/anp/register"
    ok, resp = False, {}
    deadline = time.time() + max(1.0, timeout)
    delay = 0.05
    # ★必须等端点就绪★：`subprocess.Popen` 是立刻返回的，插件进程要 import + bind 才真的 Listening。
    # 不等待的话 load 就是拼运气——实测同一个插件时通时不通（2026-09-22 抓到）。
    while True:
        ok, resp = _http_json(url, payload, token=entry.get("token"),
                              timeout=min(5.0, max(0.5, deadline - time.time())))
        if ok or not resp.get("transport") or time.time() >= deadline:
            break
        time.sleep(delay)
        delay = min(delay * 2, 0.5)
    if not ok:
        raise ManifestError(
            f"插件端点不可达：POST {base}/anp/register —— {resp.get('error')}"
            f"（已等待 {timeout:.0f} 秒）",
            code="endpoint.unreachable")
    if not isinstance(resp, dict) or resp.get("ok") is False:
        detail = resp.get("error") if isinstance(resp, dict) else str(resp)
        raise ManifestError(f"插件拒绝了注册：{detail}", code="plugin.error")
    return _norm_contributions(resp)


def announce_in(man: dict[str, Any], entry: dict[str, Any], contrib: dict[str, Any],
                home: Path, *, timeout: float = REGISTER_TIMEOUT) -> dict[str, Any]:
    """★★方向一：Plugin → Core。处理 `POST /plugin/register`（spec §4.0）★★

    插件主动报到：交**贡献**，换回**凭证**。这是"插件重启后来找回自己是谁"的那条路。
    """
    _ = man  # manifest 在 load 时已校验过；这里只更新贡献，不重新校验
    entry = dict(entry)
    entry["contributions"] = contrib
    entry["registered_at"] = time.time()
    return entry


def config_for(entry: dict[str, Any], home: Path) -> dict[str, Any]:
    """读插件的 config.json（Core 拥有路径，原样交给插件；v0.1 无 schema 校验）。"""
    p = Path(entry.get("dir") or ".") / "config.json"
    text = core.read_text_shared(p).strip()   # 不存在 → 空串 → 当成"没有配置"
    if not text:
        return {}
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as e:
        raise ManifestError(f"config.json 不是合法 JSON：{e}") from e
    return raw if isinstance(raw, dict) else {}


def env_for(entry: dict[str, Any], home: Path) -> dict[str, str]:
    """Core 给插件的运行环境（不含任何秘密；token 走 register 响应，不进环境变量）。"""
    return {"ANP_PLUGIN_ID": entry["id"], "AGENT_ANP_HOME": str(home)}


def tools_dir(home: Path = core.DEFAULT_HOME) -> Path:
    return Path(home) / "plugins"


# --------------------------------------------------------------------------- #
# 工具名映射（spec §4.1）
# --------------------------------------------------------------------------- #
# MCP 工具名只允许 `^[a-zA-Z0-9_-]{1,64}$`，**不含点号**；而插件的规范工具名形如
# `alice.werewolf.create_game`。所以「规范名 → MCP 名」的映射由 Core 负责，
# 插件只写规范名。映射要确定：非法字符一律换成 `_`。
def mcp_tool_name(plugin_id: str, tool_name: str) -> str:
    raw = f"{plugin_id}.{tool_name}"

    def keep(ch: str) -> str:
        return ch if ((ch.isalnum() and ch.isascii()) or ch in "_-") else "_"

    return ("".join(keep(c) for c in raw))[:64] or "unnamed_tool"


def _tools_of(entry: dict[str, Any]) -> list[dict[str, Any]]:
    """注册条目里**允许暴露**的工具列表。

    ★`tools.register` 在这里执行★（2026-09-26 外部审计复现）：manifest 的权限闭集
    原来只有 `webui.panel` 有实际检查，`permissions: []` 的插件照样能把工具塞进
    agent 可见的 API（实测复现）。spec §5 写的是"调用点查表"，工具表就是调用点：
    没声明 `tools.register` 的插件，它的工具**根本不进注册表**——`/tools` 列不到、
    `/tool` 调不到，与"没有这些工具"无法区分（停用也是这个形态，语义一致）。
    """
    if "tools.register" not in (entry.get("permissions") or []):
        return []
    return list((entry.get("contributions") or {}).get("tools") or [])


def tool_registry(home: Path = core.DEFAULT_HOME) -> dict[str, dict[str, Any]]:
    """`{mcp工具名: {plugin_id, canonical, tool, state, base_url}}`。

    只有 **enabled 且已完成握手**的插件的工具才会进来。撞名时**后一个不上**
    （先注册先得），由 `tool_clashes()` 报出来——静默覆盖会让 agent 调到别人的工具。
    """
    out: dict[str, dict[str, Any]] = {}
    try:
        plugins = read_registry(home)
    except Exception:  # noqa: BLE001 - 注册表读不动就当没有插件工具
        return {}
    for pid, entry in sorted(plugins.items()):
        if entry.get("state") != "enabled" or entry.get("contributions") is None:
            continue
        for tool in _tools_of(entry):
            mcp_name = mcp_tool_name(pid, tool.get("name", ""))
            if mcp_name in out:
                continue
            out[mcp_name] = {"plugin_id": pid, "canonical": f"{pid}.{tool.get('name', '')}",
                             "tool": tool, "state": "enabled",
                             "base_url": entry.get("base_url")}
    return out


def tool_clashes(home: Path = core.DEFAULT_HOME) -> list[dict[str, str]]:
    """哪些插件工具因为撞名没进来（可观测：静默少一个工具比报错更难查）。"""
    out: list[dict[str, str]] = []
    seen: dict[str, str] = {}
    try:
        plugins = read_registry(home)
    except Exception:  # noqa: BLE001
        return []
    for pid, entry in sorted(plugins.items()):
        if entry.get("state") != "enabled" or entry.get("contributions") is None:
            continue
        for tool in _tools_of(entry):
            mcp_name = mcp_tool_name(pid, tool.get("name", ""))
            if mcp_name in seen:
                out.append({"mcp_name": mcp_name, "kept": seen[mcp_name], "dropped": pid})
                continue
            seen[mcp_name] = pid
    return out


def call_plugin_tool(plugin_id: str, entry: dict[str, Any], tool_name: str,
                     arguments: dict[str, Any], caller: str,
                     home: Path = core.DEFAULT_HOME,
                     timeout: float | None = None) -> dict[str, Any]:
    """把一次工具调用转发给插件的 `POST /anp/tool`。

    返回的 dict 已带 `mode: "plugin"` / `plugin: <id>`；失败时 `code` 是闭集之一
    （`endpoint.unreachable` / `plugin.error` / `timeout`）。
    ★插件端点永远不能替 hub 签收★：这里只转述结果，不碰任何消息状态。
    """
    base = str(entry.get("base_url") or "").strip()
    if not base:
        return {"ok": False, "mode": "plugin", "plugin": plugin_id,
                "code": "endpoint.unreachable", "error": "插件没有 base_url"}
    if entry.get("state") == "disabled":
        return {"ok": False, "mode": "plugin", "plugin": plugin_id,
                "code": "permission.denied", "error": f"插件 {plugin_id} 已停用"}
    budget = timeout if timeout is not None else float(
        os.environ.get("AGENT_ANP_PLUGIN_TIMEOUT") or 60)
    # 头里带**插件自己那一枚** agent token（不是 hub.secret——永久架构约束）
    ok, resp = _http_json(base.rstrip("/") + "/anp/tool",
                          {"name": tool_name, "arguments": arguments, "caller_identity": caller},
                          timeout=budget, token=entry.get("token"))
    if not ok:
        err = str(resp.get("error") or "")
        code = "timeout" if ("timed out" in err or "Timeout" in err) else "endpoint.unreachable"
        return {"ok": False, "mode": "plugin", "plugin": plugin_id, "code": code,
                "error": f"插件 {plugin_id} 端点不可达：{err}"}
    if not isinstance(resp, dict):
        return {"ok": False, "mode": "plugin", "plugin": plugin_id, "code": "plugin.error",
                "error": f"插件 {plugin_id} 的响应不是对象"}
    if resp.get("ok") is False:
        return {"ok": False, "mode": "plugin", "plugin": plugin_id, "code": "plugin.error",
                "error": str(resp.get("error") or "插件返回 ok:false"),
                "result": resp}
    out = dict(resp)
    out.setdefault("ok", True)
    out["mode"] = "plugin"
    out["plugin"] = plugin_id
    return out


# --------------------------------------------------------------------------- #
# 事件总线（spec §6：Core 只出 4 个事件）
# --------------------------------------------------------------------------- #
EVENTS = frozenset({"agent.online", "agent.offline",
                    "message.delivered", "message.undelivered"})


class EventBus:
    """插件事件的旁路投递：**绝不阻塞调用方，绝不把失败抛回消息路径**。

    设计（与 `core.AsyncLog` 同一个哲学）：
      * 有界队列，满了丢**最旧**的并计数——事件是旁路，主路永远优先；
      * 一个后台线程逐个 POST 到订阅者的 `events_url`；
      * 单个订阅者失败只记数，不影响别人、不影响主路；
      * ★插件端点永远不能替 hub 签收★：事件与消息投递状态完全无关。
    """

    def __init__(self, home: Path, capacity: int | None = None) -> None:
        self.home = Path(home)
        self.capacity = capacity if capacity is not None else int(
            os.environ.get("AGENT_ANP_EVENT_QUEUE") or 2000)
        self._q: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        self._cv = threading.Condition(self._lock)
        self._stop = threading.Event()
        self.stats = {"queued": 0, "dropped": 0, "delivered": 0, "failed": 0, "last_error": None}
        self._thread: threading.Thread | None = None

    def bind_home(self, home: Path) -> None:
        """把总线指到真正的 home。

        ★为什么需要★：hub 的 `HOME` 在 `main()` 解析 `--home` 之后才确定，而模块级单例在
        import 时只能拿到默认值。不重绑的话，事件总线会去读**真实信箱**的注册表——
        实测表现是"插件明明订阅了却收不到任何事件"，而 health 里一切正常（2026-09-22 抓到）。
        """
        self.home = Path(home)

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="anp-events", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        with self._cv:
            self._cv.notify_all()
        if self._thread:
            self._thread.join(timeout=timeout)

    def emit(self, type_: str, **payload: Any) -> bool:
        """发一个事件。返回是否入队（False = 被丢或类型未知）。
        同步调用方的唯一成本是一次加锁，绝不发 HTTP。"""
        if type_ not in EVENTS:
            return False
        subs = self.subscribers(type_)
        if not subs:
            return False
        ev = {"type": type_, "ts": time.time(), "home": str(self.home), **payload}
        with self._cv:
            if len(self._q) >= self.capacity:
                self._q.pop(0)                # 丢最旧的：新事件比旧事件更接近"现在"
                self.stats["dropped"] += 1
            self._q.append(ev)
            self.stats["queued"] += 1
            self._cv.notify_all()
        _ = subs
        return True

    def subscribers(self, type_: str) -> list[dict[str, Any]]:
        """订阅了某个事件的插件（enabled + 已握手 + 有 events_url）。"""
        out: list[dict[str, Any]] = []
        try:
            plugins = read_registry(self.home)
        except Exception:  # noqa: BLE001
            return []
        for pid, entry in sorted(plugins.items()):
            if entry.get("state") != "enabled" or entry.get("contributions") is None:
                continue
            events = (entry.get("contributions") or {}).get("events") or []
            url = (entry.get("contributions") or {}).get("events_url")
            if type_ in events and url:
                out.append({"plugin_id": pid, "events_url": url, "token": entry.get("token")})
        return out

    def subscribe(self, pid: str, type_: str) -> bool:
        """这个插件订阅这个事件吗（给 transition 检测用，避免无订阅者还去扫）。"""
        return bool(self.subscribers(type_))

    def _run(self) -> None:
        while not self._stop.is_set():
            with self._cv:
                while not self._q and not self._stop.is_set():
                    self._cv.wait(timeout=1.0)
                if self._stop.is_set() and not self._q:
                    return
                ev = self._q.pop(0)
            try:
                self._dispatch(ev)
            except Exception as e:  # noqa: BLE001 - 事件线程的任何异常都不许外溢
                self.stats["failed"] += 1
                self.stats["last_error"] = f"{type(e).__name__}: {e}"

    def _dispatch(self, ev: dict[str, Any]) -> None:
        for sub in self.subscribers(str(ev.get("type"))):
            ok, resp = _http_json(sub["events_url"], ev, timeout=5.0, token=sub.get("token"))
            if ok and isinstance(resp, dict) and resp.get("ok") is not False:
                self.stats["delivered"] += 1
            else:
                self.stats["failed"] += 1
                self.stats["last_error"] = str((resp or {}).get("error") or resp)[:200]


def state_transitions(home: Path) -> list[dict[str, Any]]:
    """账本里"刚离线"的身份（心跳超过 ONLINE_TTL 且此前没报过）。

    ★为什么在账本里做幂等，而不是内存里做★：hub 重启后内存态全丢，
    重启后再报一次 offline 比"永远不再报"好——而且真要严格一次，得把状态写进账本，
    那是给一个旁路功能加持久化负担。用 `offline_reported_at` 字段记在账本行里，
    重启后仍在，代价只是一次账本追加。
    """
    now = time.time()
    out = []
    ttl = core.ONLINE_TTL
    for name, row in core.agent_table(home).items():
        if name == "hub":
            continue
        last = float(row.get("ts") or 0)
        already = float(row.get("offline_reported_at") or 0)
        if now - last > ttl and (not already or already < last):
            out.append({"name": name, "last_seen": last, "now": now})
    return out


def mark_offline_reported(name: str, home: Path) -> None:
    """把"这个身份的离线已经报过了"记在账本里（幂等）。"""
    core.heartbeat(name, home, offline_reported_at=time.time())


def clear_offline_flag(name: str, home: Path) -> None:
    """身份又心跳了 → 清掉离线标记，下次真离线还要能报。"""
    core.heartbeat(name, home, offline_reported_at=None)


def call_plugin_panel(plugin_id: str, entry: dict[str, Any],
                      home: Path = core.DEFAULT_HOME,
                      timeout: float | None = None) -> tuple[int, str, str]:
    """代理插件的 `GET /anp/panel`。返回 `(HTTP 码, body, content-type)`。

    ★Core 只做转发★：不解析、不缓存、不注入。面板内容是插件的应用语义，
    Core 理解它就意味着 Core 开始理解应用（违反 §0.2 封板原则）。

    ★P0-2（2026-09-23）：GET **不带 body**★。原来发 `data=b"{}"`，
    而 GET 带 body 不是常规 HTTP：不少服务端根本不读 body，keep-alive 下残留字节
    会被当成下一条请求（错帧/卡死）；`BaseHTTPRequestHandler.do_GET` 也从不
    `rfile.read`，和带 Content-Length 的 GET 组合是经典卡死来源。

    ★超时可配，默认 2s★（原来是硬编码 10s）。管理 UI 不该被一个慢面板
    吊住 HTTP 线程——一次挂起就是浏览器侧好几秒空白。
    """
    if timeout is None:
        timeout = float(os.environ.get("AGENT_ANP_PANEL_TIMEOUT") or 2.0)
    base = str(entry.get("base_url") or "").strip()
    panel = str((entry.get("contributions") or {}).get("panel") or "/anp/panel")
    if not base:
        return 502, json.dumps({"ok": False, "error": "插件没有 base_url"}), "application/json"
    url = base.rstrip("/") + "/" + panel.lstrip("/")
    # ★GET 无 body★：不带 data、也不带 content-length
    headers = {}
    token = entry.get("token")
    if token:
        headers["x-mailbox-key"] = str(token)
    try:
        req = urllib.request.Request(url, headers=headers, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            ctype = str(resp.headers.get("content-type") or "application/json")
            return resp.status, resp.read().decode("utf-8", "replace"), ctype
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            body = json.dumps({"ok": False, "error": f"HTTP {e.code}"})
        return e.code, body, "application/json"
    except Exception as e:  # noqa: BLE001
        return 502, json.dumps({"ok": False, "code": "endpoint.unreachable",
                                "error": f"插件 {plugin_id} 面板不可达：{type(e).__name__}: {e}",
                                }), "application/json"


def panels(home: Path = core.DEFAULT_HOME) -> list[dict[str, Any]]:
    """所有提供面板的插件（enabled + 有 webui.panel 权限 + 声明了 panel）。"""
    out: list[dict[str, Any]] = []
    try:
        plugins = read_registry(home)
    except Exception:  # noqa: BLE001
        return []
    for pid, entry in sorted(plugins.items()):
        if entry.get("state") != "enabled" or entry.get("contributions") is None:
            continue
        panel = (entry.get("contributions") or {}).get("panel")
        if panel and "webui.panel" in (entry.get("permissions") or []):
            out.append({"plugin_id": pid, "name": entry.get("name") or pid,
                        "description": entry.get("description"), "panel": panel})
    return out
