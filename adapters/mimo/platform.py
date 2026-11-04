"""MiMo 平台语义 —— ★这一层是 MiMo 专属知识，ANP 一概不知道★

从旧 `agent-mailbox/src/mailbox_mimo.py`（514 行）搬过来的**平台部分**。
ANP 通信部分不在这里（在 plugin.py，且只用 SDK）。

★这次迁移的纪律★：原样搬行为，不顺手重构。旧实现的每个档位行为都要能在新实现里找到对应。

档位（旧实现的约定，原样保留）：
  A  常驻端点可达        → serve 提供 wake 路由
  B  有 CLI               → 拉起 `mimo run` headless
  C  都没有               → 落"待取信摘要"，由会话侧周期性收信
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

# 旧实现的默认值，原样保留
DEFAULT_SERVE = os.environ.get("AGENT_MAILBOX_MIMO_SERVE") or ""
DEFAULT_HEALTH_PORT = int(os.environ.get("AGENT_MAILBOX_MIMO_HEALTH_PORT") or 43128)
CLI_CANDIDATES = [
    os.environ.get("AGENT_MAILBOX_MIMO_BIN") or "",
    shutil.which("mimo") or "",
    str(Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "mimo" / "mimo.exe"),
    str(Path.home() / ".local" / "bin" / "mimo.exe"),
]

INSTRUCTIONS = """你是 mimo（MiMo Desktop），正在通过 ANP（AgentNetPort）处理一封外部 agent 消息。

这次唤醒的完整信件已经由 Adapter 放在下面的 JSON 中，不要再用 mailbox、mail_inbox、
mail_send、bash、PowerShell、终端或文件系统去读取/发送它们。Adapter 会负责 ANP 的签收、
reply_to、correlation_id 和最终投递。

