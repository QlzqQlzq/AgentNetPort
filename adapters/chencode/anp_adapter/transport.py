"""Transport 共性：把「这次唤醒从哪来」这个差异关在这里。

Adapter 的主逻辑不关心自己是什么 mode，它只问 transport 两个问题：
  "我的 wake 声明怎么写？"  /  "下一封信封从哪来？"
所以 mode 的差异**只在这一层**（Adapter Spec §4 + #6 的结论）。

三档对应 Spec §4：
  http   Core push      Core POST 信封到 adapter 的端点
  exec   Core launch    Core 每封信拉起一个进程，信封走环境变量
  queue  Core persist   Core 只落盘；Agent 稍后自己拉
"""
from __future__ import annotations

import json
import os
import queue
import socket
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .client import AnpClient
from .envelope import Envelope, from_wake_payload, DEFAULT_RESPONSE_MODE

# Spec §5.2：exec 档的环境变量名
ENV_WAKE_JSON = "AGENT_ANP_WAKE_JSON"
ENV_SELF = "AGENT_ANP_SELF"
ENV_SELF_KEY = "AGENT_ANP_SELF_KEY"
ENV_KEY = "AGENT_ANP_KEY"          # 既有约定名，与 SELF_KEY 同值


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class WakeTransport:
    """唤醒通道基类。子类只回答两个问题，其余一概不管。"""
    name = "?"

    def declare_wake(self, endpoint: str = "") -> dict:
        """注册时要写的 `wake` 字段。"""
        raise NotImplementedError

    def next_envelope(self, client: AnpClient,
                      timeout: float = 5.0) -> Envelope | None:
        """拿到下一封要处理的信封；取不到返回 None。"""
        raise NotImplementedError

    def stop(self) -> None:
        """有资源的子类在这里释放。"""


