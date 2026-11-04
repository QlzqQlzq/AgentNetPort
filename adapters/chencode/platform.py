"""ChenCode 平台语义 —— ★这一层是 ChenCode 专属知识，ANP 一概不知道★

依据（架构审阅 + 旧 mailbox_chen.py + Codex 参考实现）：

  运行时    %LOCALAPPDATA%\\Programs\\ChenCode\\resources\\runtime\\
  引擎入口  runtime/src/bootstrap-entry.ts   （bun.exe 拉起）
  凭证      env CHEN_API_KEY → 桌面 .credentials.json → wrapper chen.ps1
  会话      ~/.chen/projects/<slug>/<sessionId>.jsonl
  无头形态  bun bootstrap-entry.ts -p <prompt> --bare --output-format json

档位（平台知识，ANP 不感知）：
  A  交互/桌面可达（health / 进程）→ 落待取信摘要，由会话侧处理
  B  有 CLI（bun + bootstrap-entry）→ 拉起 headless 一轮
  C  都没有                       → 落盘待取信，会话侧周期收信

★它不知道 ANP 的存在★：入站是"信封的字段"，出站是"原生回信的字段"。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import string
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

DEFAULT_RUNTIME = (
    Path(os.environ.get("LOCALAPPDATA", ""))
    / "Programs" / "ChenCode" / "resources" / "runtime"
)
CLI_EFFORTS = {"low", "medium", "high", "xhigh", "max", "ultra"}
READ_TOOLS = ["Read", "Glob", "Grep"]
WRITE_TOOLS = READ_TOOLS + ["Write", "Edit", "Bash"]

INSTRUCTIONS = """你是 ChenCode，正在通过 ANP（AgentNetPort）与其它 agent 通信（dsh、codex、mimo…都可能给你写信）。

你现在处理一封入站 ANP 消息，请这样做：
1. 理解信里的诉求。这是无头唤醒会话：只做"读信内容 → 处理 → 给出结论"这一件事。
2. 除非信里明确要求改文件/跑命令，否则不要探索文件系统、不要 grep 仓库。
3. 你的**最终回复正文**会被 Adapter 包成 ANP 回信发回原信 from。
   请直接给出结论/答复/结果，带上关键结论、文件路径、下一步。
   不要讨论包装层，不要发明第二套消息协议。
4. 信里明确说不需要回复（no-reply）或只是通知时，给出简短知会即可。