请直接理解信件正文并完成请求。若需要回复，只输出你给发件人的最终回复正文；
不要输出 JSON 信封，不要调用任何消息工具，不要回复“已收到”这类空话。
若信件不要求回复，完成必要工作后给出简短结果即可。
"""

# 旧实现的 caps 表，原样保留（档位 → caps）
CAPS_BY_TIER = {
    "A": {"inject_session": True, "spawn_cwd": True, "resume_session": False, "queue": True},
    "B": {"inject_session": False, "spawn_cwd": True, "resume_session": False, "queue": True},
    "C": {"inject_session": False, "spawn_cwd": False, "resume_session": False, "queue": True},
}


class MiMoPlatform:
    """MiMo Desktop 这一侧的一切。

    ★它不知道 ANP 的存在★：入站是"信封的字段"，出站是"原生回信的字段"。
    """

    def __init__(self, identity: str = "mimo", data_dir: Path | None = None):
        self.identity = identity
        self.workspace = os.environ.get("AGENT_MAILBOX_MIMO_CWD") or str(
            Path(__file__).resolve().parents[3])
        self.health_port = DEFAULT_HEALTH_PORT
        self.health_url = f"http://127.0.0.1:{self.health_port}/process/health"
        appdata = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        self.preferences_path = Path(
            os.environ.get("AGENT_MAILBOX_MIMO_PREFERENCES")
            or (Path(appdata) / "Xiaomi MiMo" / "preferences.json")
        )
        self.desktop_api_path = Path(
            os.environ.get("AGENT_ANP_MIMO_DESKTOP_API")
            or (Path(appdata) / "Xiaomi MiMo" / "desktop-api.json")
        )
        # ★平台状态放自己的目录★。旧实现把它写进 `<HOME>/mimo-pending.*`，
        # 那是 Core 的地盘——两套系统共管一个目录，#4.1 记下的 Spec 缺口之一。
        self.data_dir = data_dir or Path(__file__).resolve().parent / "data"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.pending_json = self.data_dir / "mimo-pending.json"
        self.pending_md = self.data_dir / "mimo-pending.md"
        self._log = self.data_dir / "mimo-wake.log"
        self.credentials_path = self.data_dir / "mimo-credentials.json"
        self._state_lock = threading.RLock()
        self._desktop_turn_lock = threading.RLock()
        self._desktop_inflight: set[str] = set()
        self._last_tier: dict[str, Any] | None = None

    def load_credentials(self) -> dict[str, Any]:
        try:
            data = json.loads(self.credentials_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def save_credentials(self, agent_key: str) -> None:
        """★S2-⑦a★：agent 令牌落盘，进程重启后才能续约（而不是再 bootstrap 被 401）。"""
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

    def agent_key(self) -> str:
        return str(self.load_credentials().get("agent_key") or "")

    # ---- 日志 ----
    def log(self, text: str) -> None:
        try:
            with self._log.open("a", encoding="utf-8") as fp:
                fp.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {text}\n")
        except OSError:
            pass

    # ---- 档位探测（纯平台知识）----

    def enqueue_work(self, envelope: Any, native: dict[str, Any]) -> tuple[str, bool]:
        """S3-A：待办先持久化（mimo-work.jsonl）。"""
        pending_id = str(getattr(envelope, "id", "") or f"work-{int(time.time()*1000)}")
        row = {"pending_id": pending_id, "state": "queued",
               "envelope_id": pending_id, "at": time.time(), "native": native}
        path = self.data_dir / "mimo-work.jsonl"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        with self._state_lock:
            if path.is_file():
                try:
                    for line in path.read_text(encoding="utf-8").splitlines():
                        try:
                            previous = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if (isinstance(previous, dict)
                                and str(previous.get("pending_id") or "") == pending_id):
                            return pending_id, False
                except OSError:
                    pass
            with path.open("a", encoding="utf-8") as fp:
                fp.write(json.dumps(row, ensure_ascii=False) + "\n")
                fp.flush()
        return pending_id, True

    def finish_work(self, pending_id: str, *, ok: bool, detail: str = "") -> None:
        """Append a terminal work record.

        Deferred Desktop API turns must use :meth:`mark_work` while they are
        being watched; ``finish_work`` is deliberately reserved for the final
        success/failure record so the journal never reports an accepted turn
        as completed before its reply exists.
        """
        self.mark_work(
            pending_id,
            state="done" if ok else "failed",
            ok=bool(ok),
            detail=detail,
        )

    def mark_work(self, pending_id: str, *, state: str,
                  ok: bool | None = None, detail: str = "") -> None:
        """Append a non-terminal or terminal work state to ``mimo-work.jsonl``."""
        path = self.data_dir / "mimo-work.jsonl"
        row = {"pending_id": str(pending_id or ""), "state": str(state or "unknown"),
               "detail": str(detail or "")[:500], "at": time.time()}
        if ok is not None:
            row["ok"] = bool(ok)
        try:
            with self._state_lock:
                with path.open("a", encoding="utf-8") as fp:
                    fp.write(json.dumps(row, ensure_ascii=False) + "\n")
                    fp.flush()
        except OSError:
            self.log(f"work state write failed for {pending_id}")

    def find_cli(self) -> str | None:
        for c in CLI_CANDIDATES:
            if c and Path(c).is_file() and not self._is_desktop_launcher(c):
                return c
        return None

    @staticmethod
    def _is_desktop_launcher(candidate: str) -> bool:
        path = Path(candidate)
        return (path.name.casefold() == "xiaomi mimo.exe"
                or any(part.casefold() == "xiaomi mimo" for part in path.parts))

    @staticmethod
    def extract_reply(stdout: str) -> str:
        """Return model text, excluding CLI diagnostics and unrecognized JSON."""
        raw = str(stdout or "").strip()
        if not raw:
            return ""
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            lines = [line for line in raw.splitlines()
                     if line.strip() and not line.lstrip().startswith("[api-base]")]
            return "\n".join(lines).strip()
        if isinstance(parsed, str):
            return parsed.strip()
        if isinstance(parsed, dict):
            for key in ("reply", "text", "result"):
                value = parsed.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
        return ""

    def probe_serve(self, url: str, timeout: float = 1.2) -> bool:
        if not url:
            return False
        base = url.rstrip("/")
        for path in ("/global/health", "/health", "/"):
            try:
                with urllib.request.urlopen(
                        urllib.request.Request(base + path, method="GET"),
                        timeout=timeout) as resp:
                    return 200 <= getattr(resp, "status", 200) < 500
            except Exception:  # noqa: BLE001
                continue
        return False

    def evolve_enabled(self) -> bool | None:
        """Read MiMo Desktop's opt-in bridge switch without touching the platform.

        ``evolveEnabled`` is a host setting, not a transport health signal.  A
        missing or malformed preferences file is deliberately reported as
        ``None`` so an installation with a different settings layout is not
        falsely classified as disabled.
        """
        try:
            data = json.loads(self.preferences_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(data, dict) or "evolveEnabled" not in data:
            return None
        return bool(data.get("evolveEnabled"))

    def desktop_api_config(self) -> dict[str, Any] | None:
        """Read MiMo Desktop's own loopback API credential file."""
        try:
            data = json.loads(self.desktop_api_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(data, dict):
            return None
        try:
            port = int(data.get("port") or 0)
        except (TypeError, ValueError):
            return None
        token = str(data.get("token") or "").strip()
        if not 1 <= port <= 65535 or not token:
            return None
        return {
            "api": int(data.get("api") or 1),
            "port": port,
            "token": token,
            "base_url": f"http://127.0.0.1:{port}",
        }

    def probe_desktop_api(self, config: dict[str, Any] | None = None,
                          timeout: float = 2.5) -> dict[str, Any] | None:
        """Return the official Desktop API health payload when it is usable."""
        config = config or self.desktop_api_config()
        if not config:
            return None
        request = urllib.request.Request(
            config["base_url"] + "/v1/health",
            headers={"Authorization": f"Bearer {config['token']}"},
            method="GET",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                payload = json.loads(response.read().decode("utf-8", "replace") or "{}")
        except Exception:  # noqa: BLE001
            return None
        if not isinstance(payload, dict) or payload.get("ok") is not True:
            return None
        if str(payload.get("engine") or "") not in ("ready", "idle"):
            return None
        return {**config, "health": payload}

    def detect_tier(self) -> dict[str, Any]:
        """原样搬旧实现的判定顺序：serve 可达优先，其次 CLI，最后 C 档。"""
        serve_url = DEFAULT_SERVE or "http://127.0.0.1:4096"
        cli = self.find_cli()
        if self.probe_serve(serve_url):
            result = {"tier": "A", "label": "http-wake",
                      "detail": f"serve 可达: {serve_url}", "cli": cli, "serve": serve_url}
            self._last_tier = result
            return result
        if cli:
            result = {"tier": "B", "label": "wake.exec-headless",
                      "detail": f"mimo CLI: {cli}", "cli": cli, "serve": ""}
            self._last_tier = result
            return result
        desktop_api = self.probe_desktop_api()
        if desktop_api:
            result = {
                "tier": "A",
                "label": "desktop-api",
                "detail": (
                    "MiMo Desktop 官方 loopback API 可达："
                    f"{desktop_api['base_url']}/v1"
                ),
                "cli": None,
                "serve": "",
                "desktop_api": desktop_api,
            }
            self._last_tier = result
            return result
        evolve = self.evolve_enabled()
        if evolve is False:
            detail = (
                "MiMo Desktop 的 evolveEnabled=false：桌面桥未启用，"
                "信件已落盘，需启用 Evolve/桥后由会话取信"
            )
        else:
            detail = "无常驻端点、无 CLI：落盘待取信，由 MiMo 会话周期性收信"
        result = {"tier": "C", "label": "queue-poll",
                  "detail": detail, "cli": None, "serve": "",
                  "evolve_enabled": evolve}
        self._last_tier = result
        return result

    def caps_for(self, tier: str) -> dict[str, bool]:
        return dict(CAPS_BY_TIER.get(tier, CAPS_BY_TIER["C"]))

    def process_api_config(self) -> dict[str, Any]:
        # MiMo Desktop does not expose the historical 43128 health endpoint on
        # every installation.  Keep the registered process surface reachable
        # through the adapter itself; the concrete platform URL remains in
        # `health_url` for diagnostics.
        port = os.environ.get("ANP_PLUGIN_PORT") or ""
        url = (f"http://127.0.0.1:{port}/anp/platform-health" if port
               else self.health_url)
        return {"kind": "http", "url": url, "timeout_sec": 1.5,
                "platform_url": self.health_url}

    # ---- 待取信摘要（C 档的产出物）----
    def write_pending(self, messages: list[dict], tier: dict, wake_json: Any) -> None:
        with self._state_lock:
            self.pending_json.write_text(json.dumps({
                "identity": self.identity, "tier": tier, "at": time.time(),
                "unread": len(messages), "messages": messages, "wake_json": wake_json,
                "poll_hint": "在 MiMo 会话里调用 ANP: receive(peek=true) 或 receive()",
            }, ensure_ascii=False, indent=2), encoding="utf-8")

        lines = [f"# mimo 待取信摘要（{time.strftime('%Y-%m-%d %H:%M:%S')}）", "",
                 f"- 推送档位: **{tier['tier']}**（{tier['label']}）",
                 f"- 说明: {tier['detail']}", f"- 未读: {len(messages)} 封", ""]
        if messages:
            lines += ["| id | from | subject | need_reply |", "| --- | --- | --- | --- |"]
            for m in messages:
                meta = m.get("meta") if isinstance(m.get("meta"), dict) else {}
                need = bool(meta.get("need_reply") or m.get("need_reply"))
                subj = str(m.get("subject") or "").replace("|", "\\|")
                lines.append(f"| {m.get('id')} | {m.get('from')} | {subj} | "
                             f"{'是' if need else '否'} |")
            lines += ["", "完整正文请用 ANP 的 receive 读取（peek 可先看列表）。"]
        else:
            lines.append("当前没有未读。")
        if wake_json:
            lines += ["", "## 触发唤醒的投递信息", "", "```json",
                      json.dumps(wake_json, ensure_ascii=False, indent=2), "```"]
        with self._state_lock:
            self.pending_md.write_text("\n".join(lines) + "\n", encoding="utf-8")

    # ---- 提示词（★旧实现的 build_prompt，属于平台语义★）----
    def build_prompt(self, messages: list[dict], wake_json: Any) -> str:
        """旧实现的提示词模板原样搬。

        ★这条 answering #4.1 的第 4 问★：`build_prompt` 是**平台工作流语义**，
        归 adapter 所有，Core 与 Spec 都不该有它。
        """
        lines = [INSTRUCTIONS, "```json",
                 json.dumps(wake_json or {}, ensure_ascii=False, indent=2), "```", ""]
        lines.append(f"（本次未读共 {len(messages)} 封，概要：）")
        for m in messages:
            lines.append(f"- id={m.get('id')} from={m.get('from')} "
                         f"subject={m.get('subject') or '(无)'}")
        return "\n".join(lines)

    def should_reply(self, envelope: Any) -> bool:
        """S4：只有显式 need_reply=true 才进入需要平台产出的路径。"""
        meta = getattr(envelope, "meta", None)
        meta = meta if isinstance(meta, dict) else {}
        return meta.get("need_reply") is True

    # ---- B 档：拉起 CLI ----
    def run_headless(self, cli: str, messages: list[dict], wake_json: Any) -> dict:
        env = {**os.environ, "AGENT_MAILBOX_SELF": self.identity,
               "AGENT_ANP_HUB": os.environ.get("AGENT_ANP_HUB", "http://127.0.0.1:8900")}
        argv = [cli, "run", "--cd", self.workspace]
        if str(os.environ.get("AGENT_ANP_MIMO_DANGEROUSLY_BYPASS", "0")).lower() in (
                "1", "true", "yes", "on"):
            argv.append("--yolo")
        argv.append(self.build_prompt(messages, wake_json))
        timeout = float(os.environ.get("AGENT_ANP_MIMO_TIMEOUT_SEC") or 900)
        try:
            proc = subprocess.run(  # noqa: S603 - 命令来自本机平台探测
                argv, cwd=self.workspace, env=env, stdin=subprocess.DEVNULL,
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=timeout, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except subprocess.TimeoutExpired:
            self.log(f"B 档 headless 超时（{timeout}s）")
            return {"ok": False, "tier": "B", "returncode": -1,
                    "reply": "", "error": f"timeout after {timeout}s"}
        stdout = proc.stdout or ""
        stderr = proc.stderr or ""
        reply = self.extract_reply(stdout)
        self.log(f"B 档 headless 完成 rc={proc.returncode}")
        if proc.returncode == 0 and not reply:
            self.log("B 档 headless 未返回可回收的回复正文")
            return {"ok": False, "tier": "B", "returncode": proc.returncode,
                    "reply": "", "error": "CLI completed without a usable reply"}
        return {"ok": proc.returncode == 0, "tier": "B",
                "returncode": proc.returncode, "reply": reply,
                "stdout": stdout[-4000:], "stderr": stderr[-2000:]}

    # ---- A 档：POST 到 serve ----
    def announce_serve(self, serve_url: str, messages: list[dict],
                       wake_json: Any) -> dict:
        """★注意平台侧的载荷形状★：`message` 是 **JSON 字符串**，
        与 ANP 的 `/anp/wake`（`message` 是信封对象）不同——这正是 adapter 要转换的东西。
        """
        body = {"message": json.dumps(wake_json or {}, ensure_ascii=False),
                "to": self.identity, "unread": len(messages)}
        raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
        for path in ("/mail/wake", "/session/wake"):
            try:
                with urllib.request.urlopen(urllib.request.Request(
                        serve_url.rstrip("/") + path, data=raw, method="POST",
                        headers={"content-type": "application/json; charset=utf-8"}),
                        timeout=3) as resp:
                    data = json.loads(resp.read().decode("utf-8", "replace") or "{}")
                    self.log(f"A 档 HTTP wake {path} → {data}")
                    return {"ok": True, "tier": "A", "result": data}
            except Exception as e:  # noqa: BLE001
                self.log(f"A 档 HTTP wake {path} 失败: {e}")
        return {"ok": False, "tier": "A", "error": "serve 未提供 wake 路由，降级到 C"}

    def _desktop_api_request(self, config: dict[str, Any], path: str,
                             *, method: str = "GET", body: dict | None = None,
                             timeout: float = 15.0) -> Any:
        raw = None
        headers = {"Authorization": f"Bearer {config['token']}",
                   "accept": "application/json"}
        if body is not None:
            raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["content-type"] = "application/json; charset=utf-8"
        request = urllib.request.Request(
            config["base_url"] + path, data=raw, headers=headers, method=method)
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8", "replace") or "{}")

    @staticmethod
    def _session_updated(session: dict[str, Any]) -> float:
        time_data = session.get("time") if isinstance(session, dict) else {}
        if not isinstance(time_data, dict):
            return 0.0
        try:
            return float(time_data.get("updated") or time_data.get("created") or 0)
        except (TypeError, ValueError):
            return 0.0

    def choose_desktop_session(self, sessions: list[dict[str, Any]]) -> dict[str, Any] | None:
        """Pick a real existing session; never create a shadow session."""
        wanted = str(os.environ.get("AGENT_ANP_MIMO_SESSION_ID") or "").strip()
        if wanted:
            for session in sessions:
                if str(session.get("id") or "") == wanted:
                    return session
        workspace = os.path.normcase(os.path.abspath(self.workspace))

        def score(session: dict[str, Any]) -> tuple[int, float]:
            directory = str(session.get("directory") or "")
            title = str(session.get("title") or "")
            norm_dir = os.path.normcase(os.path.abspath(directory)) if directory else ""
            points = 0
            if norm_dir == workspace:
                points += 1000
            if workspace and norm_dir.startswith(workspace + os.sep):
                points += 250
            haystack = f"{directory}\n{title}".casefold()
            if "agentnetport" in haystack or "anp" in haystack:
                points += 500
            return points, self._session_updated(session)

        usable = [s for s in sessions if isinstance(s, dict) and s.get("id")]
        return max(usable, key=score) if usable else None

    @staticmethod
    def _desktop_message_created(message: dict[str, Any]) -> float:
        info = message.get("info") if isinstance(message, dict) else {}
        stamp = info.get("time") if isinstance(info, dict) else {}
        if not isinstance(stamp, dict):
            return 0.0
        try:
            return float(stamp.get("created") or stamp.get("updated") or 0)
        except (TypeError, ValueError):
            return 0.0

    @classmethod
    def desktop_session_state(cls, messages: list[dict[str, Any]]) -> tuple[bool, str]:
        """Classify whether the official session can accept another turn."""
        if not isinstance(messages, list) or not messages:
            return False, "empty"
        ordered = sorted(
            (item for item in messages if isinstance(item, dict)),
            key=cls._desktop_message_created,
        )
        for item in reversed(ordered):
            parts = item.get("parts")
            if isinstance(parts, list):
                for part in parts:
                    if not isinstance(part, dict) or part.get("type") != "tool":
                        continue
                    state = part.get("state")
                    state = state if isinstance(state, dict) else {}
                    if str(state.get("status") or "").casefold() in {"running", "pending"}:
                        return True, "tool-running"
            info = item.get("info")
            info = info if isinstance(info, dict) else {}
            role = str(info.get("role") or "").casefold()
            if role == "user":
                return True, "user-awaiting-assistant"
            if role != "assistant":
                continue
            finish = str(info.get("finish") or item.get("finish") or "").casefold()
            if finish in {"tool-calls", "tool_use", "tool-calls-start"}:
                return True, f"finish={finish}"
            if finish in {"error", "cancelled", "aborted"}:
                return False, f"terminal={finish}"
            if cls._assistant_text(item):
                return False, "assistant-text"
            completed = info.get("time")
            completed = completed if isinstance(completed, dict) else {}
            if completed.get("completed"):
                return False, "assistant-completed"
            return True, "assistant-incomplete"
        return False, "idle"

    def release_desktop_turn(self, session_id: str) -> None:
        with self._desktop_turn_lock:
            self._desktop_inflight.discard(str(session_id or ""))

    @staticmethod
    def _assistant_text(message: dict[str, Any]) -> str:
        parts = message.get("parts") if isinstance(message, dict) else []
        if not isinstance(parts, list):
            return ""
        texts = [
            str(part.get("text") or "").strip()
            for part in parts
            if isinstance(part, dict) and part.get("type") == "text"
        ]
        return "\n".join(text for text in texts if text).strip()

    def send_desktop_turn(self, messages: list[dict], wake_json: Any) -> dict[str, Any]:
        """Wake a real MiMo session through its official local API."""
        config = (self._last_tier or {}).get("desktop_api") or self.desktop_api_config()
        if not config:
            return {"ok": False, "tier": "A", "error": "MiMo Desktop API unavailable"}
        try:
            sessions = self._desktop_api_request(config, "/v1/sessions?limit=200",
                                                 timeout=10.0)
            if not isinstance(sessions, list):
                raise RuntimeError("MiMo Desktop API returned an invalid session list")
            wanted = str(os.environ.get("AGENT_ANP_MIMO_SESSION_ID") or "").strip()
            candidates = [s for s in sessions if isinstance(s, dict) and s.get("id")]
            if wanted:
                candidates = [s for s in candidates if str(s.get("id")) == wanted]
            else:
                workspace = os.path.normcase(os.path.abspath(self.workspace))

                def score(session: dict[str, Any]) -> tuple[int, float]:
                    directory = str(session.get("directory") or "")
                    title = str(session.get("title") or "")
                    norm_dir = os.path.normcase(os.path.abspath(directory)) if directory else ""
                    points = 0
                    if norm_dir == workspace:
                        points += 1000
                    if workspace and norm_dir.startswith(workspace + os.sep):
                        points += 250
                    haystack = f"{directory}\n{title}".casefold()
                    if "agentnetport" in haystack or "anp" in haystack:
                        points += 500
                    return points, self._session_updated(session)

                candidates.sort(key=score, reverse=True)
            selected: tuple[dict[str, Any], list[dict[str, Any]], str] | None = None
            busy_reasons: list[str] = []
            for candidate in candidates:
                candidate_id = str(candidate.get("id") or "")
                if not candidate_id:
                    continue
                with self._desktop_turn_lock:
                    if candidate_id in self._desktop_inflight:
                        busy_reasons.append(f"{candidate_id}:adapter-inflight")
                        continue
                before = self._desktop_api_request(
                    config,
                    f"/v1/sessions/{urllib.parse.quote(candidate_id, safe='')}/messages",
                    timeout=15.0)
                before = before if isinstance(before, list) else []
                busy, reason = self.desktop_session_state(before)
                if busy:
                    busy_reasons.append(f"{candidate_id}:{reason}")
                    self.log(f"MiMo Desktop API session={candidate_id} skipped ({reason})")
                    continue
                selected = (candidate, before, reason)
                break
            if not selected:
                detail = ", ".join(busy_reasons) or "no-existing-session"
                return {"ok": False, "tier": "A",
                        "error": f"MiMo has no idle session ({detail})",
                        "reason": "desktop-session-busy"}
            session, before, session_state = selected
            session_id = str(session["id"])
            before_ids = {
                str(item.get("info", {}).get("id") or "")
                for item in before
                if isinstance(item, dict)
            }
            model = str(os.environ.get("AGENT_ANP_MIMO_MODEL") or "").strip()
            if not model:
                try:
                    prefs = json.loads(self.preferences_path.read_text(encoding="utf-8"))
                    model = str(prefs.get("model") or "").strip()
                except (OSError, json.JSONDecodeError):
                    model = ""
            model = model or "mimo-v2.6-pro"
            turn_started_ms = time.time() * 1000
            body = {"message": self.build_prompt(messages, wake_json), "model": model}
            with self._desktop_turn_lock:
                if session_id in self._desktop_inflight:
                    return {"ok": False, "tier": "A",
                            "error": f"MiMo session already claimed: {session_id}",
                            "reason": "desktop-session-busy"}
                self._desktop_inflight.add(session_id)
            try:
                self._desktop_api_request(
                    config,
                    f"/v1/sessions/{urllib.parse.quote(session_id, safe='')}/turns",
                    method="POST", body=body, timeout=20.0)
            except Exception:
                self.release_desktop_turn(session_id)
                raise
            timeout = float(os.environ.get("AGENT_ANP_MIMO_DESKTOP_TIMEOUT_SEC") or 120)
            # The Desktop API turn is asynchronous.  Waiting here exceeds ANP
            # Core's HTTP wake budget and makes a healthy accepted turn look
            # like a delivery failure, which triggers duplicate wake attempts.
            # The adapter owns the reply watcher and reaps the final assistant
            # text after the HTTP acknowledgement has already been returned.
            self.log(f"MiMo Desktop API session={session_id} accepted turn")
            return {
                "ok": True, "tier": "A", "deferred": True,
                "session_id": session_id, "model": model,
                "before_ids": sorted(before_ids),
                "turn_started_ms": turn_started_ms,
                "timeout_sec": max(1.0, timeout),
                "reason": "desktop-session-accepted",
                "session_state_before": session_state,
            }
        except Exception as exc:  # noqa: BLE001
            self.log(f"MiMo Desktop API delivery failed: {type(exc).__name__}: {exc}")
            return {"ok": False, "tier": "A",
                    "error": f"{type(exc).__name__}: {exc}"}

    # ---- 通知 ----
    def toast(self, title: str, body: str) -> None:
        try:
            env = {**os.environ, "ANP_TOAST_TITLE": str(title),
                   "ANP_TOAST_BODY": str(body)}
            subprocess.run(  # noqa: S603
                ["powershell", "-NoProfile", "-Command",
                 "Add-Type -AssemblyName System.Windows.Forms; "
                 "$n=New-Object System.Windows.Forms.NotifyIcon; "
                 "$n.Icon=[System.Drawing.SystemIcons]::Information; "
                 "$n.Visible=$true; "
                 "$n.ShowBalloonTip(5000,$env:ANP_TOAST_TITLE,$env:ANP_TOAST_BODY,"
                 "[System.Windows.Forms.ToolTipIcon]::Info); "
                 "Start-Sleep -Milliseconds 250"],
                env=env, check=False, timeout=5,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception as e:  # noqa: BLE001
            self.log(f"toast 失败（忽略）: {e}")

    # ---- 进程健康 ----
    _desktop_cache: dict[str, Any] = {"ts": 0.0, "ok": False, "pids": []}

    def desktop_running(self) -> tuple[bool, list[int]]:
        now = time.time()
        if now - float(self._desktop_cache.get("ts") or 0) < 2.0:
            return bool(self._desktop_cache.get("ok")), list(self._desktop_cache.get("pids") or [])
        pids: list[int] = []
        ok = False
        try:
            proc = subprocess.run(  # noqa: S603
                ["powershell", "-NoProfile", "-Command",
                 "Get-Process -Name 'Xiaomi MiMo' -ErrorAction SilentlyContinue "
                 "| Select-Object -ExpandProperty Id"],
                capture_output=True, timeout=1.2, text=True,
                encoding="utf-8", errors="replace",
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            for line in (proc.stdout or "").split():
                if line.strip().isdigit():
                    pids.append(int(line.strip()))
            ok = len(pids) > 0
        except Exception:  # noqa: BLE001
            ok, pids = False, []
        self._desktop_cache.update({"ts": now, "ok": ok, "pids": pids})
        return ok, pids

    def health_payload(self) -> dict[str, Any]:
        desktop_ok, pids = self.desktop_running()
        tier = self._last_tier or self.detect_tier()
        evolve = tier.get("evolve_enabled", self.evolve_enabled())
        ready = str(tier.get("tier") or "") in ("A", "B")
        if ready:
            status = "ready"
        elif desktop_ok and evolve is False:
            status = "desktop_present_bridge_disabled"
        elif desktop_ok:
            status = "desktop_present_queue_only"
        else:
            status = "desktop_down"
        return {
            "ok": ready,
            "name": self.identity,
            "pid": os.getpid(),
            "status": status,
            "desktop_pids": pids,
            "desktop_running": bool(desktop_ok),
            "evolve_enabled": evolve,
            "bridge_reachable": str(tier.get("tier") or "") == "A",
            "cli_available": str(tier.get("tier") or "") == "B",
            "tier": tier,
            "health_url": self.health_url,
            "ts": time.time(),
        }


    def route_from_stamp(self, envelope: Any) -> dict[str, Any]:
        """S1：只执行 Core 的 meta.session 章，不自建决策。"""
        meta = envelope.meta if isinstance(getattr(envelope, "meta", None), dict) else {}
        stamp = meta.get("session") if isinstance(meta.get("session"), dict) else {}
        mode = str(stamp.get("mode") or stamp.get("action") or "").strip().lower()
        session_id = str(stamp.get("session_id") or "").strip()
        if mode == "reuse" and session_id:
            return {"mode": "reuse", "session_id": session_id, "reason": "meta.session-reuse"}
        return {"mode": "new", "session_id": "", "reason": "meta.session-new"}