class HttpWake(WakeTransport):
    """http 档：Core 主动 POST 到我们起的端点（Spec §5.1）。"""
    name = "http"

    def __init__(self, on_envelope=None):
        self._on_envelope = on_envelope
        self.seen: "queue.Queue[Envelope]" = queue.Queue()
        self.server: ThreadingHTTPServer | None = None
        self.port = 0
        self.hits: list[dict] = []      # 最近几次唤醒的旁证，便于自检

    def declare_wake(self, endpoint: str = "") -> dict:
        return {"kind": "http", "url": f"http://127.0.0.1:{self.port}/anp/wake"}

    def next_envelope(self, client: AnpClient, timeout: float = 5.0):
        try:
            return self.seen.get(timeout=timeout)
        except queue.Empty:
            return None

    def handle(self, payload: dict, headers: dict) -> dict:
        """Core 打来 `/anp/wake` 时被调用。注册 on_envelope 回调即可接管。"""
        env, side = from_wake_payload(payload, headers)
        self.hits.append({"env": env, "side": side, "at": time.time()})
        if self._on_envelope is not None:
            return self._on_envelope(env, side)
        self.seen.put(env)
        # ★必须带合法 mode，否则 Core 不签收★（v0.1.1 errata B1）
        return {"ok": True, "mode": DEFAULT_RESPONSE_MODE,
                "envelope_id": env.id,
                "key_present": bool(side["agent_key"])}

    def start(self, port: int = 0) -> int:
        """起 HTTP 服务等 Core。★必须在 declare_wake() 之前调★——
        声明里要用端口。返回实际监听的端口。
        """
        self.port = port or free_port()
        transport = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self) -> None:  # noqa: N802
                if self.path.rstrip("/") != "/anp/wake":
                    self.send_error(404)
                    return
                raw = self.rfile.read(int(self.headers.get("content-length") or 0))
                try:
                    payload = json.loads(raw.decode("utf-8"))
                except (json.JSONDecodeError, UnicodeDecodeError):
                    payload = {}
                result = transport.handle(payload, dict(self.headers))
                body = json.dumps(result, ensure_ascii=False).encode("utf-8")
                self.send_response(200)
                # ★响应头必须纯 ASCII★：中文箭头会让 send_header 抛
                # UnicodeEncodeError，handler 半路死掉（2026-09-22 实测踩过）
                self.send_header("content-type", "application/json; charset=utf-8")
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = ThreadingHTTPServer(("127.0.0.1", self.port), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return self.port

    def stop(self) -> None:
        if self.server:
            try:
                self.server.shutdown()
                self.server.server_close()
            except Exception:  # noqa: BLE001 - 关服务失败不该影响退出
                pass
            self.server = None


# Spec §5.4：exec 描述的是 **Core 的唤醒动作**——每有一封信要投递，
# Core 拉起一个进程，把信封通过环境变量交给它，然后不再管它。
# exec **不定义 adapter 自己内部的宿主生命周期**：子进程可以再做任何事
# （包括自己起常驻服务），Core 既不期待也不依赖。
ENV_WAKE_SCRIPT = "AGENT_ANP_WAKE_SCRIPT"     # 可选：子进程要跑的脚本


class ExecWake(WakeTransport):
    """exec 档：Core 每封信拉起一个进程，信封走环境变量（Spec §5.2 / P0-5）。

    ★注意：Core 拉起的是**另一个进程**。所以本进程的 next_envelope 永远取不到
    东西——这正是 exec 档的契约。真正干活的是那个被拉起的子进程，
    用 `envelope_from_env()` 从环境变量取信封。
    """
    name = "exec"

    def __init__(self, argv: list[str] | None = None,
                 cwd: str = "", script: str = ""):
        self.argv = [str(a) for a in (argv or [])]
        self.cwd = str(cwd or "")
        self.script = str(script or os.environ.get(ENV_WAKE_SCRIPT) or "")

    def declare_wake(self, endpoint: str = "") -> dict:
        out = {"kind": "exec", "command": list(self.argv)}
        if self.cwd:
            out["cwd"] = self.cwd
        return out

    def next_envelope(self, client: AnpClient, timeout: float = 5.0):
        return None      # 见类注释：信封去了别的进程


def envelope_from_env(env: dict | None = None) -> Envelope | None:
    """exec 档子进程：从环境变量取信封（Spec §5.2）。

    Core 给三个变量：
      AGENT_ANP_WAKE_JSON  完整信封
      AGENT_ANP_SELF       收件人身份名
      AGENT_ANP_SELF_KEY   收件人令牌（与 AGENT_ANP_KEY 同值）
    ★后者是目标 Agent 的令牌，不是 adapter plugin 的★（P0-4）。
    """
    env = env if env is not None else os.environ
    raw = env.get(ENV_WAKE_JSON) or ""
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    return Envelope(payload)


def exec_identity(env: dict | None = None) -> dict:
    """exec 档子进程：取自己的身份名与令牌。"""
    env = env if env is not None else os.environ
    key = env.get(ENV_SELF_KEY) or env.get(ENV_KEY) or ""
    return {"who": str(env.get(ENV_SELF) or ""), "key": str(key),
            "hub": str(env.get("AGENT_ANP_HUB") or "http://127.0.0.1:8900")}


class QueueWake(WakeTransport):
    """queue 档：没有 push 目标，adapter 自己调 /messages/receive（Spec §4.1）。"""
    name = "queue"

    def declare_wake(self, endpoint: str = "") -> dict:
        return {"kind": "queue"}

    def next_envelope(self, client: AnpClient, timeout: float = 5.0):
        got = client.receive(peek=False, unread_only=True)
        msgs = got.get("messages") or []
        return Envelope(msgs[0]) if msgs else None


def probe(base: str, timeout: float = 2.0) -> bool:
    """hub 活着吗（GET /health）。纯粹的连通性探测，不是状态判断。"""
    try:
        with urllib.request.urlopen(str(base).rstrip("/") + "/health",
                                    timeout=timeout) as r:
            return r.status == 200
    except Exception:  # noqa: BLE001
        return False