入站消息（权威副本）：
"""


class ChenCodePlatform:
    """ChenCode 这一侧的一切。"""

    def __init__(self, identity: str = "chencode", data_dir: Path | None = None):
        self.identity = identity
        self.workspace = os.environ.get("AGENT_ANP_CHEN_CWD") or os.environ.get(
            "AGENT_MAILBOX_CHEN_CWD") or str(Path.cwd())
        if not Path(self.workspace).is_dir():
            self.workspace = str(Path.cwd())
        self.runtime = Path(
            os.environ.get("AGENT_ANP_CHEN_RUNTIME")
            or os.environ.get("AGENT_MAILBOX_CHEN_RUNTIME")
            or DEFAULT_RUNTIME
        )
        self.chen_home = Path(
            os.environ.get("AGENT_ANP_CHEN_HOME")
            or os.environ.get("CHEN_CONFIG_DIR")
            or (Path.home() / ".chen")
        )
        self.health_port = int(
            os.environ.get("AGENT_ANP_CHEN_HEALTH_PORT")
            or os.environ.get("AGENT_MAILBOX_CHEN_HEALTH_PORT")
            or 43127
        )
        self.health_url = f"http://127.0.0.1:{self.health_port}/process/health"
        self.timeout = float(
            os.environ.get("AGENT_ANP_CHEN_TIMEOUT")
            or os.environ.get("AGENT_MAILBOX_CHEN_TIMEOUT")
            or 900
        )
        self.max_turns = int(
            os.environ.get("AGENT_ANP_CHEN_MAX_TURNS")
            or os.environ.get("AGENT_MAILBOX_CHEN_MAX_TURNS")
            or 30
        )
        # ★平台状态放自己的目录★（Spec 缺口：不要写进信箱 <HOME>）
        self.data_dir = data_dir or Path(__file__).resolve().parent / "data"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.pending_json = self.data_dir / "chencode-pending.json"
        self.pending_md = self.data_dir / "chencode-pending.md"
        self.work_path = self.data_dir / "chencode-work.jsonl"
        self.sessions_path = self.data_dir / "chencode-sessions.json"
        self.credentials_path = self.data_dir / "chencode-credentials.json"
        self._log = self.data_dir / "chencode-adapter.log"
        self._work_lock = threading.Lock()

    # ---- 日志 ----
    def log(self, text: str) -> None:
        try:
            with self._log.open("a", encoding="utf-8") as fp:
                fp.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {text}\n")
        except OSError:
            pass

    # ---- 凭据（agent token 本地缓存，⑦a 恢复用）----
    def save_credentials(self, agent_key: str) -> None:
        payload = json.dumps(
            {"identity": self.identity, "agent_key": agent_key},
            ensure_ascii=False, indent=2) + "\n"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        try:
            self.credentials_path.write_text(payload, encoding="utf-8")
        except PermissionError:
            try:
                self.credentials_path.unlink()
            except OSError:
                pass
            self.credentials_path.write_text(payload, encoding="utf-8")
        self._tighten_secret(self.credentials_path)

    @staticmethod
    def _tighten_secret(path: Path) -> None:
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        if os.name != "nt":
            return
        try:
            users: list[str] = []
            try:
                who = subprocess.check_output(
                    ["whoami"], capture_output=True, timeout=5,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                ).decode("utf-8", "replace").strip()
                if who:
                    users.append(who)
            except Exception:  # noqa: BLE001
                pass
            user = os.environ.get("USERNAME") or ""
            domain = os.environ.get("USERDOMAIN") or ""
            if user and domain:
                users.append(f"{domain}\\{user}")
            if user:
                users.append(user)
            grants: list[str] = []
            for u in dict.fromkeys(users):
                grants.extend(["/grant:r", f"{u}:F"])
            if not grants:
                return
            subprocess.run(
                ["icacls", str(path), "/inheritance:r", *grants],
                capture_output=True, timeout=10,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except Exception:  # noqa: BLE001
            pass

    def agent_key(self) -> str:
        try:
            data = json.loads(self.credentials_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return ""
        return str((data or {}).get("agent_key") or "")

    # ---- 平台探测 ----
    def find_bun(self) -> str | None:
        for cand in (
            os.environ.get("AGENT_ANP_CHEN_BUN") or "",
            os.environ.get("AGENT_MAILBOX_CHEN_BUN") or "",
            str(self.runtime / "bun.exe"),
            str(self.runtime / "bun"),
            shutil.which("bun") or "",
        ):
            if cand and Path(cand).is_file():
                return cand
        return None

    def find_entry(self) -> str | None:
        entry = self.runtime / "src" / "bootstrap-entry.ts"
        return str(entry) if entry.is_file() else None

    def find_git_bash(self) -> str | None:
        """Find Git Bash even when the Hub inherited a stale or reduced PATH."""
        candidates = [
            os.environ.get("AGENT_ANP_CHEN_GIT_BASH") or "",
            os.environ.get("AGENT_MAILBOX_CHEN_GIT_BASH") or "",
            os.environ.get("CLAUDE_CODE_GIT_BASH_PATH") or "",
        ]
        roots: list[Path] = []

        def add_git_root(git_path: str | Path) -> None:
            path = Path(git_path)
            if path.name.casefold() == "git.exe" and path.parent.name.casefold() in {
                    "cmd", "bin"}:
                roots.append(path.parent.parent)
            elif path.name.casefold() == "git.exe":
                roots.append(path.parent)

        git = shutil.which("git")
        if git:
            add_git_root(git)
        for entry in os.environ.get("PATH", "").split(os.pathsep):
            if not entry:
                continue
            directory = Path(entry)
            if (directory / "git.exe").is_file():
                add_git_root(directory / "git.exe")
            elif directory.name.casefold() in {"cmd", "bin"}:
                roots.append(directory.parent)

        if os.name == "nt":
            try:
                import winreg

                locations = (
                    (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\GitForWindows",
                     "InstallPath"),
                    (winreg.HKEY_CURRENT_USER, r"SOFTWARE\GitForWindows",
                     "InstallPath"),
                    (winreg.HKEY_LOCAL_MACHINE,
                     r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\Git_is1",
                     "InstallLocation"),
                )
                for hive, key, value_name in locations:
                    try:
                        with winreg.OpenKey(hive, key) as handle:
                            value, _ = winreg.QueryValueEx(handle, value_name)
                        if value:
                            roots.append(Path(str(value)))
                    except OSError:
                        continue
            except ImportError:
                pass

            for variable in ("ProgramFiles", "ProgramFiles(x86)"):
                base = os.environ.get(variable)
                if base:
                    roots.append(Path(base) / "Git")

            # A desktop shortcut or an already-running Hub can inherit a
            # reduced PATH, and portable Git installs are often not present
            # in the uninstall registry.  Probe the conventional per-drive
            # ``<drive>:\Git`` locations as a last-resort discovery path.
            # This keeps the adapter self-contained without requiring a
            # change to ChenCode or to the user's global environment.
            for drive in string.ascii_uppercase:
                roots.append(Path(f"{drive}:\\Git"))

        for root in roots:
            candidates.extend((str(root / "bin" / "bash.exe"),
                               str(root / "usr" / "bin" / "bash.exe")))
        seen: set[str] = set()
        for candidate in candidates:
            candidate = os.path.expandvars(os.path.expanduser(candidate.strip()))
            normalized = os.path.normcase(os.path.abspath(candidate)) if candidate else ""
            if not candidate or normalized in seen:
                continue
            seen.add(normalized)
            if Path(candidate).is_file():
                return candidate
        return None

    def probe_health(self, timeout: float = 1.2) -> bool:
        try:
            with urllib.request.urlopen(
                    urllib.request.Request(self.health_url, method="GET"),
                    timeout=timeout) as resp:
                return 200 <= getattr(resp, "status", 200) < 500
        except Exception:  # noqa: BLE001
            return False

    def desktop_running(self) -> tuple[bool, list[int]]:
        pids: list[int] = []
        try:
            proc = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 "Get-Process -Name 'ChenCode','bun' -ErrorAction SilentlyContinue "
                 "| Select-Object -ExpandProperty Id"],
                capture_output=True, timeout=1.5, text=True,
                encoding="utf-8", errors="replace",
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            for line in (proc.stdout or "").split():
                if line.strip().isdigit():
                    pids.append(int(line.strip()))
        except Exception:  # noqa: BLE001
            return False, []
        return len(pids) > 0, pids

    def detect_tier(self) -> dict[str, Any]:
        """选择当前真正可执行的入口。

        健康端点优先代表可交互桌面；没有健康端点时，优先选择可验证的
        headless CLI。仅凭进程名不能证明桌面有可注入的会话，因此把进程
        存在作为最后的 A 档兜底，避免把可用的 B 档误降级成待取信。
        """
        bun = self.find_bun()
        entry = self.find_entry()
        if self.probe_health():
            return {"tier": "A", "label": "inject-pending",
                    "detail": f"health/desktop 可达: {self.health_url}",
                    "cli": bun, "entry": entry}
        if bun and entry:
            return {"tier": "B", "label": "headless-cli",
                    "detail": f"ChenCode CLI: {bun} + {entry}",
                    "cli": bun, "entry": entry}
        if self.desktop_running()[0]:
            return {"tier": "A", "label": "inject-pending",
                    "detail": f"桌面进程可达（无健康端点）: {self.health_url}",
                    "cli": bun, "entry": entry}
        return {"tier": "C", "label": "queue-poll",
                "detail": "无常驻端点、无 CLI：落盘待取信",
                "cli": None, "entry": None}

    def caps_for(self, tier: str) -> dict[str, bool]:
        """H-2：caps 是能力广告，必须与真实行为一致。

        A 档实际是「落 pending 摘要、会话侧再取」——并没有注入已存在的活跃会话，
        所以 inject_session 必须是 false（queue/followup 语义）。
        """
        table = {
            # A：落摘要 + 会话侧处理；可 spawn_cwd / resume，但不注入活跃会话
            "A": {"inject_session": False, "spawn_cwd": True,
                  "resume_session": True, "queue": True},
            "B": {"inject_session": False, "spawn_cwd": True,
                  "resume_session": True, "queue": True},
            "C": {"inject_session": False, "spawn_cwd": False,
                  "resume_session": False, "queue": True},
        }
        return dict(table.get(tier, table["C"]))

    def process_api_config(self) -> dict[str, Any]:
        # ChenCode desktop builds do not consistently expose 43127.  The
        # adapter health surface is always reachable while the plugin is
        # alive; retain the desktop URL as diagnostic metadata.
        port = os.environ.get("ANP_PLUGIN_PORT") or ""
        url = (f"http://127.0.0.1:{port}/anp/health" if port
               else self.health_url)
        return {"kind": "http", "url": url, "timeout_sec": 1.5,
                "platform_url": self.health_url}

    # ---- API 凭证候选（平台侧，非 ANP token）----
    def read_desktop_credentials(self) -> dict[str, str] | None:
        override = os.environ.get("AGENT_ANP_CHEN_CREDENTIALS") or os.environ.get(
            "AGENT_MAILBOX_CHEN_CREDENTIALS")
        path = Path(override) if override else (
            Path(os.environ.get("APPDATA", "")) / "ChenCode" / "data" / ".credentials.json")
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        custom = data.get("customApiEndpoint") if isinstance(data, dict) else None
        if not isinstance(custom, dict):
            return None
        key = str(custom.get("apiKey") or "").strip()
        base = str(custom.get("baseURL") or "").strip()
        if not (key and base):
            return None
        effort = str(custom.get("effortLevel") or "").strip().lower()
        if effort not in CLI_EFFORTS:
            effort = "low" if effort == "none" else ""
        return {"key": key, "base_url": base,
                "model": str(custom.get("model") or "").strip(),
                "effort": effort}

    def read_key_from_wrapper(self) -> str:
        """M-2：只读显式指定的 wrapper 或插件旁同级文件，不猜仓库外路径。"""
        pattern = re.compile(
            r'^\s*\$env:CHEN_API_KEY\s*=\s*["\']([^"\']+)["\']', re.IGNORECASE)
        candidates = []
        override = (
            os.environ.get("AGENT_ANP_CHEN_WRAPPER")
            or os.environ.get("AGENT_MAILBOX_CHEN_WRAPPER")
            or ""
        ).strip()
        if override:
            candidates.append(Path(override))
        # 仅插件目录旁的 chen.ps1（部署时随插件交付）；不再上溯仓库父级
        candidates.append(Path(__file__).resolve().parent / "chen.ps1")
        for path in candidates:
            if not path.is_file():
                continue
            try:
                for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
                    match = pattern.match(line)
                    if match:
                        return match.group(1).strip()
            except OSError:
                continue
        return ""

    def credential_candidates(self) -> list[dict[str, str]]:
        desktop = self.read_desktop_credentials()
        out: list[dict[str, str]] = []
        env_key = (
            os.environ.get("CHEN_API_KEY")
            or os.environ.get("AGENT_ANP_CHEN_API_KEY")
            or os.environ.get("AGENT_MAILBOX_CHEN_API_KEY")
            or ""
        ).strip()
        if env_key:
            env_base = (
                os.environ.get("ANTHROPIC_BASE_URL")
                or os.environ.get("AGENT_ANP_CHEN_BASE_URL")
                or os.environ.get("AGENT_MAILBOX_CHEN_BASE_URL")
                or "https://botcf.com"
            ).strip()
            model, effort = self._profile_for_base(env_base, desktop)
            out.append({"source": "env", "key": env_key, "base_url": env_base,
                        "model": model, "effort": effort})
        if desktop:
            out.append({"source": "desktop", "key": desktop["key"],
                        "base_url": desktop["base_url"],
                        "model": desktop["model"] or "claude-opus-5",
                        "effort": desktop["effort"] or "high"})
        wrapper_key = self.read_key_from_wrapper()
        if wrapper_key and wrapper_key != env_key:
            # M-3：wrapper 兜底的 base/model 全部可环境变量覆盖，不再写死单一默认
            base = (
                os.environ.get("ANTHROPIC_BASE_URL")
                or os.environ.get("AGENT_ANP_CHEN_BASE_URL")
                or os.environ.get("AGENT_MAILBOX_CHEN_BASE_URL")
                or "https://botcf.com"
            ).strip()
            model, effort = self._profile_for_base(base, desktop)
            out.append({"source": "wrapper", "key": wrapper_key,
                        "base_url": base, "model": model, "effort": effort})
        seen: set[str] = set()
        unique: list[dict[str, str]] = []
        for cand in out:
            if cand["key"] in seen:
                continue
            seen.add(cand["key"])
            unique.append(cand)
        return unique

    def _profile_for_base(self, base_url: str, desktop: dict[str, str] | None) -> tuple[str, str]:
        forced = (
            os.environ.get("AGENT_ANP_CHEN_MODEL")
            or os.environ.get("AGENT_MAILBOX_CHEN_MODEL")
            or ""
        )
        default_model = os.environ.get("AGENT_MAILBOX_CHEN_MODEL") or "claude-opus-5"
        default_effort = os.environ.get("AGENT_MAILBOX_CHEN_EFFORT") or "high"
        if forced:
            return forced, default_effort
        if desktop and desktop["base_url"].rstrip("/") == base_url.rstrip("/"):
            return desktop["model"] or default_model, desktop["effort"] or default_effort
        return default_model, default_effort

    def flag(self, name: str, default: str = "") -> str:
        """运行时开关：环境变量优先，其次 data/adapter-flags.json（验收可写）。

        插件是独立进程，测试改 os.environ 不会传进去；flags 文件可以。
        """
        env = os.environ.get(name, "")
        if env:
            return env
        try:
            data = json.loads(
                (self.data_dir / "adapter-flags.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return default
        if isinstance(data, dict) and name in data:
            return str(data.get(name) if data.get(name) is not None else default)
        return default

    def flag_on(self, name: str) -> bool:
        return self.flag(name).lower() in ("1", "true", "yes", "on")

    def pick_credential(self) -> dict[str, str] | None:
        if self.flag_on("AGENT_ANP_CHEN_SKIP_PREFLIGHT"):
            self.log("skip preflight (AGENT_ANP_CHEN_SKIP_PREFLIGHT)")
            cands = self.credential_candidates()
            return cands[0] if cands else None
        for cand in self.credential_candidates():
            ok, why = self.preflight(cand)
            self.log(f"凭证候选 {cand['source']}（{cand['model']} @ {cand['base_url']}）："
                     + ("可用" if ok else f"不可用（{why}）"))
            if ok:
                return cand
        return None

    def preflight(self, cand: dict[str, str]) -> tuple[bool, str]:
        base = cand["base_url"].rstrip("/")
        url = base + "/messages" if base.endswith("/v1") else base + "/v1/messages"
        body = json.dumps({
            "model": cand["model"],
            "max_tokens": 1,
            "messages": [{"role": "user", "content": "ping"}],
        }).encode("utf-8")
        req = urllib.request.Request(url, data=body, method="POST", headers={
            "content-type": "application/json",
            "x-api-key": cand["key"],
            "anthropic-version": "2023-06-01",
        })
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                return resp.status == 200, f"HTTP {resp.status}"
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:100].replace("\n", " ")
            return False, f"HTTP {e.code} {detail}"
        except Exception as e:  # noqa: BLE001
            return False, f"{type(e).__name__}: {e}"

    # ---- 待办持久化（S3-A）----
    def enqueue_work(self, envelope: Any, native: dict[str, Any],
                     route: dict[str, Any]) -> str:
        pending_id = str(getattr(envelope, "id", "") or f"work-{int(time.time()*1000)}")
        # M-4：并发 wake 下读-判-写必须串行，同一 envelope.id 只入队一次
        with self._work_lock:
            for row in self._load_work():
                if str(row.get("pending_id") or "") == pending_id:
                    return pending_id
            row = {
                "pending_id": pending_id,
                "envelope_id": str(getattr(envelope, "id", "") or ""),
                "from": str(getattr(envelope, "from_", "") or ""),
                "correlation_id": str(getattr(envelope, "correlation_id", "") or ""),
                "route": route,
                "native": native,
                "state": "queued",
                "at": time.time(),
            }
            self.data_dir.mkdir(parents=True, exist_ok=True)
            with self.work_path.open("a", encoding="utf-8") as fp:
                fp.write(json.dumps(row, ensure_ascii=False) + "\n")
        return pending_id

    def finish_work(self, pending_id: str, *, ok: bool, detail: str = "") -> None:
        row = {
            "pending_id": str(pending_id or ""),
            "state": "done" if ok else "failed",
            "ok": bool(ok),
            "detail": str(detail or "")[:500],
            "at": time.time(),
        }
        try:
            with self._work_lock:
                with self.work_path.open("a", encoding="utf-8") as fp:
                    fp.write(json.dumps(row, ensure_ascii=False) + "\n")
        except OSError:
            self.log(f"finish_work write failed for {pending_id}")

    def _load_work(self) -> list[dict[str, Any]]:
        if not self.work_path.is_file():
            return []
        rows: list[dict[str, Any]] = []
        try:
            for line in self.work_path.read_text(encoding="utf-8").splitlines():
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(item, dict):
                    rows.append(item)
        except OSError:
            return []
        return rows

    # ---- 会话 ----
    def record_session(self, session_id: str, workspace: str, *,
                       running: bool = False) -> None:
        session_id = str(session_id or "").strip()
        if not session_id:
            return
        rows = [r for r in self._load_sessions()
                if str(r.get("sessionId") or "") != session_id]
        now = time.time()
        rows.append({
            "sessionId": session_id, "cwd": workspace,
            "updatedAt": now, "lastPromptAt": now,
            "running": bool(running),
            "title": f"chencode/{session_id[:8]}",
        })
        self.sessions_path.write_text(
            json.dumps({"sessions": rows[-100:]}, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8")

    def _load_sessions(self) -> list[dict[str, Any]]:
        try:
            data = json.loads(self.sessions_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        rows = data.get("sessions") if isinstance(data, dict) else []
        return list(rows) if isinstance(rows, list) else []

    def session_roster(self) -> list[dict[str, Any]]:
        return list(self._load_sessions()[-100:])

    def route_from_stamp(self, envelope: Any) -> dict[str, Any]:
        """S1 Layer A+：只执行 Core 的 meta.session 章，不自建决策。"""
        meta = envelope.meta if isinstance(getattr(envelope, "meta", None), dict) else {}
        stamp = meta.get("session") if isinstance(meta.get("session"), dict) else {}
        mode = str(stamp.get("mode") or stamp.get("action") or "").strip().lower()
        session_id = str(stamp.get("session_id") or "").strip()
        workspace = (
            str(stamp.get("workspace") or "").strip()
            or str(meta.get("to_workspace") or "").strip()
            or self.workspace
        )
        if not Path(workspace).is_dir():
            workspace = self.workspace
        if mode == "reuse" and session_id:
            return {"mode": "reuse", "session_id": session_id,
                    "workspace": workspace, "reason": "meta.session-reuse"}
        return {"mode": "new", "session_id": "",
                "workspace": workspace, "reason": "meta.session-new"}

    # ---- 翻译 ----
    def to_native(self, envelope: Any) -> dict[str, Any]:
        meta = envelope.meta if isinstance(getattr(envelope, "meta", None), dict) else {}
        return {
            "text": envelope.body,
            "sender": envelope.from_,
            "subject": envelope.subject,
            "kind": getattr(envelope, "kind", "message"),
            "message_id": getattr(envelope, "id", ""),
            "correlation_id": getattr(envelope, "correlation_id", ""),
            "reply_to": getattr(envelope, "reply_to", ""),
            "context_id": str(meta.get("context_id") or ""),
            "to_workspace": str(meta.get("to_workspace") or ""),
            "from_workspace": str(meta.get("from_workspace") or ""),
            "need_reply": meta.get("need_reply"),
        }

    def from_native(self, platform_msg: dict[str, Any]) -> dict[str, Any]:
        return {"body": str(platform_msg.get("body") or platform_msg.get("text") or "").strip()}

    def build_prompt(self, native: dict[str, Any]) -> str:
        return "\n".join([
            INSTRUCTIONS,
            "```json",
            json.dumps(native, ensure_ascii=False, indent=2),
            "```",
        ])

    def should_reply(self, envelope: Any) -> bool:
        """Errata S4：need_reply === true 才要求义务回信；缺省 = 知会。"""
        meta = envelope.meta if isinstance(getattr(envelope, "meta", None), dict) else {}
        return meta.get("need_reply") is True

    def reply_meta(self, envelope: Any, session_id: str, workspace: str) -> dict[str, Any]:
        incoming = envelope.meta if isinstance(getattr(envelope, "meta", None), dict) else {}
        out: dict[str, Any] = {"from_workspace": workspace}
        if incoming.get("context_id"):
            out["context_id"] = incoming["context_id"]
        if session_id:
            out["reply_to_session"] = session_id
        return out

    # ---- 待取信摘要（A/C 档产出物）----
    def write_pending(self, messages: list[dict], tier: dict, wake_json: Any) -> None:
        self.pending_json.write_text(json.dumps({
            "identity": self.identity, "tier": tier, "at": time.time(),
            "unread": len(messages), "messages": messages, "wake_json": wake_json,
            "poll_hint": "在 ChenCode 会话里处理；或用 ANP receive 取未读",
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        lines = [
            f"# chencode 待取信摘要（{time.strftime('%Y-%m-%d %H:%M:%S')}）", "",
            f"- 推送档位: **{tier['tier']}**（{tier['label']}）",
            f"- 说明: {tier['detail']}",
            f"- 未读: {len(messages)} 封", "",
        ]
        if messages:
            lines += ["| id | from | subject | need_reply |", "| --- | --- | --- | --- |"]
            for m in messages:
                meta = m.get("meta") if isinstance(m.get("meta"), dict) else {}
                need = meta.get("need_reply") is True
                subj = str(m.get("subject") or "").replace("|", "\\|")
                lines.append(
                    f"| {m.get('id')} | {m.get('from')} | {subj} | "
                    f"{'是' if need else '否'} |")
            lines += ["", "完整正文请用 ANP 的 receive 读取。"]
        else:
            lines.append("当前没有未读。")
        if wake_json:
            lines += ["", "## 触发唤醒的投递信息", "", "```json",
                      json.dumps(wake_json, ensure_ascii=False, indent=2), "```"]
        self.pending_md.write_text("\n".join(lines) + "\n", encoding="utf-8")

    # ---- B 档：拉起 headless CLI ----
    def _command(self, native: dict[str, Any], route: dict[str, Any],
                 cred: dict[str, str]) -> list[str]:
        bun = self.find_bun()
        entry = self.find_entry()
        if not bun or not entry:
            raise FileNotFoundError("ChenCode CLI not found (bun.exe / bootstrap-entry.ts)")
        tools = WRITE_TOOLS if os.environ.get("AGENT_ANP_CHEN_WRITE") == "1" else READ_TOOLS
        prompt = self.build_prompt(native)
        cmd = [
            bun, entry,
            "-p", prompt,
            "--bare",
            "--model", cred["model"],
            "--add-dir", route["workspace"],
            "--allowedTools", *tools,
            "--max-turns", str(self.max_turns),
            "--output-format", "json",
        ]
        if cred.get("effort"):
            cmd.extend(["--effort", cred["effort"]])
        if route.get("mode") == "reuse" and route.get("session_id"):
            cmd.extend(["--resume", str(route["session_id"])])
        return cmd

    @staticmethod
    def parse_result(stdout: str) -> tuple[str, str, str]:
        """从 headless JSON 里取 (session_id, reply_text, subtype)。"""
        session_id, reply, subtype = "", "", ""
        try:
            data = json.loads(stdout or "{}")
        except json.JSONDecodeError:
            return session_id, (stdout or "").strip()[:4000], subtype
        if not isinstance(data, dict):
            return session_id, (stdout or "").strip()[:4000], subtype
        session_id = str(data.get("session_id") or "")
        reply = str(data.get("result") or "")
        subtype = str(data.get("subtype") or "")
        return session_id, reply.strip(), subtype

    def run(self, native: dict[str, Any], route: dict[str, Any]) -> dict[str, Any]:
        # 测试/演示：用固定回信 stub，不拉起真实 CLI（M-1 成功路径 E2E 用）
        stub = self.flag("AGENT_ANP_CHEN_STUB_REPLY").strip()
        if stub:
            session_id = str(route.get("session_id") or f"stub-{int(time.time()*1000)}")
            self.record_session(session_id, route.get("workspace") or self.workspace)
            return {"ok": True, "returncode": 0, "session_id": session_id,
                    "reply": stub, "subtype": "success", "stderr": ""}
        cred = self.pick_credential()
        if not cred:
            return {
                "ok": False, "error": "no usable API credential",
                "session_id": route.get("session_id") or "",
                "reply": "",
            }
        # 验收/离线：跳过真实 CLI，快速返回失败以便测 S3 补偿
        if (self.flag_on("AGENT_ANP_CHEN_SKIP_PREFLIGHT")
                and self.flag_on("AGENT_ANP_CHEN_FORCE_FAIL")):
            return {
                "ok": False, "error": "forced failure (AGENT_ANP_CHEN_FORCE_FAIL)",
                "session_id": route.get("session_id") or "",
                "reply": "",
            }
        argv = self._command(native, route, cred)
        env = {
            **os.environ,
            "CHEN_API_KEY": cred["key"],
            "ANTHROPIC_BASE_URL": cred["base_url"],
            "AGENT_ANP_SELF": self.identity,
            "AGENT_ANP_CHEN_ADAPTER": "1",
        }
        env.setdefault("ANTHROPIC_API_KEY", cred["key"])
        if os.name == "nt":
            git_bash = self.find_git_bash()
            if not git_bash:
                error = ("Git Bash not found; install Git for Windows or set "
                         "AGENT_ANP_CHEN_GIT_BASH / CLAUDE_CODE_GIT_BASH_PATH")
                self.log(error)
                return {"ok": False, "error": error,
                        "session_id": route.get("session_id") or "", "reply": ""}
            env["CLAUDE_CODE_GIT_BASH_PATH"] = git_bash
        self.log(f"launch mode={route['mode']} cwd={route['workspace']} "
                 f"cred={cred['source']} model={cred['model']}"
                 + (f" git_bash={env['CLAUDE_CODE_GIT_BASH_PATH']}"
                    if os.name == "nt" else ""))
        try:
            proc = subprocess.run(
                argv,
                cwd=route["workspace"] if Path(route["workspace"]).is_dir() else self.workspace,
                env=env, stdin=subprocess.DEVNULL,
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=self.timeout,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except subprocess.TimeoutExpired:
            self.log(f"ChenCode timeout after {self.timeout}s")
            return {"ok": False, "error": f"timeout after {self.timeout}s",
                    "session_id": route.get("session_id") or "", "reply": ""}
        session_id, reply, subtype = self.parse_result(proc.stdout or "")
        if not session_id:
            session_id = str(route.get("session_id") or "")
        if session_id:
            self.record_session(session_id, route["workspace"], running=False)
        # 把最后一次输出留痕
        try:
            (self.data_dir / "chencode-last-output.txt").write_text(
                (proc.stdout or "")[-20000:], encoding="utf-8")
        except OSError:
            pass
        ok = proc.returncode == 0 and subtype != "error_max_turns"
        if not ok:
            self.log(f"chen exit={proc.returncode} subtype={subtype} "
                     f"stderr={(proc.stderr or '')[-400:]}")
        return {
            "ok": ok, "returncode": proc.returncode,
            "session_id": session_id, "reply": reply, "subtype": subtype,
            "stderr": (proc.stderr or "")[-2000:],
        }
