"""Codex CLI platform semantics for the ANP adapter.

This module knows how to start or resume Codex sessions.  It deliberately
does not know about ANP HTTP endpoints or Core implementation details.
"""
from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any


class CodexPlatform:
    """Translate ANP work into Codex CLI invocations and native session state."""

    def __init__(self, identity: str = "codex", data_dir: Path | None = None):
        self.identity = identity
        self.workspace = self._default_workspace()
        self.codex_bin = self._find_codex()
        self.code_home = Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex"))
        self.data_dir = data_dir or (Path(__file__).resolve().parent / "data")
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.credentials_path = self.data_dir / "codex-credentials.json"
        self.sessions_path = self.data_dir / "codex-sessions.json"
        self.log_path = self.data_dir / "codex-adapter.log"
        self._work_lock = threading.RLock()

    @staticmethod
    def _default_workspace() -> str:
        value = (os.environ.get("ANP_CODEX_WORKSPACE")
                 or os.environ.get("AGENT_ANP_CODEX_CWD")
                 or os.getcwd())
        path = Path(value).expanduser()
        return str(path if path.is_dir() else Path.cwd())

    @staticmethod
    def _find_codex() -> str | None:
        candidates = [
            os.environ.get("ANP_CODEX_BIN") or "",
            os.environ.get("CODEX_CLI_PATH") or "",
            shutil.which("codex") or "",
        ]
        for candidate in candidates:
            if candidate and Path(candidate).is_file():
                return str(Path(candidate))
        return None

    def log(self, message: str) -> None:
        try:
            with self.log_path.open("a", encoding="utf-8") as fp:
                fp.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}\n")
        except OSError:
            pass

    def load_credentials(self) -> dict[str, Any]:
        try:
            data = json.loads(self.credentials_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def save_credentials(self, agent_key: str) -> None:
        payload = json.dumps({"identity": self.identity, "agent_key": agent_key},
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
        """M-1：agent token 文件收紧到本人可读（Windows 用 whoami + icacls）。"""
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        if os.name != "nt":
            return
        try:
            import subprocess
            users = []
            try:
                who = subprocess.check_output(
                    ["whoami"], capture_output=True, timeout=5,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)
                ).decode("utf-8", "replace").strip()
                if who:
                    users.append(who)
            except Exception:  # noqa: BLE001
                pass
            user = os.environ.get("USERNAME") or ""
            dom = os.environ.get("USERDOMAIN") or ""
            if user and dom:
                users.append(f"{dom}\\{user}")
            if user:
                users.append(user)
            grants = []
            for u in dict.fromkeys(users):
                grants.extend(["/grant:r", f"{u}:F"])
            if not grants:
                return
            r = subprocess.run(
                ["icacls", str(path), "/inheritance:r", *grants],
                capture_output=True, timeout=10,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if r.returncode != 0:
                subprocess.run(["icacls", str(path), "/inheritance:e"],
                               capture_output=True, timeout=10,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except Exception:  # noqa: BLE001
            pass

    def agent_key(self) -> str:
        return str(self.load_credentials().get("agent_key") or "")

    def _load_local_sessions(self) -> list[dict[str, Any]]:
        try:
            data = json.loads(self.sessions_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        rows = data.get("sessions") if isinstance(data, dict) else []
        return list(rows) if isinstance(rows, list) else []

    def record_session(self, session_id: str, workspace: str,
                       *, running: bool = False) -> None:
        session_id = str(session_id or "").strip()
        if not session_id:
            return
        rows = [row for row in self._load_local_sessions()
                if str(row.get("sessionId") or "") != session_id]
        now = time.time()
        rows.append({"sessionId": session_id, "cwd": workspace,
                     "updatedAt": now, "lastPromptAt": now,
                     "running": bool(running)})
        rows = rows[-100:]
        self.sessions_path.write_text(
            json.dumps({"sessions": rows}, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8")

    def _native_sessions(self) -> list[dict[str, Any]]:
        index = self.code_home / "session_index.jsonl"
        if not index.is_file():
            return []
        rows: list[dict[str, Any]] = []
        try:
            lines = index.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return []
        for line in lines[-200:]:
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(item, dict):
                continue
            session_id = str(item.get("id") or item.get("session_id") or "").strip()
            if not session_id:
                continue
            rows.append({
                "sessionId": session_id,
                "cwd": str(item.get("cwd") or self.workspace),
                "updatedAt": item.get("updated_at") or item.get("updatedAt"),
                "lastPromptAt": item.get("updated_at") or item.get("updatedAt"),
                "running": False,
                "title": item.get("thread_name") or item.get("title"),
            })
        return rows

    def session_roster(self) -> list[dict[str, Any]]:
        merged: dict[str, dict[str, Any]] = {}
        for row in self._load_local_sessions() + self._native_sessions():
            sid = str(row.get("sessionId") or "").strip()
            if sid:
                merged[sid] = {"sessionId": sid,
                               "cwd": row.get("cwd") or self.workspace,
                               "updatedAt": row.get("updatedAt"),
                               "lastPromptAt": row.get("lastPromptAt"),
                               "running": bool(row.get("running")),
                               "title": row.get("title")}
        return list(merged.values())[-100:]

    def to_native(self, envelope: Any) -> dict[str, Any]:
        meta = envelope.meta if isinstance(envelope.meta, dict) else {}
        return {
            "text": envelope.body,
            "sender": envelope.from_,
            "subject": envelope.subject,
            "kind": envelope.kind,
            "message_id": envelope.id,
            "correlation_id": envelope.correlation_id,
            "context_id": str(meta.get("context_id") or ""),
            "to_workspace": str(meta.get("to_workspace") or ""),
            "from_workspace": str(meta.get("from_workspace") or ""),
        }

    def from_native(self, platform_msg: Any) -> dict[str, Any]:
        """Codex `agent_message` → ANP 回信字段（Spec：平台知识只出现在翻译函数）。"""
        if not isinstance(platform_msg, dict):
            platform_msg = {"text": str(platform_msg or "")}
        text = str(platform_msg.get("text") or platform_msg.get("body") or "").strip()
        return {"body": text, "kind": "reply",
                "subject": str(platform_msg.get("subject") or "")}

    def session_cwd(self, session_id: str) -> str:
        """★P1 resume cwd★：Codex `exec resume` 默认按 cwd 过滤会话。

        reuse 时必须用**建会话时的原 cwd**，不能用本次 stamp/workspace 否则
        resume 找不到会话（PROTOCOL §6.3）。查本地 session 账本 + native index。
        """
        sid = str(session_id or "").strip()
        if not sid:
            return ""
        for row in self._load_local_sessions():
            if str(row.get("sessionId") or "") == sid:
                cwd = str(row.get("cwd") or "").strip()
                if cwd and Path(cwd).is_dir():
                    return cwd
        for row in self._native_sessions():
            if str(row.get("sessionId") or "") == sid:
                cwd = str(row.get("cwd") or "").strip()
                if cwd and Path(cwd).is_dir():
                    return cwd
        return ""

    def route_from_stamp(self, envelope: Any) -> dict[str, Any]:
        meta = envelope.meta if isinstance(envelope.meta, dict) else {}
        stamp = meta.get("session") if isinstance(meta.get("session"), dict) else {}
        mode = str(stamp.get("mode") or "new").strip().lower()
        session_id = str(stamp.get("session_id") or "").strip()
        workspace = (str(stamp.get("workspace") or "").strip()
                     or str(meta.get("to_workspace") or "").strip()
                     or self.workspace)
        if not Path(workspace).is_dir():
            workspace = self.workspace
        if mode == "reuse" and session_id:
            # ★P1★：钉住原会话 cwd；找不到映射才退回 stamp workspace
            pinned = self.session_cwd(session_id)
            if pinned:
                workspace = pinned
            return {"mode": "reuse", "session_id": session_id,
                    "workspace": workspace,
                    "cwd_pinned": bool(pinned),
                    "reason": ("meta.session-reuse" if pinned
                               else "meta.session-reuse-cwd-fallback")}
        return {"mode": "new", "session_id": "", "workspace": workspace,
                "reason": "meta.session-new"}

    def build_prompt(self, native: dict[str, Any]) -> str:
        return "\n".join([
            "You are Codex handling one inbound ANP work message.",
            "Complete the requested work in the current workspace.",
            "Your final response is forwarded to the original sender as the ANP reply.",
            "Do not discuss this wrapper or invent a second messaging protocol.",
            "",
            "Inbound message:",
            json.dumps(native, ensure_ascii=False, indent=2),
        ])

    def _command(self, native: dict[str, Any], route: dict[str, Any]) -> list[str]:
        if not self.codex_bin:
            raise FileNotFoundError("Codex CLI not found; set ANP_CODEX_BIN")
        launcher = [self.codex_bin]
        wrapper = str(os.environ.get("ANP_CODEX_WRAPPER_SCRIPT") or "").strip()
        if wrapper:
            launcher.append(wrapper)
        common = ["--skip-git-repo-check", "--cd", route["workspace"], "--json"]
        if str(os.environ.get("ANP_CODEX_DANGEROUSLY_BYPASS", "0")).lower() in (
                "1", "true", "yes", "on"):
            common.append("--dangerously-bypass-approvals-and-sandbox")
        else:
            approval = os.environ.get("ANP_CODEX_ASK_APPROVAL")
            if approval:
                common.extend(["--ask-for-approval", approval])
        extra = str(os.environ.get("ANP_CODEX_EXTRA_ARGS") or "").strip()
        if extra:
            common.extend(shlex.split(extra, posix=False))
        prompt = self.build_prompt(native)
        if route["mode"] == "reuse":
            return [*launcher, "exec", "resume", *common,
                    route["session_id"], prompt]
        return [*launcher, "exec", *common, prompt]

    @staticmethod
    def _parse_json_events(stdout: str) -> tuple[str, str]:
        session_id = ""
        messages: list[str] = []
        for line in stdout.splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue
            if event.get("type") == "thread.started":
                session_id = str(event.get("thread_id") or session_id)
            if event.get("type") != "item.completed":
                continue
            item = event.get("item") if isinstance(event.get("item"), dict) else {}
            if item.get("type") == "agent_message" and item.get("text"):
                messages.append(str(item["text"]))
        return session_id, (messages[-1] if messages else "")

    def run(self, native: dict[str, Any], route: dict[str, Any]) -> dict[str, Any]:
        argv = self._command(native, route)
        env = dict(os.environ)
        env["ANP_CODEX_ADAPTER"] = "1"
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        self.log(f"launch mode={route['mode']} cwd={route['workspace']}")
        proc = subprocess.Popen(argv, cwd=route["workspace"], env=env,
                                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True,
                                encoding="utf-8", errors="replace",
                                creationflags=creationflags)
        try:
            stdout, stderr = proc.communicate(
                timeout=float(os.environ.get("ANP_CODEX_TIMEOUT_SEC") or 1800))
        except subprocess.TimeoutExpired:
            # ★P2★：超时必须杀子进程，否则残留 CLI 继续写 session/工作区
            try:
                proc.kill()
                stdout, stderr = proc.communicate(timeout=10)
            except Exception:  # noqa: BLE001
                stdout, stderr = "", ""
            self.log(f"codex timeout; killed pid={proc.pid}")
            return {"ok": False, "returncode": -1, "session_id": route.get("session_id") or "",
                    "reply": "", "error": "timeout",
                    "stderr": (stderr or "")[-2000:]}
        session_id, reply = self._parse_json_events(stdout or "")
        if not session_id:
            session_id = route.get("session_id") or ""
        if session_id:
            self.record_session(session_id, route["workspace"], running=False)
        if proc.returncode:
            self.log(f"codex exit={proc.returncode} stderr={(stderr or '')[-500:]}")
        return {"ok": proc.returncode == 0, "returncode": proc.returncode,
                "session_id": session_id, "reply": reply,
                "stderr": (stderr or "")[-2000:]}

    def should_reply(self, envelope: Any) -> bool:
        """Errata S4：只有 need_reply===true 才要求义务回信；缺省=知会。"""
        meta = envelope.meta if isinstance(envelope.meta, dict) else {}
        return meta.get("need_reply") is True

    def _load_work(self) -> list[dict[str, Any]]:
        path = self.data_dir / "codex-work.jsonl"
        if not path.is_file():
            return []
        rows: list[dict[str, Any]] = []
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(item, dict):
                    rows.append(item)
        except OSError:
            return []
        return rows

    def enqueue_work(self, envelope: Any, native: dict[str, Any],
                     route: dict[str, Any]) -> tuple[str, bool]:
        """S3-A：待办先持久化。返回 `(pending_id, is_new)`。

        ★H-6 真幂等★：is_new=False 表示同一 envelope 已在跑/已入队/已完结，
        调用方**不得**再拉起 platform.run（否则双跑双回信）。任意历史记录即视为已接受。
        """
        pending_id = str(envelope.id or f"work-{int(time.time()*1000)}")
        path = self.data_dir / "codex-work.jsonl"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        with self._work_lock:
            for row in self._load_work():
                if str(row.get("pending_id") or "") == pending_id:
                    return pending_id, False
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
            with path.open("a", encoding="utf-8") as fp:
                fp.write(json.dumps(row, ensure_ascii=False) + "\n")
                fp.flush()
        return pending_id, True

    def finish_work(self, pending_id: str, *, ok: bool, detail: str = "") -> None:
        path = self.data_dir / "codex-work.jsonl"
        row = {
            "pending_id": str(pending_id or ""),
            "state": "done" if ok else "failed",
            "ok": bool(ok),
            "detail": str(detail or "")[:500],
            "at": time.time(),
        }
        try:
            with self._work_lock:
                with path.open("a", encoding="utf-8") as fp:
                    fp.write(json.dumps(row, ensure_ascii=False) + "\n")
                    fp.flush()
        except OSError:
            self.log(f"finish_work write failed for {pending_id}")

    def reply_meta(self, envelope: Any, session_id: str, workspace: str) -> dict[str, Any]:
        incoming = envelope.meta if isinstance(envelope.meta, dict) else {}
        out = {"from_workspace": workspace}
        if incoming.get("context_id"):
            out["context_id"] = incoming["context_id"]
        if session_id:
            out["reply_to_session"] = session_id
        return out
